"""Pause / Abort from the dashboard and the risky-actions view: the stop note the hooks obey, the control endpoint and
its guards, the risk data of a run view and the page that shows both.

The suite is pinned to Turkish (helpers.py): every test that reads human text picks its language explicitly."""
from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers
from dash_support import HAVE_NODE, run_dashboard
from orkestra_core import activity as A
from orkestra_core import i18n
from orkestra_core import monitor as MON
from orkestra_core import paths
from orkestra_core import state as ST
from test_dashboard_ux import NOW, ev, view
from test_flow import FlowBase

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
FORCE_PUSH = "git push " + "--force origin main"          # kept out of shell text: a local hook refuses it there


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunCase(FlowBase):
    """A real run in a sandbox (FlowBase binds session sess-1 to it the way the PostToolUse hook does)."""

    def setUp(self):
        super().setUp()
        self.start(approval_mode="autopilot")
        self.key = MON._view_key(self.proj, self.rid)

    def status(self):
        return json.loads((paths.run_dir(self.proj, self.rid) / "state.json").read_text())["status"]

    def pre(self, sid="sess-1", command="ls", **extra):
        payload = {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.proj)}
        payload.update(extra)
        return self.hook("PreToolUse", payload)

    def reason(self, out):
        return (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""


# ================================================================== the stop note

class TestStopNote(RunCase):
    def test_it_applies_while_the_run_has_its_status_and_ends_on_resume(self):
        self.cli("pause", "-p", "demo")
        written = ST.request_stop(self.proj, self.rid, "paused", ["sess-1", "sess-1", ""], reason="why")
        self.assertEqual(written, ["sess-1"])
        note = ST.stop_for("sess-1")
        self.assertEqual((note["run_id"], note["status"], note["reason"], note["by"]), (self.rid, "paused", "why", "dashboard"))
        self.cli("resume", "-p", "demo")
        self.assertIsNone(ST.stop_for("sess-1"))                                  # resumed: the note is gone
        self.assertFalse((paths.stops_dir() / "sess-1.json").exists())

    def test_it_expires_and_never_names_an_open_status(self):
        self.cli("pause", "-p", "demo")
        ST.request_stop(self.proj, self.rid, "paused", ["sess-1"], now=time.time() - ST.STOP_TTL - 1)
        self.assertIsNone(ST.stop_for("sess-1"))
        with self.assertRaises(ST.OrkestraError):
            ST.request_stop(self.proj, self.rid, "active", ["sess-1"])
        self.assertIsNone(ST.stop_for(""))
        self.assertIsNone(ST.clear_stop("nobody"))

    def test_a_damaged_note_stops_nobody(self):
        self.cli("pause", "-p", "demo")
        paths.stops_dir().mkdir(parents=True, exist_ok=True)
        for body in ('{"project": "%s", "run_id": "%s", "status": "paused", "at": "soon"}' % (self.proj, self.rid),
                     "[1, 2]", "not json"):
            (paths.stops_dir() / "sess-1.json").write_text(body)
            self.assertIsNone(ST.stop_for("sess-1"), body)
            self.assertFalse(self.denied(self.pre()), body)

    def test_a_chat_that_moved_on_to_another_run_is_not_stopped(self):
        MON.control_run(self.key, "pause", "")
        binding = paths.sessions_dir() / "sess-1.json"
        data = json.loads(binding.read_text())
        binding.write_text(json.dumps(dict(data, run_id="002-other")))
        self.assertIsNone(ST.stop_for("sess-1"))
        self.assertFalse((paths.stops_dir() / "sess-1.json").exists())

    def test_a_tool_the_quiet_path_skips_is_still_refused(self):
        """Agent, Task, Glob and WebFetch skip the full hook for a chat without a binding file (hookboot.is_quiet). After an
        abort the binding is gone, so the stop note itself must keep the full hook in the loop."""
        MON.control_run(self.key, "abort", "")
        (paths.sessions_dir() / "sess-1.json").unlink(missing_ok=True)
        for tool, ti in (("Agent", {"subagent_type": "general", "prompt": "go on"}), ("Glob", {"pattern": "*.py"}),
                         ("WebFetch", {"url": "https://example.com"})):
            out = self.hook("PreToolUse", {"session_id": "sess-1", "tool_name": tool, "tool_input": ti, "cwd": str(self.proj)})
            self.assertTrue(self.denied(out), tool)
        quiet = self.hook("PreToolUse", {"session_id": "sess-9", "tool_name": "Glob", "tool_input": {"pattern": "*"},
                                         "cwd": str(self.proj)})
        self.assertEqual(quiet, {})

    def test_no_note_costs_one_check_and_returns_nothing(self):
        self.assertIsNone(ST.stop_for("sess-1"))
        self.assertFalse(paths.stops_dir().exists())


# ================================================================== control_run + the hooks

class TestControlRun(RunCase):
    def test_pause_stops_the_bound_session_and_its_subagents_but_lets_them_look(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"}):
            res = MON.control_run(self.key, "pause", "")
        self.assertEqual((res["ok"], res["status"], res["changed"], res["run_id"]), (True, "paused", True, self.rid))
        self.assertGreaterEqual(res["stopped"], 1)
        self.assertEqual(self.status(), "paused")
        out = self.pre()
        self.assertTrue(self.denied(out), out)
        self.assertIn("STOP", self.reason(out))
        self.assertIn("paused run " + self.rid, self.reason(out))
        sub = self.pre(agent_type="orkestra:implementer", agent_id="a1")
        self.assertTrue(self.denied(sub))
        self.assertTrue(self.denied(self.hook("PreToolUse", {"session_id": "sess-1", "tool_name": "Edit", "cwd": str(self.proj),
                                                            "tool_input": {"file_path": str(self.proj / "calc.py")}})))
        self.assertFalse(self.denied(self.pre(command="orkestra status -p demo")))
        self.assertFalse(self.denied(self.pre(command="orkestra watch --once && orkestra next -p demo")))
        self.assertTrue(self.denied(self.pre(command="orkestra resume -p demo")))      # only the person lifts it
        self.assertTrue(self.denied(self.pre(command="orkestra status -p demo; rm -rf build")))
        self.assertFalse(self.denied(self.pre(sid="sess-other")))                       # another chat is not touched
        rows = A.read_rows(self.proj, self.rid)
        self.assertTrue(any(r["kind"] == "deny" and r["what"].startswith("[stopped]") for r in rows))
        status_rows = [r for r in rows if r["kind"] == "status"]
        self.assertIn("dashboard", status_rows[-1]["what"])
        ledger = (self.fdir / "ledger.md").read_text()
        self.assertIn("status active → paused: stopped from the dashboard", ledger)

    def test_the_person_writing_again_lifts_the_stop_with_one_note(self):
        MON.control_run(self.key, "pause", "wrong direction")
        out = self.hook("UserPromptSubmit", {"session_id": "sess-1", "prompt": "what happened?", "cwd": str(self.proj)})
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        self.assertIn("paused by the user from the Orkestra dashboard", ctx)
        self.assertIn("orkestra resume", ctx)
        self.assertIsNone(ST.stop_for("sess-1"))
        self.assertFalse(self.denied(self.pre()))

    def test_a_subagent_report_does_not_lift_the_stop(self):
        MON.control_run(self.key, "pause", "")
        self.hook("UserPromptSubmit", {"session_id": "sess-1", "cwd": str(self.proj),
                                       "prompt": '<agent-message from="impl-1">STATUS: DONE</agent-message>'})
        self.assertTrue(self.denied(self.pre()))

    def test_abort_closes_the_run_and_still_stops_its_sessions(self):
        res = MON.control_run(self.key, "abort", "  not   needed  ")
        self.assertEqual((res["ok"], res["status"]), (True, "aborted"))
        self.assertEqual(self.status(), "aborted")
        out = self.pre()
        self.assertTrue(self.denied(out))
        self.assertIn("aborted run", self.reason(out))
        self.assertIn("(not needed)", self.reason(out))
        self.assertEqual(MON.control_run(self.key, "pause", "")["error"], "closed")
        self.assertEqual(MON.control_run(self.key, "abort", "")["error"], "closed")

    def test_requests_that_cannot_be_carried_out_say_why(self):
        self.assertEqual(MON.control_run(self.key, "resume", "")["error"], "bad_action")
        self.assertEqual(MON.control_run(self.key, "pause", "x" * 301)["error"], "reason_long")
        self.assertEqual(MON.control_run("0" * 12, "pause", "")["error"], "not_found")
        self.assertEqual(MON.control_run("../../etc", "pause", "")["error"], "not_found")
        self.assertEqual(MON.control_run(str(self.proj), "pause", "")["error"], "not_found")
        self.assertEqual(self.status(), "active")

    def test_sessions_seen_in_the_activity_are_stopped_too(self):
        A.record(self.proj, self.rid, {"session_id": "cursor:worker-9", "tool_name": "Bash", "tool_input": {"command": "ls"}},
                 "tool")
        res = MON.control_run(self.key, "pause", "")
        self.assertEqual(res["stopped"], 2)
        self.assertTrue((paths.stops_dir() / "cursor_worker-9.json").exists())
        self.assertTrue(self.denied(self.pre(sid="cursor:worker-9")))

    def test_pausing_twice_changes_nothing_but_keeps_the_stop(self):
        MON.control_run(self.key, "pause", "")
        again = MON.control_run(self.key, "pause", "")
        self.assertEqual((again["ok"], again["changed"]), (True, False))
        self.assertTrue(self.denied(self.pre()))


# ================================================================== the HTTP endpoint

class TestControlEndpoint(RunCase):
    TOKEN = "tok-123"

    def serve(self, controls=True, token=TOKEN):
        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), MON.make_handler(None, port, None, controls=controls, token=token))
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return port

    def call(self, port, method="POST", path="/api/control", body=None, headers=None, raw=None):
        h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
             "X-Orkestra-Token": self.TOKEN}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def test_every_guard_must_pass_before_anything_changes(self):
        port = self.serve()
        good = {"key": self.key, "action": "pause", "reason": ""}
        cases = [({"Host": "evil.example"}, 403, "host"), ({"Origin": None}, 403, "origin"),
                 ({"Origin": "http://evil.example"}, 403, "origin"), ({"Sec-Fetch-Site": "cross-site"}, 403, "origin"),
                 ({"X-Orkestra-Token": "nope"}, 403, "token"), ({"X-Orkestra-Token": None}, 403, "token"),
                 ({"Content-Type": "text/plain"}, 415, "content_type")]
        for headers, code, error in cases:
            status, _, body = self.call(port, body=good, headers=headers)
            self.assertEqual((status, json.loads(body)["error"]), (code, error), headers)
        status, _, body = self.call(port, raw=b"x" * (MON.CONTROL_BODY_MAX + 1))
        self.assertEqual((status, json.loads(body)["error"]), (413, "body"))
        status, _, body = self.call(port, raw=b"[1,2]")
        self.assertEqual((status, json.loads(body)["error"]), (400, "body"))
        status, _, body = self.call(port, path="/api/other", body=good)
        self.assertEqual(status, 404)
        self.assertEqual(self.status(), "active")                                # nothing above changed the run
        status, _, body = self.call(port, body={"key": "0" * 12, "action": "pause"})
        self.assertEqual((status, json.loads(body)["error"]), (404, "not_found"))
        status, _, body = self.call(port, body=dict(good, lang="en"))
        res = json.loads(body)
        self.assertEqual((status, res["ok"], res["status"]), (200, True, "paused"))
        self.assertEqual(self.status(), "paused")
        status, _, body = self.call(port, body={"key": self.key, "action": "abort", "reason": "done here"})
        self.assertEqual((status, json.loads(body)["status"]), (200, "aborted"))
        status, _, body = self.call(port, body=good)
        self.assertEqual((status, json.loads(body)["error"]), (409, "closed"))

    def test_the_page_carries_the_token_only_when_controls_are_on(self):
        port = self.serve()
        _, _, page = self.call(port, method="GET", path="/?lang=en", headers={"Origin": None, "Content-Type": None})
        meta = json.loads(re.search(r"const META=(\{.*?\});", page.decode()).group(1))
        self.assertEqual((meta["controls"], meta["token"]), (True, self.TOKEN))
        ro = self.serve(controls=False, token="")
        _, _, page = self.call(ro, method="GET", path="/?lang=en", headers={"Origin": None, "Content-Type": None})
        meta = json.loads(re.search(r"const META=(\{.*?\});", page.decode()).group(1))
        self.assertEqual((meta["controls"], meta["token"]), (False, ""))
        status, _, body = self.call(ro, body={"key": self.key, "action": "pause"}, headers={"X-Orkestra-Token": ""})
        self.assertEqual((status, json.loads(body)["error"]), (403, "read_only"))
        self.assertEqual(self.status(), "active")

    def test_a_browser_that_accepts_gzip_gets_a_compressed_answer(self):
        port = self.serve()
        status, headers, body = self.call(port, method="GET", path="/api/runs?lang=en&all=1",
                                          headers={"Accept-Encoding": "gzip", "Origin": None, "Content-Type": None})
        self.assertEqual((status, headers.get("Content-Encoding")), (200, "gzip"))
        data = json.loads(gzip.decompress(body))
        self.assertEqual(data[0]["run_id"], self.rid)
        self.assertIn("risks", data[0])
        _, plain_headers, plain = self.call(port, method="GET", path="/api/runs?lang=en&all=1",
                                            headers={"Origin": None, "Content-Type": None})
        self.assertNotIn("Content-Encoding", plain_headers)
        self.assertEqual(json.loads(plain)[0]["run_id"], self.rid)


