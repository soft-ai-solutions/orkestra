"""The dashboard console: start a run, answer it, approve its plan, continue it, read its documents, close idle runs and
run the health check from the page. Agent work goes through the run service (the bridge): here a small fake of its HTTP
API stands in for it (bridge/tests/test_dashboard_console.py drives the real one end to end).

The suite is pinned to Turkish (helpers.py): every test that reads human text picks its language explicitly."""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers
from dash_support import HAVE_NODE, run_dashboard
from orkestra_core import console as CO
from orkestra_core import i18n
from orkestra_core import intent as INTENT
from orkestra_core import monitor as MON
from orkestra_core import paths
from orkestra_core import state as ST
from test_dashboard_ux import NOW, ev, view
from test_flow import FlowBase

TOKEN = "bridge-secret-token-xyz"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeService:
    """The run service's HTTP API, as far as the console uses it: bearer token, JSON in and out, every call recorded."""

    def __init__(self, token: str = TOKEN):
        self.token, self.calls, self.refuse = token, [], None
        self.job = {"id": "abc123def456", "project": "demo", "status": "queued", "engine": "claude"}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    self._json(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if self.path == "/health":
                    return self._json(200, {"ok": True, "lang": "en"})
                if not self._authorized():
                    return
                outer.calls.append(("GET", self.path, None))
                path = self.path.split("?")[0]
                if path == "/projects":
                    return self._json(200, {"projects": [{"name": "demo"}, {"name": "shop"}], "restricted": False})
                if path == "/engines":
                    return self._json(200, {"default": "claude", "engines": [
                        {"id": "claude", "label": "Claude Code", "available": True, "reason": ""},
                        {"id": "cursor", "label": "Cursor", "available": False, "reason": "agent CLI not found"}]})
                self._json(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(("POST", self.path, body))
                if outer.refuse:
                    return self._json(*outer.refuse)
                self._json(202, dict(outer.job))

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ConsoleCase(FlowBase):
    def setUp(self):
        super().setUp()
        self.start(approval_mode="manual")
        self.key = MON._view_key(self.proj, self.rid)
        self.svc = FakeService()
        self.addCleanup(self.svc.close)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "bridge.secret.json").write_text(json.dumps({"port": self.svc.port, "token": TOKEN}))

    def job_file(self, **over):
        d = self.home / "bridge" / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        job = dict({"id": "abc123def456", "project": "demo", "project_path": str(self.proj), "run_id": self.rid,
                    "status": "awaiting_user", "engine": "claude", "session_id": "11111111-2222",
                    "created": "2026-10-30T12:00:00+0300", "updated": "2026-10-30T12:01:00+0300",
                    "last_message": "Which priority should be the default? (a) medium", "approval_mode": "manual",
                    "turns": [{"at": "2026-10-30T12:00:00+0300", "prompt": "/orkestra:run add priorities", "exit": 0}]}, **over)
        (d / f"{job['id']}.json").write_text(json.dumps(job))
        return job


# ================================================================== the run service

class TestService(ConsoleCase):
    def test_status_says_why_nothing_can_start(self):
        self.assertEqual(CO.service_status(), {"installed": CO.bridge_script() is not None, "configured": True,
                                               "running": True, "port": self.svc.port})
        (self.home / "bridge.secret.json").write_text(json.dumps({"port": free_port(), "token": TOKEN}))
        st = CO.service_status()
        self.assertEqual((st["configured"], st["running"]), (True, False))
        (self.home / "bridge.secret.json").unlink()
        st = CO.service_status()
        self.assertEqual((st["configured"], st["running"], st["port"]), (False, False, CO.SERVICE_PORT))

    def test_a_proxy_in_the_environment_never_carries_the_local_call(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
            self.assertEqual(CO.projects()["projects"][0]["name"], "demo")

    def test_lists_come_from_the_service_in_the_page_language(self):
        self.assertEqual([p["name"] for p in CO.projects("en")["projects"]], ["demo", "shop"])
        eng = CO.engines("demo", "tr")
        self.assertEqual(eng["default"], "claude")
        self.assertEqual(self.svc.calls[-1][1], "/engines?project=demo&lang=tr")
        with self.assertRaises(CO.ConsoleError) as cm:
            CO.engines("../etc")
        self.assertEqual(cm.exception.code, "bad_project")

    def test_a_down_or_unset_service_is_named_not_crashed(self):
        (self.home / "bridge.secret.json").write_text(json.dumps({"port": free_port(), "token": TOKEN}))
        with self.assertRaises(CO.ConsoleError) as cm:
            CO.projects()
        self.assertEqual((cm.exception.code, cm.exception.status), ("service_down", 503))
        (self.home / "bridge.secret.json").unlink()
        with self.assertRaises(CO.ConsoleError) as cm:
            CO.projects()
        self.assertEqual(cm.exception.code, "service_not_configured")

    def test_start_service_sets_up_once_and_never_starts_a_second_copy(self):
        self.assertEqual(CO.start_service()["started"], False)                        # running already: left alone
        (self.home / "bridge.secret.json").unlink()
        down = {"installed": True, "configured": False, "running": False, "port": 47615}
        up = dict(down, configured=True, running=True)
        seq = iter([down, up])

        def fake_init(argv, **kw):
            (self.home / "bridge.secret.json").write_text(json.dumps({"port": 47615, "token": "t" * 40}))
            return mock.Mock(returncode=0, stdout="token: " + "t" * 40, stderr="")
        with mock.patch.object(CO, "service_status", side_effect=lambda: next(seq)), \
                mock.patch.object(CO.subprocess, "run", side_effect=fake_init) as run, \
                mock.patch.object(CO.subprocess, "Popen") as popen, mock.patch.object(CO.time, "sleep"):
            res = CO.start_service()
        self.assertEqual((res["ok"], res["started"], res["running"]), (True, True, True))
        self.assertEqual(run.call_args[0][0][-1], "init")
        self.assertEqual(popen.call_args[0][0][-1], "serve")
        self.assertNotIn("t" * 40, json.dumps(res))                                 # the token never reaches the page
        self.assertTrue(popen.call_args[1].get("start_new_session") or os.name == "nt")
        with mock.patch.object(CO, "bridge_script", return_value=None), \
                mock.patch.object(CO, "service_status", return_value=dict(down, configured=True)):
            with self.assertRaises(CO.ConsoleError) as cm:
                CO.start_service()
        self.assertEqual(cm.exception.code, "service_not_installed")


# ================================================================== starting and answering runs

class TestRunsFromThePage(ConsoleCase):
    def test_a_request_is_checked_before_the_service_hears_it(self):
        for args, code in ((("../x", "fix it"), "bad_project"), (("demo", "hi"), "request_short"),
                           (("demo", "x" * (CO.REQUEST_MAX + 1)), "request_long"),
                           (("demo", "fix it", "vim"), "bad_engine"), (("demo", "fix it", "claude", "yolo"), "bad_mode")):
            with self.assertRaises(CO.ConsoleError) as cm:
                CO.start(*args)
            self.assertEqual(cm.exception.code, code, args)
        self.assertEqual(self.svc.calls, [])

    def test_start_names_the_dashboard_as_source_and_the_manual_switch_in_words(self):
        res = CO.start("demo", "  add priorities  ", "claude", "autopilot", "en")
        self.assertEqual(res["job"]["id"], "abc123def456")
        method, path, body = self.svc.calls[-1]
        self.assertEqual((method, path), ("POST", "/jobs?lang=en"))
        self.assertEqual(body, {"project": "demo", "request": "add priorities", "engine": "claude", "source": "dashboard"})
        for code in ("en", "tr"):
            with i18n.use_lang(code):
                CO.start("demo", "add priorities", "", "manual", code)
            request = self.svc.calls[-1][2]["request"]
            self.assertTrue(request.startswith("add priorities\n\n"))
            self.assertTrue(INTENT.manual_approval_requested(request), (code, request))   # every bridge reads it so
        self.assertNotIn(TOKEN, json.dumps(res))

    def test_a_refusal_of_the_service_reaches_the_page_in_its_words(self):
        self.svc.refuse = (400, {"error": "demo already has an open job (abc, running)"})
        with self.assertRaises(CO.ConsoleError) as cm:
            CO.start("demo", "add priorities")
        self.assertEqual((cm.exception.code, cm.exception.detail), ("service_refused", "demo already has an open job (abc, running)"))
        self.svc.refuse = (409, {"error": "the job is closed"})
        with self.assertRaises(CO.ConsoleError) as cm:
            CO.reply("abc123def456", "yes")
        self.assertEqual(cm.exception.status, 409)

    def test_reply_and_cancel_go_to_the_job_and_check_their_input(self):
        CO.reply("abc123def456", "  medium  ", "tr")
        self.assertEqual(self.svc.calls[-1], ("POST", "/jobs/abc123def456/reply?lang=tr", {"text": "medium"}))
        CO.cancel("abc123def456")
        self.assertEqual(self.svc.calls[-1][1], "/jobs/abc123def456/cancel")
        for fn, args, code in ((CO.reply, ("nothex!", "x"), "bad_job"), (CO.reply, ("abc123def456", "  "), "reply_empty"),
                               (CO.reply, ("abc123def456", "x" * (CO.REPLY_MAX + 1)), "reply_long"),
                               (CO.cancel, ("../../x",), "bad_job")):
            with self.assertRaises(CO.ConsoleError) as cm:
                fn(*args)
            self.assertEqual(cm.exception.code, code)

    def test_jobs_are_read_from_their_records_and_matched_to_their_run(self):
        self.assertIsNone(CO.job_for(self.proj, self.rid))
        self.job_file(id="aaaaaaaaaaaa", created="2026-10-30T11:00:00+0300", status="cancelled")
        self.job_file()
        self.job_file(id="bbbbbbbbbbbb", run_id="999-other", created="2026-10-30T13:00:00+0300")
        job = CO.job_for(self.proj, self.rid)
        self.assertEqual(job["id"], "abc123def456")                               # the newest one of this run
        view_ = CO.job_view(job)
        self.assertEqual((view_["status"], view_["can_reply"], view_["can_cancel"], view_["engine"]),
                         ("awaiting_user", True, True, "claude"))
        self.assertEqual(view_["turns"][0]["prompt"], "/orkestra:run add priorities")
        self.assertNotIn("session_id", view_)
        self.job_file(status="running")
        rec = self.home / "bridge" / "jobs" / "abc123def456.json"
        st = rec.stat()
        os.utime(rec, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        self.assertEqual(CO.job_view(CO.job_for(self.proj, self.rid))["can_reply"], False)
        self.assertEqual([j["id"] for j in CO.jobs()][:1], ["bbbbbbbbbbbb"])

    def test_continue_lifts_the_stop_and_sends_the_engine_resume_prompt(self):
        self.assertEqual(self._continue_error(), "no_job")
        self.job_file(status="running")
        self.assertEqual(self._continue_error(), "job_busy")
        self.job_file(status="done")
        self.assertEqual(self._continue_error(), "job_closed")
        self.job_file(status="paused")
        MON.control_run(self.key, "pause", "")
        self.assertTrue(list(paths.stops_dir().glob("*.json")))
        CO.continue_run(self.proj, self.rid, "en")
        self.assertEqual(list(paths.stops_dir().glob("*.json")), [])
        self.assertEqual(self.svc.calls[-1], ("POST", "/jobs/abc123def456/reply?lang=en", {"text": "/orkestra:resume demo"}))

    def _continue_error(self):
        with self.assertRaises(CO.ConsoleError) as cm:
            CO.continue_run(self.proj, self.rid)
        return cm.exception.code

    def test_abort_of_a_headless_run_also_cancels_its_job(self):
        self.job_file(status="running")
        res = MON.control_run(self.key, "abort", "")
        self.assertEqual((res["ok"], res["job_cancelled"]), (True, True))
        self.assertEqual(self.svc.calls[-1][1], "/jobs/abc123def456/cancel")

    def test_a_view_carries_its_job_and_documents(self):
        self.job_file()
        v = next(x for x in MON.discover(None, include_all=True) if x["run_id"] == self.rid)
        self.assertEqual((v["job"]["id"], v["job"]["status"]), ("abc123def456", "awaiting_user"))
        self.assertEqual(v["docs"][0], "request")


# ================================================================== documents, clearing, the health check

class TestReadAndMaintain(ConsoleCase):
    def test_documents_are_read_by_name_only_and_capped(self):
        self.assertIn("request", CO.docs_of(self.proj, self.rid))
        (self.fdir / "plan.md").write_text("# Plan\nT001 add a field\n")
        self.assertEqual(CO.doc(self.proj, self.rid, "plan")["text"], "# Plan\nT001 add a field\n")
        for name, code in (("../../etc/passwd", "bad_doc"), ("state", "bad_doc"), ("report", "no_doc")):
            with self.assertRaises(CO.ConsoleError) as cm:
                CO.doc(self.proj, self.rid, name)
            self.assertEqual(cm.exception.code, code)
        (self.fdir / "spec.md").write_text("x" * (CO.DOC_MAX + 10))
        got = CO.doc(self.proj, self.rid, "spec")
        self.assertEqual((got["truncated"], len(got["text"])), (True, CO.DOC_MAX))

    def test_clear_previews_before_it_closes_and_checks_the_hours(self):
        for bad in ("x", 0, 0.5, 9999999, None):
            with self.assertRaises(CO.ConsoleError) as cm:
                CO.clear_runs(bad, False)
            self.assertEqual(cm.exception.code, "bad_hours")
        res = CO.clear_runs(24, False)
        self.assertEqual((res["applied"], res["closed"]), (False, []))
        self.assertIn(self.rid, [r["run_id"] for r in res["kept"]])
        old = time.time() - 3 * 3600
        for f in (paths.run_dir(self.proj, self.rid) / "state.json", paths.run_dir(self.proj, self.rid) / "activity.jsonl"):
            if f.exists():
                os.utime(f, (old, old))
        for s in paths.sessions_dir().glob("*.json"):
            os.utime(s, (old, old))
        preview = CO.clear_runs(1, False)
        self.assertEqual([r["run_id"] for r in preview["closed"]], [self.rid])
        self.assertEqual(json.loads((paths.run_dir(self.proj, self.rid) / "state.json").read_text())["status"], "active")
        done = CO.clear_runs(1, True)
        self.assertEqual((done["applied"], [r["run_id"] for r in done["closed"]]), (True, [self.rid]))
        self.assertEqual(json.loads((paths.run_dir(self.proj, self.rid) / "state.json").read_text())["status"], "aborted")

    def test_doctor_returns_its_text_without_marker_lines(self):
        out = "[OK] config\n[orkestra:bind] project=x run=y\n[!!] cursor: not installed\n"
        with mock.patch.object(CO.subprocess, "run", return_value=mock.Mock(returncode=1, stdout=out, stderr="")) as run:
            res = CO.doctor("en")
        self.assertEqual((res["passed"], res["text"]), (False, "[OK] config\n[!!] cursor: not installed"))
        self.assertEqual(run.call_args[0][0][-1], "doctor")
        self.assertEqual(run.call_args[1]["env"]["ORKESTRA_LANG"], "en")
        self.assertNotIn("CLAUDECODE", run.call_args[1]["env"])


# ================================================================== the HTTP endpoints

class TestConsoleEndpoints(ConsoleCase):
    PAGE_TOKEN = "page-token-1"

    def serve(self, controls=True):
        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), MON.make_handler(None, port, None, controls=controls,
                                                                        token=self.PAGE_TOKEN if controls else ""))
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return port

    def call(self, port, method, path, body=None, **headers):
        h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
             "X-Orkestra-Token": self.PAGE_TOKEN}
        h.update(headers)
        h = {k: v for k, v in h.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def test_every_console_post_passes_the_same_guard(self):
        port = self.serve()
        for path, body in (("/api/console/start", {"project": "demo", "request": "add priorities"}),
                           ("/api/console/reply", {"job": "abc123def456", "text": "ok"}),
                           ("/api/console/cancel", {"job": "abc123def456"}), ("/api/console/continue", {"key": self.key}),
                           ("/api/console/service", {}), ("/api/console/clear", {"hours": 24}), ("/api/console/doctor", {})):
            for headers, error in (({"X-Orkestra-Token": "nope"}, "token"), ({"Origin": "http://evil.example"}, "origin"),
                                   ({"Host": "evil.example"}, "host")):
                status, res = self.call(port, "POST", path, body, **headers)
                self.assertEqual((status, res["error"]), (403, error), (path, headers))
        self.assertEqual(self.svc.calls, [])                                     # nothing reached the service
        ro = self.serve(controls=False)
        status, res = self.call(ro, "POST", "/api/console/start", {"project": "demo", "request": "add priorities"},
                                **{"X-Orkestra-Token": ""})
        self.assertEqual((status, res["error"]), (403, "read_only"))

    def test_start_reply_and_errors_through_http(self):
        port = self.serve()
        status, res = self.call(port, "POST", "/api/console/start", {"project": "demo", "request": "add priorities",
                                                                      "engine": "claude", "mode": "manual", "lang": "en"})
        self.assertEqual((status, res["ok"], res["job"]["id"]), (200, True, "abc123def456"))
        self.assertIn("Use manual plan approval", self.svc.calls[-1][2]["request"])
        status, res = self.call(port, "POST", "/api/console/start", {"project": "demo", "request": "x"})
        self.assertEqual((status, res["error"]), (400, "request_short"))
        self.svc.refuse = (400, {"error": "demo already has an open job"})
        status, res = self.call(port, "POST", "/api/console/reply", {"job": "abc123def456", "text": "go"})
        self.assertEqual((status, res["error"], res["detail"]), (400, "service_refused", "demo already has an open job"))
        status, res = self.call(port, "POST", "/api/console/continue", {"key": "0" * 12})
        self.assertEqual((status, res["error"]), (404, "not_found"))

    def test_reads_need_no_token_and_never_show_the_service_token(self):
        port = self.serve()
        bare = {"X-Orkestra-Token": None, "Origin": None, "Content-Type": None}
        status, res = self.call(port, "GET", "/api/console/service", **bare)
        self.assertEqual((status, res["running"], res["controls"]), (200, True, True))
        status, res = self.call(port, "GET", "/api/console/projects?lang=en", **bare)
        self.assertEqual([p["name"] for p in res["projects"]], ["demo", "shop"])
        status, res = self.call(port, "GET", "/api/console/engines?project=demo&lang=en", **bare)
        self.assertEqual(res["engines"][1]["available"], False)
        status, res = self.call(port, "GET", f"/api/console/doc?key={self.key}&name=request", **bare)
        self.assertEqual((status, res["name"]), (200, "request"))
        status, res = self.call(port, "GET", "/api/console/doc?key=000000000000&name=request", **bare)
        self.assertEqual((status, res["error"]), (404, "not_found"))
        self.job_file()
        status, res = self.call(port, "GET", "/api/console/jobs", **bare)
        self.assertEqual(res["jobs"][0]["id"], "abc123def456")
        status, res = self.call(port, "GET", "/api/console/nope", **bare)
        self.assertEqual(status, 404)
        for path in ("/api/console/service", "/api/console/projects", "/api/console/jobs"):
            _, res = self.call(port, "GET", path, **bare)
            self.assertNotIn(TOKEN, json.dumps(res))


# ================================================================== the page

def console_answers(running=True):
    return {"/api/console/service": {"ok": True, "installed": True, "configured": True, "running": running, "port": 47615},
            "/api/console/projects": {"ok": True, "projects": [{"name": "demo"}, {"name": "shop"}]},
            "/api/console/engines": {"ok": True, "default": "claude", "engines": [
                {"id": "claude", "label": "Claude Code", "available": True, "reason": ""},
                {"id": "cursor", "label": "Cursor", "available": False, "reason": "agent CLI not found"}]},
            "/api/console/jobs": {"ok": True, "jobs": [{"id": "abc123def456", "status": "error", "engine": "copilot",
                                                        "project": "shop", "run_id": "", "message": "copilot stopped",
                                                        "can_cancel": True, "can_reply": False, "updated": "2026-10-30T12:00:00+0300"}]},
            "/api/console/doc": {"ok": True, "name": "plan", "text": "# Plan\nT001 add a field", "truncated": False, "path": "~/x/plan.md"}}


def job_view(n, **over):
    v = view(n, **over)
    v["docs"] = ["request", "spec", "plan"]
    v["job"] = dict({"id": "abc123def45" + str(n), "status": "awaiting_user", "engine": "claude", "message": "Which priority? (a) medium",
                     "turns": [{"at": "2026-10-30T12:00:00+0300", "prompt": "/orkestra:run add priorities"}],
                     "approval_mode": "manual", "can_reply": True, "can_cancel": True}, **over.pop("job", {}))
    return v


@unittest.skipUnless(HAVE_NODE, "node is needed to run the page script")
class TestConsolePage(helpers.Sandbox):
    def page(self, code="en", token="tok-9"):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code}), i18n.use_lang(code):
            return MON.page_html("", token=token)

    def run_page(self, views, steps, code="en", token="tok-9", running=True):
        lead = [{"op": "set", "key": "console", "value": console_answers(running)}]
        out = run_dashboard(self.tmp, self.page(code, token), views, lead + list(steps))
        self.assertEqual(out["errors"], [], out["errors"])
        return out

    def test_the_header_offers_new_run_and_tools_only_with_controls(self):
        out = self.run_page([view(1)], [ev("return [H.byId('acts').hidden,H.byId('new-lbl').textContent,H.byId('tools-btn').getAttribute('aria-label')]", "r")])
        self.assertEqual(out["results"]["r"], [False, "New run", "Tools"])
        # read-only: the markup keeps `hidden` and the script never labels or wires the buttons
        ro = self.run_page([view(1)], [ev("H.byId('new-btn').fire('click');return [H.byId('new-lbl').textContent,"
                                          "H.byId('nr').getAttribute('open'),H.sc.gets||[]]", "r")], token="")
        self.assertEqual(ro["results"]["r"], ["", None, []])

    def test_new_run_loads_choices_names_each_missing_field_and_starts(self):
        out = self.run_page([view(1)], [
            ev("H.byId('new-btn').fire('click');return 1", "x"), {"op": "advance", "ms": 50},
            ev("const p=H.byId('nr-project'),e=H.byId('nr-engine');return [p.childNodes.map(o=>o.textContent),e.childNodes.map(o=>[o.value,o.disabled,o.textContent]),"
               "e.value,H.byId('nr-engine-hint').textContent,H.byId('nr-svc').hidden,H.byId('nr').getAttribute('open')]", "loaded"),
            ev("H.byId('nr-form').fire('submit');return [H.byId('nr-project-err').textContent,H.byId('nr-req-err').textContent,"
               "H.byId('nr-ok-err').textContent,H.sc.posts.length,document.activeElement===H.byId('nr-project')]", "empty"),
            ev("H.byId('nr-project').value='demo';H.byId('nr-req').value='add a priority field';H.byId('nr-req').fire('input');"
               "H.byId('nr-ok').checked=true;H.byId('nr-mode-man').checked=true;H.byId('nr-mode-auto').checked=false;"
               "H.byId('nr-form').fire('submit');return H.byId('nr-go').textContent", "busy"),
            {"op": "advance", "ms": 50},
            ev("const p=H.sc.posts[0];return [p.url,p.body,p.headers['X-Orkestra-Token'],H.byId('nr').getAttribute('open'),"
               "H.byId('toast').textContent,localStorage._m['orkestra.dash.nr.project']]", "sent")])
        r = out["results"]
        self.assertEqual(r["loaded"][0], ["Choose a project", "demo", "shop"])
        self.assertEqual(r["loaded"][1], [["claude", False, "Claude Code (default)"], ["cursor", True, "Cursor (not available)"]])
        self.assertEqual((r["loaded"][2], r["loaded"][3], r["loaded"][4], r["loaded"][5]),
                         ("claude", "Cursor: agent CLI not found", True, ""))
        self.assertEqual(r["empty"], ["Choose the project the agent should work in.", "Describe the work in at least 3 characters.",
                                      "Confirm this to start the run.", 0, True])
        self.assertEqual(r["busy"], "Starting…")
        self.assertEqual(r["sent"][0], "/api/console/start")
        self.assertEqual(r["sent"][1], {"lang": "en", "project": "demo", "request": "add a priority field", "engine": "claude",
                                        "mode": "manual"})
        self.assertEqual(r["sent"][2:], ["tok-9", None, "Run started in demo with Claude Code.", "demo"])

    def test_a_stopped_service_can_be_started_from_the_dialog(self):
        out = self.run_page([view(1)], [
            {"op": "set", "key": "answers", "value": {"/api/console/service": {"ok": True, "installed": True, "configured": True,
                                                                              "running": True, "port": 47615, "started": True}}},
            ev("H.byId('new-btn').fire('click');return 1", "x"), {"op": "advance", "ms": 50},
            ev("return [H.byId('nr-svc').hidden,H.byId('nr-svc-msg').textContent,H.byId('nr-svc-btn').hidden,H.byId('nr-svc-btn').textContent]", "down"),
            ev("H.byId('nr-svc-btn').fire('click');return 1", "y"), {"op": "advance", "ms": 80},
            ev("return [H.sc.posts[0].url,H.byId('nr-svc').hidden,H.byId('nr-project').childNodes.length]", "up")], running=False)
        r = out["results"]
        self.assertEqual(r["down"], [False, "The run service is not running, so no agent can be started from here.", False,
                                     "Start the run service"])
        self.assertEqual(r["up"], ["/api/console/service", True, 3])

    def test_a_refused_start_keeps_the_dialog_with_the_service_words(self):
        out = self.run_page([view(1)], [
            {"op": "set", "key": "answers", "value": {"/api/console/start": {"ok": False, "error": "service_refused",
                                                                            "detail": "demo already has an open job"}}},
            {"op": "set", "key": "controlStatus", "value": 400},
            ev("H.byId('new-btn').fire('click');return 1", "x"), {"op": "advance", "ms": 50},
            ev("H.byId('nr-project').value='demo';H.byId('nr-req').value='add a field';H.byId('nr-ok').checked=true;"
               "H.byId('nr-form').fire('submit');return 1", "y"), {"op": "advance", "ms": 50},
            ev("return [H.byId('nr').getAttribute('open'),H.byId('nr-err').textContent,H.byId('nr-go').disabled]", "r")])
        self.assertEqual(out["results"]["r"], ["", "The run service refused: demo already has an open job", False])

    def test_the_conversation_answers_approves_and_continues(self):
        out = self.run_page([job_view(1, phase="approve", status="awaiting_user")], [
            ev("const c=H.card('k1');const b=H.find(c,n=>n.dataset&&/^j/.test(n.dataset.act||'')).map(n=>n.dataset.act);"
               "return [b,H.find(c,n=>n.className&&n.className.indexOf('jmsg')===0)[0].textContent,"
               "H.find(c,n=>n.className&&n.className.indexOf('rk jst-')===0)[0].textContent]", "shown"),
            ev("H.find(H.card('k1'),n=>n.dataset&&n.dataset.act==='jsend')[0].fire('click');"
               "return H.find(H.card('k1'),n=>n.className==='dlg-err')[0].textContent", "empty"),
            ev("H.find(H.card('k1'),n=>n.dataset&&n.dataset.act==='japprove')[0].fire('click');return 1", "a"), {"op": "advance", "ms": 50},
            ev("return [H.sc.posts[0].url,H.sc.posts[0].body,H.byId('toast').textContent]", "approved")])
        r = out["results"]
        self.assertEqual(r["shown"][0], ["jmore", "japprove", "jreview", "jsend"])          # page order
        self.assertEqual(r["shown"][1:], ["Which priority? (a) medium", "Waiting for your answer"])
        self.assertEqual(r["empty"], "Write your answer first.")
        self.assertEqual(r["approved"], ["/api/console/reply", {"lang": "en", "job": "abc123def451", "text": "approve"},
                                         "Sent to proj1. The agent continues."])
        tr = self.run_page([job_view(1, phase="approve", status="awaiting_user")], [
            ev("H.find(H.card('k1'),n=>n.dataset&&n.dataset.act==='japprove')[0].fire('click');return 1", "a"), {"op": "advance", "ms": 50},
            ev("return H.sc.posts[0].body.text", "t")], code="tr")
        self.assertEqual(tr["results"]["t"], "onayla")
        paused = self.run_page([job_view(2, group="paused", code="paused", status="paused", job={"status": "paused"})], [
            ev("H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='expand')[0].fire('click');"
               "H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='jcontinue')[0].fire('click');return 1", "c"), {"op": "advance", "ms": 50},
            ev("return [H.sc.posts[0].url,H.sc.posts[0].body.key,H.byId('toast').textContent]", "r")])
        self.assertEqual(paused["results"]["r"], ["/api/console/continue", "k2", "proj2 continues."])

    def test_a_working_agent_is_not_interrupted_by_a_reply_box(self):
        out = self.run_page([job_view(1, job={"status": "running", "can_reply": False})], [
            ev("const c=H.card('k1');return [H.find(c,n=>n.className==='jform')[0].hidden,H.find(c,n=>n.className==='hint'&&n.textContent).map(n=>n.textContent)]", "r")])
        self.assertEqual(out["results"]["r"], [True, ["The agent is working. You can answer when it stops."]])

    def test_documents_open_in_the_reader_and_switch_by_tab(self):
        out = self.run_page([job_view(1)], [
            ev("const b=H.find(H.card('k1'),n=>n.dataset&&n.dataset.act==='doc');const names=b.map(n=>n.textContent);"
               "b[2].fire('click');return names", "names"), {"op": "advance", "ms": 50},
            ev("return [H.byId('rd').getAttribute('open'),H.byId('rd-h').textContent,H.byId('rd-text').textContent,"
               "H.byId('rd-tabs').childNodes.map(t=>t.getAttribute('aria-pressed')),H.sc.gets[H.sc.gets.length-1]]", "open"),
            ev("H.byId('rd-close').fire('click');return H.byId('rd').getAttribute('open')", "closed")])
        r = out["results"]
        self.assertEqual(r["names"], ["Request", "Spec", "Plan"])
        self.assertEqual(r["open"][:4], ["", "Plan · proj1 run-1", "# Plan\nT001 add a field", ["false", "false", "true"]])
        self.assertIn("/api/console/doc?lang=en&key=k1&name=plan", r["open"][4])
        self.assertIsNone(r["closed"])

    def test_tools_show_the_service_jobs_clear_and_the_health_check(self):
        out = self.run_page([view(1)], [
            {"op": "set", "key": "answers", "value": {
                "/api/console/clear": {"ok": True, "applied": False, "closed": [{"project": "demo", "run_id": "007-x", "title": "Old",
                                                                                  "idle": 90000}], "kept": [], "dropped": 2},
                "/api/console/doctor": {"ok": True, "passed": False, "text": "[OK] config\n[!!] cursor: missing"}}},
            ev("H.byId('tools-btn').fire('click');return 1", "x"), {"op": "advance", "ms": 50},
            ev("return [H.byId('tl-svc-msg').textContent,H.byId('tl-jobs').childNodes.map(n=>n.textContent)]", "open"),
            ev("const c=H.find(H.byId('tl-jobs'),n=>n.dataset&&n.dataset.act==='jcancel')[0];c.fire('click');"
               "return [c.textContent,H.sc.posts.length]", "armed"),
            ev("H.byId('tl-hours').value='0';H.byId('tl-preview').fire('click');return [H.byId('tl-hours-err').textContent,H.sc.posts.length]", "bad"),
            ev("H.byId('tl-hours').value='24';H.byId('tl-preview').fire('click');return 1", "p"), {"op": "advance", "ms": 50},
            ev("return [H.byId('tl-clear-out').textContent,H.sc.posts[0].body]", "preview"),
            ev("H.byId('tl-doc-run').fire('click');return 1", "d"), {"op": "advance", "ms": 50},
            ev("return [H.byId('tl-doc-sum').textContent,H.byId('tl-doc-out').childNodes.map(n=>n.className)]", "doctor")])
        r = out["results"]
        self.assertEqual(r["open"][0], "The run service is running (127.0.0.1:47615).")
        self.assertIn("copilot stopped", r["open"][1][0])
        self.assertEqual(r["armed"], ["Press again to cancel the job and abort its run", 0])
        self.assertEqual(r["bad"], ["Enter a number of hours between 1 and 8760.", 0])
        self.assertIn("Would close 1 run(s) and remove 2 stale record(s):", r["preview"][0])
        self.assertIn("demo · 007-x", r["preview"][0])
        self.assertEqual(r["preview"][1], {"lang": "en", "hours": 24, "apply": False})
        self.assertEqual(r["doctor"], ["Some checks need attention (marked !!).", ["good", "bad"]])

    def test_turkish_console_speaks_turkish(self):
        out = self.run_page([job_view(1)], [ev("H.byId('new-btn').fire('click');return 1", "x"), {"op": "advance", "ms": 50},
                                            ev("return [H.byId('nr-h').textContent,H.byId('nr-go').textContent,H.byId('new-lbl').textContent]", "r")], code="tr")
        self.assertEqual(out["results"]["r"], ["Yeni çalışma başlat", "Çalışmayı başlat",
                                               "Yeni çalışma"])


if __name__ == "__main__":
    unittest.main()
