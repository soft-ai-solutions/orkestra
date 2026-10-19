"""Gates that cannot run (missing tool or deps) are reported apart from real failures and never read as a pass."""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (pins the test environment)
from orkestra_core import gates as G
from orkestra_core import i18n
from orkestra_core import runs as R

PY = shlex.quote(sys.executable)


class EnvBase(unittest.TestCase):
    lang = "en"

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="orkestra-genv-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        p = mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.lang})
        p.start()
        self.addCleanup(p.stop)

    def gate(self, name, cmd, **kw):
        return G.run_gate(self.td, dict({"name": name, "cmd": cmd, "kind": "test"}, **kw), self.td / "logs", "t")


class TestEnvErrorDetection(EnvBase):
    def test_exit_127_and_output_markers_set_env_error(self):
        self.assertTrue(self.gate("a", "orkestra-no-such-tool-xyz")["env_error"])
        for text in ("No module named pytest", "Error: Cannot find module 'jest'", "spawn x ENOENT",
                     "sh: foo: command not found"):
            res = self.gate("b", f"echo {shlex.quote(text)}; exit 1")
            self.assertTrue(res["env_error"], text)
        self.assertTrue(self.gate("c", "exit 126")["env_error"])

    def test_a_real_failure_is_not_an_env_error(self):
        res = self.gate("d", "echo 'AssertionError: 1 != 2'; exit 1")
        self.assertFalse(res["ok"])
        self.assertNotIn("env_error", res)
        self.assertNotIn("env_error", self.gate("e", "exit 0"))

    def test_code_under_test_failures_are_not_env_errors(self):
        for text in ("src/a.ts(1,8): error TS2307: Cannot find module './x' or its corresponding type declarations.",
                     "E   ModuleNotFoundError: No module named 'app.export_csv'",
                     "Cannot find module './calc' from 'src/a.test.js'",
                     "console.error ENOENT: no such file or directory, open 'fixtures/a.json'"):
            self.assertFalse(G.is_env_error(1, text), text)
            self.assertNotIn("env_error", self.gate("r", f"echo {shlex.quote(text)}; exit 1"), text)

    def test_the_gates_own_tool_missing_is_an_env_error(self):
        for text, cmd in (("/usr/bin/python3: No module named pytest", "python3 -m pytest -q"),
                          ("/usr/bin/python3: No module named mytool", "python3 -m mytool run"),
                          ("sh: 1: jest: command not found", "npm test"),
                          ("Error: Cannot find module 'jest'", "npm test"),
                          ("sh: node_modules/.bin/tsc: No such file or directory", "npm run typecheck"),
                          ("Error: spawn eslint ENOENT", "npm run lint")):
            self.assertTrue(G.is_env_error(1, text, cmd), text)

    def test_timeout_carries_the_hint(self):
        res = self.gate("t", "sleep 30", timeout=1)
        self.assertTrue(res["timed_out"])
        self.assertIn("raise timeout in .orkestra/config.json (now 1s) or set enabled:false", res["tail"])


class TestEnvReporting(EnvBase):
    def results(self):
        return [self.gate("one", "orkestra-no-such-tool-xyz"), self.gate("two", "exit 127")]

    def test_compare_keeps_env_errors_apart_from_pre_existing(self):
        base = self.results() + [dict(self.gate("three", "exit 1"))]
        cmp = G.compare(base, base)
        self.assertEqual(sorted(cmp["env_errors"]), ["one", "two"])
        self.assertEqual(cmp["pre_existing_failures"], ["three"])

    def test_report_names_the_gates_that_could_not_run(self):
        rs = self.results()
        text = G.format_results(rs, G.compare(rs, rs), rs)
        self.assertIn("2 gates could not run (missing tool or deps): one, two", text)
        self.assertIn("no gate could run", text)
        self.assertTrue(G.all_env_error(rs))
        self.assertFalse(G.all_env_error(rs + [self.gate("ok", "exit 0")]))
        self.assertFalse(G.all_env_error([]))

    def test_only_partial_env_failures_do_not_trip_the_all_rule(self):
        rs = [self.gate("ok", "exit 0"), self.gate("x", "exit 127")]
        self.assertFalse(G.all_env_error(rs))
        self.assertNotIn("no gate could run", G.format_results(rs, G.compare(rs, rs), rs))

    def test_review_needs_a_waiver_when_a_gate_could_not_run(self):
        from orkestra_core import paths
        proj = self.td / "proj"
        proj.mkdir()
        rid = "001-x"
        fdir, rdir = paths.feature_dir(proj, rid), paths.run_dir(proj, rid)
        fdir.mkdir(parents=True)
        rdir.mkdir(parents=True)
        rs = self.results()
        G.save_results(rdir / "gates-verify.json", rs, {"comparison": G.compare(rs, rs), "ts": 9e12})
        st = {"run_id": rid, "tasks": {}}
        with mock.patch.object(R.S, "changed_files", return_value=[]):
            probs = R.preconditions(proj, st, "review")
            self.assertTrue(any("one, two" in p for p in probs), probs)
            (fdir / "gates-waiver.md").write_text("tools are installed in CI only\n")
            probs = R.preconditions(proj, st, "review")
        self.assertFalse(any("could not run" in p or "çalışamadı" in p for p in probs), probs)


class TestEnvReportingTr(TestEnvReporting):
    lang = "tr"

    def test_report_names_the_gates_that_could_not_run(self):
        rs = self.results()
        text = G.format_results(rs, G.compare(rs, rs), rs)
        self.assertIn("2 gate çalışamadı", text)
        self.assertIn("hiçbir gate çalışamadı", text)

    def test_timeout_hint_is_localized(self):
        self.assertIn("timeout değerini artırın (şimdi 5s)", i18n.t("runs.gate_timeout_hint", seconds=5))

    def test_no_git_sentence(self):
        self.assertIn("git init", i18n.t("runs.no_git_rollback"))


class TestEnvCli(EnvBase):
    def test_baseline_exits_nonzero_when_every_gate_is_env_error(self):
        from orkestra_core import cli as C
        rs = [self.gate("only", "orkestra-no-such-tool-xyz")]
        with mock.patch.object(R, "gates_baseline", return_value=(rs, "table")), \
                mock.patch.object(C, "_resolve_project", return_value=self.td), \
                mock.patch.object(C, "_run_id", return_value="001-x"), \
                mock.patch.object(C, "_require_project_config"), \
                mock.patch.object(C.paths, "project_config", return_value={"gates": []}):
            ns = mock.Mock(project=None, run=None, action="baseline")
            self.assertEqual(C.cmd_gate(ns), 1)


class TestNoGitSentence(unittest.TestCase):
    def test_en(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"}):
            self.assertEqual(i18n.t("runs.no_git_rollback"),
                             "No git here: Orkestra can only restore files edited through its hooks. "
                             "Run git init for full rollback.")


if __name__ == "__main__":
    unittest.main()
