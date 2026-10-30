"""orkestra_client.py on a bad day: the bridge restarting, a busy machine, a proxy in the environment, an answer that is
not JSON. The OpenClaw plugin (Telegram) runs this client for every command and every 15 s poll, so a passing hiccup
must not reach the person, and a request that reached the bridge must never be sent twice."""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent / "orkestra_client.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fake:
    def __init__(self, port: int, status: int = 200, body: bytes = b'{"jobs": []}'):
        self.hits = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.hits.append((self.command, self.path, self.rfile.read(n) if n else b""))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ClientResilience(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="client-res-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.port = free_port()
        (self.tmp / "bridge.secret.json").write_text(json.dumps({"port": self.port, "token": "t-123"}))

    def run_client(self, *args, **env):
        e = dict(os.environ, ORKESTRA_HOME=str(self.tmp), ORKESTRA_LANG="en")
        e.update(env)
        start = time.monotonic()
        cp = subprocess.run([sys.executable, str(CLIENT), *args], capture_output=True, text=True, env=e, timeout=60)
        return cp, time.monotonic() - start

    def test_a_bridge_that_comes_up_a_moment_later_is_reached(self):
        def late():
            time.sleep(0.6)
            self.fake = Fake(self.port)
        threading.Thread(target=late, daemon=True).start()
        cp, _ = self.run_client("status")
        self.addCleanup(lambda: self.fake.close())
        self.assertEqual((cp.returncode, json.loads(cp.stdout)), (0, {"jobs": []}), cp.stderr)

    def test_a_request_that_reached_the_bridge_is_never_repeated(self):
        fake = Fake(self.port, status=500, body=b'{"error": "internal error"}')
        self.addCleanup(fake.close)
        cp, _ = self.run_client("start", "demo", "add a field", "--chat-id", "42", "--source", "openclaw")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(json.loads(cp.stdout), {"error": "internal error"})
        self.assertEqual([h[:2] for h in fake.hits], [("POST", "/jobs")])          # exactly once

    def test_an_answer_that_is_not_json_becomes_an_error_not_a_traceback(self):
        fake = Fake(self.port, status=502, body=b"<html>Bad Gateway</html>")
        self.addCleanup(fake.close)
        cp, _ = self.run_client("status")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(json.loads(cp.stdout), {"error": "<html>Bad Gateway</html>"})
        self.assertNotIn("Traceback", cp.stderr)

    def test_a_proxy_in_the_environment_is_not_used_for_the_local_bridge(self):
        fake = Fake(self.port)
        self.addCleanup(fake.close)
        dead = f"http://127.0.0.1:{free_port()}"
        cp, _ = self.run_client("status", HTTP_PROXY=dead, http_proxy=dead, ALL_PROXY=dead)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_a_bridge_that_stays_down_is_named_in_bounded_time(self):
        cp, took = self.run_client("status")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("bridge not reachable", json.loads(cp.stdout)["error"])
        self.assertNotIn("Traceback", cp.stderr)
        self.assertLess(took, 15)

    def test_projects_lists_what_the_bridge_may_start(self):
        fake = Fake(self.port, body=b'{"projects": [{"name": "demo"}], "restricted": false}')
        self.addCleanup(fake.close)
        cp, _ = self.run_client("projects")
        self.assertEqual(json.loads(cp.stdout)["projects"], [{"name": "demo"}])
        self.assertEqual(fake.hits[0][:2], ("GET", "/projects"))


if __name__ == "__main__":
    unittest.main()
