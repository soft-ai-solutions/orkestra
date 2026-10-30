"""Live activity feed per run: one JSON line per tool call / subagent event (written by hooks).

Lives in .orkestra/runs/<run>/activity.jsonl (git-ignored). Read by `orkestra watch` / `orkestra dashboard`.
Never blocks: every write is best-effort and size-bounded; secrets-looking values are masked.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .i18n import t
from .util import file_lock, read_json, write_json_atomic

MAX_BYTES = 512 * 1024
ARCHIVE_MAX_BYTES = 16 * 1024 * 1024       # activity.1.jsonl (older rows, kept for timing analysis)
# tool: a tool call starts · end: a Bash call finished (liveness only, hidden) · deny: a guard blocked a call
# result: a subagent replied / was launched · agent_done: SubagentStop · answer: AskUserQuestion answered
# user: a HUMAN prompt · handback: a subagent report / task notification delivered as a prompt (not the user)
# idle: the main turn ended (the session waits for the user) · wait_agents: the main turn ended while subagents
# are still running (their hand-back resumes it) · nudge: Stop hook kept the run going · bind: session bound
# perm_wait: the host shows a permission prompt · api_error: the turn died on a model/API error · loop: the deny
# circuit breaker blocked the run (these three are asides: never progress, never an open question)
HIDDEN_KINDS = ("end",)
ASIDE_KINDS = ("end", "nudge", "handback", "agent_done", "status", "perm_wait", "api_error", "loop")    # never hide an open question / never count as progress
KEEP_LINES = 1200
SYNTHETIC_PROMPT_PREFIXES = ("<agent-message", "<task-notification", "[Subagent hand-back]")
_MASK = re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|authorization)\s*[=:]\s*(?:bearer\s+)?"
                   r"|bearer\s+)([^\s'\"]+)")


def _mask(text: str) -> str:
    return _MASK.sub(lambda m: m.group(1) + "***", text)


def _rel(project: Path, p: str) -> str:
    try:
        return str(Path(p).resolve(strict=False).relative_to(project.resolve(strict=False)))
    except Exception:
        return p


def ask_heads(ev: Dict[str, Any]) -> str:
    """What an AskUserQuestion call asks about: the question headers (else the questions), comma separated."""
    qs = (ev.get("tool_input") or {}).get("questions") or []
    return ", ".join(str(q.get("header") or q.get("question") or "")[:30] for q in qs if isinstance(q, dict))


def summarize(ev: Dict[str, Any], project: Path) -> str:
    tool = ev.get("tool_name") or ""
    ti = ev.get("tool_input") or {}
    if tool == "Bash":
        # long enough that a risky tail (`… && git push --force`) is still on record for the dashboard's risk list
        return _mask(" ".join(str(ti.get("command") or "").split()))[:480]
    if tool in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit"):
        return _rel(project, str(ti.get("file_path") or ti.get("notebook_path") or ""))
    if tool in ("Agent", "Task"):
        return f"→ {ti.get('subagent_type') or 'agent'}: {ti.get('description') or ''}"[:160]
    if tool == "SendMessage":
        return f"→ {ti.get('to') or ti.get('recipient') or '?'}: {ti.get('summary') or ''}"[:160]
    if tool == "AskUserQuestion":
        return t("activity.ask_summary", heads=ask_heads(ev))[:160]
    if tool in ("Grep", "Glob"):
        return f"{ti.get('pattern') or ''} {ti.get('path') or ti.get('glob') or ''}".strip()[:160]
    if tool in ("WebFetch", "WebSearch"):
        return str(ti.get("url") or ti.get("query") or "")[:160]
    if tool.startswith("mcp__"):
        arg = ti.get("relative_path") or ti.get("name_path") or ti.get("libraryName") or ti.get("query") or ""
        return f"{tool.split('__')[-1]} {arg}".strip()[:160]
    return ""


def record(project: Path, run_id: str, ev: Dict[str, Any], kind: str, extra: str = "", key: str = "") -> None:
    try:
        path = paths.run_dir(project, run_id) / "activity.jsonl"
        if not path.parent.is_dir():
            return
        what = extra or summarize(ev, project)
        if kind == "tool" and not what:
            return                      # tools we do not describe (task-list widgets etc.) are noise
        row = {
            "ts": time.time(),
            "kind": kind,                                   # see KINDS
            "agent": (ev.get("agent_type") or "main"),
            "tool": ev.get("tool_name") or "",
            "what": _mask(what),
        }
        if key:
            row["key"] = key[:16]                           # deny rows: short id of the refused call (loop post-mortems)
        if kind == "deny" and extra:
            attempt = summarize(ev, project)                # the refused call itself: the page names what was blocked
            if attempt:
                row["cmd"] = _mask(attempt)
        sid = str(ev.get("session_id") or "")
        if sid:
            row["sid"] = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:120]
        if ev.get("agent_type") and ev.get("agent_id"):
            row["aid"] = str(ev["agent_id"])[:64]           # which instance (two implementers share one type)
        # the append shares _rotate's lock: a rotation rewrites the file from a snapshot and would drop a row appended
        # meanwhile (end/deny rows feed the stall classifiers). Shared, so concurrent appenders never queue on each other
        # (only a rotation excludes them). Short wait, then append unlocked: never hold a hook up.
        with file_lock(path, timeout=1.0, on_timeout="proceed", shared=True):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if path.stat().st_size > MAX_BYTES:
            _rotate(path)
    except Exception:
        pass


def _rotate(path: Path) -> None:
    """Keep the newest KEEP_LINES rows live, and no more than 3/4 of MAX_BYTES of them (long rows must not leave the file
    above MAX_BYTES, which would rotate it again on every append); move the older rows to activity.1.jsonl instead of
    dropping them."""
    with file_lock(path):
        all_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        budget, kept, size = MAX_BYTES * 3 // 4, 0, 0
        for line in reversed(all_lines):
            n = len(line.encode("utf-8")) + 1
            if kept >= KEEP_LINES or (kept and size + n > budget):
                break
            kept, size = kept + 1, size + n
        if kept >= len(all_lines):
            return
        old, keep = all_lines[:len(all_lines) - kept], all_lines[len(all_lines) - kept:]
        arch = path.with_name("activity.1.jsonl")
        try:
            if arch.exists() and arch.stat().st_size > ARCHIVE_MAX_BYTES:
                os.replace(arch, path.with_name("activity.2.jsonl"))
            with open(arch, "a", encoding="utf-8") as fh:
                fh.write("\n".join(old) + "\n")
        except OSError:
            pass                                    # archiving is best effort; the live file must stay bounded
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def is_synthetic_prompt(prompt: str) -> bool:
    """Subagent hand-backs and task notifications arrive through UserPromptSubmit but are NOT typed by the user."""
    head = (prompt or "").lstrip()[:800]
    if head.startswith(SYNTHETIC_PROMPT_PREFIXES):
        return True
    # some front ends wrap the hand-back (`<system-reminder …>Another session sent a message … <agent-message …>`)
    return head.startswith("<system-reminder") and bool(
        re.search(r"<agent-message\b|<task-notification\b|\[Subagent hand-back\]", head))



DRIVE_KINDS = frozenset({"tool", "result", "answer", "deny", "end"})


def _safe_sid_token(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "")[:120]


def last_user_what(project: Path, run_id: str, session_id: str = "") -> str:
    """Newest activity `user` row text for this session (empty when none)."""
    rows = read_rows(project, run_id)
    want = _safe_sid_token(session_id) if session_id else ""
    for r in reversed(rows):
        if r.get("kind") != "user":
            continue
        sid = str(r.get("sid") or "")
        if want and sid and sid != want:
            continue
        return str(r.get("what") or "")
    return ""


def turn_drove_run(project: Path, run_id: str, session_id: str = "") -> bool:
    """True when this session did drive work on the run since the last real user prompt.

    No user prompt yet: a fresh bind still counts, so start/resume mid-run keeps Stop pressure.
    After a user prompt with no tool/result/answer/deny/end: False (unbind + idle — do not hijack).

    If the last user row for this session is off-topic vs the bound project, return False even when
    tools ran earlier — Stop follow-ups skip beforeSubmitPrompt, so this is the bind-isolation gate.
    """
    from . import intent as _intent
    rows = read_rows(project, run_id)
    want = _safe_sid_token(session_id) if session_id else ""
    last_user = -1
    for i, r in enumerate(rows):
        if r.get("kind") != "user":
            continue
        sid = str(r.get("sid") or "")
        if want and sid and sid != want:
            continue
        last_user = i
    if last_user >= 0:
        what = str(rows[last_user].get("what") or "")
        if what and _intent.off_topic_vs_project(what, project, run_id, title=""):
            return False
    after = rows[last_user + 1:] if last_user >= 0 else rows
    for r in after:
        if r.get("kind") not in DRIVE_KINDS:
            continue
        sid = str(r.get("sid") or "")
        if want and sid and sid != want:
            continue
        return True
    if last_user < 0:
        for r in rows:
            if r.get("kind") != "bind":
                continue
            sid = str(r.get("sid") or "")
            if want and sid and sid != want:
                continue
            return True
    return False

def read_all_rows(project: Path, run_id: str) -> List[Dict[str, Any]]:
    """Archive + live rows, oldest first (timing analysis only — never called from hooks)."""
    out: List[Dict[str, Any]] = []
    for name in ("activity.2.jsonl", "activity.1.jsonl", "activity.jsonl"):
        try:
            with open(paths.run_dir(project, run_id) / name, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        out.append(row)
        except OSError:
            continue
    return out


def read_rows(project: Path, run_id: str, max_bytes: int = 96 * 1024) -> List[Dict[str, Any]]:
    path = paths.run_dir(project, run_id) / "activity.jsonl"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "ignore")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]                              # first line is probably cut
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def visible(rows: List[Dict[str, Any]], n: int = 12) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("kind") not in HIDDEN_KINDS][-n:] if n > 0 else []


def tail(project: Path, run_id: str, n: int = 12) -> List[Dict[str, Any]]:
    return visible(read_rows(project, run_id), n)


def pending_question(rows: List[Dict[str, Any]]) -> Optional[str]:
    """An AskUserQuestion that started and has not been answered (nothing else happened after it)."""
    for r in reversed(rows):
        k = r.get("kind")
        if k in ASIDE_KINDS or (r.get("agent") or "main") != "main":
            continue
        if k == "tool" and r.get("tool") == "AskUserQuestion":
            return r.get("what") or t("activity.question")
        return None
    return None


def running_agents(rows: List[Dict[str, Any]]) -> List[str]:
    """Subagents started by the main session that have not replied yet (best effort)."""
    open_: List[str] = []
    for r in rows:
        if r.get("tool") not in ("Agent", "Task"):
            continue
        what = str(r.get("what") or "")
        name = what.lstrip("→← ").split(": ", 1)[0].strip()
        if r.get("kind") == "tool":
            open_.append(name)
        elif r.get("kind") == "result" and what.rstrip().endswith("async_launched"):
            continue                                # launched in the background: closed by its agent_done row
        elif r.get("kind") in ("result", "deny") and name in open_:
            open_.remove(name)
        elif r.get("kind") == "deny" and open_:
            open_.pop()
    for r in rows:                                  # background agents end with a SubagentStop ("agent_done") row
        if r.get("kind") == "agent_done" and r.get("agent") in open_:
            open_.remove(r.get("agent"))
    return open_


# ---------------------------------------------------------------- background subagent registry (hook-written)
# .orkestra/runs/<run>/agents.json — which subagents were launched and have not stopped yet. Claude Code runs
# subagents in the background and hands their report back later; the Stop hook and `orkestra next` need to know
# whether the main session is legitimately waiting for one.

AGENT_TASK_RE = re.compile(r"\b(T\d{3}|FIX-[A-Z0-9][A-Z0-9-]{1,18})\b")


def _agents_path(project: Path, run_id: str) -> Path:
    return paths.run_dir(project, run_id) / "agents.json"


def _agents_load(path: Path) -> Dict[str, Any]:
    data = read_json(path, default={}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("pending", {})
    data.setdefault("seen", {})
    return data


def _find_key(obj: Any, names: tuple, depth: int = 0) -> Optional[str]:
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower().replace("_", "") in names and isinstance(v, (str, int)) and str(v):
                return str(v)
        for v in obj.values():
            hit = _find_key(v, names, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj[:20]:
            hit = _find_key(v, names, depth + 1)
            if hit:
                return hit
    return None


def launch_info(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """PostToolUse(Agent/Task): None when the agent ran in the foreground (its result is already here)."""
    resp = ev.get("tool_response")
    text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False, default=str)
    if isinstance(resp, dict) and ("status" in resp or "isAsync" in resp):
        # structural: a foreground agent whose REPORT merely quotes "async_launched" must not be registered
        if resp.get("status") != "async_launched" and resp.get("isAsync") is not True:
            return None
    else:
        head = text[:400]                           # older / other shapes: the launch notice is the start of the reply
        if "async_launched" not in head and not re.search(
                r"async\s+agent\s+launched|launched\s+(successfully\s+)?in\s+the\s+background", head, re.I):
            return None
    ti = ev.get("tool_input") or {}
    aid = _find_key(resp, ("agentid",)) or ""
    if not aid:
        m = re.search(r"agentId[\"':\s]+([A-Za-z0-9_-]{6,64})", text)
        aid = m.group(1) if m else ""
    blob = f"{ti.get('description') or ''} {ti.get('prompt') or ''}"
    return {"id": aid, "type": str(ti.get("subagent_type") or "agent"), "name": str(ti.get("name") or ""),
            "desc": " ".join(str(ti.get("description") or "").split())[:80],
            "tasks": sorted(set(AGENT_TASK_RE.findall(blob)))[:8]}


def agent_tasks(project: Path, run_id: str, agent_id: str) -> Optional[List[str]]:
    """The task ids registered for one running subagent (from its Task prompt); None when no entry names that id."""
    try:
        entry = (_agents_load(_agents_path(project, run_id)).get("pending") or {}).get(agent_id)
    except Exception:
        return None
    if not agent_id or not isinstance(entry, dict):
        return None
    return [str(x) for x in entry.get("tasks") or []]


def agent_launched(project: Path, run_id: str, info: Dict[str, Any], now: Optional[float] = None,
                   session: str = "") -> None:
    """`session` = the session that launched it: only that session's process can ever receive the hand-back."""
    try:
        path = _agents_path(project, run_id)
        if not path.parent.is_dir():
            return
        now = time.time() if now is None else now
        with file_lock(path):
            data = _agents_load(path)
            key = info.get("id") or f"?{info.get('type')}@{int(now * 1000)}"
            data["pending"][key] = dict(info, id=key, at=now, sid=session or "")
            seen = data["seen"]
            seen[key] = {"type": info.get("type"), "name": info.get("name") or ""}
            if len(seen) > 300:
                data["seen"] = dict(list(seen.items())[-200:])
            _ring_add(data, info.get("type"), info.get("tasks"), now)
            write_json_atomic(path, data)
    except Exception:
        pass


