"""Token usage of the Claude Code sessions bound to a run — read-only.

Source: the transcripts Claude Code itself writes for every session:
  <claude dir>/projects/*/<session-id>.jsonl                   main conversation
  <claude dir>/projects/*/<session-id>/subagents/*.jsonl       subagent sidechains (current layout)
  <claude dir>/projects/<same dir>/agent-*.jsonl               subagent sidechains (older layout)
Only the `message.usage` numbers of assistant records are read; message text is never kept or returned.
Nothing is written anywhere, and every failure degrades to "no data" (None): the monitor never breaks on this.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

RESCAN_SECONDS = 10.0                      # how often new transcript files (new subagents) are looked for
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_LOCK = threading.Lock()                   # the dashboard serves requests from several threads

Entry = Tuple[Optional[float], int, int, int, int]      # (timestamp, input, output, cache write, cache read)


class _File:
    """Incremental reader state for one transcript: only bytes appended since the last call are parsed."""
    __slots__ = ("offset", "ident", "entries", "ctx", "ctx_ts")

    def __init__(self) -> None:
        self.offset = 0
        self.ident: Tuple[int, int] = (0, 0)
        self.entries: Dict[str, Entry] = {}
        self.ctx = 0                       # context size of the latest main-conversation reply
        self.ctx_ts = 0.0


_FILES: Dict[str, _File] = {}
_LOCATED: Dict[str, Tuple[float, List[Tuple[str, bool]]]] = {}
_AGENT_SID: Dict[str, str] = {}


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _ts(v: Any) -> Optional[float]:
    if not isinstance(v, str) or not v:
        return None
    try:
        return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bases() -> List[Path]:
    override = os.environ.get("ORKESTRA_CLAUDE_DIR")
    if override:
        cands = [override]
    else:
        cands = [c for c in (os.environ.get("CLAUDE_CONFIG_DIR") or "").split(",") if c.strip()]
        cands += ["~/.claude", "~/.config/claude"]
    out: List[Path] = []
    for c in cands:
        p = Path(c.strip()).expanduser() / "projects"
        if p not in out and p.is_dir():
            out.append(p)
    return out


def _agent_session(path: Path) -> str:
    """Session id recorded in an older-layout `agent-*.jsonl` (first record); cached once known."""
    key = str(path)
    if key in _AGENT_SID:
        return _AGENT_SID[key]
    try:
        with open(path, "rb") as f:
            obj = json.loads(f.readline(1 << 20))
    except (OSError, ValueError):
        return ""
    sid = str(obj.get("sessionId") or "") if isinstance(obj, dict) else ""
    if sid:
        _AGENT_SID[key] = sid
    return sid


def _locate(sid: str) -> List[Tuple[str, bool]]:
    """[(path, is_main)] transcripts of one session; re-scanned every RESCAN_SECONDS."""
    now = time.time()
    hit = _LOCATED.get(sid)
    if hit and now - hit[0] < RESCAN_SECONDS:
        return hit[1]
    found: List[Tuple[str, bool]] = []
    for base in _bases():
        try:
            dirs = [d for d in base.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in dirs:
            main = d / f"{sid}.jsonl"
            if not main.is_file():
                continue
            found.append((str(main), True))
            sub = d / sid / "subagents"
            if sub.is_dir():
                found.extend((str(p), False) for p in sorted(sub.glob("*.jsonl")))
            for p in sorted(d.glob("agent-*.jsonl")):
                if _agent_session(p) == sid:
                    found.append((str(p), False))
    _LOCATED[sid] = (now, found)
    return found


def _take(fs: _File, raw: bytes, path: str, is_main: bool) -> None:
    if b'"usage"' not in raw or b"assistant" not in raw:
        return
    try:
        obj = json.loads(raw)
    except ValueError:
        return
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return                             # e.g. a tool result that merely carries a subagent's usage summary
    msg = obj.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return
    tin, tout, cw, cr = (_int(usage.get(k)) for k in _FIELDS)
    ts = _ts(obj.get("timestamp"))
    mid = msg.get("id")
    # one API reply is written as several records (one per content block) with the same id: count it once
    key = f"{mid}:{obj.get('requestId') or ''}" if mid else f"@{path}:{fs.offset}"
    old = fs.entries.get(key)
    if old is None or tout >= old[2]:
        fs.entries[key] = (ts, tin, tout, cw, cr)
    if is_main and not obj.get("isSidechain") and (tin + cw + cr) > 0:
        fs.ctx, fs.ctx_ts = tin + cw + cr, (ts or time.time())


def _refresh(path: str, is_main: bool) -> Optional[_File]:
    try:
        st = os.stat(path)
    except OSError:
        return _FILES.get(path)
    fs = _FILES.get(path)
    ident = (st.st_dev, st.st_ino)
    if fs is None or fs.ident != ident or st.st_size < fs.offset:
        fs = _FILES[path] = _File()
        fs.ident = ident
    if st.st_size == fs.offset:
        return fs
    try:
        with open(path, "rb") as f:
            f.seek(fs.offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break                  # a record still being written — picked up on the next call
                fs.offset += len(raw)
                _take(fs, raw, path, is_main)
    except OSError:
        pass
    return fs


def _collect(sids: List[str], since: Optional[float],
             windows: Optional[List[Tuple[str, float, float]]] = None) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Entry] = {}
    seen = set()
    ctx, ctx_ts = 0, -1.0
    for sid in sids:
        for path, is_main in _locate(sid):
            if path in seen:
                continue
            fs = _refresh(path, is_main)
            if fs is None:
                continue
            seen.add(path)
            for key, ent in fs.entries.items():      # merged across files: a resumed session repeats old records
                old = merged.get(key)
                if old is None or ent[2] >= old[2]:
                    merged[key] = ent
            if is_main and fs.ctx and fs.ctx_ts > ctx_ts:
                ctx, ctx_ts = fs.ctx, fs.ctx_ts
    if not seen:
        return None
    tin = tout = cw = cr = n = 0
    phases: Dict[str, int] = {}
    spans = []
    for name, start, end in windows or []:
        try:
            spans.append((str(name), float(start), float(end)))
        except (TypeError, ValueError):
            continue
    for ts, a, b, c, d in merged.values():
        if not (a or b or c or d) or (since is not None and ts is not None and ts < since):
            continue
        tin, tout, cw, cr, n = tin + a, tout + b, cw + c, cr + d, n + 1
        if ts is None:
            continue
        bit = a + b + c + d
        for name, start, end in spans:
            if start <= ts < end:
                phases[name] = phases.get(name, 0) + bit
                break
    out = {"input": tin, "output": tout, "cache_write": cw, "cache_read": cr, "total": tin + tout + cw + cr,
           "messages": n, "context": ctx or None, "files": len(seen)}
    if windows is not None:
        out["phases"] = phases
    return out


def for_sessions(session_ids: Iterable[str], since: Optional[float] = None,
                 windows: Optional[List[Tuple[str, float, float]]] = None) -> Optional[Dict[str, Any]]:
    """Summed token usage of the given sessions and their subagents, counted from `since` (epoch seconds) on.

    Returns None when no transcript is found. `context` is the size of the latest main-conversation request
    (input + cache), i.e. how full the orchestrator's context window currently is.
    """
    try:
        with _LOCK:
            return _collect([s for s in session_ids if isinstance(s, str) and _SID_RE.match(s)], since, windows)
    except Exception:                      # a transcript quirk must never break watch / dashboard
        return None
