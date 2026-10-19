"""Run lifecycle: start, phase transitions with hard preconditions, tasks, TDD evidence, approvals."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import evidence as E
from . import gates as G
from . import i18n
from . import intent
from . import paths
from . import planparse as PP
from . import snapshot as S
from . import tempo as T
from .messages import protocol as P
from .state import (OPEN_STATUSES, PHASES, OrkestraError, StateTxn, active_runs, bind_marker, ledger, load_state,
                    new_state, next_run_id, project_active_run, save_state, set_active_run, unbind_marker)
from .state import TERMINAL_STATUSES
from .workkind import classify, deliverable_file_problems, kind_of
from .util import (file_lock, now_iso, read_json, read_text, sha256_bytes, tail_lines, write_json_atomic,
                   write_text_atomic)

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"

BACK_EDGES = {
    "discover": ["intake"],
    "clarify": ["discover"],
    "plan": ["clarify"],
    "approve": ["plan", "clarify"],
    "implement": ["plan"],
    "verify": ["implement"],
    "review": ["implement"],
    "deliver": ["review", "implement"],
}


# ---------------------------------------------------------------- project init / memory skeleton

def _run_shell_or_list(cmd: str, *, cwd: str, timeout: int, env: dict, argv: Optional[List[str]] = None):
    """Evidence/TDD runner. `cmd` is the POSIX-quoted record of the command (what the log and the red/green comparison
    use); it runs through the shell. On Windows, several `argv` items run as a list instead: POSIX quotes mean nothing
    to cmd.exe, and a list keeps every argument (spaces, quotes, `;`) exactly as the user typed it."""
    if G._IS_WINDOWS and argv and len(argv) > 1:
        return G._run_tree(list(argv), cwd=cwd, env=env, timeout=timeout)
    return G._run_tree(cmd, cwd=cwd, env=env, timeout=timeout)


# A project .gitignore that already names the directory, in any of these shapes, is enough.
_ORKESTRA_IGNORE_LINES = {".orkestra/", ".orkestra", "*.orkestra", "**/.orkestra/", "**/.orkestra/**"}


def ensure_project_gitignore(project: Path) -> bool:
    """If the project already has a `.gitignore` and `.orkestra/` is not ignored, append that one line.

    Does not create a `.gitignore`. `*.orkestra` would also hide a source file whose name ends in
    `.orkestra`, so the line we add is the directory itself. Returns True when the file changed.
    """
    gi = Path(project) / ".gitignore"
    if not gi.is_file():
        return False
    try:
        text = gi.read_text(encoding="utf-8")
    except OSError:
        return False
    covered = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line in _ORKESTRA_IGNORE_LINES:
            covered = True
            break
    if covered:
        return False
    suffix = "" if text.endswith("\n") or text == "" else "\n"
    addition = suffix + "# orkestra: local run state stays out of the repository\n.orkestra/\n"
    write_text_atomic(gi, text + addition)
    return True


def init_project(project: Path) -> List[str]:
    created: List[str] = []
    ensure_project_gitignore(project)
    ok = paths.okdir(project)
    for d in (paths.memory_dir(project) / "specs", paths.features_dir(project), paths.runs_dir(project)):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(project)))
    gi = ok / ".gitignore"
    if not gi.exists():
        write_text_atomic(gi, "# orkestra: machine state, backups and logs stay local\nruns/\n*.lock\ncurrent\n")
        created.append(str(gi.relative_to(project)))
    for name in ("PROJECT.md", "FEATURES.md", "DECISIONS.md", "LEARNINGS.md"):
        target = paths.memory_dir(project) / name
        if not target.exists():
            tpl = read_text(i18n.template_dir(TEMPLATES) / "memory" / name) or f"# {name[:-3]}\n"
            write_text_atomic(target, tpl.replace("{{project}}", project.name))
            created.append(str(target.relative_to(project)))
    cfgp = paths.project_config_path(project)
    if not cfgp.exists():
        cfg = {"gates": G.detect(project), "gates_confirmed": False, "ui": {"kind": "unknown"}, "notes": ""}
        paths.save_project_config(project, cfg)
        created.append(str(cfgp.relative_to(project)))
    from . import memory as M
    M.observe(project)
    return created


# ---------------------------------------------------------------- start / resume

def request_scaffold(title: str, run_id: str, size: str, mode: str, engine: str, approval_mode: str,
                     request: str, code: Optional[str] = None) -> str:
    """request.md as `start` writes it, in `code` (default: the current language). Headings and labels come from
    messages/protocol; the readers accept both languages, so a run can be continued in the other one."""
    lang = code or i18n.lang()

    def label(key: str) -> str:
        pair = P.REQUEST_FIELDS[key]
        return pair[1] if lang == "tr" else pair[0]

    return (f"# {i18n.heading('request', lang)}: {title}\n\n- {label('run')}: `{run_id}`\n- {label('size')}: {size}\n"
            f"- {label('mode')}: {mode}\n- {label('engine')}: {engine}\n"
            f"- {label('approval_mode')}: {approval_mode}\n- {label('date')}: {now_iso()}\n\n"
            f"## {i18n.heading('raw_request', lang)}\n\n{request.strip()}\n")


def start_run(project: Path, title: str, request: str, size: str, mode: str, engine: str = "claude",
              approval_mode: str = "autopilot") -> Dict[str, Any]:
    """Open a run. One at a time per project: looking for an open run and creating the next one is a single step, so
    two `orkestra start` at once cannot both pass the check (they used to leave two open runs, with clashing numbers)."""
    project = project.resolve(strict=False)
    with file_lock(paths.okdir(project) / "start"):
        return _start_run_locked(project, title, request, size, mode, engine, approval_mode)


def open_run_for_request(project: Path, request: str, *, engine: str, mode: str,
                         approval_mode: Optional[str] = None, title: Optional[str] = None,
                         size: Optional[str] = None) -> Dict[str, Any]:
    """Open a tracked run for any `orkestra run` / bridge job (delivery or advisory) so the dashboard can follow it."""
    request = (request or "").strip()
    if approval_mode not in ("autopilot", "manual"):
        approval_mode = "manual" if intent.manual_approval_requested(request) else "autopilot"
    title = (title or intent.title_from_request(request)).strip() or "request"
    size = size or intent.infer_size(request)
    if size not in intent.SIZES:
        raise OrkestraError(f"invalid size {size!r}")
    return start_run(project, title, request, size, mode, engine, approval_mode)


def _start_run_locked(project: Path, title: str, request: str, size: str, mode: str, engine: str,
                      approval_mode: str) -> Dict[str, Any]:
    existing = project_active_run(project)
    if existing:
        st = load_state(project, existing)
        raise OrkestraError(f"project already has an open run '{existing}' (phase {st['phase']}, status {st['status']}). "
                            f"Resume it with `orkestra resume` or close it with `orkestra abort \"reason\"`.")
    init_project(project)
    run_id = next_run_id(project, title)
    fdir = paths.feature_dir(project, run_id)
    rdir = paths.run_dir(project, run_id)
    (fdir / "tasks").mkdir(parents=True, exist_ok=True)
    (fdir / "reviews").mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)
    st = new_state(project, run_id, title, size, mode, engine, approval_mode, work_kind=classify(request))
    base = S.take_baseline(project, rdir)
    st["baseline"] = {"git": base["git"], "head": base.get("head"), "at": base["at"],
                      "dirty_files": len(base.get("files") or {})}
    st["baseline"]["deps"] = G.dependency_sets(project)
    save_state(project, st)
    write_text_atomic(fdir / "request.md", request_scaffold(title, run_id, size, mode, engine, approval_mode, request))
    set_active_run(project, run_id)
    point_current(project, run_id)
    ledger(project, run_id, f"run started: {title!r} (size {size}, mode {mode}, engine {engine}); baseline git={base['git']} "
                            f"head={str(base.get('head'))[:10]} dirty_files={st['baseline']['dirty_files']}")
    return st


def record_engine_origin(project: Path, run_id: str, engine: str) -> bool:
    """Correct a run's origin when its host hook identifies it after CLI start."""
    if engine not in ("claude", "cursor", "copilot", "pi"):
        return False
    project = Path(project)
    with StateTxn(project, run_id) as st:
        if st.get("phase") != "intake" or st.get("engine") == engine:
            return False
        st["engine"] = engine
    request_path = paths.feature_dir(project, run_id) / "request.md"
    request = read_text(request_path)
    labels = "|".join(re.escape(x) for x in P.REQUEST_FIELDS["engine"])           # `engine` or `motor`: keep the run's own
    updated = re.sub(rf"(?m)^(- (?:{labels}): )[^\n]*$", lambda m: m.group(1) + engine, request, count=1)
    if updated != request:
        write_text_atomic(request_path, updated)
    ledger(project, run_id, f"origin engine identified by host hook: {engine}")
    return True