# Every launch (background or foreground, Claude or Cursor) goes into a bounded ring: `orkestra next` counts identical
# dispatches without progress from it (tempo.dispatch_escalation) instead of repeating the same instruction forever.
LAUNCH_RING = 200


def _ring_add(data: Dict[str, Any], agent_type: Any, tasks: Any, now: float) -> None:
    ring = [x for x in (data.get("launches") or []) if isinstance(x, dict)]
    ring.append({"type": str(agent_type or "agent"), "tasks": sorted({str(x) for x in (tasks or [])})[:8], "at": now})
    data["launches"] = ring[-LAUNCH_RING:]


def launch_from_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """{type, tasks} of any Agent/Task call, foreground ones included (launch_info only describes background ones)."""
    ti = ev.get("tool_input") or {}
    blob = f"{ti.get('description') or ''} {ti.get('prompt') or ''}"
    return {"type": str(ti.get("subagent_type") or "agent"), "tasks": sorted(set(AGENT_TASK_RE.findall(blob)))[:8]}


def record_launch(project: Path, run_id: str, info: Dict[str, Any], now: Optional[float] = None) -> None:
    """Add one launch to the ring (hooks call it for foreground launches; agent_launched does it for background ones)."""
    try:
        path = _agents_path(project, run_id)
        if not path.parent.is_dir():
            return
        with file_lock(path):
            data = _agents_load(path)
            _ring_add(data, info.get("type"), info.get("tasks"), time.time() if now is None else now)
            write_json_atomic(path, data)
    except Exception:
        pass


