"""plugincopy: a Claude Code cache copy that runs older code than its local checkout is named at SessionStart."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts lib/ on sys.path, pins the suite's environment)
from helpers import DISPATCH

from orkestra_core import plugincopy as PC


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Layout(unittest.TestCase):
    """<tmp>/checkout (a directory marketplace) and <tmp>/cfg/plugins/cache/mk/orkestra/v1 (Claude Code's copy)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ork-plugincopy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.checkout = self.tmp / "checkout"
        plugin = self.checkout / "plugins" / "orkestra"
        _write(plugin / "hooks" / "hooks.json", "{}")
        _write(plugin / "hooks" / "dispatch.py", "print('v1')\n")
        _write(plugin / "lib" / "orkestra_core" / "guards.py", "RULES = 1\n")
        _write(plugin / "lib" / "orkestra_core" / "messages" / "common.py", "M = {}\n")
        _write(plugin / "skills" / "run" / "SKILL.md", "not code\n")
        _write(self.checkout / ".claude-plugin" / "marketplace.json",
               json.dumps({"name": "mk", "plugins": [{"name": "orkestra", "source": "./plugins/orkestra"}]}))
        self.plugin = plugin
        self.plugins_dir = self.tmp / "cfg" / "plugins"
        self.copy = self.plugins_dir / "cache" / "mk" / "orkestra" / "v1"
        shutil.copytree(plugin, self.copy)
        _write(self.plugins_dir / "known_marketplaces.json",
               json.dumps({"mk": {"source": {"source": "directory", "path": str(self.checkout)},
                                  "installLocation": str(self.checkout)}}))
        self.register([{"scope": "user", "installPath": str(self.copy), "version": "v1"}])

    def register(self, entries):
        _write(self.plugins_dir / "installed_plugins.json", json.dumps({"version": 2, "plugins": {"orkestra@mk": entries}}))


class TestStaleNote(Layout):
    def test_an_identical_copy_says_nothing(self):
        self.assertEqual(PC.checkout_of(self.copy), Path(os.path.realpath(self.plugin)))
        self.assertTrue(PC.same_code(self.copy, self.plugin))
        self.assertEqual(PC.stale_note(self.copy), "")

    def test_a_changed_guard_names_the_copy_the_checkout_and_the_repair_line(self):
        _write(self.plugin / "lib" / "orkestra_core" / "guards.py", "RULES = 2\n")      # same size, other bytes
        note = PC.stale_note(self.copy)
        self.assertIn("OLDER copy", note)
        self.assertIn(str(self.copy), note)
        self.assertIn(os.path.realpath(self.plugin), note)
        self.assertIn("`claude plugin marketplace update mk && claude plugin update orkestra@mk`", note)

    def test_an_added_or_removed_code_file_counts_and_a_non_code_file_does_not(self):
        _write(self.plugin / "skills" / "run" / "SKILL.md", "edited, still not code\n")
        self.assertEqual(PC.stale_note(self.copy), "")
        _write(self.plugin / "lib" / "orkestra_core" / "plugincopy.py", "x = 1\n")
        self.assertNotEqual(PC.stale_note(self.copy), "")
        (self.plugin / "lib" / "orkestra_core" / "plugincopy.py").unlink()
        (self.plugin / "hooks" / "dispatch.py").unlink()
        self.assertNotEqual(PC.stale_note(self.copy), "")

    def test_every_registered_scope_gets_its_update_line_user_first(self):
        _write(self.plugin / "hooks" / "dispatch.py", "print('v2')\n")
        self.register([{"scope": "project", "installPath": str(self.copy), "projectPath": "/home/u"},
                       {"scope": "user", "installPath": str(self.copy)},
                       {"scope": "user", "installPath": str(self.copy)},
                       {"scope": "local", "installPath": str(self.tmp / "elsewhere")}])
        note = PC.stale_note(self.copy)
        self.assertIn("claude plugin marketplace update mk && claude plugin update orkestra@mk && "
                      '(cd "/home/u" && claude plugin update orkestra@mk -s project)`', note)
        self.assertEqual(note.count("claude plugin update orkestra@mk`"), 0)        # the user line is not repeated last
        self.assertNotIn("-s local", note)

    def test_not_a_cache_copy_a_remote_marketplace_or_a_broken_registry_says_nothing(self):
        _write(self.plugin / "hooks" / "dispatch.py", "print('v2')\n")
        self.assertEqual(PC.stale_note(self.plugin), "")                           # the checkout itself
        self.assertIsNone(PC.checkout_of(Path("/")))
        for reg in ('{"mk": {"source": {"source": "github", "repo": "o/r"}}}', "not json", "[]", '{"mk": 1}',
                    '{"mk": {"source": {"source": "directory", "path": "/nonexistent/orkestra"}}}'):
            with self.subTest(registry=reg):
                _write(self.plugins_dir / "known_marketplaces.json", reg)
                self.assertEqual(PC.stale_note(self.copy), "")
        (self.plugins_dir / "known_marketplaces.json").unlink()
        self.assertEqual(PC.stale_note(self.copy), "")

    def test_a_marketplace_manifest_without_the_plugin_falls_back_to_plugins_dir(self):
        _write(self.checkout / ".claude-plugin" / "marketplace.json", "{broken")
        self.assertEqual(PC.checkout_of(self.copy), Path(os.path.realpath(self.plugin)))


class TestDoctorCopies(Layout):
    """`orkestra doctor` lists each cached copy once, however many install scopes point at it."""

    def test_two_scopes_on_one_copy_are_one_line_and_its_code_is_compared(self):
        from unittest import mock
        from orkestra_core import cli
        self.register([{"scope": "user", "installPath": str(self.copy), "version": "v1"},
                       {"scope": "project", "installPath": str(self.copy), "version": "v1", "projectPath": "/home/u"}])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "cfg")}), \
                mock.patch.object(cli, "PLUGIN_ROOT", self.plugin):
            self.assertEqual(cli._claude_copies(), [(str(self.copy), "v1", True)])
            _write(self.plugin / "hooks" / "dispatch.py", "print('v2')\n")
            self.assertEqual(cli._claude_copies(), [(str(self.copy), "v1", False)])


