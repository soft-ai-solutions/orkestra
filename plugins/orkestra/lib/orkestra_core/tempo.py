"""Tempo: where a run stands, what the single next step is, how long things take, and whether it is stalled.

Read-only and derived from files that already exist (state.json, evidence, review files, activity.jsonl,
agents.json) — there is no second source of truth. `orkestra next`, `orkestra tempo`, `orkestra watch`,
`orkestra watchdog` and the hooks all call the same functions, so a run interrupted at any sub-step resumes with the
same deterministic answer: nothing is skipped, nothing is done twice.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import activity as A
from . import i18n
from . import intent
from . import paths
from . import planparse as PP
from .messages import protocol as PR
from .state import PHASES
from .util import read_json, read_text, write_json_atomic
from .workkind import kind_of

# thresholds live in ~/.orkestra/config.json → "watchdog" (defaults: paths.DEFAULT_GLOBAL_CONFIG)
WD_DEFAULTS: Dict[str, Any] = {"dead_wait_seconds": 90, "hung_command_seconds": 900, "gate_command_seconds": 1800,
                               "silent_seconds": 600, "user_wait_notify_seconds": 300, "loop_repeats": 3,
                               "agent_max_minutes": 60, "agent_silent_seconds": 150, "health_check_seconds": 120}
MAX_REVIEW_ROUNDS = 3                    # the 3rd FAIL goes to the user (manual) / to defer or block (autopilot)
MAX_DISPATCHES = 3                       # identical dispatches without new evidence before `next` escalates (tempo.max_dispatches)
DUE_REPORT_SECONDS = 60                  # a stopped subagent's report that has not arrived after this long is a dead wait
WAIT_CMD_RE = re.compile(r"\bsleep\s+\d|time\.sleep\(|while\s+time\.time\(\)|\borkestra\s+wait\b")
OPEN_TASK = ("pending", "in_progress", "blocked")


def wd_cfg(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(WD_DEFAULTS)
    user = ((cfg if cfg is not None else paths.global_config()).get("watchdog") or {})
    out.update({k: v for k, v in user.items() if v is not None})
    return out


def _ts(iso: Any) -> Optional[float]:
    try:
        return _dt.datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def compact_dur(seconds: Optional[float]) -> str:
    """The short duration a person reads: `45s` / `45 sn`, `3m` / `3 dk`, `2h` / `2 sa`, `2h 5m` / `2 sa 5 dk`.
    A zero unit is left off, so an hour is not written as `1h 0m`."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s < 60:
        return i18n.t("tempo.dur_s", n=s)
    if s < 3600:
        return i18n.t("tempo.dur_m", n=s // 60)
    h, m = s // 3600, s % 3600 // 60
    if h < 24:
        return i18n.t("tempo.dur_hm", h=h, m=m) if m else i18n.t("monitor.dur_h", h=h)
    d, h = h // 24, h % 24
    return i18n.t("monitor.dur_dh", d=d, h=h) if h else i18n.t("monitor.dur_d", d=d)


def token_mark(n: Optional[int]) -> str:
    """` (10k)` / ` (1.5k)` / ` (1.2m)` / ` (842)`. Empty when the number was not measured."""
    if n is None:
        return ""
    n = int(n)
    if n < 0:
        n = 0
    if n < 1000:
        return f" ({n})"
    if n < 1_000_000:
        whole, frac = divmod(n, 1000)
        tenth = (frac + 50) // 100
        if tenth == 10:
            whole, tenth = whole + 1, 0
        return f" ({whole}k)" if tenth == 0 else f" ({whole}.{tenth}k)"
    whole, frac = divmod(n, 1_000_000)
    tenth = (frac + 50_000) // 100_000
    if tenth == 10:
        whole, tenth = whole + 1, 0
    return f" ({whole}m)" if tenth == 0 else f" ({whole}.{tenth}m)"


def stage_text(label: str, seconds: Optional[float], tokens: Optional[int] = None) -> str:
    """`discovery: 3m (10k)`. The token mark is omitted when that phase has no measured usage."""
    return f"{label}: {compact_dur(seconds)}{token_mark(tokens)}"


def clock(seconds: Optional[float]) -> str:
    """Hours:minutes:seconds for a person watching a run. Minutes and seconds are two digits. Hours are not padded
    and keep growing past 24, so a long run does not lose a day."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    hours, rest = divmod(s, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def dur_tr(seconds: Optional[float]) -> str:
    """Duration for a human, in the current language (the name is historical: it began as the Turkish formatter,
    `45 sn` · `12 dk` · `2 sa 5 dk`; English reads `45s` · `12m` · `2h 5m`). `dur` stays the fixed form the model reads."""
    return i18n.duration(seconds)


# ---------------------------------------------------------------- per-task sub-step (derived, never stored)

def reopen_count(t: Dict[str, Any]) -> int:
    """Review rounds spent: reopens by a review FAIL. A red-evidence-missing reopen is bookkeeping, not a round."""
    return sum(1 for n in (t.get("notes") or []) if str(n).startswith("reopened") and not str(n).startswith(RED_MISSING))


RED_MISSING = "reopened(red-missing)"
CAP_BLOCK = "blocked: review round limit"      # note of a task parked by the round cap in autopilot


def is_autopilot(st: Dict[str, Any]) -> bool:
    return st.get("approval_mode", "autopilot") == "autopilot"


def p_arg(project: Path) -> str:
    """`-p <name>` when the folder sits directly in the workspace (the CLI resolves a bare name there), else the
    absolute path: shorter hints, same meaning."""
    try:
        name = Path(project).name
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name) and \
                (paths.workspace_root() / name).resolve(strict=False) == Path(project).resolve(strict=False):
            return f"-p {name}"
    except Exception:
        pass
    return f"-p {project}"


def once_per_run(project: Path, run_id: str, key: str) -> bool:
    """True the first time `key` is asked for in this run, False afterwards (prefaces the model must read once, not on
    every call). A marker file in the run folder, so the state file's mtime (a liveness signal) is left alone."""
    try:
        path = paths.run_dir(project, run_id) / "prefaces.json"
        seen = read_json(path, default={}) or {}
        if not isinstance(seen, dict):
            seen = {}
        if key in seen:
            return False
        seen[key] = time.time()
        write_json_atomic(path, seen)
        return True
    except Exception:
        return True                       # cannot remember: say it again rather than drop it


def task_review(fdir: Path, tid: str) -> Dict[str, Any]:
    """Newest review file of a task (reviews/T001.review.md, reviews/T001.md, reviews/task-T001-review.md …)."""
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(tid)}(?![A-Za-z0-9])", re.I)
    best: Optional[Tuple[float, Path]] = None
    rdir = fdir / "reviews"
    if rdir.is_dir():
        for p in rdir.glob("*.md"):
            if pat.search(p.stem):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if best is None or m > best[0]:
                    best = (m, p)
    if best is None:
        return {}
    text = read_text(best[1])
    return {"path": str(best[1]), "name": best[1].name, "verdict": PP.verdict(text), "mtime": best[0],
            "minors": len(re.findall(r"^\s*\|[^\n]*\|\s*minor\s*\|", text, re.M | re.I))}


