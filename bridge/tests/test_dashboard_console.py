"""The dashboard console against the real run service: a bridge subprocess with the fake `claude`, driven only through
orkestra_core.console and monitor.control_run (what the dashboard's endpoints call). Start a run with plan approval,
see its question on the run's view, answer it, pause and continue it, then abort it and watch the job stop."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / "orkestra_bridge.py"
FAKE = HERE / "fake_claude.py"
sys.path.insert(0, str(HERE.parent.parent / "plugins" / "orkestra" / "lib"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DashboardConsoleE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dash-console-")).resolve()
        cls.home, cls.ws = cls.tmp / "home", cls.tmp / "Projects"
        cls.proj = cls.ws / "demo"
        cls.proj.mkdir(parents=True)
        (cls.proj / "AGENTS.md").write_text("# demo\n")
        subprocess.run(["git", "init", "-q", str(cls.proj)], check=True)
        cls.home.mkdir()
        cls.port = free_port()
        (cls.home / "bridge.secret.json").write_text(json.dumps({
            "port": cls.port, "token": "e2e-token-" + "x" * 20, "workspace": str(cls.ws), "claude_bin": str(FAKE),
            "allowed_projects": ["demo"], "watchdog_interval": 0, "auto_resume_delays": [0, 0, 0],
            "telegram": {"bot_token": "", "allowed_chat_ids": []}}))
        cls.saved = {k: os.environ.get(k) for k in ("ORKESTRA_HOME", "ORKESTRA_WORKSPACE", "ORKESTRA_LANG")}
        os.environ.update(ORKESTRA_HOME=str(cls.home), ORKESTRA_WORKSPACE=str(cls.ws), ORKESTRA_LANG="en")
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "ORKESTRA_HEADLESS"):
            env.pop(k, None)
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
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def job(self, job_id):
        from orkestra_core import console as CO
        return next((j for j in CO.all_jobs() if j.get("id") == job_id), {})

    def wait(self, job_id, pred, what, timeout=40):
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            last = self.job(job_id)
            if last and pred(last):
                return last
            time.sleep(0.2)
        self.fail(f"job {job_id} never {what}; last: {json.dumps(last)[:800]}")

    def test_the_whole_flow_from_the_page(self):
        from orkestra_core import console as CO
        from orkestra_core import monitor as MON

        self.assertEqual(CO.service_status()["running"], True)
        self.assertEqual([p["name"] for p in CO.projects("en")["projects"]], ["demo"])
        self.assertTrue(next(e for e in CO.engines("demo", "en")["engines"] if e["id"] == "claude")["available"])

        started = CO.start("demo", "add a priority field", "claude", "manual", "en")
        jid = started["job"]["id"]
        job = self.wait(jid, lambda j: j.get("status") == "awaiting_user" and j.get("run_id"), "waited for an answer")
        self.assertEqual(job["approval_mode"], "manual")                          # the switch reached the service
        self.assertEqual(job["source"], "dashboard")
        rid = job["run_id"]
        key = MON._view_key(self.proj, rid)
        view = next(v for v in MON.discover(self.proj) if v["run_id"] == rid)
        self.assertEqual((view["job"]["id"], view["job"]["status"], view["job"]["can_reply"]), (jid, "awaiting_user", True))
        self.assertIn("1.", view["job"]["message"])                              # the agent's question is on the card
        self.assertIn("request", view["docs"])

        CO.reply(jid, "medium", "en")
        job = self.wait(jid, lambda j: any(t.get("prompt") == "medium" for t in j.get("turns") or [])
                        and j.get("status") == "awaiting_user", "took the answer")

        paused = MON.control_run(key, "pause", "checking")
        self.assertEqual((paused["ok"], paused["status"]), (True, "paused"))
        turns = len(self.job(jid).get("turns") or [])
        CO.continue_run(self.proj, rid, "en")
        job = self.wait(jid, lambda j: len(j.get("turns") or []) > turns and j.get("status") == "awaiting_user",
                        "continued")
        self.assertEqual(job["turns"][turns]["prompt"], "/orkestra:resume demo")
        self.assertEqual(list((self.home / "stops").glob("*.json")) if (self.home / "stops").exists() else [], [])

        aborted = MON.control_run(key, "abort", "done for today")
        self.assertEqual((aborted["ok"], aborted["status"], aborted["job_cancelled"]), (True, "aborted", True))
        self.wait(jid, lambda j: j.get("status") == "cancelled", "was cancelled")
        state = json.loads((self.proj / ".orkestra" / "runs" / rid / "state.json").read_text())
        self.assertEqual(state["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
