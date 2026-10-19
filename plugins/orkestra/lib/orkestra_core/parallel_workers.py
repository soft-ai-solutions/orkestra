"""Start disjoint tasks on their own headless CLI processes.

The session that asked `orkestra next` keeps the first task. Other pending tasks whose
files do not overlap start in the background, up to three implementers in total.
A worker cannot start further workers. No network and no real CLI in unit tests:
the binary comes from `engines.binary`.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List

from . import engines as EN
from . import paths
from . import tempo as T
from .runs import task_start
from .state import ledger, load_state
from .util import file_lock, read_json, write_json_atomic

MAX_WORKERS = 3


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _record_path(project: Path, run_id: str) -> Path:
    return paths.run_dir(project, run_id) / "parallel.json"


def _load(project: Path, run_id: str) -> Dict[str, Any]:
    data = read_json(_record_path(project, run_id))
    return data if isinstance(data, dict) else {}


def _targets(st: Dict[str, Any]) -> List[str]:
    """Disjoint pending tasks except the one this session still has to start."""
    tasks = st.get("tasks") or {}
    in_prog = sorted(tid for tid, v in tasks.items() if v.get("status") == "in_progress")
    primary = T.next_task(st)
    reserved = list(in_prog)
    if primary and primary not in reserved:
        reserved.append(primary)
    slots = MAX_WORKERS - len(in_prog) - (1 if primary and primary not in in_prog else 0)
    if slots <= 0 or not reserved:
        return []
    return T.parallel_ready(st, reserved[0], limit=slots, busy=reserved)


def worker_prompt(st: Dict[str, Any], tid: str, project_name: str) -> str:
    """One task, the skill that matches the run kind. The worker must not fan out."""
    task = (st.get("tasks") or {}).get(tid) or {}
    files = ", ".join(task.get("files") or []) or "(none listed)"
    kind = st.get("work_kind") or "software"
    if kind in ("writing", "general"):
        role = ("You are writing the run deliverable only. TDD is n/a (content check). "
                "Do not edit product source.")
    else:
        role = ("You are the implementer for this one task. Write a failing test first, "
                "then the smallest code that makes it pass.")
    return (f"Orkestra parallel worker. Own only {tid}.\n"
            f"Files: {files}\n{role}\n"
            f"Run `orkestra brief {tid} -p {project_name}` and finish that task. "
            f"Do not start another task.\n")


def _enabled() -> bool:
    return os.environ.get("ORKESTRA_PARALLEL_CLI", "1") != "0"


def launch_ready(project: Path, st: Dict[str, Any]) -> List[str]:
    """Start background CLIs for disjoint tasks. Returns human lines. Never raises for a missing binary."""
    if not _enabled():
        return []
    if st.get("phase") != "implement" or st.get("status") != "active":
        return []
    if st.get("approval_mode", "autopilot") != "autopilot":
        return []
    engine = EN.normalize(str(st.get("engine") or "claude"))
    binary = EN.binary(engine, paths.global_config())
    if not binary:
        return []
    project = Path(project)
    run_id = str(st["run_id"])
    lines: List[str] = []
    lock = paths.run_dir(project, run_id) / "parallel.lock"
    with file_lock(lock):
        record = _load(project, run_id)
        fresh = load_state(project, run_id)
        for tid in _targets(fresh):
            prev = record.get(tid) if isinstance(record.get(tid), dict) else {}
            pid = int(prev.get("pid") or 0)
            if _alive(pid):
                continue
            try:
                task_start(project, run_id, tid, agent=f"parallel:{engine}")
            except Exception as exc:
                lines.append(f"parallel: {tid} not started ({exc})")
                continue
            try:
                session = EN.new_session(engine, binary, str(project)) if engine == "cursor" else str(uuid.uuid4())
                prompt = worker_prompt(fresh, tid, project.name)
                opts: Dict[str, Any] = {"approval_mode": "autopilot"}
                if engine == "copilot":     # same profile as the main run (model, effort, context, allow-all, hooks)
                    cfg = paths.global_config()
                    opts.update(EN.copilot_resolve(cfg, selected=fresh.get("copilot_profile")))
                    opts["plugin_dir"] = EN.copilot_plugin_dir(cfg)
                spec = EN.headless_command(engine, binary, session, prompt, True, opts)
            except Exception as exc:
                lines.append(f"parallel: {tid} not started ({exc})")
                continue
            env = EN.child_env(engine)
            env["ORKESTRA_PARALLEL_CLI"] = "0"
            log = paths.run_dir(project, run_id) / f"parallel-{tid}.log"
            try:
                with open(log, "w", encoding="utf-8") as fh:
                    proc = subprocess.Popen(spec["argv"], cwd=str(project), env=env, stdin=subprocess.PIPE,
                                            stdout=fh, stderr=subprocess.STDOUT, encoding="utf-8",
                                            start_new_session=True)
                if proc.stdin is not None and spec.get("stdin") is not None:
                    proc.stdin.write(spec["stdin"])
                    proc.stdin.close()
            except Exception as exc:
                lines.append(f"parallel: {tid} not started ({exc})")
                continue
            record[tid] = {"pid": int(proc.pid), "engine": engine, "session": session}
            write_json_atomic(_record_path(project, run_id), record)
            ledger(project, run_id, f"parallel {tid} started on {engine} pid={proc.pid}")
            lines.append(f"parallel: started {tid} ({engine} pid {proc.pid})")
    return lines