def set_approval_mode(project: Path, run_id: str, mode: str, reason: str = "") -> bool:
    """Change the decision owner only when a real user explicitly asks to take it back."""
    if mode not in ("autopilot", "manual"):
        raise OrkestraError("approval mode must be autopilot or manual")
    with StateTxn(project, run_id) as st:
        if st.get("status") in ("done", "aborted"):
            return False
        if st.get("approval_mode", "autopilot") == mode:
            return False
        st["approval_mode"] = mode
        if mode == "manual" and st.get("phase") == "approve":
            st["status"] = "active"
            st["awaiting"] = None
    ledger(project, run_id, f"approval mode → {mode}" + (f": {reason[:120]}" if reason else ""))
    return True


def other_open_runs(project: Path) -> List[str]:
    """Open runs in OTHER projects (they share this machine's CPU: tests, builds, simulators)."""
    out: List[str] = []
    me = str(Path(project).resolve(strict=False))
    for key, rid in sorted(active_runs().items()):
        if str(Path(key).resolve(strict=False)) == me:
            continue
        st = read_json(paths.run_dir(Path(key), rid) / "state.json")
        if isinstance(st, dict) and st.get("status") in OPEN_STATUSES:
            out.append(f"{Path(key).name} ({st.get('phase')})")
    return out


def point_current(project: Path, run_id: Optional[str]) -> None:
    """`.orkestra/current` → features/<run> (stable short path for agents; removed when the run closes)."""
    link = paths.okdir(project) / "current"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        if run_id:
            link.symlink_to(Path("features") / run_id)
    except OSError:
        pass


def resume_run(project: Path, run_id: Optional[str]) -> Dict[str, Any]:
    project = project.resolve(strict=False)
    rid = run_id or project_active_run(project)
    if not rid:
        raise OrkestraError(f"no open run in {project}")
    st = load_state(project, rid)
    if st["status"] in ("done", "aborted"):
        raise OrkestraError(f"run '{rid}' is {st['status']}")
    set_active_run(project, rid)
    point_current(project, rid)
    if st["status"] == "paused":
        with StateTxn(project, rid) as s:
            s["status"] = "active"
        st = load_state(project, rid)
        ledger(project, rid, "resumed")
    return st


