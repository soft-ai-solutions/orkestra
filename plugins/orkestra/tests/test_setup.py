"""FR-004 / SC-003 / E5–E7 / E12 — interactive `orkestra setup` wizard."""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import cli as CLI
from orkestra_core import cliparse as CP
from orkestra_core import copilotkit as CopK
from orkestra_core import cursorkit as CurK
from orkestra_core.state import OrkestraError


def _path_without_claude() -> str:
    parts = []
    for d in ("/usr/bin", "/bin", str(Path(sys.executable).parent)):
        if Path(d).is_dir() and d not in parts:
            parts.append(d)
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if d and d not in parts and not (Path(d) / "claude").exists():
            parts.append(d)
    return os.pathsep.join(parts)


class TestSetupWizard(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.chome = Path(os.environ["ORKESTRA_CURSOR_HOME"])
        self.cophome = self.tmp / "copilot-home"
        self.safe_path = _path_without_claude()
        self.assertIsNone(shutil.which("claude", path=self.safe_path))
        self._copilot_home = os.environ.get("COPILOT_HOME")
        self._path = os.environ.get("PATH")
        os.environ["COPILOT_HOME"] = str(self.cophome)
        os.environ["PATH"] = self.safe_path
        self.env = {"PATH": self.safe_path, "COPILOT_HOME": str(self.cophome)}

    def tearDown(self):
        if self._copilot_home is None:
            os.environ.pop("COPILOT_HOME", None)
        else:
            os.environ["COPILOT_HOME"] = self._copilot_home
        if self._path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._path
        super().tearDown()

    def test_handlers_registers_setup(self):
        self.assertIn("setup", CLI.HANDLERS)
        self.assertIs(CLI.HANDLERS["setup"], getattr(CLI, "cmd_setup", None))
        ns = CP.parse(CP.build_parser(), ["setup", "--yes"])
        self.assertEqual(ns.cmd, "setup")
        self.assertTrue(ns.yes)

    def test_yes_copilot_only_installs_copilot_skips_claude(self):
        """E6 / FR-004 / SC-003: detected Copilot-only + --yes → copilot install; no Claude marketplace."""
        self.assertTrue(hasattr(CLI, "cmd_setup"), "cmd_setup missing")
        self.cophome.mkdir(parents=True)
        self.assertFalse(self.chome.is_dir())
        calls = {"claude": 0, "cursor": 0, "copilot": 0}

        def track_claude():
            calls["claude"] += 1

        def track_cursor(**_kw):
            calls["cursor"] += 1
            return ["cursor"]

        def track_copilot(**_kw):
            calls["copilot"] += 1
            return ["copilot"]

        with mock.patch.object(CLI, "_ASKER", None), \
             mock.patch.object(CLI, "_setup_install_claude", create=True, side_effect=track_claude), \
             mock.patch.object(CurK, "install", side_effect=track_cursor), \
             mock.patch.object(CopK, "install", side_effect=track_copilot), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            ns = argparse.Namespace(yes=True)
            code = CLI.cmd_setup(ns)
        self.assertEqual(code, 0)
        self.assertEqual(calls["copilot"], 1)
        self.assertEqual(calls["claude"], 0)
        self.assertEqual(calls["cursor"], 0)

    def test_interactive_decline_skips_engine(self):
        """E7: user answers n → that engine is not installed."""
        self.assertTrue(hasattr(CLI, "cmd_setup"), "cmd_setup missing")
        self.cophome.mkdir(parents=True)
        with mock.patch.object(CLI, "_ASKER", None), \
             mock.patch.object(CLI, "_setup_install_claude", create=True) as claude, \
             mock.patch.object(CurK, "install") as cursor, \
             mock.patch.object(CopK, "install") as copilot, \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            ns = argparse.Namespace(yes=False)
            code = CLI.cmd_setup(ns)
        self.assertEqual(code, 0)
        claude.assert_not_called()
        cursor.assert_not_called()
        copilot.assert_not_called()

    def fake_claude(self, listing="", failing=()):
        """A `claude` on PATH that logs its arguments, prints `listing` for `plugin marketplace list`, and exits 1 for the
        subcommands named in `failing` (for example "plugin install")."""
        bindir = self.tmp / "fakeclaude"
        bindir.mkdir(exist_ok=True)
        self.log = self.tmp / "claude.log"
        self.log.write_text("")
        script = bindir / "claude"
        script.write_text(
            "#!/bin/sh\necho \"$*\" >> '%s'\ncase \"$*\" in\n  \"plugin marketplace list\") printf '%%s\\n' '%s';;\n  --version) echo 9;;\nesac\n"
            "%sexit 0\n" % (self.log, listing.replace("'", ""), "".join(
                'case "$*" in "%s"*) exit 1;; esac\n' % f for f in failing)))
        script.chmod(0o755)
        os.environ["PATH"] = os.pathsep.join([str(bindir), self.safe_path])
        self.assertIsNotNone(shutil.which("claude"))

    def claude_calls(self):
        return [line for line in self.log.read_text().splitlines() if line != "plugin marketplace list"]

    def run_setup(self, lang="en"):
        out = io.StringIO()
        with mock.patch.object(CLI, "_ASKER", None), mock.patch.dict(os.environ, {"ORKESTRA_LANG": lang}), \
                mock.patch("sys.stdout", out):
            code = CLI.cmd_setup(argparse.Namespace(yes=True))
        return code, out.getvalue()

    @unittest.skipIf(os.name == "nt", "#!/bin/sh claude stub")
    def test_a_failing_claude_command_is_a_failed_setup_not_done(self):
        self.fake_claude(failing=("plugin install", "plugin marketplace add"))
        for lang, failed, done in (("en", "FAILED", "done"), ("tr", "BAŞARISIZ", "tamam")):
            with self.subTest(lang=lang):
                code, out = self.run_setup(lang)
                self.assertEqual(code, 1, out)
                self.assertIn(failed, out)
                self.assertNotIn(f"Claude marketplace/plugin {done}", out)
                self.assertIn("claude plugin install orkestra@orkestra-local -s user", out)       # what to run by hand

    @unittest.skipIf(os.name == "nt", "#!/bin/sh claude stub")
    def test_a_claude_that_accepts_every_step_is_done(self):
        self.fake_claude()
        code, out = self.run_setup()
        self.assertEqual(code, 0, out)
        self.assertIn("Claude marketplace/plugin done", out)
        self.assertEqual(self.claude_calls(), ["plugin marketplace add %s" % CLI.PLUGIN_ROOT.parent.parent,
                                               "plugin install orkestra@orkestra-local -s user",
                                               "plugin update orkestra@orkestra-local"])

    @unittest.skipIf(os.name == "nt", "#!/bin/sh claude stub")
    def test_a_marketplace_registered_from_another_checkout_is_registered_again_for_this_one(self):
        root = CLI.PLUGIN_ROOT.parent.parent
        self.fake_claude(listing="orkestra-local\n  Source: Directory (/somewhere/else/orkestra)")
        code, out = self.run_setup()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.claude_calls(), ["plugin marketplace remove orkestra-local",
                                               "plugin marketplace add %s" % root,
                                               "plugin install orkestra@orkestra-local -s user",
                                               "plugin update orkestra@orkestra-local"])
        self.assertIn("Restart Claude Code", out)
        self.fake_claude(listing="orkestra-local\n  Source: Directory (%s)" % root)       # this checkout: just refreshed
        code, out = self.run_setup()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.claude_calls(), ["plugin marketplace update orkestra-local",
                                               "plugin install orkestra@orkestra-local -s user",
                                               "plugin update orkestra@orkestra-local"])

    def test_a_yes_is_a_whole_word_not_a_first_letter(self):
        """`exit`, `error`, `yeah` (English) and `yok`, `yapma` (Turkish: none, don't) must not install anything."""
        def ask(answer, lang, default="y"):
            with mock.patch.dict(os.environ, {"ORKESTRA_LANG": lang}), mock.patch("builtins.input", return_value=answer):
                return CLI._ask_yn("q", default)

        for answer in ("exit", "error", "yeah", "yep", "nope", "n", "no", "yok", "yapma", "hayır", "evetçe"):
            for lang in ("en", "tr"):
                with self.subTest(answer=answer, lang=lang):
                    self.assertFalse(ask(answer, lang))
        for answer in ("y", "Y", "yes", "YES", " yes "):
            for lang in ("en", "tr"):
                with self.subTest(answer=answer, lang=lang):
                    self.assertTrue(ask(answer, lang))
        for answer in ("e", "E", "evet", "Evet", "EVET"):
            with self.subTest(answer=answer):
                self.assertTrue(ask(answer, "tr"))
                self.assertFalse(ask(answer, "en"), "`e` is a yes only at the Turkish prompt [E/h]")
        for lang in ("en", "tr"):                                   # Enter takes the default; no terminal means the default
            self.assertTrue(ask("", lang, "y"))
            self.assertFalse(ask("", lang, "n"))
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(CLI._ask_yn("q", "n"))
            self.assertTrue(CLI._ask_yn("q", "y"))

    def test_asker_refuses(self):
        """E6 / FR-003: setup from an agent shell raises OrkestraError; shim not attempted."""
        self.assertTrue(hasattr(CLI, "cmd_setup"), "cmd_setup missing")
        with mock.patch.object(CLI, "_ASKER", "copilot"), \
             mock.patch.object(CurK, "ensure_path_shim") as shim:
            with self.assertRaises(OrkestraError) as ctx:
                CLI.cmd_setup(argparse.Namespace(yes=True))
        self.assertIn("terminal", str(ctx.exception).lower())
        shim.assert_not_called()

    def test_asker_refuses_via_cli(self):
        self.cophome.mkdir(parents=True)
        cp = self.cli("setup", "--yes", check=False, env={**self.env, "ORKESTRA_ENGINE": "copilot"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("terminal", (cp.stderr + cp.stdout).lower())

    def test_motorsuz_nonzero_no_installs(self):
        """E12 / FR-004 / SC-003: no engines → non-zero, motorsuz message, zero installs."""
        self.assertTrue(hasattr(CLI, "cmd_setup"), "cmd_setup missing")
        self.assertFalse(self.chome.is_dir())
        self.assertFalse(self.cophome.is_dir())
        for flag in ([], ["--yes"]):
            with self.subTest(flag=flag):
                with mock.patch.object(CLI, "_ASKER", None), \
                     mock.patch.object(CLI, "_setup_install_claude", create=True) as claude, \
                     mock.patch.object(CurK, "install") as cursor, \
                     mock.patch.object(CopK, "install") as copilot, \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ns = argparse.Namespace(yes=bool(flag))
                    code = CLI.cmd_setup(ns)
                self.assertNotEqual(code, 0)
                self.assertIn("motorsuz", out.getvalue().lower())
                claude.assert_not_called()
                cursor.assert_not_called()
                copilot.assert_not_called()

    def test_setup_always_ensures_path_shim(self):
        """FR-003 / E5–E6 / SC-001: motorsuz, Copilot-only, Cursor ask n → ensure_path_shim once."""
        self.assertTrue(hasattr(CLI, "cmd_setup"), "cmd_setup missing")

        # E5: motorsuz early exit still attempts shim
        self.assertFalse(self.chome.is_dir())
        self.assertFalse(self.cophome.is_dir())
        with mock.patch.object(CLI, "_ASKER", None), \
             mock.patch.object(CurK, "ensure_path_shim", return_value=[]) as shim, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            code = CLI.cmd_setup(argparse.Namespace(yes=True))
        self.assertNotEqual(code, 0)
        shim.assert_called_once_with()

        # Copilot-only + --yes
        self.cophome.mkdir(parents=True)
        self.assertFalse(self.chome.is_dir())
        with mock.patch.object(CLI, "_ASKER", None), \
             mock.patch.object(CLI, "_setup_install_claude", create=True), \
             mock.patch.object(CurK, "install", return_value=[]), \
             mock.patch.object(CopK, "install", return_value=["copilot"]), \
             mock.patch.object(CurK, "ensure_path_shim", return_value=[]) as shim, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            code = CLI.cmd_setup(argparse.Namespace(yes=True))
        self.assertEqual(code, 0)
        shim.assert_called_once_with()

        # Cursor ask n — shim still once (Cursor install skipped)
        self.chome.mkdir(parents=True)
        with mock.patch.object(CLI, "_ASKER", None), \
             mock.patch.object(CLI, "_setup_install_claude", create=True), \
             mock.patch.object(CurK, "install") as cursor, \
             mock.patch.object(CopK, "install") as copilot, \
             mock.patch.object(CurK, "ensure_path_shim", return_value=[]) as shim, \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            code = CLI.cmd_setup(argparse.Namespace(yes=False))
        self.assertEqual(code, 0)
        cursor.assert_not_called()
        copilot.assert_not_called()
        shim.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
