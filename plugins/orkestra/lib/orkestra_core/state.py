"""Run state (machine-owned JSON), session bindings, grants and the human ledger."""
from __future__ import annotations

import contextlib
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import i18n, paths
from .util import (LockTimeout, append_record, file_lock, now_iso, read_json, slugify, write_json_atomic)

PHASES = ["intake", "discover", "clarify", "plan", "approve",
          "implement", "verify", "review", "deliver", "done"]
OPEN_STATUSES = ("active", "awaiting_user", "blocked", "paused")
TERMINAL_STATUSES = ("done", "aborted")
SCHEMA = 1


class OrkestraError(Exception):
    """User-facing error; message is printed and exit code is 2."""


# ---------------------------------------------------------------- run ids

def next_run_id(project: Path, title: str) -> str:
    fdir = paths.features_dir(project)
    nums = []
    if fdir.is_dir():
        for p in fdir.iterdir():
            m = re.match(r"^(\d{3})-", p.name)
            if m:
                nums.append(int(m.group(1)))
    return f"{(max(nums) + 1) if nums else 1:03d}-{slugify(title)}"


# ---------------------------------------------------------------- state io

def state_path(project: Path, run_id: str) -> Path:
    return paths.run_dir(project, run_id) / "state.json"


def load_state(project: Path, run_id: str) -> Dict[str, Any]:
    path = state_path(project, run_id)
    st = read_json(path)
    if not isinstance(st, dict) and not path.exists():
        raise OrkestraError(f"run '{run_id}' not found in {project}")
    reason = _state_damage(st)
    if reason:                                  # the file is there but unusable: say so (it is not "missing")
        raise OrkestraError(i18n.t("cli.state_damaged", run_id=run_id, path=path, reason=reason,
                                   rdir=paths.run_dir(project, run_id), fdir=paths.feature_dir(project, run_id)))
    return st


def _state_damage(st: Any) -> str:
    """Why a parsed state.json cannot drive a run ('' when it can): the CLI reads these keys without a default."""
    if not isinstance(st, dict):
        return i18n.t("cli.damage_json" if st is None else "cli.damage_object")
    missing = [k for k in ("run_id", "phase", "status") if not st.get(k)]
    if missing:
        return i18n.t("cli.damage_keys", keys=", ".join(missing))
    if st["phase"] not in PHASES:
        return i18n.t("cli.damage_phase", phase=st["phase"])
    return ""


def save_state(project: Path, st: Dict[str, Any]) -> None:
    st["updated"] = now_iso()
    write_json_atomic(state_path(project, st["run_id"]), st)


class StateTxn:
    """`with StateTxn(project, run_id) as st:` — locked read-modify-write. The lock is given back on every path out
    (a failed load, any exception in the body, a failed save). A busy lock is an OrkestraError after the lock budget
    (hook 2 s, CLI 10 s), never an unlocked write and never an endless wait."""

    def __init__(self, project: Path, run_id: str):
        self.project = Path(project)
        self.run_id = run_id
        self._stack: Optional[contextlib.ExitStack] = None
        self.st: Dict[str, Any] = {}

    def __enter__(self) -> Dict[str, Any]:
        path = state_path(self.project, self.run_id)
        if not path.exists():                   # taking the lock would mkdir the run folder of a run that is gone
            raise OrkestraError(f"run '{self.run_id}' not found in {self.project}")
        stack = contextlib.ExitStack()
        try:
            try:
                stack.enter_context(file_lock(path, on_timeout="raise"))
            except LockTimeout as exc:
                raise OrkestraError(str(exc)) from None
            self.st = load_state(self.project, self.run_id)
        except BaseException:
            stack.close()                       # a failed __enter__ never reaches __exit__
            raise
        self._stack = stack
        return self.st

    def __exit__(self, exc_type, exc, tb) -> None:
        stack, self._stack = self._stack, None
        try:
            if exc_type is None:
                save_state(self.project, self.st)
        finally:
            if stack is not None:
                stack.close()


