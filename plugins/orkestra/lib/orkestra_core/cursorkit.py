"""Cursor side of Orkestra: the generated skills and subagents, and installing them (plus the hooks) into ~/.cursor.

One source of truth: the Claude Code plugin's `skills/*/SKILL.md` and `agents/*.md`. `build()` derives the Cursor
versions (other frontmatter, the vocabulary header from engines.with_notes) into `plugins/orkestra/cursor/`; a test
fails when that folder is stale. `install()` links them into the user's Cursor home and merges our hook entries into
`hooks.json` without touching anybody else's entries (a timestamped backup is written first).

Never called from hooks. Writes outside a project, so it is run by the user (`orkestra cursor install`), not by agents.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import engines as EN
from . import i18n
from .util import hook_python, replace_file

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
ASSETS = PLUGIN_ROOT / "cursor"
HOOK_SCRIPT = PLUGIN_ROOT / "hooks" / "cursor_hook.py"
BIN = PLUGIN_ROOT / "bin" / "orkestra"
# preToolUse covers Shell/Read/Write/Delete/Task/MCP; the older before*/after* events would only repeat it
# Seconds Cursor waits for each hook. A hook that is cut off counts as "no answer": a guard that did not run.
# 2026-10-05, machine overloaded (load average 230+): the full path needed up to 19 s, the old 15 s cut it off.
HOOK_EVENTS = {"sessionStart": 30, "beforeSubmitPrompt": 30, "preToolUse": 30, "postToolUse": 30,
               "postToolUseFailure": 30, "stop": 30, "subagentStart": 15, "sessionEnd": 15}
MARK = "cursor_hook.py"
# Cursor stops honouring a stop hook's followup_message after loop_limit (default 5) follow-ups per conversation;
# orkestra caps its own nudges (hookmain), so the stop entry carries an explicit null (= no Cursor-side limit).
# Opt-in `--fail-closed`: Cursor then blocks the call when these hooks crash or time out (default: it lets it through).
FAIL_CLOSED = ("preToolUse", "subagentStart")


def cursor_home() -> Path:
    return Path(os.environ.get("ORKESTRA_CURSOR_HOME") or "~/.cursor").expanduser()


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, m.group(2).lstrip("\n")


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)        # a JSON string is a valid YAML scalar


def skill_text(name: str) -> str:
    fm, body = _split_frontmatter((PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))
    desc = fm.get("description", "").strip().strip('"')
    body = body.replace("$ARGUMENTS", "(what the user wrote with this command)")
    head = ["---", f"name: orkestra-{name}", f"description: {_yaml(desc)}"]
    if fm.get("disable-model-invocation", "").lower() == "true":
        head.append("disable-model-invocation: true")
    head.append("---")
    return "\n".join(head) + "\n" + EN.with_notes(body, "cursor").rstrip("\n") + "\n"


def agent_text(name: str) -> str:
    fm, body = _split_frontmatter((PLUGIN_ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8"))
    desc = fm.get("description", "").strip().strip('"')
    # readonly stays false on purpose: a Cursor "readonly" subagent has no shell and cannot write its report file.
    # What each role may touch is enforced by the orkestra guards (hooks), exactly as in Claude Code.
    head = ["---", f"name: orkestra-{name}", f"description: {_yaml(desc)}", "model: inherit", "readonly: false",
            "is_background: false", "---"]
    return "\n".join(head) + "\n" + EN.with_notes(body, "cursor", agent=True).rstrip("\n") + "\n"


def expected() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n in EN.SKILL_NAMES:
        out[f"skills/orkestra-{n}/SKILL.md"] = skill_text(n)
    for n in EN.AGENT_NAMES:
        out[f"agents/orkestra-{n}.md"] = agent_text(n)
    return out


def stale(root: Optional[Path] = None) -> List[str]:
    root = root or ASSETS
    bad = []
    for rel, text in expected().items():
        p = root / rel
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            bad.append(rel)
    return bad


def build(root: Optional[Path] = None, dry: bool = False) -> List[str]:
    """Regenerate the committed tree; returns the files written (with dry=True: the files that would be)."""
    root = root or ASSETS
    written = []
    for rel, text in expected().items():
        p = root / rel
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            if not dry:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="\n") as fh:      # LF on every OS: the generated tree is committed
                    fh.write(text)
            written.append(rel)
    return written


# ---------------------------------------------------------------- hooks.json

def _python() -> str:
    """The interpreter the hook commands name (util.hook_python: not a virtualenv's, never the Windows Store stub)."""
    return hook_python()


def hook_command(event: str, python: Optional[str] = None) -> str:
    return f'"{python or _python()}" "{HOOK_SCRIPT}" {event}'


def _is_ours(entry: Any) -> bool:
    return isinstance(entry, dict) and MARK in str(entry.get("command") or "") and "orkestra" in str(entry.get("command") or "")


def _check_shape(doc: Any, where: str = "hooks.json") -> None:
    """Refuse a file we do not understand instead of guessing (nothing is changed)."""
    if doc is None:
        return
    if not isinstance(doc, dict):
        raise EN.EngineError(i18n.t("cli.kit_not_object", where=where))
    hooks = doc.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise EN.EngineError(i18n.t("cli.kit_hooks_not_object", where=where))
    for event, entries in (hooks or {}).items():
        if not isinstance(entries, list):
            raise EN.EngineError(i18n.t("cli.kit_event_not_list", where=where, event=event))


def _kept_python(entries: List[Any], event: str) -> Optional[str]:
    """An entry of ours that already points at this checkout with an interpreter that exists is left as it is
    (a second install from another PATH must not rewrite the file)."""
    for e in entries:
        m = re.match(r'^"([^"]+)" "([^"]+)" (\S+)$', str(e.get("command") or "")) if _is_ours(e) else None
        if m and m.group(2) == str(HOOK_SCRIPT) and m.group(3) == event and os.path.exists(m.group(1)):
            return m.group(1)
    return None


def merge_hooks(doc: Any, fail_closed: bool = False) -> Dict[str, Any]:
    """Our entries added (or refreshed) at the END of each event's list; every other entry is kept as it is."""
    _check_shape(doc)
    doc = dict(doc) if isinstance(doc, dict) else {}
    doc.setdefault("version", 1)
    hooks = dict(doc.get("hooks") or {})
    for event, timeout in HOOK_EVENTS.items():
        cur = list(hooks.get(event) or [])
        rest = [e for e in cur if not _is_ours(e)]
        entry: Dict[str, Any] = {"command": hook_command(event, _kept_python(cur, event)), "timeout": timeout}
        if event == "stop":
            entry["loop_limit"] = None
        if (fail_closed or any(_is_ours(e) and e.get("failClosed") for e in cur)) and event in FAIL_CLOSED:   # opt-in survives a re-install
            entry["failClosed"] = True
        hooks[event] = rest + [entry]
    doc["hooks"] = hooks
    return doc


def strip_hooks(doc: Any) -> Dict[str, Any]:
    _check_shape(doc)
    doc = dict(doc) if isinstance(doc, dict) else {}
    hooks = dict(doc.get("hooks") or {})
    for event in list(hooks):
        cur = list(hooks.get(event) or [])
        rest = [e for e in cur if not _is_ours(e)]
        if rest or len(rest) == len(cur):           # an event that was empty before we came stays as it was
            hooks[event] = rest
        else:
            hooks.pop(event)
    doc["hooks"] = hooks
    return doc


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_hooks(hp: Path, doc: Dict[str, Any], actions: List[str], dry: bool) -> None:
    """Backup, then an atomic write — through a symlink if hooks.json is one (dotfiles repositories)."""
    target = Path(os.path.realpath(hp)) if hp.is_symlink() else hp
    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"hooks.json.orkestra-backup-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = target.with_name(f"hooks.json.orkestra-backup-{stamp}-{n}")
        actions.append(i18n.t("cli.kit_backup", target=target, backup=backup.name))
        if not dry:
            shutil.copy2(target, backup)
    if dry:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    replace_file(tmp, target)


def _same(src: Path, dst: Path) -> bool:
    if src.is_dir():
        if not dst.is_dir():
            return False
        names = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
        return names == sorted(p.relative_to(dst) for p in dst.rglob("*") if p.is_file()) and \
            all((src / n).read_bytes() == (dst / n).read_bytes() for n in names)
    return dst.is_file() and src.read_bytes() == dst.read_bytes()


def _link(src: Path, dst: Path, copy: bool, actions: List[str], dry: bool) -> None:
    if dst.is_symlink() or dst.exists():
        if dst.is_symlink() and not copy and os.path.realpath(dst) == os.path.realpath(src):
            return
        if copy and not dst.is_symlink() and _same(src, dst):
            return
        if not dry:
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
    actions.append(i18n.t("cli.kit_copy" if copy else "cli.kit_link", dst=dst, src=src))
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
    else:
        try:
            os.symlink(src, dst)
        except OSError:
            # Windows without Developer Mode (and rare POSIX) — fall back to a real copy.
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
            actions[-1] = i18n.t("cli.kit_copy_nolink", dst=dst, src=src)


def _targets() -> List[Tuple[Path, Path]]:
    home = cursor_home()
    out = [(ASSETS / "skills" / f"orkestra-{n}", home / "skills" / f"orkestra-{n}") for n in EN.SKILL_NAMES]
    out += [(ASSETS / "agents" / f"orkestra-{n}.md", home / "agents" / f"orkestra-{n}.md") for n in EN.AGENT_NAMES]
    return out


def _is_windows() -> bool:
    return os.name == "nt"


def _default_shim_dir() -> str:
    """ORKESTRA_SHIM_DIR, else ~/.local/bin; on Windows the per-user WindowsApps folder (on PATH out of the box) when it
    exists, because ~/.local/bin is not on PATH there and is rarely created."""
    env = os.environ.get("ORKESTRA_SHIM_DIR")
    if env:
        return env
    if _is_windows():
        apps = Path(os.environ.get("LOCALAPPDATA") or "~/AppData/Local").expanduser() / "Microsoft" / "WindowsApps"
        if apps.is_dir():
            return str(apps)
    return "~/.local/bin"


def shim_path() -> Path:
    return Path(_default_shim_dir()).expanduser() / "orkestra"


def _orkestra_on_path() -> bool:
    return bool(shutil.which("orkestra") or shutil.which("orkestra.cmd"))


def _windows_cmd_body() -> str:
    """The cmd.exe wrapper: CRLF, the active Python and the plugin's bin/orkestra."""
    return f'@echo off\r\n"{sys.executable}" "{BIN}" %*\r\n'


def _write_windows_cmd_shim(cmd_path: Path, dry: bool) -> None:
    """Write a cmd.exe wrapper that invokes the plugin bin with the active Python."""
    if dry:
        return
    with open(cmd_path, "w", encoding="utf-8", newline="") as fh:       # Path.write_text(newline=) is 3.10+; body has CRLF
        fh.write(_windows_cmd_body())


def _read_small(path: Path) -> str:
    """Text of a small regular file, line endings untouched ('' when missing, large or unreadable)."""
    try:
        if path.is_file() and path.stat().st_size < 4096:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
                return fh.read()
    except OSError:
        pass
    return ""


def ensure_path_shim(*, dry: bool = False) -> List[str]:
    """Ensure `orkestra` is reachable on PATH via a shim (idempotent).

    Uses `shim_path()` / `ORKESTRA_SHIM_DIR`. No-op when the parent directory is missing or
    `orkestra` is already found on PATH (dangling shims are still renewed). On Windows writes
    `orkestra.cmd` (symlink may be unavailable without Developer Mode). Returns the same
    style of action strings as `install`.
    """
    actions: List[str] = []
    shim = shim_path()
    if _orkestra_on_path() or not shim.parent.is_dir():
        return actions
    if _is_windows():
        cmd_path = shim.parent / "orkestra.cmd"
        have = _read_small(cmd_path)
        # missing, or our own wrapper left by another Python / checkout (renewed); a file of the user's is left alone
        if not cmd_path.exists() or (have != _windows_cmd_body() and "orkestra" in have.lower() and "%*" in have):
            actions.append(i18n.t("cli.kit_write_cmd_shim", path=cmd_path, target=BIN))
            _write_windows_cmd_shim(cmd_path, dry)
        return actions
    dangling = shim.is_symlink() and not shim.exists()
    if dangling or not os.path.lexists(shim):
        actions.append(i18n.t("cli.kit_link_shim", path=shim, target=BIN))
        if not dry:
            if dangling:
                shim.unlink()
            try:
                os.symlink(BIN, shim)
            except OSError:
                # Rare POSIX environments without symlink privilege — fall back to a tiny wrapper.
                shim.write_text(f"#!/usr/bin/env bash\nexec '{sys.executable}' '{BIN}' \"$@\"\n", encoding="utf-8")
                shim.chmod(0o755)
                actions[-1] = i18n.t("cli.kit_write_shim_wrapper", path=shim, target=BIN)
    return actions


def install(copy: bool = False, dry: bool = False, fail_closed: bool = False) -> List[str]:
    """Idempotent. Returns what was (or with dry=True: would be) done."""
    actions: List[str] = []
    if stale():
        raise EN.EngineError(i18n.t("cli.kit_stale"))
    home = cursor_home()
    hp = home / "hooks.json"
    cur = _read_json(hp)
    if (hp.exists() or hp.is_symlink()) and not isinstance(cur, dict):       # checked before anything is changed
        raise EN.EngineError(i18n.t("cli.kit_bad_json", path=hp))
    new = merge_hooks(cur, fail_closed)
    for src, dst in _targets():
        _link(src, dst, copy, actions, dry)
    if new != cur:
        _write_hooks(hp, new, actions, dry)
        actions.append(i18n.t("cli.kit_hooks_added", path=hp, events=", ".join(HOOK_EVENTS)))
    actions.extend(ensure_path_shim(dry=dry))
    return actions


def uninstall(dry: bool = False) -> List[str]:
    actions: List[str] = []
    hp = cursor_home() / "hooks.json"
    cur = _read_json(hp)
    if (hp.exists() or hp.is_symlink()) and not isinstance(cur, dict):       # as in install(): before anything is removed
        raise EN.EngineError(i18n.t("cli.kit_bad_json", path=hp))
    new = strip_hooks(cur) if isinstance(cur, dict) else None      # a shape we do not understand: stop before any change
    for src, dst in _targets():
        if dst.is_symlink() or dst.exists():
            actions.append(i18n.t("cli.kit_remove", path=dst))
            if not dry:
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
    if new is not None and new != cur:
        _write_hooks(hp, new, actions, dry)
        actions.append(i18n.t("cli.kit_hooks_removed", path=hp))
    shim = shim_path()
    wrapper = _read_small(shim) if not shim.is_symlink() else ""       # the shell wrapper written where symlinks failed
    if (shim.is_symlink() and os.path.realpath(shim) == os.path.realpath(BIN)) or (wrapper and str(BIN) in wrapper):
        actions.append(i18n.t("cli.kit_remove", path=shim))
        if not dry:
            shim.unlink()
    cmd_path = shim.parent / "orkestra.cmd"                               # Windows PATH shim written by ensure_path_shim
    have = _read_small(cmd_path)
    # only a wrapper of THIS checkout: the stable checkout's shim must survive uninstalling a development copy
    if have and "orkestra" in have.lower() and "%*" in have and str(BIN) in have:
        actions.append(i18n.t("cli.kit_remove", path=cmd_path))
        if not dry:
            cmd_path.unlink()
    return actions


def _entries_ok(doc: Any) -> bool:
    hooks = (doc.get("hooks") if isinstance(doc, dict) else None)
    if not isinstance(hooks, dict):
        return False
    return all(any(_is_ours(x) and str(HOOK_SCRIPT) in str(x.get("command")) for x in (hooks.get(e) or [])
                   if isinstance(hooks.get(e), list))
               for e in HOOK_EVENTS)


def hooks_installed() -> bool:
    """Are OUR hooks (this checkout's cursor_hook.py) registered for every event in the user's Cursor home? Without
    them a Cursor run has no guards at all, so headless drivers refuse to start one."""
    return _entries_ok(_read_json(cursor_home() / "hooks.json"))


def hooks_active(project: Optional[Path] = None) -> bool:
    """…or in the project's own `.cursor/hooks.json` (a team that keeps them per repository)."""
    return hooks_installed() or bool(project and _entries_ok(_read_json(Path(project) / ".cursor" / "hooks.json")))


def stale_claude_copy() -> str:
    """Cursor also loads Claude Code plugins ("third-party plugins") — from the path Claude Code's registry records.
    If that is an old cached copy of orkestra, its hooks answer in Cursor chats too, with old rules. Returns a
    description of such a copy, or ''. Read-only; never touches ~/.claude."""
    reg = _read_json(Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "plugins" / "installed_plugins.json")
    try:
        from . import __version__ as current
        for name, entries in ((reg or {}).get("plugins") or {}).items():
            if not str(name).startswith("orkestra@"):
                continue
            for e in entries if isinstance(entries, list) else []:
                path = Path(str(e.get("installPath") or "")).expanduser()
                if not str(e.get("installPath") or "") or os.path.realpath(path) == os.path.realpath(PLUGIN_ROOT):
                    continue
                marker = path / "hooks" / "cursor_hook.py"      # 1.5.0+ copies stay silent for Cursor's payloads
                if path.is_dir() and not marker.exists():
                    return f"{path} (version {e.get('version') or '?'}; this checkout is {current})"
    except Exception:
        return ""
    return ""


def status(cfg: Optional[Dict[str, Any]] = None) -> List[Tuple[Optional[bool], str, str]]:
    """[(ok | None = information only, label, detail)] — read-only."""
    rows: List[Tuple[bool, str, str]] = []
    b = EN.binary("cursor", cfg)
    rows.append((bool(b), "cursor CLI (agent)", b or i18n.t("cli.kit_cursor_cli_missing")))
    rows.append((not stale(), "generated assets current", "" if not stale() else i18n.t("cli.kit_run_build")))
    missing = [str(dst) for src, dst in _targets()
               if not (dst.exists() and (os.path.realpath(dst) == os.path.realpath(src) or not dst.is_symlink()))]
    rows.append((not missing, "skills and subagents installed",
                 i18n.t("cli.kit_n_missing", count=len(missing)) if missing else str(cursor_home())))
    doc = _read_json(cursor_home() / "hooks.json")
    hooks = doc.get("hooks") if isinstance(doc, dict) and isinstance(doc.get("hooks"), dict) else {}
    have = [e for e in HOOK_EVENTS if isinstance(hooks.get(e), list) and
            any(_is_ours(x) and str(HOOK_SCRIPT) in str(x.get("command")) for x in hooks[e])]
    rows.append((len(have) == len(HOOK_EVENTS), "hooks.json entries",
                 i18n.t("cli.kit_n_events", have=len(have), total=len(HOOK_EVENTS))))
    rows.append((bool(shutil.which("orkestra")), "`orkestra` on PATH",
                 shutil.which("orkestra") or i18n.t("cli.kit_not_on_path")))
    old = stale_claude_copy()
    if old:
        rows.append((None, "an older orkestra copy is also loaded by Cursor through Claude Code's plugin registry",
                     i18n.t("cli.kit_older_copy", path=old)))
    return rows
