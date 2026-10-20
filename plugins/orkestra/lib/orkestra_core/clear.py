"""`orkestra clear`: close runs that have been idle for a long time and drop the registry entries they leave behind.

Machine-wide (every project under the workspace root). A run is stale when it is still open (active, awaiting_user,
blocked, paused), nothing touched it for `older_than` seconds (state, activity feed, session heartbeats) and no agent
process for its sessions is alive. Stale runs are aborted through the normal status path (ledger + activity row), so
the run directory, its evidence and the project files stay untouched. Dangling index/binding files are removed too.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import activity as A
from . import paths
from . import runs as R
from . import state as ST
from .util import read_json, write_json_atomic

DEFAULT_HOURS = 24.0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _last_touch(rdir: Path, sessions: List[Dict[str, Any]]) -> float:
    return max([_mtime(rdir / "state.json"), _mtime(rdir / "activity.jsonl")] + [float(s["heartbeat"]) for s in sessions])


def _alive(session_ids: List[str]) -> bool:
    from . import watchdog as W
    return bool(session_ids) and W._process_alive(session_ids)


def scan_runs(now: float, older_than: float) -> Dict[str, List[Dict[str, Any]]]:
    """{'stale': [...], 'kept': [...]}: every open run, judged. `kept` carries why it was left alone."""
    from .monitor import _project_dirs
    stale: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    for proj in _project_dirs(paths.workspace_root()):
        rdir = paths.runs_dir(proj)
        if not rdir.is_dir():
            continue
        for d in sorted(rdir.iterdir()):
            st = read_json(d / "state.json")
            if not isinstance(st, dict) or st.get("status") not in ST.OPEN_STATUSES or not st.get("run_id"):
                continue
            rid = str(st["run_id"])
            sessions = ST.run_sessions(proj, rid)
            idle = now - _last_touch(d, sessions)
            row = {"project": str(proj), "run_id": rid, "title": st.get("title", ""), "status": st.get("status"),
                   "phase": st.get("phase"), "idle": int(idle)}
            if idle < older_than:
                kept.append(dict(row, why="recent"))
            elif _alive([s["id"] for s in sessions]):
                kept.append(dict(row, why="alive"))
            else:
                stale.append(row)
    stale.sort(key=lambda r: -r["idle"])
    return {"stale": stale, "kept": kept}


def _close_run(row: Dict[str, Any], hours: float) -> Optional[str]:
    """Abort one stale run without disturbing the project's other run. Returns an error text or None."""
    proj, rid = Path(row["project"]), row["run_id"]
    before_idx = ST.active_runs().get(str(proj))
    link = paths.okdir(proj) / "current"
    try:
        before_cur = os.readlink(link) if link.is_symlink() else None
    except OSError:
        before_cur = None
    try:
        R.set_status(proj, rid, "aborted", f"orkestra clear: idle for over {hours:g}h ({row['status']})",
                     source="clear", who={"agent_type": "cli"})
    except Exception as exc:                                # one broken run must not stop the sweep
        return f"{type(exc).__name__}: {exc}"
    if before_idx and before_idx != rid:                    # set_status clears the project index: keep the newer run's
        ST.set_active_run(proj, before_idx)
    if before_cur and before_cur != str(Path("features") / rid):
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(before_cur)
        except OSError:
            pass
    for s in ST.run_sessions(proj, rid):
        ST.unbind_session(s["id"])
    A.agents_purge(proj, rid)
    return None


def _run_open(project: Any, rid: Any) -> bool:
    if not project or not rid:
        return False
    st = read_json(ST.state_path(Path(str(project)), str(rid)))
    return isinstance(st, dict) and st.get("status") in ST.OPEN_STATUSES


def scan_dangling(now: float, older_than: float) -> List[Dict[str, Any]]:
    """Registry files that point at a run that is closed or gone: index entries, session bindings, child bindings."""
    out: List[Dict[str, Any]] = []
    for key, rid in sorted(ST.active_runs().items()):
        if not _run_open(key, rid):
            out.append({"kind": "index", "path": key, "run_id": str(rid)})
    sdir = paths.sessions_dir()
    for kind, d in (("session", sdir), ("child", ST.children_dir())):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            data = read_json(f)
            if not isinstance(data, dict):
                continue
            if _run_open(data.get("project"), data.get("run_id")) or now - _mtime(f) < older_than:
                continue
            out.append({"kind": kind, "path": str(f), "run_id": str(data.get("run_id") or "")})
    return out


def _drop_dangling(item: Dict[str, Any]) -> Optional[str]:
    try:
        if item["kind"] == "index":
            p = paths.active_runs_path()
            with ST.file_lock(p):
                data = ST.active_runs()
                if not _run_open(item["path"], data.get(item["path"])):
                    data.pop(item["path"], None)
                    write_json_atomic(p, data)
        else:
            Path(item["path"]).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def clear(older_than_hours: float = DEFAULT_HOURS, dry_run: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Close stale runs and drop dangling registry entries. dry_run only reports."""
    now = time.time() if now is None else now
    found = scan_runs(now, older_than_hours * 3600)
    closed: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for row in found["stale"]:
        if not dry_run:
            err = _close_run(row, older_than_hours)
            if err:
                errors.append({"target": f"{Path(row['project']).name}/{row['run_id']}", "error": err})
                continue
        closed.append(row)
    dangling = scan_dangling(now, older_than_hours * 3600)       # after closing: the new aborts leave bindings behind
    dropped: List[Dict[str, Any]] = []
    for item in dangling:
        if not dry_run:
            err = _drop_dangling(item)
            if err:
                errors.append({"target": item["path"], "error": err})
                continue
        dropped.append(item)
    return {"dry_run": dry_run, "older_than_hours": older_than_hours, "closed": closed, "kept": found["kept"],
            "dropped": dropped, "errors": errors}