def launches(project: Path, run_id: str) -> List[Dict[str, Any]]:
    """The launch ring, oldest first: [{type, tasks, at}]."""
    ring = _agents_load(_agents_path(project, run_id)).get("launches")
    return [x for x in ring if isinstance(x, dict)] if isinstance(ring, list) else []


def agent_messaged(project: Path, run_id: str, to: str, summary: str = "", now: Optional[float] = None,
                   session: str = "") -> None:
    """PostToolUse(SendMessage) — the call went through: a finished subagent was asked to continue → pending again."""
    try:
        path = _agents_path(project, run_id)
        if not to or not path.exists():
            return
        now = time.time() if now is None else now
        with file_lock(path):
            data = _agents_load(path)
            key = None
            for aid, meta in data["seen"].items():
                if aid == to or (meta.get("name") and meta.get("name") == to):
                    key = aid
                    break
            if key is None:
                return                              # not one of this run's subagents (a teammate, another session)
            meta = data["seen"][key]
            data["pending"][key] = {"id": key, "type": meta.get("type") or "agent", "name": meta.get("name") or "",
                                    "desc": " ".join(str(summary or t("activity.follow_up")).split())[:80],
                                    "tasks": sorted(set(AGENT_TASK_RE.findall(summary or "")))[:8], "at": now,
                                    "sid": session or ""}
            write_json_atomic(path, data)
    except Exception:
        pass


