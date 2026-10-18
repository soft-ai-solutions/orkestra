"""A finished run indexes itself, and the next request sees the lessons that share its words."""
from __future__ import annotations

import unittest
from unittest import mock

import helpers
from orkestra_core import memory, runs, state as ST


class TestRank(unittest.TestCase):
    def test_overlap_wins_and_a_miss_stays_on_the_newest(self):
        lines = [
            "- 2026-01-01 [001-old] widget price must stay in minor units",
            "- 2026-02-01 [002-mid] deploy script rejects a foreign docroot",
            "- 2026-03-01 [003-new] unrelated clock label spacing",
        ]
        picked, matched = memory.rank_learnings(lines, "fix the widget price rounding", 2)
        self.assertTrue(matched)
        self.assertEqual(picked, [lines[0]])
        newest, matched = memory.rank_learnings(lines, "hello", 2)
        self.assertFalse(matched)
        self.assertEqual(newest, lines[-2:])


class TestRemember(helpers.Sandbox):
    def test_done_records_the_run_and_the_proved_mistake_once(self):
        proj = self.make_project()
        runs.init_project(proj)
        rid = "012-index"
        st = ST.new_state(proj, rid, "Widget price", "S", "interactive")
        st["tasks"] = {"T001": {"notes": [
            "BLOCKED: widget price ignored the minor unit",
            "minor: rename a local",
        ]}}
        review = proj / ".orkestra" / "features" / rid / "reviews"
        review.mkdir(parents=True)
        (review / "triage.md").write_text(
            "| id | source | finding (file:line) | verdict | status | note |\n"
            "| R-001 | reviewer | `src/a.ts:42` missing null check | fix | done | example |\n"
            "| R-002 | reviewer | `pricing.ts:12` widget price dropped the minor unit | fix | done | closed |\n"
            "| R-003 | adversary | guessed a timeout | reject | - | no |\n"
            "| R-004 | reviewer | `cart.ts:3` sibling still uses the old total | fix | open | waiting |\n",
            encoding="utf-8")
        self.assertEqual(memory.remember_finished_run(proj, st), 2)
        self.assertEqual(memory.remember_finished_run(proj, st), 0)
        text = (proj / ".orkestra" / "memory" / "LEARNINGS.md").read_text(encoding="utf-8")
        self.assertIn("widget price ignored the minor unit", text)
        self.assertIn("widget price dropped the minor unit", text)
        self.assertNotIn("missing null check", text)
        self.assertNotIn("sibling still uses", text)
        self.assertIn("| 012-index |", (proj / ".orkestra" / "memory" / "FEATURES.md").read_text(encoding="utf-8"))
        memory.feature_add(proj, rid, "Widget price", "kept summary", ["price"])
        memory.remember_finished_run(proj, st)
        feats = (proj / ".orkestra" / "memory" / "FEATURES.md").read_text(encoding="utf-8")
        self.assertIn("kept summary", feats)
        self.assertEqual(feats.count("| 012-index |"), 1)

    def test_phase_next_indexes_and_a_failed_index_still_finishes(self):
        proj = self.make_project()
        runs.init_project(proj)
        st = ST.new_state(proj, "013-done", "Widget price", "S", "interactive")
        st["phase"] = "deliver"
        st["tasks"] = {"T001": {"notes": ["BLOCKED: widget price ignored the minor unit"]}}
        ST.save_state(proj, st)
        with mock.patch.object(runs, "preconditions", return_value=[]):
            runs.phase_next(proj, "013-done")
        learned = (proj / ".orkestra" / "memory" / "LEARNINGS.md").read_text(encoding="utf-8")
        self.assertIn("widget price ignored the minor unit", learned)
        picked, matched = memory.rank_learnings(
            [ln for ln in learned.splitlines() if ln.startswith("- ")], "widget price rounding", 4)
        self.assertTrue(matched)
        self.assertIn("minor unit", picked[0])
        st2 = ST.new_state(proj, "014-done", "Other", "S", "interactive")
        st2["phase"] = "deliver"
        ST.save_state(proj, st2)
        with mock.patch.object(runs, "preconditions", return_value=[]), \
                mock.patch.object(memory, "remember_finished_run", side_effect=OSError("full")):
            cur, nxt = runs.phase_next(proj, "014-done")
        self.assertEqual((cur, nxt), ("deliver", "done"))


class TestFirstObserve(helpers.Sandbox):
    def test_init_fills_only_what_exists_and_check_rejects_an_uncited_claim(self):
        proj = self.make_project()
        (proj / "AGENTS.md").write_text("# Demo\n\nA shop calculator.\n\n1. Never delete tests.\n", encoding="utf-8")
        (proj / ".env.example").write_text("SECRET=hunter2\n", encoding="utf-8")
        (proj / "src").mkdir()
        (proj / "src" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
        self.assertTrue(runs.init_project(proj))
        text = (proj / ".orkestra" / "memory" / "PROJECT.md").read_text(encoding="utf-8")
        self.assertIn("A shop calculator.", text)
        self.assertIn("`AGENTS.md`", text)
        self.assertIn("`Makefile`", text)
        self.assertIn("`src/index.js`", text)
        self.assertIn("`tests`", text)
        self.assertIn("SECRET", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("unknown", text)
        self.assertEqual(memory.check(proj), [])
        self.assertFalse(memory.observe(proj))
        bad = text.replace("## Conventions\n- unknown", "## Conventions\n- uses classes")
        (proj / ".orkestra" / "memory" / "PROJECT.md").write_text(bad, encoding="utf-8")
        self.assertTrue(any("no cited path" in item for item in memory.check(proj)))
        out = self.cli("memory", "check", "-p", "demo", check=False)
        self.assertEqual(out.returncode, 1)
        memory.observe(proj)  # filled file is kept, even when the check would fail
        self.assertIn("uses classes", (proj / ".orkestra" / "memory" / "PROJECT.md").read_text(encoding="utf-8"))
