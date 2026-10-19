"""FR-001, FR-003, FR-007, FR-009, FR-010: host selection and status. No network."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import helpers
from orkestra_core import i18n
from orkestra_core.mcpinstall import McpError, install, status

CURSOR_CONTEXT7 = {"url": "https://mcp.context7.com/mcp"}
CURSOR_PLAYWRIGHT = {"command": "npx", "args": ["@playwright/mcp@0.0.83"]}
CURSOR_SERENA = {"command": "serena", "args": ["start-mcp-server"]}
COPILOT_CONTEXT7 = {"type": "http", "url": "https://mcp.context7.com/mcp", "tools": ["*"]}
COPILOT_PLAYWRIGHT = {"type": "stdio", "command": "npx", "args": ["@playwright/mcp@0.0.83"], "tools": ["*"]}
COPILOT_SERENA = {"type": "stdio", "command": "serena", "args": ["start-mcp-server"], "tools": ["*"]}
CLAUDE_CONTEXT7 = ["mcp", "add", "--transport", "http", "context7", "https://mcp.context7.com/mcp", "-s", "user"]
CLAUDE_PLAYWRIGHT = ["mcp", "add", "playwright", "-s", "user", "--", "npx", "@playwright/mcp@0.0.83"]
COPILOT_ADD_CONTEXT7 = ["mcp", "add", "--transport", "http", "context7", "https://mcp.context7.com/mcp"]
COPILOT_ADD_PLAYWRIGHT = ["mcp", "add", "playwright", "--", "npx", "@playwright/mcp@0.0.83"]
ADDED = (
    {"host": "cursor", "name": "context7", "state": "added"},
    {"host": "cursor", "name": "playwright", "state": "added"},
    {"host": "copilot", "name": "context7", "state": "added"},
    {"host": "copilot", "name": "playwright", "state": "added"},
)
ABSENT = (
    {"host": "claude", "name": "context7", "state": "absent"},
    {"host": "claude", "name": "playwright", "state": "absent"},
    {"host": "cursor", "name": "context7", "state": "absent"},
    {"host": "cursor", "name": "playwright", "state": "absent"},
    {"host": "copilot", "name": "context7", "state": "absent"},
    {"host": "copilot", "name": "playwright", "state": "absent"},
)


class TestMcpHosts(helpers.Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cursor = self.tmp / "cursor-home"
        self.copilot = self.tmp / "copilot-home"
        self.fake_user = self.tmp / "fake-user-home"
        self.fake_user.mkdir()
        self.log = self.tmp / "argv.log"
        self._saved = {
            k: os.environ.get(k) for k in ("PATH", "COPILOT_HOME", "ORKESTRA_CURSOR_HOME", "HOME", "USERPROFILE")
        }
        # Temp bin only: never prepend, never keep the machine PATH.
        os.environ["PATH"] = str(self.bin)
        os.environ["COPILOT_HOME"] = str(self.copilot)
        os.environ["ORKESTRA_CURSOR_HOME"] = str(self.cursor)
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

    def plant(self, name: str, *, list_text: str = "", exit_code: int = 0) -> None:
        """Executable stub. Appends each argv JSON array to the log.

        ``mcp list`` prints ``list_text`` and exits ``exit_code`` (default 0). Any other argv exits 0.
        """
        script = "\n".join([
            f"#!{sys.executable}",
            "import json, sys",
            f"log = {str(self.log)!r}",
            "with open(log, 'a', encoding='utf-8') as fh:",
            "    fh.write(json.dumps(sys.argv) + '\\n')",
            "args = sys.argv[1:]",
            "if args[:2] == ['mcp', 'list']:",
            f"    sys.stdout.write({list_text!r})",
            f"    raise SystemExit({int(exit_code)})",
            "raise SystemExit(0)",
            "",
        ])
        path = self.bin / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    def invocations(self) -> list:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def adds_for(self, tool: str) -> list:
        found = shutil.which(tool)
        self.assertIsNotNone(found)
        binary = Path(found).resolve()
        rows = []
        for row in self.invocations():
            if row[1:3] != ["mcp", "add"]:
                continue
            if Path(row[0]).resolve() == binary:
                rows.append(row[1:])
        return rows

    def assert_no_auth(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("Authorization", text)
        self.assertNotIn("headers", text)

    def assert_real_homes_untouched(self) -> None:
        self.assertFalse((self.fake_user / ".cursor").exists())
        self.assertFalse((self.fake_user / ".copilot").exists())
        self.assertIsNone(shutil.which("npx"))

    def test_fr001_sc001_e1_e8_all_empty_homes_installs_defaults(self) -> None:
        """FR-001, SC-001, E1, E8: empty homes get context7 and playwright only, no Authorization."""
        self.cursor.mkdir()
        self.copilot.mkdir()
        rows = install("all")
        cursor_doc = json.loads((self.cursor / "mcp.json").read_text(encoding="utf-8"))
        copilot_doc = json.loads((self.copilot / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(set(cursor_doc), {"mcpServers"})
        self.assertEqual(list(cursor_doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(cursor_doc["mcpServers"]["context7"], CURSOR_CONTEXT7)
        self.assertEqual(cursor_doc["mcpServers"]["playwright"], CURSOR_PLAYWRIGHT)
        self.assertNotIn("serena", cursor_doc["mcpServers"])
        self.assertNotIn("type", cursor_doc["mcpServers"]["context7"])
        self.assertNotIn("type", cursor_doc["mcpServers"]["playwright"])
        self.assertEqual(set(copilot_doc), {"mcpServers"})
        self.assertEqual(list(copilot_doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(copilot_doc["mcpServers"]["context7"], COPILOT_CONTEXT7)
        self.assertEqual(copilot_doc["mcpServers"]["playwright"], COPILOT_PLAYWRIGHT)
        self.assertNotIn("serena", copilot_doc["mcpServers"])
        self.assert_no_auth(self.cursor / "mcp.json")
        self.assert_no_auth(self.copilot / "mcp-config.json")
        self.assertNotIn('"type"', (self.cursor / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(rows, list(ADDED))
        raw_cursor = (self.cursor / "mcp.json").read_bytes()
        raw_copilot = (self.copilot / "mcp-config.json").read_bytes()
        again = install("all")
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw_cursor)
        self.assertEqual((self.copilot / "mcp-config.json").read_bytes(), raw_copilot)
        self.assertEqual(again, [
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "present"},
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])
        self.assert_real_homes_untouched()

    def test_fr009_all_with_nothing_present_creates_nothing(self) -> None:
        """FR-009: all with no home and no binary creates nothing and does not raise."""
        rows = install("all")
        self.assertEqual(rows, [])
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.copilot.exists())
        reported = status("all")
        self.assertEqual(reported, list(ABSENT))
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.copilot.exists())
        self.assert_real_homes_untouched()

    def test_fr009_all_skips_absent_cursor_and_installs_copilot(self) -> None:
        """FR-009: all installs the hosts that are present and skips the rest."""
        self.copilot.mkdir()
        rows = install("all")
        self.assertFalse(self.cursor.exists())
        doc = json.loads((self.copilot / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(list(doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["context7"], COPILOT_CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], COPILOT_PLAYWRIGHT)
        self.assert_no_auth(self.copilot / "mcp-config.json")
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "added"},
        ])
        self.assert_real_homes_untouched()

    def test_fr009_e7_explicit_cursor_missing_raises(self) -> None:
        """FR-009, E7: explicit cursor with no home and no binary raises host_missing and writes nothing."""
        with self.assertRaises(McpError) as caught:
            install("cursor")
        self.assertEqual(caught.exception.code, "host_missing")
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.copilot.exists())
        self.assert_real_homes_untouched()

    def test_fr009_explicit_claude_missing_raises(self) -> None:
        """FR-009: explicit claude with no binary raises host_missing and writes nothing."""
        self.cursor.mkdir()
        self.copilot.mkdir()
        with self.assertRaises(McpError) as caught:
            install("claude")
        self.assertEqual(caught.exception.code, "host_missing")
        self.assertFalse((self.cursor / "mcp.json").exists())
        self.assertFalse((self.copilot / "mcp-config.json").exists())

    def test_explicit_cursor_binary_without_home_creates_nothing(self) -> None:
        """Cursor on PATH with no home writes mcp.json (context7 and playwright) and does not spawn cursor."""
        self.plant("cursor", list_text="context7\n")
        rows = install("cursor")
        path = self.cursor / "mcp.json"
        self.assertTrue(path.is_file())
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["context7"], CURSOR_CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], CURSOR_PLAYWRIGHT)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assert_no_auth(path)
        self.assertEqual(rows, [
            {"host": "cursor", "name": "context7", "state": "added"},
            {"host": "cursor", "name": "playwright", "state": "added"},
        ])
        self.assertEqual(self.invocations(), [])
        self.assertFalse(self.copilot.exists())
        self.assert_real_homes_untouched()

    def test_all_cursor_binary_and_copilot_home_write_defaults(self) -> None:
        """all: a cursor binary and an empty copilot home both get the two default servers. cursor is not spawned."""
        self.plant("cursor")
        self.copilot.mkdir()
        self.assertIsNone(shutil.which("copilot"))
        rows = install("all")
        cursor_path = self.cursor / "mcp.json"
        copilot_path = self.copilot / "mcp-config.json"
        self.assertTrue(cursor_path.is_file())
        self.assertTrue(copilot_path.is_file())
        cursor_doc = json.loads(cursor_path.read_text(encoding="utf-8"))
        copilot_doc = json.loads(copilot_path.read_text(encoding="utf-8"))
        self.assertEqual(list(cursor_doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(cursor_doc["mcpServers"]["context7"], CURSOR_CONTEXT7)
        self.assertEqual(cursor_doc["mcpServers"]["playwright"], CURSOR_PLAYWRIGHT)
        self.assertEqual(list(copilot_doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(copilot_doc["mcpServers"]["context7"], COPILOT_CONTEXT7)
        self.assertEqual(copilot_doc["mcpServers"]["playwright"], COPILOT_PLAYWRIGHT)
        self.assert_no_auth(cursor_path)
        self.assert_no_auth(copilot_path)
        cursor_bin = Path(shutil.which("cursor")).resolve()
        spawned = [Path(row[0]).resolve() for row in self.invocations()]
        self.assertNotIn(cursor_bin, spawned)
        self.assertEqual(rows, list(ADDED))
        self.assert_real_homes_untouched()

    def test_fr007_explicit_copilot_without_binary_creates_file(self) -> None:
        """FR-007: explicit copilot with an empty disk and no binary still merges mcp-config.json."""
        self.assertFalse(self.copilot.exists())
        rows = install("copilot")
        doc = json.loads((self.copilot / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(list(doc["mcpServers"]), ["context7", "playwright"])
        self.assertEqual(doc["mcpServers"]["context7"], COPILOT_CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], COPILOT_PLAYWRIGHT)
        self.assertNotIn("serena", doc["mcpServers"])
        self.assert_no_auth(self.copilot / "mcp-config.json")
        self.assertFalse(self.cursor.exists())
        self.assertEqual(rows, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "added"},
        ])
        self.assert_real_homes_untouched()

    def test_fr001_names_serena_replaces_defaults(self) -> None:
        """FR-001: a names sequence replaces context7 and playwright. Serena has no --context."""
        self.cursor.mkdir()
        self.copilot.mkdir()
        rows = install("all", ("serena",))
        cursor_doc = json.loads((self.cursor / "mcp.json").read_text(encoding="utf-8"))
        copilot_doc = json.loads((self.copilot / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(list(cursor_doc["mcpServers"]), ["serena"])
        self.assertEqual(cursor_doc["mcpServers"]["serena"], CURSOR_SERENA)
        self.assertEqual(list(copilot_doc["mcpServers"]), ["serena"])
        self.assertEqual(copilot_doc["mcpServers"]["serena"], COPILOT_SERENA)
        self.assertNotIn("--context", json.dumps(cursor_doc))
        self.assertNotIn("--context", json.dumps(copilot_doc))
        self.assert_no_auth(self.cursor / "mcp.json")
        self.assert_no_auth(self.copilot / "mcp-config.json")
        self.assertEqual(rows, [
            {"host": "cursor", "name": "serena", "state": "added"},
            {"host": "copilot", "name": "serena", "state": "added"},
        ])

    def test_binaries_on_path_do_not_fall_through_to_a_file(self) -> None:
        """Claude and Copilot binaries are used as-is. Copilot's file is not also written."""
        self.plant("claude")
        self.plant("copilot")
        self.cursor.mkdir()
        self.copilot.mkdir()
        rows = install("all")
        self.assertEqual(self.adds_for("claude"), [CLAUDE_CONTEXT7, CLAUDE_PLAYWRIGHT])
        self.assertEqual(self.adds_for("copilot"), [COPILOT_ADD_CONTEXT7, COPILOT_ADD_PLAYWRIGHT])
        self.assertFalse((self.copilot / "mcp-config.json").exists())
        cursor_doc = json.loads((self.cursor / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor_doc["mcpServers"]["context7"], CURSOR_CONTEXT7)
        self.assertEqual(cursor_doc["mcpServers"]["playwright"], CURSOR_PLAYWRIGHT)
        self.assertNotIn("serena", cursor_doc["mcpServers"])
        self.assert_no_auth(self.cursor / "mcp.json")
        for row in self.invocations():
            self.assertNotIn("serena", row[1:])
        self.assertEqual(rows, [
            {"host": "claude", "name": "context7", "state": "added"},
            {"host": "claude", "name": "playwright", "state": "added"},
            {"host": "cursor", "name": "context7", "state": "added"},
            {"host": "cursor", "name": "playwright", "state": "added"},
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "added"},
        ])
        self.log.unlink()
        self.copilot.rmdir()
        explicit = install("copilot")
        self.assertEqual(self.adds_for("copilot"), [COPILOT_ADD_CONTEXT7, COPILOT_ADD_PLAYWRIGHT])
        self.assertFalse(self.copilot.exists())
        self.assertEqual(explicit, [
            {"host": "copilot", "name": "context7", "state": "added"},
            {"host": "copilot", "name": "playwright", "state": "added"},
        ])
        self.assertIsNone(shutil.which("npx"))

    def test_fr003_all_bad_cursor_json_writes_copilot_and_raises(self) -> None:
        """FR-003: Cursor [] is not written, Copilot is written, and the call raises."""
        self.cursor.mkdir()
        self.copilot.mkdir()
        raw = b"[]"
        (self.cursor / "mcp.json").write_bytes(raw)
        with self.assertRaises(McpError) as caught:
            install("all")
        self.assertEqual(caught.exception.code, "not_object")
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw)
        self.assertEqual(sorted(p.name for p in self.cursor.iterdir()), ["mcp.json"])
        doc = json.loads((self.copilot / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["mcpServers"]["context7"], COPILOT_CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], COPILOT_PLAYWRIGHT)
        self.assert_no_auth(self.copilot / "mcp-config.json")
        copilot_raw = (self.copilot / "mcp-config.json").read_bytes()
        reported = status("all")
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw)
        self.assertEqual((self.copilot / "mcp-config.json").read_bytes(), copilot_raw)
        self.assertEqual(reported, [
            {"host": "claude", "name": "context7", "state": "absent"},
            {"host": "claude", "name": "playwright", "state": "absent"},
            {"host": "cursor", "name": "context7", "state": "absent"},
            {"host": "cursor", "name": "playwright", "state": "absent"},
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])

    def test_single_failure_with_no_added_row_keeps_its_message(self) -> None:
        """Nothing added: the one McpError keeps its original message."""
        self.cursor.mkdir()
        raw = b"[]"
        path = self.cursor / "mcp.json"
        path.write_bytes(raw)
        saved = os.environ.get("ORKESTRA_LANG")
        os.environ["ORKESTRA_LANG"] = "en"
        try:
            with self.assertRaises(McpError) as caught:
                install("cursor")
            expected = i18n.t("cli.kit_not_object", where=str(path))
            self.assertEqual(str(caught.exception), expected)
            self.assertEqual(caught.exception.code, "not_object")
            self.assertIn("nothing was changed", str(caught.exception))
        finally:
            if saved is None:
                os.environ.pop("ORKESTRA_LANG", None)
            else:
                os.environ["ORKESTRA_LANG"] = saved
        self.assertEqual(path.read_bytes(), raw)

    def _assert_partial_language(self, lang: str) -> None:
        self.cursor.mkdir(exist_ok=True)
        self.copilot.mkdir(exist_ok=True)
        raw = b"[]"
        (self.cursor / "mcp.json").write_bytes(raw)
        other = {"command": "keep"}
        (self.copilot / "mcp-config.json").write_text(
            json.dumps({"mcpServers": {"other": other}}), encoding="utf-8")
        os.environ["ORKESTRA_LANG"] = lang
        with self.assertRaises(McpError) as caught:
            install("all")
        text = str(caught.exception)
        self.assertIn("copilot", text)
        self.assertIn(str(self.cursor / "mcp.json"), text)
        self.assertIn(i18n.t("cli.doctor_mcp_failed"), text)
        self.assertNotIn("nothing was changed", text)
        self.assertNotIn("hiçbir şey değiştirilmedi", text)
        line = i18n.catalog(lang)["cli.mcp_partial"]
        self.assertNotIn("%", line)
        self.assertNotIn("\u2014", line)
        self.assertEqual(i18n.fields(line), ["hosts"])
        if lang == "en":
            for ch in "İıŞşĞğÜüÖöÇç":
                self.assertNotIn(ch, line)
        self.assertTrue(text.startswith(line.format(hosts="copilot")), text)
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw)
        doc = json.loads((self.copilot / "mcp-config.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["mcpServers"]["other"], other)
        self.assertEqual(doc["mcpServers"]["context7"], COPILOT_CONTEXT7)
        self.assertEqual(doc["mcpServers"]["playwright"], COPILOT_PLAYWRIGHT)
        self.assertEqual(set(doc["mcpServers"]), {"other", "context7", "playwright"})
        self.assert_no_auth(self.copilot / "mcp-config.json")

    def test_partial_install_names_copilot_and_keeps_every_failure(self) -> None:
        """Cursor [] plus a failing claude list still writes copilot. en and tr name that host and both failures."""
        saved = os.environ.get("ORKESTRA_LANG")
        self.plant("claude", exit_code=1)
        try:
            self._assert_partial_language("en")
            self._assert_partial_language("tr")
        finally:
            if saved is None:
                os.environ.pop("ORKESTRA_LANG", None)
            else:
                os.environ["ORKESTRA_LANG"] = saved

    def test_fr010_status_reports_present_or_absent_without_writing(self) -> None:
        """FR-010: status names are present or absent and the files stay byte for byte."""
        self.cursor.mkdir()
        self.copilot.mkdir()
        self.assertEqual(status("all"), list(ABSENT))
        self.assertFalse((self.cursor / "mcp.json").exists())
        self.assertFalse((self.copilot / "mcp-config.json").exists())
        install("all")
        raw_cursor = (self.cursor / "mcp.json").read_bytes()
        raw_copilot = (self.copilot / "mcp-config.json").read_bytes()
        self.assertEqual(status("all"), [
            {"host": "claude", "name": "context7", "state": "absent"},
            {"host": "claude", "name": "playwright", "state": "absent"},
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "present"},
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])
        self.assertEqual(status("cursor"), [
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "present"},
        ])
        self.assertEqual(status("cursor", ("serena",)), [
            {"host": "cursor", "name": "serena", "state": "absent"},
        ])
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw_cursor)
        self.assertEqual((self.copilot / "mcp-config.json").read_bytes(), raw_copilot)
        self.assert_real_homes_untouched()

    def test_fr010_status_reads_files_and_binary_lists(self) -> None:
        """FR-010: cursor and copilot files plus claude/copilot mcp list. Cursor's binary is not spawned."""
        self.cursor.mkdir()
        self.copilot.mkdir()
        cursor_raw = b'{"mcpServers":{"playwright":{"command":"local"}}}'
        copilot_raw = b'{"mcpServers":{"playwright":{"type":"stdio","command":"local","tools":["*"]}}}'
        (self.cursor / "mcp.json").write_bytes(cursor_raw)
        (self.copilot / "mcp-config.json").write_bytes(copilot_raw)
        self.plant("claude", list_text="playwright\n")
        self.plant("copilot", list_text="context7\n")
        self.plant("cursor", list_text="context7\n")
        rows = status("all")
        self.assertEqual(rows, [
            {"host": "claude", "name": "context7", "state": "absent"},
            {"host": "claude", "name": "playwright", "state": "present"},
            {"host": "cursor", "name": "context7", "state": "absent"},
            {"host": "cursor", "name": "playwright", "state": "present"},
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), cursor_raw)
        self.assertEqual((self.copilot / "mcp-config.json").read_bytes(), copilot_raw)
        cursor_bin = Path(shutil.which("cursor")).resolve()
        spawned = [Path(row[0]).resolve() for row in self.invocations()]
        self.assertNotIn(cursor_bin, spawned)
        self.assertIn(Path(shutil.which("claude")).resolve(), spawned)
        self.assertIn(Path(shutil.which("copilot")).resolve(), spawned)
        self.assertIsNone(shutil.which("npx"))

    def test_status_keeps_other_hosts_when_claude_list_fails(self) -> None:
        """A non-zero claude mcp list is unavailable. Cursor stays present or absent. Bytes do not change."""
        self.cursor.mkdir()
        raw = b'{"mcpServers":{"context7":{"url":"https://mcp.context7.com/mcp"}}}'
        (self.cursor / "mcp.json").write_bytes(raw)
        self.plant("claude", exit_code=1)
        try:
            rows = status("all")
        except McpError as exc:
            self.fail(f"status raised {exc.code}: {exc}")
        self.assertEqual(rows, [
            {"host": "claude", "name": "context7", "state": "unavailable"},
            {"host": "claude", "name": "playwright", "state": "unavailable"},
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "absent"},
            {"host": "copilot", "name": "context7", "state": "absent"},
            {"host": "copilot", "name": "playwright", "state": "absent"},
        ])
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw)
        self.assertFalse((self.copilot / "mcp-config.json").exists())
        (self.bin / "claude").unlink()
        self.assertIsNone(shutil.which("claude"))
        self.assertEqual(status("claude"), [
            {"host": "claude", "name": "context7", "state": "absent"},
            {"host": "claude", "name": "playwright", "state": "absent"},
        ])
        self.assertEqual((self.cursor / "mcp.json").read_bytes(), raw)

    def test_homes_arguments_override_env(self) -> None:
        """cursor_home and copilot_home override ORKESTRA_CURSOR_HOME and COPILOT_HOME."""
        other_cursor = self.tmp / "override-cursor"
        other_copilot = self.tmp / "override-copilot"
        other_cursor.mkdir()
        other_copilot.mkdir()
        install("all", None, other_cursor, other_copilot)
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.copilot.exists())
        self.assertTrue((other_cursor / "mcp.json").is_file())
        self.assertTrue((other_copilot / "mcp-config.json").is_file())
        raw_cursor = (other_cursor / "mcp.json").read_bytes()
        raw_copilot = (other_copilot / "mcp-config.json").read_bytes()
        self.assertEqual(status("all"), list(ABSENT))
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.copilot.exists())
        self.assertEqual(status("all", None, other_cursor, other_copilot), [
            {"host": "claude", "name": "context7", "state": "absent"},
            {"host": "claude", "name": "playwright", "state": "absent"},
            {"host": "cursor", "name": "context7", "state": "present"},
            {"host": "cursor", "name": "playwright", "state": "present"},
            {"host": "copilot", "name": "context7", "state": "present"},
            {"host": "copilot", "name": "playwright", "state": "present"},
        ])
        self.assertEqual((other_cursor / "mcp.json").read_bytes(), raw_cursor)
        self.assertEqual((other_copilot / "mcp-config.json").read_bytes(), raw_copilot)
        self.assert_real_homes_untouched()
