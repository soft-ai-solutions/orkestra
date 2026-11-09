"""Weekday GMT+3 commit and push window. The clock is injected; nothing here reads the wall clock."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import helpers  # noqa: F401
from orkestra_core import guards, i18n
from orkestra_core.messages.common import EN
from orkestra_core.officehours import GMT3, REASON, commit_push_closed, in_orkestra_checkout

GRANTS = ("commit", "push")


def _at(year, month, day, hour, minute, tz=GMT3):
    return datetime(year, month, day, hour, minute, tzinfo=tz)


class Window(unittest.TestCase):
    def test_weekdays_are_closed_from_nine_until_eighteen(self):
        # 2026-11-09 is a Monday. Friday the same week is the 13th.
        self.assertFalse(commit_push_closed(_at(2026, 11, 9, 8, 59)))
        self.assertTrue(commit_push_closed(_at(2026, 11, 9, 9, 0)))
        self.assertTrue(commit_push_closed(_at(2026, 11, 9, 12, 0)))
        self.assertTrue(commit_push_closed(_at(2026, 11, 13, 17, 59)))
        self.assertFalse(commit_push_closed(_at(2026, 11, 9, 18, 0)))
        self.assertFalse(commit_push_closed(_at(2026, 11, 9, 19, 0)))

    def test_weekend_stays_open(self):
        self.assertFalse(commit_push_closed(_at(2026, 11, 14, 9, 0)))   # Saturday
        self.assertFalse(commit_push_closed(_at(2026, 11, 15, 17, 59)))  # Sunday

    def test_other_zones_are_read_as_gmt_plus_3(self):
        utc = timezone.utc
        self.assertTrue(commit_push_closed(_at(2026, 11, 9, 6, 0, utc)))    # 09:00 GMT+3
        self.assertFalse(commit_push_closed(_at(2026, 11, 9, 5, 59, utc)))  # 08:59 GMT+3
        self.assertFalse(commit_push_closed(datetime(2026, 11, 9, 8, 59)))  # naive, treated as GMT+3

    def test_english_sentence_matches_the_catalog(self):
        self.assertEqual(EN["common.office_hours"], REASON)
        os.environ["ORKESTRA_LANG"] = "en"
        try:
            self.assertEqual(i18n.t("common.office_hours"), REASON)
        finally:
            os.environ["ORKESTRA_LANG"] = "tr"


class Checkout(unittest.TestCase):
    def test_only_the_orkestra_tree_matches(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "plugins" / "orkestra" / "lib" / "orkestra_core"
            launcher = root / "plugins" / "orkestra" / "bin"
            marker.mkdir(parents=True)
            launcher.mkdir(parents=True)
            (marker / "guards.py").write_text("#\n", encoding="utf-8")
            (launcher / "orkestra").write_text("#\n", encoding="utf-8")
            nested = marker / "nested"
            nested.mkdir()
            self.assertTrue(in_orkestra_checkout(str(nested)))
            outside = root.parent / ("not-orkestra-" + root.name)
            outside.mkdir()
            self.addCleanup(outside.rmdir)
            self.assertFalse(in_orkestra_checkout(str(outside)))


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        marker = root / "plugins" / "orkestra" / "lib" / "orkestra_core"
        launcher = root / "plugins" / "orkestra" / "bin"
        marker.mkdir(parents=True)
        launcher.mkdir(parents=True)
        (marker / "guards.py").write_text("#\n", encoding="utf-8")
        (launcher / "orkestra").write_text("#\n", encoding="utf-8")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_and_push_are_refused_inside_the_window_even_with_a_grant(self):
        from orkestra_core.paths import DEFAULT_GLOBAL_CONFIG
        when = _at(2026, 11, 9, 10, 17)
        for cmd in ("git commit -m 'ship'", "git push origin HEAD", "git commit --amend -m 'ship'"):
            ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.root), "_clock": when}
            decision = guards.pre_tool_decision(ev, DEFAULT_GLOBAL_CONFIG, None, GRANTS)
            self.assertTrue(decision.denied, cmd)
            self.assertIn("09:00 to 18:00 GMT+3", decision.reason)

    def test_the_same_commands_are_allowed_at_nineteen_and_outside_this_repo(self):
        from orkestra_core.paths import DEFAULT_GLOBAL_CONFIG
        evening = {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"},
                   "cwd": str(self.root), "_clock": _at(2026, 11, 9, 19, 0)}
        self.assertFalse(guards.pre_tool_decision(evening, DEFAULT_GLOBAL_CONFIG, None, GRANTS).denied)
        other = {"tool_name": "Bash", "tool_input": {"command": "git commit -m ship"},
                 "cwd": "/w/proj", "_clock": _at(2026, 11, 9, 10, 17)}
        self.assertFalse(guards.pre_tool_decision(other, DEFAULT_GLOBAL_CONFIG, None, GRANTS).denied)
        status = {"tool_name": "Bash", "tool_input": {"command": "git status"},
                  "cwd": str(self.root), "_clock": _at(2026, 11, 9, 10, 17)}
        self.assertFalse(guards.pre_tool_decision(status, DEFAULT_GLOBAL_CONFIG, None, ()).denied)


if __name__ == "__main__":
    unittest.main()
