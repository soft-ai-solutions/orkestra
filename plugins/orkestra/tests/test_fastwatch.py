"""Fast, zero-token stall watcher (fastwatch.py): deterministic predicates on synthetic timelines, the singleton
lifecycle, the heal actions and nothing else, the circuit breaker, the bounded renotification of the one-shot
watchdog, and the small engine/config switches that belong to the same change (budget cap, Copilot continue cap)."""
import ast
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
from orkestra_core import activity as A
from orkestra_core import engines as EN
from orkestra_core import paths
from orkestra_core import state as ST
from orkestra_core import watchdog as W
from orkestra_core.util import file_lock

T0 = 1_000_000.0


def row(ts, kind="tool", agent="main", tool="", what="", **kw):
    r = {"ts": ts, "kind": kind, "agent": agent, "tool": tool, "what": what}
    r.update(kw)
    return r


def bash(ts, agent="main", what="make test", **kw):
    return row(ts, "tool", agent, "Bash", what, **kw)


def wd(**kw):
    from orkestra_core import tempo as T
    out = dict(T.WD_DEFAULTS)
    out.update(kw)
    return out


def ctx(rows, now, st=None, **kw):
    from orkestra_core import fastwatch as F
    st = dict({"run_id": "001-x", "status": "active", "phase": "implement", "mode": "interactive",
               "approval_mode": "autopilot"}, **(st or {}))
    return F.RunCtx(Path("/w/p"), st, rows, now, kw.pop("wd", wd()), **kw)


class TestFlags(unittest.TestCase):
    def test_claude_budget_flag_only_when_set(self):
        base = EN.headless_command("claude", "claude", "sid", "p", False, {})["argv"]
        self.assertNotIn("--max-budget-usd", base)
        zero = EN.headless_command("claude", "claude", "sid", "p", False, {"max_budget_usd": 0})["argv"]
        self.assertNotIn("--max-budget-usd", zero)
        on = EN.headless_command("claude", "claude", "sid", "p", False, {"max_budget_usd": 2.5})["argv"]
        self.assertEqual(on[on.index("--max-budget-usd") + 1], "2.5")

    def test_copilot_interactive_passes_the_continue_cap(self):
        argv = EN.interactive_command("copilot", "copilot", "go", {"max_autopilot_continues": 7})
        self.assertEqual(argv[argv.index("--max-autopilot-continues") + 1], "7")
        self.assertNotIn("--max-autopilot-continues", EN.interactive_command("copilot", "copilot", "go", {}))

    def test_config_defaults(self):
        d = paths.DEFAULT_GLOBAL_CONFIG
        self.assertFalse(d["headless"]["max_budget_usd"])            # off unless the user opts in
        self.assertEqual(d["bridge"]["turn_timeout_seconds"], 7200)
        self.assertGreater(d["bridge"]["turn_idle_seconds"], 0)
        self.assertIs(d["watchdog"]["autostart"], True)
        self.assertEqual(d["watchdog"]["daemon_idle_minutes"], 30)


