"""Engine fallback chain: which agent (and which account) drives a headless turn, and where to go when it is out.

`~/.orkestra/config.json` → `engine.chain` is an ordered list of profiles. A profile is one way to run a turn:

    {"name": "cursor-claude", "engine": "claude",
     "bin": "~/.cursor/extensions/anthropic.claude-code-*/resources/native-binary/claude",
     "config_dir": "", "model": "claude-opus-5-5"}

* `engine`     claude | cursor | copilot | pi (engines.ENGINES)
* `bin`        optional; may hold a glob, the newest match wins (IDE extensions keep one folder per version)
* `config_dir` claude only: the CLAUDE_CONFIG_DIR of the account ("" = Claude Code's default, ~/.claude). Two IDEs
               signed in to two accounts are two profiles; a session lives in exactly one of them.
* `model`      passed as --model

A turn that ends on a usage limit (or a signed-out account) puts its profile on cooldown and the job moves to the next
profile on a NEW session: sessions cannot cross accounts or engines, the Orkestra run state in the project can.
An empty chain keeps the single-engine behaviour. Stdlib only, no I/O at import.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import engines as EN

DEFAULT_COOLDOWN_MINUTES = 60

# Words the engines print when an account cannot take another turn right now. Transient errors (overloaded, network)
# are NOT here: the normal stalled/auto-resume ladder handles those on the same profile.
_LIMIT = re.compile(
    r"(hit your (?:weekly |daily |monthly |session |5-hour |usage )?limit"
    r"|usage limit (?:reached|exceeded)|reached your (?:\w+ )?(?:usage )?limit|limit reached"
    r"|out of (?:extra )?usage|credit balance is too low|quota (?:exceeded|exhausted)"
    r"|please run /login|not logged in|invalid api key|oauth token (?:has )?expired)",
    re.IGNORECASE)


def chain(cfg: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The valid profiles of engine.chain, in order, each with a stable `name` and `index`."""
    raw = (((cfg or {}).get("engine") or {}).get("chain")) or []
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(p, dict):
            continue
        try:
            engine = EN.normalize(str(p.get("engine") or ""))
        except EN.EngineError:
            continue
        prof = {"name": str(p.get("name") or f"{engine}-{i + 1}"), "engine": engine, "bin": str(p.get("bin") or ""),
                "config_dir": str(p.get("config_dir") or ""), "model": str(p.get("model") or "")}
        prof["index"] = len(out)
        out.append(prof)
    return out


def cooldown_seconds(cfg: Optional[Mapping[str, Any]]) -> float:
    try:
        minutes = float(((cfg or {}).get("engine") or {}).get("chain_cooldown_minutes") or DEFAULT_COOLDOWN_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_COOLDOWN_MINUTES
    return max(1.0, minutes) * 60


def _version_key(path: str) -> List[Any]:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", path)]


def resolve_bin(profile: Mapping[str, Any]) -> Optional[str]:
    """Absolute path of the profile's binary: its `bin` (newest glob match), else the engine's usual lookup."""
    raw = os.path.expanduser(str(profile.get("bin") or ""))
    if raw:
        hits = [h for h in glob.glob(raw) if os.path.isfile(h) and os.access(h, os.X_OK)]
        if hits:
            return sorted(hits, key=_version_key)[-1]
        if not glob.has_magic(raw) and os.path.isfile(raw):
            return raw
        return None
    try:
        return EN.binary(str(profile.get("engine") or "claude"))
    except EN.EngineError:
        return None


def config_dir(profile: Optional[Mapping[str, Any]]) -> str:
    raw = str((profile or {}).get("config_dir") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def apply_env(profile: Optional[Mapping[str, Any]], env: Dict[str, str]) -> Dict[str, str]:
    """Point a claude child at the profile's account. Without a config_dir the inherited CLAUDE_CONFIG_DIR is
    REMOVED (setting it to ~/.claude is not the same as the default: Claude Code then looks elsewhere for its
    state and sign-in)."""
    if not profile or profile.get("engine") != "claude":
        return env
    d = config_dir(profile)
    if d:
        env["CLAUDE_CONFIG_DIR"] = d
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def with_model(args: List[str], model: str) -> List[str]:
    """`args` with every --model / --model=X replaced by the profile's model (the last flag would win anyway; one
    flag keeps the command line readable)."""
    if not model:
        return list(args)
    out: List[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--model":
            skip = True
            continue
        if a.startswith("--model="):
            continue
        out.append(a)
    return out + ["--model", model]


def is_limit_error(*texts: Any) -> bool:
    return any(_LIMIT.search(str(t or "")) for t in texts)


# ---------------------------------------------------------------------------------------------------- cooldown book
def _book_path(home: Path) -> Path:
    return home / "bridge" / "limits.json"


def _read_book(home: Path) -> Dict[str, float]:
    try:
        data = json.loads(_book_path(home).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, TypeError, OSError):
        return {}


def mark_limited(home: Path, profile: Mapping[str, Any], seconds: float, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    book = {k: v for k, v in _read_book(home).items() if v > now}
    book[str(profile.get("name"))] = now + seconds
    path = _book_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(book, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def limited_until(home: Path, profile: Mapping[str, Any], now: Optional[float] = None) -> float:
    now = time.time() if now is None else now
    until = _read_book(home).get(str(profile.get("name")), 0.0)
    return until if until > now else 0.0


def pick(profiles: List[Dict[str, Any]], home: Path, after: int = -1, now: Optional[float] = None,
         usable: Any = None) -> Optional[Dict[str, Any]]:
    """The first profile past index `after` that is not cooling down and whose binary exists (`usable(profile)` may
    add a check, e.g. Cursor's hooks). None = nothing left."""
    for p in profiles:
        if p["index"] <= after or limited_until(home, p, now):
            continue
        if not resolve_bin(p):
            continue
        if usable is not None and not usable(p):
            continue
        return dict(p)
    return None


def profile_for_session(profiles: List[Dict[str, Any]], session_id: str) -> Optional[Dict[str, Any]]:
    """The claude profile whose account holds this session's transcript (<config dir>/projects/*/<id>.jsonl)."""
    sid = EN.raw_session_id(str(session_id or ""))
    if not sid or not re.fullmatch(r"[0-9A-Za-z-]{8,80}", sid):
        return None
    for p in profiles:
        if p["engine"] != "claude":
            continue
        base = config_dir(p) or os.path.expanduser("~/.claude")
        if glob.glob(os.path.join(glob.escape(base), "projects", "*", sid + ".jsonl")):
            return dict(p)
    return None
