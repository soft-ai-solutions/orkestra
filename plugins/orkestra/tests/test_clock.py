"""H:MM:SS clocks on the watch row and the dashboard phase line."""
from __future__ import annotations

import unittest
from unittest import mock

import helpers  # puts the plugin lib on sys.path before orkestra_core
from orkestra_core import hookmain as HM
from orkestra_core import monitor as MON
from orkestra_core import runs
from orkestra_core import state as ST
from orkestra_core import tempo as T
from test_dashboard_ux import EXPAND, PageCase, ev, view
from test_i18n_monitor import lang


class TestClock(unittest.TestCase):
    def test_clock_is_hours_minutes_seconds(self):
        self.assertEqual(T.clock(0), "0:00:00")
        self.assertEqual(T.clock(59), "0:00:59")
        self.assertEqual(T.clock(60), "0:01:00")
        self.assertEqual(T.clock(3599), "0:59:59")
        self.assertEqual(T.clock(3600), "1:00:00")
        self.assertEqual(T.clock(7000), "1:56:40")
        self.assertEqual(T.clock(90000), "25:00:00")
        self.assertEqual(T.clock(None), "?")
        self.assertEqual(T.clock(-4), "0:00:00")

    def test_a_phase_visited_twice_is_summed(self):
        st = {"created": "2026-10-18T00:00:00+00:00", "updated": "2026-10-18T02:00:00+00:00", "status": "done",
              "phase": "done", "tasks": {},
              "history": [
                  {"at": "2026-10-18T00:00:00+00:00", "to": "intake"},
                  {"at": "2026-10-18T00:10:00+00:00", "to": "plan"},
                  {"at": "2026-10-18T00:40:00+00:00", "to": "intake"},
                  {"at": "2026-10-18T01:00:00+00:00", "to": "done"},
              ]}
        phases = T.timings(st, now=0)["phases"]
        self.assertEqual(int(phases["intake"]), 30 * 60)
        self.assertEqual(int(phases["plan"]), 30 * 60)
        self.assertNotIn("done", phases)


def _view(**over):
    v = {"project_name": "demo", "run_id": "r1", "title": "Add sub", "size": "S", "mode": "interactive",
         "engine": "cursor", "phase": "implement", "phase_index": 5, "phase_since": None,
         "live": {"level": "run", "text": "working"},
         "stall": {"cls": 0, "key": "working", "text": "ok", "loops": []},
         "timing": {"run": 7000, "phases": {"implement": 6880, "intake": 120}},
         "tasks_total": 0, "tasks_done": 0, "current_tasks": [],
         "gates": {"baseline": "—", "verify": "—"}, "sessions": [], "usage": None,
         "activity": [], "ledger": []}
    v.update(over)
    return v


class TestWatchClock(unittest.TestCase):
    def text(self, code, **over):
        with lang(code):
            return MON.render_text([_view(**over)], color=False, width=160)

    def test_english_row_is_a_clock_and_the_pace_row_stays(self):
        text = self.text("en")
        self.assertIn("Total 1:56:40 · intake 0:02:00 · implementation 1:54:40", text)
        self.assertIn("total 1h 56m", text)
        self.assertLess(text.index("intake 0:02:00"), text.index("implementation 1:54:40"))

    def test_turkish_row_uses_phase_names(self):
        text = self.text("tr")
        self.assertIn("Toplam 1:56:40 · giriş 0:02:00 · uygulama 1:54:40", text)
        self.assertIn("toplam 1 sa 56 dk", text)

    def test_a_run_with_no_time_omits_the_row(self):
        text = self.text("en", timing={"run": 0})
        self.assertNotIn("0:00:00", text)
        self.assertNotIn("Total", text)

    def test_a_finished_run_keeps_the_clocks(self):
        text = self.text("en", status="done", phase="done", phase_index=9)
        self.assertIn("Total 1:56:40", text)