class TestPredicates(unittest.TestCase):
    def setUp(self):
        from orkestra_core import fastwatch as F
        self.F = F

    def kinds(self, c):
        return {f["kind"] for f in self.F.analyze(c)}

    def test_deny_loop_fires_at_the_third_identical_denial_only(self):
        rows = [row(T0 + i * 10, "deny", tool="Bash", what="git push", key="k1") for i in range(2)]
        self.assertIsNone(self.F.deny_loop(ctx(rows, T0 + 25)))
        rows.append(row(T0 + 20, "deny", tool="Bash", what="git push", key="k1"))
        f = self.F.deny_loop(ctx(rows, T0 + 25))
        self.assertEqual((f["kind"], f["data"]["count"], f["data"]["block"]), ("deny_loop", 3, False))
        # distinct commands do not add up, and a slow trickle is not a loop
        mixed = [row(T0 + i, "deny", tool="Bash", what=f"c{i}", key=f"k{i}") for i in range(5)]
        self.assertIsNone(self.F.deny_loop(ctx(mixed, T0 + 10)))
        slow = [row(T0 + i * 100, "deny", tool="Bash", what="x", key="k1") for i in range(3)]
        self.assertIsNone(self.F.deny_loop(ctx(slow, T0 + 250)))
        many = [row(T0 + i * 60, "deny", tool="Bash", what="x", key="k1") for i in range(8)]
        self.assertTrue(self.F.deny_loop(ctx(many, T0 + 8 * 60))["data"]["block"])

    def test_control_plane_flips_fire_at_three_in_ten_minutes(self):
        rows = [row(T0 + i * 100, "status", what="paused -> active") for i in range(2)]
        self.assertIsNone(self.F.control_flips(ctx(rows, T0 + 250)))
        rows.append(row(T0 + 200, "status", what="active -> paused"))
        self.assertEqual(self.F.control_flips(ctx(rows, T0 + 250))["data"]["flips"], 3)
        spread = [row(T0 + i * 400, "status", what="x") for i in range(3)]
        self.assertIsNone(self.F.control_flips(ctx(spread, T0 + 900)))

    def test_paused_run_with_fresh_tool_rows_after_the_pause(self):
        st = {"status": "paused"}
        rows = [row(T0 + 40, "tool", tool="Edit", what="a.py")]
        c = ctx(rows, T0 + 50, st=st, state_mtime=T0 + 10)
        self.assertEqual(self.F.paused_working(c)["kind"], "paused_working")
        self.assertIsNone(self.F.paused_working(ctx(rows, T0 + 105, st=st, state_mtime=T0 + 10)))   # row > 60 s old
        self.assertIsNone(self.F.paused_working(ctx(rows, T0 + 50, st=st, state_mtime=T0 + 38)))    # still in flight
        self.assertIsNone(self.F.paused_working(ctx(rows, T0 + 50, st={"status": "active"}, state_mtime=T0 + 10)))

    def test_report_overdue_threshold_and_cancellations(self):
        due = {"agent-1": {"at": T0, "type": "implementer", "name": ""}}
        self.assertIsNone(self.F.report_overdue(ctx([], T0 + 59.9, reports_due=due)))
        f = self.F.report_overdue(ctx([], T0 + 60, reports_due=due))
        self.assertEqual((f["kind"], f["since"]), ("report_overdue", T0))
        self.assertIsNone(self.F.report_overdue(ctx([row(T0 + 20, "handback")], T0 + 90, reports_due=due)))
        self.assertIsNone(self.F.report_overdue(ctx([], T0 + 90, reports_due=due, handback_at=T0 + 30)))
        self.assertIsNone(self.F.report_overdue(ctx([], T0 + 90, reports_due=due, pending=[{"id": "a2", "at": T0}])))
        self.assertIsNone(self.F.report_overdue(ctx([row(T0 + 10, "tool", tool="Edit", what="x")], T0 + 90,
                                                    reports_due=due)))        # the main session is moving

    def test_idle_active_after_twenty_seconds_when_work_is_actionable(self):
        rows = [row(T0, "idle", what="turn ended")]
        act = lambda: {"action": "work"}   # noqa: E731
        self.assertIsNone(self.F.idle_active(ctx(rows, T0 + 19.9, next_action=act, state_mtime=T0 - 5)))
        f = self.F.idle_active(ctx(rows, T0 + 20, next_action=act, state_mtime=T0 - 5))
        self.assertEqual(f["kind"], "idle_active")
        for stop in ({"action": "ask_user"}, {"action": "wait_user"}, {"action": "wait_agents"}, {"action": "none"}):
            self.assertIsNone(self.F.idle_active(ctx(rows, T0 + 60, next_action=lambda s=stop: s, state_mtime=T0 - 5)))
        self.assertIsNone(self.F.idle_active(ctx(rows, T0 + 60, next_action=act, state_mtime=T0 + 5)))   # state moved on
        self.assertIsNone(self.F.idle_active(ctx(rows, T0 + 60, next_action=act, state_mtime=T0 - 5,
                                                 pending=[{"id": "a", "at": T0}])))
        for st in ({"status": "awaiting_user"}, {"status": "paused"}, {"approval_mode": "manual"}):
            self.assertIsNone(self.F.idle_active(ctx(rows, T0 + 60, st=st, next_action=act, state_mtime=T0 - 5)))

    def test_permission_wait_names_the_tool_after_thirty_seconds(self):
        rows = [row(T0, "perm_wait", what="Claude needs your permission to use Bash")]
        self.assertIsNone(self.F.permission_wait(ctx(rows, T0 + 29.9)))
        f = self.F.permission_wait(ctx(rows, T0 + 30))
        self.assertIn("Bash", f["data"]["what"])
        answered = rows + [row(T0 + 10, "tool", tool="Bash", what="ls")]
        self.assertIsNone(self.F.permission_wait(ctx(answered, T0 + 60)))
        aside = rows + [row(T0 + 10, "agent_done")]                       # an aside is not an answer
        self.assertIsNotNone(self.F.permission_wait(ctx(aside, T0 + 60)))

    def test_hung_command_needs_silence_of_every_agent_and_no_live_process(self):
        rows = [bash(T0)]
        self.assertIsNone(self.F.hung_command(ctx(rows, T0 + 900)))                   # limit is "more than 900 s"
        f = self.F.hung_command(ctx(rows, T0 + 901))
        self.assertEqual((f["kind"], f["data"]["verified"]), ("hung_command", False))
        self.assertIsNone(self.F.hung_command(ctx(rows, T0 + 901, proc_alive=True)))  # CPU / child says: working
        self.assertTrue(self.F.hung_command(ctx(rows, T0 + 901, proc_alive=False))["data"]["verified"])
        other = rows + [row(T0 + 400, "tool", agent="implementer", tool="Edit", what="x")]
        self.assertIsNone(self.F.hung_command(ctx(other, T0 + 901)))                  # another agent moved: not hung
        gate = [bash(T0, what="orkestra gate verify")]
        self.assertIsNone(self.F.hung_command(ctx(gate, T0 + 1700)))                  # gates get 1800 s
        self.assertIsNotNone(self.F.hung_command(ctx(gate, T0 + 1801)))
        wait = [bash(T0, what="sleep 600")]
        self.assertIsNone(self.F.hung_command(ctx(wait, T0 + 2000)))                  # main's own wait is not a hang

    def test_replay_of_the_false_hung_case_never_fires(self):
        """A subagent's Bash row without an end row (hooks never fire for interrupted calls) while the run keeps
        working: the old classifier called 2.9% of such instants hung / dead wait. No deterministic predicate may."""
        rows = [bash(T0, agent="implementer", aid="a1", what="npm run build")]
        pending = []
        for i in range(1, 160):
            rows.append(row(T0 + i * 20, "tool", tool="Edit", what=f"f{i}.py"))
            for dt in (0.4, 19.0):
                now = T0 + i * 20 + dt
                got = self.kinds(ctx(rows, now, pending=pending, state_mtime=T0 + i * 20 - 1,
                                     next_action=lambda: {"action": "work"}))
                self.assertFalse(got & {"hung_command", "report_overdue", "idle_active", "permission_wait",
                                        "dead_session"}, (i, dt, got))

    def test_stop_failure_and_the_retry_ladder(self):
        rows = [row(T0, "api_error", what="rate_limit")]
        f = self.F.stop_failure(ctx(rows, T0 + 1))
        self.assertEqual((f["kind"], f["since"]), ("stop_failure", T0))
        self.assertIsNone(self.F.stop_failure(ctx(rows + [row(T0 + 5, "tool", tool="Edit", what="x")], T0 + 20)))
        due = self.F.resume_due
        self.assertFalse(due(0, 0, T0, T0 + 9.9, first_delay=10))
        self.assertTrue(due(0, 0, T0, T0 + 10, first_delay=10))
        self.assertFalse(due(1, T0 + 10, T0, T0 + 69, first_delay=10))
        self.assertTrue(due(1, T0 + 10, T0, T0 + 70, first_delay=10))
        self.assertFalse(due(2, T0 + 70, T0, T0 + 369, first_delay=10))
        self.assertTrue(due(2, T0 + 70, T0, T0 + 370, first_delay=10))
        self.assertFalse(due(3, T0 + 370, T0, T0 + 99999, first_delay=10, cap=3))      # the existing cap holds

    def test_hooks_degraded_needs_three_errors_inside_a_minute(self):
        ev = [(T0, 1), (T0 + 20, 1)]
        self.assertIsNone(self.F.hooks_degraded(ev, T0 + 30))
        ev.append((T0 + 59, 1))
        self.assertEqual(self.F.hooks_degraded(ev, T0 + 60)["data"]["errors"], 3)
        self.assertIsNone(self.F.hooks_degraded([(T0, 1), (T0 + 40, 1), (T0 + 70, 1)], T0 + 71))

    def test_soft_silence_is_a_flag_not_an_incident(self):
        rows = [row(T0, "tool", tool="Edit", what="x")]
        self.assertIsNone(self.F.soft_silence(ctx(rows, T0 + 179)))
        f = self.F.soft_silence(ctx(rows, T0 + 180))
        self.assertEqual((f["kind"], f["level"]), ("soft_silence", "flag"))
        self.assertIsNone(self.F.soft_silence(ctx(rows + [bash(T0 + 1)], T0 + 400)))       # a command is running
        self.assertIsNone(self.F.soft_silence(ctx(rows, T0 + 400, st={"status": "awaiting_user"})))

    def test_dead_session_needs_a_dead_host_pid_to_notify_and_only_flags_a_long_silence(self):
        sess = [{"id": "s1", "age": 500.0, "host_pid": 4194000}]
        f = self.F.dead_session(ctx([], T0, sessions=sess, pid_alive=lambda p: False))
        self.assertEqual((f["kind"], f["level"]), ("dead_session", "notify"))
        f = self.F.dead_session(ctx([], T0, sessions=sess, pid_alive=lambda p: True))          # alive host, silent: a flag
        self.assertEqual(f["level"], "flag")
        self.assertIsNone(self.F.dead_session(ctx([], T0, sessions=[{"id": "s1", "age": 30.0, "host_pid": None}])))
        self.assertIsNone(self.F.dead_session(ctx([], T0, sessions=[{"id": "s1", "age": 30.0, "host_pid": 9}],
                                                  pid_alive=lambda p: False)))
        flag = self.F.dead_session(ctx([], T0, sessions=[{"id": "s1", "age": 500.0, "host_pid": None}]))
        self.assertEqual(flag["level"], "flag")                                                # no pid recorded: never a notice
        self.assertIsNone(self.F.dead_session(ctx([], T0, sessions=[])))

    def test_registry_staleness_finds_commands_a_later_row_outlived(self):
        rows = [bash(T0, agent="implementer", aid="a1"), row(T0 + 10, "tool", agent="implementer", aid="a1",
                                                              tool="Edit", what="x"),
                bash(T0 + 20, agent="reviewer", aid="r1")]
        stale = self.F.stale_commands(rows)
        self.assertEqual([(s["agent"], s["aid"]) for s in stale], [("implementer", "a1")])   # reviewer's is open, not stale
        self.assertEqual(self.F.stale_commands(rows + [row(T0 + 11, "end", agent="implementer", aid="a1", tool="Bash")]), [])


