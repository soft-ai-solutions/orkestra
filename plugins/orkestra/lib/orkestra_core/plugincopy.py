"""Is the Claude Code plugin copy that runs this code older than the local checkout it was installed from?

Claude Code runs a plugin's hooks from its own cache (`<config>/plugins/cache/<marketplace>/<plugin>/<version>`) and
refreshes that copy only on `claude plugin update`: a checkout edited after the install is NOT what the guards run, and
nothing says so unless someone runs `orkestra doctor`. SessionStart asks `stale_note()` and tells the session.

Read-only and lean (it runs inside a hook): json/os/pathlib only, no hashing. The code files are compared by list,
then size, then bytes, stopping at the first difference. Every function returns a neutral answer instead of raising.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

# The code that runs the guards: what makes two copies behave the same.
CODE_GLOBS = ("hooks/*.py", "hooks/*.json", "hooks/*.sh", "lib/orkestra_core/*.py", "lib/orkestra_core/messages/*.py")


def code_files(root: Path) -> Dict[str, Path]:
    """Relative POSIX path -> file, for every code file under `root`."""
    out: Dict[str, Path] = {}
    for pattern in CODE_GLOBS:
        for f in root.glob(pattern):
            if f.is_file():
                out[f.relative_to(root).as_posix()] = f
    return out


def same_code(a: Path, b: Path) -> bool:
    """True when both trees hold the same code files with the same bytes (False when either has none)."""
    fa, fb = code_files(a), code_files(b)
    if not fa or fa.keys() != fb.keys():
        return False
    try:
        if any(fa[k].stat().st_size != fb[k].stat().st_size for k in fa):
            return False
        return all(fa[k].read_bytes() == fb[k].read_bytes() for k in fa)
    except OSError:
        return False


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_layout(root: Path) -> Optional[Path]:
    """`<config>/plugins` when `root` is `<config>/plugins/cache/<marketplace>/<plugin>/<version>`, else None."""
    parts = root.parts
    if len(parts) < 6 or parts[-4] != "cache" or parts[-5] != "plugins":
        return None
    return root.parents[3]


def checkout_of(root: Path) -> Optional[Path]:
    """The local checkout a Claude Code cache copy was installed from; None when `root` is not a cache copy, its
    marketplace is not a local directory (git / GitHub sources update themselves from a remote), or nothing resolves."""
    plugins_dir = _cache_layout(root)
    if plugins_dir is None:
        return None
    marketplace, plugin = root.parts[-3], root.parts[-2]
    reg = _read(plugins_dir / "known_marketplaces.json")
    entry = reg.get(marketplace) if isinstance(reg, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, dict) or source.get("source") != "directory":
        return None
    base = Path(str(source.get("path") or entry.get("installLocation") or "")).expanduser()
    if not str(base) or not base.is_dir():
        return None
    candidates: List[Path] = []
    manifest = _read(base / ".claude-plugin" / "marketplace.json")
    for p in (manifest.get("plugins") if isinstance(manifest, dict) else None) or []:
        if isinstance(p, dict) and p.get("name") == plugin and isinstance(p.get("source"), str):
            candidates.append(base / p["source"])
    candidates.append(base / "plugins" / plugin)
    for c in candidates:
        if (c / "hooks" / "hooks.json").is_file() and os.path.realpath(c) != os.path.realpath(root):
            return Path(os.path.realpath(c))
    return None


def _update_lines(root: Path) -> List[str]:
    """The `claude plugin update` line for every install scope Claude Code registered at `root` (user scope first)."""
    plugins_dir = _cache_layout(root)
    marketplace, plugin = root.parts[-3], root.parts[-2]
    ref = f"{plugin}@{marketplace}"
    lines = [f"claude plugin marketplace update {marketplace}"]
    reg = _read(plugins_dir / "installed_plugins.json") if plugins_dir else None
    entries = (reg.get("plugins") or {}).get(ref) if isinstance(reg, dict) and isinstance(reg.get("plugins"), dict) else None
    scopes: List[tuple] = []
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and os.path.realpath(str(e.get("installPath") or "")) == os.path.realpath(root):
            scope = str(e.get("scope") or "user")
            where = str(e.get("projectPath") or "")
            if (scope, where) not in scopes:
                scopes.append((scope, where))
    scopes.sort(key=lambda s: s[0] != "user")
    for scope, where in scopes or [("user", "")]:
        if scope == "user":
            lines.append(f"claude plugin update {ref}")
        elif where:
            lines.append(f'(cd "{where}" && claude plugin update {ref} -s {scope})')
        else:
            lines.append(f"claude plugin update {ref} -s {scope}")
    return lines


def stale_note(root: Path) -> str:
    """Model-facing context when the copy at `root` runs older code than its checkout; '' otherwise (never raises)."""
    try:
        checkout = checkout_of(root)
        if checkout is None or same_code(root, checkout):
            return ""
        repair = " && ".join(_update_lines(root))
    except Exception:  # a convenience check: it must never break the hook that asks
        return ""
    return (f"[orkestra] Claude Code is running an OLDER copy of the orkestra plugin ({root}) than its checkout "
            f"({checkout}): guard and workflow changes made since that install are NOT active in this session. Tell "
            f"the user once, in their language, to run this in a terminal and then restart Claude Code: `{repair}`")