def agent_finished(project: Path, run_id: str, agent_id: str, agent_type: str,
                   now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """SubagentStop: remove the matching pending entry — by id; an entry registered WITHOUT an id (the launch reply
    carried none) is matched by type, oldest first. An unknown id never closes somebody else's entry: stale entries
    are cleared by the hand-back (agent_handed_back), a session restart (agents_purge) and the driver filter in
    agents_pending instead."""
    try:
        path = _agents_path(project, run_id)
        if not path.exists():
            return None
        now = time.time() if now is None else now
        with file_lock(path):
            data = _agents_load(path)
            pend = data["pending"]
            key = agent_id if agent_id in pend else None
            if key is None:
                same = sorted((v.get("at", 0), k) for k, v in pend.items()
                              if k.startswith("?") and v.get("type") == agent_type)
                key = same[0][1] if same else None
            gone = pend.pop(key, None) if key else None
            data["last_done"] = {"id": agent_id, "type": agent_type, "at": now, "async": bool(gone)}
            if gone:        # a registered background agent: its report is still on its way as a message
                due = data.setdefault("reports_due", {})
                due[str(agent_id or key)] = {"at": now, "name": gone.get("name") or "", "type": agent_type}
                for k in [k for k, v in due.items() if now - float(v.get("at") or 0) > 3600]:
                    due.pop(k, None)
            write_json_atomic(path, data)
            return gone
    except Exception:
        return None


def agent_handed_back(project: Path, run_id: str, who: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A hand-back message from `who` (agent id or name) reached the main session: that agent is certainly not
    pending any more, whether or not its SubagentStop was seen (closes entries a missed stop would leave behind)."""
    try:
        path = _agents_path(project, run_id)
        if not who or not path.exists():
            return None
        now = time.time() if now is None else now
        with file_lock(path):
            data = _agents_load(path)
            pend = data["pending"]
            key = next((k for k, v in pend.items()
                        if k == who or v.get("name") == who or (len(who) >= 8 and (k.startswith(who) or who.startswith(k)))), None)
            gone = pend.pop(key, None) if key else None
            data["last_handback"] = {"id": who, "at": now}
            due = data.get("reports_due") or {}
            if due:         # this report is no longer "on its way"; an unknown sender settles the oldest one
                dk = next((k for k, v in due.items() if k == who or v.get("name") == who
                           or (len(who) >= 8 and (k.startswith(who) or who.startswith(k)))), None)
                due.pop(dk if dk is not None else min(due, key=lambda k: float(due[k].get("at") or 0)), None)
                data["reports_due"] = due
            write_json_atomic(path, data)
            return gone
    except Exception:
        return None


def agents_reports_due(project: Path, run_id: str, max_age: float = 90.0,
                       now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Background subagents that have stopped but whose report has not reached the main session yet (younger than
    max_age — a report that never arrives is the watchdog's "dead wait", not a reason to wait forever)."""
    due = _agents_load(_agents_path(project, run_id)).get("reports_due")
    if not isinstance(due, dict) or not due:
        return []
    now = time.time() if now is None else now
    return [dict(v, id=k) for k, v in due.items() if 0 <= now - float(v.get("at") or 0) < max_age]


def last_handback_at(project: Path, run_id: str) -> float:
    """When the newest subagent hand-back reached the main session (0.0 = none recorded)."""
    hb = _agents_load(_agents_path(project, run_id)).get("last_handback")
    try:
        return float(hb.get("at") or 0) if isinstance(hb, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def agents_purge(project: Path, run_id: str, session: Optional[str] = None,
                 ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Drop pending entries (all, only the ones `session` launched, or only `ids`) — its process restarted, the
    agents show no sign of life, or the user said so. Returns what was dropped."""
    try:
        path = _agents_path(project, run_id)
        if not path.exists():
            return []
        with file_lock(path):
            data = _agents_load(path)
            pend = data["pending"]
            keys = [k for k, v in pend.items() if (session is None or (v.get("sid") or "") in ("", session))
                    and (ids is None or k in ids)]
            gone = [pend.pop(k) for k in keys]
            stale_reports = ids is None and bool(data.get("reports_due"))
            if stale_reports:                       # a restarted process will not receive them either
                data["reports_due"] = {}
            if gone or stale_reports:
                write_json_atomic(path, data)
            return gone
    except Exception:
        return []


def session_ages(project: Path, run_id: str, now: Optional[float] = None) -> Dict[str, float]:
    """{session id (file form): seconds since its last hook event}, newest first, for the sessions bound to the run.
    Every hook event of a session — its subagents' tool calls included — refreshes that heartbeat."""
    try:
        from . import state as _ST                 # late: state does not import activity, keep it that way
        now = time.time() if now is None else now
        return {str(s["id"]): max(0.0, now - float(s["heartbeat"])) for s in _ST.run_sessions(project, run_id)}
    except Exception:
        return {}


def _lease_seconds() -> float:
    try:
        return float((paths.global_config().get("watchdog") or {}).get("lease_seconds") or 120)
    except Exception:
        return 120.0


def _safe_sid(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "")[:120]       # same form as state._safe (binding file stem)


def agents_pending(project: Path, run_id: str, max_age: float = 3600.0,
                   now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Subagents launched and not stopped yet, oldest first.

    Lost entries are not reported: older than max_age, or launched by a session that no longer drives the run (a
    background agent dies with the process that started it — a new window can never receive its report)."""
    data = _agents_load(_agents_path(project, run_id))
    if not data["pending"]:
        return []
    now = time.time() if now is None else now
    out = [dict(v, age=int(now - float(v.get("at") or now))) for v in data["pending"].values()
           if now - float(v.get("at") or 0) <= max_age]
    if any(v.get("sid") for v in out):
        ages = session_ages(project, run_id, now)
        if ages:
            driver, lease = next(iter(ages)), _lease_seconds()
            # an entry is lost when ANOTHER session drives the run now and the one that launched it has been silent
            # for a lease period (or is no longer bound). A second window that merely binds does not make the first
            # window's running agents disappear.
            out = [v for v in out if not v.get("sid") or _safe_sid(str(v["sid"])) == driver
                   or ages.get(_safe_sid(str(v["sid"])), lease + 1) <= lease]
    return sorted(out, key=lambda v: v.get("at", 0))


# ---------------------------------------------------------------- Cursor: which conversation is which subagent?
# Cursor runs a subagent as a conversation of its own: its tool calls arrive with a NEW conversation id and no agent
# type. The parent's Task call is seen just before (type + prompt), so the launch is written down here and the first
# event of the new conversation claims it (hooks/cursor_hook.py).

CURSOR_LAUNCH_KEEP = 40


def cursor_launch(project: Path, run_id: str, session: str, launch_id: str, agent_type: str, prompt: str,
                  transcripts_dir: str = "", now: Optional[float] = None) -> None:
    try:
        path = _agents_path(project, run_id)
        if not path.parent.is_dir():
            return
        now = time.time() if now is None else now
        with file_lock(path):
            data = _agents_load(path)
            launches = [x for x in (data.get("cursor_launches") or []) if isinstance(x, dict)]
            launches.append({"id": launch_id or f"?{int(now * 1000)}", "type": agent_type, "sid": session,
                             "prompt": " ".join((prompt or "").split())[:600], "tdir": transcripts_dir, "at": now, "child": ""})
            data["cursor_launches"] = launches[-CURSOR_LAUNCH_KEEP:]
            _ring_add(data, agent_type, AGENT_TASK_RE.findall(prompt or ""), now)
            write_json_atomic(path, data)
    except Exception:
        pass


def cursor_claim(project: Path, run_id: str, conversation_id: str, first_text: str = "",
                 now: Optional[float] = None, max_age: float = 3600.0, unmatched_age: float = 120.0,
                 hold_unmatched: bool = False) -> Optional[Dict[str, Any]]:
    """Match an unknown conversation to a recorded launch. Returns the launch (marked as claimed), a dict with
    {"ambiguous": True, …} when it has to be treated as a subagent although which one cannot be told, or None.

    A subagent's first message is the Task prompt: the launch whose prompt the conversation starts with is taken and
    written down. A conversation that already has its own words and matches no launch is a different chat. It is not
    given a waiting launch's role: that hold blocked ordinary work in every other Cursor chat while one run had a
    subagent open. The only silent case that borrows a role for THIS event is a conversation with no words yet
    (a subagent's first tool calls arrive before its transcript exists). Nothing is written down for that case.
    `hold_unmatched` keeps the short hold only for a conversation Cursor already named as a child of the driver
    whose first message still does not match. A user chat never sets it."""
    try:
        path = _agents_path(project, run_id)
        if not path.exists():
            return None
        now = time.time() if now is None else now
        with file_lock(path):
            data = _agents_load(path)
            launches = [x for x in (data.get("cursor_launches") or []) if isinstance(x, dict)]
            for x in launches:
                if x.get("child") == conversation_id:
                    return x
            open_ = [x for x in launches if not x.get("child") and now - float(x.get("at") or 0) <= max_age]
            if not open_:
                return None
            text = " ".join((first_text or "").split())
            pick = None
            if text:
                pick = next((x for x in open_ if (x.get("prompt") or "") and text.startswith(x["prompt"])), None) or \
                    next((x for x in open_ if len(x.get("prompt") or "") >= 80 and x["prompt"] in text), None)
            if pick is None:
                # Own words that match nothing: this is not an unidentified subagent.
                if text and not hold_unmatched:
                    return None
                waiting = [x for x in open_ if not x.get("closed")
                           and (not text or now - float(x.get("at") or 0) <= unmatched_age)]
                if not waiting:
                    return None
                kinds = {str(x.get("type") or "") for x in waiting}
                return {"ambiguous": True, "sid": waiting[-1].get("sid") or "",
                        "type": kinds.pop() if len(kinds) == 1 else "orkestra:unknown"}
            pick["child"] = conversation_id
            data["cursor_launches"] = launches
            write_json_atomic(path, data)
            return pick
    except Exception:
        return None


def cursor_returned(project: Path, run_id: str, session: str, now: Optional[float] = None,
                    min_age: float = 5.0) -> List[Dict[str, Any]]:
    """Task calls of `session` that have returned. Cursor runs a subagent in the foreground and fires no event when
    it ends (no postToolUse for Task, no subagentStop — measured), but the session cannot make its next move before
    the call is back: the caller reports that move here. Marks the launches and returns the ones just closed.
    A launch younger than `min_age` is left open: a sibling call sent in the same message as the Task arrives
    within milliseconds, while the subagent has not even started."""
    try:
        path = _agents_path(project, run_id)
        now = time.time() if now is None else now

        def due(x: Any) -> bool:
            return isinstance(x, dict) and x.get("sid") == session and not x.get("closed") \
                and now - float(x.get("at") or 0) >= min_age

        peek = read_json(path, default={}) or {}
        if not any(due(x) for x in (peek.get("cursor_launches") or [])):
            return []
        with file_lock(path):
            data = _agents_load(path)
            launches = [x for x in (data.get("cursor_launches") or []) if isinstance(x, dict)]
            done = [x for x in launches if due(x)]
            for x in done:
                x["closed"] = now
            if done:
                data["cursor_launches"] = launches
                write_json_atomic(path, data)
            return done
    except Exception:
        return []


def agents_last_done(project: Path, run_id: str) -> Optional[Dict[str, Any]]:
    ld = _agents_load(_agents_path(project, run_id)).get("last_done")
    return ld if isinstance(ld, dict) else None


def short_agent(agent: str) -> str:
    a = agent or "main"
    return t("activity.main_agent") if a == "main" else a.split(":", 1)[-1]


def liveness(st: Dict[str, Any], rows: List[Dict[str, Any]], now: Optional[float] = None,
             state_mtime: Optional[float] = None, pending: Optional[List[Dict[str, Any]]] = None) -> Dict[str, str]:
    """What the pipeline is doing right now, in one line + a level for colouring (the line is in the user's language
    and in the words of the run's engine: a Cursor run has no "Claude window" and other skill names).

    level: run | wait | quiet | stale | blocked | paused | done
    """
    now = time.time() if now is None else now
    cursor = (st or {}).get("engine") == "cursor"
    where = t("activity.where_cursor" if cursor else "activity.where_claude")
    resume = "/orkestra-resume" if cursor else "/orkestra:resume"
    status = st.get("status")
    if status == "done":
        return {"level": "done", "text": t("activity.live_done")}
    if status == "aborted":
        return {"level": "done", "text": t("activity.live_aborted")}
    if status == "awaiting_user":
        q = (st.get("awaiting") or {}).get("question") or ""
        return {"level": "wait", "text": t("activity.live_wait_question", q=q)[:220]}
    if status == "blocked":
        reason = (st.get("blocked") or {}).get("reason") or ""
        return {"level": "blocked", "text": t("activity.live_blocked", reason=reason)[:220]}
    if status == "paused":
        return {"level": "paused", "text": t("activity.live_paused", resume=resume)}
    q = pending_question(rows)
    if q:
        return {"level": "wait", "text": t("activity.live_wait_open_question", where=where, q=q)[:220]}
    last = None
    for r in reversed(rows):
        if r.get("kind") != "nudge":
            last = r
            break
    event_ts = float(last.get("ts") or 0) if last else 0.0
    state_ts = float(state_mtime or 0)
    last_ts = max(event_ts, state_ts)
    age = max(0, int(now - last_ts)) if last_ts else None
    if pending:                                     # background subagents registered by the hooks (agents.json)
        who = ", ".join(f"{short_agent(str(a.get('type') or ''))}"
                        + (f" {'/'.join(a.get('tasks') or [])}" if a.get("tasks") else "")
                        + f" {_dur(int(now - float(a.get('at') or now)))}" for a in pending[:4])
        main_waits = any(r.get("kind") == "wait_agents" for r in rows[-3:])
        return {"level": "run", "text": t("activity.live_agents_running", who=who)
                                        + (" · " + t("activity.live_main_waits") if main_waits else "")}
    if last and last.get("kind") == "idle":
        if state_ts <= event_ts:
            return {"level": "wait", "text": t("activity.live_wait_turn_ended", ago=_ago(age))}
    talk_phase = st.get("phase") in ("clarify", "approve")
    if talk_phase and (age is None or age >= 90) and not (last and last.get("kind") == "tool"
                                                          and last.get("tool") == "Bash" and (age or 0) < 1800):
        idle = "" if age is None else t("activity.live_idle_for", dur=_dur(age))
        return {"level": "wait", "text": t("activity.live_maybe_waiting", phase=st.get("phase"), idle=idle, where=where)}
    if status == "active" and state_ts > event_ts:
        state_age = max(0, int(now - state_ts))
        event_age = max(0, int(now - event_ts)) if event_ts else None
        level = "run" if state_age < 90 else "quiet"
        hook_activity = t("activity.live_hook_last", ago=_ago(event_age)) if event_age is not None \
            else t("activity.live_hook_none")
        return {"level": level, "text": t("activity.live_phase_open",
                                          phase=st.get("phase") or t("activity.live_phase_fallback"),
                                          state_ago=_ago(state_age), hook=hook_activity)}
    if age is None:
        return {"level": "quiet", "text": t("activity.live_no_record")}
    agents = running_agents(rows)
    who = " · " + t("activity.live_running_agents", names=", ".join(short_agent(a) for a in agents)) if agents else ""
    if last and last.get("kind") == "tool" and last.get("tool") == "Bash" and age >= 60:
        return {"level": "run" if age < 1800 else "stale",
                "text": t("activity.live_command", dur=_dur(age), what=str(last.get("what") or "")[:90], who=who)}
    if age < 90:
        return {"level": "run", "text": t("activity.live_working_agent", who=who,
                                          agent=short_agent(str(last.get("agent") or "main"))) if last
                else t("activity.live_working")}
    if age < 900:
        return {"level": "quiet", "text": t("activity.live_quiet", dur=_dur(age), who=who)}
    return {"level": "stale", "text": t("activity.live_stale", dur=_dur(age), resume=resume)}


def _dur(seconds: Optional[int], seconds_below: int = 60) -> str:
    """`45s` / `3m` / `1h 5m` (`45 sn` / `3 dk` / `1 sa 5 dk`); under `seconds_below` the count stays in seconds."""
    if seconds is None:
        return "?"
    if seconds < seconds_below:
        return t("activity.dur_s", n=seconds)
    if seconds < 3600:
        return t("activity.dur_m", n=seconds // 60)
    return t("activity.dur_h", h=seconds // 3600, m=seconds % 3600 // 60)


def human_duration(seconds: Optional[float]) -> str:
    """A span for an incident headline (the watchdog): seconds up to 90, then minutes, then hours."""
    return "?" if seconds is None else _dur(int(max(0, seconds)), 90)


def _ago(seconds: Optional[int]) -> str:
    return "?" if seconds is None else t("common.ago", dur=_dur(seconds))