class TestSessionStartHook(Layout):
    """The real hook process, started from the cache copy the way Claude Code starts it."""

    def setUp(self):
        super().setUp()
        real = Path(DISPATCH).resolve().parent.parent
        shutil.rmtree(self.copy)
        shutil.copytree(real, self.copy, ignore=shutil.ignore_patterns("tests", "cursor", "__pycache__", "*.pyc"))
        shutil.rmtree(self.plugin)
        shutil.copytree(self.copy, self.plugin)
        self.home = self.tmp / "home"
        self.env = dict(os.environ, HOME=str(self.home), ORKESTRA_HOME=str(self.home / ".orkestra"),
                        ORKESTRA_WORKSPACE=str(self.tmp / "ws"), ORKESTRA_LANG="en", ORKESTRA_NO_DAEMON="1")
        (self.tmp / "ws").mkdir()

    def start(self) -> str:
        ev = {"hook_event_name": "SessionStart", "session_id": "s-plugincopy", "source": "startup",
              "cwd": str(self.tmp / "ws")}
        cp = subprocess.run([sys.executable, str(self.copy / "hooks" / "dispatch.py"), "SessionStart"],
                            input=json.dumps(ev), capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr[-600:])
        return cp.stdout

    def test_a_current_copy_adds_no_context(self):
        self.assertEqual(self.start().strip(), "")

    def test_an_older_copy_adds_one_well_formed_context_block(self):
        guards = self.plugin / "lib" / "orkestra_core" / "guards.py"
        guards.write_text(guards.read_text(encoding="utf-8") + "\n# newer\n", encoding="utf-8")
        out = json.loads(self.start())
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("OLDER copy of the orkestra plugin", ctx["additionalContext"])
        self.assertIn("claude plugin update orkestra@mk", ctx["additionalContext"])


if __name__ == "__main__":
    unittest.main()
