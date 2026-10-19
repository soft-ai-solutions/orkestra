"""Merge MCP server entries into host config files. No import-time I/O. No network."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import engines as EN
from . import i18n
from .util import write_json_atomic

_DEFAULT_NAMES = ("context7", "playwright")
# Exact npx package pin; must equal PLAYWRIGHT_MCP_VERSION in install.sh (test_mcp_install_lock checks it).
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.83"


class McpError(EN.EngineError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _cursor_server(name: str) -> Dict[str, Any]:
    if name == "context7":
        return {"url": "https://mcp.context7.com/mcp"}
    if name == "playwright":
        return {"command": "npx", "args": [PLAYWRIGHT_MCP_PACKAGE]}
    if name == "serena":
        return {"command": "serena", "args": ["start-mcp-server"]}
    raise McpError("not_object", name)


def _cursor_file(home: Path) -> Path:
    """The file to read and write. A symlink is followed so the link itself is not replaced."""
    path = Path(home) / "mcp.json"
    if path.is_symlink():
        return Path(os.path.realpath(path))
    return path


def install_cursor(home: Path, names: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """Merge ``names`` into ``home/mcp.json`` under ``mcpServers``.

    Defaults are context7 and playwright. A name that already exists is left as it is.
    Nothing is written when every requested name is already present, or when the JSON is refused.
    """
    wanted = _DEFAULT_NAMES if names is None else tuple(names)
    target = _cursor_file(home)
    if target.exists():
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise McpError("bad_json", i18n.t("cli.kit_bad_json", path=target)) from exc
        if not isinstance(doc, dict):
            raise McpError("not_object", i18n.t("cli.kit_not_object", where=str(target)))
    else:
        doc = {}
    if "mcpServers" in doc and not isinstance(doc["mcpServers"], dict):
        raise McpError("not_object", i18n.t("cli.kit_not_object", where=f"{target} mcpServers"))
    servers: Dict[str, Any] = dict(doc["mcpServers"]) if "mcpServers" in doc else {}
    rows: List[Dict[str, str]] = []
    added = False
    for name in wanted:
        if name in servers:
            rows.append({"host": "cursor", "name": name, "state": "present"})
            continue
        servers[name] = _cursor_server(name)
        rows.append({"host": "cursor", "name": name, "state": "added"})
        added = True
    if not added:
        return rows
    new_doc = dict(doc)
    new_doc["mcpServers"] = servers
    write_json_atomic(target, new_doc)
    return rows


def _copilot_server(name: str) -> Dict[str, Any]:
    if name == "context7":
        return {"type": "http", "url": "https://mcp.context7.com/mcp", "tools": ["*"]}
    if name == "playwright":
        return {"type": "stdio", "command": "npx", "args": [PLAYWRIGHT_MCP_PACKAGE], "tools": ["*"]}
    if name == "serena":
        return {"type": "stdio", "command": "serena", "args": ["start-mcp-server"], "tools": ["*"]}
    raise McpError("not_object", name)


def _copilot_file(home: Path) -> Path:
    """The file to read and write. A symlink is followed so the link itself is not replaced."""
    path = Path(home) / "mcp-config.json"
    if path.is_symlink():
        return Path(os.path.realpath(path))
    return path


def install_copilot_file(home: Path, names: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """Merge ``names`` into ``home/mcp-config.json`` under ``mcpServers``.

    Defaults are context7 and playwright. Copilot entries carry ``type`` and ``tools``.
    A name that already exists is left as it is. Nothing is written when every requested
    name is already present, or when the JSON is refused.
    """
    wanted = _DEFAULT_NAMES if names is None else tuple(names)
    target = _copilot_file(home)
    if target.exists():
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise McpError("bad_json", i18n.t("cli.kit_bad_json", path=target)) from exc
        if not isinstance(doc, dict):
            raise McpError("not_object", i18n.t("cli.kit_not_object", where=str(target)))
    else:
        doc = {}
    if "mcpServers" in doc and not isinstance(doc["mcpServers"], dict):
        raise McpError("not_object", i18n.t("cli.kit_not_object", where=f"{target} mcpServers"))
    servers: Dict[str, Any] = dict(doc["mcpServers"]) if "mcpServers" in doc else {}
    rows: List[Dict[str, str]] = []
    added = False
    for name in wanted:
        if name in servers:
            rows.append({"host": "copilot", "name": name, "state": "present"})
            continue
        servers[name] = _copilot_server(name)
        rows.append({"host": "copilot", "name": name, "state": "added"})
        added = True
    if not added:
        return rows
    new_doc = dict(doc)
    new_doc["mcpServers"] = servers
    write_json_atomic(target, new_doc)
    return rows


_CMD_TIMEOUT = 30

_CLAUDE_ADD: Dict[str, Tuple[str, ...]] = {
    "context7": ("mcp", "add", "--transport", "http", "context7", "https://mcp.context7.com/mcp", "-s", "user"),
    "playwright": ("mcp", "add", "playwright", "-s", "user", "--", "npx", PLAYWRIGHT_MCP_PACKAGE),
    "serena": (
        "mcp", "add", "--scope", "user", "serena", "--", "serena", "start-mcp-server",
        "--context", "claude-code", "--project-from-cwd",
    ),
}

_COPILOT_ADD: Dict[str, Tuple[str, ...]] = {
    "context7": ("mcp", "add", "--transport", "http", "context7", "https://mcp.context7.com/mcp"),
    "playwright": ("mcp", "add", "playwright", "--", "npx", PLAYWRIGHT_MCP_PACKAGE),
    "serena": ("mcp", "add", "serena", "--", "serena", "start-mcp-server"),
}


def _listed(stdout: str, name: str, *, word: bool) -> bool:
    text = stdout or ""
    if word:
        return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE) is not None
    return name.lower() in text.lower()


def _spawn(binary: str, args: Sequence[str]) -> str:
    """Run ``binary`` plus ``args`` with no shell. Non-zero and spawn errors are ``command_failed``."""
    try:
        cp = subprocess.run([binary, *args], capture_output=True, encoding="utf-8", errors="replace",
                            timeout=_CMD_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise McpError("command_failed", i18n.t("cli.doctor_mcp_failed")) from exc
    if cp.returncode != 0:
        raise McpError("command_failed", i18n.t("cli.doctor_mcp_failed"))
    return cp.stdout or ""


def _install_cli(tool: str, host: str, catalog: Dict[str, Tuple[str, ...]], names: Optional[Sequence[str]],
                 *, word: bool) -> List[Dict[str, str]]:
    wanted = _DEFAULT_NAMES if names is None else tuple(names)
    for name in wanted:
        if name not in catalog:
            raise McpError("not_object", name)
    binary = shutil.which(tool)
    if not binary:
        raise McpError("host_missing", tool)
    stdout = _spawn(binary, ("mcp", "list"))
    rows: List[Dict[str, str]] = []
    for name in wanted:
        if _listed(stdout, name, word=word):
            rows.append({"host": host, "name": name, "state": "present"})
            continue
        _spawn(binary, catalog[name])
        rows.append({"host": host, "name": name, "state": "added"})
    return rows


def install_claude(names: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """Add ``names`` with ``claude mcp add``. A name already in ``claude mcp list`` stdout is skipped.

    Defaults are context7 and playwright. ``shutil.which`` resolves ``claude`` on the caller's PATH.
    Nothing is written to disk.
    """
    return _install_cli("claude", "claude", _CLAUDE_ADD, names, word=False)


def install_copilot(names: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """Add ``names`` with ``copilot mcp add``. A name that is already a word in ``copilot mcp list`` is skipped.

    Defaults are context7 and playwright. A non-zero exit raises ``command_failed``. This never writes
    ``mcp-config.json`` and never merges the Copilot file.
    """
    return _install_cli("copilot", "copilot", _COPILOT_ADD, names, word=True)


_HOSTS = ("claude", "cursor", "copilot")


def _engine_list(engine: str) -> Tuple[str, ...]:
    key = str(engine).strip().lower()
    if key == "all":
        return _HOSTS
    if key in _HOSTS:
        return (key,)
    raise McpError("host_missing", key)


def _home(explicit: Optional[Path], env_key: str, default: str) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get(env_key) or default).expanduser()


def _install_host(host: str, explicit: bool, names: Optional[Sequence[str]],
                  cursor: Path, copilot: Path) -> List[Dict[str, str]]:
    if host == "claude":
        if shutil.which("claude"):
            return install_claude(names)
        if explicit:
            raise McpError("host_missing", "claude")
        return []
    if host == "cursor":
        # Present when the home is a directory or `cursor` is on PATH. Writing mcp.json
        # creates a missing home. The cursor binary is never spawned.
        if cursor.is_dir() or shutil.which("cursor"):
            return install_cursor(cursor, names)
        if explicit:
            raise McpError("host_missing", "cursor")
        return []
    if shutil.which("copilot"):
        return install_copilot(names)
    if explicit or copilot.is_dir():
        return install_copilot_file(copilot, names)
    return []


def install(engine: str, names: Optional[Sequence[str]] = None, cursor_home: Optional[Path] = None,
            copilot_home: Optional[Path] = None) -> List[Dict[str, str]]:
    """Install ``names`` (default context7 and playwright) on the selected engine.

    ``all`` skips a host that is not present and returns no rows when none are.
    Cursor is present when its home is a directory or ``cursor`` is on PATH. A missing home is
    created by writing ``mcp.json``; the ``cursor`` binary is not spawned.
    Explicit Claude with no binary, and explicit Cursor with neither a home directory nor a binary,
    raise ``host_missing`` and write nothing.
    Explicit Copilot with no binary still merges ``mcp-config.json``, creating the home if needed.
    A Copilot or Claude binary is used alone: the Copilot file is not also written.
    Every selected host is attempted. Every ``McpError`` is kept, then one is raised.
    When a row was ``added``, that message starts with ``cli.mcp_partial`` and drops the
    unchanged parenthetical. A single failure with nothing added keeps its original message.
    """
    selected = _engine_list(engine)
    explicit = len(selected) == 1
    cursor = _home(cursor_home, "ORKESTRA_CURSOR_HOME", "~/.cursor")
    copilot = _home(copilot_home, "COPILOT_HOME", "~/.copilot")
    rows: List[Dict[str, str]] = []
    failures: List[McpError] = []
    for host in selected:
        try:
            rows.extend(_install_host(host, explicit, names, cursor, copilot))
        except McpError as exc:
            failures.append(exc)
    if failures:
        _raise_failures(rows, failures)
    return rows


def _unchanged_note() -> str:
    """The parenthetical on ``cli.kit_not_object`` in the current language, or ``""``."""
    rendered = i18n.t("cli.kit_not_object", where=".")
    mark = rendered.rfind(" (")
    if mark < 0 or not rendered.endswith(")"):
        return ""
    return rendered[mark:]


def _raise_failures(rows: List[Dict[str, str]], failures: List[McpError]) -> None:
    """Raise one error that still carries every failure. Never returns."""
    added: List[str] = []
    for row in rows:
        host = str(row.get("host") or "")
        if row.get("state") == "added" and host and host not in added:
            added.append(host)
    if not added and len(failures) == 1:
        raise failures[0]
    texts = [str(item) for item in failures]
    if added:
        note = _unchanged_note()
        if note:
            texts = [text.replace(note, "") for text in texts]
        message = i18n.t("cli.mcp_partial", hosts=", ".join(added)) + "\n" + "\n".join(texts)
    else:
        message = "\n".join(texts)
    raise McpError(failures[0].code, message)


def _server_names(path: Path) -> Set[str]:
    if not path.is_file():
        return set()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(doc, dict):
        return set()
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        return set()
    return {str(key) for key in servers}


def _list_names(tool: str, names: Sequence[str], *, word: bool) -> Tuple[Set[str], bool]:
    """(names found, list failed). A missing binary is (empty, False) so those rows stay absent."""
    binary = shutil.which(tool)
    if not binary:
        return set(), False
    try:
        stdout = _spawn(binary, ("mcp", "list"))
    except McpError:
        return set(), True
    return {name for name in names if _listed(stdout, name, word=word)}, False


def status(engine: str, names: Optional[Sequence[str]] = None, cursor_home: Optional[Path] = None,
           copilot_home: Optional[Path] = None) -> List[Dict[str, str]]:
    """Report each selected name. Never writes, and a failed ``mcp list`` does not raise.

    Cursor is read from ``mcp.json`` (``present`` or ``absent``). Claude is read from
    ``claude mcp list``: a missing binary is ``absent``; a non-zero list is ``unavailable``
    and does not drop the other hosts. Copilot is the file plus ``copilot mcp list`` when
    that binary is on PATH. A name proved by the file stays ``present``.
    """
    selected = _engine_list(engine)
    wanted = _DEFAULT_NAMES if names is None else tuple(names)
    cursor = _home(cursor_home, "ORKESTRA_CURSOR_HOME", "~/.cursor")
    copilot = _home(copilot_home, "COPILOT_HOME", "~/.copilot")
    found: Dict[str, Set[str]] = {host: set() for host in _HOSTS}
    failed: Dict[str, bool] = {host: False for host in _HOSTS}
    if "claude" in selected:
        found["claude"], failed["claude"] = _list_names("claude", wanted, word=False)
    if "cursor" in selected:
        found["cursor"] = _server_names(_cursor_file(cursor))
    if "copilot" in selected:
        listed, failed["copilot"] = _list_names("copilot", wanted, word=True)
        found["copilot"] = listed | (_server_names(_copilot_file(copilot)) & set(wanted))
    rows: List[Dict[str, str]] = []
    for host in selected:
        have = found[host]
        down = failed[host]
        for name in wanted:
            if name in have:
                state = "present"
            elif down:
                state = "unavailable"
            else:
                state = "absent"
            rows.append({"host": host, "name": name, "state": state})
    return rows
