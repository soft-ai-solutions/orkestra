"""FR-001, FR-002, FR-003, FR-004, FR-008: merge Cursor mcp.json. No network."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import helpers
from orkestra_core.engines import EngineError
from orkestra_core.mcpinstall import McpError, install_cursor

CONTEXT7 = {"url": "https://mcp.context7.com/mcp"}
PLAYWRIGHT = {"command": "npx", "args": ["@playwright/mcp@0.0.83"]}
SERENA = {"command": "serena", "args": ["start-mcp-server"]}


class TestCursorMcp(helpers.Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cursor = self.tmp / "cursor-home"
        self.cursor.mkdir()
        self.env_cursor = self.tmp / "env-cursor"
        self.env_cursor.mkdir()
        self.copilot = self.tmp / "copilot-home"
        self.fake_user = self.tmp / "fake-user-home"
        self.fake_user.mkdir()
        self._saved = {k: os.environ.get(k) for k in ("PATH", "COPILOT_HOME", "HOME", "USERPROFILE")}
        # Temp bin only: never prepend, never keep the machine PATH.
        os.environ["PATH"] = str(self.bin)
        os.environ["COPILOT_HOME"] = str(self.copilot)
        os.environ["ORKESTRA_CURSOR_HOME"] = str(self.env_cursor)
        os.environ["HOME"] = str(self.fake_user)
        os.environ["USERPROFILE"] = str(self.fake_user)
        self.assertEqual(os.environ["PATH"].split(os.pathsep), [str(self.bin)])
        for tool in ("npx", "claude", "copilot", "cursor", "serena"):
            self.assertIsNone(shutil.which(tool), tool)

    def tearDown(self) -> None:
        for key, value in getattr(self, "_saved", {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def mcp(self) -> Path:
        return self.cursor / "mcp.json"

    def test_fr001_fr008_e1_e8_empty_home_exact_defaults(self) -> None:
        """FR-001, FR-008, E1, E8: defaults are context7 and playwright, never serena."""
        self.assertFalse(self.mcp().exists())
        rows = install_cursor(self.cursor)
        doc = json.loads(self.mcp().read_text(encoding="utf-8"))
        self.assertEqual(set(doc), {"mcpServers"})
        self.assertEqual(list(doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], PLAYWRIGHT)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assertNotIn("type", doc["mcpServers"]["context7"])
        self.assertNotIn("headers", doc["mcpServers"]["context7"])
        self.assertNotIn("type", doc["mcpServers"]["playwright"])
        self.assertNotIn("headers", doc["mcpServers"]["playwright"])
        text = self.mcp().read_text(encoding="utf-8")
        self.assertNotIn("Authorization", text)
        self.assertNotIn("headers", text)
        self.assertNotIn('"type"', text)
        self.assertFalse((self.env_cursor / "mcp.json").exists())
        self.assertFalse((self.fake_user / ".cursor" / "mcp.json").exists())
        raw = self.mcp().read_bytes()
        again = install_cursor(self.cursor)
        self.assertEqual(self.mcp().read_bytes(), raw)
        self.assertEqual(rows, [
            {"host": "cursor", "name": "context7", "state": "added"},
            {"host": "cursor", "name": "playwright", "state": "added"},
        ])
        self.assertEqual(again, [
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "present"},
        ])

    def test_fr002_sc002_e2_keeps_other_server_and_foreign_key(self) -> None:
        """FR-002, SC-002, E2: another server and a non-server key both stay."""
        self.mcp().write_text(
            json.dumps({"foreign": {"n": 1}, "mcpServers": {"other": {"command": "stay"}}}) + "\n",
            encoding="utf-8",
        )
        install_cursor(self.cursor)
        doc = json.loads(self.mcp().read_text(encoding="utf-8"))
        self.assertEqual(list(doc), ["foreign", "mcpServers"])
        self.assertEqual(doc["foreign"], {"n": 1})
        self.assertEqual(list(doc["mcpServers"]), ["other", "context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["other"], {"command": "stay"})
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], PLAYWRIGHT)
        self.assertNotIn("serena", doc["mcpServers"])

    def test_fr004_e3_existing_names_are_not_rewritten(self) -> None:
        """FR-004, E3: canonical context7 and a local playwright are left byte for byte."""
        raw = (
            b'{"extra":true,"mcpServers":{"context7":{"url":"https://mcp.context7.com/mcp"},'
            b'"playwright":{"command":"local"}}}'
        )
        self.mcp().write_bytes(raw)
        rows = install_cursor(self.cursor)
        self.assertEqual(self.mcp().read_bytes(), raw)
        self.assertEqual([p.name for p in self.cursor.iterdir()], ["mcp.json"])
        self.assertEqual(rows, [
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "present"},
        ])

    def test_fr001_fr004_e3_adds_context7_and_keeps_local_playwright(self) -> None:
        """FR-001, FR-004, E3: a missing name is added; the existing playwright object stays."""
        raw = b'{"extra":true,"mcpServers":{"playwright":{"command":"local"}}}'
        self.mcp().write_bytes(raw)
        rows = install_cursor(self.cursor)
        self.assertNotEqual(self.mcp().read_bytes(), raw)
        doc = json.loads(self.mcp().read_text(encoding="utf-8"))
        self.assertIs(doc["extra"], True)
        self.assertEqual(list(doc["mcpServers"]), ["playwright", "context7"])
        self.assertEqual(doc["mcpServers"]["playwright"], {"command": "local"})
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assertNotIn("type", doc["mcpServers"]["context7"])
        self.assertNotIn("headers", doc["mcpServers"]["context7"])
        self.assertNotIn("Authorization", self.mcp().read_text(encoding="utf-8"))
        self.assertEqual(rows, [
            {"host": "cursor", "name": "context7", "state": "added"},
            {"host": "cursor", "name": "playwright", "state": "present"},
        ])

    def test_fr001_e9_only_serena_is_stdio_without_context(self) -> None:
        """FR-001, E9: --only serena is command serena, args start-mcp-server, no --context."""
        rows = install_cursor(self.cursor, ("serena",))
        doc = json.loads(self.mcp().read_text(encoding="utf-8"))
        self.assertEqual(list(doc), ["mcpServers"])
        self.assertEqual(list(doc["mcpServers"]), ["serena"])
        self.assertEqual(doc["mcpServers"]["serena"], SERENA)
        self.assertNotIn("--context", doc["mcpServers"]["serena"]["args"])
        self.assertNotIn("type", doc["mcpServers"]["serena"])
        self.assertNotIn("headers", doc["mcpServers"]["serena"])
        self.assertNotIn("Authorization", self.mcp().read_text(encoding="utf-8"))
        self.assertNotIn("context7", doc["mcpServers"])
        self.assertNotIn("playwright", doc["mcpServers"])
        self.assertEqual(rows, [{"host": "cursor", "name": "serena", "state": "added"}])

    def test_symlink_is_followed_and_other_servers_stay(self) -> None:
        """A mcp.json symlink is updated at its target; the link itself stays a link."""
        target = self.tmp / "real-mcp.json"
        target.write_text('{"mcpServers":{"other":{"command":"keep"}}}\n', encoding="utf-8")
        self.mcp().symlink_to(target)
        install_cursor(self.cursor)
        self.assertTrue(self.mcp().is_symlink())
        doc = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(doc["mcpServers"]["other"], {"command": "keep"})
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], PLAYWRIGHT)

    def test_fr003_sc003_e4_invalid_json_raises_and_keeps_bytes(self) -> None:
        """FR-003, SC-003, E4: [] , bad JSON, and a non-object mcpServers do not write."""
        cases = (
            (b"[]", "not_object"),
            (b"{", "bad_json"),
            (b'{"keep":1,"mcpServers":[]}', "not_object"),
            (b'{"keep":1,"mcpServers":"no"}', "not_object"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.mcp().write_bytes(raw)
                with self.assertRaises(McpError) as caught:
                    install_cursor(self.cursor)
                self.assertIsInstance(caught.exception, EngineError)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.mcp().read_bytes(), raw)
                self.assertEqual([p.name for p in self.cursor.iterdir()], ["mcp.json"])
