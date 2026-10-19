"""cursorkit.merge_hooks: stop loop_limit null (always) and the opt-in failClosed."""
from __future__ import annotations

import json

import helpers  # noqa: F401  (puts the plugin on sys.path)
import unittest

from orkestra_core import cursorkit as CK
from orkestra_core.cliparse import build_parser


def _entry(doc, event):
    return [e for e in doc["hooks"][event] if CK._is_ours(e)][0]


class TestCursorHooksMerge(unittest.TestCase):
    def test_stop_has_explicit_null_loop_limit(self) -> None:
        doc = CK.merge_hooks(None)
        e = _entry(doc, "stop")
        self.assertIn("loop_limit", e)
        self.assertIsNone(e["loop_limit"])
        self.assertNotIn("loop_limit", _entry(doc, "preToolUse"))
        self.assertIn("null", json.dumps(doc))

    def test_fail_closed_is_off_by_default(self) -> None:
        doc = CK.merge_hooks(None)
        self.assertFalse(any("failClosed" in e for es in doc["hooks"].values() for e in es))

    def test_fail_closed_survives_a_plain_reinstall(self) -> None:
        doc = CK.merge_hooks(None, fail_closed=True)
        again = CK.merge_hooks(doc)
        self.assertEqual(again, doc)

    def test_fail_closed_opt_in_and_idempotent(self) -> None:
        doc = CK.merge_hooks(None, fail_closed=True)
        for ev in CK.HOOK_EVENTS:
            self.assertEqual(_entry(doc, ev).get("failClosed"), True if ev in CK.FAIL_CLOSED else None)
        self.assertEqual(CK.merge_hooks(doc, fail_closed=True), doc)
        self.assertEqual(len(doc["hooks"]["preToolUse"]), 1)

    def test_foreign_entry_untouched(self) -> None:
        mine = {"command": "echo hi", "failClosed": True}
        doc = CK.merge_hooks({"version": 1, "hooks": {"stop": [mine]}}, fail_closed=True)
        self.assertEqual(doc["hooks"]["stop"][0], mine)

    def test_cli_flag_parses(self) -> None:
        p = build_parser()
        self.assertTrue(p.parse_args(["cursor", "install", "--fail-closed"]).fail_closed)
        self.assertFalse(p.parse_args(["cursor", "install"]).fail_closed)


if __name__ == "__main__":
    unittest.main()
