"""The dashboard as a mission-control page: the ONE verdict per run (attention), the additive JSON, the activity feed, the
keyed DOM updates, polling / offline / render-error handling, per-request language, accessibility numbers and the
"old dashboard still holds the port" detection.

The page script runs for real in node (dash_harness.js: DOM stub + fake clock + fake server); everything else is Python.
The suite is pinned to Turkish (helpers.py), so every test picks its language explicitly."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import socket
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers
from dash_support import HAVE_NODE, run_dashboard
from orkestra_core import i18n
from orkestra_core import monitor as MON
from test_i18n_monitor import MonitorBase, lang, row

NOW = 1_790_000_000.0
BASELINE_KEYS = {  # every key of a view before the redesign: they stay, with their meaning
    "project", "project_name", "run_id", "title", "size", "mode", "engine", "phase", "phase_label", "phase_tr", "phase_index",
    "phase_since", "status", "status_label", "status_tr", "approved", "live", "agents", "stall", "flags", "timing", "tasks",
    "tasks_done", "tasks_total", "current_tasks", "gates", "activity", "ledger", "sessions", "usage", "updated", "artifacts"}
NEW_KEYS = {"key", "now", "home", "attention", "last_event_ts", "state_updated_ts", "phase_history", "phases_skipped",
            "tasks_by_status", "gates_struct", "notices"}
ATTN_KEYS = {"group", "code", "rank", "level", "since", "since_ts", "head", "what", "do", "command", "where", "activity_ts"}
SOURCE = (Path(MON.__file__)).read_text(encoding="utf-8")


def attn(group="running", code="working", since=None, **kw):
    out = {"group": group, "code": code, "rank": MON.GROUP_RANK[group], "level": "run", "since": since,
           "since_ts": NOW - since if since is not None else None, "head": code.replace("_", " ").title(),
           "what": f"what {code}", "do": "", "command": "", "where": "none"}
    out.update(kw)
    return out


def view(n, group="running", code="working", since=None, **over):
    """A complete view as collect() builds it (synthetic: the node tests need exact, repeatable states)."""
    v = {"project": f"/w/p{n}", "project_name": f"proj{n}", "run_id": f"run-{n}", "title": f"Title {n}", "size": "M",
         "mode": "interactive", "engine": "claude", "phase": "implement", "phase_label": "implementation", "phase_tr": "uygulama",
         "phase_index": 5, "phase_since": NOW - 600, "status": "active", "status_label": "active", "status_tr": "aktif",
         "approved": True, "live": {"level": "run", "text": "working"}, "agents": [], "stall": {"cls": 0, "key": "working", "since": None, "text": "working", "loops": []},
         "flags": [], "timing": {"run": 900, "phases": {}, "task_avg": None, "reopens": 0, "tasks_reopened": 0}, "tasks": [],
         "tasks_done": 0, "tasks_total": 0, "current_tasks": [], "gates": {"baseline": "\u2014", "verify": "\u2014"},
         "activity": [], "ledger": [], "sessions": [], "usage": None, "updated": "", "artifacts": f"/w/p{n}/.orkestra/features/run-{n}",
         "key": f"k{n}", "now": NOW, "home": "/Users/me", "attention": attn(group, code, since), "last_event_ts": NOW - 5,
         "state_updated_ts": NOW - 5, "phase_history": [], "phases_skipped": [],
         "tasks_by_status": {"done": 0, "in_progress": 0, "blocked": 0, "pending": 0, "deferred": 0},
         "gates_struct": {"baseline": {"state": "none", "ok": 0, "total": 0}, "verify": {"state": "none", "ok": 0, "total": 0}},
         "notices": []}
    v.update(over)
    return v


def feed_row(i, kind="tool", tool="Bash", what="ls", ago=10, agent="implementer", **kw):
    r = {"time": "10:00:00", "agent": agent, "kind": kind, "tool": tool, "what": what, "ts": NOW - ago,
         "id": f"row{i:04d}", "count": 1, "main": agent == "main"}
    r.update(kw)
    return r


class PageCase(MonitorBase):
    """A page in a language plus the harness to drive it."""

    def page(self, code="en", build=""):
        with lang(code):
            return MON.page_html(build)

    def run_page(self, views, steps=(), code="en", **kw):
        out = run_dashboard(self.tmp, self.page(code, kw.pop("page_build", "")), views, list(steps), **kw)
        self.assertEqual(out["errors"], [], out["errors"])
        return out

    def snap(self, views, steps=(), code="en", **kw):
        out = self.run_page(views, list(steps) + [{"op": "snap", "as": "s"}], code, **kw)
        return out["results"]["s"], out


def ev(code, as_="r"):
    return {"op": "eval", "code": code, "as": as_}


# ================================================================== the attention verdict (server side)

class TestAttention(MonitorBase):
    def test_every_state_of_the_fixture_runs_has_one_verdict(self):
        self.populate()
        want = {"r-hung": ("stalled", "hung"), "r-wait": ("you", "question"), "r-silent": ("stalled", "silent"),
                "r-dead": ("stalled", "dead_wait"), "r-done": ("done", "done"), "r-blocked": ("you", "blocked"),
                "r-paused": ("paused", "paused"), "r-nolog": ("running", "working")}
        heads = {}
        for code in ("en", "tr"):
            with lang(code):
                by_id = {v["run_id"]: v for v in self.views()}
            self.assertEqual({k: (v["attention"]["group"], v["attention"]["code"]) for k, v in by_id.items()}, want, code)
            for rid, v in by_id.items():
                a = v["attention"]
                self.assertEqual(set(a), ATTN_KEYS)
                self.assertEqual(a["rank"], MON.GROUP_RANK[a["group"]])
                self.assertTrue(a["head"] and a["what"], rid)
                heads.setdefault(rid, {})[code] = (a["head"], a["what"], a["do"])
        for rid in want:                                       # group and code are language-free, the words are not
            self.assertNotEqual(heads[rid]["en"][0], heads[rid]["tr"][0], rid)
        self.assertEqual(heads["r-hung"]["en"][0], "Long-running command")
        self.assertEqual(heads["r-hung"]["tr"][0], "Uzun s\u00fcren komut")
        self.assertEqual(heads["r-wait"]["en"][1], "Which colour?")
        self.assertIn("Answer in the Claude window.", heads["r-wait"]["en"][2])

    def test_hung_command_beats_the_quiet_liveness_and_never_mentions_the_model(self):
        self.populate()
        with lang("en"):
            v = {x["run_id"]: x for x in self.views()}["r-hung"]
        a = v["attention"]
        self.assertEqual((a["group"], a["head"]), ("stalled", "Long-running command"))
        self.assertIn("npx jest big.test.ts", a["what"])
        self.assertIn("Ctrl+C", a["do"])
        self.assertNotIn("do not retry", json.dumps(v["notices"]))         # the model-facing sentence is gone from the notices
        self.assertEqual(v["live"]["level"], "run")                          # live / stall stay in the JSON, unchanged
        self.assertEqual(v["stall"]["cls"], 3)

    def test_commands_are_ready_to_paste_and_run_the_right_run(self):
        self.populate()
        with lang("en"):
            by_id = {v["run_id"]: v for v in self.views()}
        blocked = by_id["r-blocked"]["attention"]
        self.assertEqual(blocked["command"], 'orkestra unblock -p "f-blocked" --run r-blocked')
        self.assertEqual(blocked["where"], "terminal")
        self.assertEqual(by_id["r-paused"]["attention"]["command"], "/orkestra:resume")
        self.assertEqual(by_id["r-paused"]["attention"]["where"], "claude")
        self.assertEqual(by_id["r-silent"]["attention"]["command"], "/orkestra:resume")
        self.assertEqual(by_id["r-dead"]["attention"]["command"], "/orkestra:resume")
        self.assertEqual(by_id["r-hung"]["attention"]["command"], "")
        self.assertEqual(by_id["r-hung"]["attention"]["where"], "terminal")

    def stall(self, key="working", cls=0, since=None):
        return {"cls": cls, "key": key, "since": since, "text": key, "loops": []}

    def test_turn_ended_copy_depends_on_the_approval_mode(self):
        st = {"run_id": "r1", "status": "active", "phase": "implement", "engine": "claude", "tasks": {}}
        proj = Path("/w/demo")
        for lg, auto_head, auto_what, man_head in (
                ("en", "Chat is idle", "write \"continue\" in the chat", "Waiting for your reply"),
                ("tr", "Sohbet bekliyor", "sohbete \"devam\" yazman", "Yanıtını bekliyor")):
            with lang(lg):
                auto = MON._attention(dict(st, approval_mode="autopilot"), {"level": "wait"},
                                      self.stall("turn_ended", 1, 420), [], proj, [], NOW, NOW, 0)
                manual = MON._attention(dict(st, approval_mode="manual"), {"level": "wait"},
                                        self.stall("turn_ended", 1, 420), [], proj, [], NOW, NOW, 0)
                legacy = MON._attention(dict(st), {"level": "wait"},
                                        self.stall("turn_ended", 1, 420), [], proj, [], NOW, NOW, 0)   # no approval_mode: old copy
            self.assertEqual(auto["code"], "turn_ended")
            self.assertEqual(auto["head"], auto_head)
            self.assertIn(auto_what, auto["what"])
            self.assertNotIn("\u2014", auto["head"] + auto["what"])
            self.assertNotRegex((auto["head"] + auto["what"]).lower(), r"approv|onay")
            self.assertEqual(legacy["head"], man_head)       # only a state that says autopilot gets the autopilot copy
            self.assertEqual(manual["head"], man_head)
            self.assertNotIn(auto_what, manual["what"])

    def test_the_other_codes_of_the_table(self):
        st = {"run_id": "r1", "status": "active", "phase": "implement", "engine": "claude", "tasks": {}}
        proj = Path("/w/demo")
        cases = [
            (dict(st), {"level": "wait", "text": "x"}, self.stall("turn_ended", 1, 420), [], "you", "turn_ended"),
            (dict(st, phase="clarify"), {"level": "wait", "text": "x"}, self.stall(), [], "running", "quiet"),    # a talk phase with no activity is a measured gap, never a guess about the human
            (dict(st), {"level": "stale", "text": "x"}, self.stall(), [], "stalled", "stale"),
            (dict(st), {"level": "quiet", "text": "x"}, self.stall(), [], "running", "quiet"),
            (dict(st), {"level": "run", "text": "x"}, self.stall(), ["reviewer"], "running", "agents"),
            (dict(st, status="aborted"), {"level": "done", "text": "x"}, self.stall("closed"), [], "done", "aborted"),
            (dict(st, status="awaiting_user", awaiting={"question": ""}), {"level": "wait", "text": "x"},
             self.stall("user", 1, 30), [], "you", "question"),
        ]
        for state, live, stall, agents, group, code in cases:
            for lg in ("en", "tr"):
                with lang(lg):
                    a = MON._attention(state, live, stall, agents, proj, [], NOW, NOW - 700, 1200)
                self.assertEqual((a["group"], a["code"]), (group, code), (lg, code))
                self.assertTrue(a["head"] and a["what"], (lg, code))
        with lang("en"):
            a = MON._attention(dict(st, status="awaiting_user", awaiting={"question": ""}), {"level": "wait"},
                               self.stall("user", 1, 30), [], proj, [], NOW, NOW - 30, 0)
            self.assertEqual(a["what"], "The record says it is waiting for an answer, but no question text was saved.")
            a = MON._attention(dict(st, engine="cursor"), {"level": "wait"}, self.stall("turn_ended", 1, 420), [], proj, [],
                               NOW, NOW, 0)
            self.assertEqual((a["what"], a["do"], a["where"]), ("Cursor finished its turn 7m ago.",
                                                              "Reply in the Cursor chat to continue.", "cursor"))
            proj = self.ws / "demo"
            headless = MON._attention(dict(st, mode="headless"), {"level": "run"}, self.stall("silent", 4, 700), [], proj,
                                      [row(1, "tool", tool="Read", what="x")], NOW, NOW - 700, 0)
            self.assertEqual((headless["command"], headless["where"]), ('orkestra resume -p "demo" --run r1', "terminal"))

    def test_the_silence_badge_follows_the_newest_feature_file(self):
        """A plan written later than the tool log is life. The corner must not keep counting from the run start."""
        proj = self.make_run("shop", "run-quiet", updated_ago=4000, history_ago=4000,
                             rows=[self.ago_row(4000, "tool", tool="Read", what="old")])
        plan = proj / ".orkestra" / "features" / "run-quiet" / "plan.md"
        plan.write_text("plan\n", encoding="utf-8")
        now = time.time()
        os.utime(plan, (now - 120, now - 120))
        st = json.loads((proj / ".orkestra" / "runs" / "run-quiet" / "state.json").read_text(encoding="utf-8"))
        stall = {"cls": 4, "key": "silent", "since": 4000, "text": "silent"}
        with lang("tr"):
            a = MON._attention(st, {"level": "stale"}, stall, [], proj,
                               [{"ts": now - 4000, "kind": "tool", "agent": "main", "tool": "Read", "what": "old"}],
                               now, now - 4000, 4000)
        self.assertEqual(a["code"], "silent")
        self.assertGreaterEqual(a["since"], 100)
        self.assertLess(a["since"], 180)
        self.assertIn("2 dk", a["what"])
        self.assertNotIn("1 sa", a["what"])

    def test_a_live_driver_does_not_inherit_old_silence(self):
        """The corner clock is the current process, not the age of files left by the previous one."""
        proj = self.make_run("shop", "run-restart", updated_ago=4000, history_ago=4000,
                             rows=[self.ago_row(4000, "tool", tool="Read", what="old")])
        plan = proj / ".orkestra" / "features" / "run-restart" / "plan.md"
        plan.write_text("plan\n", encoding="utf-8")
        now = time.time()
        os.utime(plan, (now - 4000, now - 4000))
        st = json.loads((proj / ".orkestra" / "runs" / "run-restart" / "state.json").read_text(encoding="utf-8"))
        stall = {"cls": 4, "key": "silent", "since": 4000, "text": "silent"}
        rows = [{"ts": now - 4000, "kind": "tool", "agent": "main", "tool": "Read", "what": "old"}]
        with lang("tr"):
            young = MON._attention(st, {"level": "stale"}, stall, [], proj, rows, now, now - 4000, 4000,
                                   driver_born=now - 70)
            mid = MON._attention(st, {"level": "stale"}, stall, [], proj, rows, now, now - 4000, 4000,
                                 driver_born=now - 200)
            aged = MON._attention(st, {"level": "stale"}, stall, [], proj, rows, now, now - 4000, 4000,
                                  driver_born=now - 400)
            untouched = MON._attention(st, {"level": "stale"}, stall, [], proj, rows, now, now - 4000, 4000)
        self.assertEqual((young["group"], young["code"], young["since"]), ("running", "working", None))
        self.assertEqual(mid["group"], "running")
        self.assertEqual(mid["code"], "quiet")
        self.assertGreaterEqual(mid["since"], 180)
        self.assertLess(mid["since"], 220)
        self.assertEqual(aged["code"], "silent")
        self.assertGreaterEqual(aged["since"], 380)
        self.assertLess(aged["since"], 430)
        self.assertEqual(untouched["code"], "silent")
        self.assertGreaterEqual(untouched["since"], 3900)

    def test_a_quiet_card_does_not_inherit_old_files(self):
        """The quiet line uses the live process age, not a file left by the previous turn."""
        proj = self.make_run("shop", "run-quiet-live", updated_ago=4000, history_ago=4000,
                             rows=[self.ago_row(4000, "tool", tool="Read", what="old")])
        now = time.time()
        st = json.loads((proj / ".orkestra" / "runs" / "run-quiet-live" / "state.json").read_text(encoding="utf-8"))
        stall = {"cls": 0, "key": "ok", "since": 4000, "text": "ok"}
        rows = [{"ts": now - 400, "kind": "tool", "agent": "main", "tool": "Read", "what": "old"}]
        with lang("tr"):
            capped = MON._attention(st, {"level": "quiet"}, stall, [], proj, rows, now, now - 400, 4000,
                                    driver_born=now - 120)
            young = MON._attention(st, {"level": "quiet"}, stall, [], proj, rows, now, now - 400, 4000,
                                   driver_born=now - 40)
        self.assertEqual(capped["code"], "quiet")
        self.assertGreaterEqual(capped["since"], 100)
        self.assertLess(capped["since"], 140)
        self.assertEqual((young["group"], young["code"], young["since"]), ("running", "working", None))

    def test_driver_age_uses_the_youngest_matching_process(self):
        self.assertEqual(MON._etime_seconds("01:23"), 83)
        self.assertEqual(MON._etime_seconds("1-00:00:05"), 86405)
        now = 1_000_000.0
        sid = "abcdef12-3456-4000-8000-000000000000"
        rows = [(400, f"agent --resume {sid}"), (80, f"agent --resume {sid} --model grok")]
        self.assertEqual(MON.driver_born_at({"session_id": sid}, [], now, rows), now - 80)
        self.assertEqual(MON.driver_born_at({"session_id": "no-such-session"}, [], now, rows), 0.0)

    def test_long_reasons_are_not_cut_mid_token_and_the_cap_is_marked(self):
        reason = "needs credentials: " + "x" * 400
        st = {"run_id": "r1", "status": "blocked", "phase": "plan", "blocked": {"reason": reason}}
        with lang("en"):
            a = MON._attention(st, {"level": "blocked"}, self.stall("blocked", 1, 100), [], Path("/w/demo"), [], NOW, NOW, 0)
        self.assertEqual(a["what"], reason)                                  # live.text keeps its [:220]; attention does not
        with lang("en"):
            a = MON._attention(dict(st, blocked={"reason": "y" * 900}), {"level": "blocked"}, self.stall("blocked", 1, 5), [],
                               Path("/w/demo"), [], NOW, NOW, 0)
        self.assertEqual(len(a["what"]), 600)
        self.assertTrue(a["what"].endswith("\u2026"))


# ================================================================== the additive JSON

class TestViewJson(MonitorBase):
    def test_the_view_is_a_superset_of_what_it_was_and_serialisable(self):
        self.populate()
        with lang("en"):
            views = self.views()
        for v in views:
            self.assertTrue(BASELINE_KEYS <= set(v), BASELINE_KEYS - set(v))
            self.assertTrue(NEW_KEYS <= set(v), NEW_KEYS - set(v))
            self.assertEqual(json.loads(json.dumps(v, ensure_ascii=False)), v)
            self.assertRegex(v["key"], r"^[0-9a-f]{12}$")
            self.assertEqual(v["home"], os.path.expanduser("~"))
        self.assertEqual(len({v["key"] for v in views}), len(views))

    def test_a_cursor_driver_is_shown_even_when_the_run_was_recorded_as_claude(self):
        """Cursor CLI binds after start. The stored origin stays claude; the card must say cursor."""
        proj = self.make_run("shop", "run-cursor", engine="claude")
        d = self.home / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cursor-11111111-2222-4333-8444-555555555555.json").write_text(json.dumps({
            "project": str(proj), "run_id": "run-cursor", "engine": "cursor", "bound_at": self.iso(time.time()),
        }), encoding="utf-8")
        with lang("tr"):
            v = {x["run_id"]: x for x in self.views()}["run-cursor"]
        self.assertEqual(v["engine"], "cursor")
        raw = (proj / ".orkestra" / "runs" / "run-cursor" / "state.json").read_text(encoding="utf-8")
        self.assertIn('"engine": "claude"', raw)

    def test_the_card_names_the_model_of_the_live_driver(self):
        """The tag is the model on that run's own process, not the engine name and not a configured default."""
        proj = self.make_run("shop", "run-model", engine="cursor")
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        d = self.home / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"cursor-{sid}.json").write_text(json.dumps({
            "project": str(proj), "run_id": "run-model", "engine": "cursor", "bound_at": self.iso(time.time()),
        }), encoding="utf-8")
        own = f"agent -p --resume {sid} --model grok-4.7-xhigh Orkestra run. Model: something-else"
        other = "agent -p --resume bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee --model other-model do it"
        prompt_only = f"agent -p --resume {sid} the prompt mentions grok-4.7-xhigh but names no flag"
        with mock.patch.object(MON, "_agent_commands", return_value=[other, own]):
            v = {x["run_id"]: x for x in self.views()}["run-model"]
        self.assertEqual(v["model"], "grok-4.7-xhigh")
        self.assertIn("v.model", SOURCE)
        with mock.patch.object(MON, "_agent_commands", return_value=[prompt_only]):
            bare = {x["run_id"]: x for x in self.views()}["run-model"]
        self.assertEqual(bare["model"], "")

    def test_the_key_is_the_project_path_and_the_run_not_the_folder_name(self):
        self.make_run("shop", "r-same")
        other = self.ws / "clients" / "shop"
        run = other / ".orkestra" / "runs" / "r-same"
        run.mkdir(parents=True)
        (other / ".orkestra" / "features" / "r-same").mkdir(parents=True)
        st = json.loads((self.ws / "shop" / ".orkestra" / "runs" / "r-same" / "state.json").read_text())
        (run / "state.json").write_text(json.dumps(st), encoding="utf-8")
        keys = [v["key"] for v in MON.discover(include_all=True)]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2, "two projects with the same folder name must not share a key")

    def test_tasks_phases_gates_and_notices(self):
        self.populate()
        with lang("en"):
            v = {x["run_id"]: x for x in self.views()}["r-hung"]
        self.assertEqual(v["tasks_by_status"], {"done": 1, "in_progress": 1, "blocked": 0, "pending": 0, "deferred": 0})
        self.assertEqual([t["tests"] for t in v["tasks"]], ["passed", "none"])
        self.assertTrue(v["tasks_show_tests"])
        self.assertEqual([t["evidence"] for t in v["tasks"]], ["RG", "--"])      # the cryptic code stays for old consumers
        self.assertEqual(v["gates_struct"]["baseline"], {"state": "pass", "ok": 2, "total": 2})
        verify = v["gates_struct"]["verify"]
        self.assertEqual((verify["state"], verify["ok"], verify["total"], verify["regressions"]), ("fail", 1, 2, ["unit", "lint"]))
        self.assertEqual(v["gates"]["baseline"], "2/2 passed")                      # the localized sentence stays
        self.assertEqual(v["phase_history"][0]["phase"], "implement")
        self.assertEqual(v["phases_skipped"], [])        # the history does not start at the first phase: nothing is claimed skipped
        for n in v["notices"]:
            self.assertIn(n["severity"], ("warn", "info"))
            self.assertNotIn("do not retry", n["text"])
            self.assertNotIn("orkestra playbook", n["text"])
        done = {x["run_id"]: x for x in self.views()}["r-done"]
        self.assertEqual(done["notices"], [])                                       # a finished run shows no model instruction

    def test_skipped_phases_come_from_the_history(self):
        now = time.time()
        hist = [{"to": p, "at": self.iso(now - 5000 + i * 100)} for i, p in enumerate(["intake", "discover", "plan", "approve", "implement"])]
        self.make_run("hist", "r-h", history_ago=0, extra={"history": hist, "phase": "implement"})
        v = MON.discover(include_all=True)[0]
        self.assertEqual(v["phases_skipped"], ["clarify"])
        self.assertEqual([h["phase"] for h in v["phase_history"]], ["intake", "discover", "plan", "approve", "implement"])
        self.assertTrue(all(h["seconds"] >= 0 for h in v["phase_history"]))

    def test_the_response_headers_and_the_csp(self):
        base = self.serve_api()
        for path in ("/", "/api/runs"):
            resp = urllib.request.urlopen(base + path, timeout=5)
            self.assertRegex(resp.headers["X-Orkestra-Time"], r"^\d+\.\d{3}$")
            self.assertEqual(resp.headers["Content-Security-Policy"],
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.assertEqual(resp.headers["Cache-Control"], "no-store")
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
            self.assertTrue(resp.headers["Server"].startswith("orkestra-dashboard"))
        self.assertIsInstance(json.loads(urllib.request.urlopen(base + "/api/runs", timeout=5).read()), list)   # still a bare list

    def serve_api(self, project=None):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        fresh = MON._Freshness()
        srv = ThreadingHTTPServer(("127.0.0.1", port), MON.make_handler(project, port, fresh))
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), thread.join(5)))
        return f"http://127.0.0.1:{port}"

    def test_the_language_is_chosen_per_request_and_never_leaks(self):
        self.populate()
        base = self.serve_api()
        results = {}

        def fetch(i, code):
            for _ in range(6):
                page = urllib.request.urlopen(f"{base}/?lang={code}", timeout=10).read().decode("utf-8")
                api = json.loads(urllib.request.urlopen(f"{base}/api/runs?lang={code}&all=1", timeout=10).read().decode("utf-8"))
                results.setdefault(code, []).append((page, api))

        with lang("tr"):                                                  # the server's own language: Turkish
            threads = [threading.Thread(target=fetch, args=(i, c)) for i, c in enumerate(("en", "tr", "en", "tr"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(results["en"]), 12)
        for page, api in results["en"]:
            self.assertIn('<html lang="en">', page)
            hung = {v["run_id"]: v for v in api}["r-hung"]
            self.assertEqual(hung["attention"]["head"], "Long-running command")
            self.assertEqual(hung["gates"]["baseline"], "2/2 passed")
            self.assertEqual(hung["phase_label"], "implementation")
        for page, api in results["tr"]:
            self.assertIn('<html lang="tr">', page)
            hung = {v["run_id"]: v for v in api}["r-hung"]
            self.assertEqual(hung["attention"]["head"], "Uzun s\u00fcren komut")
            self.assertEqual(hung["gates"]["baseline"], "2/2 ge\u00e7ti")
        for bad in ("xx", "", "tr;en", "../tr", "%00"):                    # unsupported values keep the server's language
            page = urllib.request.urlopen(f"{base}/?lang={bad}", timeout=5).read().decode("utf-8")
            self.assertIn('<html lang="tr">', page, bad)
        english = urllib.request.urlopen(base + "/?lang=en", timeout=5).read().decode("utf-8")
        self.assertEqual(re.findall("[\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc]", english), [])
        self.assertNotIn("\u2014", english)

    def test_the_trust_line_data_is_in_the_page(self):
        with lang("en"):
            page = MON.page_html("abc123def456", started=NOW)
        meta = re.search(r"const META=(\{.*?\});", page).group(1)
        data = json.loads(meta)
        self.assertEqual((data["build"], data["started"]), ("abc123def456", NOW))
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+")
        self.assertEqual(data["workspace"], MON.paths.workspace_display())
        self.assertNotRegex(data["workspace"], r"^/(?:Users|home)/[^/]+/")
        self.assertIn('const BUILD="abc123def456"', page)


# ================================================================== activity feed (server side)

class TestFeedData(unittest.TestCase):
    def rows(self):
        # the pre-tool hook writes a `tool` row for an allowed call and ONLY a `deny` row for a refused one
        return [
            {"ts": 1001.0, "kind": "deny", "agent": "orkestra:implementer", "tool": "Bash", "what": "[deps] not in the plan: pick an installed one"},
            {"ts": 1101.0, "kind": "deny", "agent": "orkestra:implementer", "tool": "Bash", "what": "[deps] not in the plan: pick an installed one"},
            {"ts": 1200.0, "kind": "tool", "agent": "main", "tool": "Read", "what": "a.py"},
            {"ts": 1201.0, "kind": "tool", "agent": "main", "tool": "Read", "what": "a.py"},
            {"ts": 1202.0, "kind": "tool", "agent": "main", "tool": "Read", "what": "a.py"},
            {"ts": 1300.0, "kind": "tool", "agent": "main", "tool": "Bash", "what": "ls"},
            {"ts": 1330.0, "kind": "deny", "agent": "main", "tool": "Bash", "what": "no bracket here"},
        ]

    def test_a_refused_call_is_one_guard_row_with_its_reason_and_repeats_merge(self):
        feed = MON._activity_feed(self.rows(), 14)
        denies = [r for r in feed if r["kind"] == "deny"]
        self.assertEqual(len(denies), 2)
        deps = next(r for r in denies if r["category"] == "deps")
        self.assertEqual((deps["repeat_count"], deps["count"]), (2, 2))
        self.assertEqual(deps["reason"], "not in the plan: pick an installed one")
        self.assertEqual(deps["ts"], 1101.0)                                     # the latest repeat
        general = next(r for r in denies if r["category"] == "general")
        self.assertEqual((general["reason"], general["attempt"]), ("no bracket here", ""))

    def test_an_earlier_allowed_command_is_never_shown_as_the_blocked_one(self):
        """The hook records no tool row for a refused call, so the row before a deny is another, ALLOWED call: it keeps its
        own row and the guard row names no command (it used to say "Blocked: ls -la" and hide the ls row)."""
        rows = [{"ts": 2000.0, "kind": "tool", "agent": "orkestra:implementer", "tool": "Bash", "what": "ls -la"},
                {"ts": 2000.5, "kind": "end", "agent": "orkestra:implementer", "tool": "Bash", "what": ""},
                {"ts": 2001.0, "kind": "deny", "agent": "orkestra:implementer", "tool": "Bash", "what": "[internals] do not touch the hooks"}]
        for compact in (True, False):
            feed = MON._activity_feed(rows, 14, compact=compact)
            self.assertEqual([(r["kind"], r["what"]) for r in feed][0], ("tool", "ls -la"), compact)
            deny = feed[-1]
            self.assertEqual((deny["kind"], deny["category"], deny["attempt"]), ("deny", "internals", ""), compact)

    def test_identical_consecutive_rows_collapse_with_a_count(self):
        feed = MON._activity_feed(self.rows(), 14)
        reads = [r for r in feed if r["tool"] == "Read"]
        self.assertEqual(len(reads), 1)
        self.assertEqual((reads[0]["count"], reads[0]["ts"]), (3, 1202.0))

    def test_rows_have_stable_ids_epoch_times_and_the_main_flag(self):
        a, b = MON._activity_feed(self.rows(), 14), MON._activity_feed(self.rows()[:-2], 14)
        ids = [r["id"] for r in a]
        self.assertEqual(len(set(ids)), len(ids))
        for r in a:
            self.assertRegex(r["id"], r"^[0-9a-f]{10}$")
            self.assertIsInstance(r["ts"], float)
            self.assertNotIn("_agent", r)
        first = {r["id"] for r in b}
        self.assertTrue(first <= set(ids), "appending rows never renames the older ones")
        self.assertEqual([r["main"] for r in a][-1], True)
        self.assertEqual(sum(1 for r in a if r["main"] is False), 1)
        self.assertEqual(len(MON._activity_feed(self.rows(), 2)), 2)
        self.assertEqual(MON._activity_feed(self.rows(), 0), [])

    def test_the_short_form_vectors(self):
        ctx = ("/Users/me/Projects/app", "/Users/me")
        cases = [
            ("Bash", "cd /Users/me/Projects/app && orkestra phase next 2>&1 | tee /tmp/out.log | tail -40", "orkestra phase next"),
            ("Bash", "cd /Users/me/Projects/app/sub; npm test | head -20", "(sub) npm test"),
            ("Read", "/Users/me/.cursor/projects/x/terminals/1.txt", "~/.cursor/projects/x/terminals/1.txt"),
            ("Read", "/Users/me/Projects/app/src/a.ts", "src/a.ts"),
            ("Grep", "mushaf /Users/me/Projects/app/.orkestra/memory", "mushaf .orkestra/memory"),
            ("Edit", "/etc/hosts", "/etc/hosts"),
            ("Bash", "echo   hi", "echo hi"),
        ]
        for tool, what, want in cases:
            self.assertEqual(MON.short_what(tool, what, *ctx), want)
        long_cmd = ("cd /Users/me/Projects/other && docker compose -f deploy/compose.measure.yml run --rm measure npm run "
                    "overpass:bench -- --region=istanbul --out geom 2>&1 | tee /tmp/b.log | tail -60")
        out = MON.short_what("Bash", long_cmd, *ctx)
        self.assertTrue(out.startswith("(~/Projects/other) docker compose -f deploy/comp"), out)
        self.assertTrue(out.endswith("--out geom"), out)
        self.assertEqual(len(out), 88)
        self.assertLessEqual(len(MON.short_what("Bash", long_cmd, *ctx, limit=80)), 80)


# ================================================================== the page: structure, accessibility numbers

def srgb(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def luminance(color):
    return sum(w * (v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4) for w, v in zip((0.2126, 0.7152, 0.0722), srgb(color)))


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class TestPageStyle(PageCase):
    def css(self):
        return re.search(r"<style>(.*?)</style>", self.page("en"), re.S).group(1)

    def tokens(self, block):
        return dict(re.findall(r"(--[a-z0-9-]+):(#[0-9a-fA-F]{6})", block))

    def themes(self):
        css = self.css()
        light = self.tokens(re.search(r":root\{(.*?)\}", css, re.S).group(1))
        dark = self.tokens(re.search(r'@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}\}', css, re.S).group(1))
        dark2 = self.tokens(re.search(r':root\[data-theme="dark"\]\{(.*?)\}', css, re.S).group(1))
        return light, dark, dark2

    def test_the_dark_theme_is_declared_twice_the_same(self):
        light, dark, dark2 = self.themes()
        self.assertEqual(dark, dark2)
        self.assertTrue(set(dark) <= set(light))
        css = self.css()
        self.assertIn("body{margin:0;background:var(--bg)", css)               # explicit background
        self.assertIn("color-scheme:light dark", css)

    def test_every_pair_meets_the_contrast_floor_in_both_themes(self):
        light, dark, _ = self.themes()
        for name, theme in (("light", light), ("dark", dark)):
            t = dict(light, **theme)
            text = [("--ink", "--bg"), ("--ink", "--surface"), ("--ink2", "--bg"), ("--ink2", "--surface"), ("--focus", "--bg"),
                    ("--focus", "--surface")]
            for g in ("you", "stall", "run", "pause", "done"):
                text.append((f"--{g}-fg", f"--{g}-bg"))
                text.append((f"--{g}-fg", "--surface"))
            for fg, bg in text:
                self.assertGreaterEqual(contrast(t[fg], t[bg]), 4.5, f"{name}: {fg} on {bg}")
            nontext = [("--ctl", "--surface"), ("--focus", "--surface"), ("--focus", "--bg")]
            nontext += [(f"--{g}-edge", "--surface") for g in ("you", "stall", "run", "pause", "done")]
            for fg, bg in nontext:
                self.assertGreaterEqual(contrast(t[fg], t[bg]), 3.0, f"{name}: {fg} on {bg}")
            self.assertGreaterEqual(contrast(t["--ink2"], t["--surface"]), 7.0, name)     # secondary text: never opacity

    @staticmethod
    def without_block(css, opener):
        """`css` without the `opener{...}` block (braces matched, nested rules included)."""
        start = css.index(opener)
        depth, i = 0, css.index("{", start)
        while True:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
            if depth == 0:
                return css[:start] + css[i:]

    def test_no_state_is_expressed_by_opacity(self):
        css = self.without_block(self.css(), "@media (prefers-reduced-motion:no-preference)")   # the dot's pulse is motion, not state
        self.assertNotRegex(css, r"opacity\s*:\s*(?!1\s*[;}])\.?\d")      # `opacity:1` (a reset) is no state; `.5` / `0` would be
        self.assertNotIn(".past", css)

    def test_motion_only_exists_when_the_visitor_allows_it(self):
        css = self.css()
        outside = self.without_block(css, "@media (prefers-reduced-motion:no-preference)")
        self.assertNotRegex(outside, r"animation\s*:")
        self.assertNotRegex(outside, r"transition\s*:")
        self.assertRegex(css, r"@media \(prefers-reduced-motion:no-preference\)\{[^@]*animation\s*:")
        self.assertRegex(css, r"@media print")
        self.assertRegex(css, r"@media \(forced-colors:active\)")
        self.assertRegex(css, r"@media \(prefers-contrast:more\)")

    def test_focus_ring_skip_link_and_sticky_header_offset(self):
        css = self.css()
        self.assertIn(":focus-visible{outline:2px solid var(--focus)", css)
        self.assertIn("scroll-padding-top", css)
        self.assertIn("viewport-fit=cover", self.page("en"))
        page = self.page("en")
        self.assertEqual(len(re.findall(r"<h1[ >]", page)), 1)
        self.assertIn('<a class="skip" href="#main">', page)
        self.assertIn('<main id="main"', page)
        self.assertIn('aria-live="polite"', page)

    def test_open_cards_do_not_stretch_across_the_whole_main(self):
        """Three open runs fit in three columns. A lone card stays at reading width (520px),
        not stretched across the 1560px main. A definite 520px track max only fitted two."""
        css = self.css()
        self.assertIn("repeat(auto-fit,minmax(min(100%,320px),1fr))", css)
        self.assertIn(".grid>.card{width:100%;max-width:520px}", css)
        self.assertNotIn("minmax(min(100%,420px),520px)", css)
        self.assertRegex(css, r"\.list\{[^}]*grid-template-columns:minmax\(0,1fr\);max-width:960px")
        self.assertRegex(css, r"@media\(min-width:601px\)\{[^}]*\.compact\{[^}]*grid-template-columns:auto minmax\(0,1fr\) auto")
        self.assertNotRegex(css, r"\.compact\{[^}]*max-width:560px")
        self.assertIn("max-height:min(40vh,280px)", css)

    def test_task_column_keeps_whole_words(self):
        """Body overflow-wrap:anywhere must not stack Görev or a task title one letter per line."""
        css = self.css()
        self.assertIn(".tt th,.tt td{overflow-wrap:normal;word-break:normal}", css)
        self.assertIn(".tt th{", css)
        self.assertIn("white-space:nowrap", css.split(".tt th{")[1].split("}")[0])
        self.assertIn(".tt td.task{min-width:12em;max-width:28em;white-space:normal;overflow-wrap:break-word}", css)
        self.assertIn('el("td","task"', self.page("en"))

    def test_the_page_stays_self_contained_and_never_builds_markup_from_text(self):
        page = self.page("en")
        script = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "dangerouslySet"):
            self.assertNotIn(banned, script, banned)
        self.assertNotRegex(page, r"""(?:src|href)\s*=\s*["']https?://""")
        self.assertNotRegex(script, r"""fetch\(\s*["']https?://""")
        self.assertNotRegex(self.css(), r"url\(|@import|@font-face")
        # two fetch calls, both to the page's own origin: the poll, and `req` that carries every click of the person
        # (Pause / Abort and the console); `req` is only ever given a path of this server
        self.assertEqual(script.count("fetch("), 2)
        self.assertIn('fetch(url(),{cache:"no-store"', script)
        self.assertIn('const r=await fetch(path,{method:post?"POST":"GET"', script)
        targets = re.findall(r"(?<!function )\breq\(([^,)]+)", script)
        self.assertTrue(targets)
        for target in targets:
            self.assertRegex(target, r'^(?:"/api/(?:control|console/[a-z]+)"|cpath\("[a-z]+")', target)
        self.assertIn('"/api/runs?lang="', script)
        self.assertNotIn("innerHTML", SOURCE.split('PAGE = r"""')[1].split('"""')[0])


# ================================================================== the page script: sections, sorting, filters

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestPageSections(PageCase):
    def mixed(self):
        return [view(1, "paused", "paused", status="paused"), view(2, "running", "working"), view(3, "you", "question", 420),
                view(4, "stalled", "hung", 1800), view(5, "done", "done", status="done", state_updated_ts=NOW - 3600),
                view(6, "you", "blocked", 5200), view(7, "stalled", "silent", 3000), view(8, "you", "turn_ended", 430)]

    def test_sections_are_in_the_fixed_order_and_sorted_by_how_long_they_wait(self):
        snap, _ = self.snap(self.mixed())
        self.assertEqual([s["g"] for s in snap["sections"]], ["you", "stalled", "running", "paused", "done"])
        keys = {s["g"]: [i["key"] for i in s["items"]] for s in snap["sections"]}
        self.assertEqual(keys["you"], ["k6", "k3", "k8"])                  # evidence first (blocked 5200 s, question 420 s), a turn that only ended (430 s) after them
        self.assertEqual(keys["stalled"], ["k7", "k4"])
        self.assertEqual({s["g"]: s["count"] for s in snap["sections"]}, {"you": "3", "stalled": "2", "running": "1",
                                                                          "paused": "1", "done": "1"})
        kinds = {i["key"]: i["kind"] for s in snap["sections"] for i in s["items"]}
        self.assertEqual((kinds["k6"], kinds["k4"], kinds["k2"], kinds["k1"], kinds["k5"]), ("card", "card", "card", "item", "item"))

    def test_running_cards_are_longest_first(self):
        def aged(n, seconds):
            v = view(n, "running", "working")
            v["timing"] = dict(v["timing"], run=seconds)
            return v
        snap, _ = self.snap([aged(1, 100), aged(2, 5000), aged(3, 800), aged(4, 5000)])
        keys = {s["g"]: [i["key"] for i in s["items"]] for s in snap["sections"]}
        self.assertEqual(keys["running"], ["k2", "k4", "k3", "k1"])

    def test_summary_chips_title_count_and_calm_line(self):
        snap, _ = self.snap(self.mixed())
        self.assertEqual([c["text"].replace("\u25c6", "").replace("\u2717", "").replace("\u25cf", "").replace("\u2161", "")
                          .replace("\u2713", "") for c in snap["chips"]],
                         ["All8", "Needs you3", "Needs a look2", "Running1", "Paused1", "Finished1"])
        self.assertEqual(snap["title"], "(5) Orkestra \u00b7 live")
        self.assertEqual(snap["calm"], "")
        self.assertEqual([c["pressed"] for c in snap["chips"]], ["true"] + ["false"] * 5)
        calm, _ = self.snap([view(1, "running", "working"), view(2, "done", "done", status="done")])
        self.assertEqual(calm["title"], "Orkestra \u00b7 live")
        self.assertEqual(calm["calm"], "Nothing needs you right now.")
        tr, _ = self.snap(self.mixed(), code="tr")
        self.assertEqual(tr["title"], "(5) Orkestra \u00b7 canl\u0131")

    def test_the_title_only_changes_when_the_count_does(self):
        views = self.mixed()
        fewer = [view(i, "running", "working") for i in range(1, 4)] + views[2:3]               # one run still needs you
        out = self.run_page(views, [{"op": "advance", "ms": 7000}, ev("return [document.title,H.titleWrites]", "same"),
                                    {"op": "set", "key": "views", "value": fewer}, {"op": "advance", "ms": 2100},
                                    ev("return [document.title,H.titleWrites]", "changed")])
        self.assertEqual(out["results"]["same"], ["(5) Orkestra \u00b7 live", 1])                 # four more ticks, one write
        self.assertEqual(out["results"]["changed"], ["(1) Orkestra \u00b7 live", 2])

    def test_filters_search_and_the_filtered_empty_state(self):
        views = self.mixed()
        snap, _ = self.snap(views, [ev("S.chips.you.b.fire('click');S.chips.stalled.b.fire('click');return 0", "x")])
        self.assertEqual([s["g"] for s in snap["sections"]], ["you", "stalled"])      # multi-select: both groups
        self.assertEqual([c["pressed"] for c in snap["chips"]], ["false", "true", "true", "false", "false", "false"])
        snap, _ = self.snap(views, [ev("S.chips.you.b.fire('click');S.chips.all.b.fire('click');return 0", "x")])
        self.assertEqual(len(snap["sections"]), 5)                                    # All clears the selection
        snap, _ = self.snap(views, [ev("const q=H.byId('q');q.value='PROJ4';q.fire('input');return 0", "x")])
        self.assertEqual([[i["key"] for i in s["items"]] for s in snap["sections"]], [["k4"]])
        snap, out = self.snap(views, [ev("const q=H.byId('q');q.value='zzz';q.fire('input');return 0", "x")])
        self.assertEqual(snap["sections"], [])
        self.assertIn("No runs match your filters.", snap["other"][0])
        out2 = self.run_page(views, [ev("const q=H.byId('q');q.value='zzz';q.fire('input');"
                                        "H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='clear')[0].fire('click');"
                                        "return [H.byId('q').value,S.q]", "r")])
        self.assertEqual(out2["results"]["r"], ["", ""])
        # the search also reads the head and the sentence of the verdict
        views[0]["attention"]["what"] = "Needle in the verdict"
        snap, _ = self.snap(views, [ev("const q=H.byId('q');q.value='needle';q.fire('input');return 0", "x")])
        self.assertEqual([[i["key"] for i in s["items"]] for s in snap["sections"]], [["k1"]])
        views[3]["attention"]["head"] = "Hung command"
        snap, _ = self.snap(views, [ev("const q=H.byId('q');q.value='HUNG COMMAND';q.fire('input');return 0", "x")])
        self.assertEqual([[i["key"] for i in s["items"]] for s in snap["sections"]], [["k4"]])
        snap, _ = self.snap(views, [ev("S.chips.you.b.fire('click');return localStorage._m['orkestra.dash.filter']", "x")])
        remembered = self.run_page(views, [ev("return Array.from(S.filter)", "f")], options={"local": {"orkestra.dash.filter": '["done"]'}})
        self.assertEqual(remembered["results"]["f"], ["done"])                          # the filter is remembered per viewer

    def test_turkish_search_lowercases_the_turkish_way(self):
        v = view(1, "running", "working", project_name="\u0130STANBUL")
        snap, _ = self.snap([v], [ev("const q=H.byId('q');q.value='istanbul';q.fire('input');return 0", "x")], code="tr")
        self.assertEqual(len(snap["sections"]), 1)

    def test_finished_runs_collapse_to_five_with_a_show_more_button(self):
        views = [view(i, "done", "done", status="done", state_updated_ts=NOW - i * 100) for i in range(1, 9)]
        snap, _ = self.snap(views)
        done = snap["sections"][0]
        self.assertEqual([i["key"] for i in done["items"]], ["k1", "k2", "k3", "k4", "k5"])       # newest finished first
        self.assertEqual(done["more"], "Show 3 more finished runs")
        snap, _ = self.snap(views, [ev("H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='finmore')[0].fire('click');return 0", "x")])
        self.assertEqual(len(snap["sections"][0]["items"]), 8)
        self.assertEqual(snap["sections"][0]["more"], "Show less")

    def test_a_compact_row_opens_its_full_card_in_place(self):
        views = [view(1, "paused", "paused", status="paused")]
        out = self.run_page(views, [ev("const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='expand')[0];"
                                       "const before=[b.getAttribute('aria-expanded'),H.find(H.byId('main'),n=>n.tag==='article').length];"
                                       "b.fire('click');"
                                       "const after=[b.getAttribute('aria-expanded'),H.find(H.byId('main'),n=>n.tag==='article').length,"
                                       "H.find(H.byId('main'),n=>n.tag==='article')[0].hidden];"
                                       "b.fire('click');const closed=H.find(H.byId('main'),n=>n.tag==='article')[0].hidden;"
                                       "return {before,after,closed}", "r")])
        r = out["results"]["r"]
        self.assertEqual(r["before"], ["false", 0])
        self.assertEqual(r["after"], ["true", 1, False])
        self.assertTrue(r["closed"])

    def test_the_empty_state_names_the_workspace_and_offers_the_commands(self):
        snap, _ = self.snap([])
        text = snap["other"][0]
        self.assertIn("No runs yet", text)
        shown = MON.paths.workspace_display()
        self.assertIn(shown, text)
        self.assertNotRegex(text, r"/(?:Users|home)/[^/\s]+/")
        self.assertIn("/orkestra:run describe the work", text)
        self.assertIn('orkestra start --title "Short title" --request "What to build"', text)
        self.assertIn("Include runs older than 12 hours", text)
        tr, _ = self.snap([], code="tr")
        self.assertIn("Hen\u00fcz \u00e7al\u0131\u015fma yok", tr["other"][0])
        self.assertIn("/orkestra:run i\u015fi anlat", tr["other"][0])
        self.assertIn(shown, tr["other"][0])

    def test_many_runs_keep_the_dom_small_and_the_finished_list_short(self):
        views = [view(i, ["you", "stalled", "running", "paused", "done"][i % 5], "working", i, status="done" if i % 5 == 4 else "active",
                      activity=[feed_row(j, what=f"cmd {j}") for j in range(14)]) for i in range(200)]
        out = self.run_page(views, [ev("return [H.nodes(), S.many, H.find(H.byId('main'),n=>n.tag==='article').length]", "r"),
                                    {"op": "snap", "as": "s"}])
        nodes, many, cards = out["results"]["r"]
        self.assertTrue(many)
        self.assertLess(nodes, 6000, nodes)
        self.assertLessEqual(cards, 24)
        done = [s for s in out["results"]["s"]["sections"] if s["g"] == "done"][0]
        self.assertEqual(len(done["items"]), 5)

    def test_a_hostile_run_is_text_and_nothing_else(self):
        evil = '<img src=x onerror=alert(1)></script><script>alert(2)</script>'
        v = view(1, "you", "question", 100, project_name=evil, title=evil, run_id=evil, artifacts=evil,
                 attention=attn("you", "question", 100, what=evil, do=evil, command=evil, head=evil),
                 activity=[feed_row(1, what=evil), feed_row(2, kind="deny", tool="Bash", what="[" + evil + "] " + evil,
                                                           category=evil, reason=evil, attempt=evil)],
                 notices=[{"code": "guard_loop", "severity": "warn", "text": evil}], ledger=[evil],
                 sessions=[{"session": evil, "bound_at": ""}], tasks=[{"id": evil, "status": "done", "title": evil, "tests": "none", "evidence": "--"}],
                 tasks_total=1, tasks_done=1)
        out = self.run_page([v], [ev("const bad=H.find(H.byId('main'),n=>['img','script','iframe'].includes(n.tag));"
                                     "const t=H.byId('main').textContent;return [bad.length,t.split(" + json.dumps(evil) + ").length-1]", "r")])
        bad, occurrences = out["results"]["r"]
        self.assertEqual(bad, 0)
        self.assertGreater(occurrences, 5)                                     # shown as text, many times, never interpreted


# ================================================================== the card

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestCard(PageCase):
    def card_text(self, v, steps=(), code="en"):
        snap, _ = self.snap([v], steps, code)
        return snap["sections"][0]["items"][0]["text"]

    def test_the_verdict_card_says_what_and_what_to_do(self):
        v = view(1, "you", "blocked", 600, status="blocked",
                 attention=attn("you", "blocked", 600, head="Blocked", what="needs credentials", do="Resolve the blocker:",
                                command='orkestra unblock -p "demo" --run r1', where="terminal"))
        text = self.card_text(v)
        for part in ("Blocked", "waiting 10m", "needs credentials", "Resolve the blocker:", 'orkestra unblock -p "demo" --run r1', "Copy"):
            self.assertIn(part, text)
        self.assertIn("proj1", text)
        self.assertNotIn("working", text.split("Details")[0].split("proj1")[0])            # no second verdict bar

    def test_the_command_copies_and_announces_it(self):
        v = view(1, "stalled", "silent", 700, attention=attn("stalled", "silent", 700, command="/orkestra:resume", where="claude"))
        out = self.run_page([v], [ev("const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='copy')[0];"
                                     "const label=b.getAttribute('aria-label');b.fire('click');await H.settle();"
                                     "return [label,H.clipboard,H.byId('live').textContent.trim()]", "r"),
                                    {"op": "advance", "ms": 10}])
        label, clip, live = out["results"]["r"]
        self.assertEqual((label, clip), ("Copy: /orkestra:resume", ["/orkestra:resume"]))
        self.assertEqual(live, "Copied")

    def test_copy_falls_back_to_selecting_the_text(self):
        v = view(1, "stalled", "silent", 700, attention=attn("stalled", "silent", 700, command="/orkestra:resume", where="claude"))
        out = self.run_page([v], [ev("const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='copy')[0];b.fire('click');"
                                     "return [H.byId('live').textContent.trim(),H.sel.isCollapsed]", "r")],
                            options={"noClipboard": True})
        self.assertEqual(out["results"]["r"], ["Copy failed: select the text and press Ctrl+C", True])   # stub range: no selection object

    def test_the_stepper_has_one_item_per_phase_with_text_for_every_state(self):
        v = view(1, "running", "working", phase_index=5, phase_history=[
            {"phase": "intake", "at": NOW - 7200, "seconds": 300}, {"phase": "plan", "at": NOW - 6900, "seconds": 2400}],
                 phases_skipped=["discover", "clarify"])
        out = self.run_page([v], [ev("const ol=H.find(H.byId('main'),n=>n.tag==='ol')[0];"
                                     "return [ol.getAttribute('aria-label'),ol.children.map(li=>[li.textContent,li.className,li.getAttribute('aria-current'),li.title])]", "r")])
        label, items = out["results"]["r"]
        self.assertEqual(label, "Phase 6 of 10: implementation")
        self.assertEqual(len(items), 10)
        states = [i[0].split(": ")[1] for i in items]
        self.assertEqual(states, ["completed", "skipped", "skipped", "completed", "completed", "current", "upcoming", "upcoming",
                                  "upcoming", "upcoming"])
        self.assertEqual([i[2] for i in items].count("step"), 1)
        self.assertEqual(items[3][3], "planning \u00b7 40m")
        self.assertEqual({i[1] for i in items}, {"sd", "sk", "sc", ""})                  # fill / dashed / current / hollow
        tr = self.run_page([v], [ev("return H.find(H.byId('main'),n=>n.tag==='ol')[0].children.map(li=>li.textContent)", "r")], code="tr")
        self.assertEqual(tr["results"]["r"][0], "giri\u015f: tamamland\u0131")

    def test_a_finished_run_shows_every_phase_completed(self):
        v = view(1, "done", "done", status="done", phase="done", phase_index=9)
        out = self.run_page([v], [EXPAND, ev("return H.find(H.byId('main'),n=>n.tag==='ol')[0].children.map(li=>li.textContent.split(': ')[1])", "r")])
        self.assertEqual(set(out["results"]["r"]), {"completed"})

    def test_tasks_are_a_bar_a_line_and_a_table_on_demand(self):
        tasks = [{"id": f"T{i:03d}", "status": s, "title": f"Task {i}", "tests": t, "evidence": "--"}
                 for i, (s, t) in enumerate([("done", "red_green")] * 4 + [("in_progress", "red"), ("blocked", "none"), ("pending", "none")], 1)]
        v = view(1, "running", "working", tasks=tasks, tasks_total=7, tasks_done=4, current_tasks=[tasks[4]],
                 tasks_by_status={"done": 4, "in_progress": 1, "blocked": 1, "pending": 1, "deferred": 0})
        out = self.run_page([v], [ev("const m=H.byId('main');const tbar=H.find(m,n=>n.className==='tbar')[0];"
                                     "const before=H.find(m,n=>n.tag==='table').length;"
                                     "H.find(m,n=>n.dataset&&n.dataset.act==='tasks')[0].fire('click');"
                                     "const t=H.find(m,n=>n.tag==='table')[0];"
                                     "return {aria:tbar.getAttribute('aria-label'),role:tbar.getAttribute('role'),line:H.find(m,n=>n.className==='tl')[0].textContent,"
                                     "before,heads:H.find(t,n=>n.tag==='th').map(h=>[h.textContent,h.getAttribute('scope')]),"
                                     "rows:H.find(t,n=>n.tag==='tr').slice(1).map(r=>r.textContent),caption:H.find(t,n=>n.tag==='caption')[0].textContent}", "r")])
        r = out["results"]["r"]
        self.assertEqual(r["aria"], "4/7 done, 1 in progress, 1 blocked, 1 to do")
        self.assertEqual(r["role"], "img")
        self.assertEqual(r["line"], "4/7 done \u00b7 1 in progress \u00b7 1 blocked \u00b7 1 to do")
        self.assertEqual(r["before"], 0)                                                  # nothing is rendered inline as chips
        self.assertEqual(r["heads"], [["ID", "col"], ["Task", "col"], ["Status", "col"], ["Tests", "col"]])
        self.assertEqual(r["rows"][0], "T001Task 1\u2713 Completed\u2713 All tests passed")
        self.assertEqual(r["rows"][4], "T005Task 5\u25d0 In progress\u25d0 Awaiting green")
        self.assertEqual(r["rows"][5], "T006Task 6\u25a0 Blocked\u25cb Not run yet")
        self.assertEqual(r["caption"], "tasks")

    def test_tests_column_hidden_when_every_task_skips_tdd(self):
        tasks = [{"id": "T001", "status": "done", "title": "Docs only", "tests": "na", "tdd": "n/a", "evidence": "--"},
                 {"id": "T002", "status": "done", "title": "Ship", "tests": "na", "tdd": "n/a", "evidence": "--"}]
        v = view(1, "running", "working", tasks=tasks, tasks_total=2, tasks_done=2, tasks_show_tests=False,
                 tasks_by_status={"done": 2, "in_progress": 0, "blocked": 0, "pending": 0, "deferred": 0})
        out = self.run_page([v], [ev("const m=H.byId('main');"
                                     "H.find(m,n=>n.dataset&&n.dataset.act==='tasks')[0].fire('click');"
                                     "const t=H.find(m,n=>n.tag==='table')[0];"
                                     "return {heads:H.find(t,n=>n.tag==='th').map(h=>h.textContent),"
                                     "cls:t.className,cells:H.find(t,n=>n.tag==='tr')[1].children.length}", "r")])
        r = out["results"]["r"]
        self.assertEqual(r["heads"], ["ID", "Task", "Status"])
        self.assertIn("no-tests", r["cls"])
        self.assertEqual(r["cells"], 3)

    def test_health_chips_flag_the_regression_with_a_glyph_and_the_notices_are_deduped(self):
        v = view(1, "running", "working", gates={"baseline": "6/6 passed", "verify": "5/6 passed \u2717 regression: i18n-parity"},
                 gates_struct={"baseline": {"state": "pass", "ok": 6, "total": 6}, "verify": {"state": "fail", "ok": 5, "total": 6, "regressions": ["i18n-parity"]}},
                 timing={"run": 900, "phases": {}, "task_avg": None, "reopens": 2, "tasks_reopened": 2},
                 notices=[{"code": "a", "severity": "warn", "text": f"notice {i}"} for i in range(4)])
        out = self.run_page([v], [ev("const m=H.byId('main');const chips=H.find(m,n=>n.className&&n.className.startsWith('gchip'));"
                                     "const notes=H.find(m,n=>n.className==='notice').map(n=>n.textContent);"
                                     "H.find(m,n=>n.dataset&&n.dataset.act==='notices')[0].fire('click');"
                                     "return {chips:chips.map(c=>[c.textContent,c.className]),notes,all:H.find(m,n=>n.className==='notice').length}", "r")])
        r = out["results"]["r"]
        self.assertEqual(r["chips"][0], ["Baseline 6/6 passed", "gchip"])
        self.assertEqual(r["chips"][1], ["Verify 5/6 passed \u2717 regression: i18n-parity", "gchip bad"])
        self.assertEqual(r["chips"][2][0], "review rejects 2 (tasks affected: 2)")
        self.assertEqual(r["notes"], ["\u2691 notice 0", "\u2691 notice 1"])                  # two, then "Show 2 more"
        self.assertEqual(r["all"], 4)

    def test_long_titles_and_paths_wrap_instead_of_overflowing(self):
        css = re.search(r"<style>(.*?)</style>", self.page("en"), re.S).group(1)
        self.assertIn("overflow-wrap:anywhere", css.split("body{")[1].split("}")[0])
        self.assertIn("-webkit-line-clamp:2", css)
        v = view(1, "running", "working", title="T" * 400)
        out = self.run_page([v], [ev("return H.find(H.byId('main'),n=>n.className==='title')[0].getAttribute('title').length", "r")])
        self.assertEqual(out["results"]["r"], 400)                                         # the full text stays reachable

    def test_details_keep_the_technical_verdict_and_the_run_folder(self):
        v = view(1, "stalled", "hung", 1800, live={"level": "quiet", "text": "verify phase open, last hook activity 30m ago"},
                 stall={"cls": 3, "key": "hung", "since": 1800, "text": "hung command (main, 30m): docker compose", "loops": []})
        text = self.card_text(v)
        self.assertIn("Technical verdict: verify phase open, last hook activity 30m ago", text)
        self.assertIn("HUNG COMMAND: hung command (main, 30m): docker compose", text)
        self.assertIn("Run folder: /w/p1/.orkestra/features/run-1", text)
        self.assertIn("Last activity 5s ago", text)
        quiet = self.card_text(view(2, "running", "working", last_event_ts=NOW - 3000, state_updated_ts=NOW - 3000))
        self.assertIn("No activity for 50m", quiet)

    def test_relative_times_tick_and_carry_the_absolute_time(self):
        v = view(1, "you", "question", 60)
        out = self.run_page([v], [ev("const t=H.find(H.byId('main'),n=>n.className==='age')[0];const a=[t.textContent];"
                                     "return a", "a"), {"op": "advance", "ms": 125000},
                                  ev("const t=H.find(H.byId('main'),n=>n.className==='age')[0];"
                                     "return [t.textContent,t.getAttribute('datetime'),t.title]", "b")])
        self.assertEqual(out["results"]["a"], ["waiting 1m"])
        text, dt, title = out["results"]["b"]
        self.assertEqual(text, "waiting 3m")
        self.assertRegex(dt, r"^2026-")
        self.assertRegex(title, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")


EXPAND = {"op": "eval", "code": "H.find(H.byId('main'), n => n.dataset && n.dataset.act === 'expand').forEach(b => b.fire('click'));"}


# ================================================================== the feed

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestFeedPage(PageCase):
    def rows_of(self, rows, steps=(), code="en", **over):
        v = view(1, "running", "working", activity=rows, **over)
        out = self.run_page([v], list(steps) + [ev("return H.find(H.byId('main'),n=>n.tag==='li'&&n.className.startsWith('fr')).map(li=>li.textContent)", "rows")])
        return out["results"]["rows"], out

    def test_noise_collapses_and_the_story_reads_newest_first(self):
        rows = [feed_row(1, "tool", "Read", "a.py", 100), feed_row(2, "tool", "Grep", "x", 99), feed_row(3, "tool", "Glob", "*.ts", 98),
                feed_row(4, "tool", "Bash", "cd /w/p1 && npm test 2>&1 | tee /tmp/o | tail -30", 60),
                feed_row(5, "result", "Agent", "<- reviewer: async_launched", 50), feed_row(6, "agent_done", "", "reviewer done", 40, agent="orkestra:reviewer"),
                feed_row(7, "handback", "", "report text", 30), feed_row(8, "idle", "", "turn ended", 20)]
        texts, _ = self.rows_of(rows)
        self.assertEqual(len(texts), 4)                                       # the newest four; the rest is one click away
        self.assertTrue(texts[0].startswith("Turn ended"))
        self.assertTrue(texts[1].startswith("Report"))
        self.assertTrue(texts[2].startswith("Agent done"))
        self.assertTrue(texts[3].startswith("Result"))
        more, _ = self.rows_of(rows, [ev("H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='feedmore')[0].fire('click');return 0", "x")])
        self.assertEqual(len(more), 6)
        self.assertTrue(any(t.startswith("Read") and "3 reads and searches" in t for t in more))
        self.assertFalse(any("cd /w/p1" in t or "tee /tmp" in t for t in more), "the presentation noise is removed")

    def test_nine_reads_in_a_row_are_one_row_and_a_lone_read_shows_its_path(self):
        rows = [feed_row(i, "tool", "Read", f"/Users/me/Projects/app/src/f{i}.ts", 100 - i) for i in range(9)]
        texts, out = self.rows_of(rows, project="/Users/me/Projects/app")
        self.assertEqual(len(texts), 1)
        self.assertIn("9 reads and searches", texts[0])
        lone, _ = self.rows_of([feed_row(1, "tool", "Read", "/Users/me/Projects/app/src/a.ts", 20)], project="/Users/me/Projects/app")
        self.assertIn("src/a.ts", lone[0])

    def test_a_blocked_command_is_one_guard_row_with_its_category(self):
        deny = feed_row(1, "deny", "Bash", "[deps] not in the plan", 30, count=4, repeat_count=4, category="deps", reason="not in the plan",
                        attempt="npm i overpass-turbo-cli")
        texts, _ = self.rows_of([deny])
        self.assertEqual(len(texts), 1)
        for part in ("Guard \u00d74", "Blocked: npm i overpass-turbo-cli", "deps guard", "implementer"):
            self.assertIn(part, texts[0])
        tr, _ = self.rows_of([deny], code="tr")
        self.assertEqual(tr, texts)                                           # same data: this test pins English
        with lang("tr"):
            page = MON.page_html()
        out = run_dashboard(self.tmp, page, [view(1, activity=[deny])], [ev("return H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr')[0].textContent", "t")])
        self.assertIn("Koruma \u00d74", out["results"]["t"])
        self.assertIn("Engellendi: npm i overpass-turbo-cli", out["results"]["t"])

    def test_the_full_command_is_one_click_away_and_never_altered(self):
        long_cmd = "cd /w/p1 && " + "docker compose -f deploy/compose.measure.yml run --rm measure npm run bench -- --region=x " * 2 + "2>&1 | tee /tmp/b.log | tail -60"
        out = self.run_page([view(1, activity=[feed_row(1, what=long_cmd, ago=10)])],
                            [ev("const m=H.byId('main');const li=H.find(m,n=>n.tag==='li'&&n.className==='fr')[0];"
                                "const tog=H.find(li,n=>n.dataset&&n.dataset.act==='rowfull')[0];const before=[tog.textContent,tog.getAttribute('aria-expanded')];"
                                "const sum=H.find(li,n=>n.className==='sum')[0];const title=sum.getAttribute('title');"
                                "tog.fire('click');const pre=H.find(li,n=>n.tag==='pre')[0];"
                                "return {before,after:[tog.textContent,tog.getAttribute('aria-expanded')],full:pre.textContent,title,sum:sum.textContent}", "r")])
        r = out["results"]["r"]
        self.assertEqual(r["before"], ["Show full command", "false"])
        self.assertEqual(r["after"], ["Hide full command", "true"])
        self.assertEqual(r["full"], long_cmd)
        self.assertEqual(r["title"], long_cmd)
        self.assertNotIn("cd /w/p1", r["sum"])
        self.assertNotIn("tee /tmp", r["sum"])

    def test_every_row_has_a_relative_and_an_absolute_time(self):
        rows = [feed_row(4, what="id", ago=90000), feed_row(3, what="whoami", ago=5580), feed_row(2, what="pwd", ago=420), feed_row(1, ago=12)]
        out = self.run_page([view(1, activity=rows)], [ev("return H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr').map(li=>{"
                                                          "const t=H.find(li,n=>n.tag==='time')[0];return [t.textContent,t.title,t.getAttribute('datetime')]})", "r")])
        times = out["results"]["r"]
        self.assertEqual([t[0] for t in times][:3], ["12s ago", "7m ago", "1h 33m ago"])
        self.assertNotRegex(times[3][0], r"ago$")                                   # a day old: a date, not "25h ago"
        for _, title, dt in times:
            self.assertRegex(title, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
            self.assertRegex(dt, r"T.*Z$")

    def test_the_empty_feed_says_how_logging_starts_and_the_cap_is_marked(self):
        texts, _ = self.rows_of([])
        self.assertEqual(texts, ["No activity recorded yet. Logging starts with the first tool call."])
        rows = [feed_row(i, what=f"cmd{i}", ago=100 - i) for i in range(14)]
        texts, _ = self.rows_of(rows)
        self.assertEqual(len(texts), 4)

    def test_new_rows_are_added_without_touching_the_old_ones(self):
        rows = [feed_row(i, what=f"cmd{i}", ago=100 - i * 10) for i in range(3)]
        newer = rows + [feed_row(9, what="fresh", ago=1)]
        out = self.run_page([view(1, activity=rows)], [
            ev("const m=H.byId('main');window.__lis=H.find(m,n=>n.tag==='li'&&n.className==='fr');"
               "H.mut.log.length=0;return window.__lis.length", "n"),
            {"op": "set", "key": "views", "value": [view(1, activity=newer)]}, {"op": "advance", "ms": 2100},
            ev("const m=H.byId('main');const now=H.find(m,n=>n.tag==='li'&&n.className.startsWith('fr'));"
               "return {same:window.__lis.every(li=>now.includes(li)),n:now.length,first:now[0].textContent.slice(0,12),"
               "oldTouched:H.mut.log.filter(x=>x.el.tag!=='time'&&window.__lis.some(li=>li.contains(x.el))).length}", "r")])
        r = out["results"]["r"]
        self.assertTrue(r["same"])
        self.assertEqual(r["oldTouched"], 0)                                        # older rows: no write at all (but their time)
        self.assertEqual(r["n"], 4)
        self.assertTrue(r["first"].startswith("Run"))


# ================================================================== keyed updates: nothing the user is doing is lost

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestKeyedUpdates(PageCase):
    def fleet(self):
        return [view(1, "you", "question", 420, activity=[feed_row(1, ago=30), feed_row(2, what="pwd", ago=20)]),
                view(2, "stalled", "hung", 1800, activity=[feed_row(3, ago=30)]),
                view(3, "running", "working", activity=[feed_row(4, ago=30)]),
                view(4, "paused", "paused", status="paused"), view(5, "done", "done", status="done")]

    def test_identical_ticks_change_nothing_and_keep_every_node(self):
        steps = [ev("window.__nodes=H.find(H.byId('main'),()=>true);H.mut.log.length=0;H.mut.n=0;return window.__nodes.length", "n"),
                 {"op": "advance", "ms": 12100},                                              # six ticks of identical data
                 ev("const now=H.find(H.byId('main'),()=>true);"
                    "const real=H.mut.log.filter(m=>{let n=m.el;while(n){if(n.tag==='time')return false;n=n.parentNode;}return true;});"
                    "return {same:window.__nodes.length===now.length&&window.__nodes.every((n,i)=>n===now[i]),real:real.map(m=>m.kind+':'+m.el.tag+':'+m.el.className),calls:S.cards.size}", "r")]
        out = self.run_page(self.fleet(), steps)
        r = out["results"]["r"]
        self.assertTrue(r["same"], "a tick rebuilt DOM nodes")
        self.assertEqual(r["real"], [], "a tick with identical data wrote to the DOM")
        self.assertGreaterEqual(len(out["calls"]), 7)

    def test_a_selection_open_details_and_focus_survive_a_tick_with_new_data(self):
        original = self.fleet()
        original[0]["attention"]["what"] = "The first sentence is long enough to select forty-three characters"
        changed = copy.deepcopy(original)
        changed[0]["attention"]["what"] = "A new sentence"
        changed[0]["activity"].append(feed_row(7, what="fresh", ago=1))
        steps = [ev("const key='k1';const card=H.card(key);const what=H.find(card,n=>n.className==='now')[0];"
                    "H.select(what,0,43);const det=H.find(card,n=>n.tag==='details'&&n.className==='dtl')[0];det.open=true;await H.settle();"
                    "const btn=H.find(card,n=>n.tag==='summary')[0];btn.focus();window.__what=what;window.__det=det;window.__card=card;return what.textContent.length", "a"),
                 {"op": "set", "key": "views", "value": changed}, {"op": "advance", "ms": 2100},
                 ev("const card=H.card('k1');const what=H.find(card,n=>n.className==='now')[0];"
                    "return {sameCard:card===window.__card,sameWhat:what===window.__what,text:what.textContent,selected:H.sel._text.length,"
                    "collapsed:H.sel.isCollapsed,anchorInside:window.__what.contains(H.sel.anchorNode),"
                    "open:window.__det.open,focus:document.activeElement===window.__card||window.__card.contains(document.activeElement),"
                    "feed:H.find(card,n=>n.tag==='li'&&n.className.startsWith('fr')).map(li=>li.textContent.slice(0,6))}", "r"),
                 ev("H.collapse();return 0", "x"), {"op": "advance", "ms": 50},
                 ev("return H.find(H.card('k1'),n=>n.className==='now')[0].textContent", "after")]
        out = self.run_page(original, steps)
        r = out["results"]["r"]
        self.assertTrue(r["sameCard"])
        self.assertTrue(r["open"])
        self.assertTrue(r["focus"])
        self.assertNotEqual(r["text"], "A new sentence", "the part with the selection is not rewritten under the user's hands")
        self.assertTrue(r["sameWhat"])
        self.assertEqual((r["selected"], r["collapsed"], r["anchorInside"]), (43, False, True))
        self.assertEqual(out["results"]["after"], "A new sentence", "it is applied once the selection is gone")
        self.assertTrue(any(t.startswith("Run") for t in r["feed"]), "the other parts of the card are not held back")

    def test_a_long_selection_is_applied_after_ten_seconds(self):
        changed = self.fleet()
        changed[0]["attention"]["what"] = "Later sentence"
        steps = [ev("const what=H.find(H.card('k1'),n=>n.className==='now')[0];H.select(what,0,10);return 1", "a"),
                 {"op": "set", "key": "views", "value": changed}, {"op": "advance", "ms": 4000},
                 ev("return H.find(H.card('k1'),n=>n.className==='now')[0].textContent", "early"),
                 {"op": "advance", "ms": 9000}, ev("return H.find(H.card('k1'),n=>n.className==='now')[0].textContent", "late")]
        out = self.run_page(self.fleet(), steps)
        self.assertNotEqual(out["results"]["early"], "Later sentence")
        self.assertEqual(out["results"]["late"], "Later sentence")

    def test_a_reorder_never_moves_the_card_that_has_focus(self):
        views = [view(1, "you", "question", 900), view(2, "you", "question", 500), view(3, "you", "question", 100)]
        flipped = [view(1, "you", "question", 50), view(2, "you", "question", 500), view(3, "you", "question", 900)]
        steps = [ev("const c=H.card('k1');c.focus();window.__c=c;H.mut.log.length=0;return H.find(H.byId('main'),n=>n.tag==='article').map(n=>n.dataset.key)", "a"),
                 {"op": "set", "key": "views", "value": flipped}, {"op": "advance", "ms": 2100},
                 ev("const keys=H.find(H.byId('main'),n=>n.tag==='article').map(n=>n.dataset.key);"
                    "return {keys,reinserted:H.mut.log.filter(m=>m.node===window.__c).length,focused:document.activeElement===window.__c}", "r"),
                 ev("document.activeElement=document.body;H.mut.log.length=0;return 0", "x"), {"op": "advance", "ms": 2100},
                 ev("return H.find(H.byId('main'),n=>n.tag==='article').map(n=>n.dataset.key)", "settled")]
        out = self.run_page(views, steps)
        self.assertEqual(out["results"]["a"], ["k1", "k2", "k3"])
        self.assertEqual(out["results"]["r"]["reinserted"], 0)             # the focused card was never taken out of the page
        self.assertTrue(out["results"]["r"]["focused"])
        self.assertEqual(out["results"]["settled"], ["k3", "k2", "k1"])    # the order follows once nothing is held

    def test_no_reorder_while_a_pointer_is_down(self):
        views = [view(1, "you", "question", 900), view(2, "you", "question", 500)]
        flipped = [view(1, "you", "question", 10), view(2, "you", "question", 500)]
        steps = [{"op": "fire", "target": "doc", "type": "pointerdown"}, {"op": "set", "key": "views", "value": flipped},
                 {"op": "advance", "ms": 2100}, ev("return H.find(H.byId('main'),n=>n.tag==='article').map(n=>n.dataset.key)", "down"),
                 {"op": "fire", "target": "doc", "type": "pointerup"}, {"op": "advance", "ms": 2100},
                 ev("return H.find(H.byId('main'),n=>n.tag==='article').map(n=>n.dataset.key)", "up")]
        out = self.run_page(views, steps)
        self.assertEqual(out["results"]["down"], ["k1", "k2"])
        self.assertEqual(out["results"]["up"], ["k2", "k1"])

    def test_a_run_that_changes_group_moves_and_the_open_state_is_kept(self):
        views = self.fleet()
        moved = copy.deepcopy(views)
        moved[2] = view(3, "stalled", "silent", 700)
        steps = [ev("const d=H.find(H.card('k3'),n=>n.tag==='details'&&n.className==='act')[0];d.open=false;await H.settle();window.__c=H.card('k3');return 1", "a"),
                 {"op": "set", "key": "views", "value": moved}, {"op": "advance", "ms": 2100}, {"op": "snap", "as": "s"},
                 ev("const d=H.find(H.card('k3'),n=>n.tag==='details'&&n.className==='act')[0];return [window.__c===H.card('k3'),d.open]", "r")]
        out = self.run_page(views, steps)
        stalled = [s for s in out["results"]["s"]["sections"] if s["g"] == "stalled"][0]
        self.assertIn("k3", [i["key"] for i in stalled["items"]])
        self.assertEqual(out["results"]["r"], [True, False], "same node, and the closed Activity stays closed")

    def test_group_changes_are_announced_once_and_throttled(self):
        views = [view(1, "running", "working"), view(2, "running", "working")]
        waiting = [view(1, "you", "question", 5), view(2, "stalled", "silent", 700)]
        steps = [ev("return H.byId('live').textContent", "none"), {"op": "set", "key": "views", "value": waiting}, {"op": "advance", "ms": 2100},
                 ev("return H.byId('live').textContent.trim()", "first"),
                 {"op": "set", "key": "views", "value": views}, {"op": "advance", "ms": 2100},
                 ev("return H.byId('live').textContent.trim()", "calm"), {"op": "advance", "ms": 12000},
                 ev("return H.byId('live').textContent.trim()", "later")]
        out = self.run_page(views, steps)
        res = out["results"]
        self.assertEqual(res["none"], "")
        self.assertEqual(res["first"], "A run needs you: proj1. A run needs a look: proj2")
        self.assertEqual(res["calm"], res["first"], "within ten seconds nothing new is spoken")
        self.assertEqual(res["later"], "Nothing needs you now.")

    def test_removed_runs_leave_and_new_ones_appear(self):
        steps = [{"op": "set", "key": "views", "value": self.fleet()[:2] + [view(9, "running", "working")]}, {"op": "advance", "ms": 2100}, {"op": "snap", "as": "s"}]
        out = self.run_page(self.fleet(), steps)
        keys = [i["key"] for s in out["results"]["s"]["sections"] for i in s["items"]]
        self.assertEqual(sorted(keys), ["k1", "k2", "k9"])


# ================================================================== polling, connection, errors, language boot

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestPolling(PageCase):
    def test_the_delay_sequence(self):
        out = self.run_page([], [ev("return [OrkDash.nextDelay(0,false,0.5),OrkDash.nextDelay(0,true,0.5),[1,2,3,4,5,9].map(n=>OrkDash.nextDelay(n,false,0.5)),"
                                    "[OrkDash.nextDelay(1,false,0),OrkDash.nextDelay(1,false,1)]]", "r")])
        visible, hidden, backoff, jitter = out["results"]["r"]
        self.assertEqual((visible, hidden), (2000, 10000))
        self.assertEqual(backoff, [2000, 4000, 8000, 8000, 8000, 8000])               # the server is on this computer: never wait long
        self.assertEqual(jitter, [1600, 2400])                                       # +-20 %

    def test_a_slow_server_never_has_two_requests_in_flight(self):
        out = self.run_page([view(1)], [{"op": "set", "key": "latency", "value": 4500}, {"op": "advance", "ms": 30000}])
        self.assertEqual(out["maxInflight"], 1)
        gaps = [b["at"] - a["at"] for a, b in zip(out["calls"], out["calls"][1:])]
        self.assertTrue(all(g >= 2000 for g in gaps), gaps)

    def test_a_request_that_hangs_is_aborted_after_five_seconds(self):
        out = self.run_page([view(1)], [{"op": "set", "key": "latency", "value": 60000}, {"op": "advance", "ms": 2100}, {"op": "advance", "ms": 5200},
                                        {"op": "snap", "as": "s"}])
        self.assertEqual(out["maxInflight"], 1)
        self.assertIn("Connection lost", out["results"]["s"]["conn"])

    def test_the_page_polls_the_visible_rate_and_slows_down_in_the_background(self):
        out = self.run_page([view(1)], [{"op": "advance", "ms": 10000}])
        self.assertEqual(len(out["calls"]), 6)                                       # the first one + one every 2 s
        hidden = self.run_page([view(1)], [{"op": "advance", "ms": 30000}], options={"hidden": True})
        self.assertLessEqual(len(hidden["calls"]), 4)

    def test_becoming_visible_again_polls_at_once(self):
        steps = [{"op": "advance", "ms": 5000}, ev("return H.sc.calls.length", "before"),
                 ev("document.hidden=false;return 0", "y"), {"op": "fire", "target": "doc", "type": "visibilitychange"},
                 ev("return H.sc.calls.length", "after")]
        out = self.run_page([view(1)], steps, options={"hidden": True})           # in a background tab the next poll is 10 s away
        self.assertEqual((out["results"]["before"], out["results"]["after"]), (1, 2))
        self.assertAlmostEqual((out["calls"][1]["at"] - out["calls"][0]["at"]) / 1000, 5.1, delta=0.2)    # at once, not after 10 s
        online = self.run_page([view(1)], [ev("document.hidden=true;return 0", "x"), {"op": "advance", "ms": 100},
                                          {"op": "fire", "target": "win", "type": "online"}, ev("return H.sc.calls.length", "n")])
        self.assertEqual(online["results"]["n"], 2)

    def test_the_page_sends_its_language_and_the_older_toggle_with_every_poll(self):
        out = self.run_page([view(1)], [ev("const o=H.byId('older');o.checked=true;o.fire('change');return 0", "x"), {"op": "advance", "ms": 50}], code="tr")
        self.assertEqual(out["calls"][0]["url"], "/api/runs?lang=tr")
        self.assertEqual(out["calls"][-1]["url"], "/api/runs?lang=tr&all=1")
        en = self.run_page([view(1)])
        self.assertEqual(en["calls"][0]["url"], "/api/runs?lang=en")

    def test_the_older_toggle_is_remembered_and_the_page_works_with_storage_blocked(self):
        out = self.run_page([view(1)], [ev("const o=H.byId('older');o.checked=true;o.fire('change');return localStorage._m['orkestra.dash.older']", "r")])
        self.assertEqual(out["results"]["r"], "1")
        restored = self.run_page([view(1)], options={"local": {"orkestra.dash.older": "1"}})
        self.assertTrue(restored["calls"][0]["url"].endswith("&all=1"))
        snap, _ = self.snap([view(1, "you", "question", 5)], [ev("const o=H.byId('older');o.checked=true;o.fire('change');return 0", "x")],
                            options={"storage": "throw"})
        self.assertEqual(len(snap["sections"]), 1)                                  # no storage, no problem

    def test_connection_chip_reconnecting_offline_and_back(self):
        steps = [{"op": "snap", "as": "live"}, {"op": "set", "key": "fail", "value": True}, {"op": "advance", "ms": 2100},
                 {"op": "snap", "as": "retry"}, {"op": "advance", "ms": 12000}, {"op": "snap", "as": "offline"},
                 {"op": "set", "key": "fail", "value": False}, {"op": "advance", "ms": 20000}, {"op": "snap", "as": "back"}]
        out = self.run_page([view(1, "you", "question", 100), view(2)], steps)
        res = out["results"]
        self.assertEqual((res["live"]["conn"], res["live"]["connState"]), ("Live \u00b7 updated just now", "live"))
        self.assertRegex(res["retry"]["conn"], r"^Connection lost\. Retrying in \d+ s$")
        self.assertEqual(res["retry"]["connState"], "retry")
        self.assertEqual(len(res["retry"]["sections"]), 2)                           # the data stays on screen
        off = res["offline"]
        self.assertEqual((off["conn"], off["connState"]), ("Offline", "offline"))
        self.assertTrue(off["banners"]["offline"])
        self.assertRegex(off["banners"]["offlineText"], r"^Cannot reach the dashboard server\. Showing data from \d\d:\d\d \(\d+m? ?\d*s? ?ago\)\. Is `orkestra dashboard` still running in your terminal\?$")
        back = res["back"]
        self.assertEqual((back["connState"], back["live"].strip()), ("live", "Connection restored."))
        self.assertFalse(back["banners"]["offline"])

    def test_offline_cards_are_marked_with_the_time_of_the_last_good_data(self):
        steps = [{"op": "set", "key": "fail", "value": True}, {"op": "advance", "ms": 20000},
                 ev("const c=H.card('k1');return [c.getAttribute('data-stale'),H.find(c,n=>n.className==='asof')[0].textContent,H.find(c,n=>n.className==='asof')[0].hidden]", "r"),
                 {"op": "set", "key": "fail", "value": False}, {"op": "advance", "ms": 20000},
                 ev("const c=H.card('k1');return [c.getAttribute('data-stale'),H.find(c,n=>n.className==='asof')[0].hidden]", "back")]
        out = self.run_page([view(1, "you", "question", 100)], steps)
        stale, asof, hidden = out["results"]["r"]
        self.assertEqual((stale, hidden), ("true", False))
        self.assertRegex(asof, r"^\(as of \d\d:\d\d\)$")
        self.assertEqual(out["results"]["back"], [None, True])

    def test_a_server_error_is_a_failure_not_data(self):
        out = self.run_page([view(1)], [{"op": "set", "key": "status", "value": 503}, {"op": "advance", "ms": 2100}, {"op": "snap", "as": "s"}])
        self.assertIn("Connection lost", out["results"]["s"]["conn"])

    def test_a_render_error_is_not_reported_as_no_connection(self):
        bad = view(2, "running", "working", activity="not a list")
        steps = [{"op": "set", "key": "views", "value": [view(1), bad]}, {"op": "advance", "ms": 2100}, {"op": "snap", "as": "bad"},
                 {"op": "set", "key": "views", "value": [view(1)]}, {"op": "advance", "ms": 2100}, {"op": "snap", "as": "fixed"}]
        out = run_dashboard(self.tmp, self.page(), [view(1)], steps)
        bad_snap, fixed = out["results"]["bad"], out["results"]["fixed"]
        self.assertTrue(bad_snap["banners"]["rerr"])
        self.assertEqual(bad_snap["banners"]["rerrText"], "The page could not display the latest data. Reload the page; if it persists, restart `orkestra dashboard`.")
        self.assertEqual(bad_snap["connState"], "live")                                # the network was fine
        self.assertFalse(bad_snap["banners"]["offline"])
        self.assertTrue(out["consoleErrors"])
        self.assertEqual([i["key"] for s in bad_snap["sections"] for i in s["items"]], ["k1"], "the last good page stays")
        self.assertFalse(fixed["banners"]["rerr"])

    def test_the_stale_banner_wins_over_the_other_two_and_offers_the_restart_command(self):
        steps = [ev("const b=H.byId('stale-copy');const before=b.textContent;b.fire('click');await H.settle();return [before,b.textContent,H.clipboard]", "r"),
                 {"op": "advance", "ms": 1700}, ev("return H.byId('stale-copy').textContent", "later"), {"op": "snap", "as": "s"}]
        out = self.run_page([view(1)], steps, build="abc", page_build="abc", headers={"X-Orkestra-Stale": "1"})
        self.assertEqual(out["results"]["r"], ["Copy the restart command", "Copied", ["orkestra dashboard --open"]])
        self.assertEqual(out["results"]["later"], "Copy the restart command")              # the label comes back
        self.assertTrue(out["results"]["s"]["banners"]["stale"])

    def test_a_new_build_reloads_the_page_exactly_once(self):
        out = self.run_page([view(1)], [{"op": "advance", "ms": 20000}], build="new", page_build="old")
        self.assertEqual(out["reloads"], 1)
        self.assertEqual(len(out["calls"]), 1, "an old script does not keep polling while the reload happens")

    def test_the_footer_and_chip_say_which_build_this_is(self):
        with lang("en"):
            page = MON.page_html("abc123def456", started=NOW - 2 * 86400 - 3 * 3600)
        snap, _ = (lambda o: (o["results"]["s"], o))(run_dashboard(self.tmp, page, [view(1)], [{"op": "snap", "as": "s"}], build="abc123def456"))
        self.assertRegex(snap["foot"], r"^Orkestra \d+\.\d+\.\d+ \u00b7 build abc123def456 \u00b7 server started 2d 3h ago$")

    def test_language_boot_follows_the_last_choice_once(self):
        self.assertEqual(run_dashboard(self.tmp, self.page("en"), [], [{"op": "eval", "code": "return H.replaced", "as": "r"}],
                                       options={"local": {"orkestra.dash.lang": "tr"}})["results"]["r"], ["?lang=tr"])
        done = run_dashboard(self.tmp, self.page("en"), [], [{"op": "eval", "code": "return H.replaced", "as": "r"}],
                             options={"local": {"orkestra.dash.lang": "tr"}, "search": "?lang=en"})
        self.assertEqual(done["results"]["r"], [])                                     # an explicit ?lang is never overridden
        same = run_dashboard(self.tmp, self.page("tr"), [], [{"op": "eval", "code": "return H.replaced", "as": "r"}],
                             options={"local": {"orkestra.dash.lang": "tr"}})
        self.assertEqual(same["results"]["r"], [])
        remembered = run_dashboard(self.tmp, self.page("en"), [], [{"op": "eval", "code": "return localStorage._m['orkestra.dash.lang']", "as": "r"}],
                                   options={"search": "?lang=en"})
        self.assertEqual(remembered["results"]["r"], "en")
        blocked = run_dashboard(self.tmp, self.page("en"), [view(1)], [{"op": "snap", "as": "s"}], options={"storage": "throw"})
        self.assertEqual(len(blocked["results"]["s"]["sections"]), 1)


# ================================================================== keyboard, help, print

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestKeyboard(PageCase):
    def fleet(self):
        return [view(1, "you", "question", 900), view(2, "stalled", "hung", 700), view(3, "running", "working"), view(4, "you", "turn_ended", 500)]

    def key(self, k, **kw):
        return {"op": "fire", "target": "doc", "type": "keydown", "props": dict({"key": k}, **kw)}

    def active(self):
        return ev("const a=document.activeElement;return a&&a.dataset?a.dataset.key||null:null", "k")

    def test_j_and_k_move_between_runs_without_trapping_focus(self):
        out = self.run_page(self.fleet(), [self.key("j"), ev("return document.activeElement.dataset.key", "a"), self.key("j"),
                                           ev("return document.activeElement.dataset.key", "b"), self.key("k"),
                                           ev("return document.activeElement.dataset.key", "c")])
        res = out["results"]
        self.assertEqual([res["a"], res["b"], res["c"]], ["k1", "k4", "k1"])

    def test_n_jumps_to_runs_that_need_you_and_names_the_run(self):
        steps = [self.key("n"), ev("return [document.activeElement.dataset.key,H.byId('live').textContent.trim()]", "a"),
                 self.key("n"), ev("return document.activeElement.dataset.key", "b"), self.key("n"), ev("return document.activeElement.dataset.key", "c"),
                 self.key("n"), ev("return document.activeElement.dataset.key", "d")]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual([res["a"][0], res["b"], res["c"], res["d"]], ["k1", "k4", "k2", "k1"])      # you, you, stalled, wraps
        self.assertEqual(res["a"][1], "proj1 \u00b7 Question")                                       # the run is named for the screen reader

    def test_slash_focuses_search_and_escape_clears_it(self):
        steps = [self.key("/"), ev("return document.activeElement.id", "a"),
                 ev("const q=H.byId('q');q.value='zzz';q.fire('input');return H.find(H.byId('main'),n=>n.tag==='article').length", "b"),
                 {"op": "fire", "target": "doc", "type": "keydown", "props": {"key": "Escape"}},
                 ev("return [H.byId('q').value,H.find(H.byId('main'),n=>n.tag==='article').length]", "c")]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual((res["a"], res["b"], res["c"]), ("q", 0, ["", 4]))

    def test_shortcuts_are_ignored_while_typing(self):
        steps = [ev("H.byId('q').focus();return 0", "x"), {"op": "fire", "target": "doc", "type": "keydown", "props": {"key": "j", "target": {"tagName": "INPUT"}}},
                 ev("return document.activeElement.id", "a")]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual(res["a"], "q")

    def test_the_help_line_toggles_inline_without_a_modal(self):
        out = self.run_page(self.fleet(), [self.key("?"), {"op": "snap", "as": "on"}, self.key("?"), {"op": "snap", "as": "off"}])
        self.assertTrue(out["results"]["on"]["help"])
        self.assertFalse(out["results"]["off"]["help"])
        out = self.run_page(self.fleet(), [self.key("?"), ev("return H.byId('help').textContent", "t")])
        self.assertEqual(out["results"]["t"], "Keyboard: j and k move between runs \u00b7 Enter opens or closes \u00b7 n jumps to the next run that needs you \u00b7 / searches \u00b7 ? shows this help")

    def test_enter_toggles_the_activity_of_the_focused_card(self):
        steps = [ev("const c=H.card('k1');c.focus();window.__d=H.find(c,n=>n.tag==='details'&&n.className==='act')[0];return window.__d.open", "a"),
                 # a browser delivers the key to the focused element: aim it at the card
                 ev("document.fire('keydown',{key:'Enter',target:document.activeElement});await H.settle();return 0", "x"),
                 ev("return window.__d.open", "b")]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual((res["a"], res["b"]), (True, False))

    def test_printing_opens_every_disclosure_and_prints_the_build(self):
        steps = [{"op": "advance", "ms": 10}, ev("H.winFire('beforeprint');await H.settle();"
                                              "return [H.find(H.byId('main'),n=>n.tag==='details').every(d=>d.open),H.byId('printline').textContent]", "r"),
                 ev("H.winFire('afterprint');await H.settle();return H.find(H.byId('main'),n=>n.tag==='details'&&n.className==='dtl').every(d=>!d.open)", "after")]
        res = self.run_page(self.fleet(), steps, build="abc123def456", page_build="abc123def456")["results"]
        self.assertTrue(res["r"][0])
        self.assertRegex(res["r"][1], r"^Orkestra dashboard, printed \d{4}-\d\d-\d\d \d\d:\d\d, build abc123def456$")
        self.assertTrue(res["after"])

    def test_many_runs_build_closed_disclosures_only_when_they_are_opened(self):
        views = [view(i, "running", "working", activity=[feed_row(1)]) for i in range(40)]
        steps = [ev("const m=H.byId('main');return [H.find(m,n=>n.tag==='li'&&n.className==='fr').length,H.find(m,n=>n.tag==='details'&&n.className==='act'&&n.open).length]", "a"),
                 ev("const d=H.find(H.card('k0'),n=>n.tag==='details'&&n.className==='act')[0];d.open=true;await H.settle();return H.find(H.card('k0'),n=>n.tag==='li'&&n.className==='fr').length", "b")]
        res = self.run_page(views, steps)["results"]
        self.assertEqual(res["a"], [0, 0])
        self.assertEqual(res["b"], 1)


# ================================================================== shortening in the page (same vectors as the server)

@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestPageShortening(PageCase):
    def test_the_page_and_the_server_shorten_alike(self):
        ctx = {"project": "/Users/me/Projects/app", "home": "/Users/me"}
        cases = [("Bash", "cd /Users/me/Projects/app && orkestra phase next 2>&1 | tee /tmp/out.log | tail -40"),
                 ("Bash", "cd /Users/me/Projects/app/sub; npm test | head -20"), ("Bash", "cd /Users/me/Projects/other && ls -la 2>&1 | tail -3"),
                 ("Read", "/Users/me/.cursor/projects/x/terminals/1.txt"), ("Read", "/Users/me/Projects/app/src/a.ts"),
                 ("Grep", "mushaf /Users/me/Projects/app/.orkestra/memory"), ("Edit", "/etc/hosts"), ("Bash", "echo   hi"),
                 ("Bash", "cd /Users/me/Projects/other && docker compose -f deploy/compose.measure.yml run --rm measure npm run "
                          "overpass:bench -- --region=istanbul --out geom 2>&1 | tee /tmp/b.log | tail -60"),
                 ("Bash", "x" * 300), ("Bash", "cd /Users/me/Projects/app && ls"), ("Write", "/Users/me/Projects/app")]
        out = self.run_page([], [ev("const c=" + json.dumps(ctx) + ";return " + json.dumps(cases) + ".map(x=>OrkDash.shortWhat(x[0],x[1],c))", "r")])
        for (tool, what), got in zip(cases, out["results"]["r"]):
            self.assertEqual(got["text"], MON.short_what(tool, what, ctx["project"], ctx["home"]), (tool, what))
            self.assertEqual(got["full"], what)
            self.assertEqual(got["changed"], got["text"] != what)

    def test_group_feed_and_kinds(self):
        rows = [feed_row(1, "tool", "Read", "a"), feed_row(2, "tool", "mcp__x__y", "q"), feed_row(3, "tool", "Bash", "ls"),
                feed_row(4, "tool", "Bash", "ls"), feed_row(5, "deny", "Bash", "[x] y")]
        out = self.run_page([], [ev("const g=OrkDash.groupFeed(" + json.dumps(rows) + ");return [g.map(x=>[x.type,x.count||x.n]),"
                                    "['Bash','Edit','Read','Grep','Agent','AskUserQuestion','Other'].map(t=>OrkDash.kindOf({kind:'tool',tool:t})),"
                                    "['deny','idle','wait_agents','agent_done','result','handback','user','answer','bind'].map(k=>OrkDash.kindOf({kind:k,tool:''}))]", "r")])
        grouped, tools, kinds = out["results"]["r"]
        self.assertEqual(grouped, [["row", 1], ["row", 2], ["reads", 2]])                  # newest first: deny, ls x2, 2 reads
        self.assertEqual(tools, ["run", "edit", "read", "search", "agent", "question", "other"])
        self.assertEqual(kinds, ["guard", "turn_ended", "waiting", "agent_done", "result", "report", "user", "answer", "other"])


# ================================================================== a dashboard that is already there

class _Fake:
    """A listener on a free port that answers like a dashboard of some generation."""

    def __init__(self, server_header, build=None):
        outer = self

        class H(BaseHTTPRequestHandler):
            server_version = server_header

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                outer.hits += 1
                body = b"<html></html>"
                self.send_response(200)
                if build:
                    self.send_header("X-Orkestra-Build", build)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.hits = 0
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)


class TestPortAlreadyTaken(MonitorBase):
    def start_fake(self, server, build=None):
        fake = _Fake(server, build)
        self.addCleanup(fake.stop)
        return fake

    def run_dashboard(self, port, code="en", open_browser=False, replace=False, tty=False, claudecode="", replaced=False):
        """MON.dashboard() against a taken port with its output captured: (exit code, stdout, stderr, opened urls)."""
        class Out(io.StringIO):
            def isatty(self):
                return tty

        class In(io.StringIO):
            def isatty(self):
                return tty

        err, out, opened = Out(), Out(), []
        with lang(code), mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stdin", In()), \
                mock.patch.dict(os.environ, {"CLAUDECODE": claudecode}), mock.patch.object(MON, "_replace_listener", return_value=replaced), \
                mock.patch.dict(sys.modules, {"webbrowser": mock.Mock(open=opened.append)}):
            code_ = MON.dashboard(None, port, open_browser, replace=replace)
        return code_, out.getvalue(), err.getvalue(), opened

    def test_an_older_dashboard_is_named_with_its_pid_in_both_languages(self):
        fake = self.start_fake("orkestra-dashboard")                       # no X-Orkestra-Build: it cannot update itself
        self.assertEqual(MON.probe_dashboard(fake.port), "old")
        for code, want in (("en", f"orkestra: 127.0.0.1:{fake.port} is held by an older dashboard (pid 4242, started 59h 53m ago) that "
                                  "cannot update itself, so you would keep seeing the old page. Stop it with `kill 4242` (or Ctrl+C "
                                  "in the terminal that started it) and run `orkestra dashboard --open` again, or use "
                                  "`orkestra dashboard --replace`.\n"),
                           ("tr", f"orkestra: 127.0.0.1:{fake.port} kendini g\u00fcncelleyemeyen eski bir dashboard taraf\u0131ndan tutuluyor "
                                  "(pid 4242, 59 sa 53 dk \u00f6nce ba\u015flad\u0131); bu y\u00fczden eski sayfay\u0131 g\u00f6rmeye devam edersin. "
                                  "`kill 4242` ile (ya da onu ba\u015flatan terminalde Ctrl+C ile) durdurup `orkestra dashboard --open` "
                                  "komutunu yeniden \u00e7al\u0131\u015ft\u0131r, ya da `orkestra dashboard --replace` kullan.\n")):
            with mock.patch.object(MON, "listener_pid", return_value=4242), \
                    mock.patch.object(MON, "_process_age", side_effect=lambda pid: i18n.ago(215_600)):
                code_, out, err, opened = self.run_dashboard(fake.port, code)
            self.assertEqual((code_, out), (2, ""), code)
            self.assertEqual(err, want, code)
            self.assertNotIn("\u2014", err)                                  # the new copy keeps the workspace rule

    def test_without_a_pid_the_message_has_no_kill_hint(self):
        fake = self.start_fake("orkestra-dashboard")
        with mock.patch.object(MON, "listener_pid", return_value=None):
            code_, _, err, _ = self.run_dashboard(fake.port)
        self.assertEqual(code_, 2)
        self.assertIn("is held by an older dashboard that cannot update itself", err)
        self.assertNotIn("kill", err)
        with mock.patch.object(MON, "listener_pid", return_value=None):
            _, _, err_tr, _ = self.run_dashboard(fake.port, "tr")
        self.assertIn("kendini g\u00fcncelleyemeyen eski bir dashboard", err_tr)
        self.assertNotIn("kill", err_tr)

    def test_a_current_dashboard_is_reused_and_opened(self):
        fake = self.start_fake("orkestra-dashboard", build="abc123")
        self.assertEqual(MON.probe_dashboard(fake.port), "current")
        code_, out, err, opened = self.run_dashboard(fake.port, open_browser=True)
        self.assertEqual((code_, err), (0, ""))
        self.assertEqual(out, f"orkestra dashboard is already running \u2192 http://127.0.0.1:{fake.port}/\n")
        self.assertEqual(opened, [f"http://127.0.0.1:{fake.port}/"])
        code_, out, _, opened = self.run_dashboard(fake.port, "tr")
        self.assertEqual((code_, opened), (0, []))
        self.assertIn("zaten \u00e7al\u0131\u015f\u0131yor", out)

    def test_anything_else_keeps_the_generic_message(self):
        fake = self.start_fake("SomethingElse/1.0")
        self.assertEqual(MON.probe_dashboard(fake.port), "other")
        code_, out, err, _ = self.run_dashboard(fake.port)
        self.assertEqual((code_, out), (2, ""))
        self.assertIn(f"could not open 127.0.0.1:{fake.port}", err)
        with socket.socket() as s:                                          # nothing listens at all
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        self.assertEqual(MON.probe_dashboard(free), "other")

    def test_replace_refuses_without_a_terminal_or_under_an_agent_and_sends_no_signal(self):
        fake = self.start_fake("orkestra-dashboard")
        with mock.patch.object(MON.os, "kill") as kill, mock.patch.object(MON, "listener_pid", return_value=4242), \
                mock.patch.object(MON, "_process_age", return_value="2h 0m ago"), \
                mock.patch.object(MON, "_is_orkestra_dashboard", return_value=True):
            code_, _, err, _ = self.run_dashboard(fake.port, replace=True)               # output is not a terminal
            self.assertEqual(code_, 2)
            self.assertIn("older dashboard", err)
            code_, _, err, _ = self.run_dashboard(fake.port, replace=True, tty=True, claudecode="1")   # a terminal, but an agent's
            self.assertEqual(code_, 2)
            self.assertIn("older dashboard", err)
            code_, _, err, _ = self.run_dashboard(fake.port, replace=True, tty=True, replaced=False)   # the old one would not stop
            self.assertEqual(code_, 2)
            self.assertIn("older dashboard", err)
        kill.assert_not_called()

    def test_replace_that_frees_the_port_lets_the_dashboard_bind_again(self):
        exc = OSError("Address already in use")
        class Tty(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(MON, "probe_dashboard", return_value="old"), mock.patch.object(MON, "_replace_listener", return_value=True) as stop, \
                mock.patch.object(sys, "stdin", Tty()), mock.patch.object(sys, "stdout", Tty()), mock.patch.dict(os.environ, {"CLAUDECODE": ""}):
            if os.name == "nt":
                self.skipTest("replace is POSIX only")
            self.assertIsNone(MON._busy_outcome(47616, exc, False, True))
        stop.assert_called_once_with(47616)
        with mock.patch.object(MON, "probe_dashboard", return_value="current"), mock.patch.object(MON, "_replace_listener", return_value=False), lang("en"):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(MON._busy_outcome(47616, exc, False, False), 0)
        self.assertIn("already running", out.getvalue())

    def test_replace_stops_only_an_orkestra_dashboard_with_sigterm(self):
        import signal
        sent = []
        with mock.patch.object(MON, "listener_pid", return_value=4242), mock.patch.object(MON, "_is_orkestra_dashboard", return_value=True), \
                mock.patch.object(MON.os, "kill", side_effect=lambda pid, sig: sent.append((pid, sig))), \
                mock.patch.object(MON.time, "sleep"), mock.patch("socket.socket.connect_ex", return_value=111):
            self.assertTrue(MON._replace_listener(47616))
        self.assertEqual(sent, [(4242, signal.SIGTERM)])                    # never SIGKILL
        sent.clear()
        with mock.patch.object(MON, "listener_pid", return_value=4242), mock.patch.object(MON, "_is_orkestra_dashboard", return_value=False), \
                mock.patch.object(MON.os, "kill", side_effect=lambda pid, sig: sent.append((pid, sig))):
            self.assertFalse(MON._replace_listener(47616))                  # some other program: not touched
        self.assertEqual(sent, [])
        with mock.patch.object(MON, "_run_text", return_value="/usr/bin/python3 /x/orkestra dashboard --open"):
            self.assertTrue(MON._is_orkestra_dashboard(1))
        with mock.patch.object(MON, "_run_text", return_value="/usr/bin/python3 -m http.server"):
            self.assertFalse(MON._is_orkestra_dashboard(1))

    def test_replace_needs_a_terminal_a_posix_system_and_no_agent(self):
        class Tty:
            def isatty(self):
                return True

        with mock.patch.object(sys, "stdin", Tty()), mock.patch.object(sys, "stdout", Tty()), mock.patch.dict(os.environ, {"CLAUDECODE": ""}):
            self.assertTrue(MON._may_replace() or os.name == "nt")
            with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
                self.assertFalse(MON._may_replace())
            with mock.patch.object(MON.os, "name", "nt"):
                self.assertFalse(MON._may_replace())

    def test_the_process_age_is_read_from_ps(self):
        self.assertEqual(MON._etime_seconds("2-11:53:41"), 215_621)
        self.assertEqual(MON._etime_seconds("05:12"), 312)
        self.assertIsNone(MON._etime_seconds("garbage"))
        with mock.patch.object(MON, "_run_text", return_value="2-11:53:41"), lang("en"):
            self.assertEqual(MON._process_age(1), "59h 53m ago")


# ================================================================== numbers

class TestWeight(MonitorBase):
    def test_collecting_many_runs_stays_cheap(self):
        for i in range(60):
            self.make_run(f"proj{i:02d}", f"r{i}", rows=[self.ago_row(10, "tool", tool="Bash", what="ls")] * 5)
        views = helpers.assert_cpu_under(self, 6.0, lambda: MON.discover(include_all=True), "60 runs")
        self.assertEqual(len(views), 60)
        self.assertLess(len(json.dumps(views)), 600_000)

    def test_a_few_hundred_runs_render_in_the_page_script_quickly(self):
        if not HAVE_NODE:
            self.skipTest("node is not installed")
        views = [view(i, ["you", "stalled", "running", "paused", "done"][i % 5], "working", i % 700 or None,
                      status="done" if i % 5 == 4 else "active", activity=[feed_row(j) for j in range(14)]) for i in range(200)]
        started = time.time()
        out = run_dashboard(self.tmp, MON.page_html(), views, [{"op": "advance", "ms": 6100}, {"op": "snap", "as": "s"}])
        self.assertEqual(out["errors"], [])
        self.assertLess(time.time() - started, 30)
        self.assertEqual(len(out["calls"]), 4)


class TestFinishMeansDone(unittest.TestCase):
    def test_a_deferred_task_is_not_done_and_blocks_the_finish(self):
        from orkestra_core import runs as R
        st = {"run_id": "r", "tasks": {"T001": {"status": "done"}, "FIX-V001": {"status": "deferred", "deferred_reason": "later"}}}
        self.assertEqual(R.unfinished_task_ids(st), ["FIX-V001"])
        self.assertEqual(R.unfinished_task_ids({"tasks": {"T001": {"status": "done"}}}), [])
        for target in ("deliver", "done"):
            problems = R.preconditions(Path("/tmp/does-not-matter"), st, target)
            self.assertTrue(any("FIX-V001" in p and "deferred" in p for p in problems), problems)

    def test_a_finished_row_names_unfinished_work_and_does_not_wear_the_risk_chip(self):
        page = SOURCE.split('PAGE = r"""', 1)[1].split('"""', 1)[0]
        self.assertIn('v.status==="done"', page)
        self.assertIn("ui_gap_n", page)
        self.assertIn("ui_t_deferred", page)
        self.assertIn("rkn gap", page)
        self.assertIn(".rkn.gap{", SOURCE)


if __name__ == "__main__":
    unittest.main()
