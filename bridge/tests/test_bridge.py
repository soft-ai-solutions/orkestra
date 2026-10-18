"""Bridge tests: real HTTP server subprocess + fake claude + local callback receiver (no network)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["ORKESTRA_NO_DAEMON"] = "1"      # no test starts the detached fast watcher (fastwatch.ensure_running)
BRIDGE = HERE.parent / "orkestra_bridge.py"
FAKE = HERE / "fake_claude.py"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Receiver(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        Receiver.got.append(json.loads(self.rfile.read(n)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-test-")).resolve()
        cls.home = cls.tmp / "home"
        cls.ws = cls.tmp / "Projects"
        (cls.ws / "demo").mkdir(parents=True)
        (cls.ws / "demo" / "AGENTS.md").write_text("# demo\n")
        subprocess.run(["git", "init", "-q", str(cls.ws / "demo")], check=True)
        (cls.ws / "demo2").mkdir()
        (cls.ws / "demo2" / "AGENTS.md").write_text("# demo2\n")
        subprocess.run(["git", "init", "-q", str(cls.ws / "demo2")], check=True)
        (cls.ws / "secret-proj").mkdir()
        (cls.ws / "demo3").mkdir()
        (cls.ws / "demo3" / "AGENTS.md").write_text("# demo3\n")
        subprocess.run(["git", "init", "-q", str(cls.ws / "demo3")], check=True)
        for extra in ("demo4", "demo5"):
            (cls.ws / extra).mkdir()
            (cls.ws / extra / "AGENTS.md").write_text(f"# {extra}\n")
            subprocess.run(["git", "init", "-q", str(cls.ws / extra)], check=True)
        cls.cb_port = free_port()
        cls.receiver = HTTPServer(("127.0.0.1", cls.cb_port), Receiver)
        threading.Thread(target=cls.receiver.serve_forever, daemon=True).start()
        cls.port = free_port()
        cls.token = "test-token-123"
        cls.home.mkdir()
        (cls.home / "bridge.secret.json").write_text(json.dumps({
            "port": cls.port, "token": cls.token, "workspace": str(cls.ws),
            "claude_bin": str(FAKE), "callback_url": f"http://127.0.0.1:{cls.cb_port}/cb",
            "allowed_projects": ["demo", "demo2", "demo3", "demo4", "demo5"],
            "watchdog_interval": 0, "auto_resume_delays": [0, 0, 0],
            "telegram": {"bot_token": "", "allowed_chat_ids": ["42"]}}))
        # The assertions below predate the English default and read Turkish bridge text: pinned like plugins/orkestra/tests
        # (helpers.py). English is covered in test_bridge_i18n.py.
        env = dict(os.environ, ORKESTRA_HOME=str(cls.home), ORKESTRA_WORKSPACE=str(cls.ws), ORKESTRA_LANG="tr")
        for k in ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "ORKESTRA_HEADLESS"):
            env.pop(k, None)
        cls.proc = subprocess.Popen([sys.executable, str(BRIDGE), "serve"], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.receiver.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, token=True):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait_status(self, job_id, want, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, job = self.call("GET", f"/jobs/{job_id}")
            if job.get("status") == want:
                return job
            time.sleep(0.2)
        self.fail(f"job {job_id} never reached {want}; last: {job}")

    def test_1_health_and_auth(self):
        self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health").status, 200)
        self.assertEqual(self.call("GET", "/jobs", token=False)[0], 401)
        self.assertEqual(self.call("POST", "/jobs", {"project": "demo", "request": "x"}, token=False)[0], 401)

    def test_local_ui_assets_and_legal_links(self):
        base = f"http://127.0.0.1:{self.port}"
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
            self.assertIn("default-src 'none'", response.headers.get("Content-Security-Policy", ""))
            self.assertIn("script-src 'self'", response.headers.get("Content-Security-Policy", ""))
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("id=\"job-form\"", html)
        self.assertIn("id=\"flows-list\"", html)
        self.assertIn("/legal/third-party-notices", html)
        for path, marker in (("/app.css", "--accent"), ("/app.js", "Bearer ${state.token}"),
                             ("/legal/license", "MIT License"),
                             ("/legal/third-party-notices", "https://github.com/obra/superpowers"),
                             ("/legal/security", "not a sandbox")):
            with urllib.request.urlopen(base + path, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(marker, response.read().decode("utf-8"))
        request = urllib.request.Request(base + "/", headers={"Host": "example.invalid"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        with urllib.request.urlopen(base + "/app.js", timeout=3) as response:
            self.assertIn('api("/runs")', response.read().decode("utf-8"))
        with urllib.request.urlopen(base + "/app.css", timeout=3) as response:
            self.assertIn("flow-stage-list", response.read().decode("utf-8"))

    def test_ui_project_and_engine_options_are_authenticated_and_scoped(self):
        self.assertEqual(self.call("GET", "/projects", token=False)[0], 401)
        code, projects = self.call("GET", "/projects")
        self.assertEqual(code, 200)
        self.assertTrue(projects["restricted"])
        self.assertEqual({item["name"] for item in projects["projects"]},
                         {"demo", "demo2", "demo3", "demo4", "demo5"})
        self.assertNotIn(str(self.ws), json.dumps(projects))
        code, engines = self.call("GET", "/engines?project=demo")
        self.assertEqual(code, 200)
        self.assertEqual(engines["default"], "claude")
        self.assertEqual({item["id"] for item in engines["engines"]}, {"claude", "cursor", "copilot", "pi"})
        self.assertTrue(all(isinstance(item["available"], bool) for item in engines["engines"]))
        self.assertEqual(self.call("GET", "/engines?project=../etc")[0], 400)

    def test_0_job_can_be_cancelled_by_id(self):
        code, job = self.call("POST", "/jobs", {"project": "demo3", "request": "öncelik ekle; ben karar verici olayım"})
        self.assertEqual(code, 202, job)
        self.wait_status(job["id"], "awaiting_user")
        self.assertEqual(self.call("POST", f"/jobs/{job['id']}/cancel", {}, token=False)[0], 401)
        code, cancelled = self.call("POST", f"/jobs/{job['id']}/cancel", {})
        self.assertEqual(code, 202, cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.call("POST", f"/jobs/{job['id']}/cancel", {})[0], 409)

    def test_2_project_validation(self):
        for bad in ("../etc", "/etc", "nope", "", "a b"):
            code, body = self.call("POST", "/jobs", {"project": bad, "request": "bir şey yap"})
            self.assertEqual(code, 400, (bad, body))

    def test_3_job_lifecycle(self):
        code, job = self.call("POST", "/jobs", {"project": "demo", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "42"})
        self.assertEqual(code, 202, job)
        job = self.wait_status(job["id"], "awaiting_user")
        self.assertIn("Varsayılan öncelik", job["last_message"])
        self.assertEqual(job["phase"], "intake")
        # a second job for the same project is refused while one is open
        code, body = self.call("POST", "/jobs", {"project": "demo", "request": "başka iş"})
        self.assertEqual(code, 400)
        self.assertIn("açık bir iş", body["error"])
        # callback delivered
        deadline = time.time() + 5
        while time.time() < deadline and not Receiver.got:
            time.sleep(0.1)
        self.assertTrue(any(p.get("id") == job["id"] and "Yanıtını bekliyor" in p.get("message", "") for p in Receiver.got))
        # reply by chat id → fake claude blocks the run → status maps to blocked
        code, _ = self.call("POST", "/reply", {"chat_id": "42", "text": "engelle"})
        self.assertEqual(code, 202)
        self.wait_status(job["id"], "blocked")
        code, listing = self.call("GET", "/jobs?chat_id=42")
        self.assertTrue(any(j["id"] == job["id"] for j in listing["jobs"]))

    def test_4_telegram_command_language(self):
        code, body = self.call("POST", "/telegram", {"chat_id": "42", "text": "/yap"})
        self.assertEqual(code, 200)
        self.assertIn("/yap claude:|cursor:|copilot:|pi:", body["message"])
        code, body = self.call("POST", "/telegram", {"chat_id": "42", "text": "/durum"})
        self.assertEqual(code, 200)
        self.assertIn("Canlı çalışmalar", body["message"])
        code, body = self.call("GET", "/runs")
        self.assertEqual(code, 200)
        self.assertIsInstance(body["runs"], list)
        self.assertEqual(self.call("GET", "/runs", token=False)[0], 401)
        code, body = self.call("POST", "/telegram", {"chat_id": "999", "text": "/durum"})
        self.assertEqual(code, 403)
        code, body = self.call("POST", "/telegram", {"chat_id": "42", "text": "/yap ../etc kötü"})
        self.assertIn("Başlatılamadı", body["message"])


    def test_5_dash_reply_and_allowlist(self):
        code, body = self.call("POST", "/jobs", {"project": "secret-proj", "request": "bir şey yap"})
        self.assertEqual(code, 400)
        self.assertIn("allowed_projects", body["error"])
        code, job = self.call("POST", "/jobs", {"project": "demo2", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "77"})
        self.assertEqual(code, 202)
        self.wait_status(job["id"], "awaiting_user")
        code, _ = self.call("POST", f"/jobs/{job['id']}/reply", {"text": "- T2'yi çıkar"})
        self.assertEqual(code, 202)
        deadline = time.time() + 20
        while time.time() < deadline:
            j = self.call("GET", f"/jobs/{job['id']}")[1]
            if "devam ediyorum: - T2" in (j.get("last_message") or ""):
                break
            time.sleep(0.2)
        self.assertIn("devam ediyorum: - T2", j.get("last_message"))


    def test_6_client_cli(self):
        client = HERE.parent / "orkestra_client.py"
        env = dict(os.environ, ORKESTRA_HOME=str(self.home), ORKESTRA_LANG="tr")
        def run(*a):
            return subprocess.run([sys.executable, str(client), *a], capture_output=True, text=True, env=env)
        cp = run("start", "demo3", "bir", "şey", "yap", "ben", "karar", "verici", "olayım",
             "--chat-id", "55", "--source", "openclaw")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        job = json.loads(cp.stdout)
        out = run("wait", job["id"], "--timeout", "30").stdout
        self.assertIn("awaiting_user", out)
        self.assertEqual(run("status", job["id"]).returncode, 0)
        self.assertEqual(run("start").returncode, 2)
        cp = run("runs")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("demo3", cp.stdout)
        self.assertIsInstance(json.loads(run("runs", "--json").stdout)["runs"], list)

    def test_7_stalled_job_is_resumed_on_the_same_session(self):
        code, job = self.call("POST", "/jobs", {"project": "demo4", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "81"})
        self.assertEqual(code, 202, job)
        self.wait_status(job["id"], "awaiting_user")
        sid_before = json.loads((self.home / "bridge" / "jobs" / f"{job['id']}.json").read_text())["session_id"]
        # the next turn ends while the run is still active → the bridge continues it by itself with /orkestra:resume
        self.assertEqual(self.call("POST", f"/jobs/{job['id']}/reply", {"text": "takıl"})[0], 202)
        deadline = time.time() + 30
        j = {}
        while time.time() < deadline:
            j = json.loads((self.home / "bridge" / "jobs" / f"{job['id']}.json").read_text())
            if j.get("status") == "awaiting_user" and "kaldığım yerden" in (j.get("last_message") or ""):
                break
            time.sleep(0.2)
        self.assertIn("kaldığım yerden devam ettim", j.get("last_message") or "", j)
        prompts = [t["prompt"] for t in j["turns"]]
        self.assertEqual(prompts[-2:], ["takıl", "/orkestra:resume"], prompts)
        self.assertEqual(j["session_id"], sid_before)          # same session: nothing runs twice in parallel
        self.assertEqual(j.get("auto_resumes"), 1)

    def test_8_auto_resume_is_capped_and_then_asks_the_user(self):
        code, job = self.call("POST", "/jobs", {"project": "demo5", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "82"})
        self.assertEqual(code, 202, job)
        self.wait_status(job["id"], "awaiting_user")
        self.assertEqual(self.call("POST", f"/jobs/{job['id']}/reply", {"text": "hep takıl"})[0], 202)
        deadline = time.time() + 40
        j = {}
        while time.time() < deadline:
            j = json.loads((self.home / "bridge" / "jobs" / f"{job['id']}.json").read_text())
            if j.get("status") == "awaiting_user" and "sınırı aşıldı" in (j.get("last_message") or ""):
                break
            time.sleep(0.2)
        self.assertIn("Otomatik devam sınırı aşıldı", j.get("last_message") or "", j)
        prompts = [t["prompt"] for t in j["turns"]]
        self.assertEqual(prompts.count("/orkestra:resume"), 3, prompts)     # default max_auto_resumes
        # a human reply starts a fresh budget (the run stalls again here, so it is continued again — up to the cap)
        self.assertEqual(self.call("POST", f"/jobs/{job['id']}/reply", {"text": "devam"})[0], 202)
        deadline = time.time() + 30
        n = 0
        while time.time() < deadline and n < 6:
            j = json.loads((self.home / "bridge" / "jobs" / f"{job['id']}.json").read_text())
            n = [t["prompt"] for t in j["turns"]].count("/orkestra:resume")
            time.sleep(0.2)
        self.assertEqual(n, 6)

    def test_9_a_cancel_during_the_turn_end_wins(self):
        """Reading the run state at turn end takes a subprocess: a cancel in that window must not be overwritten."""
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import orkestra_bridge as B\n"
                "job = {'id': 'aaaa11112222', 'status': 'running', 'turns': [{'at': 'x', 'prompt': 'p'}], 'project': 'demo'}\n"
                "B.save_job(job)\n"
                "B.save_job(dict(job, status='cancelled'))\n"
                "late = dict(job, status='running', auto_resumes=1)\n"
                "print(json.dumps([B._save_unless_cancelled(late), B.load_job('aaaa11112222')['status'],\n"
                "                  B._save_unless_cancelled(dict(job, id='bbbb11112222', status='awaiting_user')),\n"
                "                  B.load_job('bbbb11112222')['status']]))\n")
        home = self.tmp / "home-race"
        home.mkdir(exist_ok=True)
        cp = subprocess.run([sys.executable, "-c", code, str(BRIDGE.parent)], capture_output=True, text=True,
                            env=dict(os.environ, ORKESTRA_HOME=str(home), ORKESTRA_WORKSPACE=str(self.ws)))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(json.loads(cp.stdout.strip().splitlines()[-1]), [False, "cancelled", True, "awaiting_user"])

    def test_10_a_narrow_locale_does_not_break_a_request_with_turkish_letters(self):
        """The agent is fed and read as UTF-8 whatever the locale (Windows ANSI code pages, LC_ALL=C): the prompt used to
        raise UnicodeEncodeError in the wait thread, which died before updating the job, so it stayed "running" for ever
        and blocked its project."""
        script = self.tmp / "utf8_claude.py"
        script.write_text("import json, sys\n"
                          "text = sys.stdin.buffer.read().decode('utf-8')\n"
                          "print(json.dumps({'type': 'result', 'result': 'echo:' + text, 'session_id': 'sess-10'}))\n",
                          encoding="utf-8")
        if os.name == "nt":                 # CreateProcess cannot start a .py file: a .cmd wrapper stands in for the CLI
            fake = self.tmp / "utf8_claude.cmd"
            fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
        else:
            script.write_text("#!" + sys.executable + "\n" + script.read_text(encoding="utf-8"), encoding="utf-8")
            script.chmod(0o755)
            fake = script
        code = ("import json, sys, time; sys.path.insert(0, sys.argv[1]); import orkestra_bridge as B\n"
                "cfg = {'claude_bin': sys.argv[3], 'permission_mode': 'auto', 'max_turns': 3, 'extra_args': []}\n"
                "job = {'id': 'dddd11112222', 'status': 'new', 'turns': [], 'project': 'demo', 'project_path': sys.argv[2],\n"
                "       'session_id': 'sess-10', 'engine': 'claude'}\n"
                "B.run_turn(cfg, job, 'istek: T\\u00fcrk\\u00e7e \\u015f\\u011f\\u0130\\u0131 \\u2713', new=True)\n"
                "for _ in range(150):\n"
                "    cur = B.load_job('dddd11112222')\n"
                "    if cur['status'] != 'running':\n"
                "        break\n"
                "    time.sleep(0.1)\n"
                "print(json.dumps([cur['status'], cur.get('last_message')]))\n")
        home = self.tmp / "home-narrow"
        home.mkdir(exist_ok=True)
        env = dict(os.environ, ORKESTRA_HOME=str(home), ORKESTRA_WORKSPACE=str(self.ws), LC_ALL="C",
                   PYTHONCOERCECLOCALE="0", PYTHONUTF8="0", PYTHONIOENCODING="utf-8")
        env.pop("ORKESTRA_HEADLESS", None)
        cp = subprocess.run([sys.executable, "-c", code, str(BRIDGE.parent), str(self.ws / "demo2"), str(fake)],
                            capture_output=True, env=env, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace"))
        status, message = json.loads(cp.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(status, "done", f"{message}\n{cp.stderr.decode('utf-8', 'replace')}")
        self.assertEqual(message, "echo:istek: Türkçe şğİı ✓")


if __name__ == "__main__":
    unittest.main()