def new_state(project: Path, run_id: str, title: str, size: str, mode: str, engine: str = "claude",
              approval_mode: str = "autopilot", work_kind: str = "software") -> Dict[str, Any]:
    from .workkind import kind_of  # not at import time: a quiet hook loads state and must stay light
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "title": title,
        "project": str(project),
        "created": now_iso(),
        "updated": now_iso(),
        "phase": "intake",
        "status": "active",
        "size": size,
        "work_kind": kind_of({"work_kind": work_kind}),
        "mode": mode,
        "approval_mode": approval_mode,
        "engine": engine,               # which agent started it; any engine may resume it
        "history": [{"at": now_iso(), "from": None, "to": "intake", "note": "run created"}],
        "tasks": {},
        "approved": None,
        "baseline": {},
        "gates": {},
        "awaiting": None,
        "blocked": None,
        "stop_blocks": {},
        "subagent_stop_blocks": {},
    }


# ---------------------------------------------------------------- active runs index

def active_runs() -> Dict[str, str]:
    data = read_json(paths.active_runs_path(), default={}) or {}
    return data if isinstance(data, dict) else {}


def set_active_run(project: Path, run_id: Optional[str]) -> None:
    p = paths.active_runs_path()
    with file_lock(p):
        data = active_runs()
        key = str(Path(project))
        if run_id:
            data[key] = run_id
        else:
            data.pop(key, None)
        write_json_atomic(p, data)


def project_active_run(project: Path) -> Optional[str]:
    key = str(Path(project))
    rid = active_runs().get(key)
    if rid and state_path(project, rid).exists():
        st = read_json(state_path(project, rid))
        # Unreadable JSON stays indexed: read_json gives None, a list, or a string, and the check is "not a dict".
        if not isinstance(st, dict) or st.get("status") not in TERMINAL_STATUSES:
            return rid
        # Same lock shape as set_active_run. Do not call it here: flock does not nest, and a newer id must survive.
        p = paths.active_runs_path()
        with file_lock(p):
            data = active_runs()
            if data.get(key) == rid:
                data.pop(key, None)
                write_json_atomic(p, data)
    # fall back to scanning runs/ for an open run (index may be stale or from another machine)
    rdir = paths.runs_dir(project)
    if rdir.is_dir():
        cands = []
        for p in rdir.iterdir():
            st = read_json(p / "state.json")
            if isinstance(st, dict) and st.get("status") in OPEN_STATUSES:
                open_id = st.get("run_id")
                updated = st.get("updated")
                # A missing id cannot be resumed. A non-string updated must not crash the sort
                # (a null next to an ISO string raises TypeError and blocks every later start).
                if isinstance(open_id, str) and open_id:
                    cands.append((updated if isinstance(updated, str) else "", open_id))
        if cands:
            return sorted(cands)[-1][1]
    return None


# ---------------------------------------------------------------- session bindings

def _clear_children_for_parent(parent_session: str, only_other_project: Optional[Path] = None) -> int:
    """Drop child bindings for a parent chat. If only_other_project is set, drop children whose project differs."""
    if not parent_session:
        return 0
    removed = 0
    want = None
    if only_other_project is not None:
        try:
            want = Path(only_other_project).resolve()
        except OSError:
            want = Path(only_other_project)
    try:
        kids = list(children_dir().glob("*.json"))
    except OSError:
        return 0
    for path in kids:
        data = read_json(path)
        if not isinstance(data, dict) or data.get("parent") != parent_session:
            continue
        if want is not None:
            try:
                child_proj = Path(str(data.get("project") or "")).resolve()
            except OSError:
                child_proj = Path(str(data.get("project") or ""))
            if child_proj == want:
                continue
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


DRIVEN_STATUSES = ("active", "awaiting_user", "blocked")   # a chat can be driving these; paused and closed runs hold no chat


def _is_abs(path: Any) -> bool:
    """Absolute on POSIX or Windows (tests simulate Windows paths on POSIX)."""
    from pathlib import PureWindowsPath
    text = str(path or "")
    return bool(text) and (Path(text).is_absolute() or PureWindowsPath(text).is_absolute())


def bind_problem(project: Path, run_id: str) -> str:
    """Why this (project, run) can never be bound ('' when it can): a relative path, an unknown run, a closed run."""
    if not run_id or not re.fullmatch(r"[A-Za-z0-9][\w.-]*", run_id) or ".." in run_id:
        return "no usable run id"                  # an id names a folder under .orkestra/runs: no separators, no ..
    if not _is_abs(project):
        return "the project path is not absolute"
    st = read_json(state_path(Path(project), run_id))
    if not isinstance(st, dict):
        return f"run '{run_id}' does not exist in {project}"
    if st.get("status") in TERMINAL_STATUSES:
        return f"run '{run_id}' is {st.get('status')}"
    return ""