# ================================================================== risk data in a view

def row(ts, kind, what, tool="Bash", agent="main", **kw):
    return dict({"ts": ts, "kind": kind, "agent": agent, "tool": tool, "what": what}, **kw)


class TestRiskData(unittest.TestCase):
    def rows(self):
        return [row(1, "tool", "rm -rf ~/old-backups"), row(2, "tool", "a.py", tool="Read"),
                row(3, "tool", "rm -rf ~/old-backups"),                                    # folded with the one above
                row(4, "deny", "[git] pushing needs your go-ahead", cmd=FORCE_PUSH),
                row(5, "deny", "[secrets] .env is a secret file"),                          # no command on record
                row(6, "deny", "[phase] edits wait for the plan approval"),                 # a process rule: not a risk
                row(7, "tool", "npm install left-pad", agent="orkestra:implementer"),
                row(8, "tool", "ls -la")]

    def test_risky_actions_are_listed_newest_first_with_counts(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"}):
            risks, counts = MON._risks(self.rows(), "/w/demo", "/Users/me")
        self.assertEqual(counts, {"high": 2, "medium": 1, "blocked": 2})
        self.assertEqual([(e["code"], e["outcome"]) for e in risks],
                         [("install_package", "ran"), ("guard", "blocked"), ("git_force_push", "blocked"),
                          ("rm_outside", "ran")])
        rm = risks[-1]
        self.assertEqual((rm["count"], rm["ts"], rm["level"], rm["agent"], rm["main"]), (2, 3, "high", "main", True))
        push = risks[2]
        self.assertEqual((push["guard"], push["what"], push["level"]), ("git", FORCE_PUSH, "high"))
        self.assertEqual(risks[1]["label"], "Refused by the secrets guard")
        self.assertEqual(risks[0]["agent"], "implementer")
        for e in risks:
            self.assertEqual(TR_LETTERS.findall(e["label"]), [])
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "tr"}):
            risks_tr, _ = MON._risks(self.rows(), "/w/demo", "/Users/me")
        self.assertEqual(risks_tr[1]["label"], "secrets korumasının reddettiği işlem")

    def test_the_list_is_capped_but_the_counts_cover_every_row(self):
        rows = [row(i, "tool", f"rm -rf ~/dir{i}") for i in range(MON.RISK_KEEP + 15)]
        risks, counts = MON._risks(rows)
        self.assertEqual(len(risks), MON.RISK_KEEP)
        self.assertEqual(counts["high"], MON.RISK_KEEP + 15)
        self.assertEqual(risks[0]["what"], f"rm -rf ~/dir{MON.RISK_KEEP + 14}")

    def test_feed_rows_carry_their_risk_and_a_refused_command(self):
        feed = MON._activity_feed(self.rows(), 14, project="/w/demo", home="/Users/me")
        push = next(r for r in feed if r["kind"] == "deny" and r["category"] == "git")
        self.assertEqual((push["attempt"], push["risk"]["code"]), (FORCE_PUSH, "git_force_push"))
        self.assertNotIn("risk", next(r for r in feed if r["what"] == "ls -la"))
        secrets = next(r for r in feed if r["kind"] == "deny" and r["category"] == "secrets")
        self.assertEqual(secrets["attempt"], "")


