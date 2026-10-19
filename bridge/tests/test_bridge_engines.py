"""Bridge + engines: a job runs with Claude (default) or Cursor, chosen per request. Real HTTP server subprocess,
fake `claude` and fake `agent` CLIs, the real orkestra CLI and the real Cursor hook (no network, no ~/.cursor)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["ORKESTRA_NO_DAEMON"] = "1"      # no test starts the detached fast watcher (fastwatch.ensure_running)
BRIDGE = HERE.parent / "orkestra_bridge.py"
PLUGIN = HERE.parent.parent / "plugins" / "orkestra"
sys.path.insert(0, str(PLUGIN / "lib"))

PROJECTS = ["demo", "demo2", "demo3", "demo4", "demo5", "demo6", "cursor", "copilot", "copilot2"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class BridgeEnginesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-engines-")).resolve()
        cls.home, cls.ws, cls.chome = cls.tmp / "home", cls.tmp / "Projects", cls.tmp / "cursor-home"
        cls.copilot_plugin = cls.tmp / "copilot-plugin"
        copilot_hooks = cls.copilot_plugin / "com.github.copilot" / "hooks" / "hooks.json"
        copilot_hooks.parent.mkdir(parents=True)
        (cls.copilot_plugin / "plugin.json").write_text(json.dumps({"name": "orkestra"}))
        hook_script = PLUGIN / "hooks" / "copilot_hook.py"
        copilot_hooks.write_text(json.dumps({"hooks": {
            event: [{"command": f"{sys.executable} {hook_script} {event}", "timeoutSec": 20}]
            for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse")
        }}))
        cls.fake_copilot = cls.tmp / "fake_copilot.py"
        cls.fake_copilot.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\n"
            "args = sys.argv[1:]\n"
            "sid = next(a.split('=', 1)[1] for a in args if a.startswith(('--session-id=', '--resume=')))\n"
            "with open(os.path.join(os.getcwd(), '.fake-copilot.jsonl'), 'a') as f: f.write(json.dumps(args) + '\\n')\n"
            "print(json.dumps({'type': 'result', 'result': 'copilot-ok', 'session_id': sid}))\n")
        cls.fake_copilot.chmod(0o755)
        for name in PROJECTS:
            (cls.ws / name).mkdir(parents=True)
            (cls.ws / name / "AGENTS.md").write_text(f"# {name}\n")
            subprocess.run(["git", "init", "-q", str(cls.ws / name)], check=True)
        cls.port, cls.token = free_port(), "test-token-456"
        cls.home.mkdir()
        (cls.home / "bridge.secret.json").write_text(json.dumps({
            "port": cls.port, "token": cls.token, "workspace": str(cls.ws), "claude_bin": str(HERE / "fake_claude.py"),
            "cursor_bin": str(HERE / "fake_agent.py"), "copilot_bin": str(cls.fake_copilot),
            "copilot_plugin_dir": str(cls.copilot_plugin), "allowed_projects": PROJECTS, "watchdog_interval": 0,
            "auto_resume_delays": [0, 0, 0], "telegram": {"bot_token": "", "allowed_chat_ids": ["42"]}}))
        # Turkish assertions below predate the English default; English is covered in test_bridge_i18n.py.
        cls.env = dict(os.environ, ORKESTRA_HOME=str(cls.home), ORKESTRA_WORKSPACE=str(cls.ws),
                       ORKESTRA_CURSOR_HOME=str(cls.chome), ORKESTRA_SHIM_DIR=str(cls.tmp / "no-shim"),
                       ORKESTRA_LANG="tr")
        for k in ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "ORKESTRA_CURSOR_BIN", "ORKESTRA_CLAUDE_BIN",
                  "ORKESTRA_HEADLESS"):
            cls.env.pop(k, None)
        cls.install_hooks()
        cls.proc = subprocess.Popen([sys.executable, str(BRIDGE), "serve"], env=cls.env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def install_hooks(cls):
        cp = subprocess.run([sys.executable, str(PLUGIN / "bin" / "orkestra"), "cursor", "install"], env=cls.env,
                            capture_output=True, text=True)
        # the wording follows the language (this suite runs in Turkish): the file is what counts
        hooks = cls.chome / "hooks.json"
        assert cp.returncode == 0 and hooks.exists() and "cursor_hook.py" in hooks.read_text(encoding="utf-8"), cp.stdout + cp.stderr

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def job_file(self, job_id):
        return json.loads((self.home / "bridge" / "jobs" / f"{job_id}.json").read_text())

    def wait_for(self, job_id, check, timeout=40):
        deadline = time.time() + timeout
        j = {}
        while time.time() < deadline:
            j = self.job_file(job_id)
            if check(j):
                return j
            time.sleep(0.2)
        self.fail(f"job {job_id} never got there; last: {j}")

    def run_state(self, project):
        states = sorted((self.ws / project / ".orkestra" / "runs").glob("*/state.json"))
        return json.loads(states[-1].read_text())

    def test_1_cursor_job_from_start_to_reply_and_auto_resume(self):
        code, job = self.call("POST", "/jobs", {"project": "demo", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "42",
                                                "engine": "cursor"})
        self.assertEqual(code, 202, job)
        self.assertEqual(job["engine"], "cursor")
        j = self.wait_for(job["id"], lambda j: j.get("status") == "awaiting_user")
        self.assertIn("Varsayılan öncelik", j["last_message"])
        chat = j["session_id"]
        st = self.run_state("demo")
        self.assertEqual((st["engine"], st["mode"], st["status"]), ("cursor", "headless", "awaiting_user"))
        self.assertTrue((self.home / "sessions" / f"cursor-{chat}.json").exists())      # bound as a Cursor conversation
        self.assertFalse((self.home / "sessions" / f"{chat}.json").exists())
        turns = [json.loads(x) for x in (self.ws / "demo" / ".fake-agent.jsonl").read_text().splitlines()]
        self.assertEqual(turns[0]["args"][:7], ["-p", "--output-format", "json", "--force", "--trust", "--resume", chat])
        self.assertIn("orkestra playbook run", turns[0]["args"][-1])
        self.assertTrue(turns[0]["args"][-1].endswith("İstek: öncelik ekle; ben karar verici olayım"))
        self.assertEqual(turns[0]["stdin"], "")                                         # the prompt is never on stdin
        self.assertEqual(j["turns"][0]["prompt"], "/orkestra-run öncelik ekle; ben karar verici olayım")
        # a second job for the same project is refused while one is open — whatever the engine
        code, body = self.call("POST", "/jobs", {"project": "demo", "request": "başka iş", "engine": "claude"})
        self.assertEqual(code, 400)
        # the user's reply reaches the hook layer through the driver (print mode fires no prompt hook)…
        code, _ = self.call("POST", "/reply", {"chat_id": "42", "text": "mavi olsun"})
        self.assertEqual(code, 202)
        # …the turn ends while the run is active → continued on the SAME chat with Cursor's resume prompt
        j = self.wait_for(job["id"], lambda j: j.get("status") == "awaiting_user" and "kaldığım yerden" in (j.get("last_message") or ""))
        self.assertEqual([t["prompt"] for t in j["turns"]][-2:], ["mavi olsun", "/orkestra-resume"])
        self.assertEqual((j["session_id"], j.get("auto_resumes")), (chat, 1))
        turns = [json.loads(x) for x in (self.ws / "demo" / ".fake-agent.jsonl").read_text().splitlines()]
        self.assertEqual([t["args"][6] for t in turns], [chat] * 3)
        self.assertEqual(turns[1]["args"][-1], "mavi olsun")
        self.assertIn("orkestra resume", turns[2]["args"][-1])
        rows = [json.loads(x) for x in next((self.ws / "demo" / ".orkestra" / "runs").glob("*/activity.jsonl")).read_text().splitlines()]
        self.assertTrue(any(r.get("kind") == "user" and "mavi olsun" in (r.get("what") or "") for r in rows), rows[-5:])
        code, listing = self.call("GET", f"/jobs/{job['id']}")
        self.assertEqual(listing["engine"], "cursor")

    def test_2_engine_word_in_the_chat_command(self):
        def yap(text):
            code, body = self.call("POST", "/telegram", {"chat_id": "42", "text": text})
            self.assertEqual(code, 200, body)
            return body["message"]

        msg = yap("/yap cursor demo2 öncelik ekle; ben karar verici olayım")
        self.assertIn("Başladım: [demo2]", msg)
        self.assertIn("· cursor", msg)
        msg = yap("/yap Cursor: demo3 öncelik ekle; ben karar verici olayım")
        self.assertIn("[demo3]", msg)
        self.assertIn("· cursor", msg)
        msg = yap("/yap claude demo4 öncelik ekle; ben karar verici olayım")
        self.assertIn("[demo4]", msg)
        self.assertNotIn("cursor", msg)
        msg = yap("/yap demo5 öncelik ekle; ben karar verici olayım")  # no engine word: claude
        self.assertNotIn("cursor", msg)
        msg = yap("/yap cursor öncelik ekle; ben karar verici olayım")  # a project that is itself called "cursor"
        self.assertIn("Başladım: [cursor]", msg)
        self.assertNotIn("· cursor", msg)
        self.assertIn("/yap claude:|cursor:|copilot:|pi: <proje> <istek>", yap("/yap cursor: demo6"))
        self.assertIn("Başlatılamadı", yap("/yap cursor: nope bir şey yap"))
        self.assertIn("/yap claude:|cursor:|copilot:|pi:", yap("/yardim"))
        code, listing = self.call("GET", "/jobs?chat_id=42")
        engines = {j["project"]: j["engine"] for j in listing["jobs"]}
        for project, engine in (("demo2", "cursor"), ("demo3", "cursor"), ("demo4", "claude"), ("demo5", "claude"),
                                ("cursor", "claude")):
            self.assertEqual(engines.get(project), engine, engines)
        for project in ("demo2", "demo3", "demo4", "demo5", "cursor"):
            jid = [j["id"] for j in listing["jobs"] if j["project"] == project][-1]
            self.wait_for(jid, lambda j: j.get("status") == "awaiting_user")
        self.assertEqual(self.run_state("demo2")["engine"], "cursor")
        self.assertEqual(self.run_state("demo4")["engine"], "claude")
        self.assertEqual(self.run_state("demo5")["engine"], "claude")

    def test_copilot_engine_word_in_the_telegram_command(self):
        code, body = self.call("POST", "/telegram", {
            "chat_id": "42", "text": "/yap copilot: copilot2 küçük bir iş yap"})
        self.assertEqual(code, 200, body)
        self.assertIn("· copilot", body["message"])
        _, listing = self.call("GET", "/jobs?chat_id=42")
        job = [j for j in listing["jobs"] if j.get("project") == "copilot2"][-1]
        self.wait_for(job["id"], lambda value: value.get("status") == "done")

    def test_3_cursor_is_refused_without_its_hooks_and_unknown_engines(self):
        hooks = self.chome / "hooks.json"
        saved = hooks.read_text()
        try:
            hooks.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}))
            code, body = self.call("POST", "/jobs", {"project": "demo6", "request": "bir şey yap", "engine": "cursor"})
            self.assertEqual(code, 400, body)
            self.assertIn("orkestra cursor install", body["error"])                     # never an unguarded Cursor agent
        finally:
            hooks.write_text(saved)
        self.assertFalse((self.ws / "demo6" / ".fake-agent.jsonl").exists())
        code, body = self.call("POST", "/jobs", {"project": "demo6", "request": "bir şey yap", "engine": "gemini"})
        self.assertEqual(code, 400)
        self.assertIn("bilinmeyen motor", body["error"])
        self.assertEqual([p.name for p in (self.home / "bridge" / "jobs").glob("*.json")
                          if json.loads(p.read_text()).get("project") == "demo6"], [])

    def test_4_defaults_and_command_parsing(self):
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import orkestra_bridge as B\n"
                "cfg = B.load_config()\n"
                "out = [B.pick_engine(cfg, None), B.pick_engine(dict(cfg, default_engine='claude'), None),\n"
                "       B.pick_engine(cfg, 'claude'), list(B.split_engine(cfg, 'demo', 'x y')),\n"
                "       list(B.split_engine(cfg, 'cursor', 'demo x y')), list(B.split_engine(cfg, 'claude:', 'demo x y')),\n"
                "       list(B.split_engine(cfg, 'cursor', 'cursor x y')), list(B.split_engine(cfg, 'cursor', 'x y'))]\n"
                "print(json.dumps(out))\n")
        home = self.tmp / "home-default"
        home.mkdir(exist_ok=True)
        shutil.copy(self.home / "bridge.secret.json", home / "bridge.secret.json")
        (home / "config.json").write_text(json.dumps({"engine": {"default": "cursor"}}))
        cp = subprocess.run([sys.executable, "-c", code, str(BRIDGE.parent)], capture_output=True, text=True,
                            env=dict(self.env, ORKESTRA_HOME=str(home)))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        got = json.loads(cp.stdout.strip().splitlines()[-1])
        self.assertEqual(got[:3], ["cursor", "claude", "claude"])       # orkestra config · bridge override · the request
        self.assertEqual(got[3], [None, "demo", "x y"])
        self.assertEqual(got[4], ["cursor", "demo", "x y"])
        self.assertEqual(got[5], ["claude", "demo", "x y"])
        self.assertEqual(got[6], ["cursor", "cursor", "x y"])
        self.assertEqual(got[7], [None, "cursor", "x y"])               # the project named "cursor", default engine

    def test_copilot_engine_honors_explicit_model_and_effort(self):
        code, catalog = self.call("GET", "/engines?project=demo")
        self.assertEqual(code, 200)
        copilot = next(item for item in catalog["engines"] if item["id"] == "copilot")
        self.assertTrue(copilot["available"], copilot)

        code, job = self.call("POST", "/jobs", {"project": "copilot", "request": "kısa bir iş; use gpt-6-astra with reasoning effort high",
                              "engine": "copilot"})
        self.assertEqual(code, 202, job)
        finished = self.wait_for(job["id"], lambda value: value.get("status") == "done")
        self.assertEqual(finished["engine"], "copilot")
        args = json.loads((self.ws / "copilot" / ".fake-copilot.jsonl").read_text().splitlines()[0])
        self.assertIn("--allow-all-tools", args)
        self.assertIn("--autopilot", args)
        self.assertIn("--no-ask-user", args)
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")
        self.assertEqual(args[args.index("--reasoning-effort") + 1], "high")
        self.assertEqual(args[args.index("--plugin-dir") + 1], str(self.copilot_plugin))

    @unittest.skipUnless(os.name != "nt", "#!/bin/sh non-exec fixture (FR-007)")
    def test_6_a_turn_that_cannot_start_never_leaves_the_job_running(self):
        notx = self.tmp / "not-executable-agent"
        notx.write_text("#!/bin/sh\n")
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import orkestra_bridge as B\n"
                "cfg = B.load_config(); cfg['cursor_bin'] = sys.argv[2]; cfg['callback_url'] = ''\n"
                "job = {'id': 'cccc11112222', 'status': 'awaiting_user', 'turns': [], 'project': 'demo6', 'engine': 'cursor',\n"
                "       'project_path': sys.argv[3], 'session_id': '0a0a0a0a-1111-4222-8333-444455556666', 'chat_id': None}\n"
                "B.save_job(job)\n"
                "B.run_turn(cfg, job, 'devam', new=False)\n"
                "j = B.load_job('cccc11112222'); print(json.dumps([j['status'], j['last_message']]))\n")
        home = self.tmp / "home-popen"
        home.mkdir(exist_ok=True)
        shutil.copy(self.home / "bridge.secret.json", home / "bridge.secret.json")
        cp = subprocess.run([sys.executable, "-c", code, str(BRIDGE.parent), str(notx), str(self.ws / "demo6")],
                            capture_output=True, text=True, env=dict(self.env, ORKESTRA_HOME=str(home)))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        status, message = json.loads(cp.stdout.strip().splitlines()[-1])
        self.assertEqual(status, "error")                                   # not "running" for ever
        self.assertIn("başlatılamadı", message)

    def test_7_hooks_are_checked_before_every_cursor_turn(self):
        code, job = self.call("POST", "/jobs", {"project": "demo6", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "66",
                                                "engine": "cursor"})
        self.assertEqual(code, 202, job)
        self.wait_for(job["id"], lambda j: j.get("status") == "awaiting_user")
        hooks = self.chome / "hooks.json"
        saved = hooks.read_text()
        turns_before = len((self.ws / "demo6" / ".fake-agent.jsonl").read_text().splitlines())
        try:
            hooks.write_text(json.dumps({"version": 1, "hooks": {}}))          # uninstalled while the job waits
            self.assertEqual(self.call("POST", f"/jobs/{job['id']}/reply", {"text": "mavi"})[0], 202)
            j = self.wait_for(job["id"], lambda j: "orkestra cursor install" in (j.get("last_message") or ""))
            self.assertEqual(j["status"], "awaiting_user")
            self.assertEqual(len((self.ws / "demo6" / ".fake-agent.jsonl").read_text().splitlines()), turns_before)
        finally:
            hooks.write_text(saved)
        self.assertEqual(self.call("POST", f"/jobs/{job['id']}/reply", {"text": "mavi"})[0], 202)       # installed again
        self.wait_for(job["id"], lambda j: "kaldığım yerden" in (j.get("last_message") or ""))
        rows = [json.loads(x) for x in next((self.ws / "demo6" / ".orkestra" / "runs").glob("*/activity.jsonl")).read_text().splitlines()]
        self.assertIn("idle", [r["kind"] for r in rows])                       # a print-mode turn ends in the log

    def test_5_doctor_mentions_the_optional_engine(self):
        # stdin is a pipe nobody closes — a terminal. 2026-10-06: install.sh, run by hand, stopped here: the doctor's
        # `claude plugin list` inherited the keyboard, waited 60 s and the doctor died with a traceback.
        proc = subprocess.Popen([sys.executable, str(BRIDGE), "doctor"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)
        try:
            deadline = time.time() + 45
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            self.assertIsNotNone(proc.poll(), "doctor waits for input")
            out = proc.stdout.read()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdin.close(); proc.stdout.close(); proc.stderr.close()
            proc.wait()
        self.assertIn("[OK] cursor motoru", out)
        self.assertIn("bridge çalışıyor", out)


if __name__ == "__main__":
    unittest.main()
