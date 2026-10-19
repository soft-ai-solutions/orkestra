"""FR-001, FR-002, FR-003, FR-004, FR-007, SC-001: merge Copilot mcp-config.json. No network."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import helpers
from orkestra_core.engines import EngineError
from orkestra_core.mcpinstall import McpError, install_copilot_file

CONTEXT7 = {"type": "http", "url": "https://mcp.context7.com/mcp", "tools": ["*"]}
PLAYWRIGHT = {"type": "stdio", "command": "npx", "args": ["@playwright/mcp@0.0.83"], "tools": ["*"]}
SERENA = {"type": "stdio", "command": "serena", "args": ["start-mcp-server"], "tools": ["*"]}


class TestCopilotMcpFile(helpers.Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.copilot = self.tmp / "copilot-home"
        self.copilot.mkdir()
        self.env_cursor = self.tmp / "env-cursor"
        self.env_cursor.mkdir()
        self.fake_user = self.tmp / "fake-user-home"
        self.fake_user.mkdir()
        self._saved = {
            k: os.environ.get(k) for k in ("PATH", "COPILOT_HOME", "ORKESTRA_CURSOR_HOME", "HOME", "USERPROFILE")
        }
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

    def cfg(self) -> Path:
        return self.copilot / "mcp-config.json"

    def test_fr007_sc001_e6_e8_defaults_are_typed_and_have_no_auth(self) -> None:
        """FR-007, E6, SC-001, FR-001, E8: context7 http, playwright stdio, tools [*], no serena."""
        self.assertFalse(self.cfg().exists())
        rows = install_copilot_file(self.copilot, None)
        doc = json.loads(self.cfg().read_text(encoding="utf-8"))
        self.assertEqual(set(doc), {"mcpServers"})
        self.assertEqual(list(doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], PLAYWRIGHT)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assertNotIn("headers", doc["mcpServers"]["context7"])
        self.assertNotIn("headers", doc["mcpServers"]["playwright"])
        self.assertNotIn("Authorization", self.cfg().read_text(encoding="utf-8"))
        self.assertNotIn("--context", json.dumps(doc))
        self.assertFalse((self.fake_user / ".copilot" / "mcp-config.json").exists())
        raw = self.cfg().read_bytes()
        again = install_copilot_file(self.copilot, None)
        self.assertEqual(self.cfg().read_bytes(), raw)
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "added"},
        ])
        self.assertEqual(again, [
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])

    def test_fr001_e9_only_serena_has_no_context_flag(self) -> None:
        """FR-001, E9: --only serena is stdio serena start-mcp-server, tools [*], no --context."""
        rows = install_copilot_file(self.copilot, ("serena",))
        doc = json.loads(self.cfg().read_text(encoding="utf-8"))
        self.assertEqual(list(doc), ["mcpServers"])
        self.assertEqual(list(doc["mcpServers"]), ["serena"])
        self.assertEqual(doc["mcpServers"]["serena"], SERENA)
        self.assertNotIn("--context", doc["mcpServers"]["serena"]["args"])
        self.assertNotIn("headers", doc["mcpServers"]["serena"])
        self.assertNotIn("Authorization", self.cfg().read_text(encoding="utf-8"))
        self.assertNotIn("context7", doc["mcpServers"])
        self.assertNotIn("playwright", doc["mcpServers"])
        self.assertEqual(rows, [{"host": "copilot", "name": "serena", "state": "added"}])

    def test_fr002_foreign_server_and_key_stay(self) -> None:
        """FR-002: another server and a non-server key both stay; defaults are added."""
        self.cfg().write_text(
            json.dumps({"foreign": {"n": 1}, "mcpServers": {"other": {"command": "stay"}}}) + "\n",
            encoding="utf-8",
        )
        install_copilot_file(self.copilot, None)
        doc = json.loads(self.cfg().read_text(encoding="utf-8"))
        self.assertEqual(list(doc), ["foreign", "mcpServers"])
        self.assertEqual(doc["foreign"], {"n": 1})
        self.assertEqual(list(doc["mcpServers"]), ["other", "context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["other"], {"command": "stay"})
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], PLAYWRIGHT)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assertNotIn("Authorization", self.cfg().read_text(encoding="utf-8"))

    def test_fr004_existing_same_name_object_is_not_rewritten(self) -> None:
        """FR-004: an existing context7 and a local playwright stay byte for byte."""
        raw = (
            b'{"extra":true,"mcpServers":{"context7":{"url":"https://mcp.context7.com/mcp"},'
            b'"playwright":{"command":"local"}}}'
        )
        self.cfg().write_bytes(raw)
        rows = install_copilot_file(self.copilot, None)
        self.assertEqual(self.cfg().read_bytes(), raw)
        self.assertEqual([p.name for p in self.copilot.iterdir()], ["mcp-config.json"])
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])

    def test_fr001_fr004_adds_context7_and_keeps_local_playwright(self) -> None:
        """FR-001, FR-004: a missing name is added; the existing playwright object stays."""
        raw = b'{"extra":true,"mcpServers":{"playwright":{"command":"local"}}}'
        self.cfg().write_bytes(raw)
        rows = install_copilot_file(self.copilot, None)
        self.assertNotEqual(self.cfg().read_bytes(), raw)
        doc = json.loads(self.cfg().read_text(encoding="utf-8"))
        self.assertIs(doc["extra"], True)
        self.assertEqual(list(doc["mcpServers"]), ["playwright", "context7"])
        self.assertEqual(doc["mcpServers"]["playwright"], {"command": "local"})
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assertNotIn("Authorization", self.cfg().read_text(encoding="utf-8"))
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])

    def test_symlink_is_followed_and_other_servers_stay(self) -> None:
        """A mcp-config.json symlink is updated at its target; the link itself stays a link."""
        target = self.tmp / "real-mcp-config.json"
        target.write_text('{"mcpServers":{"other":{"command":"keep"}}}\n', encoding="utf-8")
        self.cfg().symlink_to(target)
        install_copilot_file(self.copilot, None)
        self.assertTrue(self.cfg().is_symlink())
        doc = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(doc["mcpServers"]["other"], {"command": "keep"})
        self.assertEqual(doc["mcpServers"]["context7"], CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], PLAYWRIGHT)

    def test_fr003_invalid_json_raises_and_keeps_bytes(self) -> None:
        """FR-003: [], bad JSON, and a non-object mcpServers do not write."""
        cases = (
            (b"[]", "not_object"),
            (b"{", "bad_json"),
            (b"null", "not_object"),
            (b'{"keep":1,"mcpServers":[]}', "not_object"),
            (b'{"keep":1,"mcpServers":"no"}', "not_object"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.cfg().write_bytes(raw)
                with self.assertRaises(McpError) as caught:
                    install_copilot_file(self.copilot, None)
                self.assertIsInstance(caught.exception, EngineError)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.cfg().read_bytes(), raw)
                self.assertEqual([p.name for p in self.copilot.iterdir()], ["mcp-config.json"])
