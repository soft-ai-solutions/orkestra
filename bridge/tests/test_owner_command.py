"""Owner commands end to end on a real bridge with a recording stand-in for `claude -p`: the job runs on the claude
engine with the owner model flags, the session carries the owner grant while the job lives, a question is answered
with the recommended option instead of waiting (a bounded number of times), only OpenClaw / the command line may ask
for it, and cancelling removes the grant. Plus the client's `--owner`, `ensure` and `revive`."""
from __future__ import annotations

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / "orkestra_bridge.py"
CLIENT = HERE.parent / "orkestra_client.py"
ORK = HERE.parents[1] / "plugins" / "orkestra" / "bin" / "orkestra"
sys.path.insert(0, str(HERE.parent.parent / "plugins" / "orkestra" / "lib"))

FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
home = Path(os.environ["ORKESTRA_HOME"])
grant = (home / "grants" / "owner" / f"{sid}.json").exists()
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt, "grant": grant}) + "\n")
def ork(*a):
    subprocess.run([sys.executable, os.environ["ORK_BIN"], *a, "-p", os.getcwd()], capture_output=True, text=True)
if prompt.startswith("/orkestra:run") and "stall" not in prompt:
    ork("block", "the signing keychain is locked")      # autopilot stops only on a real external dependency
    result = "blocked: keychain locked"
else:
    result = "half way"                                 # the run stays active: a stall
