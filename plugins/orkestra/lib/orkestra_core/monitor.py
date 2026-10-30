"""Read-only live view of every orkestra run in the workspace: `orkestra watch` (terminal) and
`orkestra dashboard` (local web page on 127.0.0.1).

Sources (all files, nothing is executed in the projects):
  .orkestra/runs/<run>/state.json      phase, status, tasks, evidence      (CLI-written)
  .orkestra/runs/<run>/activity.jsonl  tool calls / subagents / questions   (hook-written)
  .orkestra/runs/<run>/gates-*.json    gate results
  .orkestra/features/<run>/ledger.md   human-readable event log
  ~/.orkestra/active-runs.json, ~/.orkestra/sessions/*.json
  ~/.claude/projects/*/<session>.jsonl  token usage of the bound sessions (read-only, numbers only — usage.py)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import html
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import activity as A
from . import i18n
from . import paths
from . import tempo as T
from . import engines as EN
from . import usage as U
from . import risk as RK
from . import console as CO
from .state import OPEN_STATUSES, PHASES, active_runs
from .util import read_json, read_text

SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "target", "Pods", "__pycache__"}


def _word(prefix: str, ident: Any, default: str = "", code: Optional[str] = None) -> str:
    """Display name of a phase / status id from the catalog (`monitor.phase_plan`) in the current language, or in `code`;
    `default` when the id has no entry. The ids themselves are protocol values and are never translated."""
    key = f"{prefix}{ident}"
    text = i18n.catalog(code).get(key) if code else i18n.t(key)
    return default if not text or text == key else text


# ---------------------------------------------------------------- discovery

def _project_dirs(workspace: Path) -> Iterable[Path]:
    seen = set()
    for key in active_runs():
        p = Path(key)
        if p.is_dir() and p not in seen:
            seen.add(p)
            yield p
    if not workspace.is_dir():
        return
    for child in sorted(workspace.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        if (child / ".orkestra" / "runs").is_dir():
            if child not in seen:
                seen.add(child)
                yield child
            continue
        try:                                    # one level deeper (e.g. ~/Projects/clients/acme)
            for sub in sorted(child.iterdir()):
                if sub.is_dir() and not sub.name.startswith(".") and (sub / ".orkestra" / "runs").is_dir() \
                        and sub not in seen:
                    seen.add(sub)
                    yield sub
        except OSError:
            continue


UNREADABLE_MARK = "\u0000unreadable"          # private marker on the stand-in state of a run whose state.json cannot be parsed


def _view_key(project: Path, run_id: str) -> str:
    """The page's id of a run (`key` in a view): stable, and never a path the page could send back to act on."""
    return hashlib.sha1((str(project) + "\0" + str(run_id)).encode("utf-8")).hexdigest()[:12]


# A finished run does not change, but `--all` lists every one of them and each view reads its activity, gates and
# usage again: 127 runs took ~0.45 s per poll (every 2 s). The server keeps a closed run's view while its files keep
# their mtime, at most CLOSED_CACHE_SECONDS (the view's "ago" sentences move on), keyed per language.
CLOSED_CACHE_SECONDS = 30.0
CLOSED_CACHE_MAX = 2000
_closed_cache: Dict[Tuple[str, str, str], Tuple[float, float, Dict[str, Any]]] = {}
_closed_lock = threading.Lock()


def _collect_cached(project: Path, st: Dict[str, Any], mtime: float) -> Dict[str, Any]:
    if st.get("status") in OPEN_STATUSES or not mtime:
        return _collect_safe(project, st, mtime)
    key, now = (str(project), str(st.get("run_id")), i18n.lang()), time.time()
    with _closed_lock:
        hit = _closed_cache.get(key)
    if hit and hit[0] == mtime and 0 <= now - hit[1] < CLOSED_CACHE_SECONDS:
        return dict(hit[2], now=round(now, 3))
    view = _collect_safe(project, st, mtime)
    with _closed_lock:
        if len(_closed_cache) >= CLOSED_CACHE_MAX:
            _closed_cache.clear()
        _closed_cache[key] = (mtime, now, view)
    return view


def discover(project: Optional[Path] = None, include_all: bool = False, recent_hours: float = 12.0,
             workspace: Optional[Path] = None, cache: bool = False) -> List[Dict[str, Any]]:
    """Open runs (+ runs finished in the last `recent_hours`, or every run with include_all). `cache`: reuse the views
    of closed runs whose files did not change (the dashboard server; `watch` and one-off calls read fresh)."""
    projects = [project] if project else list(_project_dirs(workspace or paths.workspace_root()))
    now = time.time()
    found = []
    for proj in projects:
        rdir = paths.runs_dir(proj)
        if not rdir.is_dir():
            continue
        for d in rdir.iterdir():
            sp = d / "state.json"
            st = read_json(sp)
            if sp.is_file() and not isinstance(st, dict):
                # a state.json that exists but cannot be read is a run in trouble: it must never vanish from the page
                try:
                    umt = sp.stat().st_mtime
                except OSError:
                    umt = 0
                found.append((proj, {"run_id": d.name, UNREADABLE_MARK: True}, umt))
                continue
            if not isinstance(st, dict) or "run_id" not in st:
                continue
            try:
                mtime = max(sp.stat().st_mtime, (d / "activity.jsonl").stat().st_mtime
                            if (d / "activity.jsonl").exists() else 0)
            except OSError:
                mtime = 0
            if st.get("status") in OPEN_STATUSES or include_all or (now - mtime) < recent_hours * 3600:
                found.append((proj, st, mtime))
    # stable order (open runs first, then project name): cards/blocks must not jump around as activity arrives
    found.sort(key=lambda x: (x[1].get("status") not in OPEN_STATUSES and UNREADABLE_MARK not in x[1],
                              x[0].name.lower(), str(x[1].get("run_id"))))
    one = _collect_cached if cache else _collect_safe
    return [_unreadable_view(proj, st, mtime, ValueError("state.json is not valid JSON")) if UNREADABLE_MARK in st
            else one(proj, st, mtime) for proj, st, mtime in found]


def _collect_safe(project: Path, st: Dict[str, Any], mtime: float) -> Dict[str, Any]:
    """`collect`, but a run whose files do not have the expected shape (hand-edited, half written, from another version)
    becomes a view that says so instead of failing the whole list: every other run keeps showing."""
    try:
        return collect(project, st, mtime)
    except Exception as exc:
        return _unreadable_view(project, st, mtime, exc)


def _text_of(value: Any, limit: int = 200) -> str:
    """Display text for a field of unknown type (lone surrogates removed: the answer must stay encodable)."""
    return str(value if value is not None else "").encode("utf-8", "replace").decode("utf-8")[:limit]


def _unreadable_view(project: Path, st: Dict[str, Any], mtime: float, exc: BaseException) -> Dict[str, Any]:
    """The smallest complete view of a run that could not be read: same keys as `collect`, nothing guessed. It sits in
    the Stalled group with a plain sentence; the technical reason is in the Details (`live.text`)."""
    now = time.time()
    rid = _text_of(st.get("run_id"), 120)
    phase = st.get("phase") if st.get("phase") in PHASES else "intake"
    status = st.get("status") if isinstance(st.get("status"), str) else ""
    folder = str(paths.run_dir(project, rid)) if rid else str(project)
    cause = f"{type(exc).__name__}: {_text_of(exc, 160)}"
    zero = {"done": 0, "in_progress": 0, "blocked": 0, "pending": 0, "deferred": 0}
    none_gate: Dict[str, Any] = {"state": "none", "ok": 0, "total": 0}
    return {
        "project": str(project), "project_name": project.name, "run_id": rid, "title": _text_of(st.get("title")),
        "size": _text_of(st.get("size"), 20), "mode": _text_of(st.get("mode"), 20),
        "engine": _text_of(st.get("engine") or "claude", 20), "phase": phase,
        "phase_label": _word("monitor.phase_", phase, phase), "phase_tr": _word("monitor.phase_", phase, phase, "tr"),
        "phase_index": PHASES.index(phase), "phase_since": None, "status": status,
        "status_label": _word("monitor.status_", status, ""), "status_tr": _word("monitor.status_", status, "", "tr"),
        "approved": False, "live": {"level": "stale", "text": cause}, "agents": [],
        "stall": {"cls": 0, "key": "unreadable", "since": None, "text": cause, "loops": []}, "flags": [],
        "timing": {"run": 0, "phases": {}, "task_avg": None, "reopens": 0, "tasks_reopened": 0},
        "tasks": [], "tasks_done": 0, "tasks_total": 0, "current_tasks": [], "gates": {"baseline": "\u2014", "verify": "\u2014"},
        "activity": [], "ledger": [], "sessions": [], "usage": None, "updated": "", "artifacts": folder,
        "key": hashlib.sha1((str(project) + "\0" + rid).encode("utf-8", "replace")).hexdigest()[:12],
        "now": round(now, 3), "home": _home(),
        "attention": {"group": "stalled", "code": "unreadable", "rank": GROUP_RANK["stalled"], "level": "stale",
                      "since": None, "since_ts": None, "head": i18n.t("monitor.attn_unreadable_head"),
                      "what": i18n.t("monitor.attn_unreadable_what"), "do": i18n.t("monitor.attn_unreadable_do"),
                      "command": "", "where": "none", "activity_ts": None},
        "last_event_ts": None, "state_updated_ts": float(mtime) if mtime else None, "phase_history": [],
        "phases_skipped": [], "tasks_by_status": zero, "gates_struct": {"baseline": dict(none_gate), "verify": dict(none_gate)},
        "notices": [], "activity_feed": [], "risks": [], "risk_counts": {"high": 0, "medium": 0, "blocked": 0},
        "job": None, "docs": [],
    }


def _driving_engine(project: Path, run_id: str, stored: str) -> str:
    """Who drives the run now. `stored` is who started it (`claude` when the CLI did not see Cursor).

    A Cursor CLI or IDE chat binds afterwards (`sessions/cursor-*.json`, `engine: cursor`). The card must name
    that driver. A session file that names no engine leaves the stored value alone."""
    stored = stored or "claude"
    d = paths.sessions_dir()
    if not d.is_dir():
        return stored
    newest_hb, newest_engine = -1.0, ""
    found = False
    for f in d.glob("*.json"):
        data = read_json(f)
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            continue
        if Path(str(data.get("project") or "")) != Path(project):
            continue
        try:
            hb = f.stat().st_mtime
        except OSError:
            continue
        sid = f.stem
        if sid.startswith("cursor-"):
            identified = "cursor"
        elif sid.startswith("copilot-"):
            identified = "copilot"
        elif sid.startswith("pi-"):
            identified = "pi"
        else:
            eng = str(data.get("engine") or "")
            identified = eng if eng in ("claude", "cursor", "copilot", "pi") else ""
        if hb >= newest_hb:
            found, newest_hb, newest_engine = True, hb, identified
    if not found:
        return stored
    return newest_engine or stored


def _sessions_for(project: Path, run_id: str) -> List[Dict[str, Any]]:
    out = []
    d = paths.sessions_dir()
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        data = read_json(f)
        if isinstance(data, dict) and data.get("run_id") == run_id and Path(data.get("project", "")) == Path(project):
            out.append({"id": f.stem, "session": EN.short_sid(f.stem), "bound_at": data.get("bound_at", "")})
    return out


def _session_beats(project: Path, run_id: str) -> Dict[str, float]:
    """{session file stem: heartbeat (epoch)} of the sessions bound to the run: the hooks touch the file on every event."""
    out: Dict[str, float] = {}
    d = paths.sessions_dir()
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        data = read_json(f)
        if isinstance(data, dict) and data.get("run_id") == run_id and Path(data.get("project", "")) == Path(project):
            try:
                out[f.stem] = f.stat().st_mtime
            except OSError:
                continue
    return out


def _gate_summary(rdir: Path) -> Dict[str, str]:
    out = {}
    for label in ("baseline", "verify"):
        data = read_json(rdir / f"gates-{label}.json")
        if not isinstance(data, dict):
            out[label] = "—"
            continue
        res = data.get("results") or []
        ok = sum(1 for r in res if r.get("ok"))
        txt = i18n.t("monitor.gates_passed", ok=ok, total=len(res)) if res else i18n.t("monitor.gates_none")
        cmp_ = data.get("comparison") or {}
        if label == "verify" and cmp_:
            txt += " ✓" if cmp_.get("ok") else \
                " ✗ " + i18n.t("monitor.gates_regression", names=", ".join(cmp_.get("regressions") or []) or "?")
        out[label] = txt
    return out


def _parse_iso(s: str) -> Optional[float]:
    try:
        return _dt.datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _gate_struct(rdir: Path) -> Dict[str, Dict[str, Any]]:
    """The gates as numbers (`gates` keeps the localized sentence): state pass | fail | none, passed, total, regressions."""
    out: Dict[str, Dict[str, Any]] = {}
    for label in ("baseline", "verify"):
        data = read_json(rdir / f"gates-{label}.json")
        entry: Dict[str, Any] = {"state": "none", "ok": 0, "total": 0}
        if isinstance(data, dict):
            res = [r for r in (data.get("results") or []) if isinstance(r, dict)]
            ok = sum(1 for r in res if r.get("ok"))
            cmp_ = data.get("comparison") or {}
            entry.update(ok=ok, total=len(res))
            if res:
                failed = ok < len(res) or (label == "verify" and bool(cmp_) and not cmp_.get("ok"))
                entry["state"] = "fail" if failed else "pass"
            if label == "verify":
                entry["regressions"] = [str(x) for x in (cmp_.get("regressions") or [])]
        out[label] = entry
    return out


# ---- shortening of raw commands and paths (the page has the same algorithm in JS: keep them in step)

def _trim(text: str, limit: int) -> str:
    """`text` cut at a word boundary with an ellipsis when it is longer than `limit` (never mid-word, never silent)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "\u2026"


def _mid(text: str, limit: int) -> str:
    """`text` cut in the middle with an ellipsis when it is longer than `limit` (the beginning and the end stay)."""
    if len(text) <= limit:
        return text
    head = int(math.ceil((limit - 1) * 0.62))
    tail = int(math.floor((limit - 1) * 0.38))
    return text[:head] + "…" + (text[len(text) - tail:] if tail else "")


def _rel_path(path: str, project: str, home: str) -> str:
    if project and (path == project or path.startswith(project + "/")):
        return path[len(project) + 1:] or "."
    if home and path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


_CD_PREFIX = re.compile(r"^\s*cd\s+(\S+)\s*(?:&&|;)\s*")
_PIPE_TAIL = re.compile(r"\s*(?:2>&1|\|\s*(?:tee\s+\S+|tail\s+-?\d+|head\s+-?\d+))\s*$")


def short_what(tool: str, what: str, project: str = "", home: str = "", limit: int = 88) -> str:
    """A command / path in the words a person scans: the project prefix and presentation pipes are dropped, the home
    directory is `~`, and the middle is elided when it is still long. The stored text is never changed."""
    s = str(what or "")
    if tool == "Bash":
        def cd(match: Any) -> str:
            rel = _rel_path(match.group(1), project, home)
            return "" if rel == "." else "(" + rel + ") "
        s = _CD_PREFIX.sub(cd, s, count=1)
        for _ in range(4):
            s = _PIPE_TAIL.sub("", s)
        s = re.sub(r"\s+", " ", s).strip()
    elif tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        s = _rel_path(s, project, home)
    elif tool in ("Grep", "Glob"):
        s = " ".join(_rel_path(w, project, home) if w.startswith("/") else w for w in s.split(" "))
    if home:
        s = s.replace(home + "/", "~/")
    return _mid(s, limit)


def _cap(text: str, limit: int = 600) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---- activity feed

def _row_id(ts: float, agent: str, tool: str, what: str) -> str:
    return hashlib.sha1(f"{ts}|{agent}|{tool}|{what}".encode("utf-8")).hexdigest()[:10]


def _deny_parts(what: str) -> Tuple[str, str]:
    """`[category] reason` of a guard denial (category `general` when the text has none)."""
    m = re.match(r"\s*\[([^\]]*)\]\s*(.*)", str(what or ""), re.S)
    return ((m.group(1).strip() or "general"), m.group(2)) if m else ("general", str(what or ""))


# Rows the hooks write in their own words (messages/dispatch.py): the page shows the kind as a badge already, so the text
# next to it only needs what the badge does not say. (kind, hook template, short phrase in this catalog or None for "")
_BRIEFS = (
    ("idle", "dispatch.turn_plain", None),
    ("idle", "dispatch.turn_done", "monitor.brief_turn_done"),
    ("idle", "dispatch.turn_awaiting", "monitor.brief_turn_awaiting"),
    ("idle", "dispatch.turn_status", "monitor.brief_turn_status"),
    ("idle", "dispatch.turn_off_topic", "monitor.brief_turn_unbound"),
    ("idle", "dispatch.turn_not_driving", "monitor.brief_turn_unbound"),
    ("idle", "dispatch.turn_transcript_off_topic", "monitor.brief_turn_unbound"),
    ("idle", "dispatch.turn_nudge_limit", "monitor.brief_turn_nudge_limit"),
    ("idle", "dispatch.turn_session_closed", "monitor.brief_turn_closed"),
    ("wait_agents", "dispatch.wait_agents", "monitor.brief_wait_agents"),
    ("wait_agents", "dispatch.wait_report", "monitor.brief_wait_report"),
    ("agent_done", "dispatch.agent_done", "monitor.brief_agent_done"),
)
_TEMPLATE_RE: Dict[str, Any] = {}


def _template_re(template: str) -> Any:
    """A template with {fields} as an anchored regular expression (the fields become named groups)."""
    found = _TEMPLATE_RE.get(template)
    if found is None:
        parts = re.split(r"\{(\w+)\}", template)
        found = re.compile("".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>.+?)" for i, p in enumerate(parts)), re.S)
        _TEMPLATE_RE[template] = found
    return found


def _brief(kind: str, what: str, tool: str, cats: Dict[str, Dict[str, str]]) -> Optional[str]:
    """The short phrase (current language) for a hook-written row, or None when the text is not one the hooks write. The
    hook wrote it in its own session's language, so both catalogs (`cats`) are tried."""
    text = str(what or "").strip()
    if kind == "tool":                                      # an AskUserQuestion row reads "question: <headings>"
        if tool != "AskUserQuestion":
            return None
        for code in ("en", "tr"):
            template = cats[code].get("activity.ask_summary")
            m = _template_re(template).fullmatch(text) if template else None
            if m is not None:
                return m.group("heads")
        return None
    for row_kind, hook_key, brief_key in _BRIEFS:
        if row_kind != kind:
            continue
        for code in ("en", "tr"):
            template = cats[code].get(hook_key)
            m = _template_re(template).fullmatch(text) if template else None
            if m is None:
                continue
            if brief_key is None:
                return ""
            groups = m.groupdict()
            if "status" in groups:
                groups["status"] = _word("monitor.status_", groups["status"], groups["status"])
            if "agent" in groups:
                groups["agent"] = A.short_agent(groups["agent"])
            return i18n.t(brief_key, **groups)
    return None


# ---- risky actions: what an agent ran (or tried, and a guard refused) that can delete data, publish, push or reach
# secrets. risk.classify decides from the recorded text alone; a refused call counts when its own text is risky or when
# the guard that refused it protects against damage (not a process rule such as the plan phase or a wait loop).
DANGER_GUARDS = frozenset({"git", "secrets", "store", "deploy", "deps", "isolation", "home", "internals", "destructive",
                           "rollback"})
RISK_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read"})
RISK_KEEP = 40                       # newest risky actions per run in a view (the counts cover every row read)


def _row_risk(row: Dict[str, Any], project: str = "", home: str = "") -> Optional[Dict[str, str]]:
    """risk.classify of one activity row: a tool call's own text, or the refused call a deny row carries."""
    kind, tool = row.get("kind"), str(row.get("tool") or "")
    what = str(row.get("what") or "") if kind == "tool" else str(row.get("cmd") or "") if kind == "deny" else ""
    if not what or not (tool in RISK_TOOLS or tool.startswith("mcp__")):
        return None
    return RK.classify(tool, what, project, home)