def bind_session(session_id: str, project: Path, run_id: str, engine: str = "") -> bool:
    """Bind a chat to a run. Validates first (a bogus binding would govern the chat with a run that cannot exist);
    False = nothing was written. Use `try_bind` where a live binding of the chat must not be replaced."""
    if not session_id or bind_problem(project, run_id):
        return False
    path = paths.sessions_dir() / f"{_safe(session_id)}.json"
    prev = read_json(path)
    # One Cursor chat must not drive two projects: drop foreign child roles when rebinding.
    if isinstance(prev, dict) and prev.get("project") and str(prev.get("project")) != str(project):
        _clear_children_for_parent(session_id)
    else:
        _clear_children_for_parent(session_id, only_other_project=Path(project))
    data = {"project": str(project), "run_id": run_id, "bound_at": now_iso()}
    if engine:
        data["engine"] = engine                     # which agent drives from this session (claude when absent)
    write_json_atomic(path, data)
    _prune(paths.sessions_dir(), days=21)
    return True


def _same_run(binding: Dict[str, Any], project: Path, run_id: str) -> bool:
    if binding.get("run_id") != run_id:
        return False
    a, b = str(binding.get("project") or ""), str(project)
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def binding_record(session_id: str) -> Optional[Dict[str, Any]]:
    """The binding file as written (also when its run is closed: `session_binding` hides that case)."""
    data = read_json(paths.sessions_dir() / f"{_safe(session_id)}.json") if session_id else None
    return data if isinstance(data, dict) else None


def binding_age(session_id: str) -> Optional[float]:
    """Seconds since this chat last produced a hook event (the binding file's mtime); None without a binding."""
    try:
        return max(0.0, time.time() - (paths.sessions_dir() / f"{_safe(session_id)}.json").stat().st_mtime)
    except OSError:
        return None


def try_bind(session_id: str, project: Path, run_id: str, engine: str = "", lease: float = 120.0) -> Dict[str, Any]:
    """Bind a chat to a run unless it is still driving ANOTHER one. One rule for every engine (the Claude Code and
    Copilot hooks, the CLI in a Cursor shell). Returns {"result": ..., ...}:

      bound    written (or the same run again)
      kept     the chat's binding is to a different run that is still driven (open, not paused) and the chat was heard
               from less than `lease` seconds ago: nothing changed. "prev" = that binding, "age" = seconds since the
               last event, "duplicate" = this very refusal was already reported (report it once, not per marker)
      refused  the pair cannot be bound at all ("why")"""
    why = "no session id" if not session_id else bind_problem(project, run_id)
    if why:
        return {"result": "refused", "why": why}
    cur = session_binding(session_id)
    if cur and not _same_run(cur, project, run_id) and (cur.get("state") or {}).get("status") in DRIVEN_STATUSES:
        age = binding_age(session_id)
        if age is not None and age < lease:
            seen = cur.get("refused") if isinstance(cur.get("refused"), dict) else {}
            duplicate = (seen.get("run") == run_id and seen.get("project") == str(project)
                         and time.time() - float(seen.get("ts") or 0) < 300)
            if not duplicate:
                note = {k: v for k, v in cur.items() if k != "state"}
                note["refused"] = {"run": run_id, "project": str(project), "ts": time.time()}
                write_json_atomic(paths.sessions_dir() / f"{_safe(session_id)}.json", note)
            return {"result": "kept", "prev": cur, "age": int(age), "duplicate": duplicate}
    if not bind_session(session_id, project, run_id, engine=engine):
        return {"result": "refused", "why": "could not bind"}
    return {"result": "bound"}