print(json.dumps({"type": "result", "result": result, "session_id": sid}))
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OwnerJobE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="owner-e2e-")).resolve()
        cls.home, cls.ws = cls.tmp / "home", cls.tmp / "Projects"
        cls.proj = cls.ws / "demo"
        cls.proj.mkdir(parents=True)
        (cls.proj / "AGENTS.md").write_text("# demo\n")
        subprocess.run(["git", "init", "-q", str(cls.proj)], check=True)
        cls.home.mkdir()
        cls.fake = cls.tmp / "fake_claude.py"
        cls.fake.write_text(FAKE)
        cls.fake.chmod(0o755)
        cls.log = cls.tmp / "calls.jsonl"
        cls.port = free_port()
        cls.token = "owner-token-" + "x" * 20
        (cls.home / "bridge.secret.json").write_text(json.dumps({
            "port": cls.port, "token": cls.token, "workspace": str(cls.ws), "claude_bin": str(cls.fake),
            "allowed_projects": ["demo"], "watchdog_interval": 0, "auto_resume_delays": [0, 0, 0],
            "owner_max_auto_resumes": 4, "telegram": {"bot_token": "", "allowed_chat_ids": []}}))
        env = dict(os.environ, ORKESTRA_HOME=str(cls.home), ORKESTRA_WORKSPACE=str(cls.ws), ORKESTRA_LANG="en",
                   FAKE_LOG=str(cls.log), ORK_BIN=str(ORK))
        for k in ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "ORKESTRA_HEADLESS"):
            env.pop(k, None)
        cls.env = env
        cls.proc = subprocess.Popen([sys.executable, str(BRIDGE), "serve"], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def api(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def start(self, request, **extra):
        code, job = self.api("POST", "/jobs", {"project": "demo", "request": request, **extra})
        self.assertEqual(code, 202, job)
        self.addCleanup(self.api, "POST", f"/jobs/{job['id']}/cancel", {})
        return job

    def stored(self, job_id):
        return json.loads((self.home / "bridge" / "jobs" / f"{job_id}.json").read_text())

    def settle(self, job_id):
        deadline = time.time() + 60
        while time.time() < deadline:
            _, cur = self.api("GET", f"/jobs/{job_id}")
            if cur["status"] not in ("running", "queued"):
                return cur
            time.sleep(0.2)
        self.fail(f"job {job_id} still running")

    def test_only_openclaw_or_the_command_line_may_ask_for_an_owner_job(self):
        code, body = self.api("POST", "/jobs", {"project": "demo", "request": "make the button blue and ship it",
                                                "source": "n8n", "owner": True})
        self.assertEqual(code, 400)
        self.assertIn("owner command", body["error"])

    def test_owner_job_runs_with_the_best_model_and_the_grant_until_it_ends(self):
        before = len(self.calls())
        job = self.start("make the button blue and ship it", engine="cursor", chat_id="42", source="openclaw",
                         owner=True)
        self.assertTrue(self.stored(job["id"]).get("owner"))
        self.assertEqual(job["engine"], "claude")          # the owner always gets the claude engine (best model)
        cur = self.settle(job["id"])
        self.assertEqual(cur["status"], "blocked", cur)
        first = self.calls()[before]
        for flag, value in (("--model", "fable"), ("--fallback-model", "opus"), ("--effort", "xhigh")):
            self.assertEqual(first["args"][first["args"].index(flag) + 1], value)
        self.assertIn("[Owner command:", first["prompt"])
        self.assertTrue(first["grant"])                    # the turn ran with the owner grant
        from orkestra_core import intent
        self.assertNotIn("force", intent.git_grants(first["prompt"]))
        self.assertFalse(intent.manual_approval_requested(first["prompt"]))

        sid = self.stored(job["id"])["session_id"]
        grant = self.home / "grants" / "owner" / f"{sid}.json"
        self.assertTrue(grant.exists())                    # blocked: the owner may still answer, the grant stays
        code, _ = self.api("POST", f"/jobs/{job['id']}/cancel", {})
        self.assertIn(code, (200, 202))
        deadline = time.time() + 30                       # the cancel is carried out in the background
        while grant.exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(grant.exists())

    def test_a_stalled_owner_job_is_continued_longer_than_a_plain_one(self):
        before = len(self.calls())
        owner = self.start("stall on the owner job", chat_id="42", source="openclaw", owner=True)
        self.assertEqual(self.settle(owner["id"])["status"], "awaiting_user")
        owner_calls = self.calls()[before:]
        self.assertEqual(len(owner_calls), 1 + 4, owner_calls)        # owner_max_auto_resumes
        self.assertTrue(all("--model" in c["args"] and c["grant"] for c in owner_calls))
        self.api("POST", f"/jobs/{owner['id']}/cancel", {})

        before = len(self.calls())
        plain = self.start("stall on the plain job", source="api")
        self.assertFalse(self.stored(plain["id"]).get("owner"))
        self.assertEqual(self.settle(plain["id"])["status"], "awaiting_user")
        plain_calls = self.calls()[before:]
        self.assertEqual(len(plain_calls), 1 + 3, plain_calls)        # watchdog.max_auto_resumes default
        self.assertFalse(any("--model" in c["args"] or c["grant"] for c in plain_calls))


class Fake:
    def __init__(self, port: int):
        self.hits = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.hits.append(json.loads(self.rfile.read(n) or b"{}"))
                body = b'{"id": "abc", "owner": true}'
                self.send_response(202)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ClientOwnerAndRevive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="client-owner-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.port = free_port()
        (self.tmp / "bridge.secret.json").write_text(json.dumps({"port": self.port, "token": "t-1"}))
        patcher = mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.tmp), "ORKESTRA_LANG": "en"})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.path.insert(0, str(HERE.parent))
        import importlib
        import orkestra_client
        self.C = importlib.reload(orkestra_client)          # HOME is read at import

    def test_start_with_owner_sends_the_flag_and_keeps_it_out_of_the_request(self):
        fake = Fake(self.port)
        self.addCleanup(fake.close)
        cp = subprocess.run([sys.executable, str(CLIENT), "start", "demo", "ship", "it", "--owner", "--chat-id", "42",
                             "--source", "openclaw"], capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(fake.hits[0]["owner"], True)
        self.assertEqual(fake.hits[0]["request"], "ship it")

    def test_ensure_reports_a_running_bridge_and_starts_a_missing_one(self):
        fake = Fake(self.port)
        self.assertEqual(self.C.ensure_bridge()["state"], "running")
        fake.close()
        started = []

        def spawn(argv, log):
            started.append(argv)
            self.addCleanup(Fake(self.port).close)

        with mock.patch.object(self.C.sys, "platform", "linux"), mock.patch.object(self.C, "_spawn", spawn):
            out = self.C.ensure_bridge(wait=5)
        self.assertEqual(out["state"], "started")
        self.assertTrue(started[0][-2].endswith("orkestra_bridge.py"))

    def test_ensure_on_macos_asks_launchd_first(self):
        runs = []

        def run(argv, timeout=20):
            runs.append(argv)
            if argv[:2] == ["launchctl", "kickstart"]:
                self.addCleanup(Fake(self.port).close)
                return 0
            return 1

        with mock.patch.object(self.C.sys, "platform", "darwin"), mock.patch.object(self.C, "_run", run), \
                mock.patch.object(self.C, "_spawn", lambda *a: self.fail("no second bridge next to launchd")):
            out = self.C.ensure_bridge(wait=5)
        self.assertEqual((out["state"], out["how"]), ("started", "launchd"))
        self.assertEqual(runs[0][:2], ["launchctl", "kickstart"])
        self.assertNotIn("-k", runs[0])                     # never restarts a running bridge

    def test_apps_are_checked_before_they_are_opened(self):
        self.assertEqual(self.C.ensure_app("../evil")["state"], "invalid")
        self.assertEqual(self.C.ensure_app("Surely Not Installed 123")["state"],
                         "missing" if sys.platform == "darwin" else "unsupported")
        runs = []
        with mock.patch.object(self.C.sys, "platform", "darwin"), \
                mock.patch.object(self.C.Path, "is_dir", lambda p: True), \
                mock.patch.object(self.C, "_app_running", lambda bundle: False), \
                mock.patch.object(self.C, "_run", lambda argv, timeout=20: runs.append(argv) or 0):
            self.assertEqual(self.C.ensure_app("Cursor")["state"], "started")
        self.assertEqual([r[0] for r in runs], ["open"])
        ps = "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper.app/Contents/MacOS/Cursor Helper\n" \
             "/Applications/Cursor.app/Contents/MacOS/Cursor\n"
        with mock.patch.object(self.C.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, ps, "")):
            self.assertTrue(self.C._app_running(Path("/Applications/Cursor.app")))
        with mock.patch.object(self.C.subprocess, "run",
                               lambda *a, **k: subprocess.CompletedProcess(a, 0, ps.split("\n")[0], "")):
            self.assertFalse(self.C._app_running(Path("/Applications/Cursor.app")))   # a helper is not the app

    def test_revive_reports_every_tool(self):
        with mock.patch.object(self.C, "ensure_bridge", lambda: {"state": "running"}), \
                mock.patch.object(self.C, "ensure_dashboard", lambda: {"state": "started"}), \
                mock.patch.object(self.C, "ensure_app", lambda a: {"app": a, "state": "failed"}):
            out = self.C.revive(["Cursor"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["apps"], [{"app": "Cursor", "state": "failed"}])

    def test_revive_command_line_with_a_dangling_app_flag_ends(self):
        cp = subprocess.run([sys.executable, "-c",
                             "import sys; sys.argv=['x']; sys.path.insert(0, sys.argv[0] if False else %r)\n"
                             "import orkestra_client as C\n"
                             "C.ensure_bridge=lambda: {'state':'running'}\n"
                             "C.ensure_dashboard=lambda: {'state':'running'}\n"
                             "sys.exit(C.main(['revive','--app']))" % str(HERE.parent)],
                            capture_output=True, text=True, env=dict(os.environ), timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(json.loads(cp.stdout)["apps"], [])


if __name__ == "__main__":
    unittest.main()
