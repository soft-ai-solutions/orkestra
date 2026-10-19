"""FR-001 / FR-002 / SC-001 — doctor soft-pass when any engine is ready."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

import helpers
from orkestra_core import cursorkit as CK


def _path_without_claude() -> str:
    """PATH with git/python but no `claude` binary (isolates the soft-pass cases)."""
    parts = []
    for d in ("/usr/bin", "/bin", str(Path(sys.executable).parent)):
        if Path(d).is_dir() and d not in parts:
            parts.append(d)
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if d and d not in parts and not (Path(d) / "claude").exists():
            parts.append(d)
    return os.pathsep.join(parts)


class TestDoctorSoftPass(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.chome = Path(os.environ["ORKESTRA_CURSOR_HOME"])
        self.safe_path = _path_without_claude()
        self.assertIsNone(shutil.which("claude", path=self.safe_path),
                          "fixture PATH must not resolve claude")
        self.doctor_env = {"PATH": self.safe_path, "COPILOT_HOME": str(self.tmp / "nocopilot")}

    def _install_cursor_hooks(self) -> None:
        self.chome.mkdir(parents=True, exist_ok=True)
        (self.chome / "hooks.json").write_text(json.dumps(CK.merge_hooks(None)))
        self.assertTrue(CK.hooks_installed())

    def test_cursor_hooks_only_passes_and_claude_is_soft(self):
        """E1 / FR-001 / FR-002 / SC-001: Cursor hooks ready, no claude → exit 0, soft claude."""
        self._install_cursor_hooks()
        cp = self.cli("doctor", check=False, env=self.doctor_env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotRegex(cp.stdout, r"\[!!\]\s+claude\b")
        self.assertRegex(cp.stdout, r"\[--\]\s+claude\b")

    def test_no_engines_ready_fails(self):
        """E2 / FR-001 / SC-001: required OK but no claude/cursor/copilot → exit 1."""
        self.assertFalse(CK.hooks_installed())
        self.assertFalse((self.tmp / "nocopilot").exists())
        cp = self.cli("doctor", check=False, env=self.doctor_env)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)

    def test_missing_workspace_root_is_a_hint_not_a_failure(self):
        """A fresh machine has no ~/Projects: projects resolve by .git / .orkestra, so doctor must not fail on it."""
        self._install_cursor_hooks()
        env = dict(self.doctor_env, ORKESTRA_WORKSPACE=str(self.tmp / "does-not-exist"))
        cp = self.cli("doctor", check=False, env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout, r"\[--\]\s+workspace root")


    def _registry(self, install_path, version="1.6.0"):
        cfg = self.tmp / "claude-config"
        (cfg / "plugins").mkdir(parents=True, exist_ok=True)
        (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
            "orkestra@orkestra-local": [{"scope": "user", "installPath": str(install_path), "version": version}]}}))
        return dict(self.doctor_env, CLAUDE_CONFIG_DIR=str(cfg))

    def test_a_stale_claude_plugin_copy_is_reported_and_a_fresh_one_is_not(self):
        """Claude Code runs hooks from its own cached copy: an edited checkout is not what the guards run until it is
        updated. doctor says so (soft: a hint, never a failure)."""
        self._install_cursor_hooks()
        copy = self.tmp / "cache" / "orkestra" / "1.6.0"
        shutil.copytree(helpers.PLUGIN, copy, ignore=shutil.ignore_patterns("tests", "__pycache__", "cursor", "vendor"))
        env = dict(self._registry(copy), ORKESTRA_LANG="en")
        cp = self.cli("doctor", check=False, env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout, r"\[OK\]\s+claude plugin copy .*matches this checkout")
        (copy / "lib" / "orkestra_core" / "monitor.py").write_text("# an older version\n", encoding="utf-8")
        cp = self.cli("doctor", check=False, env=env)
        self.assertEqual(cp.returncode, 0, "a stale copy is a hint, not a failure")
        self.assertRegex(cp.stdout, r"\[--\]\s+claude plugin copy .*differs from this checkout")
        self.assertIn("claude plugin update", cp.stdout)
        self.assertIn("claude plugin marketplace update orkestra-local && claude plugin update orkestra@orkestra-local",
                      cp.stdout.replace("\n", " "))
        # the row name and the [--] mark are stable; the detail follows the language
        tr = self.cli("doctor", check=False, env=dict(env, ORKESTRA_LANG="tr"))
        self.assertEqual(tr.returncode, 0, tr.stdout + tr.stderr)
        self.assertRegex(tr.stdout, r"\[--\]\s+claude plugin copy .*bu checkout'tan farklı")
        self.assertIn("claude plugin update", tr.stdout)

    def test_the_details_and_the_summary_follow_the_language_the_row_names_do_not(self):
        self._install_cursor_hooks()
        for lang, summary, none in (("en", "doctor: all required checks passed", "not found"),
                                    ("tr", "doctor: gerekli tüm kontroller geçti", "bulunamadı")):
            cp = self.cli("doctor", check=False, env=dict(self.doctor_env, ORKESTRA_LANG=lang))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(summary, cp.stdout)
            self.assertRegex(cp.stdout, r"\[--\] claude\b.*" + none)         # the fixture PATH holds no claude
            for name in ("python >= 3.9", "git", "orkestra home", "hook self-test", "engine ready", "cursor hooks"):
                self.assertIn(name, cp.stdout, f"{lang}: the row name {name!r} is a stable identifier")
            self.assertNotIn("some required checks FAILED", cp.stdout)

    def test_no_registry_or_a_missing_copy_prints_no_row(self):
        self._install_cursor_hooks()
        for env in (dict(self.doctor_env, CLAUDE_CONFIG_DIR=str(self.tmp / "none")),
                    self._registry(self.tmp / "gone"), self._registry(helpers.PLUGIN)):
            cp = self.cli("doctor", check=False, env=env)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertNotIn("claude plugin copy", cp.stdout)


if __name__ == "__main__":
    unittest.main()