def _risks(rows: List[Dict[str, Any]], project: str = "", home: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """(newest risky actions first, at most RISK_KEEP, identical consecutive ones folded with a count;
    {"high", "medium", "blocked"} counts over all of them). Blocked ones never ran, whatever their level."""
    events: List[Dict[str, Any]] = []
    counts = {"high": 0, "medium": 0, "blocked": 0}
    for row in rows:
        kind = row.get("kind")
        if kind not in ("tool", "deny"):
            continue
        hit = _row_risk(row, project, home)
        guard = reason = ""
        if kind == "deny":
            guard, reason = _deny_parts(str(row.get("what") or ""))
            if hit is None and guard not in DANGER_GUARDS:
                continue
            if hit is None:
                hit = {"level": "medium", "code": "guard", "label": i18n.t("monitor.risk_guard", guard=guard)}
        elif hit is None:
            continue
        ts, agent, tool = float(row.get("ts") or 0), str(row.get("agent") or "main"), str(row.get("tool") or "")
        what = str(row.get("what") or "") if kind == "tool" else str(row.get("cmd") or "")
        outcome = "blocked" if kind == "deny" else "ran"
        counts["blocked" if outcome == "blocked" else hit["level"]] += 1
        last = events[-1] if events else None
        if last and (last["_agent"], last["tool"], last["what"], last["outcome"]) == (agent, tool, what, outcome):
            last["count"] += 1
            last["ts"] = ts
            continue
        ev = {"id": _row_id(ts, agent, tool, kind + "|" + what), "ts": ts, "level": hit["level"], "code": hit["code"],
              "label": hit["label"], "tool": tool, "what": _cap(what, 600), "agent": A.short_agent(agent),
              "main": agent == "main", "outcome": outcome, "count": 1, "_agent": agent}
        if outcome == "blocked":
            ev["guard"], ev["reason"] = guard, _cap(reason, 300)
        events.append(ev)
    events.sort(key=lambda e: -e["ts"])
    out = events[:RISK_KEEP]
    for ev in out:
        ev.pop("_agent", None)
    return out, counts


def _activity_feed(rows: List[Dict[str, Any]], limit: int, compact: bool = True, project: str = "",
                   home: str = "") -> List[Dict[str, Any]]:
    """The newest `limit` rows for a person. `compact=False` is the legacy list (what `watch` prints and the `activity`
    key always held): every row is kept, only repeated guard denials merge. `compact=True` (the `activity_feed` key the
    page reads) also folds identical consecutive rows into one with a `count`. A risky row carries `risk`
    ({level, code, label}, see risk.py)."""
    if limit <= 0:
        return []
    entries: List[Dict[str, Any]] = []
    latest_denial: Dict[Tuple[str, str], Dict[str, Any]] = {}
    cats = {code: i18n.catalog(code) for code in ("en", "tr")}
    for row in A.visible(rows, len(rows)):
        ts = float(row.get("ts") or 0)
        agent = str(row.get("agent") or "main")
        item = {"time": time.strftime("%H:%M:%S", time.localtime(ts)),
                "agent": A.short_agent(agent), "kind": row.get("kind"), "tool": row.get("tool") or "",
                "what": row.get("what") or "", "ts": ts, "id": _row_id(ts, agent, row.get("tool") or "", row.get("what") or ""),
                "count": 1, "main": agent == "main", "_agent": agent, "_ts": ts, "_sort_ts": ts}
        brief = _brief(str(item["kind"]), str(item["what"]), str(item["tool"]), cats) \
            if item["kind"] in ("idle", "wait_agents", "agent_done", "tool") else None
        if brief is not None:
            item["brief"] = brief
        if item["kind"] == "deny":
            # the hook records a refused call as ONE deny row (the command itself is not stored there), so the row
            # carries the category and the reason only: an earlier, allowed call must never be shown as "blocked"
            item["category"], item["reason"] = _deny_parts(item["what"])
            item["attempt"] = str(row.get("cmd") or "") if item["tool"] == "Bash" else ""   # the refused call, as the hook recorded it
            if item["tool"] == "Bash":
                key = (agent, item["what"], item["attempt"])
                previous = latest_denial.get(key)
                if previous and 0 <= ts - previous["_last_ts"] <= 600:
                    previous["repeat_count"] += 1
                    previous["count"] = previous["repeat_count"]
                    previous["time"] = item["time"]
                    previous["ts"] = ts
                    previous["_last_ts"] = ts
                    previous["_sort_ts"] = ts
                    continue
                item["repeat_count"] = 1
                item["_last_ts"] = ts
                latest_denial[key] = item
        elif compact and entries:
            last = entries[-1]
            if last["kind"] != "deny" and (last["_agent"], last["kind"], last["tool"], last["what"]) == \
                    (agent, item["kind"], item["tool"], item["what"]):
                last["count"] += 1
                last["time"] = item["time"]
                last["ts"] = ts
                last["_sort_ts"] = ts
                continue
        entries.append(item)
    entries.sort(key=lambda item: item["_sort_ts"])
    feed = entries[-limit:]
    for item in feed:
        for private in ("_last_ts", "_sort_ts", "_agent", "_ts"):
            item.pop(private, None)
        hit = _row_risk({"kind": item["kind"], "tool": item["tool"],
                         "what": item["what"], "cmd": item.get("attempt")}, project, home) if item["kind"] in ("tool", "deny") else None
        if hit:
            item["risk"] = hit
    return feed


# ---- attention: ONE verdict per run, computed once (the page shows this and nothing else as the run's state)
#
# TRUTH TABLE. What the page may say about a run, the exact evidence that makes it true, and what happens without it.
# Evidence is read from the raw files; a sentence is never stronger than its evidence ("recorded" is not "measured").
#
#  assertion on screen        deterministic evidence                                          absent or contradicted
#  -------------------------  --------------------------------------------------------------  -------------------------------
#  Finished / Aborted + when  state.status done|aborted; time = state.updated                 -
#  Waiting for your answer    status awaiting_user, or the newest main row is an unanswered   awaiting_user but the main
#                             AskUserQuestion (activity row)                                   session kept working after the
#                                                                                              question: "working, recorded as
#                                                                                              waiting" (contradiction)
#  Waiting for your reply     newest main row is a Stop row ("turn ended") written by a        a later row of any agent, a
#                             bound session, no later row, no registered subagent working,     state change, a running subagent
#                             state not touched since                                          or a session-left Stop row: no claim
#  Waiting for your input     newest main row is a recorded permission / idle prompt row       later activity: no claim
#  Blocked                    status blocked (+ reason as recorded)                            fresh work after blocked.at:
#                                                                                              "working, recorded as blocked"
#  Paused                     status paused AND no work after the pause was recorded           a row newer than ~60 s (and newer
#                                                                                              than the recorded change), a bound
#                                                                                              session heartbeat, or a registered
#                                                                                              agent still alive: "working,
#                                                                                              recorded as paused"
#  Working (+ ago)            newest work row <= 90 s old, or registered subagents alive       -
#  Quiet (+ phase, dur)       measured gap since the newest row / state update, no running     never "waiting for you": only the
#                             command; says "may be thinking or running a long step"          measured gap and the phase
#  Long-running command       Bash row with no end row, older than the hung threshold          states the measured age; "can be
#                                                                                              normal for builds"
#  Silent session             no row and no state update for silent_seconds, no open command   hedged: "may have closed"
#  Report overdue             agent_done row, no later progress, no hand-back for dead_wait_s   measured age only
#  last signal / ages         server clock minus a row or state timestamp                      browser clock never used alone
#  task and gate counts       state.tasks[*].status, gates-*.json                              absent file: "no gates"
#  tokens                     the bound sessions' transcripts (read-only numbers)              absent log: "n/a"
#  session binding            ~/.orkestra/sessions/*.json whose run_id and project match       none: "no bound session"
#
# A person is only ever told to act when the evidence in the first column exists.

GROUP_RANK = {"you": 0, "stalled": 1, "running": 2, "paused": 3, "done": 4}
FRESH_S = 60.0                    # a work row this young means the agent is working right now
FUTURE_S = 120.0                  # a row further ahead than this is a clock error: it proves nothing
FRESH_MARGIN_S = 5.0              # work must come this much after the recorded status change to contradict it
WORK_KINDS = frozenset({"tool", "result", "answer", "deny", "end", "agent_done", "handback", "user"})
PROMPT_KINDS = frozenset({"permission", "permission_prompt", "idle_prompt", "notification"})
_TURN_ENDED = ("dispatch.turn_plain", "dispatch.turn_awaiting", "dispatch.turn_nudge_limit")
_TURN_LEFT = ("dispatch.turn_done", "dispatch.turn_status", "dispatch.turn_off_topic", "dispatch.turn_not_driving",
              "dispatch.turn_transcript_off_topic", "dispatch.turn_session_closed")


def _hum(sec: Optional[float]) -> str:
    """Compact duration for the page's sentences. Same words as `tempo.compact_dur`."""
    return T.compact_dur(sec)


def _hago(sec: Optional[float]) -> str:
    return "?" if sec is None else i18n.t("common.ago", dur=_hum(sec))


def _quote(text: str) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", text) + '"'


def _project_arg(project: Path) -> str:
    """`-p` value that resolves back to this project: its folder name when it sits directly in the workspace."""
    try:
        direct = (paths.workspace_root() / project.name).resolve() == Path(project).resolve()
    except OSError:
        direct = False
    return _quote(project.name if direct else str(project))


def _terminal_command(verb: str, project: Path, run_id: str) -> str:
    run = run_id if re.fullmatch(r"[\w.-]+", run_id or "") else _quote(run_id)
    return f"orkestra {verb} -p {_project_arg(project)} --run {run}"


def _resume_action(st: Dict[str, Any], project: Path) -> Tuple[str, str]:
    """(command, where) that continues a run: the slash command of its engine, or the terminal command when the run has
    no chat window to type into (headless / other engines)."""
    engine = st.get("engine") or "claude"
    if st.get("mode") == "headless" or engine not in ("claude", "cursor"):
        return _terminal_command("resume", project, str(st.get("run_id") or "")), "terminal"
    return ("/orkestra-resume", "cursor") if engine == "cursor" else ("/orkestra:resume", "claude")


def _who(agent: str) -> str:
    """The subject of a sentence: "The main session" for main (the bare short name reads as the word "mother" or "main"
    in a sentence), the agent's short name otherwise, first letter capital."""
    name = i18n.t("monitor.who_main") if (agent or "main") == "main" else A.short_agent(agent)
    return name[:1].upper() + name[1:]


def _hung_command(rows: List[Dict[str, Any]], include_wait: bool = False) -> Tuple[str, str]:
    """(agent key, command) of the longest-running Bash call that has no end row (the one the stall verdict names)."""
    best: Optional[Tuple[float, str, str]] = None
    for agent, ts, what in T._running_commands(rows):
        if agent == "main" and T.WAIT_CMD_RE.search(what) and not include_wait:
            continue
        if best is None or ts < best[0]:
            best = (ts, agent, what)
    return (best[1], best[2]) if best else ("main", "")


def _idle_meaning(what: str) -> str:
    """What a Stop row (kind idle) says, matched against what the hooks write in either language: `ended` (the session
    finished its turn and still drives the run), `left` (it unbound or the run closed) or `unknown` (not a text the hooks
    write: no claim is made from it)."""
    text = str(what or "").strip()
    if not text:
        return "ended"
    for names, meaning in ((_TURN_ENDED, "ended"), (_TURN_LEFT, "left")):
        for key in names:
            for code in ("en", "tr"):
                template = i18n.catalog(code).get(key)
                if template and _template_re(template).fullmatch(text):
                    return meaning
    return "unknown"


def _facts(st: Dict[str, Any], rows: List[Dict[str, Any]], now: float, state_ts: Optional[float] = None,
           pending: Optional[List[Dict[str, Any]]] = None, beats: Optional[Dict[str, float]] = None,
           wd: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The deterministic evidence of the table above, measured once. Nothing here is a guess about a person."""
    wd = wd or T.wd_cfg()
    pending = pending or []
    beats = beats or {}
    bound = set(beats)
    work_ts = main_work_ts = 0.0
    activity_ts = max([float(r.get("ts") or 0) for r in rows if r.get("kind") != "nudge"] or [0.0])
    for r in rows:
        if r.get("kind") not in WORK_KINDS:
            continue
        ts = float(r.get("ts") or 0)
        agent = str(r.get("agent") or "main")
        sid = str(r.get("sid") or "")
        if agent == "main" and bound and sid and sid not in bound:
            continue                                    # a session that no longer drives this run is not its heartbeat
        work_ts = max(work_ts, ts)
        if agent == "main":
            main_work_ts = max(main_work_ts, ts)
    main = [r for r in rows if (r.get("agent") or "main") == "main" and r.get("kind") not in A.ASIDE_KINDS]
    last_main = main[-1] if main else None
    idle_ts = float(last_main.get("ts") or 0) if last_main and last_main.get("kind") == "idle" else 0.0
    hb = max(beats.values() or [0.0])
    if idle_ts and hb <= idle_ts + 3:
        hb = 0.0                                        # the Stop hook itself refreshes the heartbeat: not new work
    agents_alive = bool(pending) and T.agents_quiet(rows, pending, now, wd) is None
    cmd = wait_cmd = None
    for agent, ts, what in T._running_commands(rows):
        if agent == "main" and T.WAIT_CMD_RE.search(what):
            if wait_cmd is None or ts < wait_cmd[1]:
                wait_cmd = (agent, ts, what)
            continue
        if cmd is None or ts < cmd[1]:
            cmd = (agent, ts, what)
    later = any(float(r.get("ts") or 0) > idle_ts and r.get("kind") in WORK_KINDS for r in rows) if idle_ts else False
    meaning = _idle_meaning(str((last_main or {}).get("what") or "")) if idle_ts else ""
    # another bound session that was heard from after this Stop row is still attached: one session's Stop is not the run's
    idle_sid = str((last_main or {}).get("sid") or "")
    other_live = bool(idle_ts and idle_sid) and any(
        sid != idle_sid and b > idle_ts + 3 and -5 <= now - b <= FRESH_S for sid, b in beats.items())
    turn_ended = bool(idle_ts) and meaning == "ended" and not later and not agents_alive and not pending \
        and (state_ts is None or state_ts <= idle_ts) and not other_live
    # the session unbound itself (or the run closed under it): no one is attached unless something newer says otherwise
    attached = any(b > idle_ts + 3 and now - b <= 300 for b in beats.values()) if idle_ts else False
    left = bool(idle_ts) and meaning == "left" and not later and not agents_alive and not pending and not attached
    prompt_ts = float(last_main.get("ts") or 0) if last_main and last_main.get("kind") in PROMPT_KINDS else 0.0
    return {"work_ts": work_ts, "main_work_ts": main_work_ts, "hb_ts": hb, "agents_alive": agents_alive,
            "activity_ts": activity_ts, "wait_cmd": wait_cmd, "left": left,
            "cmd": cmd, "idle_ts": idle_ts, "turn_ended": turn_ended, "prompt_ts": prompt_ts, "state_ts": state_ts,
            "last_main": last_main}


def _alive_after(facts: Dict[str, Any], mark: Optional[float], now: float, main_only: bool = False,
                 with_agents: bool = True) -> Optional[Tuple[str, float]]:
    """Proof that work is happening NOW although a status was recorded at `mark`: ("row", ts) for a work row younger than
    FRESH_S and newer than the record, or ("agents", 0) for registered subagents that still show life. A session heartbeat
    is NOT proof: opening or clearing a chat bound to the run touches it without any work (see `_beat_after`)."""
    ts = facts["main_work_ts"] if main_only else facts["work_ts"]
    if ts and 0 <= now - ts <= FRESH_S and (not mark or ts > mark + FRESH_MARGIN_S):
        return "row", ts
    if with_agents and facts["agents_alive"]:
        return "agents", 0.0
    return None


def _beat_after(facts: Dict[str, Any], mark: Optional[float], now: float) -> Optional[float]:
    """A fresh session heartbeat newer than the record: a fact worth one clause, never a claim of work."""
    hb = facts["hb_ts"]
    return hb if hb and 0 <= now - hb <= FRESH_S and (not mark or hb > mark + FRESH_MARGIN_S) else None


def _status_at(st: Dict[str, Any], status: str, state_ts: Optional[float]) -> Optional[float]:
    key = {"awaiting_user": "awaiting", "blocked": "blocked"}.get(status)
    at = T._ts((st.get(key) or {}).get("at")) if key and isinstance(st.get(key), dict) else None
    return at or state_ts


def _attention(st: Dict[str, Any], live: Dict[str, Any], stall: Dict[str, Any], agents: List[str], project: Path,
               rows: Optional[List[Dict[str, Any]]] = None, now: Optional[float] = None,
               last_ts: float = 0.0, run_seconds: float = 0.0, facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The single answer to "what is this run doing and what must I do": group (you | stalled | running | paused | done),
    a language-free `code`, and the sentences for a person. First matching rule wins; the model-facing wording of the
    legacy `live` / `stall` texts is never used here. `facts` (see `_facts`) is the measured evidence; without it only the
    rows given are read. Idleness is measured from the last activity ROW: a write to state.json (a CLI command, a phase
    change, another session's pause or resume) is a record change, not agent activity. See the truth table above."""
    now = time.time() if now is None else now
    rows = rows or []
    legacy = facts is None
    facts = facts or _facts(st, rows, now)
    status = st.get("status")
    key, cls = stall.get("key"), int(stall.get("cls") or 0)
    level = live.get("level")
    cursor = (st.get("engine") or "claude") == "cursor"
    headless = st.get("mode") == "headless"           # `claude -p`: it exits with its turn, there is no window to type into
    window = "cursor" if cursor else "claude"
    surface = "headless" if headless else window
    engine_name = "Cursor" if cursor else "Claude"
    since: Optional[float] = stall.get("since")
    idle = max(0.0, now - last_ts) if last_ts else None
    act_ts = float(facts.get("activity_ts") or 0.0)
    gap = max(0.0, now - act_ts) if act_ts else None    # time since the last activity row (None: no row at all)
    command, where = "", "none"
    group, code = "running", "working"
    phase_word = _word("monitor.phase_", st.get("phase"), str(st.get("phase")))
    state_ts = facts.get("state_ts")
    open_q = A.pending_question(rows)
    ended = key == "turn_ended" if legacy else bool(facts["turn_ended"])
    cmd = facts["cmd"]
    wait_cmd = facts.get("wait_cmd")
    cmd_age = now - cmd[1] if cmd else None
    hung_s = float(T.wd_cfg().get("hung_command_seconds") or 900)
    long_wait = bool(wait_cmd) and now - wait_cmd[1] > 3 * hung_s     # a main-session `sleep` that long is no agent wait
    if agents and facts.get("agents_alive"):
        eff = "run"
    elif gap is not None:
        eff = "run" if gap < 90 else "quiet" if gap < 900 else "stale"
    else:
        eff = "stale" if level == "stale" else "quiet" if level in ("quiet", "wait") else "run"
    record_note = ""
    if gap is not None and state_ts and state_ts > act_ts + FRESH_MARGIN_S and now - state_ts < gap:
        record_note = " " + i18n.t("monitor.attn_record_changed", ago=_hago(max(0.0, now - state_ts)))
    proof: Optional[Tuple[str, float]] = None
    mark = _status_at(st, str(status), state_ts)
    if status in ("paused", "blocked", "awaiting_user") and not (status == "awaiting_user" and open_q):
        proof = _alive_after(facts, mark, now, main_only=status == "awaiting_user", with_agents=status != "awaiting_user")
    head_key = ""
    do_surface = surface

    if status in ("done", "aborted"):
        group, code, since = "done", str(status), None
        fin = max(0.0, now - float(state_ts)) if state_ts else None
        took = run_seconds >= 1
        if status == "aborted":
            what = i18n.t("monitor.attn_aborted_what", ago=_hago(fin)) if fin is not None \
                else i18n.t("monitor.attn_aborted_what_bare")
        elif took and fin is not None:
            what = i18n.t("monitor.attn_done_what", dur=_hum(run_seconds), ago=_hago(fin))
        elif took:
            what = i18n.t("monitor.attn_done_what_bare", dur=_hum(run_seconds))
        else:
            what = i18n.t("monitor.attn_done_what_ago", ago=_hago(fin)) if fin is not None \
                else i18n.t("monitor.attn_done_what_none")
    elif proof:
        group, code, since = "running", "recorded", None
        if proof[0] == "row":
            why = i18n.t("monitor.attn_proof_row", ago=_hago(now - proof[1]))
        else:
            why = i18n.t("monitor.attn_proof_agents", who=", ".join(agents[:4]) or i18n.t("monitor.attn_proof_agents_anon"))
        what = i18n.t(f"monitor.attn_recorded_{status}_what", proof=why)
    elif status == "awaiting_user" or (key == "user" and open_q):
        group, code, where = "you", "question", window
        q = (st.get("awaiting") or {}).get("question") if status == "awaiting_user" else open_q
        text = str(q or open_q or "").strip()
        what = i18n.t("monitor.attn_question_what", question=text) if text else i18n.t("monitor.attn_question_empty")
        if since is None and status == "awaiting_user" and state_ts:
            since = max(0.0, now - float(state_ts))
    elif status == "blocked":
        group, code, where = "you", "blocked", "terminal"
        reason = str((st.get("blocked") or {}).get("reason") or "").strip()
        what = i18n.t("monitor.attn_blocked_what", reason=reason) if reason else i18n.t("monitor.attn_blocked_empty")
        command = _terminal_command("unblock", project, str(st.get("run_id") or ""))
    elif status == "paused":
        group, code, since = "paused", "paused", None
        last = i18n.t("monitor.attn_last_activity", ago=_hago(gap)) if gap is not None else i18n.t("monitor.attn_no_activity")
        beat = _beat_after(facts, mark, now)
        if beat:
            last += " " + i18n.t("monitor.attn_paused_beat", ago=_hago(max(0.0, now - beat)))
        if cmd:        # a command that never reported its end may still be running in the terminal
            what = i18n.t("monitor.attn_paused_what_cmd", ago=_hago(cmd_age), last=last)
            do_surface = "cmd"
        else:
            what = i18n.t("monitor.attn_paused_what", last=last)
        command, where = _resume_action(st, project)
    elif open_q:
        group, code, where = "you", "question", window
        what = i18n.t("monitor.attn_question_what", question=str(open_q).strip()) if str(open_q).strip() \
            else i18n.t("monitor.attn_question_empty")
    elif facts["prompt_ts"]:
        group, code, where, since = "you", "prompt", window, max(0.0, now - facts["prompt_ts"])
        what = i18n.t("monitor.attn_prompt_what", ago=_hago(since))
    elif ended:
        group, code, where = "you", "turn_ended", window
        if not legacy:
            since = max(0.0, now - facts["idle_ts"])
        if headless:       # the process exited with its turn: nobody can type a reply anywhere
            head_key = "monitor.attn_turn_ended_head_headless"
            what = i18n.t("monitor.attn_turn_ended_what_headless", ago=_hago(since))
        elif st.get("approval_mode") == "autopilot":   # nothing to approve: any short chat message resumes it
            head_key = "monitor.attn_turn_ended_head_auto"
            what = i18n.t("monitor.attn_turn_ended_what_auto", engine=engine_name, ago=_hago(since))
        else:
            what = i18n.t("monitor.attn_turn_ended_what", engine=engine_name, ago=_hago(since))
    elif cls == 3 or long_wait or (eff == "stale" and cmd and cmd_age is not None and cmd_age > 0 and cls != 2):
        group, code, where = "stalled", "hung", "terminal"
        agent, cmd_text = _hung_command(rows, include_wait=long_wait and not cmd)
        if cls != 3 and (cmd or long_wait):
            since = cmd_age if cmd else now - wait_cmd[1]
        what = i18n.t("monitor.attn_hung_what", agent=_who(agent),
                      cmd=short_what("Bash", cmd_text, str(project), _home(), 80), ago=_hago(since))
    elif cls == 2:
        group, code = "stalled", "dead_wait"
        main_waits = any(a == "main" and T.WAIT_CMD_RE.search(w) for a, _, w in T._running_commands(rows))
        what = i18n.t("monitor.attn_dead_wait_what_cmd" if main_waits else "monitor.attn_dead_wait_what_report",
                      ago=_hago(since))
        command, where = _resume_action(st, project)
    elif facts.get("left") and not cmd:
        group, code, since = "stalled", "detached", max(0.0, now - facts["idle_ts"])
        what = i18n.t("monitor.attn_detached_what", ago=_hago(since))
        command, where = _resume_action(st, project)
    elif cls == 4:
        group, code = "stalled", "silent"
        if not rows:
            since = run_seconds if run_seconds >= 1 else since
            what = i18n.t("monitor.attn_silent_what_never", dur=_hum(run_seconds))
        else:
            if gap is not None:
                since = gap
            what = i18n.t("monitor.attn_silent_what", dur=_hum(since)) + record_note
        command, where = _resume_action(st, project)
    elif eff == "stale" and not (wait_cmd and not long_wait):
        group, code, since = "stalled", "stale", gap if gap is not None else idle
        what = i18n.t("monitor.attn_stale_what", dur=_hum(since)) + record_note
        command, where = _resume_action(st, project)
    elif eff != "run" and (cmd or wait_cmd):
        # an open command call is not quiet: say what the record shows instead of "the agent may be thinking"
        group, code, since = "running", "working", None
        what = i18n.t("monitor.attn_working_cmd_what", ago=_hago(now - (cmd or wait_cmd)[1]))
    elif eff == "quiet":
        # "wait" here is the legacy guess about the human (a talk phase with no activity): without evidence it is only a gap
        group, code, since = "running", "quiet", gap if gap is not None else idle
        what = i18n.t("monitor.attn_quiet_what", dur=_hum(since), phase=phase_word) + record_note
    elif agents:
        group, code, since = "running", "agents", None
        what = i18n.t("monitor.attn_agents_what", who=", ".join(agents[:4]))
    else:
        group, code, since = "running", "working", None
        what = i18n.t("monitor.attn_working_what", ago=_hago(gap)) if gap is not None \
            else i18n.t("monitor.attn_working_state_what", ago=_hago(idle))
    if headless and code in ("question", "turn_ended", "prompt"):
        where = "terminal"
        command = _resume_action(st, project)[0]
    if code in ("question", "turn_ended", "prompt", "dead_wait"):
        do_key = f"monitor.attn_{code}_do_{surface}"
    elif code == "paused":
        do_key = "monitor.attn_paused_do_cmd" if do_surface == "cmd" else "monitor.attn_paused_do"
    elif code in ("blocked", "hung", "detached"):
        do_key = f"monitor.attn_{code}_do"
    elif code in ("silent", "stale"):
        do_key = "monitor.attn_silent_do"
    elif code == "recorded":
        do_key = f"monitor.attn_recorded_{status}_do"
    else:
        do_key = ""
    since_s = int(since) if since is not None and since >= 0 else None
    return {"group": group, "code": code, "rank": GROUP_RANK[group], "level": level, "since": since_s,
            "since_ts": round(now - since_s, 3) if since_s is not None else None,
            "head": i18n.t(head_key or (f"monitor.attn_{code}_head" if code != "recorded" else f"monitor.attn_recorded_{status}_head")),
            "what": _cap(what), "do": i18n.t(do_key) if do_key else "", "command": command, "where": where,
            # additive: the newest activity row, apart from the record's own write time (the page labels each honestly)
            "activity_ts": round(act_ts, 3) if act_ts else None}


def _home() -> str:
    return os.path.expanduser("~")


# ---- notices: loops and review rounds in words for a person (the model-facing `flags` stay as they were)

def _notice(code: str, severity: str, **kw: Any) -> Dict[str, str]:
    return {"code": code, "severity": severity, "text": i18n.t(f"monitor.notice_{code}", **kw)}


def _notices(st: Dict[str, Any], rows: List[Dict[str, Any]], project: Path, wd: Dict[str, Any]) -> List[Dict[str, str]]:
    if st.get("status") not in OPEN_STATUSES:
        return []
    out: List[Dict[str, str]] = []
    tasks = st.get("tasks") or {}
    for tid in sorted(tasks):
        t = tasks[tid]
        if t.get("status") in T.OPEN_TASK and T.reopen_count(t) >= T.MAX_REVIEW_ROUNDS - 1:
            out.append(_notice("review_rounds", "warn", task=tid, n=T.reopen_count(t) + 1))
    in_prog = sorted(tid for tid, t in tasks.items() if t.get("status") == "in_progress")
    for i, tid in enumerate(in_prog):
        for other, shared in T.overlap(tasks, tid, in_prog[i + 1:]):
            out.append(_notice("overlap", "info", a=tid, b=other, file=shared[0]))
    repeats = int(wd.get("loop_repeats") or 3)
    recent = rows[-120:]
    denies: Dict[Tuple[str, str], int] = {}
    last_deny: Dict[Tuple[str, str], float] = {}
    last_ok: Dict[str, float] = {}                    # per agent: its newest row that is not a refusal
    for r in recent:
        agent = str(r.get("agent") or "main")
        ts = float(r.get("ts") or 0)
        if r.get("kind") == "deny":
            k = (agent, str(r.get("what") or "")[:90])
            denies[k] = denies.get(k, 0) + 1
            last_deny[k] = max(last_deny.get(k, 0.0), ts)
        elif r.get("kind") in ("tool", "result", "answer", "end"):
            last_ok[agent] = max(last_ok.get(agent, 0.0), ts)
    for (agent, what), n in denies.items():
        # a loop that the agent has since got past is history, not a warning: only a refusal newer than its last success
        if n >= repeats and last_deny[(agent, what)] >= last_ok.get(agent, 0.0):
            out.append(_notice("guard_loop", "warn", agent=_who(agent), n=n,
                               what=_trim(_deny_parts(what)[1] or what, 70)))
    last_cmd: Dict[str, Tuple[str, int]] = {}
    for r in recent:
        if r.get("kind") == "tool" and r.get("tool") == "Bash":
            agent, what = str(r.get("agent") or "main"), str(r.get("what") or "")
            prev = last_cmd.get(agent)
            last_cmd[agent] = (what, prev[1] + 1) if prev and prev[0] == what else (what, 1)
        elif r.get("kind") == "tool":
            last_cmd.pop(str(r.get("agent") or "main"), None)
    for agent, (what, n) in last_cmd.items():
        if n >= repeats and what:
            out.append(_notice("same_command", "info", agent=_who(agent), n=n,
                               what=short_what("Bash", what, str(project), _home(), 60)))
    return out


# ---- phases

def _phase_history(st: Dict[str, Any], now: float, closed_at: Optional[float]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every phase the run entered with the seconds spent in it, and the phases below the current one it never entered
    (only when the history reaches back to the first phase: older runs report none)."""
    hist = [(str(h.get("to") or ""), _parse_iso(h.get("at", ""))) for h in (st.get("history") or []) if isinstance(h, dict)]
    hist = [(name, at) for name, at in hist if name and at]
    end = closed_at if closed_at else now
    out = []
    for i, (name, at) in enumerate(hist):
        nxt = hist[i + 1][1] if i + 1 < len(hist) else end
        out.append({"phase": name, "at": at, "seconds": int(max(0.0, float(nxt or end) - float(at)))})
    skipped: List[str] = []
    phase = st.get("phase", "intake")
    if hist and hist[0][0] == PHASES[0] and phase in PHASES:
        entered = {name for name, _ in hist}
        skipped = [p for p in PHASES[:PHASES.index(phase)] if p not in entered]
    return out, skipped


def _honest_live(live: Dict[str, Any], attention: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy one-line `live` verdict says "probably WAITING FOR YOU" for a talk phase with no recent rows and says
    "paused" for a recorded status; neither is evidence (see the truth table). Where the dashboard verdict contradicts
    it, `watch` and the JSON carry the dashboard's measured sentence instead (same level vocabulary)."""
    if attention.get("code") == "recorded":
        return {"level": "run", "text": attention["what"]}
    if attention.get("code") == "detached":              # the Stop row says "reply in that window": the session left instead
        return {"level": "stale", "text": attention["what"]}
    if attention.get("where") == "terminal" and attention.get("code") in ("turn_ended", "prompt"):
        return {"level": "wait", "text": attention["what"]}        # headless: there is no window to reply in
    if live.get("level") == "wait" and attention.get("code") == "quiet":
        return {"level": "quiet", "text": attention["what"]}
    return live


def _clean_rows(rows: List[Any], now: float) -> List[Dict[str, Any]]:
    """Activity rows a verdict can rely on: dicts whose time is a finite number that is not in the far future and whose text
    fields are strings. A row that fails these keeps its place but carries time 0 (it proves nothing about now)."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean = dict(row)
        try:
            ts = float(clean.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        clean["ts"] = ts if math.isfinite(ts) and 0 < ts <= now + FUTURE_S else 0.0
        for field in ("kind", "agent", "tool", "what"):
            if not isinstance(clean.get(field), str):
                clean[field] = "" if clean.get(field) is None or field != "agent" else "main"
        if not clean["agent"]:
            clean["agent"] = "main"
        out.append(clean)
    return out


def collect(project: Path, st: Dict[str, Any], mtime: float = 0.0, n_activity: int = 14) -> Dict[str, Any]:
    rid = st["run_id"]
    engine = _driving_engine(project, rid, str(st.get("engine") or ""))
    if engine and engine != (st.get("engine") or ""):
        st = dict(st, engine=engine)          # do not rewrite the run file; the card names the live driver
    now = time.time()
    rdir = paths.run_dir(project, rid)
    rows = _clean_rows(A.read_rows(project, rid), now)
    tasks = st.get("tasks") or {}
    tlist = []
    by_status = {"done": 0, "in_progress": 0, "blocked": 0, "pending": 0, "deferred": 0}
    show_tests = False
    for tid in sorted(tasks):
        t = tasks[tid]
        red, green = bool((t.get("red") or {}).get("ok")), bool((t.get("green") or {}).get("ok"))
        tdd = t.get("tdd") or "required"
        needs_tests = tdd != "n/a"
        if needs_tests or red or green:
            show_tests = True
        if red and green:
            tests = "passed"
        elif red:
            tests = "awaiting_green"
        elif not needs_tests:
            tests = "na"  # column hidden when every task is n/a; blank cell if mixed
        else:
            tests = "none"
        tlist.append({"id": tid, "status": t.get("status"), "title": t.get("title") or "", "tdd": tdd,
                      "agent": t.get("agent") or "", "evidence": ("R" if red else "-") + ("G" if green else "-"),
                      "tests": tests})
        by_status[t.get("status") if t.get("status") in by_status else "pending"] += 1
    done = sum(1 for t in tlist if t["status"] in ("done", "deferred"))
    hist = st.get("history") or []
    phase_since = _parse_iso(hist[-1].get("at", "")) if hist else None
    ledger = [ln[2:] for ln in read_text(paths.feature_dir(project, rid) / "ledger.md").splitlines()
              if ln.startswith("- ")][-4:]
    home = _home()
    feed = _activity_feed(rows, n_activity, compact=False, project=str(project), home=home)
    feed_page = _activity_feed(rows, n_activity, project=str(project), home=home)
    risks, risk_counts = _risks(rows, str(project), home)
    phase = st.get("phase", "intake")
    state_updated = _parse_iso(st.get("updated")) or mtime
    sessions = _sessions_for(project, rid)
    created = _parse_iso(st.get("created", ""))
    usage = U.for_sessions([s["id"] for s in sessions], since=created - 120 if created else None,
                           windows=T.phase_windows(st, now))
    if not usage and isinstance(st.get("usage"), dict):
        usage = dict(st["usage"], final=True)       # closed run: the numbers saved when it finished
    wd = T.wd_cfg()
    is_open = st.get("status") in OPEN_STATUSES
    pending = A.agents_pending(project, rid, max_age=float(wd["agent_max_minutes"]) * 60) if is_open else []
    stall = T.classify(st, rows, pending, wd=wd, state_mtime=state_updated,
                       reports_due=(A.agents_reports_due(project, rid, max_age=3600.0) or None) if is_open else None)   # no registry: row-based
    tm = T.timings(st)
    try:
        flags = T.flags(project, st, rows, wd) if is_open else []
    except Exception:
        flags = []
    live = A.liveness(st, rows, state_mtime=state_updated, pending=pending)
    facts = _facts(st, rows, now, state_updated or None, pending, _session_beats(project, rid), wd)
    agents = [A.short_agent(str(a.get("type") or "")) for a in pending] or [A.short_agent(a) for a in A.running_agents(rows)]
    activity_ts = max([float(r.get("ts") or 0) for r in rows] or [0.0])
    last_ts = max(activity_ts, float(state_updated or 0))
    phase_history, phases_skipped = _phase_history(st, now, None if is_open else state_updated)
    attention = _attention(st, live, stall, agents, project, rows, now, last_ts, tm["run"], facts)
    live = _honest_live(live, attention)
    gates_struct = _gate_struct(rdir)
    return {
        "project": str(project), "project_name": project.name, "run_id": rid, "title": st.get("title", ""),
        "size": st.get("size", ""), "mode": st.get("mode", ""), "engine": st.get("engine") or "claude", "phase": phase,
        # `*_label`: display name in the current language. `*_tr`: the same in Turkish, always (what older consumers,
        # e.g. the OpenClaw plugin, read); the machine values are `phase` / `status`.
        "phase_label": _word("monitor.phase_", phase, phase), "phase_tr": _word("monitor.phase_", phase, phase, "tr"),
        "phase_index": PHASES.index(phase) if phase in PHASES else 0,
        "phase_since": phase_since, "status": st.get("status"),
        "status_label": _word("monitor.status_", st.get("status"), ""),
        "status_tr": _word("monitor.status_", st.get("status"), "", "tr"),
        "approved": bool(st.get("approved")), "live": live,
        "agents": agents,
        "stall": stall, "flags": flags,
        "timing": {"run": int(tm["run"]), "phases": {k: int(v) for k, v in tm["phases"].items()},
                   "task_avg": int(tm["task_avg"]) if tm.get("task_avg") else None, "reopens": tm["reopens"],
                   "tasks_reopened": tm["tasks_reopened"]},
        "tasks": tlist, "tasks_done": done, "tasks_total": len(tlist),
        "tasks_show_tests": show_tests,
        "current_tasks": [t for t in tlist if t["status"] == "in_progress"],
        "gates": _gate_summary(rdir), "activity": feed, "ledger": ledger,
        "sessions": [{"session": s["session"], "bound_at": s["bound_at"]} for s in sessions], "usage": usage,
        "updated": st.get("updated", ""),
        "artifacts": str(paths.feature_dir(project, rid)),
        # ---- additive (the dashboard page reads these; every key above is unchanged)
        "key": _view_key(project, rid),
        "now": round(now, 3), "home": _home(),
        "attention": attention,
        "last_event_ts": activity_ts or None, "state_updated_ts": float(state_updated) if state_updated else None,
        "phase_history": phase_history, "phases_skipped": phases_skipped,
        "tasks_by_status": by_status, "gates_struct": gates_struct,
        "notices": _notices(st, rows, project, wd),
        # the page's feed: identical consecutive rows folded into one with a count (`activity` stays the plain list)
        "activity_feed": feed_page,
        "risks": risks, "risk_counts": risk_counts,
        # the run service's job that drives this run (headless), and the run documents a person can open
        "job": CO.job_view(CO.job_for(project, rid)), "docs": CO.docs_of(project, rid),
    }


# ---------------------------------------------------------------- terminal

class _C:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


LEVEL_COLOR = {"run": "32", "wait": "1;33", "quiet": "36", "stale": "31", "blocked": "1;31", "paused": "35",
               "done": "2"}
LEVEL_DOT = {"run": "●", "wait": "◆", "quiet": "○", "stale": "✗", "blocked": "■", "paused": "Ⅱ", "done": "✓"}
KIND_MARK = {"tool": " ", "deny": "!", "result": "←", "agent_done": "✓", "answer": "✎", "user": "»", "idle": "⏸",
             "nudge": "↻", "bind": "⚓", "handback": "⇠", "wait_agents": "⏳", "status": "≡"}
STALL_CLASSES = (2, 3, 4)            # dead wait · hung command · silent session: the ones worth a banner


def _dur(sec: Optional[float]) -> str:
    return "" if sec is None else _hum(sec)


ROW_LABELS = tuple(f"monitor.label_{name}" for name in
                   ("phase", "status", "loop", "pace", "clock", "tasks", "gates", "sessions", "tokens", "ledger"))


def _label_width() -> int:
    """Column width of the row labels of the terminal view: the longest one in the current language plus a space, so the
    values line up (Turkish: 7, the width the view always had)."""
    return max(len(i18n.t(key)) for key in ROW_LABELS) + 1


def _enable_ansi() -> None:
    """Windows consoles interpret colour / cursor escapes only once virtual-terminal mode is on; running an empty
    command through the shell switches it on for this console (a no-op everywhere else)."""
    if sys.platform == "win32":
        try:
            os.system("")
        except Exception:
            pass


def _emit(text: str, stream: Any = None) -> None:
    """Write to the terminal even when its encoding (a legacy Windows code page, ASCII under LANG=C) cannot hold every
    character of the view: those become '?' instead of ending `orkestra watch` with a traceback."""
    out = stream or sys.stdout
    try:
        out.write(text)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        out.write(text.encode(enc, "replace").decode(enc))


def _json_text(data: Any, stream: Any = None) -> str:
    """JSON for the terminal: readable (UTF-8 as is) unless the stream cannot encode it, then \\u-escaped (still valid)."""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    enc = getattr(stream or sys.stdout, "encoding", None)
    if enc:
        try:
            text.encode(enc)
        except (UnicodeEncodeError, LookupError):
            return json.dumps(data, ensure_ascii=True, indent=2)
    return text


def _tok(n: Any) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}K" if n < 1_000_000 else f"{n / 1_000_000:.2f}M"


def _clock_body(v: Dict[str, Any]) -> str:
    """Total and each phase as `discovery: 3m (10k)`. The token mark is omitted when that phase was not measured."""
    tmg = v.get("timing") or {}
    try:
        run = int(tmg.get("run") or 0)
    except (TypeError, ValueError):
        run = 0
    phases = tmg.get("phases") if isinstance(tmg.get("phases"), dict) else {}
    usage = v.get("usage") if isinstance(v.get("usage"), dict) else {}
    counted = usage.get("phases") if isinstance(usage.get("phases"), dict) else {}
    parts = []
    for name in PHASES:
        if name not in phases:
            continue
        try:
            seconds = int(phases[name])
        except (TypeError, ValueError):
            continue
        tokens = counted.get(name) if name in counted else None
        parts.append(T.stage_text(i18n.t("monitor.phase_" + name), seconds, tokens))
    if run <= 0 and not parts:
        return ""
    total_tokens = usage.get("total") if "total" in usage else None
    total = T.compact_dur(run) + T.token_mark(total_tokens)
    if parts:
        return i18n.t("monitor.ui_clock", total=total, phases=" · ".join(parts))
    return i18n.t("monitor.ui_clock_total", total=total)


def render_text(views: List[Dict[str, Any]], color: bool = False, width: Optional[int] = None) -> str:
    c = _C(color)
    width = width or shutil.get_terminal_size((110, 40)).columns
    width = max(60, min(width, 160))
    lw = _label_width()

    def row(key: str) -> str:
        return " " + i18n.t(key).ljust(lw)

    def cut(s: str, n: int) -> str:
        return s if len(s) <= n else s[: max(0, n - 1)] + "…"

    if not views:
        return i18n.t("monitor.none_open", workspace=paths.workspace_display())
    out: List[str] = []
    now = time.time()
    for v in views:
        head = f"━━ {v['project_name']} · {v['run_id']} · \"{v['title']}\" ({v['size']}{', headless' if v['mode'] == 'headless' else ''}{', cursor' if v.get('engine') == 'cursor' else ''}) "
        out.append(c("1", cut(head, width) + "━" * max(0, width - len(head))))
        bar = []
        for i, ph in enumerate(PHASES):
            if i < v["phase_index"]:
                bar.append(c("2", ph))
            elif i == v["phase_index"]:
                bar.append(c("1;7", f" {ph} "))
            else:
                bar.append(ph)
        since = " · " + i18n.t("monitor.in_phase_for", dur=_dur(now - v["phase_since"])) if v.get("phase_since") else ""
        out.append(f"{row('monitor.label_phase')}{' › '.join(bar)}" + c("2", since))
        lv = v["live"]
        out.append(row("monitor.label_status") + c(LEVEL_COLOR.get(lv["level"], "0"), f"{LEVEL_DOT.get(lv['level'], '•')} {cut(lv['text'], width - 10)}"))
        stl = v.get("stall") or {}
        if stl.get("cls") in STALL_CLASSES:
            att = v.get("attention") or {}
            mark, text = i18n.t(f"monitor.stall_mark_{stl['cls']}"), stl.get("text", "")
            if att.get("group") == "stalled" and att.get("what"):
                text = att["what"]          # the page's measured sentence: the terminal adds no claim of its own
            out.append(c("1;31", cut(" " + i18n.t("monitor.stall_line", mark=mark, text=text), width)))
        for fl in (stl.get("loops") or [])[:2]:
            out.append(c("31", cut(f"{row('monitor.label_loop')}{fl}", width)))
        tmg = v.get("timing") or {}
        if tmg.get("run"):
            bits = [i18n.t("monitor.pace_total", dur=_dur(tmg["run"]))]
            if tmg.get("task_avg"):
                bits.append(i18n.t("monitor.pace_per_task", dur=_dur(tmg["task_avg"])))
            if tmg.get("reopens"):
                bits.append(i18n.t("monitor.pace_rejects", n=tmg["reopens"], tasks=tmg["tasks_reopened"]))
            out.append(c("2", cut(row("monitor.label_pace") + " · ".join(bits), width)))
        clock_body = _clock_body(v)
        if clock_body:
            out.append(c("2", cut(row("monitor.label_clock") + clock_body, width)))
        if v["tasks_total"]:
            cur = v["current_tasks"]
            cur_txt = " · " + i18n.t("monitor.tasks_now", list="; ".join(f"{t['id']} {t['evidence']} {t['title']}"
                                                                          for t in cur)) if cur else ""
            out.append(cut(f"{row('monitor.label_tasks')}"
                           f"{i18n.t('monitor.tasks_done', done=v['tasks_done'], total=v['tasks_total'])}{cur_txt}", width))
        g = v["gates"]
        if g.get("baseline") != "—" or g.get("verify") != "—":
            out.append(row("monitor.label_gates") + i18n.t("monitor.gates_line", baseline=g["baseline"], verify=g["verify"]))
        sess = v["sessions"]
        out.append(c("2", row("monitor.label_sessions")
                     + (i18n.t("monitor.sessions_bound", n=len(sess), ids=", ".join(s["session"] for s in sess)) if sess
                        else i18n.t("monitor.sessions_none"))))
        u = v.get("usage")
        if u:
            out.append(cut(row("monitor.label_tokens")
                           + i18n.t("monitor.tokens", input=_tok(u["input"]), output=_tok(u["output"]),
                                    cache_write=_tok(u["cache_write"]), cache_read=_tok(u["cache_read"]))
                           + (" · " + i18n.t("monitor.tokens_context", n=_tok(u["context"])) if u.get("context") else "")
                           + (" " + i18n.t("monitor.tokens_final") if u.get("final") else ""), width))
        if v["activity"]:
            out.append(" " + i18n.t("monitor.recent"))
            for r in v["activity"]:
                mark = KIND_MARK.get(r["kind"], " ")
                tool, what = r["tool"], r["what"]
                if r["kind"] == "deny":
                    count = int(r.get("repeat_count") or 1)
                    category = str(what).partition("]")[0].lstrip("[") if str(what).startswith("[") \
                        else i18n.t("monitor.guard_general")
                    tool = i18n.t("monitor.guard_n", count=count) if count > 1 else i18n.t("monitor.guard")
                    what = i18n.t("monitor.guard_what", category=category)
                line = f"   {r['time']} {cut(r['agent'], 11):<11} {mark} {cut(tool, 15):<15} {what}"
                colored = cut(line, width)
                if r["kind"] == "deny":
                    colored = c("33", colored)
                elif r["kind"] in ("idle", "user", "answer"):
                    colored = c("33", colored)
                elif r["kind"] in ("result", "agent_done", "handback", "wait_agents"):
                    colored = c("36", colored)
                out.append(colored)
        else:
            out.append(c("2", "   " + i18n.t("monitor.no_activity")))
        if v["ledger"]:
            out.append(c("2", cut(f"{row('monitor.label_ledger')}{v['ledger'][-1]}", width)))
        out.append("")
    return "\n".join(out)


def watch(project: Optional[Path], interval: float, once: bool, include_all: bool, as_json: bool) -> int:
    if as_json:
        _emit(_json_text(discover(project, include_all)) + "\n")
        return 0
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if once or not sys.stdout.isatty():          # agents / pipes get one snapshot, never an endless loop
        _emit(render_text(discover(project, include_all), color=color) + "\n")
        return 0
    if color:
        _enable_ansi()
    try:
        while True:
            body = render_text(discover(project, include_all), color=color)
            stamp = time.strftime("%H:%M:%S")
            header = i18n.t("monitor.watch_header", stamp=stamp, interval=f"{interval:g}")
            _emit("\033[H\033[2J" + _C(color)("2", header) + "\n\n" + body + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


# ---------------------------------------------------------------- web dashboard (read-only, 127.0.0.1)

# Catalog keys the page script reads as `L.<name>` (the key without its area prefix). The server sends them to the page
# as one JSON table, so the page holds no display text of its own: a language needs no change to this file.
UI_KEYS = (
    "monitor.stall_mark_2", "monitor.stall_mark_3", "monitor.stall_mark_4",
    "monitor.label_pace", "monitor.label_tokens", "monitor.label_ledger",
    "monitor.pace_total", "monitor.pace_per_task", "monitor.pace_rejects", "monitor.tasks_done", "monitor.gates_line",
    "monitor.tokens", "monitor.tokens_context", "monitor.tokens_final", "monitor.in_phase_for",
    "monitor.ui_clock", "monitor.ui_clock_total",
    "monitor.guard", "monitor.guard_n", "monitor.guard_what", "monitor.guard_general",
    "monitor.phase_intake", "monitor.phase_discover", "monitor.phase_clarify", "monitor.phase_plan",
    "monitor.phase_approve", "monitor.phase_implement", "monitor.phase_verify", "monitor.phase_review",
    "monitor.phase_deliver", "monitor.phase_done",
    "monitor.ui_stall", "monitor.ui_tasks", "monitor.ui_gates", "monitor.ui_sessions", "monitor.ui_no_sessions",
    "monitor.ui_tokens_none", "monitor.ui_tokens_tip", "monitor.ui_empty", "monitor.ui_offline", "monitor.ui_locale",
    "monitor.ui_stale", "monitor.ui_stale_cmd",
    "monitor.ui_conn_live", "monitor.ui_conn_updated", "monitor.ui_ago_now", "monitor.ui_conn_retry",
    "monitor.ui_conn_offline_chip", "monitor.ui_conn_offline", "monitor.ui_conn_back", "monitor.ui_as_of",
    "monitor.ui_render_error", "monitor.ui_reload", "monitor.ui_build", "monitor.ui_readonly", "monitor.ui_print",
    "monitor.ui_sum", "monitor.ui_f_all", "monitor.ui_g_you", "monitor.ui_g_stalled", "monitor.ui_g_running",
    "monitor.ui_g_paused", "monitor.ui_g_done", "monitor.ui_calm", "monitor.ui_no_match", "monitor.ui_clear",
    "monitor.ui_empty_h", "monitor.ui_empty_p", "monitor.ui_empty_claude", "monitor.ui_empty_claude_cmd",
    "monitor.ui_empty_term", "monitor.ui_empty_term_cmd",
    "monitor.ui_announce_you", "monitor.ui_announce_stalled", "monitor.ui_announce_calm", "monitor.ui_keys",
    "monitor.ui_copy", "monitor.ui_copied", "monitor.ui_copy_failed",
    "monitor.ui_age_waiting", "monitor.ui_age_stalled", "monitor.ui_phase_of",
    "monitor.ui_step_done", "monitor.ui_step_current", "monitor.ui_step_todo", "monitor.ui_step_skipped",
    "monitor.ui_t_progress", "monitor.ui_t_blocked", "monitor.ui_t_todo", "monitor.ui_t_all", "monitor.ui_t_hide",
    "monitor.ui_t_col_id", "monitor.ui_t_col_task", "monitor.ui_t_col_status", "monitor.ui_t_col_tests",
    "monitor.ui_ts_done", "monitor.ui_ts_in_progress", "monitor.ui_ts_blocked", "monitor.ui_ts_pending",
    "monitor.ui_ts_deferred",
    "monitor.ui_tests_passed", "monitor.ui_tests_awaiting_green",
    "monitor.ui_tests_none", "monitor.ui_tests_red_green", "monitor.ui_tests_red",
    "monitor.ui_gate_baseline", "monitor.ui_gate_verify",
    "monitor.ui_act", "monitor.ui_act_last", "monitor.ui_act_none", "monitor.ui_act_more", "monitor.ui_act_less",
    "monitor.ui_act_reads", "monitor.ui_act_times", "monitor.ui_act_blocked",
    "monitor.ui_show_full", "monitor.ui_hide_full", "monitor.ui_show_more", "monitor.ui_collapse",
    "monitor.ui_announce_lost", "monitor.ui_announce_offline", "monitor.ui_retry_now", "monitor.ui_conn_error",
    "monitor.ui_error_http", "monitor.ui_load_failed_h", "monitor.ui_load_failed_net", "monitor.ui_keys_btn",
    "monitor.ui_keys_toggle",
    "monitor.ui_k_run", "monitor.ui_k_edit", "monitor.ui_k_read", "monitor.ui_k_search", "monitor.ui_k_agent",
    "monitor.ui_k_question", "monitor.ui_k_answer", "monitor.ui_k_turn_ended", "monitor.ui_k_waiting",
    "monitor.ui_k_agent_done", "monitor.ui_k_result", "monitor.ui_k_report", "monitor.ui_k_user", "monitor.ui_k_other",
    "monitor.ui_details", "monitor.ui_tech", "monitor.ui_run_folder", "monitor.ui_last_signal", "monitor.ui_no_signal",
    "monitor.ui_fin_took", "monitor.ui_fin_ago", "monitor.ui_expand", "monitor.ui_fin_more",
    "common.ago", "tempo.dur_s", "tempo.dur_m", "tempo.dur_hm", "monitor.dur_h", "monitor.dur_d", "monitor.dur_dh",
    "monitor.ui_age_hung", "monitor.ui_announce_hung", "monitor.ui_record_changed", "monitor.ui_fin_aborted",
    "monitor.ui_ctl_pause", "monitor.ui_ctl_abort", "monitor.ui_ctl_pause_aria", "monitor.ui_ctl_abort_aria",
    "monitor.ui_ctl_pause_h", "monitor.ui_ctl_abort_h", "monitor.ui_ctl_pause_p", "monitor.ui_ctl_abort_p",
    "monitor.ui_ctl_reason", "monitor.ui_ctl_reason_ph", "monitor.ui_ctl_cancel", "monitor.ui_ctl_ok_pause",
    "monitor.ui_ctl_ok_abort", "monitor.ui_ctl_busy_pause", "monitor.ui_ctl_busy_abort", "monitor.ui_ctl_done_pause",
    "monitor.ui_ctl_done_abort", "monitor.ui_ctl_err_token", "monitor.ui_ctl_err_closed", "monitor.ui_ctl_err_not_found",
    "monitor.ui_ctl_err_reason_long", "monitor.ui_ctl_err_read_only", "monitor.ui_ctl_err_origin", "monitor.ui_ctl_err_net",
    "monitor.ui_ctl_err_failed", "monitor.ui_ctl_foot",
    "monitor.ui_risk_h", "monitor.ui_risk_sum", "monitor.ui_risk_p", "monitor.ui_risk_all", "monitor.ui_risk_less",
    "monitor.ui_risk_go", "monitor.ui_risk_go_aria", "monitor.ui_risk_by", "monitor.ui_risk_guard", "monitor.ui_risk_n",
    "risk.lvl_high", "risk.lvl_medium", "risk.lvl_blocked",
    "monitor.ui_new",
    "monitor.ui_tools",
    "monitor.ui_nr_h",
    "monitor.ui_nr_project",
    "monitor.ui_nr_project_none",
    "monitor.ui_nr_project_err",
    "monitor.ui_nr_engine",
    "monitor.ui_nr_engine_off",
    "monitor.ui_nr_engine_default",
    "monitor.ui_nr_engine_err",
    "monitor.ui_nr_mode",
    "monitor.ui_nr_mode_auto",
    "monitor.ui_nr_mode_man",
    "monitor.ui_nr_req",
    "monitor.ui_nr_req_ph",
    "monitor.ui_nr_req_n",
    "monitor.ui_nr_req_short",
    "monitor.ui_nr_req_long",
    "monitor.ui_nr_ok",
    "monitor.ui_nr_ok_err",
    "monitor.ui_nr_go",
    "monitor.ui_nr_busy",
    "monitor.ui_nr_cancel",
    "monitor.ui_nr_done",
    "monitor.ui_nr_loading",
    "monitor.ui_nr_noprojects",
    "monitor.ui_svc_down",
    "monitor.ui_svc_unset",
    "monitor.ui_svc_missing",
    "monitor.ui_svc_start",
    "monitor.ui_svc_starting",
    "monitor.ui_svc_up",
    "monitor.ui_svc_failed",
    "monitor.ui_cerr_down",
    "monitor.ui_cerr_refused",
    "monitor.ui_cerr_generic",
    "monitor.ui_cerr_closed",
    "monitor.ui_cerr_busy",
    "monitor.ui_cerr_nojob",
    "monitor.ui_job_h",
    "monitor.ui_job_via",
    "monitor.ui_job_status_queued",
    "monitor.ui_job_status_running",
    "monitor.ui_job_status_awaiting_user",
    "monitor.ui_job_status_blocked",
    "monitor.ui_job_status_paused",
    "monitor.ui_job_status_stalled",
    "monitor.ui_job_status_done",
    "monitor.ui_job_status_error",
    "monitor.ui_job_status_cancelled",
    "monitor.ui_job_none",
    "monitor.ui_job_turns",
    "monitor.ui_job_reply",
    "monitor.ui_job_reply_ph",
    "monitor.ui_job_send",
    "monitor.ui_job_sending",
    "monitor.ui_job_sent",
    "monitor.ui_job_reply_empty",
    "monitor.ui_job_reply_long",
    "monitor.ui_job_approve",
    "monitor.ui_job_approve_word",
    "monitor.ui_job_review",
    "monitor.ui_job_continue",
    "monitor.ui_job_continued",
    "monitor.ui_job_working",
    "monitor.ui_job_here",
    "monitor.ui_docs",
    "monitor.ui_doc_request",
    "monitor.ui_doc_context",
    "monitor.ui_doc_spec",
    "monitor.ui_doc_plan",
    "monitor.ui_doc_verification",
    "monitor.ui_doc_report",
    "monitor.ui_doc_loading",
    "monitor.ui_doc_cut",
    "monitor.ui_doc_close",
    "monitor.ui_doc_open",
    "monitor.ui_tl_svc_h",
    "monitor.ui_tl_jobs_h",
    "monitor.ui_tl_jobs_none",
    "monitor.ui_tl_job_cancel",
    "monitor.ui_tl_job_confirm",
    "monitor.ui_tl_job_cancelled",
    "monitor.ui_tl_clear_h",
    "monitor.ui_tl_clear_p",
    "monitor.ui_tl_hours",
    "monitor.ui_tl_hours_err",
    "monitor.ui_tl_preview",
    "monitor.ui_tl_none",
    "monitor.ui_tl_would",
    "monitor.ui_tl_apply",
    "monitor.ui_tl_applied",
    "monitor.ui_tl_idle",
    "monitor.ui_tl_doc_h",
    "monitor.ui_tl_doc_p",
    "monitor.ui_tl_doc_run",
    "monitor.ui_tl_doc_busy",
    "monitor.ui_tl_doc_ok",
    "monitor.ui_tl_doc_bad",
)

# The page: one self-contained document (inline CSS and script, no external request: the CSP below forbids them). The
# server fills the __TOKENS__ per request (language, build, static texts, the label table, META); the script keeps the
# DOM keyed per run, so a poll never rebuilds what is already there. User and run text only ever reaches the DOM as
# text (textContent), never as markup. The source is ASCII: every glyph is a \u escape.
PAGE = r"""<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>__TITLE__</title>
<style>
:root{--bg:#f3f5f9;--surface:#ffffff;--ink:#0f172a;--ink2:#3f4a5c;--line:#dfe4ec;--ctl:#6b7588;--focus:#2457d6;
--you-fg:#8a4300;--you-bg:#fff4e0;--you-edge:#c26a06;--stall-fg:#a11a1a;--stall-bg:#fdecec;--stall-edge:#d32f2f;
--run-fg:#0b6b3a;--run-bg:#e7f6ee;--run-edge:#1f9254;--pause-fg:#5136b0;--pause-bg:#f0ecfd;--pause-edge:#6d50d6;
--done-fg:#465062;--done-bg:#edf0f4;--done-edge:#7c8597;--brand:#2457d6;--brand2:#7c4ddb;--on-brand:#ffffff;
--shadow:0 1px 2px rgba(15,23,42,.05),0 2px 10px rgba(15,23,42,.06);--shadow-h:0 2px 4px rgba(15,23,42,.07),0 8px 24px rgba(15,23,42,.10);
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0a0e17;--surface:#121826;--ink:#e8ecf4;--ink2:#b3bccb;
--line:#283244;--ctl:#8a94a7;--focus:#8db4ff;--you-fg:#ffc670;--you-bg:#2a1f0a;--you-edge:#e39431;--stall-fg:#ffa3a3;
--stall-bg:#331517;--stall-edge:#f05a57;--run-fg:#72e4a7;--run-bg:#0f2a1d;--run-edge:#38c675;--pause-fg:#cbbcff;
--pause-bg:#231c40;--pause-edge:#9076f2;--done-fg:#aeb6c4;--done-bg:#1b2230;--done-edge:#7f889a;--brand:#8db4ff;--brand2:#b69cff;--on-brand:#0a0e17;
--shadow:0 1px 2px rgba(0,0,0,.4),0 2px 12px rgba(0,0,0,.25);--shadow-h:0 2px 6px rgba(0,0,0,.45),0 10px 28px rgba(0,0,0,.35)}}
:root[data-theme="dark"]{--bg:#0a0e17;--surface:#121826;--ink:#e8ecf4;--ink2:#b3bccb;
--line:#283244;--ctl:#8a94a7;--focus:#8db4ff;--you-fg:#ffc670;--you-bg:#2a1f0a;--you-edge:#e39431;--stall-fg:#ffa3a3;
--stall-bg:#331517;--stall-edge:#f05a57;--run-fg:#72e4a7;--run-bg:#0f2a1d;--run-edge:#38c675;--pause-fg:#cbbcff;
--pause-bg:#231c40;--pause-edge:#9076f2;--done-fg:#aeb6c4;--done-bg:#1b2230;--done-edge:#7f889a;--brand:#8db4ff;--brand2:#b69cff;--on-brand:#0a0e17;
--shadow:0 1px 2px rgba(0,0,0,.4),0 2px 12px rgba(0,0,0,.25);--shadow-h:0 2px 6px rgba(0,0,0,.45),0 10px 28px rgba(0,0,0,.35)}
@media (prefers-contrast:more){:root{--line:var(--ctl)}}
*{box-sizing:border-box}html{scroll-padding-top:calc(var(--bar-h,120px) + 8px);-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
sans-serif;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
h1,h2,h3,p,ul,ol{margin:0}code,pre,.mono{font-family:var(--mono);font-size:12px}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip{position:absolute;left:8px;top:-60px;background:var(--surface);color:var(--ink);padding:8px 12px;
border:2px solid var(--focus);border-radius:8px;z-index:30}.skip:focus{top:8px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
.btn,.fchip,.seg a,.compact,summary,.tog,.more{cursor:pointer}
.btn[data-copied]{border-color:var(--run-edge);color:var(--run-fg)}
.btn{font:inherit;font-size:12px;border:1px solid var(--ctl);background:var(--surface);color:var(--ink);
border-radius:6px;padding:2px 8px;min-height:24px}
.banner{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0;padding:10px max(16px,env(safe-area-inset-left));
font-weight:600;border-bottom:1px solid var(--line)}.banner[hidden]{display:none}
.banner.warn{background:var(--you-bg);color:var(--you-fg);border-left:4px solid var(--you-edge)}
.banner.bad{background:var(--stall-bg);color:var(--stall-fg);border-left:4px solid var(--stall-edge)}
.banner .cmd{display:inline-flex;align-items:center;gap:6px;color:var(--ink)}
.bar{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:6px max(16px,env(safe-area-inset-left)) 6px max(16px,env(safe-area-inset-right))}
.r1{display:flex;align-items:center;gap:6px 12px;min-height:36px}
h1{font-size:18px;letter-spacing:.2px}
.conn{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink2);min-height:24px;min-width:0;flex:1 1 0}
.conn>span{min-width:0}.conn .btn{flex:none}.conn:not([data-state="retry"]) .btn{display:none}
.r1 h1{flex:none;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--run-edge);flex:none}
.conn[data-state="retry"] .dot{background:var(--you-edge)}.conn[data-state="offline"] .dot{background:var(--stall-edge)}
.seg{display:inline-flex;flex:none;border:1px solid var(--ctl);border-radius:8px;overflow:hidden;margin-left:auto}
.seg a{padding:6px 12px;font-size:12px;font-weight:600;color:var(--ink);text-decoration:none;min-width:40px;text-align:center;white-space:nowrap}
.seg a[aria-current="true"]{background:var(--ink);color:var(--bg)}
.r2{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:6px}
#q{font:inherit;font-size:13px;padding:6px 10px;border:1px solid var(--ctl);border-radius:8px;background:var(--surface);
color:var(--ink);min-width:0;width:240px;flex:none}
#q::placeholder{color:var(--ink2);opacity:1}
#sum{display:flex;flex-wrap:wrap;gap:8px}
.older{margin-left:auto;font-size:12px;color:var(--ink2);display:inline-flex;align-items:center;gap:6px;min-height:24px}
.fchip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--ctl);background:var(--surface);
color:var(--ink);border-radius:999px;padding:4px 12px;font:inherit;font-size:13px;min-height:32px}
.fchip b{font-variant-numeric:tabular-nums}.fchip[aria-pressed="true"]{box-shadow:inset 0 0 0 2px var(--ink)}
.fchip.you{background:var(--you-bg);color:var(--you-fg);border-color:var(--you-edge)}
.fchip.stalled{background:var(--stall-bg);color:var(--stall-fg);border-color:var(--stall-edge)}
.fchip.running{background:var(--run-bg);color:var(--run-fg);border-color:var(--run-edge)}
.fchip.paused{background:var(--pause-bg);color:var(--pause-fg);border-color:var(--pause-edge)}
.fchip.done{background:var(--done-bg);color:var(--done-fg);border-color:var(--done-edge)}
.fchip[data-n="0"]{background:var(--surface);color:var(--ink2);border-color:var(--line)}
.calm{margin:6px 0 0;color:var(--ink2);font-size:13px}.calm:empty{display:none}
.helpline{position:fixed;left:0;right:0;bottom:0;z-index:25;margin:0;color:var(--ink);font-size:13px;background:var(--surface);border-top:2px solid var(--ctl);padding:10px max(16px,env(safe-area-inset-left)) calc(10px + env(safe-area-inset-bottom))}.helpline[hidden]{display:none}
main{padding:8px max(16px,env(safe-area-inset-left)) 24px max(16px,env(safe-area-inset-right));max-width:1560px;margin:0 auto}
main:focus{outline:none}
.sec{margin:12px 0}.sec:first-child{margin-top:4px}
h2{font-size:15px;margin:0 0 6px;display:flex;gap:8px;align-items:baseline}h2 small{font-weight:500;color:var(--ink2);font-size:13px}
/* Three open runs share one row. A definite 520px track max only fitted two columns
   inside the 1560px main (3*520 + gaps overflow), so the third card wrapped under the
   first. Tracks are counted from a minimum that fits three on a desktop width; each
   card stays capped so one run does not stretch across the page. */