def task_step(project: Path, st: Dict[str, Any], tid: str, deep: bool = False) -> Dict[str, Any]:
    """started · red · green_no_red · green_stale · review_pending · review_fail_rN · review_pass · done …"""
    t = (st.get("tasks") or {}).get(tid) or {}
    fdir = paths.feature_dir(project, st["run_id"])
    status = t.get("status") or "pending"
    rounds = reopen_count(t)
    out: Dict[str, Any] = {"task": tid, "status": status, "round": rounds + 1, "step": status}
    if status != "in_progress":
        if status == "pending" and rounds:
            out["step"] = "reopened"
        return out
    tdd = t.get("tdd") == "required"
    red_ok = bool((t.get("red") or {}).get("ok"))
    green = t.get("green") or {}
    green_ok = bool(green.get("ok"))
    notes = t.get("notes") or []
    if notes and str(notes[-1]).upper().startswith("BLOCKED") and not (tdd and green_ok):
        out.update(step="blocked_note", note=str(notes[-1])[:160])     # the implementer gave up: a decision is needed
        return out
    if tdd and not green_ok:
        out["step"] = "red" if red_ok else "started"
        return out
    if tdd and not red_ok:
        out["step"] = "green_no_red"
        return out
    if not tdd:
        # the report must belong to THIS round: after a reopen the round-1 report is still on disk
        try:
            fresh = (fdir / "tasks" / f"{tid}.report.md").stat().st_mtime >= (_ts(t.get("started")) or 0.0)
        except OSError:
            fresh = False
        if not fresh:
            out["step"] = "started"
            return out
    green_at = _ts(green.get("at")) or _ts(t.get("started")) or 0.0
    if deep and tdd:
        for rel in t.get("files") or []:
            try:
                if (project / rel).stat().st_mtime > green_at + 2:
                    out.update(step="green_stale", file=rel, cmd=green.get("cmd"))
                    return out
            except OSError:
                continue
    rv = task_review(fdir, tid)
    out["review"] = rv.get("name")
    # a review already applied by `task reopen` (review_seen) belongs to an earlier round
    if not rv or rv.get("verdict") is None or float(rv.get("mtime") or 0) <= float(t.get("review_seen") or 0):
        out["step"] = "review_pending"
    elif rv["verdict"] in ("PASS", "PASS_WITH_NOTES"):       # the same two verdicts `task reopen` treats as a pass
        out.update(step="review_pass", minors=rv.get("minors") or 0)
    elif rv["verdict"] == "FAIL":
        out["step"] = f"review_fail_r{rounds + 1}"
    else:
        out.update(step="review_unclear", verdict=rv["verdict"])
    return out


def parallel_ready(st: Dict[str, Any], primary: str, limit: int = 2, busy: Optional[List[str]] = None) -> List[str]:
    """Other pending tasks that can start with `primary`: dependencies done, files disjoint, at most `limit`."""
    tasks = st.get("tasks") or {}
    chosen = list(busy) if busy else [primary]
    if primary not in chosen:
        chosen.append(primary)
    extras: List[str] = []
    for tid in sorted(tasks):
        if tid == primary or len(extras) >= limit:
            continue
        t = tasks[tid]
        if t.get("status") != "pending":
            continue
        if not all((tasks.get(d) or {}).get("status") in ("done", "deferred") for d in (t.get("depends") or [])):
            continue
        if overlap(tasks, tid, chosen):
            continue
        extras.append(tid)
        chosen.append(tid)
    return extras


def next_task(st: Dict[str, Any]) -> Optional[str]:
    tasks = st.get("tasks") or {}
    for tid in sorted(tasks):
        t = tasks[tid]
        if t.get("status") == "pending" and all((tasks.get(d) or {}).get("status") in ("done", "deferred")
                                                for d in t.get("depends") or []):
            return tid
    return None


# ---------------------------------------------------------------- background subagents: running or lost?

def agents_quiet(rows: List[Dict[str, Any]], pending: List[Dict[str, Any]], now: float,
                 wd: Dict[str, Any], key: str = "silent_seconds", life: float = 0.0) -> Optional[float]:
    """Seconds since any subagent showed life, when that is longer than the `key` threshold — else None.

    A working subagent writes a row for every tool call. Registered agents with no row and no open command for that
    long are lost (killed, or their session crashed): waiting for them would park the run. `key` is
    watchdog.silent_seconds for the stall classification (600 s) and watchdog.agent_silent_seconds for the hooks,
    the wait guard and `next` (150 s); `life` is any other sign of life (a subagent transcript still being written)."""
    if not pending:
        return None
    if any(agent != "main" for agent, _, _ in _running_commands(rows)):
        return None
    last = max([float(a.get("at") or 0) for a in pending]
               + [float(r.get("ts") or 0) for r in rows if (r.get("agent") or "main") != "main"] + [float(life or 0)])
    gap = now - last
    default = 150 if key == "agent_silent_seconds" else 600
    return gap if gap > float(wd.get(key) or default) else None


def transcript_life(pending: List[Dict[str, Any]]) -> float:
    """Newest write time of a registered subagent's own transcript (best effort, 0.0 = unknown). A thinking subagent
    writes no tool-call rows but still writes its transcript, so it is not declared lost."""
    best = 0.0
    try:
        from . import usage as _U
        for a in pending:
            sid, aid = str(a.get("sid") or ""), str(a.get("id") or "")
            if not sid or not aid or aid.startswith("?"):
                continue
            for path, is_main in _U._locate(sid):
                if not is_main and aid in Path(path).stem:
                    try:
                        best = max(best, Path(path).stat().st_mtime)
                    except OSError:
                        continue
    except Exception:
        return 0.0
    return best


