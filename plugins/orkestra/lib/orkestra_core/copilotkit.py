"""Copilot side of Orkestra: install/status/uninstall for hooks under ~/.copilot/plugins/orkestra.

Mirrors cursorkit's hooks merge (keep other entries, timestamped backup, dry-run, refuse broken JSON).
Canonical layout: $COPILOT_HOME/plugins/orkestra/com.github.copilot/hooks/hooks.json with commands that
invoke this checkout's hooks/copilot_hook.py. Never called from hooks — user runs `orkestra copilot install`.
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

from . import cursorkit as CK
from . import engines as EN
from . import i18n
from . import paths
from .util import hook_python, replace_file

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_SCRIPT = PLUGIN_ROOT / "hooks" / "copilot_hook.py"
# Events Copilot Agent Plugins may fire; UserPromptSubmit/PreToolUse/PostToolUse are required for readiness.
HOOK_EVENTS = {
    "SessionStart": 20,
    "UserPromptSubmit": 20,
    "subagentStart": 20,
    "PreToolUse": 20,
    "PostToolUse": 20,
    "PostToolUseFailure": 20,
    "Stop": 20,
    "SubagentStop": 20,
}
MARK = "copilot_hook.py"
# Agent Plugins 1.0 requires $schema + name; a directory without plugin.json is not loaded as a plugin.
MANIFEST = {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": "orkestra",
            "version": "1.0.0", "description": "Orkestra guards and skills for GitHub Copilot"}


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or "~/.copilot").expanduser()


def plugin_dir() -> Path:
    return copilot_home() / "plugins" / "orkestra"


def hooks_path() -> Path:
    return plugin_dir() / "com.github.copilot" / "hooks" / "hooks.json"


def _python() -> str:
    """The interpreter the hook commands name (util.hook_python: not a virtualenv's, never the Windows Store stub)."""
    return hook_python()


def hook_command(event: str, python: Optional[str] = None) -> str:
    return f'"{python or _python()}" "{HOOK_SCRIPT}" {event}'


def hook_entry(event: str, timeout: int, python: Optional[str] = None) -> Dict[str, Any]:
    """`command` alone is copied to PowerShell too, where a quoted program name needs the `&` call operator."""
    cmd = hook_command(event, python)
    return {"type": "command", "bash": cmd, "powershell": "& " + cmd, "timeoutSec": timeout}


def _entry_command(entry: Any) -> str:
    """The shell line of an entry: the current `bash` field or the older single `command` field."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("bash") or entry.get("command") or "")


def _is_ours(entry: Any) -> bool:
    cmd = _entry_command(entry)
    return MARK in cmd and "orkestra" in cmd


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
    for e in entries:
        m = re.match(r'^"([^"]+)" "([^"]+)" (\S+)$', _entry_command(e)) if _is_ours(e) else None
        if m and m.group(2) == str(HOOK_SCRIPT) and m.group(3) == event and os.path.exists(m.group(1)):
            return m.group(1)
    return None


def merge_hooks(doc: Any) -> Dict[str, Any]:
    """Our entries added (or refreshed) at the END of each event's list; every other entry is kept as it is."""
    _check_shape(doc)
    doc = dict(doc) if isinstance(doc, dict) else {}
    doc.setdefault("version", 1)
    hooks = dict(doc.get("hooks") or {})
    for event, timeout in HOOK_EVENTS.items():
        cur = list(hooks.get(event) or [])
        rest = [e for e in cur if not _is_ours(e)]
        hooks[event] = rest + [hook_entry(event, timeout, _kept_python(cur, event))]
    doc["hooks"] = hooks
    return doc


def strip_hooks(doc: Any) -> Dict[str, Any]:
    _check_shape(doc)
    doc = dict(doc) if isinstance(doc, dict) else {}
    hooks = dict(doc.get("hooks") or {})
    for event in list(hooks):
        cur = list(hooks.get(event) or [])
        rest = [e for e in cur if not _is_ours(e)]
        if rest or len(rest) == len(cur):
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


def manifest_path() -> Path:
    return plugin_dir() / "plugin.json"


def skills_dir() -> Path:
    return plugin_dir() / "skills"


def skill_text(name: str) -> str:
    """The Claude skill source, in the shape cursorkit generates, with the Copilot vocabulary header."""
    fm, body = CK._split_frontmatter((PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))
    desc = fm.get("description", "").strip().strip('"')
    body = body.replace("$ARGUMENTS", "(what the user wrote with this command)")
    head = ["---", f"name: orkestra-{name}", f"description: {CK._yaml(desc)}", "---"]
    return "\n".join(head) + "\n" + EN.with_notes(body, "copilot").rstrip("\n") + "\n"


def _write_text(p: Path, text: str, actions: List[str], dry: bool, label: str) -> None:
    try:
        if p.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    actions.append(label)
    if dry:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    replace_file(tmp, p)


def _install_plugin_files(actions: List[str], dry: bool) -> None:
    """plugin.json (without it Copilot ignores the directory) and the generated skills."""
    manifest = json.dumps(MANIFEST, indent=2) + "\n"
    mp = manifest_path()
    if not mp.exists():                       # never overwrite a manifest someone else wrote
        _write_text(mp, manifest, actions, dry, i18n.t("cli.kit_copilot_manifest"))
    for n in EN.SKILL_NAMES:
        _write_text(skills_dir() / f"orkestra-{n}" / "SKILL.md", skill_text(n), actions, dry,
                    i18n.t("cli.kit_copilot_skill", name=f"orkestra-{n}"))


def _uninstall_plugin_files(actions: List[str], dry: bool) -> None:
    """Remove only what install wrote: our skills (carry the Copilot mark) and a manifest equal to ours."""
    for n in EN.SKILL_NAMES:
        d = skills_dir() / f"orkestra-{n}"
        f = d / "SKILL.md"
        try:
            ours = EN.COPILOT_MARK in f.read_text(encoding="utf-8")
        except OSError:
            continue
        if ours:
            actions.append(i18n.t("cli.kit_copilot_skill_removed", name=d.name))
            if not dry:
                shutil.rmtree(d, ignore_errors=True)
    if not dry:
        try:
            skills_dir().rmdir()
        except OSError:
            pass
    if _read_json(manifest_path()) == MANIFEST:
        actions.append(i18n.t("cli.kit_copilot_manifest_removed"))
        if not dry:
            try:
                manifest_path().unlink()
            except OSError:
                pass


def install(dry: bool = False) -> List[str]:
    """Idempotent. Returns what was (or with dry=True: would be) done."""
    actions: List[str] = []
    hp = hooks_path()
    cur = _read_json(hp)
    if (hp.exists() or hp.is_symlink()) and not isinstance(cur, dict):
        raise EN.EngineError(i18n.t("cli.kit_bad_json", path=hp))
    new = merge_hooks(cur)
    if new != cur:
        _write_hooks(hp, new, actions, dry)
        actions.append(i18n.t("cli.kit_hooks_added", path=hp, events=", ".join(HOOK_EVENTS)))
    _install_plugin_files(actions, dry)       # after the hooks file was accepted: a broken one changes nothing
    return actions


def uninstall(dry: bool = False) -> List[str]:
    actions: List[str] = []
    hp = hooks_path()
    cur = _read_json(hp)
    if (hp.exists() or hp.is_symlink()) and not isinstance(cur, dict):
        raise EN.EngineError(i18n.t("cli.kit_bad_json", path=hp))
    new = strip_hooks(cur) if isinstance(cur, dict) else None
    if new is not None and new != cur:
        _write_hooks(hp, new, actions, dry)
        actions.append(i18n.t("cli.kit_hooks_removed", path=hp))
    _uninstall_plugin_files(actions, dry)
    return actions


def hooks_active() -> bool:
    """Are OUR prompt/tool guards registered under the Copilot plugin dir?"""
    return EN.copilot_hooks_active(str(plugin_dir()) if plugin_dir().is_dir() else "")


def status(cfg: Optional[Dict[str, Any]] = None) -> List[Tuple[Optional[bool], str, str]]:
    """[(ok | None = information only, label, detail)] — read-only."""
    rows: List[Tuple[Optional[bool], str, str]] = []
    b = EN.binary("copilot", cfg)
    rows.append((bool(b), "copilot CLI", b or i18n.t("cli.kit_not_found")))
    active = hooks_active()
    rows.append((active, "copilot hooks", i18n.t("cli.kit_active" if active else "cli.kit_not_installed")))
    detail, supported = profile_info(cfg, b)
    rows.append((None, "copilot profile", detail))
    if supported is False:
        rows.append((None, "copilot context", i18n.t("cli.kit_context_unsupported")))
    return rows


def profile_info(cfg: Optional[Dict[str, Any]], binary: Optional[str] = None) -> Tuple[str, Optional[bool]]:
    """(effective model/effort/context/allow-all with where each came from, does the CLI know --context?)."""
    supported = EN.copilot_cli_has(binary, "--context") if binary else None
    user = paths.read_json(paths.orkestra_home() / "config.json", default={}) or {}      # the merged cfg hides what is default
    try:
        o = EN.copilot_resolve(user if isinstance(user, dict) else {})
    except EN.EngineError as exc:
        return str(exc), supported
    src = {k: i18n.t("cli.kit_src_" + v) for k, v in o["sources"].items()}
    allow = i18n.t("cli.kit_on" if o["allow_all"] else "cli.kit_off")
    return (i18n.t("cli.kit_profile", model=o["model"], model_src=src["model"], effort=o["reasoning_effort"],
                   effort_src=src["reasoning_effort"], context=o["context"], context_src=src["context"],
                   allow=allow, allow_src=src["allow_all"]), supported)


# ---------------------------------------------------------------- VS Code (settings + optional agent file)
# Levers checked in VS Code 1.141: chat.plugins.enabled, chat.pluginLocations (machine-scoped: user settings only),
# chat.permissions.default, chat.defaultConfiguration {mode, approvals}, chat.defaultModel, and a custom agent's
# `model` / `reasoning-effort`. VS Code has NO setting for the reasoning effort or context size of built-in models
# (picker only), so nothing here pretends to pin those globally.
AGENT_MARK = "<!-- orkestra-managed: orkestra copilot vscode -->"
AGENT_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def vscode_user_settings() -> Path:
    home = Path(os.path.expanduser("~"))
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Code" / "User" / "settings.json"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Code" / "User" / "settings.json"


def vscode_workspace_settings(workspace: str) -> Path:
    return Path(workspace).expanduser() / ".vscode" / "settings.json"


DEFAULT_CONFIGURATION = {"mode": "autopilot", "approvals": "allowAll"}      # what --apply writes when allow_all is on


def vscode_merge(cur: Dict[str, Any], plugin: str, permissions: Optional[str], remove: bool,
                 model: Optional[str] = None, default_config: Optional[Dict[str, str]] = None,
                 plugin_locations: bool = True) -> Dict[str, Any]:
    """Only our keys change. `chat.plugins.enabled` stays true on removal (other plugins may rely on it).

    Removal drops a key only while it still holds the value Orkestra writes (`permissions` when given, else either
    value it can write; `model`; DEFAULT_CONFIGURATION): a value the user changed since is theirs.
    `plugin_locations=False` (workspace file): chat.pluginLocations is machine-scoped, so it is not written there."""
    doc = dict(cur)
    locs = dict(doc.get("chat.pluginLocations") or {}) if isinstance(doc.get("chat.pluginLocations"), dict) else {}
    if remove:
        locs.pop(plugin, None)
        if locs:
            doc["chat.pluginLocations"] = locs
        else:
            doc.pop("chat.pluginLocations", None)
        if doc.get("chat.permissions.default") in ((permissions,) if permissions else ("autoApprove", "autopilot")):
            doc.pop("chat.permissions.default")
        if model and doc.get("chat.defaultModel") == model:
            doc.pop("chat.defaultModel")
        if doc.get("chat.defaultConfiguration") == DEFAULT_CONFIGURATION:
            doc.pop("chat.defaultConfiguration")
        return doc
    if plugin_locations:
        doc["chat.plugins.enabled"] = True
        locs[plugin] = True
        doc["chat.pluginLocations"] = locs
    if permissions:
        doc["chat.permissions.default"] = permissions
    if model:
        doc["chat.defaultModel"] = model
    if default_config:
        doc["chat.defaultConfiguration"] = dict(default_config)
    return doc


def _vscode_backup_and_write(target: Path, text: str, actions: List[str], dry: bool) -> None:
    target = Path(os.path.realpath(target)) if target.is_symlink() else target
    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.orkestra-backup-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = target.with_name(f"{target.name}.orkestra-backup-{stamp}-{n}")
        actions.append(i18n.t("cli.kit_backup", target=target, backup=backup.name))
        if not dry:
            shutil.copy2(target, backup)
    if not dry:
        _write_text(target, text, [], False, "")


def vscode_settings(path: Path, plugin: str, permissions: Optional[str], remove: bool, dry: bool,
                    model: Optional[str] = None, default_config: Optional[Dict[str, str]] = None,
                    plugin_locations: bool = True) -> List[str]:
    """Merge into one settings.json. Refuses a file that is not plain JSON (VS Code allows comments: never stripped)."""
    actions: List[str] = []
    cur: Any = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            cur = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            raise EN.EngineError(i18n.t("cli.kit_vscode_bad_json", path=path))
        if not isinstance(cur, dict):
            raise EN.EngineError(i18n.t("cli.kit_vscode_bad_json", path=path))
    new = vscode_merge(cur, plugin, permissions, remove, model, default_config, plugin_locations)
    if new == cur:
        return actions
    _vscode_backup_and_write(path, json.dumps(new, indent=4, ensure_ascii=False) + "\n", actions, dry)
    actions.append(i18n.t("cli.kit_vscode_removed" if remove else "cli.kit_vscode_written", path=path))
    return actions


def vscode_agent(workspace: str, model: str, effort: Optional[str], remove: bool, dry: bool) -> List[str]:
    """`.github/agents/orkestra.agent.md` pinned to a model (and effort). Only a file we wrote is replaced or removed."""
    path = Path(workspace).expanduser() / ".github" / "agents" / "orkestra.agent.md"
    actions: List[str] = []
    try:
        old = path.read_text(encoding="utf-8")
    except OSError:
        old = None
    if old is not None and AGENT_MARK not in old:
        raise EN.EngineError(i18n.t("cli.kit_vscode_agent_foreign", path=path))
    if remove:
        if old is not None:
            actions.append(i18n.t("cli.kit_vscode_agent_removed", path=path))
            if not dry:
                path.unlink()
        return actions
    model = EN._copilot_check("model", model, "flag")
    head = ["---", "name: orkestra", "description: Orkestra guarded run", f"model: [{json.dumps(model)}]"]
    if effort:
        if effort.lower() not in AGENT_EFFORTS:
            raise EN.EngineError(i18n.t("cli.copilot_bad_value", key="reasoning-effort", value=effort[:60],
                                        allowed=", ".join(AGENT_EFFORTS), origin="flag"))
        head.append(f"reasoning-effort: {effort.lower()}")
    body = ("Run the shell command `orkestra playbook run` first and follow what it prints exactly. "
            "Orkestra's hooks stay in force.")
    text = "\n".join(head + ["---", AGENT_MARK, "", body]) + "\n"
    if text != old:
        _write_text(path, text, [], dry, "")
        actions.append(i18n.t("cli.kit_vscode_agent_written", path=path))
    return actions