class FW(unittest.TestCase):
    """A throwaway ORKESTRA_HOME + workspace with hand-made runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-fw-")).resolve()
        self.home, self.ws = self.tmp / "home", self.tmp / "Projects"
        self.home.mkdir()
        self.ws.mkdir()
        self._env = {k: os.environ.get(k) for k in ("ORKESTRA_HOME", "ORKESTRA_WORKSPACE", "ORKESTRA_NO_DAEMON")}
        os.environ["ORKESTRA_HOME"], os.environ["ORKESTRA_WORKSPACE"] = str(self.home), str(self.ws)
        os.environ["ORKESTRA_NO_DAEMON"] = "1"
        (self.home / "config.json").write_text(json.dumps({"watchdog": {"macos_notify": False}}))
        self.sent = []
        self.proj = self.ws / "demo"
        self.proj.mkdir()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_run(self, rid="001-x", status="active", mode="interactive", rows=(), approval="autopilot", state_ts=None):
        d = self.proj / ".orkestra" / "runs" / rid
        d.mkdir(parents=True, exist_ok=True)
        st = {"run_id": rid, "status": status, "phase": "implement", "mode": mode, "approval_mode": approval,
              "title": "t", "tasks": {}}
        (d / "state.json").write_text(json.dumps(st))
        if state_ts is not None:
            os.utime(d / "state.json", (state_ts, state_ts))
        with open(d / "activity.jsonl", "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        self.rdir = d
        return d

    def bind(self, sid="sess-1", rid="001-x", age=0.0, proj=None, now=None):
        sd = self.home / "sessions"
        sd.mkdir(exist_ok=True)
        f = sd / f"{sid}.json"
        f.write_text(json.dumps({"project": str(proj or self.proj), "run_id": rid, "bound_at": "2026-01-01T00:00:00"}))
        t = (now if now is not None else time.time()) - age
        os.utime(f, (t, t))
        return f

    def watcher(self, **kw):
        from orkestra_core import fastwatch as F
        kw.setdefault("notifier", lambda title, text, payload: self.sent.append((title, text, payload)))
        kw.setdefault("alive", lambda ids, pid=None: False)
        kw.setdefault("bridge_owns", lambda ids: False)
        kw.setdefault("next_action", lambda proj, st, now: {"action": "work"})
        return F.Watcher(**kw)

    def snapshot(self, root):
        out = {}
        for p in sorted(Path(root).rglob("*")):
            if p.is_file() and "watchdog" not in p.name and p.suffix != ".lock" and "logs" not in p.parts:
                out[str(p)] = p.read_bytes()
        return out


class TestWatcher(FW):
    def test_cadence_idle_normal_and_hot(self):
        w = self.watcher()
        self.assertEqual(w.tick(T0)["interval"], 15)                      # no open run
        self.make_run(rows=[row(T0 - 5, "tool", tool="Edit", what="x")])
        w = self.watcher()
        self.assertEqual(w.tick(T0)["interval"], 5)
        self.make_run(rows=[row(T0 - 55, "agent_done", agent="implementer")])
        due = {"agent-1": {"at": T0 - 55, "type": "implementer", "name": ""}}
        (self.rdir / "agents.json").write_text(json.dumps({"pending": {}, "seen": {}, "reports_due": due}))
        w = self.watcher()
        self.assertEqual(w.tick(T0)["interval"], 1)                       # a candidate is about to fire (T0 + 5)

    def test_report_overdue_notifies_once_at_the_threshold_then_backs_off_and_caps(self):
        self.make_run(rows=[row(T0 - 100, "agent_done", agent="implementer")])
        (self.rdir / "agents.json").write_text(json.dumps(
            {"pending": {}, "seen": {}, "reports_due": {"a1": {"at": T0, "type": "implementer", "name": ""}}}))
        w = self.watcher()
        w.tick(T0 + 59)
        self.assertEqual(self.sent, [])
        w.tick(T0 + 61)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("demo", self.sent[0][1])
        self.assertEqual(self.sent[0][2]["key"], "report_overdue")
        w.tick(T0 + 62)
        w.tick(T0 + 500)
        self.assertEqual(len(self.sent), 1)                               # same incident: no repeat inside 30 min
        t = T0 + 61
        for gap, total in ((1800, 2), (7200, 3), (28800, 4), (86400, 5)):   # 30 min, 2 h, 8 h, a day
            w.tick(t + gap - 1)
            self.assertEqual(len(self.sent), total - 1)
            t += gap
            w.tick(t)
            self.assertEqual(len(self.sent), total)
        w.tick(t + 10 * 86400)
        self.assertEqual(len(self.sent), 5)                               # and then silence: capped per incident

    def test_incident_clears_and_can_fire_again(self):
        self.make_run(rows=[row(T0 - 100, "agent_done", agent="implementer")])
        ag = self.rdir / "agents.json"
        ag.write_text(json.dumps({"pending": {}, "seen": {}, "reports_due": {"a1": {"at": T0, "type": "x", "name": ""}}}))
        w = self.watcher()
        w.tick(T0 + 61)
        ag.write_text(json.dumps({"pending": {}, "seen": {}, "reports_due": {}}))
        w.tick(T0 + 70)
        ag.write_text(json.dumps({"pending": {}, "seen": {}, "reports_due": {"a2": {"at": T0 + 100, "type": "x", "name": ""}}}))
        w.tick(T0 + 165)
        self.assertEqual(len(self.sent), 2)

    def test_notify_only_findings_write_no_run_state(self):
        rows = [row(T0 + i, "deny", tool="Bash", what="git push", key="k1") for i in range(4)]
        rows.append(row(T0 + 10, "perm_wait", what="needs permission for Bash"))
        self.make_run(rows=rows)
        self.bind(now=T0)
        (self.rdir / "agents.json").write_text(json.dumps(
            {"pending": {}, "seen": {}, "reports_due": {"a1": {"at": T0, "type": "x", "name": ""}}}))
        before = self.snapshot(self.tmp)
        w = self.watcher()
        res = w.tick(T0 + 100)
        self.assertGreaterEqual(len(self.sent), 2)
        self.assertEqual(res["actions"], [])
        self.assertEqual(self.snapshot(self.tmp), before)

    def test_only_the_listed_heal_actions_exist(self):
        from orkestra_core import fastwatch as F
        self.assertEqual(set(F.HEAL_ACTIONS), {"unbind_stale", "close_open_command", "purge_agents", "freeze_storm"})
        w = self.watcher()
        with self.assertRaises(AssertionError):
            w.act("rewrite_state", lambda: None)

    def test_heal_drops_bindings_to_missing_and_terminal_runs(self):
        self.make_run(rid="002-done", status="done")
        self.bind(sid="to-missing", rid="404-gone")
        self.bind(sid="to-done", rid="002-done")
        self.make_run(rid="001-x", status="active")
        self.bind(sid="live", rid="001-x")
        res = self.watcher().tick(T0, force_discover=True)
        names = {f.stem for f in (self.home / "sessions").glob("*.json")}
        self.assertEqual(names, {"live"})
        self.assertEqual(sorted(a["action"] for a in res["actions"]), ["unbind_stale", "unbind_stale"])

    def test_heal_closes_a_stale_open_command_with_an_honest_synthetic_end(self):
        rows = [bash(T0, agent="implementer", aid="a1"),
                row(T0 + 30, "tool", agent="implementer", aid="a1", tool="Edit", what="x")]
        self.make_run(rows=rows)
        res = self.watcher().tick(T0 + 40)
        self.assertEqual([a["action"] for a in res["actions"]], ["close_open_command"])
        last = A.read_rows(self.proj, "001-x")[-1]
        self.assertEqual((last["kind"], last["tool"], last["aid"]), ("end", "Bash", "a1"))
        self.assertEqual(last["ts"], T0 + 30)                    # the moment the agent provably moved on, not "now"
        self.assertTrue(last.get("synthetic"))
        res = self.watcher().tick(T0 + 50)
        self.assertEqual(res["actions"], [])                      # healed once

    def test_heal_purges_agents_that_are_past_their_lifetime(self):
        self.make_run(rows=[row(T0, "tool", tool="Edit", what="x")])
        (self.rdir / "agents.json").write_text(json.dumps(
            {"pending": {"old": {"id": "old", "type": "implementer", "at": T0 - 4000, "sid": ""},
                         "fresh": {"id": "fresh", "type": "reviewer", "at": T0 - 10, "sid": ""}}, "seen": {}}))
        res = self.watcher().tick(T0)
        self.assertEqual([a["action"] for a in res["actions"]], ["purge_agents"])
        left = json.loads((self.rdir / "agents.json").read_text())["pending"]
        self.assertEqual(list(left), ["fresh"])

    def test_flip_storm_is_frozen_with_one_incident_line(self):
        rows = [row(T0 + i * 30, "status", what="paused -> active") for i in range(4)]
        self.make_run(rows=rows)
        w = self.watcher()
        res = w.tick(T0 + 200)
        self.assertEqual([a["action"] for a in res["actions"]], ["freeze_storm"])
        w.tick(T0 + 210)
        ledger = (self.proj / ".orkestra" / "features" / "001-x" / "ledger.md").read_text()
        self.assertEqual(ledger.count("flip storm"), 1)
        self.assertEqual([s[2]["key"] for s in self.sent], ["control_flips"])

    def test_circuit_breaker_stops_heals_after_three_failures_but_keeps_notifying(self):
        from orkestra_core import fastwatch as F
        self.make_run(rows=[row(T0, "perm_wait", what="needs permission for Bash")])
        w = self.watcher()
        with mock.patch.object(F, "analyze", side_effect=RuntimeError("boom")):
            for i in range(3):
                w.step(T0 + 100 + i)
        self.assertTrue(w.breaker_open)
        self.assertEqual([s[2]["key"] for s in self.sent].count("breaker"), 1)
        self.assertIn("boom", [s for s in self.sent if s[2]["key"] == "breaker"][0][1])
        n = len(self.sent)
        w.step(T0 + 200)                                          # real analysis again, but notify-only
        self.assertGreater(len(self.sent), n)
        rows = [bash(T0, agent="implementer", aid="a1"),
                row(T0 + 30, "tool", agent="implementer", aid="a1", tool="Edit", what="x")]
        self.make_run(rid="002-y", rows=rows)
        res = w.tick(T0 + 300, force_discover=True)
        self.assertEqual(res["actions"], [])
        self.assertEqual(A.read_rows(self.proj, "002-y")[-1]["kind"], "tool")        # nothing was healed

    def test_idle_exit_after_thirty_minutes_without_runs_or_sessions(self):
        w = self.watcher()
        w.tick(T0)
        self.assertFalse(w.should_exit(T0 + 1799, idle_exit=1800))
        self.assertTrue(w.should_exit(T0 + 1800, idle_exit=1800))
        self.assertFalse(w.should_exit(T0 + 99999, idle_exit=None))       # the bridge-hosted watcher never leaves
        self.make_run()
        w2 = self.watcher()
        w2.tick(T0)
        self.assertFalse(w2.should_exit(T0 + 5000, idle_exit=1800))       # an open run keeps it alive
        self.bind(sid="reader", rid="001-x", now=T0, age=60)
        shutil_runs = self.proj / ".orkestra" / "runs" / "001-x" / "state.json"
        st = json.loads(shutil_runs.read_text())
        st["status"] = "done"
        shutil_runs.write_text(json.dumps(st))
        w3 = self.watcher()
        w3.tick(T0 + 4000)                                                 # run closed, binding long idle
        self.assertTrue(w3.should_exit(T0 + 4000 + 1800, idle_exit=1800))

    def test_never_signals_a_process_it_did_not_start(self):
        self.make_run(rows=[bash(T0)])
        self.bind(now=T0)
        calls = []
        real = os.kill
        with mock.patch("os.kill", side_effect=lambda pid, sig: calls.append(sig) or real(pid, sig) if sig == 0 else
                        (_ for _ in ()).throw(AssertionError(f"signal {sig} to {pid}"))), \
                mock.patch("os.killpg", side_effect=AssertionError("killpg")):
            w = self.watcher()
            for k in range(4):
                w.tick(T0 + 1000 + k * 40)
        self.assertIn("hung_command", [s[2]["key"] for s in self.sent])        # reported, never killed

    def test_hung_command_waits_for_the_second_cpu_sample(self):
        self.make_run(rows=[bash(T0)])
        samples = iter([10.0, 10.0, 10.0])
        w = self.watcher(probe=lambda project, st: next(samples))
        w.tick(T0 + 1000)
        self.assertEqual(self.sent, [])                           # one sample proves nothing
        w.tick(T0 + 1031)
        self.assertEqual(len(self.sent), 1)                       # no CPU used between the samples
        self.sent.clear()
        import shutil
        shutil.rmtree(self.proj / ".orkestra" / "runs" / "001-x")
        self.make_run(rid="002-y", rows=[bash(T0)])
        samples2 = iter([10.0, 14.0, 20.0])
        w2 = self.watcher(probe=lambda project, st: next(samples2))
        w2.tick(T0 + 1000, force_discover=True)
        w2.tick(T0 + 1031)
        self.assertEqual(self.sent, [])                                    # CPU moved between the samples: working


class TestHooksAndCost(FW):
    def test_hook_errors_are_noticed_with_module_and_line_but_old_history_is_not_replayed(self):
        logs = self.home / "logs"
        logs.mkdir()
        log = logs / "hooks.log"
        old = "2026-01-01T00:00:00 ERROR in on_pre_tool: old\nTraceback...\n" * 5
        log.write_text(old)
        self.make_run(rows=[row(T0, "tool", tool="Edit", what="x")])
        w = self.watcher()
        w.tick(T0)
        self.assertEqual(self.sent, [])                                    # history before the first look: ignored
        tb = ("2026-10-12T13:18:30 ERROR in on_pre_tool: boom\nTraceback (most recent call last):\n"
              '  File "/p/orkestra_core/guards.py", line 137, in check\n    x\nSyntaxError: expected \'(\'\n')
        with open(log, "a") as fh:
            fh.write(tb * 3)
        w.tick(T0 + 5)
        keys = [s[2]["key"] for s in self.sent]
        self.assertEqual(keys, ["hooks_degraded"])
        self.assertIn("guards.py:137", self.sent[0][1])

    def test_a_dozen_unchanged_runs_cost_next_to_nothing(self):
        for i in range(12):
            self.make_run(rid=f"{i:03d}-r", rows=[row(T0 + k, "tool", tool="Edit", what=f"f{k}.py") for k in range(300)])
            self.bind(sid=f"s{i}", rid=f"{i:03d}-r", now=T0 + 300)
        w = self.watcher()
        w.tick(T0 + 301)

        def thousand():
            for n in range(1000):
                w.tick(T0 + 301 + 5 * n)

        helpers.assert_cpu_under(self, 1.5, thousand, "1000 unchanged ticks over 12 runs")


class TestResume(FW):
    def setUp(self):
        super().setUp()
        self.spawned = []
        (self.home / "config.json").write_text(json.dumps({"watchdog": {"macos_notify": False, "auto_resume": "headless"}}))

    def spawner(self, v, cfg, session, attempt):
        self.spawned.append((session, attempt))
        return 4242 + attempt

    def test_a_refused_spawn_counts_against_the_cap(self):
        # LT-1: a spawner that returns a refusal string used to roll the booked attempt back, so every tick retried
        calls = []

        def refuse(v, cfg, session, attempt):
            calls.append(attempt)
            return "binary not found"

        self.make_run(mode="headless", rows=[row(T0, "api_error", what="rate_limit")])
        self.bind(now=T0 + 400, age=400)
        w = self.watcher(spawner=refuse)
        for k in range(200):
            w.tick(T0 + 10 + k * 5)
        self.assertLessEqual(len(calls), 3)

    def test_stop_failure_of_a_headless_run_is_retried_at_10_60_300_seconds_within_the_cap(self):
        self.make_run(mode="headless", rows=[row(T0, "api_error", what="rate_limit")])
        self.bind(now=T0 + 400, age=400)
        w = self.watcher(spawner=self.spawner)
        w.tick(T0 + 9)
        self.assertEqual(self.spawned, [])
        w.tick(T0 + 10)
        self.assertEqual(self.spawned, [("sess-1", 1)])
        w.tick(T0 + 12)
        self.assertEqual(len(self.spawned), 1)
        w.tick(T0 + 69)
        self.assertEqual(len(self.spawned), 1)
        w.tick(T0 + 70)
        self.assertEqual(len(self.spawned), 2)
        w.tick(T0 + 369)
        self.assertEqual(len(self.spawned), 2)
        w.tick(T0 + 370)
        self.assertEqual(len(self.spawned), 3)
        w.tick(T0 + 99999)
        self.assertEqual(len(self.spawned), 3)                    # max_auto_resumes = 3
        book = json.loads((self.home / "watchdog.json").read_text())
        self.assertEqual(book["resumes"][f"{self.proj}|001-x"]["count"], 3)

    def test_interactive_runs_are_only_announced_and_live_processes_are_never_doubled(self):
        self.make_run(mode="interactive", rows=[row(T0, "api_error", what="overloaded")])
        self.bind(now=T0, age=0)
        w = self.watcher(spawner=self.spawner)
        w.tick(T0 + 30)
        self.assertEqual(self.spawned, [])
        self.assertTrue(self.sent)
        self.sent.clear()
        self.make_run(rid="002-y", mode="headless", rows=[row(T0, "api_error", what="overloaded")])
        self.bind(sid="s2", rid="002-y", age=100)
        w2 = self.watcher(spawner=self.spawner, alive=lambda ids, pid=None: True)
        w2.tick(T0 + 30)
        self.assertEqual(self.spawned, [])                        # a claude with this session exists: second driver

    def test_report_overdue_resumes_a_headless_run_at_seconds_not_minutes(self):
        self.make_run(mode="headless", rows=[row(T0 - 5, "agent_done", agent="implementer")])
        (self.rdir / "agents.json").write_text(json.dumps(
            {"pending": {}, "seen": {}, "reports_due": {"a1": {"at": T0, "type": "x", "name": ""}}}))
        self.bind(now=T0 + 100, age=100)
        w = self.watcher(spawner=self.spawner)
        w.tick(T0 + 59)
        self.assertEqual(self.spawned, [])
        w.tick(T0 + 61)
        self.assertEqual(len(self.spawned), 1)


class TestSingleton(FW):
    def test_second_holder_is_refused_and_release_hands_over(self):
        from orkestra_core import fastwatch as F
        a, b = F.Singleton(), F.Singleton()
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertTrue(F.is_running())
        a.release()
        self.assertFalse(F.is_running())
        self.assertTrue(b.acquire())
        self.assertEqual(int((self.home / "watchdog.pid").read_text().split()[0]), os.getpid())
        b.release()

    @unittest.skipIf(os.name == "nt", "flock")
    def test_stale_pid_file_after_kill_9_is_taken_over(self):
        from orkestra_core import fastwatch as F
        lib = Path(F.__file__).resolve().parent.parent
        code = ("import sys, time; sys.path.insert(0, %r)\n"
                "from orkestra_core import fastwatch as F\n"
                "s = F.Singleton(); assert s.acquire(); print('held', flush=True); time.sleep(60)\n" % str(lib))
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")
            self.assertTrue(F.is_running())
            self.assertFalse(F.Singleton().acquire())
            p.send_signal(signal.SIGKILL)
            p.wait(timeout=10)
            self.assertFalse(F.is_running())                       # the kernel released it
            s = F.Singleton()
            self.assertTrue(s.acquire())
            self.assertEqual(int((self.home / "watchdog.pid").read_text().split()[0]), os.getpid())
            s.release()
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            p.stdout.close()

    def test_ensure_running_honours_the_switches_and_never_raises(self):
        from orkestra_core import fastwatch as F
        with mock.patch.object(F.subprocess, "Popen") as popen:
            F.ensure_running()                                      # ORKESTRA_NO_DAEMON=1 (FW.setUp)
            popen.assert_not_called()
            os.environ.pop("ORKESTRA_NO_DAEMON")
            (self.home / "config.json").write_text(json.dumps({"watchdog": {"autostart": False}}))
            F.ensure_running(self.proj)
            popen.assert_not_called()
            (self.home / "config.json").write_text("{}")
            F.ensure_running(self.proj)
            popen.assert_called_once()
            argv = popen.call_args[0][0]
            self.assertIn("--loop", argv)
            self.assertTrue(popen.call_args[1].get("start_new_session"))
            F.ensure_running(self.proj)                              # spawn is rate limited while the child boots
            popen.assert_called_once()
        for p in self.home.glob("watchdog*"):
            p.unlink()
        with mock.patch.object(F.subprocess, "Popen", side_effect=OSError("no fork")):
            self.assertIsNone(F.ensure_running(self.proj))           # never raises

    def test_ensure_running_is_a_cheap_noop_while_a_watcher_runs(self):
        from orkestra_core import fastwatch as F
        os.environ.pop("ORKESTRA_NO_DAEMON")
        s = F.Singleton()
        self.assertTrue(s.acquire())
        try:
            with mock.patch.object(F.subprocess, "Popen") as popen:
                t = time.process_time()
                for _ in range(50):
                    F.ensure_running(self.proj)
                per_call = (time.process_time() - t) / 50
                popen.assert_not_called()
            self.assertLess(per_call, 0.005 * 6)                     # < 5 ms (6x allowance for a loaded CI box)
        finally:
            s.release()

    def test_windows_is_a_documented_noop(self):
        from orkestra_core import fastwatch as F
        os.environ.pop("ORKESTRA_NO_DAEMON")
        self.assertIn("Windows", F.ensure_running.__doc__)
        with mock.patch.object(F, "_IS_WINDOWS", True), mock.patch.object(F.subprocess, "Popen") as popen:
            self.assertIsNone(F.ensure_running(self.proj))
            popen.assert_not_called()
            self.assertFalse(F.Singleton().acquire())


class TestZeroTokens(unittest.TestCase):
    def test_module_imports_no_network_or_model_code(self):
        from orkestra_core import fastwatch as F
        tree = ast.parse(Path(F.__file__).read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        banned = {"urllib", "http", "socket", "ssl", "requests", "anthropic", "openai", "smtplib", "ftplib"}
        self.assertFalse(mods & banned, mods & banned)
        text = Path(F.__file__).read_text(encoding="utf-8")
        self.assertNotIn("headless_command", text)                   # a model process is only ever started by watchdog._spawn_resume


class TestBoundedWatchdog(unittest.TestCase):
    def test_renotify_schedule_decays_and_stops(self):
        base = 1800.0
        due = W.renotify_due
        self.assertTrue(due(0, 0, T0, base))                         # first time: now
        self.assertFalse(due(1, T0, T0 + 1799, base))
        self.assertTrue(due(1, T0, T0 + 1800, base))                 # 30 min
        self.assertFalse(due(2, T0, T0 + 7199, base))
        self.assertTrue(due(2, T0, T0 + 7200, base))                 # 2 h
        self.assertTrue(due(3, T0, T0 + 28800, base))                # 8 h
        self.assertFalse(due(4, T0, T0 + 86399, base))
        self.assertTrue(due(4, T0, T0 + 86400, base))                # then daily
        self.assertFalse(due(W.MAX_NOTICES, T0, T0 + 10 ** 9, base))  # and never more than MAX_NOTICES per incident

    def test_fast_watcher_owns_dead_wait_and_hung_notices_while_it_is_fresh(self):
        v2 = {"stall": {"cls": 2}}
        v4 = {"stall": {"cls": 4}}
        with mock.patch("orkestra_core.fastwatch.owns_notifications", return_value=True):
            self.assertTrue(W.fast_owns(v2))
            self.assertFalse(W.fast_owns(v4))                        # silence stays with the one-shot watchdog
        with mock.patch("orkestra_core.fastwatch.owns_notifications", return_value=False):
            self.assertFalse(W.fast_owns(v2))


class TestWatchdogLocks(FW):
    def test_probes_run_outside_the_book_lock(self):
        self.make_run(mode="headless", rows=[row(time.time() - 2000, "tool", tool="Edit", what="x")],
                      state_ts=time.time() - 2000)
        self.bind(age=2000)
        seen = []

        def probe(ids, pid=None):
            try:
                with file_lock(W._state_path(), timeout=0):
                    seen.append("free")
            except Exception:
                seen.append("held")
            return True

        with mock.patch.object(W, "_process_alive", side_effect=probe), \
                mock.patch.object(W, "_bridge_owns", return_value=False), \
                mock.patch.object(W, "_spawn_resume", return_value="x"):
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                W.main(as_json=True, now=time.time())
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"free"})


class TestCli(FW):
    def run_cli(self, *args, lang="en"):
        env = dict(os.environ, ORKESTRA_LANG=lang)
        return subprocess.run([sys.executable, str(helpers.BIN), *args], capture_output=True, text=True, env=env,
                              cwd=str(self.tmp))

    def test_status_when_nothing_runs_in_both_languages(self):
        en = self.run_cli("watchdog", "--status", lang="en")
        tr = self.run_cli("watchdog", "--status", lang="tr")
        self.assertEqual(en.returncode, 0, en.stderr)
        self.assertEqual(tr.returncode, 0, tr.stderr)
        self.assertIn("not running", en.stdout)
        self.assertIn("çalışmıyor", tr.stdout)
        js = json.loads(self.run_cli("watchdog", "--status", "--json").stdout)
        self.assertIs(js["running"], False)

    def test_daemon_flag_respects_the_no_daemon_switch(self):
        cp = self.run_cli("watchdog", "--daemon", lang="en")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("ORKESTRA_NO_DAEMON", cp.stdout)
        self.assertFalse((self.home / "watchdog.pid").exists() and (self.home / "watchdog.pid").read_text().strip())

    def test_doctor_has_a_watcher_row(self):
        cp = self.run_cli("doctor", lang="en")
        self.assertIn("fast watcher", cp.stdout.lower() + cp.stderr.lower())
        cp = self.run_cli("doctor", lang="tr")
        self.assertIn("hızlı bekçi", (cp.stdout + cp.stderr).lower())

    def test_help_lists_the_new_options_in_both_languages(self):
        en = self.run_cli("watchdog", "--help", lang="en").stdout
        tr = self.run_cli("watchdog", "--help", lang="tr").stdout
        for opt in ("--loop", "--daemon", "--status"):
            self.assertIn(opt, en)
            self.assertIn(opt, tr)
        self.assertNotEqual(en, tr)

    def test_new_messages_exist_in_both_catalogs_with_the_same_fields(self):
        import string
        from orkestra_core.messages import watchdog as M
        self.assertEqual(set(M.EN), set(M.TR))
        fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}   # noqa: E731
        for k in M.EN:
            self.assertEqual(fields(M.EN[k]), fields(M.TR[k]), k)
            self.assertNotIn("—", M.EN[k] if k.startswith("watchdog.fast") else "")
        self.assertTrue([k for k in M.EN if k.startswith("watchdog.fast_")])


class TestRunForeverRestoresSignalHandlers(helpers.Sandbox):
    def test_the_embedding_process_keeps_its_ctrl_c_behaviour(self):
        """run_forever installs SIGINT/SIGTERM handlers while it loops; leaving them behind made every later
        KeyboardInterrupt in the same process a no-op (an order-dependent failure in the suite)."""
        import signal
        import threading
        from orkestra_core import fastwatch as F
        before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        stop = threading.Event()
        stop.set()
        self.assertEqual(F.run_forever(idle_exit=0.0, stop=stop), 0)
        self.assertEqual((signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)), before)


if __name__ == "__main__":
    unittest.main()
