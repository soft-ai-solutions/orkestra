"""Token usage reader (usage.py), its place in the monitor, and the monitor's stable run order."""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
import unittest

import helpers
from test_flow import FlowBase

from orkestra_core import monitor as MON
from orkestra_core import usage as U


def rec(mid, out, ts="2026-10-04T10:00:00.000Z", side=False, sid="sess-1", kind="assistant"):
    """One transcript record the way Claude Code writes it (compact JSON, one per line)."""
    usage = {"input_tokens": 10, "output_tokens": out, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 1000}
    if kind != "assistant":                 # tool result carrying a subagent summary: must never be counted
        return json.dumps({"type": "user", "timestamp": ts, "sessionId": sid,
                           "message": {"role": "user", "content": "assistant said: done"},
                           "toolUseResult": {"usage": dict(usage, input_tokens=999999), "totalTokens": 999999}})
    return json.dumps({"type": "assistant", "timestamp": ts, "isSidechain": side, "requestId": "req-" + mid,
                       "sessionId": sid,
                       "message": {"id": mid, "role": "assistant", "usage": usage,
                                   "content": [{"type": "text", "text": "SECRET-WORDS"}]}})


class _ClaudeDir:
    """Mixin: an isolated fake ~/.claude so the tests never read the real transcripts."""

    def claude_setup(self):
        self.pdir = self.tmp / "claude" / "projects" / "-ws-demo"
        self.pdir.mkdir(parents=True)
        self._old_claude = os.environ.get("ORKESTRA_CLAUDE_DIR")
        os.environ["ORKESTRA_CLAUDE_DIR"] = str(self.tmp / "claude")
        U._FILES.clear(), U._LOCATED.clear(), U._AGENT_SID.clear()

    def claude_teardown(self):
        if self._old_claude is None:
            os.environ.pop("ORKESTRA_CLAUDE_DIR", None)
        else:
            os.environ["ORKESTRA_CLAUDE_DIR"] = self._old_claude


