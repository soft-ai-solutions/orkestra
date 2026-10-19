"""FR-004, FR-005, FR-006, E5, E10: claude and copilot binaries. No network. No real CLI."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import helpers
from orkestra_core.mcpinstall import McpError, install_claude, install_copilot

COPILOT_CONTEXT7 = ["mcp", "add", "--transport", "http", "context7", "https://mcp.context7.com/mcp"]
COPILOT_PLAYWRIGHT = ["mcp", "add", "playwright", "--", "npx", "@playwright/mcp@0.0.83"]
COPILOT_SERENA = ["mcp", "add", "serena", "--", "serena", "start-mcp-server"]
CLAUDE_CONTEXT7 = ["mcp", "add", "--transport", "http", "context7", "https://mcp.context7.com/mcp", "-s", "user"]
CLAUDE_PLAYWRIGHT = ["mcp", "add", "playwright", "-s", "user", "--", "npx", "@playwright/mcp@0.0.83"]
CLAUDE_SERENA = [
    "mcp", "add", "--scope", "user", "serena", "--", "serena", "start-mcp-server",
    "--context", "claude-code", "--project-from-cwd",
]


class TestMcpExec(helpers.Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.copilot = self.tmp / "copilot-home"
        self.copilot.mkdir()
        self.cursor = self.tmp / "cursor-home"
        self.cursor.mkdir()
        self.fake_user = self.tmp / "fake-user-home"
        self.fake_user.mkdir()
        self.log = self.tmp / "argv.log"
        self.npx_mark = self.tmp / "npx-ran"
        self._saved = {
            k: os.environ.get(k) for k in ("PATH", "COPILOT_HOME", "ORKESTRA_CURSOR_HOME", "HOME", "USERPROFILE")
        }
        # Temp bin only: never prepend, never keep the machine PATH.
        os.environ["PATH"] = str(self.bin)
        os.environ["COPILOT_HOME"] = str(self.copilot)
        os.environ["ORKESTRA_CURSOR_HOME"] = str(self.cursor)
        os.environ["HOME"] = str(self.fake_user)
        os.environ["USERPROFILE"] = str(self.fake_user)
        self.plant_npx()
        self.assertEqual(os.environ["PATH"].split(os.pathsep), [str(self.bin)])
        for tool in ("claude", "copilot", "cursor", "serena"):
            self.assertIsNone(shutil.which(tool), tool)
        npx = shutil.which("npx")
        self.assertIsNotNone(npx)
        self.assertEqual(Path(npx).resolve().parent, self.bin.resolve())

    def tearDown(self) -> None:
        for key, value in getattr(self, "_saved", {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def plant_npx(self) -> None:
        mark = shlex.quote(str(self.npx_mark))
        self._write_exec(self.bin / "npx", f"#!/bin/sh\nprintf ran >> {mark}\n")

    def plant(self, name: str, *, list_text: str = "", fail: str = "") -> None:
        """Executable stub. Appends each argv JSON array to the log. ``fail`` is ``all`` or empty."""
        script = "\n".join([
            f"#!{sys.executable}",
            "import json, sys",
            f"log = {str(self.log)!r}",
            "with open(log, 'a', encoding='utf-8') as fh:",
            "    fh.write(json.dumps(sys.argv) + '\\n')",
            "args = sys.argv[1:]",
            f"mode = {fail!r}",
            "if mode != 'all' and args[:2] == ['mcp', 'list']:",
            f"    sys.stdout.write({list_text!r})",
            "    raise SystemExit(0)",
            "raise SystemExit(1 if mode else 0)",
            "",
        ])
        self._write_exec(self.bin / name, script)

    def _write_exec(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def invocations(self) -> list:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def adds(self) -> list:
        return [row[1:] for row in self.invocations() if row[1:3] == ["mcp", "add"]]

    def assert_path_untouched(self) -> None:
        self.assertEqual(os.environ["PATH"], str(self.bin))
        self.assertFalse(self.npx_mark.exists())
        self.assertIsNone(shutil.which("serena"))
        self.assertIsNone(shutil.which("cursor"))

    def assert_no_config_files(self) -> None:
        self.assertFalse((self.copilot / "mcp-config.json").exists())
        self.assertFalse((self.cursor / "mcp.json").exists())
        self.assertFalse((self.fake_user / ".copilot" / "mcp-config.json").exists())
        self.assertFalse((self.fake_user / ".cursor" / "mcp.json").exists())

    def assert_used_which(self, tool: str) -> None:
        found = shutil.which(tool)
        self.assertIsNotNone(found)
        self.assertEqual(Path(found).resolve().parent, self.bin.resolve())
        rows = self.invocations()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row[0], found)

    def test_fr006_e5_copilot_defaults_log_argv_and_write_no_file(self) -> None:
        """FR-006, E5: two copilot mcp add calls, and Python writes no mcp-config.json."""
        self.plant("copilot")
        rows = install_copilot(None)
        self.assertEqual(self.adds(), [COPILOT_CONTEXT7, COPILOT_PLAYWRIGHT])
        self.assert_used_which("copilot")
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "added"},
        ])
        self.assert_no_config_files()
        self.assert_path_untouched()
        self.assertNotIn("serena", json.dumps(self.invocations()))
        self.assertNotIn("--context", json.dumps(self.adds()))

    def test_fr004_copilot_skips_a_listed_playwright_word(self) -> None:
        """FR-004: copilot mcp list already names playwright, so that add is skipped."""
        self.plant("copilot", list_text="playwright")
        rows = install_copilot(None)
        self.assertEqual(self.adds(), [COPILOT_CONTEXT7])
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])
        self.assert_no_config_files()
        self.assert_path_untouched()

    def test_fr004_copilot_ignores_a_name_that_is_not_a_word(self) -> None:
        """FR-004: the list must contain the server name as a word."""
        self.plant("copilot", list_text="notplaywright")
        rows = install_copilot(None)
        self.assertEqual(self.adds(), [COPILOT_CONTEXT7, COPILOT_PLAYWRIGHT])
        self.assertEqual(rows[1]["state"], "added")
        self.assert_no_config_files()

    def test_fr006_copilot_nonzero_raises_and_writes_nothing(self) -> None:
        """FR-006: a non-zero copilot exits command_failed and does not write or fall through."""
        target = self.copilot / "mcp-config.json"
        target.write_bytes(b"[]")
        before = target.read_bytes()
        self.plant("copilot", fail="all")
        with self.assertRaises(McpError) as caught:
            install_copilot(None)
        self.assertEqual(caught.exception.code, "command_failed")
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.fake_user / ".copilot" / "mcp-config.json").exists())
        self.assertEqual(self.adds(), [])
        self.assert_path_untouched()

    def test_fr005_e10_claude_skips_listed_playwright(self) -> None:
        """FR-005, E10: claude mcp list prints playwright, so only the context7 argv runs."""
        self.plant("claude", list_text="playwright")
        rows = install_claude(None)
        self.assertEqual(self.adds(), [CLAUDE_CONTEXT7])
        self.assert_used_which("claude")
        self.assertNotIn(CLAUDE_PLAYWRIGHT, self.adds())
        self.assertEqual(rows, [
            {"host": "claude", "name": "context7", "state": "added"},
            {"host": "claude", "name": "playwright", "state": "present"},
        ])
        self.assert_no_config_files()
        self.assert_path_untouched()

    def test_fr005_claude_serena_argv_includes_context(self) -> None:
        """FR-005: named serena uses the install.sh argv, including --context claude-code."""
        self.plant("claude")
        rows = install_claude(("serena",))
        self.assertEqual(self.adds(), [CLAUDE_SERENA])
        self.assertIn("--context", CLAUDE_SERENA)
        self.assertIn("claude-code", CLAUDE_SERENA)
        self.assertEqual(rows, [{"host": "claude", "name": "serena", "state": "added"}])
        self.assertFalse(self.npx_mark.exists())
        self.assert_no_config_files()

    def test_copilot_serena_argv_has_no_context(self) -> None:
        """Named serena on the copilot binary has no --context."""
        self.plant("copilot")
        rows = install_copilot(("serena",))
        self.assertEqual(self.adds(), [COPILOT_SERENA])
        self.assertNotIn("--context", COPILOT_SERENA)
        self.assertEqual(rows, [{"host": "copilot", "name": "serena", "state": "added"}])
        self.assert_no_config_files()
        self.assert_path_untouched()