def usage_snapshot(project: Path, st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Token usage of the sessions bound to this run, read once when the run closes (best effort, never raises)."""
    try:
        from . import usage as U
        from .util import read_json as _rj
        ids = []
        d = paths.sessions_dir()
        if d.is_dir():
            for f in d.glob("*.json"):
                data = _rj(f)
                if isinstance(data, dict) and data.get("run_id") == st.get("run_id") \
                        and Path(data.get("project", "")) == Path(project):
                    ids.append(f.stem)
        created = T._ts(st.get("created"))
        u = U.for_sessions(ids, since=created - 120 if created else None,
                           windows=T.phase_windows(st)) if ids else None
        if u:
            kept = {k: u.get(k) for k in ("input", "output", "cache_write", "cache_read", "total", "messages", "context")}
            if isinstance(u.get("phases"), dict):
                kept["phases"] = {str(k): int(v) for k, v in u["phases"].items() if isinstance(v, int)}
            u = kept
            u["at"] = now_iso()
        return u
    except Exception:
        return None


# ---------------------------------------------------------------- preconditions

def spec_plan_hash(project: Path, run_id: str) -> str:
    fdir = paths.feature_dir(project, run_id)
    data = (fdir / "spec.md").read_bytes() if (fdir / "spec.md").exists() else b""
    data += b"\0" + ((fdir / "plan.md").read_bytes() if (fdir / "plan.md").exists() else b"")
    return sha256_bytes(data)


def _legacy_plan_review_is_current(project: Path, st: Dict[str, Any]) -> bool:
    """Migrate pre-hash runs only when their critic artifact is newer than the reviewed inputs."""
    if intent.skips_plan_critic(st.get("size") or ""):
        return True
    fdir = paths.feature_dir(project, st["run_id"])
    critic = fdir / "reviews" / "plan-critic.md"
    inputs = [fdir / name for name in ("spec.md", "plan.md")]
    try:
        latest_input = max(path.stat().st_mtime_ns for path in inputs if path.exists())
        return critic.stat().st_mtime_ns >= latest_input
    except (OSError, ValueError):
        return False


def preconditions(project: Path, st: Dict[str, Any], target: str) -> List[str]:
    rid = st["run_id"]
    fdir = paths.feature_dir(project, rid)
    rdir = paths.run_dir(project, rid)
    spec = read_text(fdir / "spec.md")
    plan = read_text(fdir / "plan.md")
    problems: List[str] = []
    if target == "discover":
        if not (fdir / "request.md").exists():
            problems.append("request.md missing")
    elif target == "clarify":
        ctx = read_text(fdir / "context.md")
        need = intent.min_context_chars(st.get("size") or "M")
        if len(ctx.strip()) < need:
            problems.append(f"context.md missing or too thin (discovery output, >= {need} chars)")
    elif target == "plan":
        if not spec.strip():
            problems.append("spec.md missing")
        elif not PP.has_heading(spec, "clarifications"):
            problems.append(f"spec.md needs a `## {i18n.heading('clarifications')}` section (questions asked + answers, "
                            f"or why none were needed)")
        if spec.strip() and not PP.spec_ids(spec):
            problems.append("spec.md defines no FR-###/SC-### ids")
    elif target == "approve":
        problems += PP.check_plan(spec, plan, project, work_kind=kind_of(st), run_id=rid)
        if not intent.skips_plan_critic(st.get("size") or ""):
            critic = read_text(fdir / "reviews" / "plan-critic.md")
            v = PP.verdict(critic)
            if v not in ("PASS", "PASS_WITH_NOTES"):
                problems.append("plan-critic verdict missing or not PASS (reviews/plan-critic.md → `VERDICT: PASS`)")
            # Entering approve only: a critic older than spec.md or plan.md must not be stamped.
            phase = st.get("phase") or ""
            if phase in PHASES and PHASES.index(phase) < PHASES.index("approve") \
                    and not _legacy_plan_review_is_current(project, st):
                problems.append("plan-critic is missing or older than spec.md or plan.md")
    elif target == "implement":
        appr = read_json(rdir / "approval.json")
        if isinstance(appr, dict) and appr.get("decision") == "reject":
            problems.append("the user requested plan changes; revise the plan and run the critic again")
        elif not isinstance(appr, dict) or appr.get("decision") != "approve":
            if st.get("approval_mode", "autopilot") == "manual":
                problems.append("no user approval recorded for this manual run. Recording it yourself would be forging the user's consent; ask the user to approve the current plan.")
            else:
                critic = PP.verdict(read_text(fdir / "reviews" / "plan-critic.md"))
                if not intent.skips_plan_critic(st.get("size") or "") and critic not in ("PASS", "PASS_WITH_NOTES"):
                    problems.append("autopilot needs a current plan-critic PASS before approval")
                if st.get("plan_review_hash") != spec_plan_hash(project, rid) and not (
                        not st.get("plan_review_hash") and _legacy_plan_review_is_current(project, st)):
                    problems.append("spec/plan changed after plan review; rerun plan-critic before autopilot approval")
        elif appr.get("hash") != spec_plan_hash(project, rid):
            problems.append("spec/plan changed after approval — rerun plan-critic and record a current approval")
        problems += PP.check_plan(spec, plan, project, work_kind=kind_of(st), run_id=rid)
        # the baseline may run in the background during discover/clarify/plan (source cannot change before approval:
        # the phase guard), but it must be complete before the first edit
        if not (rdir / "gates-baseline.json").exists():
            if (rdir / "gates-baseline.partial.json").exists():
                problems.append("baseline gates still running or interrupted: wait for `orkestra gate baseline` to "
                                "finish, or run it again")
            else:
                problems.append("baseline gates not recorded: run `orkestra gate baseline` (it records an empty "
                                "baseline if the project has no gates, which is reported as a risk)")
    elif target == "verify":
        open_tasks = [tid for tid, t in st.get("tasks", {}).items() if t.get("status") not in ("done", "deferred")]
        if open_tasks:
            problems.append(f"tasks not done: {', '.join(sorted(open_tasks))}")
        for tid, t in st.get("tasks", {}).items():
            if t.get("status") == "done" and t.get("tdd") == "required" and not (t.get("green") or {}).get("ok"):
                problems.append(f"{tid}: done without green test evidence (`orkestra tdd green {tid} -- <cmd>`)")
            if t.get("status") == "deferred" and not t.get("deferred_reason"):
                problems.append(f"{tid}: deferred without a reason")
        if kind_of(st) in ("writing", "general"):
            problems += deliverable_file_problems(fdir / "deliverable.md")
    elif target == "review":
        changed = S.changed_files(project, rdir)            # ONE scan serves the newest-change check and the integrity check
        last = read_json(rdir / "gates-verify.json")
        if not isinstance(last, dict):
            problems.append("`orkestra gate verify` has not been run")
        elif not (last.get("comparison") or {}).get("ok"):
            problems.append("last `orkestra gate verify` shows regressions/failures — fix before review")
        else:
            env_names = [r["name"] for r in last.get("results") or [] if r.get("env_error") and not r.get("ok")]
            if env_names and not read_text(fdir / "gates-waiver.md").strip():
                problems.append(i18n.t("runs.gates_env_waiver_needed", names=", ".join(env_names)))
            newest_change = _newest_change_mtime(project, rdir, changed)
            if newest_change and newest_change > float(last.get("ts", 0)):
                problems.append("files changed after the last `orkestra gate verify` — run it again")
        ver = read_text(fdir / "verification.md")
        if PP.verdict(ver) not in ("PASS", "PASS_WITH_NOTES"):
            problems.append("verification.md must end with `VERDICT: PASS` or `VERDICT: PASS_WITH_NOTES`")
        problems += PP.check_trace(spec, ver)
        gate_names = [g["name"] for g in paths.project_config(project).get("gates") or []]
        problems += E.verification_problems(project, rid, spec, ver, gate_names)
        problems += E.integrity_problems(project, rid, changed)
        problems += deps_problems(project, st)
    elif target == "deliver":
        open_tasks = [tid for tid, t in st.get("tasks", {}).items() if t.get("status") not in ("done", "deferred")]
        if open_tasks:
            problems.append(f"tasks not done (incl. review fixes): {', '.join(sorted(open_tasks))}")
        tri = read_text(fdir / "reviews" / "triage.md")
        if not tri.strip():
            problems.append("reviews/triage.md missing (every review finding needs a verdict: fix/defer/reject)")
        else:
            open_items = PP.open_triage_items(tri)
            if open_items:
                problems.append(f"open review items: {', '.join(open_items)}")
        last = read_json(rdir / "gates-verify.json")
        newest_change = _newest_change_mtime(project, rdir)
        if isinstance(last, dict) and newest_change and newest_change > float(last.get("ts", 0)):
            problems.append("code changed during review fixes — run `orkestra gate verify` again")
        elif isinstance(last, dict) and not (last.get("comparison") or {}).get("ok"):
            problems.append("last gate verify is not green")
        elif isinstance(last, dict) and any(r.get("carried_from") for r in last.get("results") or []):
            problems.append("the last gate verify carried gates forward: run `orkestra gate verify --all` once so the "
                            "final proof covers every gate")
    elif target == "done":
        rep = read_text(fdir / "report.md")
        if len(rep.strip()) < 300:
            problems.append("report.md missing or too short")
        ok, why = E.report_has_current_block(project, rid)
        if not ok:
            problems.append(why)
        feats = read_text(paths.memory_dir(project) / "FEATURES.md")
        if rid not in feats:
            problems.append(f"memory/FEATURES.md has no entry for {rid} (`orkestra memory feature-add ...`)")
    return problems


def _newest_change_mtime(project: Path, rdir: Path, changed: Optional[List[Dict[str, Any]]] = None) -> float:
    newest = 0.0
    for item in changed if changed is not None else S.changed_files(project, rdir):
        p = project / item["path"]
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            newest = max(newest, 0.0)
    return newest


def deps_problems(project: Path, st: Dict[str, Any]) -> List[str]:
    before = (st.get("baseline") or {}).get("deps") or {}
    after = G.dependency_sets(project)
    added = G.new_dependencies(before, after)
    approved = {G.normalize_pkg(x) for x in ((st.get("approved") or {}).get("deps") or [])}
    unapproved = [d for d in added if G.normalize_pkg(d) not in approved]
    return [f"new dependency not approved by the user: {d}" for d in unapproved]


# ---------------------------------------------------------------- transitions

# preconditions of these targets walk the tree and hash files: they run BEFORE the state lock is taken
SCAN_TARGETS = ("review", "deliver", "done")
# bookkeeping hooks write while a command works; none of it can change what a precondition says
_VOLATILE_STATE = ("updated", "stop_blocks", "subagent_stop_blocks", "usage")


def _state_token(st: Dict[str, Any]) -> str:
    """Fingerprint of the state a precondition check was computed from."""
    kept = {k: v for k, v in st.items() if k not in _VOLATILE_STATE}
    return sha256_bytes(json.dumps(kept, sort_keys=True, default=str).encode("utf-8"))


def _precheck(project: Path, run_id: str) -> Optional[Dict[str, Any]]:
    """The slow part of phase_next (change scans, hashing, git, transcripts) computed from an unlocked read, so that
    hooks and other agents never queue behind it. phase_next uses the result only if the state is unchanged once it
    holds the lock (same fingerprint); otherwise it recomputes under the lock, as it always did."""
    try:
        st = load_state(project, run_id)
        if st["phase"] == "done":
            return None
        target = PHASES[PHASES.index(st["phase"]) + 1]
        if target not in SCAN_TARGETS:
            return None
        return {"target": target, "token": _state_token(st), "problems": preconditions(project, st, target),
                "usage": usage_snapshot(project, st) if target == "done" else None}
    except Exception:           # whatever is odd is decided (and reported) by the locked path
        return None


def write_timing(project: Path, st: Dict[str, Any]) -> None:
    """The clocks of a finished run, in the feature folder. The run is already done; the caller ignores a failure.
    A phase gets a token mark only when a bound session's transcript dated a reply inside that phase."""
    tm = T.timings(st)
    # phase_next already stored the snapshot in st: reuse it instead of scanning the transcripts twice
    measured = st.get("usage") if isinstance(st.get("usage"), dict) else (usage_snapshot(project, st) or {})
    counted = measured.get("phases") if isinstance(measured.get("phases"), dict) else {}
    total_tokens = measured.get("total") if "total" in measured else None
    lines = ["# Timing", "", "- " + i18n.t(
        "monitor.ui_clock_total", total=T.compact_dur(tm["run"]) + T.token_mark(total_tokens))]
    for name in PHASES:
        if name not in tm["phases"]:
            continue
        tokens = counted.get(name) if name in counted else None
        lines.append("- " + T.stage_text(i18n.t("monitor.phase_" + name), int(tm["phases"][name]), tokens))
    write_text_atomic(paths.feature_dir(project, st["run_id"]) / "timing.md", "\n".join(lines) + "\n")


def phase_next(project: Path, run_id: str, note: str = "") -> Tuple[str, str]:
    pre = _precheck(project, run_id)
    with StateTxn(project, run_id) as st:
        cur = st["phase"]
        if cur == "done":
            raise OrkestraError("run is already done")
        target = PHASES[PHASES.index(cur) + 1]
        if target == "implement" and st.get("approval_mode", "autopilot") == "autopilot":
            rdir = paths.run_dir(project, run_id)
            approval = read_json(rdir / "approval.json")
            current_hash = spec_plan_hash(project, run_id)
            if isinstance(approval, dict) and approval.get("decision") == "reject":
                raise OrkestraError("the plan was rejected; revise spec/plan before continuing")
            if not isinstance(approval, dict) or approval.get("decision") != "approve" or approval.get("hash") != current_hash:
                plan_problems = preconditions(project, st, "approve")
                if plan_problems:
                    raise OrkestraError("cannot auto-approve the plan:\n  - " + "\n  - ".join(plan_problems))
                if st.get("plan_review_hash") != current_hash:
                    if st.get("plan_review_hash") or not _legacy_plan_review_is_current(project, st):
                        raise OrkestraError("spec/plan changed after plan review; return to plan and run plan-critic again")
                    st["plan_review_hash"] = current_hash
                rec = {"decision": "approve", "source": "autopilot",
                       "detail": "plan checks and critic passed; autopilot is enabled", "at": now_iso(),
                       "hash": current_hash}
                write_json_atomic(rdir / "approval.json", rec)
                ledger(project, run_id, "autopilot approved plan after checks and critic passed")
        fresh = pre is not None and pre["target"] == target and pre["token"] == _state_token(st)
        problems = pre["problems"] if fresh else preconditions(project, st, target)
        if problems:
            raise OrkestraError(f"cannot move {cur} → {target}:\n  - " + "\n  - ".join(problems))
        if target == "approve":
            st["plan_review_hash"] = spec_plan_hash(project, run_id)
        if target == "implement":
            _apply_approval(project, st)
            st["status"] = "active"
        elif st.get("status") == "awaiting_user" and st.get("approval_mode", "autopilot") == "autopilot":
            st["status"] = "active"
        if target == "done":
            st["status"] = "done"
            snap = pre["usage"] if fresh else usage_snapshot(project, st)
            if snap:
                st["usage"] = snap
        st["phase"] = target
        st["awaiting"] = None
        st["history"].append({"at": now_iso(), "from": cur, "to": target, "note": note})
    ledger(project, run_id, f"phase {cur} → {target}" + (f" — {note}" if note else ""))
    if target == "done":
        set_active_run(project, None)
        point_current(project, None)
        # Regenerable Playwright/Android dumps grow across runs; wipe them after delivery (fail-open).
        try:
            from . import artifacts as ART
            report = ART.clean_after_run(project)
            n = len(report.get("removed") or [])
            freed = int(report.get("freed_bytes") or 0)
            if n or report.get("skipped"):
                detail = report.get("reason") or f"{n} path(s), {freed} bytes"
                ledger(project, run_id, f"artifact cleanup: {detail}")
        except Exception:
            pass
        try:
            write_timing(project, st)
        except Exception:
            pass
        try:
            from . import memory as M
            M.remember_finished_run(project, st)
        except Exception:
            pass
    return cur, target


MAX_PHASE_BACKS = 25            # `phase back` calls one run may make: a run that went back this often is not converging


def phase_back(project: Path, run_id: str, target: str, reason: str) -> Tuple[str, str]:
    with StateTxn(project, run_id) as st:
        cur = st["phase"]
        status = str(st.get("status") or "")
        if status in ("blocked", "paused") or status in TERMINAL_STATUSES:
            # a block (deny loop, external dependency), a pause or a close is lifted by its own command, never by moving the phase
            raise OrkestraError(f"run '{run_id}' is {status}: `phase back` does not lift that. "
                                f"Ask the user; they resume or unblock it.")
        backs = sum(1 for h in (st.get("history") or []) if str(h.get("note") or "").startswith("back:"))
        if backs >= MAX_PHASE_BACKS:
            raise OrkestraError(f"run '{run_id}' already went back {backs} times: it is not converging. "
                                f"Run `orkestra block \"<why>\"` and tell the user.")
        if target not in BACK_EDGES.get(cur, []):
            raise OrkestraError(f"cannot go back {cur} → {target}; allowed: {BACK_EDGES.get(cur, [])}")
        if PHASES.index(target) < PHASES.index("implement") and st.get("approved"):
            st["approved"] = None
            appr = paths.run_dir(project, run_id) / "approval.json"
            if appr.exists():
                appr.rename(appr.with_name(f"approval-superseded-{int(time.time())}.json"))
        if PHASES.index(target) < PHASES.index("approve"):
            st.pop("plan_review_hash", None)
        st["phase"] = target
        st["status"] = "active"
        tasks = st.get("tasks") or {}
        # "mark": how much task work existed when the phase went back (tempo.next_action: was anything added since?)
        st["history"].append({"at": now_iso(), "from": cur, "to": target, "note": f"back: {reason}",
                              "mark": {"tasks": len(tasks), "reopens": sum(T.reopen_count(t) for t in tasks.values())}})
    ledger(project, run_id, f"phase back {cur} → {target}: {reason}")
    return cur, target


def _apply_approval(project: Path, st: Dict[str, Any]) -> None:
    rid = st["run_id"]
    fdir = paths.feature_dir(project, rid)
    rdir = paths.run_dir(project, rid)
    plan = read_text(fdir / "plan.md")
    fm, _ = PP.parse_frontmatter(plan)
    deps = fm.get("approved_dependencies") or []
    if not isinstance(deps, list):
        deps = [str(deps)]
    appr = read_json(rdir / "approval.json") or {}
    st["approved"] = {"hash": appr.get("hash"), "at": appr.get("at"), "source": appr.get("source"),
                      "deps": [str(d) for d in deps if d]}
    tasks: Dict[str, Any] = {}
    for t in PP.parse_tasks(plan):
        prev = (st.get("tasks") or {}).get(t.id) or {}
        # keep everything the run recorded about the task (started, review_seen, history …); the plan owns the rest
        tasks[t.id] = dict(prev, title=t.title, status=prev.get("status", "pending"), tdd=t.tdd, files=t.files,
                           depends=t.depends, red=prev.get("red"), green=prev.get("green"), notes=prev.get("notes", []))
    st["tasks"] = tasks
    adir = rdir / "approved"
    adir.mkdir(parents=True, exist_ok=True)
    for name in ("spec.md", "plan.md"):
        if (fdir / name).exists():
            shutil.copy2(fdir / name, adir / name)
    # stamp spec as approved for humans (frontmatter edit happens on the snapshot copy only)


def record_approval(project: Path, run_id: str, decision: str, source: str, detail: str = "") -> Optional[Dict[str, Any]]:
    """Record a real user decision from a hook; automatic approvals are written by phase_next."""
    rdir = paths.run_dir(project, run_id)
    st = load_state(project, run_id)
    if st.get("phase") != "approve" or st.get("approval_mode", "autopilot") != "manual":
        return None
    rec = {"decision": decision, "source": source, "detail": detail[:500], "at": now_iso(),
           "hash": spec_plan_hash(project, run_id)}
    write_json_atomic(rdir / "approval.json", rec)
    with StateTxn(project, run_id) as s:
        s["awaiting"] = None
        if s.get("status") == "awaiting_user":
            s["status"] = "active"
    ledger(project, run_id, f"user {decision} via {source}" + (f": {detail[:120]}" if detail else ""))
    return rec


# ---------------------------------------------------------------- status changes

class _Unchanged(Exception):
    """Raised inside the state transaction when a status call changes nothing: the file is not rewritten (its
    mtime is a liveness signal), no ledger line is written."""


def _status_unchanged(st: Dict[str, Any], status: str, reason: str, source: str) -> bool:
    """Is `status` (with this reason) what the run already says? A repeated pause / unblock / abort is a no-op; a wait or
    a block with a different question or reason still updates the record."""
    if (st.get("status") or "") != status:
        return False
    if status == "awaiting_user":
        aw = st.get("awaiting") or {}
        return aw.get("question") == reason and (aw.get("source") or "") == (source or "")
    if status == "blocked":
        return (st.get("blocked") or {}).get("reason") == reason
    return True


def set_status(project: Path, run_id: str, status: str, reason: str = "", source: str = "",
               who: Optional[Dict[str, Any]] = None) -> bool:
    """Change a run's status. True = changed (or its reason updated), False = it already said that (nothing written:
    no ledger line, no state rewrite). A closed run stays closed. `who` ({session_id, agent_type, agent_id}) names the
    chat that asked: a visible `status` row (who, from, to) goes into the run's activity feed."""
    prev = ""
    try:
        with StateTxn(project, run_id) as st:
            prev = str(st.get("status") or "")
            if prev in TERMINAL_STATUSES and status != prev:
                raise OrkestraError(f"run '{run_id}' is {prev}: a closed run cannot be set to {status}")
            if status == "awaiting_user" and st.get("approval_mode", "autopilot") != "manual":
                raise OrkestraError("autopilot runs cannot wait for user input; decide conservatively or block on a real external dependency")
            if _status_unchanged(st, status, reason, source):
                raise _Unchanged()
            _apply_status(project, st, status, reason, source)
    except _Unchanged:
        if status in ("aborted", "paused"):
            set_active_run(project, None)
            point_current(project, None)
        return False
    ledger(project, run_id, f"status {prev} → {status}" + (f": {reason}" if reason else ""))
    if status in ("aborted", "paused"):
        # Paused must leave the workspace index too: otherwise a foreign chat can re-lease the run.
        set_active_run(project, None)
        point_current(project, None)
    if who is not None and prev != status:
        record_status_row(project, run_id, prev, status, who)
    return True


def record_status_row(project: Path, run_id: str, prev: str, status: str, who: Dict[str, Any]) -> None:
    """The visible trace of a status change in the run's activity feed: who asked, from, to (best effort)."""
    from . import activity as A
    from .engines import short_sid
    by = short_sid(str(who.get("session_id") or "")) or "cli"
    A.record(project, run_id, dict(who, tool_name=""), "status", i18n.t("runs.status_row", by=by, src=prev, dst=status))


def _apply_status(project: Path, st: Dict[str, Any], status: str, reason: str, source: str) -> None:
    st["status"] = status
    if status == "aborted":
        snap = usage_snapshot(project, st)
        if snap:
            st["usage"] = snap
    if status == "awaiting_user":
        st["awaiting"] = {"question": reason, "at": now_iso()}
        if source:                                  # "hook": set by the AskUserQuestion hook, ends with that call
            st["awaiting"]["source"] = source
    elif status == "blocked":
        st["blocked"] = {"reason": reason, "at": now_iso()}
    elif status == "active":
        st["blocked"] = None
        st["awaiting"] = None


# ---------------------------------------------------------------- tasks & TDD

def _need_task(st: Dict[str, Any], tid: str) -> Dict[str, Any]:
    t = (st.get("tasks") or {}).get(tid)
    if not t:
        raise OrkestraError(f"unknown task {tid} (tasks are loaded from plan.md at approval)")
    return t


def task_start(project: Path, run_id: str, tid: str, agent: str = "", force: str = "") -> List[str]:
    """Returns warnings. Two tasks may be in progress at once only when their file lists do not overlap."""
    warnings: List[str] = []
    with StateTxn(project, run_id) as st:
        if st["phase"] not in ("implement", "verify", "review"):
            raise OrkestraError(f"tasks can start only after approval (phase is {st['phase']})")
        t = _need_task(st, tid)
        if t.get("status") in ("done", "deferred"):
            raise OrkestraError(f"{tid} is already {t['status']}. Another round starts with `orkestra task reopen {tid} "
                                f"\"<why>\"` (after a review FAIL), not with task start.")
        if t.get("status") == "blocked" and T.is_autopilot(st) and str((t.get("notes") or [""])[-1]).startswith(T.CAP_BLOCK):
            raise OrkestraError(f"{tid} hit the review round limit and autopilot has no user to rule on it: "
                                f"`orkestra task defer {tid} \"<why>\"` (the final review triages it) or `orkestra block \"<why>\"`.")
        if t.get("status") == "in_progress":
            return [f"{tid} is already in progress (started {t.get('started')}) — nothing changed; continue with "
                    f"`orkestra next`."]
        for dep in t.get("depends") or []:
            if (st["tasks"].get(dep) or {}).get("status") not in ("done", "deferred"):
                raise OrkestraError(f"{tid} depends on {dep}, which is not done")
        others = sorted(o for o, v in st["tasks"].items() if o != tid and v.get("status") == "in_progress")
        clash = T.overlap(st["tasks"], tid, others)
        if clash and not force:
            o, shared = clash[0]
            raise OrkestraError(f"{tid} shares {', '.join(shared[:3])} with {o}, which is still in progress. One working "
                                f"tree: finish {o} first (`orkestra next`). If the overlap is harmless, add "
                                f"--force \"<why>\" (recorded in the ledger).")
        if clash:
            warnings.append(f"started despite shared files with {clash[0][0]}: {force}")
        elif others:
            warnings.append(f"{', '.join(others)} also in progress (disjoint files). Parallel implementers are fine only "
                            f"while their files and tests stay disjoint; run gates per task, never the full suite.")
        t["status"] = "in_progress"
        t["agent"] = agent
        t["started"] = now_iso()
        t.setdefault("first_started", t["started"])
    ledger(project, run_id, f"{tid} started" + (f" by {agent}" if agent else "")
           + (f" · forced over shared files: {force}" if force and clash else ""))
    return warnings


def task_done(project: Path, run_id: str, tid: str, note: str = "") -> bool:
    """False when the task was already done (idempotent: nothing is recorded twice)."""
    seen = _need_task(load_state(project, run_id), tid)
    if seen.get("status") == "done":
        return False
    rdir = paths.run_dir(project, run_id)
    # the change scan runs before the lock: it is file work, the lock only guards the task record
    tests_changed = bool(E.test_files_state(project, rdir)) if seen.get("tdd") == "required" else None
    with StateTxn(project, run_id) as st:
        t = _need_task(st, tid)
        if t.get("tdd") == "required":
            if not (tests_changed if tests_changed is not None else E.test_files_state(project, rdir)):
                raise OrkestraError(f"{tid}: TDD task but no test file was added or changed in this run. Write the "
                                    f"test first (or mark the task `TDD: n/a (reason)` in the plan and re-approve).")
            if not (t.get("green") or {}).get("ok"):
                raise OrkestraError(f"{tid}: no passing test evidence. Run `orkestra tdd green {tid} -- <test cmd>`.")
            if not (t.get("red") or {}).get("ok"):
                raise OrkestraError(f"{tid}: no failing-first evidence. TDD requires `orkestra tdd red {tid} -- <cmd>` "
                                    f"before the implementation (or mark the task `TDD: n/a (reason)` in the plan and re-approve).")
        why = review_gap(project, run_id, st, tid, t)
        if why:
            raise OrkestraError(why)
        t["status"] = "done"
        t["done_at"] = now_iso()
        if note:
            t.setdefault("notes", []).append(note)
    ledger(project, run_id, f"{tid} done" + (f" — {note}" if note else ""))
    return True


def is_adhoc(t: Dict[str, Any]) -> bool:
    """Added after approval (`task add`): covered by the final review and its triage, not by a per-task review."""
    return bool(t.get("adhoc")) or any(str(n).startswith("ad-hoc") for n in (t.get("notes") or [])[:1])


def review_gap(project: Path, run_id: str, st: Dict[str, Any], tid: str, t: Dict[str, Any]) -> str:
    """'' when the task may be closed; else what is missing. M/L runs: every plan task needs a PASS review of the
    current round (the playbook always said so — a weak orchestrator simply ran `task done` without dispatching the
    reviewer, or with a FAIL on file). Off: ~/.orkestra/config.json → tempo.task_review_required = false."""
    if st.get("size") in ("XS", "S") or is_adhoc(t):
        return ""
    if (paths.global_config().get("tempo") or {}).get("task_review_required", True) is False:
        return ""
    rv = T.task_review(paths.feature_dir(project, run_id), tid)
    if not rv or float(rv.get("mtime") or 0) <= float(t.get("review_seen") or 0):
        return (f"{tid}: no review of this round. Size {st.get('size')} runs need one per task: dispatch "
                f"orkestra:reviewer with `scope: task {tid}` (it writes reviews/{tid}.review.md), then `task done`.")
    if rv.get("verdict") not in ("PASS", "PASS_WITH_NOTES"):
        if rv.get("verdict") is None:
            return (f"{tid}: {rv.get('name')} has no `VERDICT: PASS | PASS_WITH_NOTES | FAIL` line — ask the reviewer "
                    f"to end the file with one.")
        return (f"{tid}: the review says {rv.get('verdict')} ({rv.get('name')}). Fix it first: "
                f"`orkestra task reopen {tid} \"review round N\"`, then `orkestra brief {tid}` for the implementer.")
    return ""


def task_set(project: Path, run_id: str, tid: str, status: str, reason: str) -> None:
    with StateTxn(project, run_id) as st:
        t = _need_task(st, tid)
        t["status"] = status
        if status == "deferred":
            t["deferred_reason"] = reason
        t.setdefault("notes", []).append(f"{status}: {reason}")
    ledger(project, run_id, f"{tid} → {status}: {reason}")


def _human_ruling_after(project: Path, run_id: str, since: float) -> bool:
    """Is there a HUMAN row (a user prompt or an answered question) newer than `since`? The only thing that may lift
    the review-round cap in manual mode: --force text is written by the model itself and proves nothing."""
    from . import activity as A
    return any(r.get("kind") in ("user", "answer") and float(r.get("ts") or 0) > since for r in A.read_rows(project, run_id))


def task_reopen(project: Path, run_id: str, tid: str, reason: str, force: str = "") -> None:
    """Reopen for another round: evidence is reset so the next round starts with a fresh red test.

    Tempo rules (deterministic; `--force "<why>"` overrides and is written to the ledger):
      * a task whose latest review says PASS is not reopened for minors — they go to the final triage
      * after MAX_REVIEW_ROUNDS - 1 reopen rounds the next one needs the user's ruling: in manual mode --force counts
        only when a user / answer row is newer than the last reopen; in autopilot there is no user, --force cannot lift
        the cap, and the task becomes blocked with the question recorded (defer or block are the only ways on)
      * a reopen for missing red evidence is bookkeeping: not a review round, at most twice per task
    """
    fdir = paths.feature_dir(project, run_id)
    refuse = ""
    red_missing = False
    with StateTxn(project, run_id) as st:
        t = _need_task(st, tid)
        rounds = T.reopen_count(t)
        auto = T.is_autopilot(st)
        rv = T.task_review(fdir, tid)
        red_missing = bool(force) and force.strip().lower().startswith("red evidence missing") \
            and t.get("tdd") == "required" and bool((t.get("green") or {}).get("ok")) and not (t.get("red") or {}).get("ok") \
            and sum(1 for n in (t.get("notes") or []) if str(n).startswith(T.RED_MISSING)) < 2
        if rv:
            t["review_seen"] = rv.get("mtime")      # this verdict is now applied; the next round needs a newer review
        if not force and rv.get("verdict") in ("PASS", "PASS_WITH_NOTES"):
            raise OrkestraError(
                f"{tid}: the latest review ({rv.get('name')}) says {rv['verdict']}. A PASS is not reopened for minors: "
                f"`orkestra task done {tid} \"review: PASS\"` and carry them with `orkestra task note {tid} \"minor: …\"` "
                f"(the final review triages them). If a fix truly cannot wait: --force \"<why>\".")
        capped = not red_missing and rounds >= T.MAX_REVIEW_ROUNDS - 1
        if capped and auto:
            refuse = (f"{tid}: already reopened {rounds}×, round {rounds + 1} would exceed the limit "
                      f"({T.MAX_REVIEW_ROUNDS}). Autopilot has no user to rule on it, so --force cannot lift it. The task is now "
                      f"blocked with the question recorded. Next: `orkestra task defer {tid} \"<why>\"` (the final review "
                      f"triages it) or `orkestra block \"<why>\"`.")
            t["status"] = "blocked"
            t.setdefault("notes", []).append(f"{T.CAP_BLOCK} ({rounds + 1} rounds failed review): no safe automatic ruling, "
                                             f"defer or block")
        elif capped and not force:
            raise OrkestraError(
                f"{tid}: already reopened {rounds}×. Round {rounds + 1} failed too → stop and put the trade-off to the "
                f"user (AskUserQuestion), then pass the ruling: `orkestra task reopen {tid} --force \"<user's ruling>\"` "
                f"or `orkestra task defer {tid} \"<why>\"`.")
        elif capped and not _human_ruling_after(project, run_id, float(t.get("last_reopen_at") or 0)):
            raise OrkestraError(
                f"{tid}: --force needs the user's ruling, and no user answer was recorded after the last reopen. Put the "
                f"trade-off to the user (AskUserQuestion), then pass their ruling with --force, or "
                f"`orkestra task defer {tid} \"<why>\"`.")
        if not refuse:
            for k in ("red", "green"):
                if t.get(k):
                    t.setdefault("history", []).append({k: t[k]})
                t[k] = None
            t["status"] = "pending"
            t["last_reopen_at"] = time.time()
            t.setdefault("notes", []).append(
                ((T.RED_MISSING if red_missing else "reopened") + f": {reason}") + (f" [forced: {force}]" if force else ""))
    if refuse:
        ledger(project, run_id, f"{tid} blocked: review round limit reached ({rounds + 1} rounds), autopilot has no user ruling")
        raise OrkestraError(refuse)
    ledger(project, run_id, f"{tid} reopened (evidence reset): {reason}" + (f" · forced: {force}" if force else ""))


def task_add(project: Path, run_id: str, tid: str, title: str, tdd: str = "required",
             files: Optional[List[str]] = None) -> None:
    """Ad-hoc task (e.g. a review fix R-001) after approval; recorded in the ledger and the report. `files` is the
    scope the task touches: the overlap check of `task start` and the parallel-dispatch advice read it."""
    if not re.match(r"^[A-Z][A-Z0-9-]{1,20}$", tid):
        raise OrkestraError("task id must look like FIX-R001 or T009")
    with StateTxn(project, run_id) as st:
        if st["phase"] not in ("implement", "verify", "review"):
            raise OrkestraError(f"tasks can be added only after approval (phase {st['phase']})")
        if tid in (st.get("tasks") or {}):
            raise OrkestraError(f"{tid} already exists")
        st.setdefault("tasks", {})[tid] = {"title": title, "status": "pending", "tdd": tdd, "files": list(files or []), "depends": [],
                                           "red": None, "green": None, "adhoc": True,
                                           "notes": ["ad-hoc (added after approval)"]}
    ledger(project, run_id, f"{tid} added after approval: {title} (tdd {tdd})" + (f" · files {', '.join(files)}" if files else ""))


def task_note(project: Path, run_id: str, tid: str, note: str) -> None:
    with StateTxn(project, run_id) as st:
        _need_task(st, tid).setdefault("notes", []).append(note)


def _norm_cmd(c: str) -> str:
    return re.sub(r"\s+", " ", (c or "").strip())


# Deterministic red classification, first match wins. "refuse" classes are not a test failing: they need --why.
# Anchored to collection-level lines (a quoted "SyntaxError" inside an assertion message is not one).
_RED_REFUSE = re.compile(
    r"^\s*(?:E\s+)?(?:\w+\.)*(?:SyntaxError|IndentationError)\b|^[\s=]*collected 0 items|^[\s=]*no tests ran"
    r"|^\s*No tests found|^\s*Ran 0 tests|^\s*(?:testing: warning: )?no tests to run|^\s*Test Suites: 0 total",
    re.I | re.M)
# A test that ran and failed. When one is present the red is real whatever else the output says (other packages
# with no tests, a SyntaxError quoted in a message).
_RED_REAL_FAIL = re.compile(
    r"AssertionError|^\s*FAILED\s+\S|^\s*FAIL:|--- FAIL|expect\(received\)|^\s*Tests:[^\n]*\b[1-9]\d* failed"
    r"|^[\s=]*[^\n]*\b[1-9]\d* failed\b|failures=[1-9]", re.M)
_RED_CLASSES = (
    ("infra", re.compile(r"command not found|could not start the command|ENOENT"
                         r"|connection refused|Permission denied", re.I)),
    ("missing_symbol", re.compile(r"ModuleNotFoundError|ImportError|No module named|NameError|AttributeError"
                                  r"|undefined: |is not defined|is not a function|Cannot find module|cannot find symbol"
                                  r"|no required module provides", re.I)),
    ("collection_or_syntax", re.compile(r"ERROR collecting|errors? during collection|build failed|\[build failed\]"
                                        r"|cannot use .* as .* value|expected declaration", re.I)),
    ("assertion", re.compile(r"AssertionError|assert |FAIL:|FAILED|Expected|expect\(|--- FAIL|\bFAIL\b", re.I)),
)


def classify_red(out: str) -> Tuple[str, bool]:
    """(reason, refuse) for a failing test run: reason is assertion | missing_symbol | collection_or_syntax | infra.
    refuse is True when the output shows no test actually failed (syntax error, nothing collected or run)."""
    if _RED_REFUSE.search(out) and not _RED_REAL_FAIL.search(out):
        return "collection_or_syntax", True
    for name, rx in _RED_CLASSES:
        if rx.search(out):
            return name, False
    return "assertion", False


def tdd_record(project: Path, run_id: str, tid: str, kind: str, cmd: List[str], why: str = "") -> Dict[str, Any]:
    """Run the test command and record red/green evidence.

    Anti-gaming rules (deterministic):
      * red must be a real failure (not success, timeout or command-not-found)
      * green must run the SAME command as red, unless --why explains the deviation
      * test files must not change between red and green (that test version was never seen failing),
        unless --why explains it; deviations are recorded and shown in the report
    """
    if not cmd:
        raise OrkestraError("usage: orkestra tdd red|green T001 -- <test command>")
    st = load_state(project, run_id)
    t = _need_task(st, tid)
    if kind == "red" and (t.get("green") or {}).get("ok"):
        raise OrkestraError(f"{tid} already has green evidence; red must come first "
                            f"(the orchestrator can `orkestra task reopen {tid}` for another round)")
    rdir_run = paths.run_dir(project, run_id)
    shell_cmd = shlex.join(cmd) if len(cmd) > 1 else cmd[0]
    deviations: List[str] = []
    owned_elsewhere: List[str] = []
    foreign_note = ""
    red = t.get("red") or {}
    if kind == "green" and red.get("ok") and t.get("tdd") == "required":
        if _norm_cmd(red.get("cmd", "")) != _norm_cmd(shell_cmd):
            if not why:
                raise OrkestraError(f"{tid}: green must run the same command as red (`{red.get('cmd')}`). If a different "
                                    f"command is genuinely needed, add --why \"<reason>\" (it is shown in the report).")
            deviations.append(f"green command differs from red: {why}")
        before = red.get("test_state") or {}
        now_state = E.test_files_state(project, rdir_run)
        changed_tests = sorted(k for k in set(before) | set(now_state) if before.get(k) != now_state.get(k))
        # A test file that belongs to a task which was WORKING between this red and now (in progress, or finished
        # after the red) changed because of that task — its own red/green covers it. A file of any other task
        # (done long ago, not started) is not exempt: rewriting it is exactly what this rule exists to catch.
        mine = set(t.get("files") or [])
        red_at = T._ts(red.get("at")) or 0.0
        owners: Dict[str, str] = {}
        for o, v in (st.get("tasks") or {}).items():
            concurrent = v.get("status") == "in_progress" or (T._ts(v.get("done_at")) or 0.0) >= red_at > 0
            if o != tid and concurrent:
                for f in v.get("files") or []:
                    owners.setdefault(f, o)
        owned_elsewhere = [k for k in changed_tests if k in owners and k not in mine and k not in shell_cmd]
        if owned_elsewhere:
            changed_tests = [k for k in changed_tests if k not in owned_elsewhere]
            foreign_note = ", ".join(f"{k} ({owners[k]})" for k in owned_elsewhere)
        if changed_tests:
            if not why:
                raise OrkestraError(f"{tid}: test file(s) changed after the red run: {', '.join(changed_tests)}. That test "
                                    f"version was never seen failing. Re-run red with the final test (before the fix), or add "
                                    f"--why \"<reason>\" (shown in the report).")
            deviations.append(f"tests changed after red ({', '.join(changed_tests)}): {why}")
    elif why:
        deviations.append(why)
    rdir = rdir_run / "evidence"
    rdir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        cp = _run_shell_or_list(shell_cmd, cwd=str(project), timeout=1800, env=_ci_env(), argv=cmd)
        code = cp.returncode
        out = (cp.stdout or b"").decode("utf-8", "replace") + (cp.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        code, out = 124, "[orkestra] TIMEOUT"
    except OSError as exc:                  # could not start: 127/126, which red refuses as "not a real failure"
        code, out = G._spawn_failure(exc)
    log = rdir / f"{tid}-{kind}-{int(started)}.log"
    write_text_atomic(log, f"$ {shell_cmd}\n\n{out}\n[exit {code}]\n")
    # red must be a real test failure: not success, not timeout, not "command not found"/not executable
    ok = (code not in (0, 124, 126, 127)) if kind == "red" else code == 0
    red_reason = ""
    if kind == "red" and ok:
        red_reason, refuse = classify_red(out)
        if refuse and not why:
            ok = False
            red_reason = ""
            deviations.append("red refused: the output shows no test failing (syntax error, nothing collected or run); "
                              "fix the test so it runs and fails, or pass --why \"<reason>\"")
    rec: Dict[str, Any] = {"ok": ok, "exit": code, "cmd": shell_cmd, "log": str(log), "at": now_iso(),
                           "tail": tail_lines(out, 25, 2500), "deviations": deviations}
    if red_reason:
        rec["reason"] = red_reason
    if kind == "red":
        rec["test_state"] = E.test_files_state(project, rdir_run)
    if owned_elsewhere:                     # changed by a concurrent task that owns them — that task's evidence covers them
        rec["foreign_tests_changed"] = owned_elsewhere
        rec["foreign_note"] = f"tests changed by a concurrent task, not counted: {foreign_note}"
    with StateTxn(project, run_id) as s:
        _need_task(s, tid)[kind] = rec
    ledger(project, run_id, f"{tid} tdd {kind}: exit {code} → {'OK' if ok else 'NOT OK'} ({shell_cmd})"
                            + (f" · deviation: {'; '.join(deviations)}" if deviations else "")
                            + (f" · concurrent-task tests changed (exempt): {foreign_note}" if foreign_note else ""))
    return rec


def run_evidence(project: Path, run_id: str, label: str, cmd: List[str]) -> Dict[str, Any]:
    """Record an ad-hoc verification command (e.g. a manual CLI check) as a real log the report can cite."""
    if not cmd:
        raise OrkestraError("usage: orkestra evidence <label> -- <command>")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:40] or "check"
    shell_cmd = shlex.join(cmd) if len(cmd) > 1 else cmd[0]
    rdir = paths.run_dir(project, run_id) / "evidence"
    rdir.mkdir(parents=True, exist_ok=True)
    try:
        cp = _run_shell_or_list(shell_cmd, cwd=str(project), timeout=900, env=_ci_env(), argv=cmd)
        code = cp.returncode
        out = (cp.stdout or b"").decode("utf-8", "replace") + (cp.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        code, out = 124, "[orkestra] TIMEOUT"
    except OSError as exc:
        code, out = G._spawn_failure(exc)
    log = rdir / f"{safe}-{int(time.time())}.log"
    write_text_atomic(log, f"$ {shell_cmd}\n\n{out}\n[exit {code}]\n")
    ledger(project, run_id, f"evidence {safe}: exit {code} ({shell_cmd}) → {log.name}")
    return {"exit": code, "log": str(log), "rel": f"evidence/{log.name}", "tail": tail_lines(out, 30, 3000)}


def _ci_env() -> Dict[str, str]:
    import os
    env = dict(os.environ)
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("FORCE_COLOR", "0")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


# ---------------------------------------------------------------- gates (state wiring)

def _gate_parallel(project: Path) -> Any:
    cfg = paths.project_config(project)
    return cfg["gates_parallel"] if "gates_parallel" in cfg else paths.global_config().get("gates_parallel", 1)


def _file_sigs(project: Path, changed: List[Dict[str, Any]]) -> Dict[str, str]:
    """{changed path: size:mtime_ns:change}: a cheap fingerprint of the working-tree state a verify judged."""
    out: Dict[str, str] = {}
    for item in changed:
        try:
            st = (project / item["path"]).stat()
            out[item["path"]] = f"{st.st_size}:{st.st_mtime_ns}:{item.get('change')}"
        except OSError:
            out[item["path"]] = f"gone:{item.get('change')}"
    return out


def _stamp_of(files: Dict[str, str]) -> str:
    import hashlib
    return hashlib.sha256("\n".join(f"{k}\0{v}" for k, v in sorted(files.items())).encode("utf-8")).hexdigest()[:16]


def _full_verify_forced(project: Path) -> bool:
    import os
    if os.environ.get("ORKESTRA_GATES_FULL", "").strip().lower() in ("1", "true", "yes"):
        return True
    cfg = paths.project_config(project)
    val = cfg["gates_rerun_failed_baseline"] if "gates_rerun_failed_baseline" in cfg else \
        paths.global_config().get("gates_rerun_failed_baseline", False)
    return val is True


BASELINE_SKIP_SECONDS = 120         # a baseline failure at least this slow is not re-run by `gate verify`


def gates_baseline(project: Path, run_id: str) -> Tuple[List[Dict[str, Any]], str]:
    cfg = paths.project_config(project)
    rdir = paths.run_dir(project, run_id)
    part = rdir / "gates-baseline.partial.json"     # progress; gates-baseline.json only ever holds a COMPLETE baseline

    def keep(rs: List[Dict[str, Any]]) -> None:
        G.save_results(part, rs, {"ts": time.time(), "partial": True})

    results = G.run_all(project, cfg.get("gates") or [], rdir / "evidence", "baseline",
                        parallel=_gate_parallel(project), on_result=keep)
    G.save_results(rdir / "gates-baseline.json", results, {"ts": time.time()})
    try:
        part.unlink()
    except OSError:
        pass
    ok = sum(1 for r in results if r["ok"])
    ledger(project, run_id, f"gate baseline: {ok}/{len(results)} passing")
    return results, G.format_results(results)


def gates_verify(project: Path, run_id: str, resume: bool = False, full: bool = False) -> Tuple[Dict[str, Any], str]:
    cfg = paths.project_config(project)
    rdir = paths.run_dir(project, run_id)
    base = (read_json(rdir / "gates-baseline.json") or {}).get("results") or []
    bmap = {r["name"]: r for r in base}
    label = f"verify-{int(time.time())}"
    full = full or _full_verify_forced(project)
    gates = [g for g in (cfg.get("gates") or []) if g.get("enabled") is not False]
    files = _file_sigs(project, S.changed_files(project, rdir))
    stamp = _stamp_of(files)
    part = rdir / "gates-verify.partial.json"
    reuse: Dict[str, Dict[str, Any]] = {}
    if resume and not full:                         # results of an interrupted verify, only for the same tree state
        prev = read_json(part)
        if isinstance(prev, dict) and prev.get("stamp") == stamp:
            names = {g["name"] for g in gates}
            for r in prev.get("results") or []:
                if r.get("name") in names:
                    reuse[r["name"]] = dict(r, resumed=True)
    run_list: List[Dict[str, Any]] = []
    last = read_json(rdir / "gates-verify.json")
    lres = {r["name"]: r for r in (last.get("results") or [])} if isinstance(last, dict) else {}
    lfiles = last.get("files") if isinstance(last, dict) else None
    for g in cfg.get("gates") or []:
        name = g.get("name")
        b = bmap.get(name)
        if g.get("enabled") is False or name in reuse or full:
            run_list.append(g)
            continue
        if b is not None and not b.get("ok"):
            dur = float(b.get("duration_s") or 0)
            if b.get("timed_out") or dur >= BASELINE_SKIP_SECONDS:
                # it failed at baseline: the regression rule can never blame the run for it, re-running only burns time
                reuse[name] = {"name": name, "cmd": g.get("cmd"), "kind": g.get("kind", "other"), "exit": b.get("exit", 1),
                               "ok": False, "timed_out": bool(b.get("timed_out")), "duration_s": 0,
                               "log": b.get("log", ""), "tail": b.get("tail", ""), "at": now_iso(),
                               "skipped": "failed at baseline", "baseline_duration_s": dur}
                run_list.append(g)
                continue
            # failed fast at baseline: still run (it may be fixed), within a bounded budget
            run_list.append(dict(g, timeout=min(int(g.get("timeout", G.DEFAULT_TIMEOUT)), max(60, int(dur * 1.5) + 1))))
            continue
        prev_r = lres.get(name)
        pats = g.get("paths")
        if pats and isinstance(pats, list) and isinstance(lfiles, dict) and prev_r and prev_r.get("ok") \
                and not prev_r.get("skipped") and prev_r.get("cmd") == g.get("cmd"):
            dirty = any(G.glob_match(str(pt), f) for f in set(files) | set(lfiles) if files.get(f) != lfiles.get(f)
                        for pt in pats)
            if not dirty:
                reuse[name] = dict(prev_r, carried_from=prev_r.get("carried_from") or last.get("label") or last.get("at"),
                                   at=now_iso())
        run_list.append(g)

    def keep(rs: List[Dict[str, Any]]) -> None:
        G.save_results(part, rs, {"ts": time.time(), "stamp": stamp, "label": label, "partial": True})

    results = G.run_all(project, run_list, rdir / "evidence", label, parallel=_gate_parallel(project),
                        on_result=keep, reuse=reuse)
    comparison = G.compare(base, results)
    # stamp AFTER the gates ran: files the gates themselves write (junit.xml, caches) must not look like new edits
    G.save_results(rdir / "gates-verify.json", results, {"comparison": comparison, "ts": time.time(), "label": label,
                                                         "stamp": stamp, "files": files})
    try:
        part.unlink()
    except OSError:
        pass
    notes = ""
    if any(r.get("skipped") for r in results):
        notes += " · not re-run (failed at baseline): " + ", ".join(r["name"] for r in results if r.get("skipped"))
    if any(r.get("carried_from") for r in results):
        notes += " · carried: " + ", ".join(r["name"] for r in results if r.get("carried_from"))
    ledger(project, run_id, f"gate verify: {'NO REGRESSIONS' if comparison['ok'] else 'REGRESSION'} "
                            f"{comparison}{notes}")
    return comparison, G.format_results(results, comparison, base)


def finish_marker(project: Path, run_id: str) -> str:
    return unbind_marker(project, run_id)


def start_marker(project: Path, run_id: str) -> str:
    return bind_marker(project, run_id)