class UsageReader(_ClaudeDir, helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.claude_setup()

    def tearDown(self):
        self.claude_teardown()
        super().tearDown()

    def test_sums_once_per_reply_with_subagents_and_incremental_reads(self):
        main = self.pdir / "sess-1.jsonl"
        main.write_text("\n".join([
            rec("m1", 5), rec("m1", 50),                      # same reply written twice (content blocks) → once, final
            rec("x", 0, kind="tool_result"),                  # subagent summary inside a tool result → ignored
            rec("m2", 20),
        ]) + "\n" + rec("m3", 9)[:40])                        # a record still being written (no newline yet)
        sub = self.pdir / "sess-1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text(rec("m9", 7, side=True) + "\n")
        (self.pdir / "agent-old.jsonl").write_text(rec("m8", 3, side=True) + "\n")               # older layout, ours
        (self.pdir / "agent-other.jsonl").write_text(rec("m7", 1000, sid="someone-else") + "\n")  # not ours
        u = U.for_sessions(["sess-1"])
        self.assertEqual((u["output"], u["input"], u["cache_write"], u["cache_read"]), (80, 40, 400, 4000))
        self.assertEqual((u["messages"], u["files"], u["context"]), (4, 3, 1110))
        self.assertEqual(u["total"], 4520)
        self.assertNotIn("SECRET-WORDS", json.dumps(u))
        with open(main, "a") as f:                            # the pending record completes, one more arrives
            f.write(rec("m3", 9)[40:] + "\n" + rec("m4", 1) + "\n")
        u = U.for_sessions(["sess-1"])
        self.assertEqual((u["output"], u["messages"]), (90, 6))

    def test_a_reply_is_counted_in_the_phase_it_was_written(self):
        start = _dt.datetime(2026, 10, 4, 10, 0, tzinfo=_dt.timezone.utc).timestamp()
        (self.pdir / "sess-1.jsonl").write_text(
            rec("d1", 10, ts="2026-10-04T10:01:00.000Z") + "\n"
            + rec("p1", 20, ts="2026-10-04T10:05:00.000Z") + "\n", encoding="utf-8")
        windows = [("discover", start, start + 180), ("plan", start + 180, start + 600)]
        u = U.for_sessions(["sess-1"], windows=windows)
        self.assertEqual(u["phases"]["discover"], 10 + 100 + 1000 + 10)
        self.assertEqual(u["phases"]["plan"], 10 + 100 + 1000 + 20)
        self.assertNotIn("implement", u["phases"])

    def test_since_counts_only_the_run(self):
        (self.pdir / "sess-1.jsonl").write_text(rec("early", 100, ts="2026-10-04T09:00:00.000Z") + "\n"
                                                + rec("late", 7, ts="2026-10-04T11:00:00.000Z") + "\n")
        since = _dt.datetime(2026, 10, 4, 10, 0, tzinfo=_dt.timezone.utc).timestamp()
        self.assertEqual(U.for_sessions(["sess-1"], since=since)["output"], 7)
        self.assertEqual(U.for_sessions(["sess-1"])["output"], 107)

    def test_resumed_session_is_not_double_counted(self):
        (self.pdir / "sess-1.jsonl").write_text(rec("m1", 50) + "\n")
        (self.pdir / "sess-2.jsonl").write_text(rec("m1", 50, sid="sess-2") + "\n" + rec("m2", 5, sid="sess-2") + "\n")
        self.assertEqual(U.for_sessions(["sess-1", "sess-2"])["output"], 55)

    def test_rewritten_file_and_bad_input_degrade_safely(self):
        p = self.pdir / "sess-1.jsonl"
        p.write_text(rec("m1", 50) + "\n" + rec("m2", 50) + "\n")
        self.assertEqual(U.for_sessions(["sess-1"])["output"], 100)
        p.write_text("not json\n" + rec("m5", 4) + "\n")       # shorter file → state is rebuilt
        self.assertEqual(U.for_sessions(["sess-1"])["output"], 4)
        self.assertIsNone(U.for_sessions(["no-such-session"]))
        self.assertIsNone(U.for_sessions(["../../etc/passwd", "*"]))
        self.assertIsNone(U.for_sessions([]))


class MonitorUsage(_ClaudeDir, FlowBase):
    def setUp(self):
        super().setUp()
        self.claude_setup()

    def tearDown(self):
        self.claude_teardown()
        super().tearDown()

    def test_usage_in_json_text_and_cli(self):
        self.start()
        self.assertIsNone(MON.discover()[0]["usage"])           # no transcript yet → no data, nothing breaks
        self.assertNotIn(" token ", MON.render_text(MON.discover(), width=140))
        U._LOCATED.clear()
        now = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        (self.pdir / f"{self.sid}.jsonl").write_text(
            rec("old", 900, ts="2020-01-01T00:00:00.000Z") + "\n" + rec("m1", 1234, ts=now) + "\n")
        v = MON.discover()[0]
        self.assertEqual(v["usage"]["output"], 1234)            # before the run started → not this run's cost
        self.assertEqual(set(v["sessions"][0]), {"session", "bound_at"})   # full session id is not exposed
        self.assertIn("çıktı 1.2K", MON.render_text([v], width=140))
        self.assertIn("çıktı 1.2K", self.cli("watch", "--once").stdout)
        self.assertEqual(json.loads(self.cli("watch", "--json").stdout)[0]["usage"]["output"], 1234)

    def test_tok_format(self):
        self.assertEqual([MON._tok(n) for n in (0, 999, 1000, 45200, 1_000_000, 38_400_000)],
                         ["0", "999", "1.0K", "45.2K", "1.00M", "38.40M"])


class StableOrder(helpers.Sandbox):
    def test_runs_keep_project_order_regardless_of_activity(self):
        for name in ("zeta", "alpha"):
            self.make_project(name)
            self.cli("init", "-p", name)
            self.cli("start", "-p", name, "--title", "T", "--request", "r", "--size", "S")
        order = lambda: [v["project_name"] for v in MON.discover()]  # noqa: E731
        self.assertEqual(order(), ["alpha", "zeta"])
        for name in ("zeta", "alpha", "zeta"):                    # whoever was active last must not jump first
            st = next((self.ws / name / ".orkestra" / "runs").glob("*/state.json"))
            future = time.time() + 60
            os.utime(st, (future, future))
            self.assertEqual(order(), ["alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