class TestRecordedRows(RunCase):
    def test_a_deny_row_keeps_the_refused_call_masked(self):
        evt = {"session_id": "sess-1", "tool_name": "Bash", "tool_input": {"command": "curl -H 'Authorization: Bearer abc123' x"}}
        A.record(self.proj, self.rid, evt, "deny", "[git] no", key="k1")
        row_ = A.read_rows(self.proj, self.rid)[-1]
        self.assertEqual(row_["kind"], "deny")
        self.assertIn("curl", row_["cmd"])
        self.assertNotIn("abc123", row_["cmd"])

    def test_long_rows_do_not_rotate_the_file_on_every_append(self):
        evt = {"tool_name": "Bash", "tool_input": {"command": "echo " + "y" * 470}}
        path = paths.run_dir(self.proj, self.rid) / "activity.jsonl"
        rotated = 0
        real = A._rotate

        def counting(p):
            nonlocal rotated
            rotated += 1
            return real(p)
        with mock.patch.object(A, "_rotate", counting):
            for _ in range(1600):
                A.record(self.proj, self.rid, evt, "tool")
        self.assertLessEqual(path.stat().st_size, A.MAX_BYTES + 2048)
        self.assertLess(rotated, 20)                                            # it used to be once per append
        self.assertEqual(len(A.read_all_rows(self.proj, self.rid)) >= 1600, True)

    def test_a_view_has_the_risk_keys_and_a_closed_view_is_reused(self):
        A.record(self.proj, self.rid, {"tool_name": "Bash", "tool_input": {"command": "rm -rf ~/x"}}, "tool")
        views = MON.discover(None, include_all=True)
        v = next(x for x in views if x["run_id"] == self.rid)
        self.assertEqual(v["risk_counts"]["high"], 1)
        self.assertEqual(v["risks"][0]["code"], "rm_outside")
        MON.control_run(self.key, "abort", "")
        MON._closed_cache.clear()
        with mock.patch.object(MON, "_collect_safe", wraps=MON._collect_safe) as spy:
            MON.discover(None, include_all=True, cache=True)
            MON.discover(None, include_all=True, cache=True)
            self.assertEqual(spy.call_count, 1)                                 # the second poll reused the closed view
            state = paths.run_dir(self.proj, self.rid) / "state.json"
            st = state.stat()
            os.utime(state, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            MON.discover(None, include_all=True, cache=True)
            self.assertEqual(spy.call_count, 2)                                 # a changed file is read again
            MON.discover(None, include_all=True)
            self.assertEqual(spy.call_count, 3)                                 # without cache: always fresh


# ================================================================== the page

def risky_view(n, **over):
    v = view(n, **over)
    v["risks"] = [
        {"id": f"r{n}a", "ts": NOW - 30, "level": "high", "code": "git_force_push", "label": "Force push", "tool": "Bash",
         "what": FORCE_PUSH, "agent": "implementer", "main": False, "outcome": "blocked", "count": 1, "guard": "git",
         "reason": "needs your go-ahead"},
        {"id": f"r{n}b", "ts": NOW - 90, "level": "high", "code": "rm_outside", "label": "Recursive delete outside the project",
         "tool": "Bash", "what": "rm -rf ~/old", "agent": "main", "main": True, "outcome": "ran", "count": 2}]
    v["risk_counts"] = {"high": 1, "medium": 0, "blocked": 1}
    v["activity_feed"] = [{"time": "12:00:00", "agent": "main", "kind": "tool", "tool": "Bash", "what": "rm -rf ~/old",
                           "ts": NOW - 90, "id": f"f{n}", "count": 1, "main": True,
                           "risk": {"level": "high", "code": "rm_outside", "label": "Recursive delete outside the project"}}]
    return v


@unittest.skipUnless(HAVE_NODE, "node is needed to run the page script")
class TestPage(helpers.Sandbox):
    def page(self, code="en", token="tok-9"):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code}):
            with i18n.use_lang(code):
                return MON.page_html("", token=token)

    def run_page(self, views, steps, code="en", token="tok-9"):
        out = run_dashboard(self.tmp, self.page(code, token), views, steps)
        self.assertEqual(out["errors"], [], out["errors"])
        return out

    PAUSE = "const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='ctl'&&n.dataset.action==='pause')[0];b.fire('click');"
    SURE = "H.byId('ctl-sure').checked=true;H.byId('ctl-sure').fire('change');"

    def test_an_open_run_has_pause_and_abort_a_closed_one_has_none(self):
        views = [risky_view(1), view(2, "done", "done", status="done")]
        out = self.run_page(views, [ev("return H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='ctl')"
                                       ".map(b=>[H.cardKey(b),b.dataset.action,b.getAttribute('aria-label'),b.textContent])", "c"),
                                    ev("return H.byId('foot-ro').textContent", "f")])
        self.assertEqual(out["results"]["c"], [["k1", "pause", "Pause proj1 run-1", "ⅡPause"],
                                               ["k1", "abort", "Abort proj1 run-1", "✕Abort"]])
        self.assertIn("Pause and Abort", out["results"]["f"])

    def test_a_read_only_page_shows_no_control(self):
        out = self.run_page([risky_view(1)], [ev("return H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='ctl').length", "n"),
                                               ev("return H.byId('foot-ro').textContent", "f")], token="")
        self.assertEqual(out["results"]["n"], 0)
        self.assertEqual(out["results"]["f"], "Read-only · this computer only")

    def test_a_paused_run_offers_abort_only(self):
        v = risky_view(1, group="paused", code="paused", status="paused")
        out = self.run_page([v], [ev("H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='expand')[0].fire('click');"
                                     "return H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='ctl'&&!n.hidden).map(b=>b.dataset.action)", "a")])
        self.assertEqual(out["results"]["a"], ["abort"])

    def test_pause_asks_first_then_sends_the_token_and_reports_the_result(self):
        out = self.run_page([risky_view(1)], [
            ev(self.PAUSE + "const d=H.byId('ctl');return [d.getAttribute('open'),H.byId('ctl-h').textContent,"
                            "H.byId('ctl-ok').textContent,H.byId('ctl-ok').disabled,H.byId('ctl-project').textContent,"
                            "H.byId('ctl-job').textContent,H.byId('ctl-sure-l').textContent,H.sc.posts.length]", "open"),
            ev("H.byId('ctl-form').fire('submit');return [H.sc.posts.length,H.byId('ctl-err').textContent]", "held"),
            ev(self.SURE + "H.byId('ctl-reason').value='  wrong   way ';H.byId('ctl-form').fire('submit');return H.byId('ctl-ok').textContent", "busy"),
            {"op": "advance", "ms": 50},
            ev("const p=H.sc.posts[0];return [p.url,p.headers['X-Orkestra-Token'],p.headers['Content-Type'],p.body.key,"
               "p.body.action,p.body.reason,p.body.lang,H.byId('ctl').getAttribute('open'),H.byId('toast').textContent,"
               "H.byId('toast').hidden,H.sc.calls.length>1]", "sent")])
        r = out["results"]
        self.assertEqual(r["open"], ["", "Pause this run?", "Pause run", True, "proj1", "Title 1",
                                     "I am sure. Pause this run.", 0])
        self.assertEqual(r["held"], [0, "Tick the confirmation. Pause and Abort do not run from the first click."])
        self.assertEqual(r["busy"], "Pausing…")
        self.assertEqual(r["sent"], ["/api/control", "tok-9", "application/json", "k1", "pause", "wrong way", "en", None,
                                     "proj1 run-1 is paused. Sessions told to stop: 1.", False, True])

    def test_a_refused_request_keeps_the_dialog_open_with_the_reason_under_it(self):
        out = self.run_page([risky_view(1)], [
            {"op": "set", "key": "control", "value": {"ok": False, "error": "token"}}, {"op": "set", "key": "controlStatus", "value": 403},
            ev(self.PAUSE + self.SURE + "H.byId('ctl-form').fire('submit');return 1", "x"), {"op": "advance", "ms": 50},
            ev("return [H.byId('ctl').getAttribute('open'),H.byId('ctl-err').hidden,H.byId('ctl-err').textContent,"
               "H.byId('ctl-ok').textContent,H.byId('ctl-ok').disabled]", "err"),
            {"op": "set", "key": "controlFail", "value": True},
            ev(self.SURE + "H.byId('ctl-form').fire('submit');return 1", "y"), {"op": "advance", "ms": 50},
            ev("return H.byId('ctl-err').textContent", "net")])
        r = out["results"]
        self.assertEqual(r["err"][:2], ["", False])
        self.assertIn("Reload the page", r["err"][2])
        self.assertEqual(r["err"][3:], ["Pause run", False])
        self.assertIn("did not answer", r["net"])

    def test_a_too_long_reason_is_named_under_the_field_and_nothing_is_sent(self):
        out = self.run_page([risky_view(1)], [
            ev(self.PAUSE + "H.byId('ctl-reason').value='x'.repeat(301);H.byId('ctl-reason').fire('input');"
                            "const live=H.byId('ctl-err').textContent;H.byId('ctl-form').fire('submit');"
                            "return [live,H.byId('ctl-err').textContent,H.byId('ctl-reason').getAttribute('aria-invalid'),H.sc.posts.length]", "r")])
        self.assertEqual(out["results"]["r"], ["Keep the reason under 300 characters (now 301).",
                                               "Keep the reason under 300 characters (now 301).", "true", 0])

    def test_escape_and_cancel_close_the_dialog_without_a_request(self):
        out = self.run_page([risky_view(1)], [
            ev(self.PAUSE + "H.byId('ctl-cancel').fire('click');const a=H.byId('ctl').getAttribute('open');"
                            "const b2=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='ctl')[1];b2.fire('click');"
                            "const t=H.byId('ctl-h').textContent;document.fire('keydown',{key:'Escape'});"
                            "return [a,t,H.byId('ctl').getAttribute('open'),H.sc.posts.length]", "r")])
        self.assertEqual(out["results"]["r"], [None, "Abort this run?", None, 0])

    def test_risky_actions_show_in_the_radar_the_card_and_the_feed(self):
        views = [risky_view(1), risky_view(2, group="paused", code="paused", status="paused"), view(3)]
        out = self.run_page(views, [ev(
            "const items=H.find(H.byId('radar-list'),n=>n.tag==='li');"
            "const card=H.card('k1');const box=H.find(card,n=>n.className&&n.className.indexOf('p-risk')===0)[0];"
            "const fr=H.find(card,n=>n.getAttribute&&n.getAttribute('data-risk'))[0];"
            "const row=H.find(H.byId('main'),n=>n.className==='rkn')[0];"
            "return [H.byId('radar').hidden,H.byId('radar-sum').textContent,items.length,items[0].textContent.indexOf('Force push')>=0,"
            "box.hidden,box.dataset.top,H.find(box,n=>n.tag==='li').length,fr.getAttribute('data-risk'),row.textContent,row.hidden]", "r")])
        r = out["results"]["r"]
        self.assertEqual(r[:4], [False, "2 high · 0 medium · 2 blocked", 4, True])
        self.assertEqual(r[4:], [False, "high", 2, "high", "2 risky", False])

    def test_go_to_the_run_opens_and_focuses_it(self):
        views = [risky_view(1, group="done", code="done", status="done")]
        out = self.run_page(views, [ev(
            "const go=H.find(H.byId('radar-list'),n=>n.dataset&&n.dataset.act==='go')[0];go.fire('click');"
            "const a=document.activeElement;return [go.getAttribute('aria-label'),a.tag,H.cardKey(a)]", "r")])
        self.assertEqual(out["results"]["r"], ["Go to proj1 run-1", "article", "k1"])

    def test_turning_on_older_runs_shows_placeholders_until_they_arrive(self):
        out = self.run_page([], [
            {"op": "set", "key": "latency", "value": 400},
            ev("const o=H.byId('older');o.checked=true;o.fire('change');"
               "return [H.find(H.byId('main'),n=>n.className==='skel').length,H.byId('main').textContent.indexOf('No runs yet')]", "during"),
            {"op": "set", "key": "views", "value": [view(1, "done", "done", status="done")]},
            {"op": "advance", "ms": 1000},
            ev("return [H.find(H.byId('main'),n=>n.className==='skel').length,H.sc.calls[H.sc.calls.length-1].url]", "after")])
        self.assertEqual(out["results"]["during"], [1, -1])
        self.assertEqual(out["results"]["after"][0], 0)
        self.assertIn("all=1", out["results"]["after"][1])

    def test_no_risk_means_no_radar(self):
        out = self.run_page([view(1)], [ev("return [H.byId('radar').hidden,H.find(H.card('k1'),n=>n.className==='p-risk')[0].hidden]", "r")])
        self.assertEqual(out["results"]["r"], [True, True])

    def test_turkish_page_speaks_turkish(self):
        out = self.run_page([risky_view(1)], [ev(self.PAUSE + "return [H.byId('ctl-h').textContent,H.byId('radar-h').textContent]", "r")],
                            code="tr")
        self.assertEqual(out["results"]["r"], ["Bu çalışma duraklatılsın mı?",
                                               "Riskli işlemler"])


if __name__ == "__main__":
    unittest.main()