def live_agents(project: Path, run_id: str, now: Optional[float] = None, wd: Optional[Dict[str, Any]] = None,
                rows: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(running, lost) — the registered subagents, split by sign of life. The activity log is read only when
    something is registered at all."""
    now = time.time() if now is None else now
    wd = wd or wd_cfg()
    pending = A.agents_pending(project, run_id, max_age=float(wd["agent_max_minutes"]) * 60, now=now)
    if not pending:
        return [], []
    rows = A.read_rows(project, run_id) if rows is None else rows
    if agents_quiet(rows, pending, now, wd, "agent_silent_seconds") is None:
        return pending, []
    if agents_quiet(rows, pending, now, wd, "agent_silent_seconds", transcript_life(pending)) is None:
        return pending, []
    return [], pending


# ---------------------------------------------------------------- the single next step

def _n(action: str, text: str, why: str = "", cmd: str = "", **extra: Any) -> Dict[str, Any]:
    d = {"action": action, "text": text, "why": why, "cmd": cmd}
    d.update(extra)
    return d


def max_dispatches() -> int:
    try:
        return max(1, int((paths.global_config().get("tempo") or {}).get("max_dispatches") or MAX_DISPATCHES))
    except (TypeError, ValueError):
        return MAX_DISPATCHES


def dispatches_since(project: Path, run_id: str, kinds: Tuple[str, ...], since: float, tid: str = "",
                     untasked: bool = False) -> int:
    """How many launches of these agent kinds (short names: `implementer`, `reviewer` …) were recorded after `since`
    (for one task when `tid` is given). Foreground and background launches alike, from the launch ring."""
    n = 0
    for x in A.launches(project, run_id):
        if float(x.get("at") or 0) <= since or A.short_agent(str(x.get("type") or "")) not in kinds:
            continue
        if tid and tid not in (x.get("tasks") or []):
            continue
        if untasked and x.get("tasks"):
            continue                                  # a task-scoped launch (a FIX review) is not the final review
        n += 1
    return n


def _task_since(t: Dict[str, Any], *extra: Any) -> float:
    """Start of the window in which dispatches count: the task's (re)start, its newest red/green evidence, and any
    extra timestamp (a review file). New evidence or a reopen therefore restarts the count."""
    marks = [_ts(t.get("started")) or 0.0, _ts((t.get("red") or {}).get("at")) or 0.0,
             _ts((t.get("green") or {}).get("at")) or 0.0, float(t.get("last_reopen_at") or 0.0)]
    marks += [float(x or 0.0) for x in extra]
    return max(marks)


def _escalate(st: Dict[str, Any], what: str, n: int, block_cmd: str, base: Dict[str, Any]) -> Dict[str, Any]:
    """The same dispatch has been made `n` times with nothing to show for it: stop repeating it."""
    auto = is_autopilot(st)
    tail = ("pick a materially different approach, `orkestra task defer` it (the final review triages it) or "
            "`orkestra block \"<why>\"`" if auto else "choose a different approach with the user, defer it or `orkestra block`")
    return _n("escalate", f"{what} dispatched {n}x with no new evidence → do NOT dispatch it again: `{block_cmd}`, then {tail}",
              f"{n} identical dispatches without progress (tempo.max_dispatches)", block_cmd, **base)


def _phase_loop(project: Path, st: Dict[str, Any], phase: str, kinds: Tuple[str, ...], limit: int, what: str,
                why: str, p: str) -> Optional[Dict[str, Any]]:
    """A phase-level agent (scouts, architect, critic, verifier, final review) launched `limit` times since the run
    entered this phase and the artifact it should have produced is still missing: escalate instead of dispatching again."""
    entered = max([_ts(h.get("at")) or 0.0 for h in (st.get("history") or []) if h.get("to") == phase] or [0.0])
    n = dispatches_since(project, st["run_id"], kinds, entered, untasked=(phase == "review"))
    if n < limit:
        return None
    return _escalate(st, f"{what}", n, f"orkestra block \"{why}\" {p}", {"phase_agent": "/".join(kinds)})


def _task_action(project: Path, st: Dict[str, Any], tid: str, deep: bool) -> Dict[str, Any]:
    s = task_step(project, st, tid, deep)
    step, p = s["step"], p_arg(project)
    small = st.get("size") in ("XS", "S")
    base = {"task": tid, "step": step}
    auto = is_autopilot(st)
    t = (st.get("tasks") or {}).get(tid) or {}
    limit = max_dispatches()
    exit_cmds = f"`orkestra task defer {tid} \"<why>\" {p}` (the final review triages it) or `orkestra block \"<why>\" {p}`"
    if step in ("started", "red"):
        n = dispatches_since(project, st["run_id"], ("implementer",), _task_since(t), tid)
        if n >= limit:
            return _escalate(st, f"{tid}: implementer", n, f"orkestra task block {tid} \"no progress\" {p}", base)
    if step == "review_pending" and not small:
        n = dispatches_since(project, st["run_id"], ("reviewer",), _task_since(t), tid)
        if n >= limit:
            return _escalate(st, f"{tid}: reviewer", n, f"orkestra task block {tid} \"no review written\" {p}", base)
    if step == "started":
        return _n("dispatch", f"{tid}: no evidence yet → dispatch orkestra:implementer with the output of `orkestra brief {tid} {p}` "
                              f"(same brief as before — do not start another task for it)",
                  "task is in progress, no red/green recorded", f"orkestra brief {tid} {p}", **base)
    if step == "blocked_note":
        if auto:
            return _n("work", f"{tid}: the implementer reported {s.get('note')} → autopilot: decide from spec/context with the "
                              f"most conservative option, record it with `orkestra task note {tid} \"decision: …\" {p}` and "
                              f"dispatch the implementer again; with no safe default: {exit_cmds}",
                      "the task's last note is BLOCKED — sending the same brief again would fail the same way", **base)
        return _n("ask_user", f"{tid}: the implementer reported {s.get('note')} → decide from spec/context or ask the user; "
                              f"record it with `orkestra task note {tid} \"decision: …\" {p}` and dispatch the implementer "
                              f"again with that decision (or `orkestra task block {tid} \"<why>\" {p}`)",
                  "the task's last note is BLOCKED — sending the same brief again would fail the same way", **base)
    if step == "review_unclear":
        return _n("dispatch", f"{tid}: {s.get('review')} ends with VERDICT: {s.get('verdict')} — only PASS, PASS_WITH_NOTES "
                              f"or FAIL count → ask the reviewer (SendMessage) to finish it with one of them",
                  "the review verdict is not one the CLI accepts", **base)
    if step == "red":
        return _n("dispatch", f"{tid}: red recorded, green missing → implementer continues from GREEN with the same brief "
                              f"(`orkestra brief {tid} {p}`); the red test must not change",
                  "failing-first evidence exists; implementation or green run is missing", f"orkestra brief {tid} {p}", **base)
    if step == "green_no_red":
        return _n("cli", f"{tid}: green without failing-first evidence → `orkestra task reopen {tid} --force \"red evidence missing\" {p}`, "
                         f"then the implementer redoes red → green", "TDD task: `task done` will refuse without red",
                  f"orkestra task reopen {tid} --force \"red evidence missing\" {p}", **base)
    if step == "green_stale":
        return _n("cli", f"{tid}: {s.get('file')} changed after the green run → re-run `orkestra tdd green {tid} {p} -- {s.get('cmd') or '<same command>'}`",
                  "green evidence is older than the code it proves", f"orkestra tdd green {tid} {p} -- {s.get('cmd') or ''}".strip(), **base)
    if step == "review_pending":
        if small:
            return _n("cli", f"{tid}: green recorded → `orkestra task done {tid} {p} \"final review covers it (XS/S)\"`",
                      "XS/S runs may skip the per-task review", f"orkestra task done {tid} {p} \"final review covers it (XS/S)\"", **base)
        return _n("dispatch", f"{tid}: green recorded, no review of this round → dispatch orkestra:reviewer (scope: task {tid}; "
                              f"it writes reviews/{tid}.review.md; start it from `orkestra brief --review {tid} {p}`)",
                  "evidence is in; the task review is the only missing step", **base)
    if step.startswith("review_fail"):
        rnd = s["round"]
        if rnd >= MAX_REVIEW_ROUNDS:
            if auto:
                return _n("cli", f"{tid}: review FAILED {rnd} times → autopilot has no user to rule on it: {exit_cmds}",
                          f"round limit ({MAX_REVIEW_ROUNDS}) reached — defer or block, never reopen", f"orkestra task defer {tid} \"review failed {rnd} rounds\" {p}", **base)
            return _n("ask_user", f"{tid}: review FAILED {rnd} times → stop; explain the trade-off to the user (AskUserQuestion), "
                                  f"then `orkestra task reopen {tid} --force \"<the user's ruling>\" {p}` or `orkestra task defer {tid} \"<why>\" {p}`",
                      f"round limit ({MAX_REVIEW_ROUNDS}) reached — the user decides", **base)
        return _n("cli", f"{tid}: review FAIL (round {rnd}) → `orkestra task reopen {tid} \"review round {rnd}\" {p}` → "
                         f"`orkestra task start {tid} {p}` → implementer with the brief + {s.get('review')}",
                  "the verdict on file is FAIL and has not been applied", f"orkestra task reopen {tid} \"review round {rnd}\" {p}", **base)
    if step == "review_pass":
        minors = int(s.get("minors") or 0)
        note = f"review: PASS ({minors} minor carried to final triage)" if minors else "review: PASS"
        return _n("cli", f"{tid}: review PASS → `orkestra task done {tid} {p} \"{note}\"`"
                         + (" — minors do NOT reopen the task; they are triaged in the final review" if minors else ""),
                  "the verdict on file is PASS and has not been applied", f"orkestra task done {tid} {p} \"{note}\"", **base)
    if step == "blocked":
        notes = (st["tasks"][tid].get("notes") or ["?"])
        if str(notes[-1]).startswith(CAP_BLOCK):
            return _n("cli", f"{tid}: review failed {reopen_count(t) + 1} rounds and autopilot has no user to rule on it → {exit_cmds}",
                      "round limit reached — defer or block, never reopen", f"orkestra task defer {tid} \"review failed\" {p}", **base)
        if auto:
            return _n("work", f"{tid} is blocked ({str(notes[-1])[:120]}) → autopilot: resolve it from spec/context with the most "
                              f"conservative option, then `orkestra task start {tid} {p}`; with no safe default: {exit_cmds}",
                      "a blocked task needs a decision", **base)
        return _n("ask_user", f"{tid} is blocked ({str(notes[-1])[:120]}) → decide from spec/context, or ask the user; then "
                              f"`orkestra task start {tid} {p}`", "a blocked task needs a decision", **base)
    extras = parallel_ready(st, tid)
    extra = ""
    if extras:
        names = " and ".join(extras)
        extra = (f" Also start {names} in this same turn: their files do not overlap "
                 f"(at most 3 implementers at once). Each still gets its own review.")
    return _n("cli", f"{tid}: `orkestra task start {tid} {p}` → `orkestra brief {tid} {p}` → dispatch orkestra:implementer"
                     + extra,
              "next task in dependency order", f"orkestra task start {tid} {p}", **base)


def _open_tasks_action(project: Path, st: Dict[str, Any], deep: bool) -> Optional[Dict[str, Any]]:
    tasks = st.get("tasks") or {}
    in_prog = sorted(t for t, v in tasks.items() if v.get("status") == "in_progress")
    if in_prog:
        act = _task_action(project, st, in_prog[0], deep)
        if len(in_prog) > 1:
            act["also"] = [task_step(project, st, t, deep) for t in in_prog[1:6]]
        return act
    nt = next_task(st)
    if nt:
        return _task_action(project, st, nt, deep)
    blocked = sorted(t for t, v in tasks.items() if v.get("status") == "blocked")
    if blocked:
        return _task_action(project, st, blocked[0], deep)
    stuck = sorted(t for t, v in tasks.items() if v.get("status") == "pending")
    if stuck:
        if is_autopilot(st):
            return _n("work", f"{stuck[0]} cannot start: its dependencies are not done/deferred → autopilot: `orkestra task defer "
                              f"{stuck[0]} \"<why>\" {p_arg(project)}` (or defer the blocking dependency); with no safe way on: "
                              f"`orkestra block \"<why>\" {p_arg(project)}`", "dependency deadlock")
        return _n("ask_user", f"{stuck[0]} cannot start: its dependencies are not done/deferred — fix the plan order or defer it",
                  "dependency deadlock")
    return None


def next_action(project: Path, st: Dict[str, Any], deep: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
    """The one thing to do now. deep=False (hooks) only reads small files; deep=True (CLI) also runs the real
    `phase next` preconditions so the answer is never 'phase next' when the CLI would refuse it."""
    now = time.time() if now is None else now
    project = Path(project)
    rid, phase, status = st["run_id"], st.get("phase", "intake"), st.get("status")
    fdir, rdir, p = paths.feature_dir(project, rid), paths.run_dir(project, rid), p_arg(project)
    head = {"run": rid, "phase": phase, "status": status}

    def done(d: Dict[str, Any]) -> Dict[str, Any]:
        d.update(head)
        return d

    if status in ("done", "aborted"):
        return done(_n("none", f"run is {status} — nothing to do"))
    if status == "paused":
        return done(_n("cli", f"run is paused → `orkestra resume {p}` (only when the user wants to continue)", "paused by the user or a topic change",
                       f"orkestra resume {p}"))
    if status == "blocked":
        return done(_n("wait_user", f"BLOCKED: {(st.get('blocked') or {}).get('reason') or '?'} — needs the user; after it is "
                                    f"resolved: `orkestra unblock {p}`", "blocked runs are never continued automatically"))
    autopilot_user_wait = st.get("approval_mode", "autopilot") == "autopilot"
    if status == "awaiting_user" and not autopilot_user_wait:
        return done(_n("wait_user", f"WAITING FOR THE USER: {(st.get('awaiting') or {}).get('question') or '?'} — never answer "
                                    f"on their behalf", "a question or approval is open"))
    pending, lost = live_agents(project, rid, now=now)
    if pending:
        who = ", ".join(f"{A.short_agent(a.get('type') or '')}"
                        + (f" {'/'.join(a.get('tasks') or [])}" if a.get("tasks") else "") + f" {dur(a.get('age'))}" for a in pending[:4])
        if paths.is_headless(st):
            # `claude -p` exits with its turn and takes its subagents with it: here the wait happens inside the turn
            return done(_n("wait_agents", f"{len(pending)} subagent(s) still running ({who}) → headless run: do NOT end the "
                                          f"turn (the process would exit and kill them). Wait inside it with ONE blocking call "
                                          f"`orkestra wait {p}` (Bash timeout 600000, no tokens while it waits); do not re-dispatch.",
                           "background subagents report back on their own", agents=pending[:8]))
        if str(st.get("engine") or "claude") in ("cursor", "copilot", "pi"):
            # These hosts return a subagent report in the same turn. Ending the turn here is an idle wait.
            return done(_n("wait_agents", f"{len(pending)} subagent(s) still running ({who}) → the report arrives in this "
                                          f"turn. Continue from it. Do not poll and do not start the same agent again.",
                           "the report comes back in this turn", agents=pending[:8]))
        return done(_n("wait_agents", f"{len(pending)} subagent(s) still running ({who}) → END YOUR TURN now; each hand-back "
                                      f"arrives as a message and resumes you. Do not poll, sleep or re-dispatch.",
                       "background subagents report back on their own", agents=pending[:8]))
    if lost:
        head["lost_agents"] = [{"type": a.get("type"), "tasks": a.get("tasks") or [], "age": a.get("age")} for a in lost[:8]]

    spec, plan = read_text(fdir / "spec.md"), read_text(fdir / "plan.md")
    act: Optional[Dict[str, Any]] = None
    if phase == "intake":
        if not PP.has_heading(read_text(fdir / "request.md"), "classification"):     # either language's heading counts
            act = _n("work", f"classify the request: append `## {i18n.heading('classification')}` to request.md "
                             "(`orkestra playbook intake`)", "request.md has no classification yet")
    elif phase == "discover":
        size = st.get("size") or "M"
        need = intent.min_context_chars(size)
        if len(read_text(fdir / "context.md").strip()) < need:
            if size == "XS":
                act = _n("work", "XS fast track: read 1–3 files yourself and write context.md "
                                 f"(≥{need} chars); skip scouts (`orkestra playbook discover`)",
                         "context.md is missing or too thin")
            elif size == "S":
                act = _n("work", "S fast track: optional 0–1 scout OR write context.md yourself "
                                 f"(≥{need} chars), then continue (`orkestra playbook discover`)",
                         "context.md is missing or too thin")
            else:
                act = _phase_loop(project, st, "discover", ("scout", "researcher"), 2 * max_dispatches(), "scouts", "scouts wrote no context", p) \
                    or _n("dispatch", "dispatch the scouts in ONE message (+ researcher if third-party APIs), then merge their notes "
                                      "into context.md (`orkestra playbook discover`)", "context.md is missing or too thin")
        elif not (rdir / "gates-baseline.json").exists():
            act = _n("cli", f"`orkestra gate baseline {p}`", "the baseline gates are not recorded", f"orkestra gate baseline {p}")
    elif phase == "clarify":
        if not spec.strip():
            if st.get("approval_mode", "autopilot") == "autopilot":
                act = _n("work", "Autopilot: infer conservative defaults from the request, codebase and project memory; record "
                                 f"each material assumption in `spec.md` → `## {i18n.heading('clarifications')}`, then continue "
                                 "without asking the user.", "spec.md does not exist")
            else:
                act = _n("work", "ask only the outcome-changing questions (AskUserQuestion, ≤ 4), then write spec.md with "
                                 f"`## {i18n.heading('clarifications')}` (`orkestra playbook clarify`)", "spec.md does not exist")
        elif not PP.has_heading(spec, "clarifications") or not PP.spec_ids(spec):
            clar = i18n.heading("clarifications")
            act = _n("work", (f"Autopilot: finish spec.md with FR-###/SC-### ids, a `## {clar}` section and "
                              "conservative assumptions; do not ask the user." if autopilot_user_wait else
                              f"finish spec.md: FR-###/SC-### ids and a `## {clar}` section"), "spec.md is incomplete")
    elif phase == "plan":
        critic = PP.verdict(read_text(fdir / "reviews" / "plan-critic.md"))
        size = st.get("size") or "M"
        if not plan.strip():
            if kind_of(st) in ("writing", "general"):
                # Content work writes the plan itself. The architect dispatch stays for software and a missing kind.
                rule = (f"Only `.orkestra/features/{rid}/deliverable.md` (new) or "
                        "`.orkestra/current/deliverable.md` (new). `TDD: n/a (content check)`.")
                if size in ("XS", "S"):
                    act = _n("work", f"{size}: write a short plan.md yourself (1–3 tasks), then `orkestra plan check` "
                                     f"(`orkestra playbook plan`). {rule}", "plan.md does not exist")
                else:
                    act = _n("work", "write plan.md yourself, then `orkestra plan check` "
                                     f"(`orkestra playbook plan`). {rule}", "plan.md does not exist")
            elif size in ("XS", "S"):
                act = _n("work", f"{size}: write a short plan.md yourself (1–3 tasks), then `orkestra plan check` "
                                 "(`orkestra playbook plan`)", "plan.md does not exist")
            else:
                act = _phase_loop(project, st, "plan", ("architect",), max_dispatches(), "orkestra:architect", "architect wrote no plan", p) \
                    or _n("dispatch", "dispatch orkestra:architect to write plan.md (`orkestra playbook plan`)", "plan.md does not exist")
        elif not intent.skips_plan_critic(size) and critic is None:
            act = _phase_loop(project, st, "plan", ("plan-critic",), max_dispatches(), "orkestra:plan-critic", "plan-critic wrote no verdict", p) \
                or _n("dispatch", "dispatch orkestra:plan-critic (it writes reviews/plan-critic.md)", "the plan has no critic verdict")
        elif not intent.skips_plan_critic(size) and critic == "FAIL":
            act = _phase_loop(project, st, "plan", ("plan-critic",), max_dispatches() + 1, "orkestra:plan-critic (still FAIL)",
                              "plan-critic keeps failing the plan", p) \
                or _n("dispatch", "plan-critic FAILED → send its blockers to the architect, then run the critic again "
                                  "(max 3 rounds, then "
                                  + ("`orkestra block \"<why>\"`: autopilot has no user to ask)" if autopilot_user_wait
                                     else "ask the user)"), "reviews/plan-critic.md ends with VERDICT: FAIL")
    elif phase == "approve":
        appr = read_json(rdir / "approval.json")
        if not isinstance(appr, dict):
            if st.get("approval_mode", "autopilot") == "autopilot":
                act = _n("cli", f"Autopilot: the plan and critic passed. Run `orkestra phase next {p}` to record the hash-bound "
                                 "policy approval and continue.", "no policy approval is recorded", f"orkestra phase next {p}")
            else:
                act = _n("ask_user", "show the plan summary and ask for approval — ONE AskUserQuestion "
                                     f"({PR.prompt_spec('approve', i18n.lang())}) and nothing else in that call.",
                         "no approval is recorded")
        elif appr.get("decision") != "approve":
            act = _n("work", "the user asked for changes: apply them to spec/plan, `orkestra plan check`, then ask for approval "
                             "again", f"last decision: {appr.get('decision')} ({str(appr.get('detail') or '')[:80]})")
    elif phase == "implement":
        act = _open_tasks_action(project, st, deep)
        hist = st.get("history") or []
        last = hist[-1] if hist else {}
        if act is None and str(last.get("note") or "").startswith("back:"):
            tasks_now = st.get("tasks") or {}
            mark = last.get("mark")
            if isinstance(mark, dict):          # exact: a task was added or reopened after the phase went back
                worked = len(tasks_now) > int(mark.get("tasks") or 0) \
                    or sum(reopen_count(v) for v in tasks_now.values()) > int(mark.get("reopens") or 0)
            else:                               # runs started before 1.4.0 have no mark: compare times
                back_at = _ts(last.get("at")) or 0.0
                worked = any((_ts(v.get("started")) or 0.0) > back_at or (_ts(v.get("done_at")) or 0.0) > back_at
                             for v in tasks_now.values())
            if not worked:      # came back to fix something, but no task carries the fix: `phase next` would ping-pong
                act = _n("cli", f"back in implement ({str(last.get('note'))[6:90]}) and every task is done → give the fix a "
                                f"task: `orkestra task add FIX-V001 \"<what to fix>\" {p}` (or `orkestra task reopen <TID> "
                                f"--force \"<why>\" {p}`), then start → implementer → review → done",
                         "the phase was moved back, but nothing has been reopened or added since",
                         f"orkestra task list {p}")
    elif phase == "verify":
        act = _open_tasks_action(project, st, deep)
        last = read_json(rdir / "gates-verify.json")
        ver = PP.verdict(read_text(fdir / "verification.md"))
        if act is None and not isinstance(last, dict):
            act = _n("cli", f"`orkestra gate verify {p}`", "the full gates have not run after implementation",
                     f"orkestra gate verify {p}")
        elif act is None and not (last.get("comparison") or {}).get("ok"):
            act = _n("cli", f"gate REGRESSION → `orkestra phase back implement --reason \"<gate> regressed\" {p}`, fix the cause "
                            f"(never the test), then verify again", "the last `gate verify` is not green",
                     f"orkestra gate last {p}")
        elif act is None and ver is None and st.get("size") in ("XS", "S"):
            # XS/S: no separate verifier agent (phase-verify.md); the final reviewer stays mandatory
            act = _n("work", f"{st.get('size')}: write verification.md yourself (no verifier agent): start from `orkestra template "
                             f"verification`, cite `orkestra gate last {p}`, then run `orkestra trace check {p}` and "
                             f"`orkestra integrity check {p}` and end with VERDICT: PASS",
                     "gates are green; verification.md is missing", f"orkestra template verification {p}")
        elif act is None and ver is None:
            act = _phase_loop(project, st, "verify", ("verifier",), max_dispatches(), "orkestra:verifier", "verifier wrote no verification.md", p) \
                or _n("dispatch", "dispatch orkestra:verifier (it writes verification.md)", "gates are green; verification.md is missing")
        elif act is None and ver not in ("PASS", "PASS_WITH_NOTES"):
            act = _n("work", "verification FAILED → fix the failing ids through an implementer "
                             f"(`orkestra phase back implement --reason \"verification\" {p}`), then verify again",
                 "verification.md does not end with VERDICT: PASS or PASS_WITH_NOTES")
    elif phase == "review":
        act = _open_tasks_action(project, st, deep)
        if act is None:
            names = [x.name.lower() for x in (fdir / "reviews").glob("*.md")] if (fdir / "reviews").is_dir() else []
            has_final = any("final" in n and "adversary" not in n for n in names)
            has_adv = any("adversary" in n for n in names)
            tri = read_text(fdir / "reviews" / "triage.md")
            if not has_final or (not intent.skips_adversary(st.get("size") or "") and not has_adv):
                act = _phase_loop(project, st, "review", ("reviewer", "adversary"), 2 * max_dispatches(), "final review", "final review not written", p) \
                    or _n("dispatch", "dispatch orkestra:reviewer (scope: final → reviews/final-review.md)"
                                     + ("" if intent.skips_adversary(st.get("size") or "") else
                                        " and orkestra:adversary (→ reviews/adversary.md) in ONE message"),
                         "the final review has not been written")
            elif not tri.strip():
                act = _n("work", "triage every finding yourself → reviews/triage.md (`orkestra template triage`); include the "
                                 "minors carried from task reviews (`orkestra task list`)", "reviews/triage.md is missing")
            else:
                items = PP.open_triage_items(tri)
                if items:
                    act = _n("work", f"open triage items: {', '.join(items[:6])} → for each `fix`: `orkestra task add FIX-R00n "
                                     f"\"<title>\" {p}` → start → implementer → done; then mark the row done",
                             "triage has fix rows that are not done")
    elif phase == "deliver":
        if len(read_text(fdir / "report.md").strip()) < 300:
            act = _n("work", "write report.md (`orkestra template report`), then `orkestra report evidence --write`",
                     "report.md is missing or too short")
        elif rid not in read_text(paths.memory_dir(project) / "FEATURES.md"):
            act = _n("cli", f"`orkestra memory feature-add {p} --title \"…\" --summary \"…\" --capabilities \"…\"`",
                     "memory/FEATURES.md has no entry for this run")
    if phase == "plan" and act:
        note = _plan_late_note(project, st, rid)
        if note:
            act["text"] = note + str(act.get("text") or "")
    if act is None:
        if phase == "done":
            return done(_n("none", "run complete"))
        target = PHASES[PHASES.index(phase) + 1]
        problems: List[str] = []
        if deep:
            from . import runs as R           # late import: runs imports the heavy evidence/snapshot modules
            try:
                problems = R.preconditions(project, st, target)
            except Exception as exc:          # never let a broken artifact hide the next step
                problems = [f"precondition check failed: {exc}"]
        if problems:
            act = _n("work", f"before {phase} → {target}: {problems[0]}"
                             + (f" (+{len(problems) - 1} more: `orkestra phase check {p}`)" if len(problems) > 1 else ""),
                     "`orkestra phase next` would refuse", f"orkestra phase check {p}", problems=problems[:8])
        else:
            act = _n("cli", f"`orkestra phase next {p}` ({phase} → {target})", "this phase's outputs are complete",
                     f"orkestra phase next {p}")
    return done(act)


def _plan_late_note(project: Path, st: Dict[str, Any], rid: str) -> str:
    """Past 10 minutes with no plan.md: write it. A critic FAIL that already has a plan is left alone."""
    opened = None
    for row in reversed(st.get("history") or []):
        if row.get("to") == "plan":
            opened = _ts(row.get("at"))
            break
    if opened is None or time.time() - opened < 600:
        return ""
    if len(read_text(paths.feature_dir(project, rid) / "plan.md").strip()) >= 200:
        return ""
    return ("This plan phase is past 10 minutes and plan.md is not written yet. Write it now from spec.md and the "
            "files the spec names. A written refusal is one task, Files: none, TDD: n/a, then `orkestra phase next`. "
            "Do not keep reading the repository. ")


def plan_late_note(project: Path, st: Dict[str, Any], rid: str) -> str:
    try:
        return _plan_late_note(project, st, rid)
    except Exception:
        return ""


def next_lines(n: Dict[str, Any], flags: Optional[List[str]] = None) -> List[str]:
    """At most 3 lines for the model."""
    out = [f"next [{n.get('phase')}{' · ' + n['task'] + ' · ' + n['step'] if n.get('task') else ''}]: {n.get('text')}"]
    if n.get("also"):
        out.append("also in progress: " + ", ".join(f"{s['task']} ({s['step']})" for s in n["also"]))
    lost = n.get("lost_agents") or []
    if lost:
        who = ", ".join(sorted({A.short_agent(str(a.get("type") or "")) for a in lost}))
        out.append(f"note: {len(lost)} subagent record(s) ignored ({who}: no sign of life for {dur(min(a.get('age') or 0 for a in lost))}) "
                   f"— presumed lost; `orkestra agents clear` removes them")
    if flags and len(out) < 3:
        out.append("flags: " + " | ".join(flags[:3]))
    elif n.get("why") and len(out) < 3:
        out.append(f"why: {n['why']}")
    return out[:3]


# ---------------------------------------------------------------- timing (state + ledger times only)

def timings(st: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    hist = [(h.get("to"), _ts(h.get("at"))) for h in (st.get("history") or []) if _ts(h.get("at"))]
    closed = st.get("status") in ("done", "aborted")
    end = (_ts(st.get("updated")) or now) if closed else now
    phases: Dict[str, float] = {}
    for i, (name, at) in enumerate(hist):
        nxt = hist[i + 1][1] if i + 1 < len(hist) else end
        if name and name != "done":
            phases[name] = phases.get(name, 0.0) + max(0.0, float(nxt or end) - float(at or end))
    tasks = st.get("tasks") or {}
    done = [t for t in tasks.values() if t.get("status") in ("done", "deferred")]
    durs = [d for d in ((_ts(t.get("done_at")) or 0) - (_ts(t.get("started")) or 0) for t in done
                        if t.get("done_at") and t.get("started")) if d > 0]
    reopened = [tid for tid, t in tasks.items() if reopen_count(t)]
    start = _ts(st.get("created")) or (hist[0][1] if hist else now)
    return {"run": max(0.0, end - float(start or end)), "phase": st.get("phase"),
            "phase_elapsed": max(0.0, end - float(hist[-1][1])) if hist else 0.0, "phases": phases,
            "tasks_done": len(done), "tasks_total": len(tasks),
            "task_avg": (phases.get("implement", 0.0) / len(done)) if done and phases.get("implement") else None,
            "task_last_round_median": sorted(durs)[len(durs) // 2] if durs else None,
            "tasks_reopened": len(reopened), "reopens": sum(reopen_count(t) for t in tasks.values())}


def phase_windows(st: Dict[str, Any], now: Optional[float] = None) -> List[Tuple[str, float, float]]:
    """Half-open `[start, end)` intervals for each entered phase, in the same order `timings` sums."""
    now = time.time() if now is None else now
    hist = [(h.get("to"), _ts(h.get("at"))) for h in (st.get("history") or []) if _ts(h.get("at"))]
    closed = st.get("status") in ("done", "aborted")
    end = (_ts(st.get("updated")) or now) if closed else now
    out: List[Tuple[str, float, float]] = []
    for i, (name, at) in enumerate(hist):
        nxt = hist[i + 1][1] if i + 1 < len(hist) else end
        if name and name != "done" and at is not None and nxt is not None:
            out.append((str(name), float(at), float(nxt)))
    return out


def overlap(tasks: Dict[str, Any], tid: str, others: List[str]) -> List[Tuple[str, List[str]]]:
    mine = set((tasks.get(tid) or {}).get("files") or [])
    out = []
    for o in others:
        shared = sorted(mine & set((tasks.get(o) or {}).get("files") or []))
        if shared:
            out.append((o, shared))
    return out


def flags(project: Path, st: Dict[str, Any], rows: Optional[List[Dict[str, Any]]] = None,
          wd: Optional[Dict[str, Any]] = None) -> List[str]:
    """Things the orchestrator must resolve before doing more of the same (each says what to do)."""
    wd = wd or wd_cfg()
    out: List[str] = []
    tasks = st.get("tasks") or {}
    for tid in sorted(tasks):
        t = tasks[tid]
        if t.get("status") in OPEN_TASK and reopen_count(t) >= MAX_REVIEW_ROUNDS - 1:
            out.append(f"{tid}: review round {reopen_count(t) + 1} — if it fails again, "
                       + ("`orkestra task defer` or `orkestra block` instead of reopening (autopilot has no user)"
                          if is_autopilot(st) else "ask the user instead of reopening"))
    in_prog = sorted(t for t, v in tasks.items() if v.get("status") == "in_progress")
    for i, tid in enumerate(in_prog):
        for other, shared in overlap(tasks, tid, in_prog[i + 1:]):
            out.append(f"{tid} and {other} are in progress and share {shared[0]} — run them one after the other")
    if rows:
        out.extend(loop_flags(rows, int(wd.get("loop_repeats") or 3)))
    return out


WAIT_MIN_BLOCK = 3.0     # a wait that returned sooner than this did not wait (interactive, idle, run not active)


def _wait_blocked(rows: List[Dict[str, Any]], idx: int) -> Optional[bool]:
    """Did the `orkestra wait` call at rows[idx] really block? The gap to the same agent's next row tells; None when
    there is no later row (the call is still running). An instant return, repeated, is a hot loop that costs a model
    turn each time."""
    agent, ts = str(rows[idx].get("agent") or "main"), float(rows[idx].get("ts") or 0)
    for nxt in rows[idx + 1:]:
        if str(nxt.get("agent") or "main") == agent:
            return float(nxt.get("ts") or 0) - ts >= WAIT_MIN_BLOCK
    return None


def loop_flags(rows: List[Dict[str, Any]], repeats: int = 3) -> List[str]:
    out: List[str] = []
    recent = rows[-120:]
    denies: Dict[Tuple[str, str], int] = {}
    for r in recent:
        if r.get("kind") == "deny":
            key = (str(r.get("agent") or "main"), str(r.get("what") or "")[:90])
            denies[key] = denies.get(key, 0) + 1
    for (agent, what), n in denies.items():
        if n >= repeats:
            out.append(f"{A.short_agent(agent)} hit the same guard {n}×: {what[:70]} — change the approach, do not retry")
    last_cmd: Dict[str, Tuple[str, int]] = {}
    for idx, r in enumerate(recent):
        if r.get("kind") == "tool" and r.get("tool") == "Bash":
            agent, what = str(r.get("agent") or "main"), str(r.get("what") or "")
            if WAIT_CMD_RE.search(what) and re.search(r"\borkestra\s+wait\b", what):
                blocked = _wait_blocked(recent, idx)
                if blocked is None:
                    continue                              # still running: neither a repeat nor a reset
                if blocked:
                    last_cmd.pop(agent, None)             # `orkestra wait` repeated is the intended wait, not a loop
                    continue
            prev = last_cmd.get(agent)
            last_cmd[agent] = (what, prev[1] + 1) if prev and prev[0] == what else (what, 1)
        elif r.get("kind") == "tool":
            last_cmd.pop(str(r.get("agent") or "main"), None)
    for agent, (what, n) in last_cmd.items():
        if n >= repeats and what:
            out.append(f"{A.short_agent(agent)} ran the same command {n}× in a row: {what[:60]} — "
                       f"`orkestra playbook debugging` before another try")
    return out


def tempo_line(project: Path, st: Dict[str, Any], now: Optional[float] = None,
               rows: Optional[List[Dict[str, Any]]] = None) -> str:
    """One line appended to `task done` / `phase next` output: elapsed, pace, reject rate, flags."""
    tm = timings(st, now)
    parts = [f"run {dur(tm['run'])}", f"{tm['phase']} {dur(tm['phase_elapsed'])}"]
    if tm["tasks_total"]:
        avg = f", avg {dur(tm['task_avg'])}/task" if tm.get("task_avg") else ""
        parts.append(f"tasks {tm['tasks_done']}/{tm['tasks_total']}{avg}")
        if tm["reopens"]:
            parts.append(f"review rejects {tm['reopens']} on {tm['tasks_reopened']} task(s)")
    fl = flags(project, st, rows)
    return "tempo: " + " · ".join(parts) + (" · FLAGS: " + " | ".join(fl[:2]) if fl else "")


def report_lines(rid: str, st: Dict[str, Any], tm: Dict[str, Any], dw: Dict[str, Any], cls: Dict[str, Any],
                 flag_lines: Optional[List[str]] = None, card: Optional[Dict[str, Any]] = None) -> List[str]:
    """The human-readable lines of `orkestra tempo` (labels from the catalog; durations and figures as measured)."""
    t = i18n.t
    out = [t("tempo.cmd_head", rid=rid, status=st["status"], phase=st["phase"], total=dur(tm["run"])),
           t("tempo.cmd_phases", phases=" · ".join(f"{name} {dur(sec)}" for name, sec in tm["phases"].items()))]
    if tm["tasks_total"]:
        out.append(t("tempo.cmd_tasks", done=tm["tasks_done"], total=tm["tasks_total"])
                   + (t("tempo.cmd_task_avg", dur=dur(tm["task_avg"])) if tm.get("task_avg") else "")
                   + (t("tempo.cmd_task_median", dur=dur(tm["task_last_round_median"])) if tm.get("task_last_round_median") else "")
                   + t("tempo.cmd_rejects", n=tm["reopens"], tasks=tm["tasks_reopened"]))
    if dw["window"]:
        out.append(t("tempo.cmd_dead_wait", dead=dur(dw["dead"]), window=dur(dw["window"]), share=f"{dw['share'] * 100:.0f}",
                     count=dw["count"], longest=dur(dw["longest"])))
    out.append(t("tempo.cmd_now", text=cls["text"]) + (t("tempo.cmd_now_since", dur=dur(cls["since"])) if cls.get("since") else ""))
    out.extend(t("tempo.cmd_flag", flag=f) for f in flag_lines or [])
    if card:
        from . import scorecard as SC        # late import: only `orkestra tempo` needs it
        out.extend(SC.lines(card, t))
        warn = SC.warning(card, t)
        if warn:
            out.append(warn)
    return out


# ---------------------------------------------------------------- stall classification (read-only)

BATCH_MARGIN = 3.0       # tool calls of one parallel batch land within this many seconds of each other


def _running_commands(rows: List[Dict[str, Any]]) -> List[Tuple[str, float, str]]:
    """Per agent: a Bash call that started and has no `end` row yet.

    Hooks never fire for interrupted or cancelled tool calls, so an `end` row alone would leave such a command open
    for ever: any LATER row of the same agent (more than a parallel batch later) proves that agent moved on, the
    main session's `idle` row ends the session (every open command with it), and an agent's stop closes its own."""
    open_: Dict[str, Tuple[str, float, str]] = {}
    for r in rows:
        agent = str(r.get("agent") or "main")
        key = str(r.get("aid") or agent)            # two implementers are two agents: one stopping must not close
        kind, ts = r.get("kind"), float(r.get("ts") or 0)
        if r.get("tool") == "Bash" and kind == "tool":                # the other one's running command
            open_[key] = (agent, ts, str(r.get("what") or ""))
        elif kind in ("end", "deny") and r.get("tool") == "Bash":
            open_.pop(key, None)
        elif kind == "agent_done":
            if open_.pop(key, None) is None:        # rows written before agent ids were recorded are keyed by type
                open_.pop(agent, None)
        elif agent == "main" and kind == "idle":
            open_.clear()                           # the session's turn is over: nothing of it is still running
        elif agent == "main" and kind in ("wait_agents", "user"):
            open_.pop("main", None)
        elif kind not in ("nudge", "status", "bind") and key in open_ and ts > open_[key][1] + BATCH_MARGIN:
            open_.pop(key, None)                    # the same agent did something else afterwards: it is not stuck there
    return list(open_.values())


def _is_wait_row(r: Dict[str, Any]) -> bool:
    return (r.get("agent") or "main") == "main" and r.get("tool") == "Bash" and r.get("kind") == "tool" \
        and bool(WAIT_CMD_RE.search(str(r.get("what") or "")))


def last_activity(rows: List[Dict[str, Any]], ignore_end: bool = False) -> float:
    """Newest row of ANY agent that is real run activity: not the Stop hook's own bookkeeping, not the main session's
    wait commands (polling is waiting, not working); `end` rows only count when ignore_end is False."""
    best = 0.0
    for r in rows:
        kind = r.get("kind")
        if kind in ("nudge", "wait_agents") or (ignore_end and kind == "end") or _is_wait_row(r):
            continue
        best = max(best, float(r.get("ts") or 0))
    return best


def classify(st: Dict[str, Any], rows: List[Dict[str, Any]], pending: Optional[List[Dict[str, Any]]] = None,
             now: Optional[float] = None, wd: Optional[Dict[str, Any]] = None,
             state_mtime: Optional[float] = None, reports_due: Optional[List[Dict[str, Any]]] = None,
             proc_alive: Optional[bool] = None) -> Dict[str, Any]:
    """cls: 0 working · 1 waiting for the user (legitimate) · 2 dead wait · 3 hung command · 4 silent session.
    `loops` (class 5) is reported next to any of them.

    Only deterministic predicates decide a stall (a thinking model is not one): `hung` needs an open command older than
    its limit AND no row of any agent since it started (and `proc_alive` not True: an optional child-process / CPU
    signal the caller may pass); `dead_wait` needs a stopped subagent whose report (activity.agents_reports_due,
    passed as `reports_due`) is older than DUE_REPORT_SECONDS while the whole run was silent since."""
    now = time.time() if now is None else now
    wd = wd or wd_cfg()
    pending = pending or []
    status = st.get("status")
    loops = loop_flags(rows, int(wd.get("loop_repeats") or 3))
    tasks = st.get("tasks") or {}
    loops += [f"{tid}: {reopen_count(t)} review rounds" for tid, t in sorted(tasks.items())
              if t.get("status") in OPEN_TASK and reopen_count(t) >= MAX_REVIEW_ROUNDS]

    def res(cls: int, key: str, since: float, text: str) -> Dict[str, Any]:
        # cls / key are machine values (never translated); `text` is the sentence a human reads, in the current language
        return {"cls": cls, "key": key, "since": int(max(0, now - since)) if since else None, "text": text, "loops": loops}

    if status in ("done", "aborted"):
        return res(0, "closed", 0, i18n.t("tempo.stall_closed"))
    if status == "awaiting_user":
        at = _ts((st.get("awaiting") or {}).get("at")) or 0
        return res(1, "user", at, i18n.t("tempo.stall_user_waiting",
                                         what=(st.get("awaiting") or {}).get("question") or i18n.t("tempo.question_word")))
    if status == "blocked":
        return res(1, "blocked", _ts((st.get("blocked") or {}).get("at")) or 0,
                   i18n.t("tempo.stall_blocked", reason=(st.get("blocked") or {}).get("reason") or ""))
    if status == "paused":
        return res(1, "paused", 0, i18n.t("tempo.stall_paused"))
    q = A.pending_question(rows)
    if q:
        ts = next((float(r.get("ts") or 0) for r in reversed(rows) if r.get("tool") == "AskUserQuestion"), 0)
        return res(1, "user", ts, i18n.t("tempo.stall_open_question", question=q))
    main_rows = [r for r in rows if (r.get("agent") or "main") == "main" and r.get("kind") not in ("end", "nudge")]
    main_last = main_rows[-1] if main_rows else None
    state_ts = float(state_mtime or 0)
    if main_last and main_last.get("kind") == "idle" and state_ts <= float(main_last.get("ts") or 0):
        return res(1, "turn_ended", float(main_last.get("ts") or 0), i18n.t("tempo.stall_turn_ended"))
    for agent, ts, what in _running_commands(rows):
        limit = float(wd["gate_command_seconds"] if re.search(r"\borkestra\s+(\S+\s+)*(gate|tdd|evidence)\b", what)
                      else wd["hung_command_seconds"])
        if now - ts > limit and not (agent == "main" and WAIT_CMD_RE.search(what)):
            if last_activity(rows) > ts or proc_alive is True:
                continue                  # a slow command while the run moves on (or the process lives) is working
            return res(3, "hung", ts, i18n.t("tempo.stall_hung", agent=A.short_agent(agent), dur=dur_tr(now - ts),
                                             cmd=what[:80]))
    quiet = agents_quiet(rows, pending, now, wd)
    if pending and quiet is None:
        who = ", ".join(A.short_agent(str(a.get("type") or "")) for a in pending[:4])
        return res(0, "agents", float(pending[0].get("at") or now), i18n.t("tempo.stall_agents", who=who))
    done_ts = max([float(r.get("ts") or 0) for r in rows if r.get("kind") == "agent_done"] or [0.0])
    handback_ts = max([float(r.get("ts") or 0) for r in main_rows if r.get("kind") == "handback"] or [0.0])
    wait_limit = min(float(wd["dead_wait_seconds"]), float(DUE_REPORT_SECONDS))
    main_waiting = any(a == "main" and WAIT_CMD_RE.search(w) for a, _, w in _running_commands(rows))
    if reports_due is not None:
        # keyed on the registry: a report that is due and has not arrived (a hand-back row settles it)
        due_at = [float(d.get("at") or 0) for d in reports_due if not handback_ts or float(d.get("at") or 0) > handback_ts]
        since: Optional[float] = min(due_at) if due_at else None
    else:
        since = done_ts if done_ts and (handback_ts <= done_ts or main_waiting) else None
    if since is not None:
        progressed = any(float(r.get("ts") or 0) > since and r.get("kind") in ("tool", "answer", "deny", "result", "user")
                         and not _is_wait_row(r) for r in main_rows)
        quiet_since = max(since, last_activity(rows, ignore_end=True))
        if not progressed and now - quiet_since > wait_limit:
            why = i18n.t("tempo.dead_why_wait_cmd" if main_waiting else "tempo.dead_why_no_report")
            return res(2, "dead_wait", since, i18n.t("tempo.stall_dead_wait", ago=i18n.ago(now - since), why=why))
    activity_ts = max([float(r.get("ts") or 0) for r in rows] or [0.0])
    last_ts = max(activity_ts, state_ts)
    if last_ts and now - last_ts > float(wd["silent_seconds"]) and not _running_commands(rows):
        lost = " " + i18n.t("tempo.lost_agents", n=len(pending), dur=dur_tr(quiet)) if pending and quiet else ""
        if state_ts > activity_ts:
            hook_activity = i18n.t("tempo.hook_last", ago=i18n.ago(now - activity_ts)) if activity_ts \
                else i18n.t("tempo.hook_none")
            return res(4, "silent", last_ts, i18n.t("tempo.stall_silent_state", ago=i18n.ago(now - state_ts),
                                                    hook=hook_activity, lost=lost))
        return res(4, "silent", last_ts, i18n.t("tempo.stall_silent", dur=dur_tr(now - last_ts), lost=lost))
    return res(0, "working", last_ts, i18n.t("tempo.stall_working"))


def dead_wait_share(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Measured dead wait: time between a subagent's stop and the main session's next real action."""
    rows = sorted(rows, key=lambda r: float(r.get("ts") or 0))
    if len(rows) < 2:
        return {"window": 0.0, "dead": 0.0, "share": 0.0, "count": 0, "longest": 0.0}
    window = float(rows[-1]["ts"]) - float(rows[0]["ts"])
    dead, count, longest = 0.0, 0, 0.0
    waiting_since: Optional[float] = None
    running = 0
    for r in rows:
        kind, agent, ts = r.get("kind"), (r.get("agent") or "main"), float(r.get("ts") or 0)
        if kind == "result" and str(r.get("what") or "").rstrip().endswith("async_launched"):
            running += 1
        elif kind == "agent_done":
            running = max(0, running - 1)
            if running == 0 and waiting_since is None:
                waiting_since = ts
        elif agent == "main" and waiting_since is not None and kind in ("tool", "answer", "result", "deny") \
                and not (r.get("tool") == "Bash" and WAIT_CMD_RE.search(str(r.get("what") or ""))):
            gap = ts - waiting_since
            if gap > 20:
                dead += gap
                count += 1
                longest = max(longest, gap)
            waiting_since = None
    return {"window": window, "dead": dead, "share": (dead / window) if window else 0.0, "count": count, "longest": longest}


# ---------------------------------------------------------------- waiting without a model (orkestra wait)

WAIT_POLL = 2.0              # seconds between looks at the registry, the state and the review files
WAIT_MAX = 540               # default cap: below the 600 s a Bash call may run


def _artifact_stamp(project: Path, run_id: str) -> float:
    """Newest write time among the files a subagent hands its result over in: reviews/*.md and tasks/*.report.md."""
    fdir, best = paths.feature_dir(project, run_id), 0.0
    for sub, pat in (("reviews", "*.md"), ("tasks", "*.report.md")):
        d = fdir / sub
        if not d.is_dir():
            continue
        try:
            for f in d.glob(pat):
                best = max(best, f.stat().st_mtime)
        except OSError:
            continue
    return best


def wait_for(project: Path, run_id: str, max_seconds: float = WAIT_MAX, poll: float = WAIT_POLL,
             sleep: Any = time.sleep, clock: Any = time.monotonic, wall: Any = time.time) -> Dict[str, Any]:
    """Block INSIDE this process (no model turn, no tokens) until something the orchestrator must react to happens.

    Returns {"reason", "waited"}: `idle` no subagent is pending (also at once, when none was) · `reports` another
    subagent stopped during the wait (its report is on its way) · `handback` a hand-back reached the main session · `status` the run left
    `active` · `artifact` a review / task report file changed · `timeout` the cap passed. Never longer than
    max_seconds (+ one poll). Clocks are injectable for tests."""
    from .state import load_state                      # late: keeps tempo importable by state-free callers
    t0 = clock()
    seen_artifact = _artifact_stamp(project, run_id)
    seen_handback = A.last_handback_at(project, run_id)
    seen_due = {str(d.get("id")) for d in A.agents_reports_due(project, run_id, now=wall())}   # already known: no thrash
    max_seconds = max(0.0, float(max_seconds))
    while True:
        waited = clock() - t0
        st = load_state(project, run_id)
        if st.get("status") != "active":
            return {"reason": "status", "waited": waited}
        running, _lost = live_agents(project, run_id, now=wall())
        if not running:
            return {"reason": "idle", "waited": waited}
        if any(str(d.get("id")) not in seen_due for d in A.agents_reports_due(project, run_id, now=wall())):
            return {"reason": "reports", "waited": waited}
        if A.last_handback_at(project, run_id) > seen_handback:
            return {"reason": "handback", "waited": waited}
        if _artifact_stamp(project, run_id) > seen_artifact:
            return {"reason": "artifact", "waited": waited}
        if waited >= max_seconds:
            return {"reason": "timeout", "waited": waited}
        sleep(max(0.0, min(poll, max_seconds - waited)))


def stop_tail(project: Path, st: Dict[str, Any]) -> str:
    """The closing sentence of the Stop nudge, by mode: autopilot never sends the model to AskUserQuestion / await."""
    p = p_arg(project)
    if is_autopilot(st):
        return (f"Autopilot has no user to ask: take the most conservative option and continue; with no safe default run "
                f"`orkestra task defer <TID> \"<why>\" {p}` or `orkestra block \"<reason>\" {p}` and explain. If the user "
                f"changed topic and this chat drives the run, run `orkestra pause {p}` first.")
    return (f"If you must wait for the user, ask with AskUserQuestion, or run `orkestra await \"<question>\" {p}` "
            f"(headless). If truly blocked, run `orkestra block \"<reason>\" {p}` and explain. If the user "
            f"changed topic and this chat drives the run, run `orkestra pause {p}` first.")