def heal_binding(session_id: str) -> str:
    """Drop a binding that can never be right again (called when a hook sees the chat). Returns why, '' = untouched.

    Dropped: a malformed or relative project path, a project folder that is gone, a run that does not exist and a run
    that is closed (the last one leaves the `known` stub, as at the end of a run). A paused run keeps its binding: the
    Stop hook releases it at the end of the turn. A damaged state.json is not 'missing': it is left for `doctor`."""
    if not session_id:
        return ""
    data = read_json(paths.sessions_dir() / f"{_safe(session_id)}.json")
    if not isinstance(data, dict):
        return ""
    project, rid = str(data.get("project") or ""), str(data.get("run_id") or "")
    if not rid or not _is_abs(project):
        why = "the binding has no absolute project path or run id"
    elif not Path(project).is_dir():
        why = f"project folder {project} is gone"
    elif not state_path(Path(project), rid).exists():
        why = f"run '{rid}' does not exist in {project}"
    else:
        st = read_json(state_path(Path(project), rid))
        if not (isinstance(st, dict) and st.get("status") in TERMINAL_STATUSES):
            return ""
        why = f"run '{rid}' is {st.get('status')}"
    unbind_session(session_id)
    return why


# ---------------------------------------------------------------- child conversations (Cursor subagents)
# Claude Code reports a subagent's tool calls under the PARENT session id plus agent_type. Cursor gives every
# subagent its own conversation id and no agent_type. The Cursor hook adapter resolves such a conversation to its
# parent once and remembers it here — outside sessions/*.json, so a child is never taken for a session that drives
# the run (run_sessions, watchdog resume target, takeover warning).

def children_dir() -> Path:
    return paths.sessions_dir() / "children"


def bind_child(conversation_id: str, parent_session: str, project: Path, run_id: str, agent_type: str) -> Dict[str, Any]:
    data = {"project": str(project), "run_id": run_id, "parent": parent_session, "agent_type": agent_type,
            "bound_at": now_iso()}
    # Refuse cross-project child: parent chat is bound to a different project (multi-root leakage).
    parent_b = session_binding(parent_session) if parent_session else None
    if parent_b:
        try:
            same = Path(parent_b["project"]).resolve() == Path(project).resolve()
        except OSError:
            same = str(parent_b.get("project")) == str(project)
        if not same:
            return {"parent": "", "refused": True, "agent_type": agent_type,
                    "project": str(project), "run_id": run_id}
    if conversation_id:
        write_json_atomic(children_dir() / f"{_safe(conversation_id)}.json", data)
        _prune(children_dir(), days=7)
    return data


def child_binding(conversation_id: str) -> Optional[Dict[str, Any]]:
    if not conversation_id:
        return None
    data = read_json(children_dir() / f"{_safe(conversation_id)}.json")
    return data if isinstance(data, dict) and data.get("parent") else None


