"""`tdd red` classifies the failure deterministically and refuses a run that shows no test failing."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import helpers  # noqa: F401
from orkestra_core import runs as R
from test_tempo import TempoBase

PYTEST_ASSERT = "FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2\n1 failed in 0.1s"
PYTEST_IMPORT = "E   ModuleNotFoundError: No module named 'calc'\nERROR collecting tests/test_a.py"
PYTEST_EMPTY = "no tests ran in 0.01s"
PYTEST_ZERO = "collected 0 items\n"
PYTEST_SYNTAX = "E     File \"calc.py\", line 1\nE   SyntaxError: invalid syntax"
UNITTEST_FAIL = "FAIL: test_add (test_a.T)\nAssertionError: 3 != 4\nRan 1 test in 0.001s\nFAILED (failures=1)"
UNITTEST_NONE = "Ran 0 tests in 0.000s\n\nNO TESTS RAN"
UNITTEST_IMPORT = "ImportError: Failed to import test module: test_a\nModuleNotFoundError: No module named 'calc'"
JEST_FAIL = "FAIL src/a.test.js\n  expect(received).toBe(expected)\nExpected: 4\nTests: 1 failed, 1 total"
JEST_NONE = "No tests found, exiting with code 1"
JEST_MISSING = "Cannot find module './calc' from 'src/a.test.js'"
GO_FAIL = "--- FAIL: TestAdd (0.00s)\n    calc_test.go:9: got 3, want 4\nFAIL\nexit status 1"
GO_MISSING = "./calc_test.go:5:9: undefined: Add\nFAIL\tdemo [build failed]"
GO_OTHER_PKGS = "?   \tdemo/util\t[no test files]\n--- FAIL: TestAdd (0.00s)\nFAIL"

JEST_TOTHROW = ("FAIL src/a.test.js\n  expect(received).toThrow(expected)\n  Expected constructor: SyntaxError\n"
                "Tests:       1 failed, 1 total")
PYTEST_QUOTED = "FAILED tests/test_a.py::test_x - AssertionError: SyntaxError was not raised\n1 failed in 0.1s"
GO_MULTI = ("testing: warning: no tests to run\nok  \tdemo/util\t0.01s [no tests to run]\n"
            "--- FAIL: TestAdd (0.00s)\n    calc_test.go:9: got 3, want 4\nFAIL\tdemo\t0.01s")
JEST_SUITE_SYNTAX = "FAIL src/a.test.js\n  Test suite failed to run\n    SyntaxError: Unexpected token\nTests: 0 total"

CLASSIFY = [
    (JEST_TOTHROW, "assertion", False), (PYTEST_QUOTED, "assertion", False), (GO_MULTI, "assertion", False),
    (JEST_SUITE_SYNTAX, "collection_or_syntax", True),
    (PYTEST_ASSERT, "assertion", False), (UNITTEST_FAIL, "assertion", False), (JEST_FAIL, "assertion", False),
    (GO_FAIL, "assertion", False), (PYTEST_IMPORT, "missing_symbol", False), (UNITTEST_IMPORT, "missing_symbol", False),
    (JEST_MISSING, "missing_symbol", False), (GO_MISSING, "missing_symbol", False),
    (PYTEST_EMPTY, "collection_or_syntax", True), (PYTEST_ZERO, "collection_or_syntax", True),
    (PYTEST_SYNTAX, "collection_or_syntax", True), (UNITTEST_NONE, "collection_or_syntax", True),
    (JEST_NONE, "collection_or_syntax", True), (GO_OTHER_PKGS, "assertion", False),
    ("something odd", "assertion", False),
]


class TestClassify(unittest.TestCase):
    def test_table(self):
        for out, reason, refuse in CLASSIFY:
            self.assertEqual(R.classify_red(out), (reason, refuse), out)


class RedBase(TempoBase):
    lang = "en"

    def red(self, out, why=""):
        self.to_implement()
        self.cli("task", "start", "T001", "-p", "demo")
        with mock.patch.object(R, "_run_shell_or_list",
                               return_value=mock.Mock(returncode=1, stdout=out.encode(), stderr=b"")):
            return R.tdd_record(self.proj, self.rid, "T001", "red", ["pytest"], why=why)

    def test_a_real_assertion_failure_is_red_with_a_reason(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.lang}):
            rec = self.red(PYTEST_ASSERT)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["reason"], "assertion")

    def test_import_failure_is_red_and_recorded(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.lang}):
            rec = self.red(UNITTEST_IMPORT)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["reason"], "missing_symbol")

    def test_no_test_ran_is_refused_unless_why(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.lang}):
            rec = self.red(PYTEST_EMPTY)
        self.assertFalse(rec["ok"])
        self.assertTrue(any("red refused" in d for d in rec["deviations"]))

    def test_why_allows_it(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.lang}):
            rec = self.red(PYTEST_SYNTAX, why="new file has no test yet")
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["reason"], "collection_or_syntax")


class RedBaseTr(RedBase):
    lang = "tr"


del TempoBase  # keep the base out of this module's test discovery

if __name__ == "__main__":
    unittest.main()