.grid{display:grid;gap:12px;justify-content:start;justify-items:start;align-items:start;
grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr))}
.grid>.card{width:100%;max-width:520px}
/* Finished/paused rows: one row per run, a single column. */
.list{display:grid;gap:8px;align-items:start;grid-template-columns:minmax(0,1fr);max-width:960px}
.card{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--edge,var(--line));
border-radius:12px;padding:10px 14px 6px;min-width:0;width:100%;max-width:100%;box-sizing:border-box}
.many .card{content-visibility:auto;contain-intrinsic-size:auto 220px}
.g-you{--edge:var(--you-edge);--fgc:var(--you-fg)}.g-stalled{--edge:var(--stall-edge);--fgc:var(--stall-fg)}
.g-running{--edge:var(--run-edge);--fgc:var(--run-fg)}.g-paused{--edge:var(--pause-edge);--fgc:var(--pause-fg)}
.g-done{--edge:var(--done-edge);--fgc:var(--done-fg)}
.card.g-you{background:linear-gradient(var(--you-bg),var(--surface) 90px)}
.card.g-stalled{background:linear-gradient(var(--stall-bg),var(--surface) 90px)}
.card[data-code="hung"],.card[data-code="silent"],.card[data-code="stale"],.card[data-code="dead_wait"],.card[data-code="detached"]{--edge:var(--you-edge);--fgc:var(--you-fg);background:linear-gradient(var(--you-bg),var(--surface) 90px)}
.card[data-stale="true"]{outline:1px dashed var(--ctl);outline-offset:-1px}
.card[data-code="recorded"]{border-left-style:dashed}.card[data-code="recorded"] .do{color:var(--ink2)}
.ch .tog{flex:none;margin-left:auto}
.ch{display:flex;align-items:center;justify-content:space-between;gap:8px}
.badge{display:inline-flex;align-items:center;gap:6px;font-weight:700;font-size:12px;letter-spacing:.2px;
text-transform:uppercase;color:var(--fgc,var(--ink))}.asof{font-size:12px;color:var(--ink2);flex:1}.asof[hidden]{display:none}
.age{font-size:12px;color:var(--ink2);white-space:nowrap;margin-left:auto}.age:empty{display:none}
h3{margin:6px 0 0;font-size:16px;line-height:1.3}
.title{margin:2px 0 4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta{margin:0 0 6px;font-size:12px;color:var(--ink2)}
.now{margin:6px 0 2px;font-weight:600}.do{margin:2px 0 4px}.do:empty,.now:empty{display:none}
.cmdrow{margin:0 0 4px}.cmd{display:inline-flex;max-width:100%;align-items:center;gap:6px;background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:3px 4px 3px 8px}.cmd code{min-width:0}.cmd .btn{flex:none;white-space:nowrap}.cmd[hidden]{display:none}
ol.steps{display:flex;gap:3px;list-style:none;padding:0;margin:8px 0 2px;align-items:center}
ol.steps li{flex:1;height:8px;border-radius:3px;border:1.5px solid var(--ctl);overflow:hidden;text-indent:-9999px;
white-space:nowrap;font-size:1px;line-height:1}
ol.steps li.sd{background:var(--ink2);border-color:var(--ink2)}
ol.steps li.sc{background:var(--edge,var(--ink));border-color:var(--edge,var(--ink));height:12px;border-radius:4px}
ol.steps li.sk{border-style:dashed}
.pl{font-size:12px;color:var(--ink2);margin:2px 0 6px}.pl b{color:var(--ink)}
.tbar{display:flex;height:8px;border-radius:4px;overflow:hidden;border:1.5px solid var(--ctl);margin:4px 0}
.tbar i{display:block;height:100%}.tbar .d{background:var(--ink2)}
.tbar .p{background:repeating-linear-gradient(45deg,var(--run-edge) 0 4px,var(--surface) 4px 8px)}
.tbar .b{background:repeating-linear-gradient(90deg,var(--stall-edge) 0 3px,var(--surface) 3px 6px)}
.tl{font-size:12px;color:var(--ink2);margin:0 0 4px}.tl b{color:var(--ink)}
.tog{font:inherit;font-size:12px;color:var(--focus);background:none;border:0;padding:0;text-decoration:underline;min-height:24px;line-height:1.2}
.ttable{overflow:auto;margin:4px 0;max-height:min(40vh,280px);border:1px solid var(--line);border-radius:8px}
table.tt{border-collapse:collapse;width:100%;font-size:12px}
.tt thead th{position:sticky;top:0;background:var(--surface);z-index:1}
.tt th{text-align:left;color:var(--ink2);font-weight:600;border-bottom:1px solid var(--line);padding:3px 6px}
.tt td{padding:3px 6px;border-top:1px solid var(--line);vertical-align:top}.tt td.id{font-family:var(--mono);white-space:nowrap}.tt td:nth-child(3),.tt td:nth-child(4){white-space:nowrap}
.tt td.st .g,.tt td.tests .g{display:inline-block;min-width:1em;margin-right:.3em;font-weight:700}
.tt td.st-done .g,.tt td.tests-passed .g{color:var(--run-fg)}
.tt td.st-in_progress .g,.tt td.tests-awaiting_green .g{color:var(--run-edge)}
.tt td.st-blocked .g{color:var(--stall-fg)}
.tt td.st-pending .g,.tt td.tests-none .g{color:var(--ctl)}
.tt td.st-deferred .g{color:var(--ink2)}
.tt td.tests-passed{color:var(--run-fg);font-weight:600}
.tt td.tests-na{color:var(--ink2)}
.tt.no-tests td:nth-child(3){white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.chips:empty{display:none}
.gchip{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:1px 8px;color:var(--ink)}
.gchip.bad{border-color:var(--stall-edge);color:var(--stall-fg);font-weight:700}
.nwrap{flex-basis:100%}.nwrap .tog{display:block}.nwrap .tog[hidden]{display:none}
.notice{font-size:12px;color:var(--ink2);margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.notice.open{display:block}
details{margin-top:4px;border-top:1px solid var(--line);padding-top:4px}
summary{font-size:13px;font-weight:600;min-height:26px;display:flex;align-items:center;gap:8px;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"\25B8";content:"\25B8" / "";color:var(--ink2);width:1em;flex:none}details[open]>summary::before{content:"\25BE";content:"\25BE" / ""}
summary .mut{font-weight:400;color:var(--ink2);font-size:12px}
ul.feed{list-style:none;margin:6px 0 0;padding:0}
.fr{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:0 8px;align-items:baseline;padding:3px 0;
border-top:1px solid var(--line);font-size:12px}.fr:first-child{border-top:0}
.k{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;border:1px solid var(--line);
border-radius:4px;padding:0 5px;color:var(--ink2);white-space:nowrap}
.k.guard{border-color:var(--stall-edge);color:var(--stall-fg)}.k.q{border-color:var(--you-edge);color:var(--you-fg)}
.k.res{border-color:var(--run-edge);color:var(--run-fg)}
.wh{min-width:0}.sum{font-family:var(--mono);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cnt,.cat{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 4px;color:var(--ink2);margin-left:6px;white-space:nowrap}
.fr time{color:var(--ink2);white-space:nowrap}
.full{grid-column:1/-1}.full pre{margin:4px 0;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);
border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.row{color:var(--ink2);font-size:12px;margin:3px 0}.row b{color:var(--ink);font-weight:600}
.item{min-width:0}.compact{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 10px;align-items:center;width:100%;
text-align:left;font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);
border-left:4px solid var(--edge,var(--line));border-radius:10px;padding:8px 12px;min-height:44px;
box-sizing:border-box}
.compact .nm{font-weight:600;min-width:0}.compact .nm span{font-weight:400;color:var(--ink2)}
.compact .sub{grid-column:1/-1;font-size:12px;color:var(--ink2)}
.compact .age{grid-column:1/-1;white-space:normal;text-align:left;min-width:0;margin-left:0}
@media(min-width:601px){
.compact{grid-template-columns:auto minmax(0,1fr) auto}
.compact .age{grid-column:3;grid-row:1;white-space:nowrap;text-align:right}
.compact .sub{grid-column:2/-1;grid-row:2}}
.compact[hidden]{display:none}
.item .card{margin-top:6px}.item .card[hidden]{display:none}
.more{display:block;margin:8px 0 0;font:inherit;font-size:13px;color:var(--focus);background:none;border:0;
text-decoration:underline;padding:6px 0;min-height:32px}.more[hidden]{display:none}
.empty{max-width:640px;margin:24px 0}.empty h2{font-size:18px}.empty p{margin:6px 0;color:var(--ink2)}
.empty .cmdrow{margin:8px 0}.empty .lbl{display:block;font-size:12px;color:var(--ink2);margin-bottom:2px}
.skel .card{height:150px;margin-bottom:12px;border-left-color:var(--line)}
.foot{max-width:1560px;margin:0 auto;padding:8px max(16px,env(safe-area-inset-left)) 32px;color:var(--ink2);font-size:12px;display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px}.foot .older{margin-left:0}
.printline{display:none}
.flash{outline:2px solid var(--focus);outline-offset:2px}
/* ---- skin: calm surfaces, soft depth, one accent; every state keeps its own token pair (contrast-tested) */
body{font-size:14px;letter-spacing:.005em}
.bar{background:var(--bg);background:color-mix(in srgb,var(--bg) 86%,transparent);-webkit-backdrop-filter:saturate(1.5) blur(12px);backdrop-filter:saturate(1.5) blur(12px)}
.mark{display:inline-block;width:24px;height:24px;border-radius:8px;flex:none;
background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:inset 0 0 0 1px rgba(255,255,255,.22),var(--shadow)}
h1{font-size:19px;letter-spacing:-.01em}
#q{border-radius:10px;border-color:var(--line);box-shadow:var(--shadow);min-height:36px}
.fchip{box-shadow:var(--shadow);font-weight:500}
.seg{border-color:var(--line);border-radius:10px;box-shadow:var(--shadow)}
.sec{margin:18px 0}h2{font-size:15px;letter-spacing:-.005em}
h2 small{display:inline-block;min-width:22px;text-align:center;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:0 8px;font-size:12px}
.card{border-radius:14px;border-color:var(--line);box-shadow:var(--shadow);padding:12px 16px 8px}
.compact{border-radius:12px;box-shadow:var(--shadow)}
.btn{border-radius:8px}
.cmd{border-radius:10px}
.gchip{border-radius:999px;padding:1px 10px}
/* risk: one pill vocabulary everywhere (radar, card, feed row) */
.rk{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:700;letter-spacing:.2px;border:1px solid;
border-radius:999px;padding:0 8px;line-height:18px;white-space:nowrap;vertical-align:baseline}
.rk-high{color:var(--stall-fg);background:var(--stall-bg);border-color:var(--stall-edge)}
.rk-medium{color:var(--you-fg);background:var(--you-bg);border-color:var(--you-edge)}
.rk-blocked{color:var(--pause-fg);background:var(--pause-bg);border-color:var(--pause-edge)}
.wh .rk{margin-left:6px}
.fr[data-risk="high"]{box-shadow:inset 3px 0 0 var(--stall-edge);padding-left:8px}
.fr[data-risk="medium"]{box-shadow:inset 3px 0 0 var(--you-edge);padding-left:8px}
.fr[data-risk="blocked"]{box-shadow:inset 3px 0 0 var(--pause-edge);padding-left:8px}
.p-risk{margin:8px 0 4px;border:1px solid var(--line);border-left:4px solid var(--stall-edge);border-radius:10px;padding:8px 10px 4px}
.p-risk[data-top="medium"]{border-left-color:var(--you-edge)}.p-risk[data-top="blocked"]{border-left-color:var(--pause-edge)}
.p-risk[hidden]{display:none}
.rh{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;font-size:13px;font-weight:700}.rh .mut{font-weight:500;color:var(--ink2);font-size:12px}
ul.rlist{list-style:none;margin:4px 0 0;padding:0}
.ri{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:2px 10px;align-items:baseline;padding:6px 0;border-top:1px solid var(--line);font-size:12px}
.ri:first-child{border-top:0}
.rb{display:flex;flex-direction:column;gap:1px;min-width:0}
.rl{font-weight:600;color:var(--ink)}
.rb code{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin:2px 0}
.rm{color:var(--ink2)}.rr{color:var(--ink2);font-style:italic;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rf{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px}
.ri time{color:var(--ink2);white-space:nowrap}
.rf .go{min-height:0}
.radar{max-width:1560px;margin:12px auto 0;padding:0 max(16px,env(safe-area-inset-right)) 0 max(16px,env(safe-area-inset-left))}
.radar[hidden]{display:none}
.radar-in{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--stall-edge);border-radius:14px;box-shadow:var(--shadow);padding:12px 16px 8px}
.radar[data-top="medium"] .radar-in{border-left-color:var(--you-edge)}.radar[data-top="blocked"] .radar-in{border-left-color:var(--pause-edge)}
.radar-head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px}
.radar-head h2{margin:0}.radar-sum{font-size:12px;color:var(--ink2);font-weight:600}
.radar-head .tog{margin-left:auto}
.radar-p{font-size:12px;color:var(--ink2);margin:2px 0 2px}
@media(min-width:1100px){.radar ul.rlist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:24px}
.radar .ri:nth-child(2){border-top:0}}
/* controls: the person's own two decisions about a run */
.ctl{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 4px}.ctl[hidden]{display:none}
.btn.cbtn{font-size:13px;font-weight:600;padding:4px 14px;min-height:32px}
.btn.warn{color:var(--you-fg);border-color:var(--you-edge);background:var(--you-bg)}
.btn.danger{color:var(--stall-fg);border-color:var(--stall-edge);background:var(--stall-bg)}
.btn[disabled]{cursor:not-allowed;color:var(--ink2);background:var(--bg);border-color:var(--line)}
.btn .ic{display:inline-block;margin-right:6px;font-weight:700}
dialog.dlg{border:1px solid var(--line);border-radius:16px;padding:0;background:var(--surface);color:var(--ink);
width:min(540px,calc(100vw - 32px));max-height:calc(100vh - 32px);box-shadow:0 24px 64px rgba(0,0,0,.35)}
dialog.dlg::backdrop{background:rgba(10,14,23,.55)}
.dlg-in{margin:0;padding:18px 20px 16px;display:flex;flex-direction:column;gap:8px}
.dlg h2{font-size:18px;margin:0}
.dlg-run{font-weight:600;margin:0}
.dlg-p{color:var(--ink2);font-size:13px;margin:0}
.dlg-lbl{font-size:12px;font-weight:600;color:var(--ink2);margin-top:6px}
#ctl-reason{font:inherit;font-size:13px;border:1px solid var(--ctl);border-radius:10px;padding:8px 10px;background:var(--bg);color:var(--ink);resize:vertical;min-height:64px;width:100%}
#ctl-reason::placeholder{color:var(--ink2);opacity:1}
#ctl-reason[aria-invalid="true"]{border-color:var(--stall-edge);box-shadow:0 0 0 1px var(--stall-edge)}
.dlg-err{color:var(--stall-fg);font-size:13px;font-weight:600;margin:0}.dlg-err[hidden]{display:none}
.dlg-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:8px;flex-wrap:wrap}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:40;
background:var(--ink);color:var(--bg);border-radius:12px;padding:10px 16px;font-weight:600;box-shadow:var(--shadow-h);max-width:calc(100vw - 32px)}
.toast[hidden]{display:none}
.compact .nm .rkn{margin-left:8px;font-size:11px;font-weight:700;color:var(--stall-fg);background:var(--stall-bg);border:1px solid var(--stall-edge);border-radius:999px;padding:0 7px}
.compact .nm .rkn[hidden]{display:none}
/* console: header actions, dialogs, conversation, documents, tools */
.acts{display:inline-flex;gap:6px;flex:none;margin-left:auto}.acts[hidden]{display:none}.acts:not([hidden])+.seg{margin-left:0}
.btn.primary{color:var(--on-brand);background:var(--brand);border-color:var(--brand)}
.btn.primary[disabled]{color:var(--ink2);background:var(--bg);border-color:var(--line)}
.acts .ic{font-weight:700}
dialog.dlg.wide{width:min(760px,calc(100vw - 32px))}
.dlg-top{display:flex;align-items:center;justify-content:space-between;gap:12px}
.dlg select,.dlg input[type="number"],.dlg textarea{font:inherit;font-size:13px;border:1px solid var(--ctl);border-radius:10px;padding:7px 10px;background:var(--bg);color:var(--ink);min-height:36px;width:100%}
.dlg input[type="number"]{width:120px}.dlg textarea{resize:vertical;min-height:96px}
.dlg textarea::placeholder{color:var(--ink2);opacity:1}
.dlg [aria-invalid="true"]{border-color:var(--stall-edge);box-shadow:0 0 0 1px var(--stall-edge)}
fieldset.modes{border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:6px 0 0;display:flex;flex-direction:column;gap:6px}
fieldset.modes legend{font-size:12px;font-weight:600;color:var(--ink2);padding:0 4px}
.modes label,.chk{display:flex;gap:8px;align-items:flex-start;font-size:13px;cursor:pointer}.chk{margin-top:6px}
.modes input,.chk input{margin-top:3px;flex:none}
.hint{font-size:12px;color:var(--ink2);margin:0}.hint:empty{display:none}
.svc-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;background:var(--you-bg);color:var(--you-fg);border:1px solid var(--you-edge);border-radius:10px;padding:8px 10px;font-size:13px;font-weight:600}
.svc-row[hidden]{display:none}.svc-row[data-ok="1"]{background:var(--run-bg);color:var(--run-fg);border-color:var(--run-edge)}
.tl-sec{border-top:1px solid var(--line);padding-top:10px;margin-top:4px}.tl-sec h3{font-size:14px;margin:0 0 6px}
.tl-row{display:flex;flex-wrap:wrap;align-items:flex-end;gap:8px 12px}.tl-row .dlg-lbl{flex-basis:100%;margin:0}
pre.doc{white-space:pre-wrap;overflow-wrap:anywhere;font-family:var(--mono);font-size:12px;line-height:1.55;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;max-height:min(60vh,560px);overflow:auto;margin:6px 0 0}
pre.doc[hidden]{display:none}pre.doc .bad{color:var(--stall-fg);font-weight:700}pre.doc .good{color:var(--run-fg)}
.tabs{display:flex;flex-wrap:wrap;gap:6px}.tab{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:3px 12px;min-height:30px;cursor:pointer}
.tab[aria-pressed="true"]{box-shadow:inset 0 0 0 2px var(--ink)}
.docs{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;font-size:12px;margin:4px 0}.docs[hidden]{display:none}
.docs>.mut{color:var(--ink2);font-weight:600}.dl{display:inline-flex;flex-wrap:wrap;gap:2px 12px}
.p-job{margin:8px 0 4px;border:1px solid var(--line);border-left:4px solid var(--brand);border-radius:10px;padding:8px 10px}.p-job[hidden]{display:none}
.jh{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;font-size:13px;font-weight:700}.jh .mut{font-weight:500;color:var(--ink2);font-size:12px}
.jmsg{white-space:pre-wrap;font-size:13px;line-height:1.5;margin:6px 0 2px;max-height:9.5em;overflow:hidden}.jmsg.open{max-height:none}.jmsg.empty{color:var(--ink2)}
.jform{display:flex;flex-direction:column;gap:6px;margin-top:6px}.jform[hidden]{display:none}
.jform textarea{font:inherit;font-size:13px;border:1px solid var(--ctl);border-radius:10px;padding:7px 10px;background:var(--bg);color:var(--ink);resize:vertical;min-height:60px;width:100%}
.jform textarea::placeholder{color:var(--ink2);opacity:1}.jform textarea[aria-invalid="true"]{border-color:var(--stall-edge)}
.jform .btn{align-self:flex-end}
ul.jt{list-style:none;margin:4px 0 0;padding:0;font-size:12px}ul.jt li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:0 8px;padding:3px 0;border-top:1px solid var(--line)}
ul.jt time{color:var(--ink2);white-space:nowrap}
.rk.jst-awaiting_user,.rk.jst-blocked{color:var(--you-fg);background:var(--you-bg);border-color:var(--you-edge)}
.rk.jst-running,.rk.jst-queued{color:var(--run-fg);background:var(--run-bg);border-color:var(--run-edge)}
.rk.jst-paused{color:var(--pause-fg);background:var(--pause-bg);border-color:var(--pause-edge)}
.rk.jst-stalled,.rk.jst-error{color:var(--stall-fg);background:var(--stall-bg);border-color:var(--stall-edge)}
.rk.jst-done,.rk.jst-cancelled{color:var(--done-fg);background:var(--done-bg);border-color:var(--done-edge)}
.rr2{color:var(--ink2);white-space:pre-wrap;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media (prefers-reduced-motion:no-preference){
.dot{animation:pulse 2.4s ease-in-out infinite}@keyframes pulse{50%{opacity:.45}}
.fr.new{animation:fade 1.2s ease-out}@keyframes fade{from{background:var(--you-bg)}to{background:transparent}}
.card,.compact,.btn,.fchip{transition:box-shadow .15s ease,background-color .15s ease,border-color .15s ease}
.card:hover,.compact:hover{box-shadow:var(--shadow-h)}
.toast{animation:rise .2s ease-out}@keyframes rise{from{transform:translate(-50%,8px)}to{transform:translate(-50%,0)}}}
@media (max-width:600px),(max-height:560px){
.bar{display:contents}
.r1{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:0 max(16px,env(safe-area-inset-left));min-height:44px}
.r1 h1{font-size:17px}
.r2{padding:8px max(16px,env(safe-area-inset-left)) 4px;margin:0}
.calm{padding:0 max(16px,env(safe-area-inset-left))}
html{--bar-h:56px}}
@media (max-width:600px),(pointer:coarse){
.seg a{padding:13px 14px}.fchip{min-height:44px}.btn,.tog,.more,.compact,summary{min-height:44px}#q{min-height:44px}
.older{min-height:44px}}
@media (max-width:600px){
#q{width:100%;flex:1 1 100%}.older{margin-left:0}#conn[data-state="live"] #conn-retry,#conn[data-state="offline"] #conn-retry{display:none}
.fr{grid-template-columns:auto minmax(0,1fr)}.fr time{grid-column:2;grid-row:1;text-align:right}
.compact{grid-template-columns:auto minmax(0,1fr)}.compact .badge{grid-row:1;grid-column:1}
.compact .nm{grid-column:1/-1;grid-row:2}.compact .sub{grid-row:3}.compact .age{grid-row:4;grid-column:1/-1;text-align:left}
.fr .wh{grid-column:1/-1}
.sec .grid,.sec .list{grid-template-columns:minmax(0,1fr)}
.ctl .btn{flex:1 1 0;min-height:44px}
dialog.dlg{width:100vw;max-width:100vw;margin:auto 0 0;border-radius:18px 18px 0 0}
.dlg-btns .btn{flex:1 1 100%;min-height:44px}
.radar{padding:0 max(12px,env(safe-area-inset-left))}.radar-in{padding:12px}
.ri{grid-template-columns:auto minmax(0,1fr)}.ri time{grid-column:2;grid-row:1;text-align:right}
.ri .rb{grid-column:1/-1}
.acts .lbl{display:none}.acts .btn{min-height:44px;min-width:44px}.acts .ic{margin:0}
.r1{flex-wrap:wrap;row-gap:0}.conn{order:5;flex:1 1 100%;padding-bottom:6px}
.dlg-top .btn,.tl-row .btn,.tab{min-height:44px}.jform .btn{align-self:stretch;min-height:44px}}
@media print{
.bar{position:static}.r1,.r2{position:static}#sum,#q,.seg,.older,.conn,.skip,.banner,.btn,.tog,.more,
.helpline,.foot label,.ctl,.dlg,.toast,#radar-tog,.go,.mark,.acts,.jform,.docs{display:none!important}.printline{display:block;margin:0 0 8px;font-size:12px}.card{break-inside:avoid}
body,:root{--bg:#fff;--surface:#fff;--ink:#000;--ink2:#222;--line:#999}
.grid,.list{display:block}.grid>*,.list>*{margin-bottom:8px}.card{background:#fff!important}}
@media (forced-colors:active){
.fchip[aria-pressed="true"]{border:3px solid Highlight;padding:2px 10px}
.badge,.fchip,.k,.gchip,.cnt,.cat,.cmd{border:1px solid CanvasText}.card,.compact{border:1px solid CanvasText}
ol.steps li{border-color:CanvasText;forced-color-adjust:none}ol.steps li.sd{background:CanvasText}
ol.steps li.sc{background:Highlight;border-color:Highlight}ol.steps li.sk{border-style:dashed}
.tbar{border-color:CanvasText}.tbar .d{background:CanvasText}.dot{background:Highlight}
.rk,.rkn,.p-risk,.radar-in,dialog.dlg{border:1px solid CanvasText}.fr[data-risk]{box-shadow:none;border-left:3px solid CanvasText}}
</style></head><body>
<a class="skip" href="#main">__SKIP__</a>
<div id="stale" class="banner warn" role="alert" hidden><span id="stale-msg"></span>
<span class="cmd"><code id="stale-code">orkestra dashboard --open</code><button type="button" class="btn" id="stale-copy"></button></span></div>
<div id="offline" class="banner bad" hidden><span id="offline-msg"></span><button type="button" class="btn" id="offline-retry"></button></div>
<div id="rerr" class="banner bad" role="alert" hidden><span id="rerr-msg"></span><button type="button" class="btn" id="rerr-reload"></button></div>
<p id="printline" class="printline"></p>
<header class="bar" id="bar">
<div class="r1" id="r1"><span class="mark" aria-hidden="true"></span><h1>Orkestra</h1><span id="conn" class="conn" data-state="live"><i class="dot" aria-hidden="true"></i><span id="conn-text">__LOADING__</span><button type="button" class="btn" id="conn-retry"></button></span>
<span id="acts" class="acts" hidden><button type="button" class="btn cbtn primary" id="new-btn"><span class="ic" aria-hidden="true">+</span><span class="lbl" id="new-lbl"></span></button><button type="button" class="btn cbtn" id="tools-btn"><span class="ic" aria-hidden="true">&#9776;</span><span class="lbl" id="tools-lbl"></span></button></span>
<span class="seg" role="group" aria-label="__LANG_LABEL__"><a id="lang-en" href="?lang=en" hreflang="en" aria-label="__LANG_EN__"__CUR_EN__>EN</a><a id="lang-tr" href="?lang=tr" hreflang="tr" aria-label="__LANG_TR__"__CUR_TR__>TR</a></span></div>
<div class="r2"><input id="q" type="search" dir="auto" placeholder="__SEARCH_PH__" aria-label="__SEARCH__" autocomplete="off" spellcheck="false">
<div id="sum" role="group"></div><label class="older"><input type="checkbox" id="older"><span id="older-text">__OLDER__</span></label></div>
<p id="calm" class="calm"></p>
</header>
<section id="radar" class="radar" aria-labelledby="radar-h" hidden><div class="radar-in">
<div class="radar-head"><h2 id="radar-h"></h2><span id="radar-sum" class="radar-sum"></span><button type="button" class="tog" id="radar-tog" aria-expanded="false" aria-controls="radar-list" hidden></button></div>
<p id="radar-p" class="radar-p"></p><ul id="radar-list" class="rlist"></ul></div></section>
<main id="main" tabindex="-1"><div id="skel" aria-hidden="true"><div class="skel"><div class="card"></div><div class="card"></div><div class="card"></div></div></div></main>
<footer class="foot"><span id="foot-build"></span><span id="foot-ro"></span><button type="button" class="tog" id="keys-btn" aria-controls="help" aria-expanded="false"></button><label class="older"><input type="checkbox" id="keys-on" checked><span id="keys-on-text"></span></label></footer>
<p id="help" class="helpline" role="region" hidden></p>
<dialog id="ctl" class="dlg" aria-labelledby="ctl-h" aria-describedby="ctl-p"><form id="ctl-form" class="dlg-in" method="dialog" novalidate>
<h2 id="ctl-h"></h2><p id="ctl-run" class="dlg-run" dir="auto"></p><p id="ctl-p" class="dlg-p"></p>
<label class="dlg-lbl" id="ctl-reason-l" for="ctl-reason"></label><textarea id="ctl-reason" rows="2" dir="auto" aria-describedby="ctl-err"></textarea>
<p id="ctl-err" class="dlg-err" hidden></p>
<div class="dlg-btns"><button type="button" class="btn cbtn" id="ctl-cancel"></button><button type="submit" class="btn cbtn" id="ctl-ok"></button></div>
</form></dialog>
<dialog id="nr" class="dlg wide" aria-labelledby="nr-h"><form id="nr-form" class="dlg-in" method="dialog" novalidate>
<h2 id="nr-h"></h2>
<div id="nr-svc" class="svc-row" hidden><span id="nr-svc-msg"></span><button type="button" class="btn cbtn" id="nr-svc-btn" hidden></button></div>
<label class="dlg-lbl" id="nr-project-l" for="nr-project"></label><select id="nr-project" aria-describedby="nr-project-err"></select>
<p id="nr-project-err" class="dlg-err" hidden></p>
<label class="dlg-lbl" id="nr-engine-l" for="nr-engine"></label><select id="nr-engine" aria-describedby="nr-engine-hint"></select>
<p id="nr-engine-hint" class="hint"></p>
<fieldset class="modes"><legend id="nr-mode-l"></legend><label><input type="radio" name="nr-mode" id="nr-mode-auto" value="autopilot" checked><span id="nr-mode-auto-t"></span></label>
<label><input type="radio" name="nr-mode" id="nr-mode-man" value="manual"><span id="nr-mode-man-t"></span></label></fieldset>
<label class="dlg-lbl" id="nr-req-l" for="nr-req"></label><textarea id="nr-req" rows="5" dir="auto" aria-describedby="nr-req-err nr-req-n"></textarea>
<p id="nr-req-n" class="hint"></p><p id="nr-req-err" class="dlg-err" hidden></p>
<label class="chk"><input type="checkbox" id="nr-ok" aria-describedby="nr-ok-err"><span id="nr-ok-t"></span></label><p id="nr-ok-err" class="dlg-err" hidden></p>
<p id="nr-err" class="dlg-err" hidden></p>
<div class="dlg-btns"><button type="button" class="btn cbtn" id="nr-cancel"></button><button type="submit" class="btn cbtn primary" id="nr-go"></button></div>
</form></dialog>
<dialog id="tl" class="dlg wide" aria-labelledby="tl-h"><div class="dlg-in">
<div class="dlg-top"><h2 id="tl-h"></h2><button type="button" class="btn cbtn" id="tl-close"></button></div>
<section class="tl-sec" aria-labelledby="tl-svc-h"><h3 id="tl-svc-h"></h3><div id="tl-svc" class="svc-row"><span id="tl-svc-msg"></span><button type="button" class="btn cbtn" id="tl-svc-btn" hidden></button></div></section>
<section class="tl-sec" aria-labelledby="tl-jobs-h"><h3 id="tl-jobs-h"></h3><ul id="tl-jobs" class="rlist"></ul></section>
<section class="tl-sec" aria-labelledby="tl-clear-h"><h3 id="tl-clear-h"></h3><p id="tl-clear-p" class="dlg-p"></p>
<div class="tl-row"><label class="dlg-lbl" id="tl-hours-l" for="tl-hours"></label><input id="tl-hours" type="number" min="1" max="8760" step="1" value="24" inputmode="numeric" aria-describedby="tl-hours-err"><button type="button" class="btn cbtn" id="tl-preview"></button></div>
<p id="tl-hours-err" class="dlg-err" hidden></p><div id="tl-clear-out"></div></section>
<section class="tl-sec" aria-labelledby="tl-doc-h"><h3 id="tl-doc-h"></h3><p id="tl-doc-p" class="dlg-p"></p><button type="button" class="btn cbtn" id="tl-doc-run"></button>
<p id="tl-doc-sum" class="dlg-p" hidden></p><pre id="tl-doc-out" class="doc" hidden></pre></section>
</div></dialog>
<dialog id="rd" class="dlg wide" aria-labelledby="rd-h"><div class="dlg-in">
<div class="dlg-top"><h2 id="rd-h" dir="auto"></h2><button type="button" class="btn cbtn" id="rd-close"></button></div>
<div id="rd-tabs" class="tabs" role="group"></div><p id="rd-note" class="hint" hidden></p><pre id="rd-text" class="doc" tabindex="0" dir="auto"></pre>
</div></dialog>
<div id="toast" class="toast" hidden></div>
<div id="live" class="vh" aria-live="polite"></div>
<script type="application/json" id="labels">__LABELS__</script>
<script>
"use strict";
const L=JSON.parse(document.getElementById("labels").textContent);
const META=__META__;
const BUILD="__BUILD__";
const PH=["intake","discover","clarify","plan","approve","implement","verify","review","deliver","done"];
const GROUPS=["you","stalled","running","paused","done"];
const GLYPH={you:"\u25c6",stalled:"\u2717",running:"\u25cf",paused:"\u2161",done:"\u2713"};
const FAMILY={quiet:"\u25cb",blocked:"\u25a0",recorded:"\u25cf"};
const TSG={done:"\u2713",in_progress:"\u25d0",blocked:"\u25a0",pending:"\u25cb",deferred:"\u2013"};
const TG={passed:"\u2713",awaiting_green:"\u25d0",none:"\u25cb"};
const TMAP={red_green:"passed",red:"awaiting_green",passed:"passed",awaiting_green:"awaiting_green",
 none:"none",na:"na",not_required:"na"};
const MAX_ROWS=4,PHONE_ROWS=3,FULL_CAP=24,MANY=30,FIN_SHOW=5,CMD_RESTART="orkestra dashboard --open";
const OPEN_ST=["active","awaiting_user","blocked","paused"],RISK_SHOW=3,RADAR_SHOW=6,RADAR_CAP=60,REASON_MAX=300;
const REQ_MAX=8000,JMSG_LONG=420,ENGINE_NAMES={claude:"Claude Code",cursor:"Cursor",copilot:"GitHub Copilot",pi:"Pi"};
const LANG=document.documentElement.lang||"en";
const BASE_TITLE=document.title;
const $=id=>document.getElementById(id);

// ---------------------------------------------------------------- small helpers
// text that comes from a run (project, title, sentences, commands) takes its direction from its own first letter
const AUTO=/(^| )(title|now|do|sum|nm|sub|notice)( |$)/;
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(t==="h3"||(c&&AUTO.test(c)))e.dir="auto";
 if(x!==undefined)e.textContent=x;return e;}
function fmt(tpl,vals){return String(tpl).replace(/\{(\w+)\}/g,(m,k)=>k in vals?String(vals[k]):m);}
function setText(n,s){s=String(s);if(n.textContent!==s)n.textContent=s;}
function setAttr(n,k,v){if(v===null||v===undefined||v===false){if(n.getAttribute(k)!==null)n.removeAttribute(k);}
 else if(n.getAttribute(k)!==String(v))n.setAttribute(k,String(v));}
function setData(n,k,v){if(n.dataset[k]!==v)n.dataset[k]=v;}
function setCls(n,c){if(n.className!==c)n.className=c;}
function show(n,on){if(n.hidden!==!on)n.hidden=!on;}
function lsGet(k){try{return localStorage.getItem(k);}catch(e){return null;}}
function lsSet(k,v){try{localStorage.setItem(k,v);}catch(e){}}
function tk(n){n=n||0;return n<1000?String(n):n<1e6?(n/1e3).toFixed(1)+"K":(n/1e6).toFixed(2)+"M";}
function compactDur(s){s=Math.max(0,s|0);
 if(s<60)return fmt(L.dur_s,{n:s});
 if(s<3600)return fmt(L.dur_m,{n:Math.floor(s/60)});
 let h=Math.floor(s/3600),m=Math.floor(s%3600/60);
 if(h<24)return m?fmt(L.dur_hm,{h:h,m:m}):fmt(L.dur_h,{h:h});
 const d=Math.floor(h/24);h=h%24;return h?fmt(L.dur_dh,{d:d,h:h}):fmt(L.dur_d,{d:d});}
function tokMark(n){if(n===null||n===undefined||n==="")return "";n=Math.max(0,n|0);
 if(n<1000)return " ("+n+")";
 if(n<1000000){var whole=Math.floor(n/1000),tenth=Math.floor((n%1000+50)/100);if(tenth===10){whole++;tenth=0;}
  return tenth?" ("+whole+"."+tenth+"k)":" ("+whole+"k)";}
 var whole=Math.floor(n/1000000),tenth=Math.floor((n%1000000+50000)/100000);if(tenth===10){whole++;tenth=0;}
 return tenth?" ("+whole+"."+tenth+"m)":" ("+whole+"m)";}
function clockLine(v){
 const spent={};
 (v.phase_history||[]).forEach(h=>{if(h&&h.phase)spent[h.phase]=(spent[h.phase]||0)+(h.seconds||0);});
 const open=v.status!=="done"&&v.status!=="aborted";
 const extra=open&&v.now?Math.max(0,Math.floor(nowS()-v.now)):0;
 const ph=(v.usage&&v.usage.phases)||{};
 const parts=[];
 PH.forEach(p=>{if(spent[p]==null)return;
  parts.push(L["phase_"+p]+": "+compactDur(spent[p]+(p===v.phase?extra:0))+(Object.prototype.hasOwnProperty.call(ph,p)?tokMark(ph[p]):""));});
 const base=(v.timing&&v.timing.run)|0;
 if(!base&&!parts.length)return "";
 const total=compactDur(base+extra)+((v.usage&&v.usage.total!==undefined&&v.usage.total!==null)?tokMark(v.usage.total):"");
 return parts.length?fmt(L.ui_clock,{total:total,phases:parts.join(" \u00b7 ")}):fmt(L.ui_clock_total,{total:total});}
function dur(s){s=Math.max(0,s|0);if(s<60)return fmt(L.dur_s,{n:s});if(s<3600)return fmt(L.dur_m,{n:Math.floor(s/60)});
 let h=Math.floor(s/3600);const m=Math.floor(s%3600/60);
 if(h<24)return m?fmt(L.dur_hm,{h:h,m:m}):fmt(L.dur_h,{h:h});
 const d=Math.floor(h/24);h=h%24;return h?fmt(L.dur_dh,{d:d,h:h}):fmt(L.dur_d,{d:d});}
function ago(s){s=Math.max(0,s);return s<5?L.ui_ago_now:fmt(L.ago,{dur:dur(s)});}
function pad(n){return String(n).padStart(2,"0");}
function stamp(ts){const d=new Date(ts*1000);return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+" "+
 pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());}
function clock(ts){return new Date(ts*1000).toLocaleTimeString(L.ui_locale,{hour:"2-digit",minute:"2-digit",hourCycle:"h23"});}
let skew=0;                                           // server clock minus browser clock, seconds
let skewHeader=false;                                 // the server said its time in a header: that wins over the views' `now`
function nowS(){return Date.now()/1000+skew;}
function rowTime(ts){const d=nowS()-ts;
 return d>86400?new Date(ts*1000).toLocaleString(L.ui_locale,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):ago(d);}
function nextDelay(fails,hidden,rnd){
 if(fails>0){const base=[2,4,8,8][Math.min(fails,4)-1]*1000;      // a local server is back at once or not at all: never wait long
 const r=rnd===undefined?Math.random():rnd;return Math.round(base*(0.8+r*0.4));}
 return hidden?10000:2000;}
// the same fold on both sides, whatever the page language: Turkish I/i rules must not make "ACME" miss "acme"
function lc(s){return String(s||"").normalize("NFD").replace(/\u0307/g,"").toLowerCase().replace(/\u0131/g,"i");}
function cmpStr(a,b){return a<b?-1:a>b?1:0;}

// ---------------------------------------------------------------- shortening of raw commands and paths (twin of monitor.short_what)
// by code points, like the server: an emoji is never cut in half
function mid(s,n){const a=Array.from(s);return a.length<=n?s:a.slice(0,Math.ceil((n-1)*0.62)).join("")+"\u2026"+a.slice(a.length-Math.floor((n-1)*0.38)).join("");}
function rel(p,ctx){
 if(ctx.project&&(p===ctx.project||p.indexOf(ctx.project+"/")===0))return p.slice(ctx.project.length+1)||".";
 if(ctx.home&&p.indexOf(ctx.home+"/")===0)return "~"+p.slice(ctx.home.length);
 return p;}
function shortWhat(tool,what,ctx){
 ctx=ctx||{};const full=String(what||"");let s=full;
 if(tool==="Bash"){
  s=s.replace(/^\s*cd\s+(\S+)\s*(?:&&|;)\s*/,(m,d)=>{const r=rel(d,ctx);return r==="."?"":"("+r+") ";});
  for(let i=0;i<4;i++)s=s.replace(/\s*(?:2>&1|\|\s*(?:tee\s+\S+|tail\s+-?\d+|head\s+-?\d+))\s*$/,"");
  s=s.replace(/\s+/g," ").trim();
 }else if(/^(Read|Edit|Write|MultiEdit|NotebookEdit)$/.test(tool)){s=rel(s,ctx);}
 else if(tool==="Grep"||tool==="Glob"){s=s.split(" ").map(w=>w.charAt(0)==="/"?rel(w,ctx):w).join(" ");}
 if(ctx.home)s=s.split(ctx.home+"/").join("~/");
 const t=mid(s,88);return {text:t,full:full,changed:t!==full};}

// ---------------------------------------------------------------- attention (the server computes it; this only fills a gap)
function keyOf(v){return v.key||(v.project+"\u0000"+v.run_id);}
function attnOf(v){
 if(v.attention&&GROUPS.indexOf(v.attention.group)>=0)return v.attention;
 const lv=(v.live||{}).level,cls=(v.stall||{}).cls|0,s=v.status;let g="running";
 if(s==="done"||s==="aborted")g="done";else if(s==="paused")g="paused";
 else if(s==="awaiting_user"||s==="blocked"||lv==="wait")g="you";else if(cls>=2||lv==="stale")g="stalled";
 return {group:g,code:"working",rank:GROUPS.indexOf(g),level:lv,since:null,since_ts:null,head:L["ui_g_"+g],
  what:(v.live||{}).text||"",do:"",command:"",where:"none"};}
function sortItems(items){
 return items.slice().sort((x,y)=>{
  const gx=x.a.group,gy=y.a.group;if(gx!==gy)return GROUPS.indexOf(gx)-GROUPS.indexOf(gy);
  let d=0;
  if(gx==="you"){const kx=x.a.code==="turn_ended"?1:0,ky=y.a.code==="turn_ended"?1:0;if(kx!==ky)return kx-ky;}
  if(gx==="you"||gx==="stalled"){const ax=x.a.since_ts==null?Infinity:x.a.since_ts,ay=y.a.since_ts==null?Infinity:y.a.since_ts;
   d=ax===ay?0:ax<ay?-1:1;}
  else if(gx==="done"){d=(y.v.state_updated_ts||0)-(x.v.state_updated_ts||0);}
  return d||cmpStr(lc(x.v.project_name),lc(y.v.project_name))||cmpStr(String(x.v.run_id),String(y.v.run_id));});}

// ---------------------------------------------------------------- activity feed
function isRoutine(r){return r.kind==="tool"&&(r.tool==="Read"||r.tool==="Grep"||r.tool==="Glob"||String(r.tool).indexOf("mcp__")===0);}
function readsOf(g){return g.rows.reduce((n,r)=>n+(r.count||1),0);}
function kindOf(r){
 if(r.kind==="tool"){
  if(r.tool==="Bash")return "run";if(/^(Edit|Write|MultiEdit|NotebookEdit)$/.test(r.tool))return "edit";
  if(r.tool==="Read")return "read";if(r.tool==="Grep"||r.tool==="Glob")return "search";
  if(r.tool==="Agent"||r.tool==="Task"||r.tool==="SendMessage")return "agent";
  if(r.tool==="AskUserQuestion")return "question";return "other";}
 return {deny:"guard",idle:"turn_ended",wait_agents:"waiting",agent_done:"agent_done",result:"result",handback:"report",
  user:"user",answer:"answer"}[r.kind]||"other";}
function sameRow(a,b){return a.kind!=="deny"&&b.kind!=="deny"&&a.agent===b.agent&&a.kind===b.kind&&a.tool===b.tool&&a.what===b.what;}
function groupFeed(rows){
 const out=[];let run=null;
 rows.forEach(r=>{
  if(isRoutine(r)){
   if(!run){run={type:"reads",id:"rd:"+(r.id||r.ts),rows:[],n:0,ts:0};out.push(run);}
   run.rows.push(r);run.n+=r.count||1;run.ts=r.ts;return;}
  run=null;const prev=out[out.length-1];
  if(prev&&prev.type==="row"&&sameRow(prev.row,r)){prev.count+=r.count||1;prev.ts=r.ts;return;}
  out.push({type:"row",id:r.id||("r:"+r.ts+":"+r.what),row:r,count:r.count||1,ts:r.ts});});
 // a lone read or search shows its own path: only two or more collapse into one line
 return out.map(g=>g.type==="reads"&&readsOf(g)<2?{type:"row",id:g.rows[0].id||("r:"+g.rows[0].ts),row:g.rows[0],count:1,ts:g.ts}:g).reverse();}

// ---------------------------------------------------------------- state
const S={cards:new Map(),items:[],shown:[],filter:new Set(),q:"",older:false,many:false,lastGoodMs:0,fails:0,inflight:false,
 timer:0,nextAt:0,staleSrv:false,renderErr:false,started:false,open:{},finAll:false,sections:{},prev:new Map(),prevNeed:0,
 first:true,lastN:-2,pointer:0,deferred:0,force:false,ann:[],annAt:0,annFlip:false,reloading:false,printing:false,data:null,
 connPrev:"live",errKind:"net",errStatus:0,keysOn:true,help:false,barH:-1,rz:0,loadErr:null,
 ctl:null,ctlBusy:{},ctlBad:false,radarAll:false,radar:{recs:[],sig:""},toastT:0,
 nr:{busy:false,seq:0,engines:[],trigger:null},jbusy:{},rd:null,rdSeq:0,tlArm:{}};
function openOf(key){return S.open[key]||(S.open[key]={});}
function saveOpen(){const keys=Object.keys(S.open);if(keys.length>150)keys.slice(0,keys.length-150).forEach(k=>delete S.open[k]);
 lsSet("orkestra.dash.open",JSON.stringify(S.open));}

// ---------------------------------------------------------------- time bindings (one 1 s timer updates only the text that changed)
function bind(st,part,n,ts,fn){const r={n:n,ts:ts,fn:fn,part:part,off:false};st.recs.push(r);paintTime(r);return r;}
function paintTime(r){
 if(r.off||!r.fn)return;
 if(r.ts){setAttr(r.n,"datetime",new Date(r.ts*1000).toISOString());setAttr(r.n,"title",stamp(r.ts));}
 setText(r.n,r.fn(r.ts));}
function tickTimes(){S.cards.forEach(st=>{st.recs=st.recs.filter(r=>!r.off);st.recs.forEach(paintTime);});
 S.radar.recs=S.radar.recs.filter(r=>!r.off);S.radar.recs.forEach(paintTime);}

// ---------------------------------------------------------------- selection / focus guards
function selIn(node){
 try{const s=getSelection();if(s&&!s.isCollapsed&&s.anchorNode&&node.contains(s.anchorNode))return true;}catch(e){}
 return false;}
function pinned(node){
 if(S.pointer&&Date.now()-S.pointer<3000)return true;     // a press that never ended (context menu, drag out of the window) lets go by itself
 try{if(document.activeElement&&document.activeElement!==document.body&&node.contains(document.activeElement))return true;}catch(e){}
 return selIn(node);}
function place(box,want){
 const keep=new Set(want);
 Array.prototype.slice.call(box.children).forEach(c=>{if(!keep.has(c))box.removeChild(c);});
 let i=0;
 for(let k=0;k<want.length;k++){const w=want[k],cur=box.children[i];
  if(cur===w){i++;continue;}
  if(pinned(w))continue;
  box.insertBefore(w,cur||null);i++;}}

// ---------------------------------------------------------------- announcements and copy
function announce(msg){const n=$("live");S.annFlip=!S.annFlip;n.textContent=msg+(S.annFlip?"\u00a0":"");}
function queueAnn(msg){S.ann.push(msg);}
function flushAnn(){if(!S.ann.length||Date.now()-S.annAt<10000)return;S.annAt=Date.now();announce(S.ann.join(". "));S.ann=[];}
function selectNode(n){try{const r=document.createRange();r.selectNodeContents(n);const s=getSelection();s.removeAllRanges();s.addRange(r);}catch(e){}}
// the button says "Copied" for a moment: a sighted viewer sees the result too (the live region is for the others)
function flashCopied(b){
 if(!b||!b.dataset)return;
 if(b._t)clearTimeout(b._t);else b._label=b.textContent;
 setText(b,L.ui_copied);setAttr(b,"data-copied","true");
 b._t=setTimeout(()=>{setText(b,b._label);setAttr(b,"data-copied",null);b._t=0;},1600);}
function doCopy(text,node,btn){
 const fail=()=>{selectNode(node);announce(L.ui_copy_failed);};
 try{if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(()=>{announce(L.ui_copied);flashCopied(btn);},fail);return;}}catch(e){}
 fail();}
function copyBtn(text,node){const b=el("button","btn",L.ui_copy);b.type="button";b.dataset.act="copy";b.dataset.copy=text;
 setAttr(b,"aria-label",L.ui_copy+": "+text);b._for=node;return b;}

// ---------------------------------------------------------------- the full card
function newCard(key,inItem){
 const st={key:key,kind:"card",recs:[],group:"",r:{},feed:{rows:new Map(),dirty:true},dbuilt:false,d:null};
 const r=st.r,art=el("article","card");art.tabIndex=-1;art.dataset.key=key;st.el=art;
 // head
 const head=el("div","p-head"),ch=el("div","ch"),badge=el("span","badge");
 r.gl=el("span","gl");r.gl.setAttribute("aria-hidden","true");r.bw=el("span","bw");badge.append(r.gl,r.bw);
 r.asof=el("span","asof");r.asof.hidden=true;r.age=el("time","age");ch.append(badge,r.asof,r.age);
 if(inItem){const cb=el("button","tog",L.ui_collapse);cb.type="button";cb.dataset.act="collapse";ch.append(cb);}
 r.h3=el("h3");r.h3.id="h-"+key;
 r.title=el("p","title");r.title.id="t-"+key;const meta=el("p","meta");r.rid=el("code");r.rid.id="r-"+key;r.tags=el("span");meta.append(r.rid,r.tags);
 art.setAttribute("aria-labelledby",r.h3.id+" "+r.rid.id+" "+r.title.id);        // two runs of one project differ in their name
 r.what=el("p","now");r.do=el("p","do");const cr=el("div","cmdrow");r.cmd=el("span","cmd");r.cmd.hidden=true;r.cmdCode=el("code");
 r.cmd.append(r.cmdCode);r.cmdBtn=null;cr.append(r.cmd);
 r.ctl=el("div","ctl");r.ctl.hidden=true;r.ctlP=null;r.ctlA=null;
 head.append(ch,r.h3,r.title,meta,r.what,r.do,cr,r.ctl);
 // phase stepper
 const steps=el("div","p-steps");r.ol=el("ol","steps");r.ol.setAttribute("aria-hidden","true");     // the line below says the same in words
 r.lis=PH.map(()=>el("li"));r.lis.forEach(li=>r.ol.append(li));
 r.pl=el("p","pl");r.plMain=el("b");r.plSince=el("time");r.pl.append(r.plMain,r.plSince);
 r.clk=el("time","clk");steps.append(r.ol,r.pl,r.clk);
 // tasks
 const tasks=el("div","p-tasks");r.tbar=el("div","tbar");r.tbar.setAttribute("role","img");
 r.tbD=el("i","d");r.tbP=el("i","p");r.tbB=el("i","b");r.tbar.append(r.tbD,r.tbP,r.tbB);
 r.tl=el("p","tl");r.tlCur=el("p","tl");r.tlTog=el("button","tog");r.tlTog.type="button";r.tlTog.dataset.act="tasks";
 r.tlTog.setAttribute("aria-expanded","false");r.tlBox=el("div","ttable");r.tlBox.hidden=true;
 tasks.append(r.tbar,r.tl,r.tlCur,r.tlTog,r.tlBox);
 // health
 const health=el("div","chips");
 // risky actions of this run
 const risk=el("div","p-risk");risk.hidden=true;r.rh=el("p","rh");r.rhS=el("span","mut");r.rh.append(el("span","",L.ui_risk_h),r.rhS);
 r.rul=el("ul","rlist");r.rtog=el("button","tog");r.rtog.type="button";r.rtog.dataset.act="risks";r.rtog.hidden=true;
 r.rtog.setAttribute("aria-expanded","false");risk.append(r.rh,r.rul,r.rtog);
 // run documents and the conversation of a background run
 const docs=el("div","docs");docs.hidden=true;r.docsBox=el("span","dl");docs.append(el("span","mut",L.ui_docs),r.docsBox);
 const job=el("div","p-job");job.hidden=true;r.jh=el("p","jh");r.jhS=el("span","rk");r.jhV=el("span","mut");r.jh.append(el("span","",L.ui_job_h),r.jhS,r.jhV);
 r.jmsg=el("div","jmsg");r.jmsg.dir="auto";r.jmore=el("button","tog");r.jmore.type="button";r.jmore.dataset.act="jmore";r.jmore.hidden=true;
 r.jbtns=el("div","ctl");r.jbtns.hidden=true;r.jb={};
 r.jform=el("div","jform");r.jform.hidden=true;r.jin=el("textarea");r.jin.id="ji-"+key;r.jin.dir="auto";r.jin.setAttribute("rows","2");
 const jl=el("label","dlg-lbl",L.ui_job_reply);jl.setAttribute("for",r.jin.id);r.jerr=el("p","dlg-err");r.jerr.id="je-"+key;r.jerr.hidden=true;
 r.jin.setAttribute("aria-describedby",r.jerr.id);r.jsend=el("button","btn cbtn primary",L.ui_job_send);r.jsend.type="button";r.jsend.dataset.act="jsend";
 r.jform.append(jl,r.jin,r.jerr,r.jsend);r.jnote=el("p","hint");
 r.jturns=el("details","jturns");r.jturnsS=el("summary");r.jturnsL=el("ul","jt");r.jturns.append(r.jturnsS,r.jturnsL);
 job.append(r.jh,r.jmsg,r.jmore,r.jbtns,r.jform,r.jnote,r.jturns);
 // activity (a real <details>) and details
 const act=el("details","act"),sum=el("summary");r.actLbl=el("span");r.actLast=el("time","mut");sum.append(r.actLbl,r.actLast);
 r.ul=el("ul","feed");r.moreBtn=el("button","tog");r.moreBtn.type="button";r.moreBtn.dataset.act="feedmore";r.moreBtn.hidden=true;
 act.append(sum,r.ul,r.moreBtn);
 const det=el("details","dtl"),dsum=el("summary",""),dbox=el("div","dbox");dsum.textContent=L.ui_details;det.append(dsum,dbox);
 st.parts={head:head,steps:steps,tasks:tasks,health:health,risk:risk,docs:docs,job:job,act:act,det:det};st.dbox=dbox;st.act=act;st.det=det;r.health=health;
 art.append(head,steps,tasks,docs,health,risk,job,act,det);
 act._want=false;det._want=false;
 act.addEventListener("toggle",()=>{remember(key,"act",act);if(act.open&&st.v)patchFeed(st,st.v,st.a);});
 det.addEventListener("toggle",()=>{remember(key,"det",det);if(det.open&&st.v)patchDetails(st,st.v,st.a);});
 return st;}

// The page opens and closes the disclosures itself (first view, printing): that is not the viewer's choice and is not stored.
// `_want` is the state the page asked for last; a toggle that ends in another state is the viewer's.
function remember(key,field,det){
 if(det._want===det.open)return;
 det._want=det.open;const o=openOf(key);if(o[field]!==det.open){o[field]=det.open;saveOpen();}}
function badgeParts(a){return {gl:FAMILY[a.code]||GLYPH[a.group],bw:a.head||L["ui_g_"+a.group]};}

function patchHead(st,v,a){
 const r=st.r,b=badgeParts(a);
 setText(r.gl,b.gl);setText(r.bw,b.bw);
 const g=a.group;
 if((g==="you"||g==="stalled")&&a.since_ts){
  if(!st.ageRec){st.ageRec=bind(st,"head",r.age,a.since_ts,null);}
  st.ageRec.off=false;st.ageRec.ts=a.since_ts;
  st.ageRec.fn=g==="you"?(ts=>fmt(L.ui_age_waiting,{dur:dur(nowS()-ts)})):a.code==="hung"?(ts=>fmt(L.ui_age_hung,{ago:ago(nowS()-ts)}))
   :(ts=>fmt(L.ui_age_stalled,{dur:dur(nowS()-ts)}));
  paintTime(st.ageRec);
 }else{if(st.ageRec)st.ageRec.off=true;setText(r.age,"");}
 setText(r.h3,v.project_name);setText(r.title,v.title);setAttr(r.title,"title",v.title);
 setText(r.rid,v.run_id);
 setText(r.tags," \u00b7 "+[v.size,v.engine].concat(v.mode==="headless"?["headless"]:[]).filter(Boolean).join(" \u00b7 "));
 setText(r.what,a.what||"");setText(r.do,a.do||"");
 if(a.command){
  show(r.cmd,true);setText(r.cmdCode,a.command);
  if(!r.cmdBtn){r.cmdBtn=copyBtn(a.command,r.cmdCode);r.cmd.append(r.cmdBtn);}
  setData(r.cmdBtn,"copy",a.command);setAttr(r.cmdBtn,"aria-label",L.ui_copy+": "+a.command);
 }else show(r.cmd,false);
 if(v.job&&v.job.can_reply&&canAct()&&(a.group==="you"||a.group==="paused"||a.group==="stalled")){setText(r.do,L.ui_job_here);show(r.cmd,false);}
 patchControls(st,v,a);}

// ---------------------------------------------------------------- Pause / Abort (the server checks token, origin and the run again)
function canControl(v,a){return !!META.controls&&!!META.token&&OPEN_ST.indexOf(v.status)>=0&&(a||{}).code!=="unreadable";}
function ctlBtn(action){
 const b=el("button","btn cbtn "+(action==="abort"?"danger":"warn")),ic=el("span","ic",action==="abort"?"\u2715":"\u2161");
 ic.setAttribute("aria-hidden","true");b.append(ic,document.createTextNode(action==="abort"?L.ui_ctl_abort:L.ui_ctl_pause));
 b.type="button";b.dataset.act="ctl";b.dataset.action=action;return b;}
function patchControls(st,v,a){
 const r=st.r,on=canControl(v,a);show(r.ctl,on);if(!on)return;
 if(!r.ctlP){r.ctlP=ctlBtn("pause");r.ctlA=ctlBtn("abort");r.ctl.append(r.ctlP,r.ctlA);}
 show(r.ctlP,v.status!=="paused");
 const name=v.project_name+" "+v.run_id,busy=!!S.ctlBusy[st.key];
 setAttr(r.ctlP,"aria-label",fmt(L.ui_ctl_pause_aria,{name:name}));setAttr(r.ctlA,"aria-label",fmt(L.ui_ctl_abort_aria,{name:name}));
 [r.ctlP,r.ctlA].forEach(b=>{if(b.disabled!==busy)b.disabled=busy;});}
function ctlErr(msg,field){
 const e=$("ctl-err");setText(e,msg||"");show(e,!!msg);S.ctlBad=!!(msg&&field);
 setAttr($("ctl-reason"),"aria-invalid",S.ctlBad?"true":null);if(msg)announce(msg);}
function ctlLabels(c){const ok=$("ctl-ok"),ab=c.action==="abort";
 setText(ok,S.ctlBusy[c.key]?(ab?L.ui_ctl_busy_abort:L.ui_ctl_busy_pause):(ab?L.ui_ctl_ok_abort:L.ui_ctl_ok_pause));
 setCls(ok,"btn cbtn "+(ab?"danger":"warn"));ok.disabled=!!S.ctlBusy[c.key];}
function openCtl(st,action,btn){
 const v=st.v;if(!v||!canControl(v,st.a))return;
 const c=S.ctl={key:st.key,action:action,btn:btn,name:v.project_name+" "+v.run_id,project:v.project_name},d=$("ctl");
 setText($("ctl-h"),action==="abort"?L.ui_ctl_abort_h:L.ui_ctl_pause_h);
 setText($("ctl-run"),[v.project_name,v.run_id,v.title].filter(Boolean).join(" \u00b7 "));
 setText($("ctl-p"),action==="abort"?L.ui_ctl_abort_p:L.ui_ctl_pause_p);
 setText($("ctl-reason-l"),L.ui_ctl_reason);setAttr($("ctl-reason"),"placeholder",L.ui_ctl_reason_ph);$("ctl-reason").value="";
 setText($("ctl-cancel"),L.ui_ctl_cancel);ctlErr("");ctlLabels(c);setData(d,"action",action);
 try{if(typeof d.showModal==="function"){if(!d.open)d.showModal();}else setAttr(d,"open","");}catch(e){setAttr(d,"open","");}
 try{$("ctl-reason").focus();}catch(e){}}
function closeCtl(){
 const d=$("ctl"),c=S.ctl;S.ctl=null;
 try{if(typeof d.close==="function"){if(d.open)d.close();}else setAttr(d,"open",null);}catch(e){setAttr(d,"open",null);}
 try{if(c&&c.btn&&!c.btn.hidden&&!c.btn.disabled)c.btn.focus();}catch(e){}}
function toast(msg){const t=$("toast");setText(t,msg);show(t,true);clearTimeout(S.toastT);S.toastT=setTimeout(()=>show(t,false),6000);}
// every request the person's clicks send goes to this server (same origin): a POST carries the page's token
async function req(path,opts){
 opts=opts||{};const post=opts.body!==undefined;
 const ctl=typeof AbortController==="function"?new AbortController():null,to=ctl?setTimeout(()=>ctl.abort(),opts.timeout||25000):0;
 try{
  const r=await fetch(path,{method:post?"POST":"GET",cache:"no-store",signal:ctl?ctl.signal:undefined,
   headers:post?{"Content-Type":"application/json","X-Orkestra-Token":META.token||""}:{},
   body:post?JSON.stringify(Object.assign({lang:LANG},opts.body)):undefined});
  let data=null;try{data=await r.json();}catch(e){}
  return data&&typeof data==="object"&&!Array.isArray(data)?data:{ok:false,error:"http",status:r.status};
 }catch(e){return {ok:false,error:"net"};}finally{clearTimeout(to);}}
function sendControl(key,action,reason){return req("/api/control",{body:{key:key,action:action,reason:reason},timeout:15000});}
function ctlErrText(res,c,reason){
 const e=(res&&res.error)||"net";
 if(e==="token")return L.ui_ctl_err_token;if(e==="closed")return L.ui_ctl_err_closed;if(e==="not_found")return L.ui_ctl_err_not_found;
 if(e==="read_only")return L.ui_ctl_err_read_only;if(e==="origin"||e==="host")return L.ui_ctl_err_origin;
 if(e==="reason_long")return fmt(L.ui_ctl_err_reason_long,{n:reason.length});if(e==="net")return L.ui_ctl_err_net;
 const cmd="orkestra "+c.action+(c.action==="abort"?' --reason "'+(reason||"dashboard").replace(/"/g,"'")+'"':"")+" -p "+c.project;
 return fmt(L.ui_ctl_err_failed,{detail:(res&&(res.detail||res.error))||("HTTP "+((res&&res.status)||"?")),cmd:cmd});}
async function submitCtl(ev){
 if(ev&&ev.preventDefault)ev.preventDefault();
 const c=S.ctl;if(!c||S.ctlBusy[c.key])return;
 const reason=String($("ctl-reason").value||"").replace(/\s+/g," ").trim();
 if(reason.length>REASON_MAX){ctlErr(fmt(L.ui_ctl_err_reason_long,{n:reason.length}),true);try{$("ctl-reason").focus();}catch(e){}return;}
 S.ctlBusy[c.key]=true;ctlErr("");ctlLabels(c);refreshControls(c.key);
 const res=await sendControl(c.key,c.action,reason);
 S.ctlBusy[c.key]=false;refreshControls(c.key);
 if(res&&res.ok){
  const msg=fmt(c.action==="abort"?L.ui_ctl_done_abort:L.ui_ctl_done_pause,{name:c.name,n:res.stopped|0});
  if(S.ctl===c)closeCtl();toast(msg);announce(msg);retryNow();return;}
 if(S.ctl===c){ctlLabels(c);ctlErr(ctlErrText(res,c,reason),!!res&&res.error==="reason_long");}}
function refreshControls(key){const st=S.cards.get(key)||S.cards.get(key+"#c");if(st&&st.kind==="card"&&st.v)patchControls(st,st.v,st.a);
 const inner=S.cards.get(key+"#c");if(inner&&inner!==st&&inner.v)patchControls(inner,inner.v,inner.a);}

function stepState(i,idx,v,skipped){
 if(v.status==="done")return "done";
 if(i<idx)return skipped.indexOf(PH[i])>=0?"skipped":"done";
 return i===idx?"current":"todo";}
function patchSteps(st,v,a){
 const r=st.r,idx=v.phase_index|0,skipped=v.phases_skipped||[],spent={};
 (v.phase_history||[]).forEach(h=>{spent[h.phase]=(spent[h.phase]||0)+(h.seconds||0);});
 PH.forEach((p,i)=>{
  const s=stepState(i,idx,v,skipped),li=r.lis[i];
  setCls(li,s==="done"?"sd":s==="current"?"sc":s==="skipped"?"sk":"");
  setText(li,L["phase_"+p]+": "+L["ui_step_"+s]);
  setAttr(li,"title",L["phase_"+p]+(spent[p]?" \u00b7 "+dur(spent[p]):""));
  setAttr(li,"aria-current",s==="current"?"step":null);});
 setAttr(r.ol,"aria-label",fmt(L.ui_phase_of,{n:idx+1,total:PH.length,name:v.phase_label||v.phase}));
 setText(r.plMain,fmt(L.ui_phase_of,{n:idx+1,total:PH.length,name:v.phase_label||v.phase}));
 if(v.phase_since&&v.status!=="done"){
  if(!st.phRec)st.phRec=bind(st,"steps",r.plSince,v.phase_since,null);
  st.phRec.off=false;st.phRec.ts=v.phase_since;st.phRec.fn=ts=>" \u00b7 "+fmt(L.in_phase_for,{dur:dur(nowS()-ts)});paintTime(st.phRec);
 }else{if(st.phRec)st.phRec.off=true;setText(r.plSince,"");}
 const line=clockLine(v);
 show(r.clk,!!line);
 if(!st.clkRec)st.clkRec=bind(st,"steps",r.clk,v.now||0,()=>clockLine(st.v||v));
 st.clkRec.off=!line;st.clkRec.ts=v.now||0;st.clkRec.fn=()=>clockLine(st.v||v);
 if(line)paintTime(st.clkRec);else setText(r.clk,"");}

function taskCounts(v){
 const c=v.tasks_by_status;if(c)return c;
 const o={done:0,in_progress:0,blocked:0,pending:0,deferred:0};
 (v.tasks||[]).forEach(t=>{o[t.status in o?t.status:"pending"]++;});return o;}
function testsOf(t){return t.tests||(t.evidence==="RG"?"passed":t.evidence&&t.evidence.charAt(0)==="R"?"awaiting_green":
 t.tdd==="n/a"?"na":"none");}
function testsKey(t){return TMAP[testsOf(t)]||"none";}
function showTestsCol(v){
 if(typeof v.tasks_show_tests==="boolean")return v.tasks_show_tests;
 return (v.tasks||[]).some(t=>testsKey(t)!=="na"||(t.tdd||"required")!=="n/a");}
function cellStatus(t){
 const s=t.status||"pending",td=el("td","st st-"+s),g=el("span","g",TSG[s]||"");
 td.append(g,document.createTextNode(" "+(L["ui_ts_"+s]||s)));return td;}
function cellTests(t){
 const k=testsKey(t),td=el("td","tests tests-"+k);
 if(k==="na")return td;
 const g=el("span","g",TG[k]||"");
 td.append(g,document.createTextNode(" "+(L["ui_tests_"+k]||k)));return td;}
function patchTasks(st,v,a){
 const r=st.r,total=v.tasks_total|0,box=st.parts.tasks;
 show(box,total>0);if(!total)return;
 const c=taskCounts(v),fin=(c.done||0)+(c.deferred||0);
 const bits=[fmt(L.tasks_done,{done:v.tasks_done,total:total})];
 if(c.in_progress)bits.push(fmt(L.ui_t_progress,{n:c.in_progress}));
 if(c.blocked)bits.push(fmt(L.ui_t_blocked,{n:c.blocked}));
 if(c.pending)bits.push(fmt(L.ui_t_todo,{n:c.pending}));
 setText(r.tl,bits.join(" \u00b7 "));setAttr(r.tbar,"aria-label",bits.join(", "));
 [[r.tbD,fin],[r.tbP,c.in_progress],[r.tbB,c.blocked]].forEach(x=>{const w=(100*(x[1]||0)/total).toFixed(2)+"%";
  if(x[0].style.width!==w)x[0].style.width=w;show(x[0],!!x[1]);});
 const cur=(v.current_tasks||[]).map(t=>t.id+" "+t.title).join(" \u00b7 ");setText(r.tlCur,cur);show(r.tlCur,!!cur);
 const open=!!openOf(st.key).tasks;
 setText(r.tlTog,open?L.ui_t_hide:fmt(L.ui_t_all,{n:total}));setAttr(r.tlTog,"aria-expanded",open?"true":"false");
 show(r.tlBox,open);
 if(open){const withT=showTestsCol(v);
  const sig=JSON.stringify([(v.tasks||[]).map(t=>[t.id,t.status,t.title,testsOf(t),t.tdd||""]),withT]);
  if(st.tsig!==sig){st.tsig=sig;buildTable(st,v);}}}
function buildTable(st,v){
 const box=st.r.tlBox,withT=showTestsCol(v),tb=el("table","tt"+(withT?"":" no-tests")),
  cap=el("caption","vh",L.ui_tasks),hd=el("thead"),hr=el("tr"),body=el("tbody");
 const heads=withT?[L.ui_t_col_id,L.ui_t_col_task,L.ui_t_col_status,L.ui_t_col_tests]
  :[L.ui_t_col_id,L.ui_t_col_task,L.ui_t_col_status];
 heads.forEach(h=>{const th=el("th","",h);th.setAttribute("scope","col");hr.append(th);});
 hd.append(hr);
 (v.tasks||[]).forEach(t=>{const tr=el("tr");
  const tt=el("td","",t.title);tt.dir="auto";
  tr.append(el("td","id",t.id),tt,cellStatus(t));
  if(withT)tr.append(cellTests(t));
  body.append(tr);});
 tb.append(cap,hd,body);box.replaceChildren(tb);}

function patchHealth(st,v,a){
 const box=st.r.health,gs=v.gates_struct,g=v.gates||{},n=v.notices||[],tm=v.timing||{};
 const showAll=!!openOf(st.key).notices;
 const sig=JSON.stringify([g.baseline,g.verify,gs&&gs.verify&&gs.verify.state,tm.reopens,tm.tasks_reopened,n.map(x=>x.text),showAll]);
 if(st.hsig===sig)return;st.hsig=sig;
 const kids=[];
 if(g.baseline&&g.baseline!=="\u2014")kids.push(el("span","gchip",L.ui_gate_baseline+" "+g.baseline));
 if(g.verify&&g.verify!=="\u2014"){
  const bad=gs&&gs.verify?gs.verify.state==="fail":g.verify.indexOf("\u2717")>=0;
  kids.push(el("span","gchip"+(bad?" bad":""),L.ui_gate_verify+" "+g.verify));}
 if(tm.reopens>0)kids.push(el("span","gchip",fmt(L.pace_rejects,{n:tm.reopens,tasks:tm.tasks_reopened})));
 st.notes=[];
 n.slice(0,showAll?n.length:2).forEach(x=>{const w=el("div","nwrap"),p=el("p","notice","\u2691 "+x.text),b=el("button","tog",L.ui_show_more);
  p.title=x.text;b.type="button";b.dataset.act="noticetxt";b.hidden=true;b.setAttribute("aria-expanded","false");w.append(p,b);kids.push(w);st.notes.push({p:p,b:b});});
 if(n.length>2){const b=el("button","tog",showAll?L.ui_act_less:fmt(L.ui_act_more,{n:n.length-2}));b.type="button";b.dataset.act="notices";kids.push(b);}
 box.replaceChildren.apply(box,kids);fitNotes(st);}
// Text cut by the line clamp must be reachable by touch and keyboard too: the toggle follows the LAYOUT (is it cut right now?),
// and only where nothing can be measured (a closed disclosure, no layout engine) a long text is assumed to be cut.
const LONG_ROW=56,LONG_NOTE=110;
function clipped(n,text,long){
 try{if(n.clientHeight>0)return n.scrollHeight>n.clientHeight+1;}catch(e){}
 return String(text||"").length>long;}
function fitNotes(st){
 (st.notes||[]).forEach(x=>{
  let w=0;try{w=x.p.clientWidth|0;}catch(e){}
  const open=x.p.className.indexOf("open")>=0,sig=x.p.textContent+"|"+w+"|"+open;if(x.sig===sig)return;x.sig=sig;
  show(x.b,open||clipped(x.p,x.p.textContent,LONG_NOTE));});}
function toggleNote(b){
 const p=b.previousSibling;if(!p)return;const open=p.className.indexOf("open")<0;
 setCls(p,open?"notice open":"notice");setText(b,open?L.ui_act_less:L.ui_show_more);setAttr(b,"aria-expanded",open?"true":"false");}

// ---- risky actions
function riskTop(c){return c&&c.high?"high":c&&c.medium?"medium":"blocked";}
function riskSum(c){c=c||{};return fmt(L.ui_risk_sum,{high:c.high|0,medium:c.medium|0,blocked:c.blocked|0});}
function riskLevel(e){return e.outcome==="blocked"?"blocked":(e.level==="high"?"high":"medium");}
function riskRow(e,ctx,where){
 const lvl=riskLevel(e),li=el("li","ri"),pill=el("span","rk rk-"+lvl,L["lvl_"+lvl]||lvl),body=el("div","rb"),tim=el("time");
 body.append(el("span","rl",e.label||e.code||""));
 if(e.what){const s=shortWhat(e.tool||"Bash",e.what,ctx),code=el("code","",s.text);code.dir="auto";setAttr(code,"title",e.what);body.append(code);}
 if(e.outcome==="blocked"&&e.reason&&!where){const why=el("span","rr",e.reason);why.dir="auto";setAttr(why,"title",e.reason);body.append(why);}   // the radar stays brief: the card says why
 const bits=[];if(where)bits.push(where);if(e.agent)bits.push(fmt(L.ui_risk_by,{agent:e.agent}));
 if(e.outcome==="blocked"&&e.guard)bits.push(fmt(L.ui_risk_guard,{guard:e.guard}));
 if((e.count|0)>1)bits.push(fmt(L.ui_act_times,{n:e.count}));
 const foot=el("div","rf"),m=el("span","rm",bits.join(" \u00b7 "));m.dir="auto";foot.append(m);body.append(foot);
 li.append(pill,body,tim);li.dataset.level=lvl;
 return {li:li,body:body,foot:foot,tim:tim};}
function patchRisk(st,v,a){
 const r=st.r,list=v.risks||[],c=v.risk_counts||{},box=st.parts.risk;
 show(box,list.length>0);
 if(!list.length){(st.riskRecs||[]).forEach(x=>{x.off=true;});st.riskRecs=[];st.rsig="";return;}
 setData(box,"top",riskTop(c));setText(r.rhS,riskSum(c));
 const all=!!openOf(st.key).risk,shown=all?list:list.slice(0,RISK_SHOW);
 show(r.rtog,list.length>RISK_SHOW);setText(r.rtog,all?L.ui_risk_less:fmt(L.ui_risk_all,{n:list.length}));setAttr(r.rtog,"aria-expanded",all?"true":"false");
 const sig=JSON.stringify([shown.map(e=>[e.id,e.count,e.ts,e.label]),all]);if(st.rsig===sig)return;st.rsig=sig;
 (st.riskRecs||[]).forEach(x=>{x.off=true;});st.riskRecs=[];
 const ctx={project:v.project,home:v.home||META.home||""},kids=[];
 shown.forEach(e=>{const x=riskRow(e,ctx,"");kids.push(x.li);st.riskRecs.push(bind(st,"risk",x.tim,e.ts,rowTime));});
 r.rul.replaceChildren.apply(r.rul,kids);}

// ---- feed
function agentPrefix(r){return (r.kind==="tool"||r.kind==="deny")&&(r.main===false||(r.main===undefined&&r.agent&&r.agent!=="main"))?r.agent+" \u00b7 ":"";}
// "Turn ended  turn ended": the badge already names the kind, so the text keeps only what it adds
function plainText(r,label){
 if(r.brief!==undefined)return r.brief;
 const t=String(r.what||""),l=label.toLowerCase();
 if(l&&t.toLowerCase().indexOf(l)===0)return t.slice(l.length).replace(/^[\s:\u00b7,-]+/,"");
 return t;}
function rowSummary(r,ctx){
 if(r.kind==="idle"||r.kind==="wait_agents"||r.kind==="agent_done"||(r.kind==="tool"&&r.tool==="AskUserQuestion")){
  const t=plainText(r,L["ui_k_"+kindOf(r)]);return {text:t,full:r.what||"",changed:false};}
 if(r.kind==="deny"){
  const reason=r.reason!==undefined?r.reason:String(r.what||"").replace(/^\s*\[[^\]]*\]\s*/,"");
  const att=r.attempt?fmt(L.ui_act_blocked,{cmd:shortWhat("Bash",r.attempt,ctx).text}):reason;
  return {text:agentPrefix(r)+att,full:r.attempt||r.what||"",changed:!!r.attempt&&shortWhat("Bash",r.attempt,ctx).changed};}
 const s=shortWhat(r.tool,r.what,ctx);return {text:agentPrefix(r)+s.text,full:r.what||"",changed:s.changed};}
function catOf(r){const c=r.category||((/^\s*\[([^\]]*)\]/.exec(r.what||"")||[])[1])||"general";
 return fmt(L.guard_what,{category:c==="general"?L.guard_general:c});}
function buildRow(it){
 const li=el("li","fr"),k=el("span","k"),wh=el("div","wh"),sm=el("span","sum"),tim=el("time");
 li.dataset.id=it.id;wh.append(sm);li.append(k,wh,tim);
 return {id:it.id,li:li,k:k,wh:wh,sm:sm,tim:tim,cnt:null,cat:null,tog:null,full:null,fullOpen:false,rec:null};}
function updateRow(st,rec,it,ctx){
 let word,sm,cls="k";
 if(it.type==="reads"){word=L.ui_k_read;sm={text:fmt(L.ui_act_reads,{n:it.n}),full:"",changed:true};}
 else{const r=it.row,kd=kindOf(r);word=L["ui_k_"+kd];sm=rowSummary(r,ctx);
  if(kd==="guard"){cls+=" guard";word=it.count>1?fmt(L.guard_n,{count:it.count}):L.guard;}
  else if(kd==="question")cls+=" q";else if(/^(result|agent_done|report)$/.test(kd))cls+=" res";}
 setCls(rec.k,cls);setText(rec.k,word);setText(rec.sm,sm.text);setAttr(rec.sm,"title",sm.full||null);
 const isDeny=it.type==="row"&&it.row.kind==="deny";
 const wantCnt=it.type==="row"&&!isDeny&&it.count>1;
 if(wantCnt){if(!rec.cnt){rec.cnt=el("span","cnt");rec.wh.append(rec.cnt);}
  setText(rec.cnt,"\u00d7"+it.count);setAttr(rec.cnt,"title",fmt(L.ui_act_times,{n:it.count}));}
 else if(rec.cnt){rec.cnt.remove();rec.cnt=null;}
 if(isDeny){if(!rec.cat){rec.cat=el("span","cat");rec.wh.append(rec.cat);}setText(rec.cat,catOf(it.row));}
 else if(rec.cat){rec.cat.remove();rec.cat=null;}
 const rk=it.type==="row"?it.row.risk:null,rl=rk?(isDeny?"blocked":(rk.level==="high"?"high":"medium")):null;
 setAttr(rec.li,"data-risk",rl);
 if(rk){if(!rec.rk){rec.rk=el("span","rk");rec.wh.append(rec.rk);}setCls(rec.rk,"rk rk-"+rl);setText(rec.rk,rk.label||"");setAttr(rec.rk,"title",L["lvl_"+rl]||null);}
 else if(rec.rk){rec.rk.remove();rec.rk=null;}
 rec.sm_=sm;
 const wantTog=sm.changed||!!rec.clip||rec.fullOpen;
 if(wantTog&&!rec.tog){rec.tog=el("button","tog");rec.tog.type="button";rec.tog.dataset.act="rowfull";rec.tog.setAttribute("aria-expanded","false");rec.wh.append(rec.tog);}
 if(!wantTog&&rec.tog){rec.tog.remove();rec.tog=null;}
 if(rec.tog)setText(rec.tog,togLabel(it,rec.fullOpen));
 rec.it=it;rec.ctx=ctx;
 if(!rec.trec){rec.trec=bind(st,"feed",rec.tim,it.ts,rowTime);}
 rec.trec.ts=it.ts;paintTime(rec.trec);}
function togLabel(it,open){
 if(it.type==="reads")return open?L.ui_act_less:fmt(L.ui_act_more,{n:it.n});
 const cmd=it.row.kind==="deny"?!!it.row.attempt:it.row.tool==="Bash";          // "full command" only for a command
 return cmd?(open?L.ui_hide_full:L.ui_show_full):(open?L.ui_act_less:L.ui_show_more);}
// re-decide, once the rows are in the page, which ones are cut (and again when the width changes)
function fitFeed(st){
 st.feed.rows.forEach(rec=>{
  if(!rec.sm_||!rec.it)return;
  let w=0;try{w=rec.sm.clientWidth|0;}catch(e){}
  const sig=rec.sm_.text+"|"+w;if(rec.fitSig===sig)return;rec.fitSig=sig;
  const want=clipped(rec.sm,rec.sm_.text,LONG_ROW);
  if(want===!!rec.clip)return;
  rec.clip=want;const on=rec.sm_.changed||want||rec.fullOpen;
  if(on&&!rec.tog){rec.tog=el("button","tog");rec.tog.type="button";rec.tog.dataset.act="rowfull";rec.tog.setAttribute("aria-expanded",rec.fullOpen?"true":"false");rec.wh.append(rec.tog);}
  if(!on&&rec.tog){rec.tog.remove();rec.tog=null;}
  if(rec.tog)setText(rec.tog,togLabel(rec.it,rec.fullOpen));});}
function fitAll(force){S.cards.forEach(st=>{if(st.kind!=="card")return;
 if(force){st.feed.rows.forEach(rec=>{rec.fitSig="";});(st.notes||[]).forEach(x=>{x.sig="";});}
 fitFeed(st);fitNotes(st);});}
function toggleFull(st,rec){
 rec.fullOpen=!rec.fullOpen;
 if(rec.fullOpen&&!rec.full){
  rec.full=el("div","full");const pre=el("pre");rec.full.append(pre);rec.li.append(rec.full);rec.pre=pre;}
 if(rec.fullOpen){
  const it=rec.it;
  if(it.type==="reads"){setText(rec.pre,it.rows.slice().reverse().map(r=>L["ui_k_"+kindOf(r)]+"  "+shortWhat(r.tool,r.what,rec.ctx).text).join("\n"));}
  else{setText(rec.pre,rowSummary(it.row,rec.ctx).full);
   if(!rec.fcopy){rec.fcopy=copyBtn(rowSummary(it.row,rec.ctx).full,rec.pre);rec.full.append(rec.fcopy);}}}
 if(rec.full)show(rec.full,rec.fullOpen);
 if(rec.tog){setText(rec.tog,togLabel(rec.it,rec.fullOpen));setAttr(rec.tog,"aria-expanded",rec.fullOpen?"true":"false");}}
function maxRows(){try{return window.matchMedia&&window.matchMedia("(max-width:600px)").matches?PHONE_ROWS:MAX_ROWS;}catch(e){return MAX_ROWS;}}
function patchFeed(st,v,a){
 const r=st.r,f=st.feed,rows=v.activity_feed||v.activity||[];
 setText(r.actLbl,L.ui_act);
 const lastTs=rows.length?rows[rows.length-1].ts:(v.last_event_ts||0);
 if(lastTs){if(!st.lastRec)st.lastRec=bind(st,"actlast",r.actLast,lastTs,null);
  st.lastRec.off=false;st.lastRec.ts=lastTs;st.lastRec.fn=ts=>fmt(L.ui_act_last,{ago:ago(nowS()-ts)});paintTime(st.lastRec);}
 else{if(st.lastRec)st.lastRec.off=true;setText(r.actLast,"");}
 if(S.many&&!st.act.open&&!S.printing){f.dirty=true;return;}
 f.dirty=false;
 const ctx={project:v.project,home:v.home||META.home||""},items=groupFeed(rows),more=!!openOf(st.key).more,cap=maxRows();
 const shown=items.slice(0,more?items.length:cap),want=[];
 shown.forEach(it=>{let rec=f.rows.get(it.id);
  if(!rec){rec=buildRow(it);f.rows.set(it.id,rec);if(f.born&&!S.printing){rec.li.classList.add("new");}}
  updateRow(st,rec,it,ctx);want.push(rec.li);});
 f.born=true;
 f.rows.forEach((rec,id)=>{if(!shown.some(it=>it.id===id)){if(rec.trec)rec.trec.off=true;f.rows.delete(id);}});
 if(!items.length){if(!f.none){f.none=el("li","fr");f.none.append(el("span","",L.ui_act_none));}want.push(f.none);}
 place(r.ul,want);fitFeed(st);
 const hidden=items.length-cap;show(r.moreBtn,hidden>0);
 if(hidden>0)setText(r.moreBtn,more?L.ui_act_less:fmt(L.ui_act_more,{n:hidden}));}

// ---- details (the technical lines, legacy templates): a fixed set of rows, each updated in place
function buildDetails(st){
 const d=st.d={},box=st.dbox;
 const mk=(name,label)=>{const p=el("p","row"),v=el("span");if(label)p.append(el("b","",label+": "));p.append(v);d[name]={p:p,v:v};box.append(p);return d[name];};
 mk("title","");mk("tech",L.ui_tech);mk("stall","");mk("pace",L.label_pace);mk("gates",L.ui_gates);mk("sess","");mk("tokens",L.label_tokens);
 mk("ledger",L.label_ledger);const f=mk("folder",L.ui_run_folder);f.code=el("code");f.v.append(f.code);f.v.append(" ");f.btn=null;
 const sg=mk("signal","");sg.t=el("time");sg.v.append(sg.t);}
function patchDetails(st,v,a){
 if(S.many&&!st.det.open&&!S.printing)return;
 if(!st.d)buildDetails(st);
 const d=st.d,row=(r,text)=>{setText(r.v,text);show(r.p,text!=="");};
 row(d.title,v.title);row(d.tech,(v.live||{}).text||"");
 const mark=v.stall&&L["stall_mark_"+v.stall.cls];
 row(d.stall,mark?fmt(L.ui_stall,{mark:mark,text:v.stall.text}):"");
 const tm=v.timing,bits=[];
 if(tm&&tm.run){bits.push(fmt(L.pace_total,{dur:dur(tm.run)}));
  if(tm.task_avg)bits.push(fmt(L.pace_per_task,{dur:dur(tm.task_avg)}));
  if(tm.reopens)bits.push(fmt(L.pace_rejects,{n:tm.reopens,tasks:tm.tasks_reopened}));}
 row(d.pace,bits.join(" \u00b7 "));
 const g=v.gates||{};row(d.gates,fmt(L.gates_line,{baseline:g.baseline,verify:g.verify}));
 row(d.sess,v.sessions&&v.sessions.length?L.ui_sessions+": "+v.sessions.map(x=>x.session).join(", "):L.ui_no_sessions);
 const u=v.usage;
 if(u){let t=fmt(L.tokens,{input:tk(u.input),output:tk(u.output),cache_write:tk(u.cache_write),cache_read:tk(u.cache_read)});
  if(u.context)t+=" \u00b7 "+fmt(L.tokens_context,{n:tk(u.context)});if(u.final)t+=" "+L.tokens_final;
  row(d.tokens,t);setAttr(d.tokens.p,"title",fmt(L.ui_tokens_tip,{messages:u.messages}));}
 else{row(d.tokens,L.ui_tokens_none);setAttr(d.tokens.p,"title",null);}
 row(d.ledger,v.ledger&&v.ledger.length?v.ledger[v.ledger.length-1]:"");
 setText(d.folder.code,v.artifacts||"");
 if(!d.folder.btn){d.folder.btn=copyBtn(v.artifacts,d.folder.code);d.folder.v.append(d.folder.btn);}
 setData(d.folder.btn,"copy",v.artifacts);setAttr(d.folder.btn,"aria-label",L.ui_copy+": "+v.artifacts);
 const act=v.last_event_ts||0,rec=v.state_updated_ts||0,sig=act||rec;      // the record's own write is not agent activity
 if(sig){if(!d.sigRec)d.sigRec=bind(st,"details",d.signal.t,sig,null);
  d.sigRec.off=false;d.sigRec.ts=sig;
  d.sigRec.fn=ts=>{const x=nowS()-ts;
   let t=!act?fmt(L.ui_record_changed,{ago:ago(x)}):x>600?fmt(L.ui_no_signal,{dur:dur(x)}):fmt(L.ui_last_signal,{ago:ago(x)});
   if(act&&rec>act+5)t+=" \u00b7 "+fmt(L.ui_record_changed,{ago:ago(nowS()-rec)});return t;};paintTime(d.sigRec);show(d.signal.p,true);}
 else{if(d.sigRec)d.sigRec.off=true;show(d.signal.p,false);}
 st.dbuilt=true;}

function guarded(st,part,fn){
 if(!S.force&&selIn(st.parts[part])){st.deferred=st.deferred||Date.now();S.deferred=S.deferred||Date.now();return;}
 fn();}
function patchCard(st,v,a){
 st.v=v;st.a=a;
 guarded(st,"head",()=>patchHead(st,v,a));guarded(st,"steps",()=>patchSteps(st,v,a));
 guarded(st,"tasks",()=>patchTasks(st,v,a));guarded(st,"health",()=>patchHealth(st,v,a));guarded(st,"risk",()=>patchRisk(st,v,a));
 guarded(st,"docs",()=>patchDocs(st,v));guarded(st,"job",()=>patchJob(st,v,a));
 guarded(st,"act",()=>patchFeed(st,v,a));guarded(st,"det",()=>patchDetails(st,v,a));
 if(st.group&&st.group!==a.group&&!S.first)flash(st.el);
 st.group=a.group;setCls(st.el,"card g-"+a.group+(st.el.className.indexOf("flash")>=0?" flash":""));
 setData(st.el,"code",a.code||"");
 const o=openOf(st.key);
 if(!st.openSet){st.openSet=true;
  const d=o.act!==undefined?o.act:(!S.many&&(a.group==="you"||a.group==="stalled"||a.group==="running"));
  setOpen(st,st.act,d);if(o.det)setOpen(st,st.det,true);}
 if(S.stale)setStaleCard(st,true);}
function setOpen(st,det,val){det._want=val;if(det.open!==val)det.open=val;}
function flash(n){n.classList.add("flash");setTimeout(()=>n.classList.remove("flash"),2000);}
function setStaleCard(st,on){
 setAttr(st.el,"data-stale",on?"true":null);
 if(st.kind==="card"){show(st.r.asof,on);setText(st.r.asof,on&&S.lastGoodMs?"("+fmt(L.ui_as_of,{time:clock(S.lastGoodMs/1000)})+")":"");}}

// ---------------------------------------------------------------- the compact row (paused / finished) with an in-place full card
function newItem(key){
 const st={key:key,kind:"item",recs:[],card:null},box=el("div","item");box.dataset.key=key;st.el=box;
 const b=el("button","compact");b.type="button";b.dataset.act="expand";b.setAttribute("aria-expanded","false");
 const badge=el("span","badge"),gl=el("span","gl"),bw=el("span","bw"),nm=el("span","nm"),pn=el("b"),rid=el("span"),age=el("time"),sub=el("span","sub");
 const rkn=el("span","rkn");rkn.hidden=true;
 gl.setAttribute("aria-hidden","true");badge.append(gl,bw);nm.append(pn,rid,rkn);b.append(badge,nm,age,sub);box.append(b);
 st.c={btn:b,gl:gl,bw:bw,pn:pn,rid:rid,age:age,sub:sub,rkn:rkn};return st;}
function patchItem(st,v,a){
 st.v=v;st.a=a;const c=st.c,b=badgeParts(a);
 setCls(st.el,"item g-"+a.group);setText(c.gl,b.gl);setText(c.bw,b.bw);setText(c.pn,v.project_name);setText(c.rid," "+v.run_id);
 setText(c.sub,v.title);setAttr(c.btn,"title",fmt(L.ui_expand,{name:v.project_name+" "+v.run_id}));
 const rc=v.risk_counts||{},rn=(rc.high|0)+(rc.medium|0)+(rc.blocked|0);
 show(c.rkn,rn>0);setText(c.rkn,rn>0?fmt(L.ui_risk_n,{n:rn}):"");setAttr(c.rkn,"title",rn>0?riskSum(rc):null);
 const ts=a.group==="done"?(v.state_updated_ts||v.last_event_ts||0):(v.last_event_ts||v.state_updated_ts||0);
 if(!st.ageRec)st.ageRec=bind(st,"item",c.age,ts,null);
 st.ageRec.ts=ts;
 st.ageRec.fn=a.group==="done"?(v.status==="aborted"?(t=>fmt(L.ui_fin_aborted,{ago:ago(nowS()-t)}))
  :(t=>(v.timing&&v.timing.run?fmt(L.ui_fin_took,{dur:dur(v.timing.run)})+" \u00b7 ":"")+fmt(L.ui_fin_ago,{ago:ago(nowS()-t)})))
  :v.last_event_ts?(t=>fmt(L.ui_last_signal,{ago:ago(nowS()-t)})):(t=>fmt(L.ui_record_changed,{ago:ago(nowS()-t)}));
 paintTime(st.ageRec);
 if(st.card){const open=!st.card.el.hidden;if(open)patchCard(st.card,v,a);}
 if(S.stale)setStaleCard(st,true);}
function toggleItem(st){
 const o=openOf(st.key);o.item=!o.item;saveOpen();applyItemOpen(st);}
function applyItemOpen(st){
 const open=!!openOf(st.key).item;
 if(open&&!st.card){st.card=newCard(st.key,true);st.card.el.tabIndex=-1;st.el.append(st.card.el);S.cards.set(st.key+"#c",st.card);}
 if(st.card){show(st.card.el,open);if(open)patchCard(st.card,st.v,st.a);}
 show(st.c.btn,!open);                                 // the open card carries the same facts: the one-line row would be a duplicate
 setAttr(st.c.btn,"aria-expanded",open?"true":"false");}

// ---------------------------------------------------------------- sections and layout
function ensureSection(g){
 let s=S.sections[g];if(s)return s;
 s={g:g};s.el=el("section","sec");s.el.dataset.g=g;s.h2=el("h2");s.h2.id="sec-"+g;s.gl=el("span","gl");s.gl.setAttribute("aria-hidden","true");
 s.tx=el("span");s.n=el("small");s.h2.append(s.gl,s.tx,s.n);s.el.setAttribute("aria-labelledby",s.h2.id);
 s.list=el("div",(g==="paused"||g==="done")?"list":"grid");s.more=el("button","more");s.more.type="button";s.more.dataset.act="finmore";s.more.hidden=true;
 s.el.append(s.h2,s.list,s.more);S.sections[g]=s;return s;}
function matchesQ(it){
 if(!S.q)return true;const v=it.v,hay=lc([v.project_name,v.run_id,v.title,it.a.head,it.a.what].join("\n"));return hay.indexOf(lc(S.q))>=0;}
function matches(it){
 if(S.filter.size&&!S.filter.has(groupOf(it)))return false;
 return matchesQ(it);}
function wantKind(g,idx){return (g==="paused"||g==="done")?"item":(S.many&&idx>=FULL_CAP)?"item":"card";}
function stateFor(it,kind){
 let st=S.cards.get(it.key);
 // a card or row the viewer is working in keeps its form (and its place) until they let go; then it changes on the next update
 if(st&&st.kind!==kind&&!pinned(st.el)){if(st.el.parentNode)st.el.parentNode.removeChild(st.el);S.cards.delete(it.key);S.cards.delete(it.key+"#c");st=null;}
 if(!st){st=kind==="card"?newCard(it.key):newItem(it.key);S.cards.set(it.key,st);}
 return st;}
// the section a run is listed in: its own group, except that a run the viewer is working in (focus, selection, a press) stays
// in the section it is in, and keeps its form, until they let go (its badge and colour already show the new state). The chips
// count with the same rule, so a chip number is always the number of rows under that section.
function groupOf(it){
 let g=it.a.group;const st=S.cards.get(it.key);
 if(st&&st.sec&&st.sec!==g&&pinned(st.el))g=st.sec;
 return g;}
function layout(){
 const byG={};GROUPS.forEach(g=>byG[g]=[]);
 S.shown.forEach(it=>{byG[groupOf(it)].push(it);});
 const wantSecs=[];let full=0;const live=new Set();
 GROUPS.forEach(g=>{
  const list=byG[g];if(!list.length)return;
  const sec=ensureSection(g);setText(sec.gl,GLYPH[g]);setText(sec.tx,L["ui_g_"+g]);setText(sec.n,list.length);
  let shown=list;
  if(g==="done"&&!S.finAll&&list.length>FIN_SHOW){shown=list.slice(0,FIN_SHOW);}
  show(sec.more,g==="done"&&list.length>FIN_SHOW);
  if(g==="done"&&list.length>FIN_SHOW)setText(sec.more,S.finAll?L.ui_act_less:fmt(L.ui_fin_more,{n:list.length-FIN_SHOW}));
  const els=shown.map(it=>{
   const kind=wantKind(g,g==="paused"||g==="done"?0:full++);
   const st=stateFor(it,kind);live.add(it.key);st.sec=g;
   if(st.kind==="card")patchCard(st,it.v,it.a);else{patchItem(st,it.v,it.a);if(openOf(it.key).item)applyItemOpen(st);}
   return st.el;});
  place(sec.list,els);wantSecs.push(sec.el);});
 // states that are no longer on the page
 S.cards.forEach((st,k)=>{if(k.slice(-2)==="#c")return;if(!live.has(k)){if(st.el.parentNode)st.el.parentNode.removeChild(st.el);S.cards.delete(k);S.cards.delete(k+"#c");}});
 const main=$("main");setCls(main,S.many?"many":"");
 const extra=[];
 if(!S.items.length){extra.push(emptyEl());show(S.olderBtn,!S.older);}
 else if(!S.shown.length){extra.push(noMatchEl());}
 place(main,wantSecs.concat(extra));
 S.nav=[];GROUPS.forEach(g=>byG[g].forEach(it=>{const st=S.cards.get(it.key);if(st)S.nav.push({st:st,g:it.a.group});}));
 fitAll(false);}                                      // the cards are in the page now: cut text can be measured

function emptyEl(){
 if(S.emptyEl)return S.emptyEl;
 const d=el("div","empty"),h=el("h2","",L.ui_empty_h);d.append(h,el("p","",fmt(L.ui_empty_p,{workspace:META.workspace||""})));
 [[L.ui_empty_claude,L.ui_empty_claude_cmd],[L.ui_empty_term,L.ui_empty_term_cmd]].forEach(x=>{
  const w=el("div","cmdrow"),lbl=el("span","lbl",x[0]),c=el("span","cmd"),code=el("code","",x[1]);c.append(code,copyBtn(x[1],code));w.append(lbl,c);d.append(w);});
 const ob=el("button","tog",$("older-text").textContent);ob.type="button";ob.dataset.act="older";d.append(ob);S.emptyEl=d;S.olderBtn=ob;return d;}
function noMatchEl(){
 if(S.noMatch)return S.noMatch;
 const d=el("div","empty"),b=el("button","btn",L.ui_clear);b.type="button";b.dataset.act="clear";
 d.append(el("p","",L.ui_no_match),b);S.noMatch=d;return d;}

// ---------------------------------------------------------------- console: the terminal's run capabilities on the page
function cpath(p,q){return "/api/console/"+p+"?lang="+encodeURIComponent(LANG)+(q?"&"+q:"");}
function canAct(){return !!(META.controls&&META.token);}
function engineName(id){return ENGINE_NAMES[id]||String(id||"");}
function cerr(res){
 const e=(res&&res.error)||"net",d=(res&&res.detail)||"";
 if(e==="service_down"||e==="net")return L.ui_cerr_down;if(e==="service_refused")return fmt(L.ui_cerr_refused,{detail:d||"?"});
 if(e==="service_not_configured")return L.ui_svc_unset;if(e==="service_not_installed")return fmt(L.ui_svc_missing,{cmd:'orkestra run "..."'});
 if(e==="service_start_failed"||e==="service_setup_failed")return L.ui_svc_failed;
 if(e==="token")return L.ui_ctl_err_token;if(e==="read_only")return L.ui_ctl_err_read_only;if(e==="origin"||e==="host")return L.ui_ctl_err_origin;
 if(e==="job_closed")return L.ui_cerr_closed;if(e==="job_busy")return L.ui_cerr_busy;if(e==="no_job")return L.ui_cerr_nojob;
 if(e==="not_found")return L.ui_ctl_err_not_found;return fmt(L.ui_cerr_generic,{detail:d||e});}
function fieldErr(id,msg,input){const e=$(id);setText(e,msg||"");show(e,!!msg);if(input)setAttr(input,"aria-invalid",msg?"true":null);}
function dlgOpen(d,focus){try{if(typeof d.showModal==="function"){if(!d.open)d.showModal();}else setAttr(d,"open","");}catch(e){setAttr(d,"open","");}
 if(focus){try{focus.focus();}catch(e){}}}
function dlgShut(d){try{if(typeof d.close==="function"){if(d.open)d.close();}else setAttr(d,"open",null);}catch(e){setAttr(d,"open",null);}}
function dlgUp(d){return d.open===true||d.getAttribute("open")!==null;}
function refocus(n){try{if(n&&n.focus&&!n.hidden)n.focus();}catch(e){}}
// ---- the run service (the page asks this server; it talks to the service with a token the browser never sees)
function svcText(s){s=s||{};return !s.installed?fmt(L.ui_svc_missing,{cmd:'orkestra run "..."'}):!s.configured?L.ui_svc_unset:
 s.running?fmt(L.ui_svc_up,{port:s.port}):L.ui_svc_down;}
function svcRow(prefix,s){
 const row=$(prefix+"-svc"),btn=$(prefix+"-svc-btn");show(row,true);
 const known=!!(s&&("installed" in s));setText($(prefix+"-svc-msg"),known?svcText(s):cerr(s));
 setData(row,"ok",known&&s.running?"1":"0");show(btn,!!(known&&s.installed&&!s.running));setText(btn,L.ui_svc_start);btn.disabled=false;}
async function startService(prefix){
 const btn=$(prefix+"-svc-btn");btn.disabled=true;setText(btn,L.ui_svc_starting);
 const r=await req("/api/console/service",{body:{},timeout:40000});
 if(r.ok){announce(fmt(L.ui_svc_up,{port:r.port}));svcRow(prefix,r);if(prefix==="nr"){show($("nr-svc"),false);await nrProjects();}return;}
 btn.disabled=false;setText(btn,L.ui_svc_start);setText($(prefix+"-svc-msg"),cerr(r));announce(cerr(r));}
// ---- new run
function nrLabels(){
 setText($("nr-h"),L.ui_nr_h);setText($("nr-project-l"),L.ui_nr_project);setText($("nr-engine-l"),L.ui_nr_engine);
 setText($("nr-mode-l"),L.ui_nr_mode);setText($("nr-mode-auto-t"),L.ui_nr_mode_auto);setText($("nr-mode-man-t"),L.ui_nr_mode_man);
 setText($("nr-req-l"),L.ui_nr_req);setAttr($("nr-req"),"placeholder",L.ui_nr_req_ph);setText($("nr-ok-t"),L.ui_nr_ok);
 setText($("nr-cancel"),L.ui_nr_cancel);setText($("nr-go"),S.nr.busy?L.ui_nr_busy:L.ui_nr_go);nrCount();}
function nrCount(){const n=String($("nr-req").value||"").trim().length;setText($("nr-req-n"),fmt(L.ui_nr_req_n,{n:n,max:REQ_MAX}));
 if(n>REQ_MAX)fieldErr("nr-req-err",fmt(L.ui_nr_req_long,{max:REQ_MAX,n:n}),$("nr-req"));else if($("nr-req").getAttribute("aria-invalid")&&n>=3)fieldErr("nr-req-err","",$("nr-req"));}
async function openNew(){
 if(!canAct())return;S.nr.trigger=document.activeElement;
 ["nr-err","nr-project-err","nr-ok-err"].forEach(id=>fieldErr(id,""));fieldErr("nr-req-err","",$("nr-req"));nrLabels();
 const m=lsGet("orkestra.dash.nr.mode");$("nr-mode-man").checked=m==="manual";$("nr-mode-auto").checked=m!=="manual";
 dlgOpen($("nr"),$("nr-req"));
 show($("nr-svc"),true);setData($("nr-svc"),"ok","0");setText($("nr-svc-msg"),L.ui_nr_loading);show($("nr-svc-btn"),false);
 const s=await req(cpath("service"));
 if(s.ok&&s.running){show($("nr-svc"),false);await nrProjects();}else svcRow("nr",s);}
function closeNew(){if(S.nr.busy)return;dlgShut($("nr"));refocus(S.nr.trigger);S.nr.trigger=null;}
async function nrProjects(){
 const sel=$("nr-project"),r=await req(cpath("projects"));
 if(!r.ok){svcRow("nr",r);return;}
 const list=(r.projects||[]).map(p=>String(p&&p.name||"")).filter(Boolean),keep=sel.value||lsGet("orkestra.dash.nr.project")||"";
 const first=el("option","",L.ui_nr_project_none);first.value="";const opts=[first];
 list.forEach(n=>{const o=el("option","",n);o.value=n;opts.push(o);});sel.replaceChildren.apply(sel,opts);
 sel.value=list.indexOf(keep)>=0?keep:"";
 fieldErr("nr-project-err",list.length?"":fmt(L.ui_nr_noprojects,{workspace:META.workspace||""}));
 await nrEngines();}
async function nrEngines(){
 const sel=$("nr-engine"),p=$("nr-project").value,seq=++S.nr.seq;
 const r=await req(cpath("engines",p?"project="+encodeURIComponent(p):""));
 if(seq!==S.nr.seq)return;                                  // a newer project choice already asked
 if(!r.ok){setText($("nr-engine-hint"),cerr(r));return;}
 const list=S.nr.engines=Array.isArray(r.engines)?r.engines:[],keep=sel.value||lsGet("orkestra.dash.nr.engine")||r.default||"claude";
 sel.replaceChildren.apply(sel,list.map(e=>{const o=el("option","",e.available?(e.id===r.default?fmt(L.ui_nr_engine_default,{name:e.label}):e.label)
  :fmt(L.ui_nr_engine_off,{name:e.label}));o.value=e.id;o.disabled=!e.available;return o;}));
 const pick=list.find(e=>e.id===keep&&e.available)||list.find(e=>e.id===r.default&&e.available)||list.find(e=>e.available);
 if(pick)sel.value=pick.id;
 setText($("nr-engine-hint"),list.filter(e=>!e.available&&e.reason).map(e=>e.label+": "+e.reason).join(" \u00b7 "));}
async function submitNew(ev){
 if(ev&&ev.preventDefault)ev.preventDefault();if(S.nr.busy)return;
 const proj=$("nr-project").value,eng=$("nr-engine").value,text=String($("nr-req").value||"").trim(),manual=!!$("nr-mode-man").checked;
 const errs=[];
 fieldErr("nr-project-err",proj?"":L.ui_nr_project_err,$("nr-project"));if(!proj)errs.push([$("nr-project"),L.ui_nr_project_err]);
 const off=(S.nr.engines||[]).find(e=>e.id===eng&&!e.available);if(off)errs.push([$("nr-engine"),fmt(L.ui_nr_engine_err,{why:off.reason||""})]);
 const rm=text.length<3?L.ui_nr_req_short:text.length>REQ_MAX?fmt(L.ui_nr_req_long,{max:REQ_MAX,n:text.length}):"";
 fieldErr("nr-req-err",rm,$("nr-req"));if(rm)errs.push([$("nr-req"),rm]);
 const ok=!!$("nr-ok").checked;fieldErr("nr-ok-err",ok?"":L.ui_nr_ok_err,$("nr-ok"));if(!ok)errs.push([$("nr-ok"),L.ui_nr_ok_err]);
 if(off)fieldErr("nr-err",errs.find(x=>x[0]===$("nr-engine"))[1]);else fieldErr("nr-err","");
 if(errs.length){refocus(errs[0][0]);announce(errs[0][1]);return;}
 S.nr.busy=true;const go=$("nr-go");go.disabled=true;setText(go,L.ui_nr_busy);
 const r=await req("/api/console/start",{body:{project:proj,request:text,engine:eng,mode:manual?"manual":"autopilot"},timeout:45000});
 S.nr.busy=false;go.disabled=false;setText(go,L.ui_nr_go);
 if(r.ok){
  lsSet("orkestra.dash.nr.project",proj);lsSet("orkestra.dash.nr.engine",eng);lsSet("orkestra.dash.nr.mode",manual?"manual":"autopilot");
  $("nr-req").value="";$("nr-ok").checked=false;nrCount();closeNew();
  const msg=fmt(L.ui_nr_done,{project:proj,engine:engineName((r.job&&r.job.engine)||eng)});toast(msg);announce(msg);retryNow();return;}
 if(r.error==="service_down"||r.error==="service_not_configured")svcRow("nr",r);
 fieldErr("nr-err",cerr(r));announce(cerr(r));}
// ---- run documents
function patchDocs(st,v){
 const list=v.docs||[];show(st.parts.docs,list.length>0);if(!list.length){st.dsig="";return;}
 const sig=list.join(",")+"|"+v.project_name+v.run_id;if(st.dsig===sig)return;st.dsig=sig;
 st.r.docsBox.replaceChildren.apply(st.r.docsBox,list.map(n=>{const b=el("button","tog",L["ui_doc_"+n]||n);b.type="button";b.dataset.act="doc";
  b.dataset.doc=n;setAttr(b,"aria-label",fmt(L.ui_doc_open,{doc:L["ui_doc_"+n]||n,name:v.project_name+" "+v.run_id}));return b;}));}
async function openDoc(st,name){
 const v=st.v;if(!v)return;S.rd={key:st.key,v:v,trigger:document.activeElement};setText($("rd-close"),L.ui_doc_close);
 dlgOpen($("rd"),$("rd-close"));await showDoc(name);}
async function showDoc(name){
 const c=S.rd;if(!c)return;const label=L["ui_doc_"+name]||name;
 setText($("rd-h"),label+" \u00b7 "+c.v.project_name+" "+c.v.run_id);
 const tabs=$("rd-tabs");tabs.replaceChildren.apply(tabs,(c.v.docs||[]).map(n=>{const b=el("button","tab",L["ui_doc_"+n]||n);b.type="button";b.dataset.doc=n;
  setAttr(b,"aria-pressed",n===name?"true":"false");return b;}));
 setText($("rd-text"),L.ui_doc_loading);show($("rd-note"),false);
 const seq=++S.rdSeq,r=await req(cpath("doc","key="+encodeURIComponent(c.key)+"&name="+encodeURIComponent(name)));
 if(seq!==S.rdSeq||S.rd!==c)return;
 if(!r.ok){setText($("rd-text"),cerr(r));return;}
 setText($("rd-text"),r.text||"");if(r.truncated){setText($("rd-note"),fmt(L.ui_doc_cut,{path:r.path||""}));show($("rd-note"),true);}}
function closeDoc(){const c=S.rd;S.rd=null;dlgShut($("rd"));if(c)refocus(c.trigger);}
// ---- the conversation of a background run: what the agent said last, and the person's answer
function jbtn(st,act,label,kind){let b=st.r.jb[act];if(!b){b=el("button","btn cbtn"+(kind?" "+kind:""));b.type="button";b.dataset.act=act;st.r.jb[act]=b;}
 setText(b,label);return b;}
// the run service writes 2026-10-30T13:01:02+0300: Safari parses ISO offsets only with a colon (+03:00)
function shortStamp(s){const t=Date.parse(String(s||"").replace(" ","T").replace(/([+-]\d\d)(\d\d)$/,"$1:$2"));return isNaN(t)?String(s||""):clock(t/1000);}
function patchJob(st,v,a){
 const r=st.r,j=v.job,box=st.parts.job;show(box,!!j);if(!j)return;
 const busy=!!S.jbusy[st.key],act=canAct(),reply=act&&!!j.can_reply;
 setCls(r.jhS,"rk jst-"+j.status);setText(r.jhS,L["ui_job_status_"+j.status]||j.status);setText(r.jhV,fmt(L.ui_job_via,{engine:engineName(j.engine)}));
 const msg=String(j.message||""),open=!!openOf(st.key).jmsg;
 setText(r.jmsg,msg||L.ui_job_none);setCls(r.jmsg,"jmsg"+(open?" open":"")+(msg?"":" empty"));
 show(r.jmore,msg.length>JMSG_LONG);setText(r.jmore,open?L.ui_act_less:L.ui_show_more);setAttr(r.jmore,"aria-expanded",open?"true":"false");
 const want=[];
 if(reply&&v.phase==="approve")want.push(jbtn(st,"japprove",L.ui_job_approve,"primary"));
 if(v.phase==="approve"&&(v.docs||[]).indexOf("plan")>=0)want.push(jbtn(st,"jreview",L.ui_job_review,""));
 if(reply&&v.status==="paused")want.push(jbtn(st,"jcontinue",L.ui_job_continue,"warn"));
 place(r.jbtns,want);show(r.jbtns,want.length>0);want.forEach(b=>{if(b.disabled!==busy)b.disabled=busy;});
 show(r.jform,reply);setAttr(r.jin,"placeholder",L.ui_job_reply_ph);if(r.jsend.disabled!==busy)r.jsend.disabled=busy;
 setText(r.jsend,busy?L.ui_job_sending:L.ui_job_send);
 const working=act&&(j.status==="running"||j.status==="queued");setText(r.jnote,working?L.ui_job_working:"");
 const turns=j.turns||[];show(r.jturns,turns.length>0);setText(r.jturnsS,fmt(L.ui_job_turns,{n:turns.length}));
 const tsig=JSON.stringify(turns.map(t=>[t.at,t.prompt]));
 if(st.jtsig!==tsig){st.jtsig=tsig;r.jturnsL.replaceChildren.apply(r.jturnsL,turns.slice().reverse().map(t=>{
  const li=el("li"),p=el("span","",t.prompt);p.dir="auto";li.append(el("time","",shortStamp(t.at)),p);return li;}));}}
function jErr(st,msg){const r=st.r;setText(r.jerr,msg||"");show(r.jerr,!!msg);setAttr(r.jin,"aria-invalid",msg?"true":null);if(msg)announce(msg);}
async function sendReply(st,preset){
 const v=st.v,j=v&&v.job;if(!j||S.jbusy[st.key]||!canAct())return;const r=st.r;
 const text=preset!==undefined?preset:String(r.jin.value||"").trim();
 if(!text){jErr(st,L.ui_job_reply_empty);refocus(r.jin);return;}
 if(text.length>REQ_MAX){jErr(st,fmt(L.ui_job_reply_long,{max:REQ_MAX,n:text.length}));refocus(r.jin);return;}
 jErr(st,"");S.jbusy[st.key]=true;patchJob(st,v,st.a);
 const res=await req("/api/console/reply",{body:{job:j.id,text:text},timeout:30000});
 S.jbusy[st.key]=false;
 if(res.ok){if(preset===undefined)r.jin.value="";const msg=fmt(L.ui_job_sent,{project:v.project_name});toast(msg);announce(msg);retryNow();}
 else jErr(st,cerr(res));
 patchJob(st,st.v,st.a);}
async function continueRun(st){
 const v=st.v;if(!v||S.jbusy[st.key]||!canAct())return;jErr(st,"");S.jbusy[st.key]=true;patchJob(st,v,st.a);
 const res=await req("/api/console/continue",{body:{key:st.key},timeout:30000});S.jbusy[st.key]=false;
 if(res.ok){const msg=fmt(L.ui_job_continued,{project:v.project_name});toast(msg);announce(msg);retryNow();}else jErr(st,cerr(res));
 patchJob(st,st.v,st.a);}
// ---- tools: the run service, background jobs, closing idle runs, the health check
function tlLabels(){
 setText($("tl-h"),L.ui_tools);setText($("tl-close"),L.ui_doc_close);setText($("tl-svc-h"),L.ui_tl_svc_h);setText($("tl-jobs-h"),L.ui_tl_jobs_h);
 setText($("tl-clear-h"),L.ui_tl_clear_h);setText($("tl-clear-p"),L.ui_tl_clear_p);setText($("tl-hours-l"),L.ui_tl_hours);
 setText($("tl-preview"),L.ui_tl_preview);setText($("tl-doc-h"),L.ui_tl_doc_h);setText($("tl-doc-p"),L.ui_tl_doc_p);setText($("tl-doc-run"),L.ui_tl_doc_run);}
async function openTools(){
 if(!canAct())return;S.tlTrigger=document.activeElement;tlLabels();dlgOpen($("tl"),$("tl-close"));
 setText($("tl-svc-msg"),L.ui_doc_loading);show($("tl-svc-btn"),false);
 const s=await req(cpath("service"));svcRow("tl",s);await loadJobs();}
function closeTools(){dlgShut($("tl"));refocus(S.tlTrigger);S.tlTrigger=null;}
async function loadJobs(){
 const ul=$("tl-jobs"),r=await req(cpath("jobs")),list=r.ok&&Array.isArray(r.jobs)?r.jobs:[],kids=[];
 if(!r.ok)kids.push(el("li","ri",cerr(r)));else if(!list.length)kids.push(el("li","ri",L.ui_tl_jobs_none));
 list.forEach(j=>{
  const li=el("li","ri"),body=el("div","rb"),foot=el("div","rf"),tim=el("time","",shortStamp(j.updated||j.created));
  body.append(el("span","rl",[j.project,j.run_id,engineName(j.engine)].filter(Boolean).join(" \u00b7 ")));
  if(j.message){const m=el("span","rr2",j.message);m.dir="auto";body.append(m);}
  const it=S.items.find(x=>x.v.run_id===j.run_id&&x.v.project_name===j.project);
  if(it){const go=el("button","tog go",L.ui_risk_go);go.type="button";go.dataset.act="go";go.dataset.key=it.key;foot.append(go);}
  if(j.can_cancel&&!it){const c=el("button","tog",L.ui_tl_job_cancel);c.type="button";c.dataset.act="jcancel";c.dataset.job=j.id;c.dataset.project=j.project;foot.append(c);}
  if(foot.childNodes.length)body.append(foot);
  li.append(el("span","rk jst-"+j.status,L["ui_job_status_"+j.status]||j.status),body,tim);kids.push(li);});
 ul.replaceChildren.apply(ul,kids);}
async function onToolsJobs(e){
 const b=actOf(e.target);if(!b)return;
 if(b.dataset.act==="go"){closeTools();goTo(b.dataset.key);return;}
 if(b.dataset.act!=="jcancel")return;
 const id=b.dataset.job;if(!S.tlArm[id]){S.tlArm[id]=true;setText(b,L.ui_tl_job_confirm);return;}   // two presses: a cancel aborts the run
 delete S.tlArm[id];b.disabled=true;const r=await req("/api/console/cancel",{body:{job:id},timeout:30000});
 if(r.ok){const msg=fmt(L.ui_tl_job_cancelled,{project:b.dataset.project||""});toast(msg);announce(msg);retryNow();await loadJobs();}
 else{b.disabled=false;setText(b,L.ui_tl_job_cancel);announce(cerr(r));toast(cerr(r));}}
async function clearRuns(apply){
 const h=parseFloat(String($("tl-hours").value||"").replace(",","."));
 if(!(h>=1&&h<=8760)){fieldErr("tl-hours-err",L.ui_tl_hours_err,$("tl-hours"));refocus($("tl-hours"));announce(L.ui_tl_hours_err);return;}
 fieldErr("tl-hours-err","",$("tl-hours"));const out=$("tl-clear-out"),btn=apply&&S.tlApply?S.tlApply:$("tl-preview");btn.disabled=true;
 const r=await req("/api/console/clear",{body:{hours:h,apply:!!apply},timeout:90000});btn.disabled=false;
 if(!r.ok){out.replaceChildren(el("p","dlg-err",cerr(r)));announce(cerr(r));return;}
 const n=(r.closed||[]).length,d=r.dropped|0;
 if(apply){const msg=fmt(L.ui_tl_applied,{n:n,d:d});out.replaceChildren(el("p","dlg-p",msg));toast(msg);announce(msg);retryNow();return;}
 if(!n&&!d){out.replaceChildren(el("p","dlg-p",L.ui_tl_none));announce(L.ui_tl_none);return;}
 const ul=el("ul","rlist");(r.closed||[]).forEach(x=>{const li=el("li","ri0",[x.project,x.run_id,fmt(L.ui_tl_idle,{dur:dur(x.idle)}),x.title].filter(Boolean).join(" \u00b7 "));
  li.dir="auto";ul.append(li);});
 const b=el("button","btn cbtn danger",fmt(L.ui_tl_apply,{n:n}));b.type="button";b.dataset.act="clearapply";S.tlApply=b;
 out.replaceChildren(el("p","dlg-p",fmt(L.ui_tl_would,{n:n,d:d})),ul,b);}
async function runDoctor(){
 const b=$("tl-doc-run"),sum=$("tl-doc-sum"),out=$("tl-doc-out");b.disabled=true;setText(b,L.ui_tl_doc_busy);
 const r=await req("/api/console/doctor",{body:{},timeout:150000});b.disabled=false;setText(b,L.ui_tl_doc_run);
 if(!r.ok){setText(sum,cerr(r));show(sum,true);show(out,false);announce(cerr(r));return;}
 setText(sum,r.passed?L.ui_tl_doc_ok:L.ui_tl_doc_bad);show(sum,true);
 out.replaceChildren.apply(out,String(r.text||"").split("\n").map(l=>el("span",/^\[!!\]/.test(l)?"bad":/^\[OK\]/.test(l)?"good":"",l+"\n")));
 show(out,true);announce(sum.textContent);}

// ---------------------------------------------------------------- risk radar: the newest risky actions across the listed runs
function radarItems(hit){const out=[];hit.forEach(it=>{const live=OPEN_ST.indexOf(it.v.status)>=0?0:1;
  (it.v.risks||[]).forEach(e=>out.push({e:e,it:it,live:live}));});
 out.sort((x,y)=>(x.live-y.live)||((y.e.ts||0)-(x.e.ts||0)));return out;}     // open runs first: those can still be stopped
function renderRadar(){
 const box=$("radar"),list=$("radar-list"),tog=$("radar-tog"),hit=S.items.filter(matchesQ),all=radarItems(hit),c={high:0,medium:0,blocked:0};
 hit.forEach(it=>{const k=it.v.risk_counts||{};c.high+=k.high|0;c.medium+=k.medium|0;c.blocked+=k.blocked|0;});
 show(box,all.length>0);
 if(!all.length){S.radar.recs.forEach(x=>{x.off=true;});S.radar.recs=[];S.radar.sig="";return;}
 setData(box,"top",riskTop(c));setText($("radar-h"),L.ui_risk_h);setText($("radar-sum"),riskSum(c));setText($("radar-p"),L.ui_risk_p);
 const open=S.radarAll,shown=all.slice(0,open?RADAR_CAP:RADAR_SHOW);
 show(tog,all.length>RADAR_SHOW);setText(tog,open?L.ui_risk_less:fmt(L.ui_risk_all,{n:Math.min(all.length,RADAR_CAP)}));
 setAttr(tog,"aria-expanded",open?"true":"false");
 const sig=JSON.stringify([shown.map(x=>[x.e.id,x.e.count,x.e.ts,x.it.key]),open]);
 if(S.radar.sig===sig||(!S.force&&pinned(list)))return;
 S.radar.sig=sig;S.radar.recs.forEach(x=>{x.off=true;});S.radar.recs=[];
 const kids=[];
 shown.forEach(x=>{const v=x.it.v,name=v.project_name+" "+v.run_id,row=riskRow(x.e,{project:v.project,home:v.home||META.home||""},v.project_name+" \u00b7 "+v.run_id);
  const go=el("button","tog go",L.ui_risk_go);go.type="button";go.dataset.act="go";go.dataset.key=x.it.key;
  setAttr(go,"aria-label",fmt(L.ui_risk_go_aria,{name:name}));row.foot.append(go);kids.push(row.li);
  const rec={n:row.tim,ts:x.e.ts,fn:rowTime,off:false};S.radar.recs.push(rec);paintTime(rec);});
 list.replaceChildren.apply(list,kids);}
function goTo(key){
 let st=S.cards.get(key);
 if(!st||!st.el.parentNode){S.filter.clear();S.q="";$("q").value="";S.finAll=true;lsSet("orkestra.dash.filter","[]");lsSet("orkestra.dash.q","");render();st=S.cards.get(key);}
 if(!st)return;
 if(st.kind==="item"&&!openOf(key).item)toggleItem(st);
 const t=st.kind==="card"?st.el:(st.card&&!st.card.el.hidden?st.card.el:st.c.btn);
 try{t.focus({preventScroll:true});}catch(e){}reveal(t);flash(t);}
function onRadarClick(e){const b=actOf(e.target);if(b&&b.dataset.act==="go")goTo(b.dataset.key);}

// ---------------------------------------------------------------- chrome: filters, summary, banners, connection
function buildChips(){
 const nav=$("sum");S.chips={};nav.setAttribute("aria-label",L.ui_sum);
 ["all"].concat(GROUPS).forEach(g=>{
  const b=el("button","fchip"+(g==="all"?"":" "+g)),gl=el("span","gl"),tx=el("span"),n=el("b");
  b.type="button";b.dataset.g=g;gl.setAttribute("aria-hidden","true");b.append(gl,tx,n);
  setText(gl,g==="all"?"":GLYPH[g]);setText(tx,g==="all"?L.ui_f_all:L["ui_g_"+g]);
  b.addEventListener("click",()=>toggleFilter(g));nav.append(b);S.chips[g]={b:b,n:n};});}
function toggleFilter(g){
 if(g==="all")S.filter.clear();else if(S.filter.has(g))S.filter.delete(g);else S.filter.add(g);
 lsSet("orkestra.dash.filter",JSON.stringify(Array.from(S.filter)));render();}
function updateChips(){
 // what a chip counts is what its section lists when the filter is off: the runs that match the search, by section
 const hit=S.items.filter(matchesQ),cnt={all:hit.length};GROUPS.forEach(g=>cnt[g]=0);hit.forEach(it=>cnt[groupOf(it)]++);
 Object.keys(S.chips).forEach(g=>{const c=S.chips[g];setText(c.n,cnt[g]);setAttr(c.b,"data-n",cnt[g]);
  setAttr(c.b,"aria-pressed",(g==="all"?S.filter.size===0:S.filter.has(g))?"true":"false");});
 const tot={you:0,stalled:0,running:0,paused:0};S.items.forEach(it=>{if(it.a.group in tot)tot[it.a.group]++;});   // the title and the calm line never depend on the search
 const need=tot.you+tot.stalled,open=need+tot.running+tot.paused;
 setText($("calm"),need>0||!S.items.length?"":(open?L.ui_calm:L.ui_empty));
 if(S.lastN!==need){S.lastN=need;document.title=(need?"("+need+") ":"")+BASE_TITLE;}
 return need;}
function banners(){
 const st=S.staleSrv,off=connState()==="offline";
 show($("stale"),st);show($("offline"),off&&!st);show($("rerr"),S.renderErr&&!st&&!off);}
function offlineText(age){
 const http=S.errKind==="http",v={status:S.errStatus};
 if(S.lastGoodMs){v.time=clock(S.lastGoodMs/1000);v.ago=ago(age);return fmt(http?L.ui_conn_error:L.ui_conn_offline,v);}
 return http?fmt(L.ui_error_http,v):L.ui_offline;}
// the chip and the banner show a countdown and an age that tick every second: they are plain text, not live regions (a screen
// reader would read them out once a second). What is spoken is the TRANSITION, once.
function announceConn(cs){
 if(cs===S.connPrev)return;const prev=S.connPrev;S.connPrev=cs;
 if(cs==="live")announce(L.ui_conn_back);
 else if(prev==="live"&&cs==="retry")announce(L.ui_announce_lost);
 else if(cs==="offline")announce(S.lastGoodMs?L.ui_announce_offline:L.ui_load_failed_h);}
function retryNow(){clearTimeout(S.timer);if(!S.inflight)tick();}
function connState(){
 if(!S.started||!S.fails)return "live";
 if(!S.lastGoodMs||S.fails>=3||Date.now()-S.lastGoodMs>10000)return "offline";return "retry";}
function renderConn(){
 if(!S.started)return;
 const cs=connState(),chip=$("conn"),txt=$("conn-text");setAttr(chip,"data-state",cs);
 const age=(Date.now()-S.lastGoodMs)/1000;
 if(cs==="live")setText(txt,L.ui_conn_live+" \u00b7 "+fmt(L.ui_conn_updated,{ago:ago(age)}));
 else if(cs==="retry")setText(txt,fmt(L.ui_conn_retry,{sec:Math.max(1,Math.ceil((S.nextAt-Date.now())/1000))}));
 else setText(txt,L.ui_conn_offline_chip);
 if(cs==="offline")setText($("offline-msg"),offlineText(age));
 announceConn(cs);
 const dim=cs==="offline"&&S.lastGoodMs>0;
 if(dim!==S.stale){S.stale=dim;S.cards.forEach(st=>setStaleCard(st,dim));}
 banners();
 setAttr(chip,"title",footText());}
function footText(){return fmt(L.ui_build,{version:META.version||"?",build:META.build||BUILD,ago:ago(nowS()-(META.started||nowS()))});}
function renderFoot(){setText($("foot-build"),footText());setText($("foot-ro"),META.controls&&META.token?L.ui_ctl_foot:L.ui_readonly);}

// ---------------------------------------------------------------- applying one answer of the server
function trackTransitions(){
 const cur=new Map(S.items.map(it=>[it.key,it.a.group]));
 const need=S.items.filter(it=>it.a.group==="you"||it.a.group==="stalled").length;
 if(!S.first){
  S.items.forEach(it=>{const g=it.a.group;
   if((g==="you"||g==="stalled")&&S.prev.get(it.key)!==g)queueAnn(fmt(g==="you"?L.ui_announce_you:it.a.code==="hung"?L.ui_announce_hung:L.ui_announce_stalled,{name:it.v.project_name}));});
  if(S.prevNeed>0&&need===0)queueAnn(L.ui_announce_calm);}
 S.prev=cur;S.prevNeed=need;}
function render(){
 S.shown=sortItems(S.items.filter(matches));
 updateChips();layout();renderRadar();}
function apply(data){
 if(S.loadErr&&S.loadErr.d.parentNode)S.loadErr.d.parentNode.removeChild(S.loadErr.d);
 S.data=data;S.items=data.map(v=>({v:v,key:keyOf(v),a:attnOf(v)}));S.many=S.items.length>MANY;
 trackTransitions();render();S.first=false;flushAnn();}

// ---------------------------------------------------------------- polling
function url(){return "/api/runs?lang="+encodeURIComponent(LANG)+(S.older?"&all=1":"");}
function schedule(){clearTimeout(S.timer);const d=nextDelay(S.fails,!!document.hidden);S.nextAt=Date.now()+d;S.timer=setTimeout(tick,d);}
function onFail(kind,status){
 S.inflight=false;S.started=true;S.fails++;S.errKind=kind;S.errStatus=status||0;
 schedule();banners();renderConn();if(!S.data)showLoadError();}
function onOk(data){
 S.inflight=false;S.started=true;S.fails=0;S.lastGoodMs=Date.now();
 if(S.again){S.again=false;setTimeout(tick,0);}
 try{apply(data);S.renderErr=false;}catch(e){S.renderErr=true;try{console.error(e);}catch(x){}}
 schedule();banners();renderConn();measureBar();}
// while the older runs load, the page shows placeholders: "No runs yet" would be false for those seconds
function showSkeleton(){const w=el("div",""),d=el("div","skel");w.setAttribute("aria-hidden","true");
 for(let i=0;i<3;i++)d.append(el("div","card"));w.append(d);place($("main"),[w]);}
// the first answer never came: the skeleton cards must not sit there forever
function showLoadError(){
 const sk=$("skel");if(sk&&sk.parentNode)sk.parentNode.removeChild(sk);
 if(!S.loadErr){const d=el("div","empty"),b=el("button","btn",L.ui_retry_now),p=el("p");b.type="button";b.dataset.act="retry";
  d.setAttribute("role","alert");d.append(el("h2","",L.ui_load_failed_h),p,b);S.loadErr={d:d,p:p};}
 setText(S.loadErr.p,S.errKind==="http"?fmt(L.ui_error_http,{status:S.errStatus}):L.ui_load_failed_net);
 place($("main"),[S.loadErr.d]);}
async function tick(){
 if(S.inflight||S.reloading)return;S.inflight=true;clearTimeout(S.timer);
 // the first answer with every finished run is read cold on the server (seconds on a busy machine): give it room
 const ctl=typeof AbortController==="function"?new AbortController():null,to=ctl?setTimeout(()=>ctl.abort(),S.older?20000:5000):0;
 let data;
 try{
  const r=await fetch(url(),{cache:"no-store",signal:ctl?ctl.signal:undefined});
  const hd=n=>r.headers&&r.headers.get?r.headers.get(n):null;
  if(r.ok===false){const he=new Error("http "+r.status);he.http=r.status;throw he;}
  const b=hd("X-Orkestra-Build");
  if(BUILD&&b&&b!==BUILD){clearTimeout(to);S.inflight=false;S.reloading=true;location.reload();return;}
  S.staleSrv=hd("X-Orkestra-Stale")==="1";
  const t=parseFloat(hd("X-Orkestra-Time"));skewHeader=t>0;if(t>0)skew=t-Date.now()/1000;
  data=await r.json();
  if(!Array.isArray(data))throw new Error("not a list");
  if(!skewHeader){const sn=data.reduce((m,v)=>v&&v.now>m?v.now:m,0);if(sn>0)skew=sn-Date.now()/1000;}   // no header: the server time inside the answer
 }catch(e){clearTimeout(to);onFail(e&&e.http?"http":"net",e&&e.http);return;}
 clearTimeout(to);onOk(data);}

// ---------------------------------------------------------------- events
function actOf(n){while(n&&n.dataset){if(n.dataset.act)return n;n=n.parentNode;}return null;}
function cardOf(n){while(n){if(n.dataset&&n.dataset.key){const k=n.dataset.key,st=S.cards.get(k);if(st&&st.el===n)return st;
  const c=S.cards.get(k+"#c");if(c&&c.el===n)return c;}n=n.parentNode;}return null;}
function onMainClick(e){
 const b=actOf(e.target);if(!b)return;const act=b.dataset.act,st=cardOf(b);
 if(act==="copy"){doCopy(b.dataset.copy,b._for||b.previousSibling,b);return;}
 if(act==="retry"){retryNow();return;}
 if(act==="expand"){const it=cardOf(b);if(it)toggleItem(it);return;}
 if(act==="collapse"){const it=S.cards.get(b.dataset.key||(st&&st.key));const row=it&&it.kind==="item"?it:S.cards.get(st&&st.key);
  if(row&&row.kind==="item"){toggleItem(row);try{row.c.btn.focus();}catch(e){}}return;}
 if(act==="finmore"){S.finAll=!S.finAll;render();return;}
 if(act==="older"){const o=$("older");o.checked=true;lsSet("orkestra.dash.older","1");S.older=true;tick();return;}
 if(act==="clear"){S.filter.clear();S.q="";$("q").value="";lsSet("orkestra.dash.filter","[]");lsSet("orkestra.dash.q","");render();return;}
 if(!st)return;
 if(act==="ctl"){openCtl(st,b.dataset.action,b);return;}
 if(act==="doc"){openDoc(st,b.dataset.doc);return;}
 if(act==="jmore"){const o=openOf(st.key);o.jmsg=!o.jmsg;saveOpen();patchJob(st,st.v,st.a);return;}
 if(act==="jsend"){sendReply(st);return;}
 if(act==="japprove"){sendReply(st,L.ui_job_approve_word);return;}
 if(act==="jreview"){openDoc(st,"plan");return;}
 if(act==="jcontinue"){continueRun(st);return;}
 if(act==="risks"){const o=openOf(st.key);o.risk=!o.risk;saveOpen();patchRisk(st,st.v,st.a);return;}
 if(act==="tasks"){const o=openOf(st.key);o.tasks=!o.tasks;saveOpen();patchTasks(st,st.v,st.a);return;}
 if(act==="notices"){const o=openOf(st.key);o.notices=!o.notices;saveOpen();patchHealth(st,st.v,st.a);return;}
 if(act==="noticetxt"){toggleNote(b);return;}
 if(act==="feedmore"){const o=openOf(st.key);o.more=!o.more;saveOpen();st.feed.born=false;patchFeed(st,st.v,st.a);return;}
 if(act==="rowfull"){let n=b;while(n&&!(n.dataset&&n.dataset.id))n=n.parentNode;const rec=n&&st.feed.rows.get(n.dataset.id);if(rec)toggleFull(st,rec);}}
// ---- the sticky header: its real size, so a focused card is never left under it
function stickyInfo(){
 let h=0,bottom=0;
 ["bar","r1"].forEach(id=>{const n=$(id);
  try{if(n&&getComputedStyle(n).position==="sticky"){const r=n.getBoundingClientRect();h=Math.max(h,r.height);bottom=Math.max(bottom,r.bottom);}}catch(e){}});
 return {h:h,bottom:bottom};}
function measureBar(){
 try{const h=Math.ceil(stickyInfo().h);if(h!==S.barH){S.barH=h;if(h>0)document.documentElement.style.setProperty("--bar-h",h+"px");}}catch(e){}}
function reveal(t){
 try{const hb=stickyInfo().bottom,r=t.getBoundingClientRect(),vh=window.innerHeight||0;
  if(r.top<hb+8||(vh>0&&r.top>vh-48)){if(typeof window.scrollBy==="function")window.scrollBy(0,Math.round(r.top-hb-8));}}catch(e){}}
function navTo(i){
 if(!S.nav.length)return;i=(i+S.nav.length)%S.nav.length;const st=S.nav[i].st;
 const t=st.kind==="card"?st.el:(st.card&&!st.card.el.hidden?st.card.el:st.c.btn);t.focus({preventScroll:true});reveal(t);return S.nav[i];}
function navIndex(){const a=document.activeElement;
 for(let i=0;i<S.nav.length;i++){const st=S.nav[i].st;if(st.el===a||st.el.contains(a))return i;}return -1;}
function setHelp(on){
 S.help=on;setText($("help"),L.ui_keys);show($("help"),on);setAttr($("keys-btn"),"aria-expanded",on?"true":"false");
 if(on)announce(L.ui_keys);}
function onKey(e){
 if(S.ctl){if(e.key==="Escape"&&!S.ctlBusy[S.ctl.key])closeCtl();return;}     // the dialog owns the keyboard while it is open
 if(dlgUp($("rd"))){if(e.key==="Escape")closeDoc();return;}
 if(dlgUp($("nr"))){if(e.key==="Escape")closeNew();return;}
 if(dlgUp($("tl"))){if(e.key==="Escape")closeTools();return;}
 if(e.ctrlKey||e.metaKey||e.altKey)return;
 const tag=e.target&&e.target.tagName?String(e.target.tagName).toLowerCase():"";
 if(e.key==="Escape"){const q=$("q");if(S.q||q.value){S.q="";q.value="";lsSet("orkestra.dash.q","");render();}
  if(S.help)setHelp(false);return;}
 if(!S.keysOn)return;                      // single-character shortcuts can be switched off (WCAG 2.1.4); Escape is not one
 if(tag==="input"||tag==="textarea"||tag==="select")return;
 const k=e.key;
 if(k==="/"){e.preventDefault();$("q").focus();}
 else if(k==="?"){setHelp(!S.help);}
 else if(k==="j"||k==="k"){const i=navIndex();navTo(k==="j"?i+1:(i<0?0:i-1));}
 else if(k==="n"){
  const need=[];S.nav.forEach((x,i)=>{if(x.g==="you"||x.g==="stalled")need.push(i);});
  if(!need.length)return;const cur=navIndex(),nxt=need.find(i=>i>cur);const x=navTo(nxt===undefined?need[0]:nxt);
  if(x)announce(x.st.v.project_name+" \u00b7 "+(x.st.a.head||""));}
 else if((k==="Enter"||k===" ")&&e.target){const st=cardOf(e.target);
  if(st&&st.kind==="card"&&e.target===st.el){e.preventDefault();st.act.open=!st.act.open;}}}
function flushDeferred(){
 if(!S.deferred)return;let sel=false;try{const s=getSelection();sel=!!(s&&!s.isCollapsed);}catch(x){}
 if(sel&&Date.now()-S.deferred<10000)return;
 S.force=sel;S.deferred=0;
 try{S.cards.forEach(st=>{if(st.deferred&&st.v){st.deferred=0;if(st.kind==="card")patchCard(st,st.v,st.a);}});}finally{S.force=false;}}
function onPrint(on){
 S.printing=on;
 if(on){const pl=$("printline");setText(pl,fmt(L.ui_print,{time:stamp(nowS()).slice(0,16),build:META.build||BUILD}));}
 S.cards.forEach(st=>{if(st.kind!=="card")return;
  if(st.r.jturns)st.r.jturns.open=on;                       // the conversation history prints too
  if(on){setOpen(st,st.act,true);setOpen(st,st.det,true);if(st.v){patchFeed(st,st.v,st.a);patchDetails(st,st.v,st.a);}}
  else{const o=openOf(st.key);setOpen(st,st.act,o.act!==undefined?o.act:!S.many&&!!st.a&&st.a.group!=="paused"&&st.a.group!=="done");
   setOpen(st,st.det,!!o.det);}});}
function secondTick(){
 if(document.hidden)return;
 tickTimes();renderConn();renderFoot();flushAnn();flushDeferred();}

// ---------------------------------------------------------------- boot
function langBoot(){
 let q="";try{q=new URLSearchParams(location.search).get("lang")||"";}catch(e){}
 const stored=lsGet("orkestra.dash.lang");
 if(q){if(q===LANG&&q!==stored)lsSet("orkestra.dash.lang",q);return false;}
 if(stored&&stored!==LANG&&/^[a-z]{2}$/.test(stored)){
  let done=false;try{done=sessionStorage.getItem("orkestra.dash.langjump")==="1";}catch(e){}
  if(!done){try{sessionStorage.setItem("orkestra.dash.langjump","1");}catch(e){}location.replace("?lang="+stored);return true;}}
 return false;}
function onResize(){clearTimeout(S.rz);S.rz=setTimeout(()=>{measureBar();fitAll(true);},150);}
function boot(){
 if(langBoot())return;
 [["lang-en","en"],["lang-tr","tr"]].forEach(x=>{$(x[0]).addEventListener("click",()=>lsSet("orkestra.dash.lang",x[1]));});
 try{const o=JSON.parse(lsGet("orkestra.dash.open")||"{}");if(o&&typeof o==="object")S.open=o;}catch(e){}
 try{const f=JSON.parse(lsGet("orkestra.dash.filter")||"[]");if(Array.isArray(f))f.forEach(g=>{if(GROUPS.indexOf(g)>=0)S.filter.add(g);});}catch(e){}
 S.q=lsGet("orkestra.dash.q")||"";S.older=lsGet("orkestra.dash.older")==="1";
 const q=$("q");q.value=S.q;$("older").checked=S.older;
 q.addEventListener("input",()=>{S.q=q.value.trim();lsSet("orkestra.dash.q",S.q);if(S.data)render();});
 $("older").addEventListener("change",()=>{S.older=$("older").checked;lsSet("orkestra.dash.older",S.older?"1":"0");
  if(S.older&&!S.items.length)showSkeleton();clearTimeout(S.timer);if(S.inflight)S.again=true;else tick();});
 setText($("stale-msg"),L.ui_stale);setText($("stale-copy"),L.ui_stale_cmd);
 $("stale-copy").addEventListener("click",()=>doCopy(CMD_RESTART,$("stale-code"),$("stale-copy")));
 setText($("rerr-msg"),L.ui_render_error);setText($("rerr-reload"),L.ui_reload);$("rerr-reload").addEventListener("click",()=>location.reload());
 buildChips();renderFoot();
 setText($("offline-retry"),L.ui_retry_now);setText($("conn-retry"),L.ui_retry_now);
 $("offline-retry").addEventListener("click",retryNow);$("conn-retry").addEventListener("click",retryNow);
 setText($("keys-btn"),L.ui_keys_btn);setText($("keys-on-text"),L.ui_keys_toggle);setAttr($("help"),"aria-label",L.ui_keys_btn);
 S.keysOn=lsGet("orkestra.dash.keys")!=="0";$("keys-on").checked=S.keysOn;
 $("keys-on").addEventListener("change",()=>{S.keysOn=$("keys-on").checked;lsSet("orkestra.dash.keys",S.keysOn?"1":"0");});
 $("keys-btn").addEventListener("click",()=>setHelp(!S.help));
 $("main").addEventListener("click",onMainClick);
 $("radar-list").addEventListener("click",onRadarClick);
 if(canAct()){show($("acts"),true);setText($("new-lbl"),L.ui_new);setText($("tools-lbl"),L.ui_tools);
  setAttr($("new-btn"),"aria-label",L.ui_new);setAttr($("tools-btn"),"aria-label",L.ui_tools);
  setAttr($("new-btn"),"title",L.ui_new);setAttr($("tools-btn"),"title",L.ui_tools);
  $("new-btn").addEventListener("click",openNew);$("tools-btn").addEventListener("click",openTools);}
 $("nr-form").addEventListener("submit",submitNew);$("nr-cancel").addEventListener("click",closeNew);
 $("nr-project").addEventListener("change",()=>{fieldErr("nr-project-err","",$("nr-project"));nrEngines();});
 $("nr-engine").addEventListener("change",()=>fieldErr("nr-err",""));
 $("nr-req").addEventListener("input",nrCount);$("nr-ok").addEventListener("change",()=>{if($("nr-ok").checked)fieldErr("nr-ok-err","",$("nr-ok"));});
 $("nr-svc-btn").addEventListener("click",()=>startService("nr"));$("tl-svc-btn").addEventListener("click",()=>startService("tl"));
 $("nr").addEventListener("cancel",e=>{if(S.nr.busy&&e.preventDefault)e.preventDefault();});
 $("tl-close").addEventListener("click",closeTools);$("tl-jobs").addEventListener("click",onToolsJobs);
 $("tl-preview").addEventListener("click",()=>clearRuns(false));$("tl-doc-run").addEventListener("click",runDoctor);
 $("tl-clear-out").addEventListener("click",e=>{const b=actOf(e.target);if(b&&b.dataset.act==="clearapply")clearRuns(true);});
 $("rd-close").addEventListener("click",closeDoc);
 $("rd-tabs").addEventListener("click",e=>{let n=e.target;while(n&&!(n.dataset&&n.dataset.doc))n=n.parentNode;if(n)showDoc(n.dataset.doc);});
 S.radarAll=lsGet("orkestra.dash.radar")==="1";
 $("radar-tog").addEventListener("click",()=>{S.radarAll=!S.radarAll;lsSet("orkestra.dash.radar",S.radarAll?"1":"0");renderRadar();});
 $("ctl-form").addEventListener("submit",submitCtl);
 $("ctl-cancel").addEventListener("click",()=>{if(S.ctl&&!S.ctlBusy[S.ctl.key])closeCtl();});
 $("ctl").addEventListener("cancel",e=>{if(S.ctl&&S.ctlBusy[S.ctl.key]&&e.preventDefault)e.preventDefault();});
 $("ctl").addEventListener("close",()=>{if(S.ctl)closeCtl();});
 $("ctl-reason").addEventListener("input",()=>{const n=String($("ctl-reason").value||"").replace(/\s+/g," ").trim().length;
  if(n>REASON_MAX)ctlErr(fmt(L.ui_ctl_err_reason_long,{n:n}),true);else if(S.ctlBad)ctlErr("");});
 document.addEventListener("keydown",onKey);
 document.addEventListener("pointerdown",e=>{if(e&&e.button>0)return;S.pointer=Date.now();});
 ["pointerup","pointercancel","contextmenu","dragend"].forEach(t=>document.addEventListener(t,()=>{S.pointer=0;}));
 document.addEventListener("selectionchange",flushDeferred);
 document.addEventListener("visibilitychange",()=>{S.pointer=0;if(!document.hidden){clearTimeout(S.timer);tick();}});
 if(typeof window!=="undefined"&&window.addEventListener){
  window.addEventListener("online",()=>{clearTimeout(S.timer);tick();});
  window.addEventListener("blur",()=>{S.pointer=0;});window.addEventListener("resize",onResize);
  window.addEventListener("beforeprint",()=>onPrint(true));window.addEventListener("afterprint",()=>onPrint(false));}
 try{if(typeof ResizeObserver==="function"){const ro=new ResizeObserver(measureBar);[$("bar"),$("r1")].forEach(n=>{if(n)ro.observe(n);});}}catch(e){}
 measureBar();setInterval(secondTick,1000);tick();}
if(typeof window!=="undefined")window.OrkDash={shortWhat:shortWhat,groupFeed:groupFeed,sortItems:sortItems,nextDelay:nextDelay,kindOf:kindOf,
 dur:dur,ago:ago,state:S,attnOf:attnOf,apply:apply,riskSum:riskSum,openCtl:openCtl,submitCtl:submitCtl,goTo:goTo,
 openNew:openNew,submitNew:submitNew,openTools:openTools,openDoc:openDoc,sendReply:sendReply};
boot();
</script></body></html>
"""


def ui_labels() -> Dict[str, str]:
    """The label table the dashboard script reads, in the current language."""
    return {key.split(".", 1)[1]: i18n.t(key) for key in UI_KEYS}


def _script_json(data: Any) -> str:
    """JSON that is safe inside a <script> element: nothing in it can close the element or open a comment."""
    text = json.dumps(data, ensure_ascii=False)
    for raw, esc in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(raw, esc)
    return text


def _json_bytes(data: Any) -> bytes:
    """The JSON answer as UTF-8 bytes. Text with a lone surrogate (a half-written file) cannot be UTF-8: it goes out
    \\u-escaped instead (still valid JSON) rather than failing the whole answer."""
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(data, ensure_ascii=True).encode("ascii")


_STARTED = time.time()                          # when this process began serving (the footer says how long ago)


def plugin_version() -> str:
    """The Orkestra release number (the manifests carry none, so Claude Code keys its cache on the commit)."""
    from . import __version__
    return __version__


_VERSION: Optional[str] = None


def page_html(build: str = "", started: Optional[float] = None, token: str = "") -> str:
    """The dashboard page in the current language: `<html lang>`, the static texts and the label table. `build` is the
    id of the code that serves it (the page reloads itself when the server answers with another one); `started` is
    when the server began (epoch seconds) and feeds the trust line in the footer. `token` is the per-process secret the
    page sends with Pause / Abort ("" = a read-only page: no control is shown)."""
    code = i18n.lang()
    meta = {"version": plugin_version(), "build": build, "started": _STARTED if started is None else started,
            "workspace": paths.workspace_display(), "controls": bool(token), "token": token}
    page = PAGE.replace("__LANG__", code).replace("__BUILD__", html.escape(build, quote=True))
    page = page.replace("__CUR_EN__", ' aria-current="true"' if code == "en" else "")
    page = page.replace("__CUR_TR__", ' aria-current="true"' if code == "tr" else "")
    for token, key in (("__TITLE__", "monitor.ui_title"), ("__LOADING__", "monitor.ui_loading"),
                       ("__OLDER__", "monitor.ui_older"), ("__SKIP__", "monitor.ui_skip"),
                       ("__LANG_LABEL__", "monitor.ui_lang_label"), ("__LANG_EN__", "monitor.ui_lang_en"),
                       ("__LANG_TR__", "monitor.ui_lang_tr"), ("__SEARCH_PH__", "monitor.ui_search_ph"),
                       ("__SEARCH__", "monitor.ui_search")):
        page = page.replace(token, html.escape(i18n.t(key)))
    return page.replace("__META__", _script_json(meta)).replace("__LABELS__", _script_json(ui_labels()))


# ---------------------------------------------------------------- code freshness
# A dashboard left running for days kept serving the code it was started with, so an updated Orkestra still showed the
# old page. The server now notices that its own sources changed on disk: it re-executes itself (POSIX) and the open
# page reloads when the build id changes; where it cannot re-execute, the page says so instead of staying silent.

_CODE_DIR = Path(__file__).resolve().parent


def code_signature() -> tuple:
    """(path, mtime_ns, size) of every module of this package: cheap, and enough to notice an update."""
    out = []
    for p in sorted(_CODE_DIR.rglob("*.py")):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((str(p.relative_to(_CODE_DIR)), st.st_mtime_ns, st.st_size))
    return tuple(out)


def build_id(signature: Optional[tuple] = None) -> str:
    """Short stable id of a code signature (what the page compares)."""
    return hashlib.sha1(repr(signature if signature is not None else code_signature()).encode("utf-8")).hexdigest()[:12]


def sources_compile(old: tuple, new: tuple) -> bool:
    """Do the modules that changed since `old` still compile? A half-saved file must not take the dashboard down."""
    before = {name: (m, sz) for name, m, sz in old}
    for name, m, sz in new:
        if before.get(name) == (m, sz):
            continue
        try:
            compile((_CODE_DIR / name).read_text(encoding="utf-8"), str(_CODE_DIR / name), "exec")
        except (OSError, SyntaxError, ValueError):
            return False
    return True


class _Freshness:
    """What the running server was started with, and whether the disk has moved on (checked at most once a second)."""

    def __init__(self) -> None:
        self.started = code_signature()
        self.build = build_id(self.started)
        self.started_at = time.time()
        self._at = 0.0
        self._stale = False

    def stale(self) -> bool:
        now = time.monotonic()
        if now - self._at >= 1.0:
            self._at = now
            self._stale = code_signature() != self.started
        return self._stale


# ---------------------------------------------------------------- control: Pause / Abort from the page
# The page may change exactly two things, the person's own decisions: pause a run, abort a run. Anything else (resume,
# approve, a phase) needs the chat that drives the run. A request names the run by its page key, which is looked up
# among the runs this dashboard lists: no path, project or run id from the request is ever trusted. Every session that
# worked on the run in the last CONTROL_SESSION_WINDOW gets a stop note (state.request_stop), so its next tool call is
# refused with "stop now" (hookmain.deny_stopped): a status change alone does not stop a turn that is under way.

CONTROL_ACTIONS = {"pause": "paused", "abort": "aborted"}
# Every POST the page can send. Each one passes the same guard (Host, Origin, token, JSON body) before it is read.
POST_ROUTES = frozenset({"/api/control", "/api/console/start", "/api/console/reply", "/api/console/cancel",
                         "/api/console/continue", "/api/console/service", "/api/console/clear", "/api/console/doctor"})
CONTROL_SESSION_WINDOW = 2 * 3600.0
CONTROL_BODY_MAX = 32 * 1024                 # a run request may be a few thousand characters
CONTROL_REASON_MAX = 300


def _locate(key: str, project: Optional[Path] = None, workspace: Optional[Path] = None) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """(project, state) of the run whose page key is `key`, among the runs this dashboard can list; None otherwise."""
    if not re.fullmatch(r"[0-9a-f]{12}", str(key or "")):
        return None
    projects = [project] if project else list(_project_dirs(workspace or paths.workspace_root()))
    for proj in projects:
        rdir = paths.runs_dir(proj)
        if not rdir.is_dir():
            continue
        for d in rdir.iterdir():
            st = read_json(d / "state.json")
            if isinstance(st, dict) and st.get("run_id") and _view_key(proj, str(st["run_id"])) == key:
                return proj, st
    return None


def _stop_sessions(project: Path, run_id: str, now: float) -> List[str]:
    """Sessions to stop: the ones bound to the run, plus every session id its activity rows show in the window (a
    background subagent shares its parent's id; a Cursor worker has its own)."""
    from . import state as ST
    out = [s["id"] for s in ST.run_sessions(project, run_id)]
    for row in A.read_rows(project, run_id):
        sid = str(row.get("sid") or "")
        if sid and now - float(row.get("ts") or 0) <= CONTROL_SESSION_WINDOW:
            out.append(sid)
    return list(dict.fromkeys(out))


def control_run(key: str, action: str, reason: str = "", project: Optional[Path] = None,
                workspace: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Pause or abort a run for the person at the dashboard. {"ok": True, "status", "changed", "stopped"} or
    {"ok": False, "error": <code>} (codes: bad_action, reason_long, not_found, closed, failed)."""
    from . import runs as R
    from . import state as ST
    if action not in CONTROL_ACTIONS:
        return {"ok": False, "error": "bad_action"}
    text = " ".join(str(reason or "").split())
    if len(text) > CONTROL_REASON_MAX:
        return {"ok": False, "error": "reason_long"}
    found = _locate(key, project, workspace)
    if not found:
        return {"ok": False, "error": "not_found"}
    proj, st = found
    rid, status = str(st["run_id"]), CONTROL_ACTIONS[action]
    if st.get("status") not in OPEN_STATUSES:
        return {"ok": False, "error": "closed", "status": st.get("status")}
    stamp = time.time() if now is None else now
    text = text or i18n.t("monitor.ctl_reason_default")
    try:
        changed = R.set_status(proj, rid, status, text, source="dashboard", who={"agent_type": "dashboard", "by": "dashboard"})
    except Exception as exc:                        # a run closed meanwhile, a lock held too long: say so, never crash
        return {"ok": False, "error": "closed" if "closed run" in str(exc) else "failed", "detail": _text_of(exc, 160)}
    stopped = ST.request_stop(proj, rid, status, _stop_sessions(proj, rid, stamp), reason=text, now=stamp)
    out = {"ok": True, "status": status, "changed": bool(changed), "stopped": len(stopped), "run_id": rid,
           "project_name": proj.name}
    job = CO.job_for(proj, rid)
    if action == "abort" and job and job.get("status") in CO.JOB_OPEN:
        # a headless run's process is the run service's child: its cancel stops the whole process group at once
        try:
            CO.cancel(str(job.get("id")))
            out["job_cancelled"] = True
        except CO.ConsoleError as exc:
            out["job_cancelled"] = False
            out["job_error"] = exc.code
    return out


def make_handler(project: Optional[Path], port: int, fresh: Optional[_Freshness] = None, controls: bool = True,
                 token: str = ""):
    from http.server import BaseHTTPRequestHandler

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "orkestra-dashboard"

        def log_message(self, *args: Any) -> None:     # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Orkestra-Time", f"{time.time():.3f}")
            if fresh is not None:
                self.send_header("X-Orkestra-Build", fresh.build)
                self.send_header("X-Orkestra-Stale", "1" if fresh.stale() else "0")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            # `--all` answers run to megabytes every 2 s: a browser that accepts gzip gets about a tenth of that
            if len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
                import gzip
                body = gzip.compress(body, compresslevel=4)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Host", "") not in allowed_hosts:    # DNS-rebinding protection
                self._send(403, b"forbidden", "text/plain")
                return
            path, _, query = self.path.partition("?")
            params = urllib.parse.parse_qs(query)
            # the language is chosen per request (`?lang=en|tr`, anything else keeps the server's own): the page and its
            # data must ask with the same code, and two browsers in two languages never see each other's text
            code = (params.get("lang") or [""])[0]
            if path in ("/", "/index.html"):
                with i18n.use_lang(code):
                    body = page_html(fresh.build if fresh else "", fresh.started_at if fresh else None,
                                     token=token if controls else "")
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/runs":
                try:
                    with i18n.use_lang(code):
                        data = discover(project, include_all=(params.get("all") or [""])[0] == "1", cache=True)
                    body = _json_bytes(data)
                except Exception as exc:     # answer with an error the page can name, never a dropped connection
                    self._send(500, _json_bytes({"error": type(exc).__name__}), "application/json")
                    return
                self._send(200, body, "application/json")
            elif path.startswith("/api/console/"):
                self._console_get(path, params, code)
            else:
                self._send(404, b"not found", "text/plain")

        def _answer(self, res: Dict[str, Any], status: int = 200) -> None:
            self._send(status, _json_bytes(res), "application/json")

        def _console_get(self, path: str, params: Dict[str, List[str]], code: str) -> None:
            """Read-only console data: no token needed (a page elsewhere cannot read the answer, and nothing changes)."""
            one = lambda name: str((params.get(name) or [""])[0])          # noqa: E731
            try:
                with i18n.use_lang(code):
                    if path == "/api/console/service":
                        res = dict(CO.service_status(), ok=True, controls=bool(controls and token))
                    elif path == "/api/console/projects":
                        res = dict(CO.projects(code), ok=True)
                    elif path == "/api/console/engines":
                        res = dict(CO.engines(one("project"), code), ok=True)
                    elif path == "/api/console/jobs":
                        res = {"ok": True, "jobs": CO.jobs()}
                    elif path == "/api/console/doc":
                        found = _locate(one("key"), project)
                        if not found:
                            raise CO.ConsoleError("not_found", status=404)
                        res = CO.doc(found[0], str(found[1]["run_id"]), one("name"))
                    else:
                        raise CO.ConsoleError("not_found", status=404)
            except CO.ConsoleError as exc:
                self._answer(exc.answer(), exc.status)
                return
            except Exception as exc:                # an answer the page can name, never a dropped connection
                self._answer({"ok": False, "error": "failed", "detail": type(exc).__name__}, 500)
                return
            self._answer(res)

        def _refuse(self, code: int, error: str) -> None:
            self._send(code, _json_bytes({"ok": False, "error": error}), "application/json")

        def do_POST(self) -> None:  # noqa: N802
            # A web page on any other site can send a POST to 127.0.0.1 (no CORS preflight for a "simple" request):
            # Host, Origin, a JSON body and the per-process token that only this server's own page carries all have
            # to match before anything is read.
            path = self.path.partition("?")[0]
            if self.headers.get("Host", "") not in allowed_hosts:
                self._refuse(403, "host")
                return
            if path not in POST_ROUTES:
                self._refuse(404, "not_found")
                return
            if not controls or not token:
                self._refuse(403, "read_only")
                return
            site = (self.headers.get("Sec-Fetch-Site") or "same-origin").lower()
            if self.headers.get("Origin", "") not in allowed_origins or site != "same-origin":
                self._refuse(403, "origin")
                return
            if not hmac.compare_digest(self.headers.get("X-Orkestra-Token", ""), token):
                self._refuse(403, "token")
                return
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._refuse(415, "content_type")
                return
            try:
                size = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                size = -1
            if size <= 0 or size > CONTROL_BODY_MAX:
                self._refuse(413 if size > CONTROL_BODY_MAX else 400, "body")
                return
            try:
                req = json.loads(self.rfile.read(size).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                req = None
            if not isinstance(req, dict):
                self._refuse(400, "body")
                return
            code = str(req.get("lang") or "")
            text = lambda name: str(req.get(name) or "")                   # noqa: E731
            status = 200
            try:
                with i18n.use_lang(code):
                    if path == "/api/control":
                        res = control_run(text("key"), text("action"), text("reason"), project=project)
                        status = 200 if res.get("ok") else {"not_found": 404, "closed": 409, "failed": 500}.get(
                            str(res.get("error")), 400)
                    elif path == "/api/console/start":
                        res = CO.start(text("project"), text("request"), text("engine"), text("mode") or "autopilot", code)
                    elif path == "/api/console/reply":
                        res = CO.reply(text("job"), text("text"), code)
                    elif path == "/api/console/cancel":
                        res = CO.cancel(text("job"), code)
                    elif path == "/api/console/continue":
                        found = _locate(text("key"), project)
                        if not found:
                            raise CO.ConsoleError("not_found", status=404)
                        res = CO.continue_run(found[0], str(found[1]["run_id"]), code)
                    elif path == "/api/console/service":
                        res = CO.start_service()
                    elif path == "/api/console/clear":
                        res = CO.clear_runs(req.get("hours"), bool(req.get("apply")))
                    else:                                   # /api/console/doctor
                        res = CO.doctor(code)
            except CO.ConsoleError as exc:
                res, status = exc.answer(), exc.status
            except Exception as exc:                # an answer the page can name, never a dropped connection
                res, status = {"ok": False, "error": "failed", "detail": type(exc).__name__}, 500
            self._send(status, _json_bytes(res), "application/json")

    return H


REEXEC_ENV = "ORKESTRA_DASHBOARD_REEXEC"          # set on the re-executed process: no second browser tab, patient bind


def _can_reexec() -> bool:
    """Self-reload needs a POSIX exec of the same command line; elsewhere the page's banner tells the user instead."""
    return (os.name != "nt" and not os.environ.get("ORKESTRA_DASHBOARD_NO_RELOAD") and bool(sys.executable)
            and bool(sys.argv) and os.path.exists(sys.argv[0]))


def _watch_code(srv: Any, fresh: _Freshness, reload_flag: threading.Event, interval: float = 1.5) -> None:
    """Ask the server to stop when its sources changed and still compile (the main thread then re-executes)."""
    seen = fresh.started
    while not reload_flag.is_set():
        time.sleep(interval)
        now = code_signature()
        if now == seen:
            continue
        if sources_compile(seen, now):
            reload_flag.set()
            srv.shutdown()
            return
        seen = now                                  # a broken save: wait for the next one, keep serving


# ---------------------------------------------------------------- a port that is already taken
# A dashboard started before the freshness feature cannot update itself and kept serving the old page for days, so
# "port busy, it may already be running" actively told the user to keep using it. The probe tells the three cases apart.

def probe_dashboard(port: int, timeout: float = 2.0) -> str:
    """What answers on 127.0.0.1:`port`: 'old' (an orkestra dashboard without the build header: it cannot update itself),
    'current' (one that carries it) or 'other'."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            server, build = resp.getheader("Server") or "", resp.getheader("X-Orkestra-Build")
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return "other"
    if not server.startswith("orkestra-dashboard"):
        return "other"
    return "current" if build else "old"


def _run_text(argv: List[str]) -> str:
    import subprocess
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def listener_pid(port: int) -> Optional[int]:
    """Process id that listens on 127.0.0.1:`port` (POSIX, via lsof); None when it cannot be found."""
    if os.name == "nt":
        return None
    out = _run_text(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    for token in out.split():
        if token.isdigit() and int(token) != os.getpid():
            return int(token)
    return None


def _etime_seconds(text: str) -> Optional[int]:
    """`ps -o etime=` (`[[dd-]hh:]mm:ss`) in seconds."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text.strip())
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + sec


def _process_age(pid: int) -> str:
    seconds = _etime_seconds(_run_text(["ps", "-o", "etime=", "-p", str(pid)]))
    return "" if seconds is None else i18n.ago(seconds)


def _is_orkestra_dashboard(pid: int) -> bool:
    """Is `pid` an `orkestra dashboard` process? The command line is read as arguments: the launcher (`orkestra`) must be
    one of them and `dashboard` the argument right after it, so a program that merely has both words somewhere in its
    command line (a path, a folder name, another subcommand's value) is never taken for one."""
    import shlex
    command = _run_text(["ps", "-o", "command=", "-p", str(pid)])
    try:
        argv = shlex.split(command)
    except ValueError:                                      # a command line that cannot be read is not touched
        return False
    for i, arg in enumerate(argv):
        name = os.path.basename(arg.replace("\\", "/")).lower()
        if name in ("orkestra", "orkestra.py") and argv[i + 1:i + 2] == ["dashboard"]:
            return True
    return False


def _may_replace() -> bool:
    return (os.name != "nt" and not os.environ.get("CLAUDECODE") and sys.stdin.isatty() and sys.stdout.isatty())


def _replace_listener(port: int) -> bool:
    """Politely stop the orkestra dashboard that holds `port` (SIGTERM, never SIGKILL) and wait for the port to free."""
    import signal
    import socket
    pid = listener_pid(port)
    if pid is None or not _is_orkestra_dashboard(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(30):
        time.sleep(0.1)
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return True
    return False


def _busy_outcome(port: int, exc: OSError, open_browser: bool, replace: bool) -> Optional[int]:
    """The dashboard could not bind. An exit code to return, or None when `--replace` freed the port (bind again)."""
    url = f"http://127.0.0.1:{port}/"
    kind = probe_dashboard(port)
    ours = kind in ("old", "current")                       # `--replace` only ever concerns an orkestra dashboard
    tried = False
    if replace and ours and _may_replace():
        tried = True
        if _replace_listener(port):
            return None
    if kind == "current" and not replace:
        _emit(i18n.t("monitor.dashboard_running", url=url) + "\n")
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        return 0
    if replace and ours:                                    # asked for, and it did not happen: say why, not the old hint
        which = i18n.t("monitor.which_old" if kind == "old" else "monitor.which_current")
        if tried:
            text = i18n.t("monitor.replace_failed", port=port, which=which)
        else:
            pid = listener_pid(port)
            text = i18n.t("monitor.replace_needs_tty", port=port, which=which, pid=pid) if pid \
                else i18n.t("monitor.replace_needs_tty_nopid", port=port, which=which)
        _emit(text + "\n", sys.stderr)
        return 2
    if kind == "old":
        pid = listener_pid(port)
        since = _process_age(pid) if pid else ""
        text = i18n.t("monitor.port_busy_old", port=port, pid=pid, since=since) if pid and since \
            else i18n.t("monitor.port_busy_old_nopid", port=port)
        _emit(text + "\n", sys.stderr)
        return 2
    _emit(i18n.t("monitor.port_busy", port=port, exc=exc) + "\n", sys.stderr)
    return 2


def dashboard(project: Optional[Path], port: int, open_browser: bool, replace: bool = False,
              read_only: bool = False) -> int:
    from http.server import ThreadingHTTPServer
    import secrets

    if os.environ.get("CLAUDECODE") and not sys.stdout.isatty():
        sys.stderr.write("orkestra: dashboard is a long-running server — the user starts it in their own terminal "
                         "(`orkestra dashboard --open`). Use `orkestra watch --once` here.\n")
        return 2

    fresh = _Freshness()
    token = "" if read_only else secrets.token_urlsafe(24)     # per process: only the page this server serves has it
    reexec = bool(os.environ.pop(REEXEC_ENV, ""))
    srv = None
    last_exc: Optional[OSError] = None
    patient = reexec
    for round_ in range(2):
        for attempt in range(40 if patient else 1):      # the previous incarnation may still be closing its socket
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(project, port, fresh, controls=not read_only,
                                                                              token=token))
                break
            except OSError as exc:
                last_exc = exc
                if attempt < (39 if patient else 0):
                    time.sleep(0.25)
        if srv is not None or reexec or round_:
            break
        outcome = _busy_outcome(port, last_exc, open_browser, replace)
        if outcome is not None:
            return outcome
        patient = True                                  # --replace stopped the old one: wait for its socket to close
    if srv is None:
        _emit(i18n.t("monitor.port_busy", port=port, exc=last_exc) + "\n", sys.stderr)
        return 2
    url = f"http://127.0.0.1:{port}/"
    _emit(i18n.t("monitor.dashboard_up" if read_only else "monitor.dashboard_up_ctl", url=url) + "\n")
    sys.stdout.flush()
    if open_browser and not reexec:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    reload_flag = threading.Event()
    if _can_reexec():
        threading.Thread(target=_watch_code, args=(srv, fresh, reload_flag), daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    if reload_flag.is_set():
        _emit(i18n.t("monitor.dashboard_reload") + "\n")
        sys.stdout.flush()
        os.environ[REEXEC_ENV] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return 0