class TestDashboardClock(PageCase):
    def line(self, v, steps=()):
        out = self.run_page([v], list(steps) + [
            ev("const n=H.find(H.byId('main'),x=>x.className==='clk')[0];return n?n.textContent:'';", "r")])
        self.assertEqual(out["errors"], [])
        return out["results"]["r"]

    def test_open_run_advances_the_current_phase_and_the_total(self):
        v = view(1, "running", "working", status="active", phase="implement",
                 timing={"run": 7000, "phases": {"intake": 120, "implement": 6880}},
                 phase_history=[{"phase": "intake", "seconds": 120}, {"phase": "implement", "seconds": 6880}])
        before = self.line(v)
        self.assertIn("Total 1:56:40", before)
        self.assertIn("intake 0:02:00", before)
        self.assertIn("implementation 1:54:40", before)
        after = self.line(v, [{"op": "advance", "ms": 5000}])
        self.assertIn("Total 1:56:45", after)
        self.assertIn("implementation 1:54:45", after)
        self.assertIn("intake 0:02:00", after)

    def test_a_finished_run_stays_frozen(self):
        v = view(1, "done", "done", status="done", phase="done", phase_index=9,
                 timing={"run": 7000, "phases": {"intake": 7000}},
                 phase_history=[{"phase": "intake", "seconds": 7000}])
        after = self.line(v, [EXPAND, {"op": "advance", "ms": 5000}])
        self.assertIn("Total 1:56:40", after)
        self.assertNotIn("1:56:45", after)

    def test_turkish_dashboard_names(self):
        v = view(1, "running", "working", timing={"run": 90, "phases": {}}, phase_history=[])
        out = self.run_page([v], [ev("return H.find(H.byId('main'),x=>x.className==='clk')[0].textContent", "r")],
                            code="tr")
        self.assertIn("Toplam 0:01:30", out["results"]["r"])


def _closed(project):
    st = ST.new_state(project, "r1", "Süre", "S", "interactive")
    st["status"] = "done"
    st["phase"] = "done"
    st["created"] = "2026-10-18T00:00:00+00:00"
    st["updated"] = "2026-10-18T02:00:00+00:00"
    st["history"] = [
        {"at": "2026-10-18T00:00:00+00:00", "to": "intake"},
        {"at": "2026-10-18T01:00:00+00:00", "to": "deliver"},
        {"at": "2026-10-18T02:00:00+00:00", "to": "done"},
    ]
    return st


class TestTimingFile(helpers.Sandbox):
    def test_done_file_lists_the_total_and_each_phase(self):
        proj = self.make_project()
        with lang("en"):
            runs.write_timing(proj, _closed(proj))
        text = (proj / ".orkestra" / "features" / "r1" / "timing.md").read_text(encoding="utf-8")
        self.assertIn("Total 2:00:00", text)
        self.assertIn("intake: 1:00:00", text)
        self.assertIn("delivery: 1:00:00", text)
        self.assertNotIn("done:", text)

    def test_phase_next_writes_the_file_and_a_failed_write_still_finishes(self):
        proj = self.make_project()
        st = ST.new_state(proj, "r1", "Süre", "S", "interactive")
        st["phase"] = "deliver"
        ST.save_state(proj, st)
        with mock.patch.object(runs, "preconditions", return_value=[]):
            runs.phase_next(proj, "r1")
        self.assertTrue((proj / ".orkestra" / "features" / "r1" / "timing.md").is_file())
        self.assertEqual(ST.load_state(proj, "r1")["phase"], "done")
        st2 = ST.new_state(proj, "r2", "Süre", "S", "interactive")
        st2["phase"] = "deliver"
        ST.save_state(proj, st2)
        with mock.patch.object(runs, "preconditions", return_value=[]), \
                mock.patch.object(runs, "write_timing", side_effect=OSError("full")):
            cur, nxt = runs.phase_next(proj, "r2")
        self.assertEqual((cur, nxt), ("deliver", "done"))
        self.assertEqual(ST.load_state(proj, "r2")["phase"], "done")


class TestSessionStartSummary(helpers.Sandbox):
    def test_the_hint_stays_and_the_full_read_is_gone(self):
        proj = self.make_project()
        st = {"run_id": "r1", "title": "Süre", "phase": "plan", "status": "active", "tasks": {}}
        with mock.patch.object(HM, "next_hint", return_value="next [plan] write plan.md"):
            text = HM.run_summary(proj, st)
        self.assertIn("Active run r1", text)
        self.assertIn("next [plan] write plan.md", text)
        self.assertNotIn("orkestra context", text)
        self.assertNotIn("orkestra playbook", text)

    def test_without_a_hint_the_summary_names_only_next(self):
        proj = self.make_project()
        st = {"run_id": "r1", "title": "Süre", "phase": "plan", "status": "active", "tasks": {}}
        with mock.patch.object(HM, "next_hint", return_value=""):
            text = HM.run_summary(proj, st)
        self.assertIn("`orkestra next -p ", text)
        self.assertNotIn("orkestra context", text)
        self.assertNotIn("orkestra playbook", text)


if __name__ == "__main__":
    unittest.main()