def unbind_session(session_id: str) -> None:
    """Drop the active binding, but remember the conversation drove a run.

    After abort / done / pause the chat must not receive Stop follow-ups, yet every-session
    guards (git, secrets) still apply — Cursor's `_known` looks for this stub.
    """
    path = paths.sessions_dir() / f"{_safe(session_id)}.json"
    data = read_json(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _clear_children_for_parent(session_id)
    if isinstance(data, dict) and session_id:
        known = paths.sessions_dir() / "known"
        known.mkdir(parents=True, exist_ok=True)
        write_json_atomic(known / f"{_safe(session_id)}.json", {
            "project": data.get("project"), "run_id": data.get("run_id"),
            "engine": data.get("engine"), "unbound_at": now_iso(),
        })
        _prune(known, days=21)


def session_binding(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    data = read_json(paths.sessions_dir() / f"{_safe(session_id)}.json")
    if not isinstance(data, dict):
        return None
    if not _is_abs(data.get("project")):
        return None                     # a relative path means "wherever this hook happens to run": never a binding
    project = Path(data.get("project", ""))
    rid = data.get("run_id", "")
    st = read_json(state_path(project, rid)) if rid else None
    if not isinstance(st, dict) or st.get("status") in TERMINAL_STATUSES:
        return None
    data["state"] = st
    return data


def touch_session(session_id: str) -> None:
    """Heartbeat: the binding file's mtime is the last time this session produced a hook event."""
    if not session_id:
        return
    try:
        os.utime(paths.sessions_dir() / f"{_safe(session_id)}.json", None)
    except OSError:
        pass


def run_sessions(project: Path, run_id: str) -> List[Dict[str, Any]]:
    """Sessions bound to a run, newest heartbeat first: [{id, bound_at, heartbeat (epoch), age (s)}]."""
    out: List[Dict[str, Any]] = []
    d = paths.sessions_dir()
    if not d.is_dir():
        return out
    now = time.time()
    for f in d.glob("*.json"):
        data = read_json(f)
        if isinstance(data, dict) and data.get("run_id") == run_id and Path(data.get("project", "")) == Path(project):
            try:
                hb = f.stat().st_mtime
            except OSError:
                continue
            out.append({"id": f.stem, "bound_at": data.get("bound_at", ""), "heartbeat": hb, "age": int(now - hb)})
    return sorted(out, key=lambda x: -x["heartbeat"])


# ---------------------------------------------------------------- who asked for a status change
# The CLI cannot see which chat typed `orkestra pause`: only the PreToolUse hook can. The hook leaves a short-lived
# note per run; `runs.set_status` takes it and names the chat in the activity row.

def _actor_path(project: Path, run_id: str) -> Path:
    import hashlib
    key = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:10]
    return paths.orkestra_home() / "actors" / f"{key}-{_safe(run_id)}.json"


def note_actor(project: Path, run_id: str, session_id: str, agent_type: str = "", agent_id: str = "") -> None:
    try:
        write_json_atomic(_actor_path(project, run_id), {"session_id": session_id, "agent_type": agent_type,
                                                        "agent_id": agent_id, "ts": time.time()})
        _prune(paths.orkestra_home() / "actors", days=1)
    except OSError:
        pass


def take_actor(project: Path, run_id: str, max_age: float = 120.0) -> Dict[str, Any]:
    """The chat that announced a status change of this run a moment ago ({} when nobody did). Read once."""
    path = _actor_path(project, run_id)
    data = read_json(path)
    try:
        path.unlink()
    except OSError:
        pass
    if isinstance(data, dict) and time.time() - float(data.get("ts") or 0) <= max_age:
        return data
    return {}


# ---------------------------------------------------------------- grants (git intent per prompt)

def write_grant(session_id: str, prompt_id: Optional[str], categories: List[str], prompt_excerpt: str) -> None:
    if not session_id:
        return
    write_json_atomic(paths.grants_dir() / f"{_safe(session_id)}.json", {
        "prompt_id": prompt_id, "categories": sorted(set(categories)),
        "at": now_iso(), "ts": time.time(), "excerpt": prompt_excerpt[:200],
    })
    _prune(paths.grants_dir(), days=2)


def clear_grant(session_id: str) -> None:
    try:
        (paths.grants_dir() / f"{_safe(session_id)}.json").unlink()
    except FileNotFoundError:
        pass


def current_grant(session_id: str, prompt_id: Optional[str]) -> List[str]:
    data = read_json(paths.grants_dir() / f"{_safe(session_id)}.json") if session_id else None
    if not isinstance(data, dict):
        return []
    if prompt_id and data.get("prompt_id"):
        return list(data.get("categories") or []) if data["prompt_id"] == prompt_id else []
    # no prompt ids available: accept grants younger than 30 minutes
    if time.time() - float(data.get("ts", 0)) < 1800:
        return list(data.get("categories") or [])
    return []


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:120]


def _prune(directory: Path, days: int) -> None:
    try:
        cutoff = time.time() - days * 86400
        for p in directory.glob("*.json"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------- ledger (human readable)

def ledger(project: Path, run_id: str, line: str) -> None:
    path = paths.feature_dir(project, run_id) / "ledger.md"
    header = f"# Ledger — {run_id}\n\nAppend-only event log written by the orkestra CLI.\n\n"
    stamp = now_iso().replace("T", " ")[:16]
    append_record(path, f"- {stamp} {line}\n".encode("utf-8"), header.encode("utf-8"))      # one O_APPEND write


def bind_marker(project: Path, run_id: str) -> str:
    """Machine-readable line parsed by the PostToolUse hook to bind the session to a run. `home` names the
    ORKESTRA_HOME of the CLI that printed it: the hook of another home (a sandbox run) ignores the line."""
    return f"[orkestra:bind] project={project} run={run_id} home={paths.orkestra_home()}"


def unbind_marker(project: Path, run_id: str) -> str:
    return f"[orkestra:unbind] project={project} run={run_id} home={paths.orkestra_home()}"


def cwd_project_from_env() -> Optional[Path]:
    env = os.environ.get("ORKESTRA_PROJECT")
    if env:
        return Path(env).expanduser().resolve(strict=False)
    return paths.find_project_root(os.getcwd())
