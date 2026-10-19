"""`orkestra` CLI — the only writer of run state. Agents call it via Bash.

Exit codes: 0 ok · 1 check failed (e.g. regression) · 2 usage/precondition error.
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from . import engines as EN
from . import evidence as E
from . import gates as G
from . import i18n
from . import intent
from . import memory as M
from . import paths
from . import planparse as PP
from . import runs as R
from . import snapshot as S
from . import tempo as T
from .cliparse import build_parser, hoist_globals, split_remainder  # noqa: F401  (one parser: CLI + guards)
from .cliparse import parse as parse_argv
from .cliparse import requested_lang
from .messages import protocol as PR
from .state import (PHASES, OrkestraError, bind_marker, load_state, project_active_run, unbind_marker)
from .util import read_json, read_text
from .workkind import kind_of

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
PLAYBOOKS = PLUGIN_ROOT / "skills" / "run" / "references"
TEMPLATES = PLUGIN_ROOT / "templates"

NEXT_ACTION = {
    "intake": "Classify the request (size XS/S/M/L, project, risk) into request.md → `orkestra phase next`.",
    "discover": "Load memory, run scouts (parallel, read-only) → context.md; confirm gates; `orkestra gate baseline` → `orkestra phase next`.",
    "plan": "orkestra:architect writes plan.md → orkestra:plan-critic → fix until VERDICT: PASS → `orkestra plan check` → `orkestra phase next`.",
    "implement": "For each task in dependency order: brief → orkestra:implementer (TDD) → orkestra:reviewer → `orkestra task done`. Then `orkestra phase next`.",
    "verify": "`orkestra gate verify` (no regressions) → orkestra:verifier writes verification.md (VERDICT: PASS) → `orkestra phase next`.",
    "review": "Parallel final review (reviewer + adversary) → triage every finding in reviews/triage.md → fix via implementer → re-verify → `orkestra phase next`.",
    "deliver": "Write report.md, update memory (feature-add, specs, decisions, learnings) → `orkestra phase next` (done).",
    "done": "Run complete.",
}


def phase_goal(phase: str, autopilot: bool = False) -> str:
    """One-line goal of a phase (model-facing, English). The two phases that name a heading or an AskUserQuestion
    prompt render it in the current language; every parser and recognizer accepts both. In autopilot there is no user
    to ask or to approve: the text says what to do instead."""
    if autopilot and phase == "clarify":
        return ("Autopilot: infer conservative defaults from the request, codebase and project memory; record each material "
                f"assumption in spec.md → ## {i18n.heading('clarifications')}; never ask → `orkestra phase next`. "
                "With no safe default: `orkestra block \"<reason>\"`.")
    if autopilot and phase == "approve":
        return "Autopilot: no approval question. `orkestra phase next` records the hash-bound policy approval after the plan checks."
    if phase == "clarify":
        return ("Ask only outcome-changing questions (AskUserQuestion; max 4 per round, ≤2 rounds) → spec.md with "
                f"## {i18n.heading('clarifications')} → `orkestra phase next`.")
    if phase == "approve":
        return (f"Show the plan summary and ask for approval ({PR.prompt_spec('approve', i18n.lang())}). "
                "Hooks record the answer → `orkestra phase next`.")
    return NEXT_ACTION.get(phase, "")


_ASKER = EN.detect_env()       # the agent whose shell runs this command (None: an ordinary terminal)


_footnoted = False


def _out(text: str = "") -> None:
    global _footnoted
    if _ASKER == "cursor" and "--json" not in sys.argv:     # same words, Cursor's names (machine output stays as it is)
        adapted = EN.adapt_text(text, "cursor", footnote=not _footnoted)
        _footnoted = _footnoted or EN.ASK_FOOTNOTE in adapted      # said once per command
        text = adapted
    sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))


def _bind_here(project: Path, rid: str) -> str:
    """Cursor tells the agent's shell which conversation it is: bind it now (Claude Code sessions are bound by the
    PostToolUse hook from the marker line; in Cursor that hook is a second, equivalent path). A conversation that still
    drives another live run keeps it (the same rule as the hook's: state.try_bind); the returned text says so."""
    raw = EN.env_session_id()
    if not raw:
        return ""
    from . import state as ST
    if ST.child_binding(raw):
        return ""                                   # a subagent's shell: its parent drives the run
    try:
        sid = EN.session_key("cursor", raw)
        lease = float((paths.global_config().get("watchdog") or {}).get("lease_seconds") or 120)
        res = ST.try_bind(sid, project, rid, engine="cursor", lease=lease)
    except Exception:
        return ""
    if res.get("result") != "kept":
        return ""
    prev = res["prev"]
    if not res.get("duplicate"):
        ST.ledger(project, rid, i18n.t("dispatch.bind_kept", sid=EN.short_sid(sid), kept_run=prev["run_id"],
                                       kept_project=prev["project"], age=res["age"]))
    return (f"NOTE: this conversation is still bound to run {prev['run_id']} ({prev['project']}), active {res['age']} s "
            f"ago, so {rid} ({project}) was NOT bound here: it has no driver from this chat, and its guards and the "
            f"Stop nudges do not apply. To drive {rid} from here: `orkestra pause -p {prev['project']}`, then "
            f"`orkestra resume -p {project}`. Otherwise tell the user.")


def _unbind_here(project: Path, rid: str) -> None:
    """Drop this Cursor conversation bind to rid immediately (pause/abort).
    Claude Code still relies on the marker parsed by PostToolUse; Cursor gets both
    so Stop cannot keep driving after pause."""
    raw = EN.env_session_id()
    if not raw:
        return
    from . import state as ST
    if ST.child_binding(raw):
        return
    try:
        sid = EN.session_key("cursor", raw)
        data = read_json(paths.sessions_dir() / f"{ST._safe(sid)}.json")
        if not isinstance(data, dict):
            return
        if data.get("run_id") != rid or Path(str(data.get("project") or "")) != project:
            return
        ST.unbind_session(sid)
    except Exception:
        pass


def _require_cursor_hooks() -> None:
    """`orkestra start|resume` typed by a Cursor agent: our preToolUse hook saw this very command a moment ago and
    left a mark. No mark = the hooks are not active in this chat — it would run without guards, and the plan
    approval (recorded by a hook) could never arrive."""
    raw = EN.env_session_id()
    if _ASKER != "cursor" or not raw:
        return
    from . import state as ST
    seen = read_json(paths.orkestra_home() / "cursor" / "seen" / f"{ST._safe(raw)}.json")
    if isinstance(seen, dict) and time.time() - float(seen.get("ts") or 0) <= 900:
        return
    raise OrkestraError("orkestra's Cursor hooks did not see this command: they are not active in this chat, so it "
                        "would run without guards and the plan approval could never be recorded. Tell the user: run "
                        "`orkestra cursor install` in a terminal (once), then start again in a NEW chat.")


def _resolve_project(arg: Optional[str], required: bool = True) -> Optional[Path]:
    if arg:
        cand = Path(os.path.expanduser(arg))
        if not cand.is_absolute():
            local = (Path.cwd() / arg).resolve(strict=False)
            ws = (paths.workspace_root() / arg).resolve(strict=False)
            if arg in (".", "..") or arg.startswith(("./", "../")):
                cand = local
            elif ws.is_dir() and ws != paths.workspace_root():
                cand = ws
            else:
                cand = local
            if cand.is_dir() and cand != paths.workspace_root():
                found = paths.find_project_root(cand)
                cand = found or cand
        cand = cand.resolve(strict=False)
        if not cand.is_dir():
            raise OrkestraError(f"project directory not found: {arg}")
        if cand == paths.workspace_root():
            raise OrkestraError("the workspace root is not a project; pass a project folder, e.g. -p shop")
        return cand
    env = os.environ.get("ORKESTRA_PROJECT")
    if env:
        return Path(env).expanduser().resolve(strict=False)
    found = paths.find_project_root(Path.cwd())
    if found is None and required:
        raise OrkestraError(i18n.t("cli.no_project", workspace=paths.workspace_display()))
    return found


def _run_id(project: Path, arg: Optional[str]) -> str:
    rid = arg or project_active_run(project)
    if not rid:
        raise OrkestraError(i18n.t("cli.no_open_run", project=project.name))
    return rid


def _mode() -> str:
    return "headless" if os.environ.get("ORKESTRA_HEADLESS") == "1" else "interactive"


def _progress(st: Dict[str, Any]) -> str:
    tasks = st.get("tasks") or {}
    if not tasks:
        return "—"
    done = sum(1 for t in tasks.values() if t.get("status") in ("done", "deferred"))
    return f"{done}/{len(tasks)}"


def _next_task(st: Dict[str, Any]) -> Optional[str]:
    tasks = st.get("tasks") or {}
    for tid in sorted(tasks):
        t = tasks[tid]
        if t.get("status") in ("pending", "in_progress", "blocked"):
            deps_ok = all((tasks.get(d) or {}).get("status") in ("done", "deferred") for d in t.get("depends") or [])
            if deps_ok:
                return tid
    return None


def _next_lines(project: Path, st: Dict[str, Any], deep: bool = True) -> List[str]:
    """The deterministic next step (≤ 3 lines). Never raises: a broken artifact must not hide the phase goal."""
    try:
        from . import activity as A
        return T.next_lines(T.next_action(project, st, deep=deep), T.flags(project, st, A.read_rows(project, st["run_id"])))
    except Exception as exc:  # pragma: no cover
        return [f"next: `orkestra playbook {st.get('phase')}` (next-step analysis failed: {exc})"]


def _tempo(project: Path, rid: str) -> str:
    try:
        return T.tempo_line(project, load_state(project, rid))
    except Exception:  # pragma: no cover
        return ""


# ====================================================================== refusals about the machine and its files

def _utf8_stdio() -> None:
    """Print UTF-8 whatever the console says. Windows pipes default to cp125x and a few POSIX locales to ASCII; our
    output has arrows, dots and the user's own text (any script, emoji), and a strict codec would end the command with a
    traceback AFTER it changed state. A stream that cannot be reconfigured (an embedding caller's buffer) is left alone."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _io_detail(exc: OSError) -> str:
    """`Permission denied: '/path'` from an OSError (its own text when it has no file name)."""
    if exc.strerror and exc.filename:
        return f"{exc.strerror}: {exc.filename}"
    return str(exc)


def _require_usable_home() -> None:
    """`start` writes the run into the project AND registers it (with the session bindings) under ORKESTRA_HOME. When
    that folder cannot be written, say so before anything is created in the project."""
    home = paths.orkestra_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        handle, probe = tempfile.mkstemp(prefix=".write-test-", dir=str(home))
        os.close(handle)
        os.unlink(probe)
    except OSError as exc:
        raise OrkestraError(i18n.t("cli.home_unusable", path=home, error=_home_reason(home, exc)))


def _home_reason(home: Path, exc: OSError) -> str:
    """Why ORKESTRA_HOME cannot be used, in a few words."""
    return i18n.t("cli.home_is_file") if home.exists() and not home.is_dir() else (exc.strerror or str(exc))


def _read_request_file(path: Path) -> str:
    """The text of `--request-file`: UTF-8 (with or without a byte order mark), or UTF-16 with one (what `>` writes in
    Windows PowerShell 5). Anything else is refused by name instead of crashing or storing garbage."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise OrkestraError(i18n.t("cli.request_file_missing", path=path))
    except OSError as exc:                                  # a folder, no permission, ...
        raise OrkestraError(i18n.t("cli.request_file_unreadable", path=path, error=exc.strerror or exc))
    try:
        text = raw.decode("utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig")
    except UnicodeDecodeError:
        raise OrkestraError(i18n.t("cli.request_file_encoding", path=path))
    if not text.strip():
        raise OrkestraError(i18n.t("cli.request_file_empty", path=path))
    return text


def _json_kind(value: Any) -> str:
    """object | array | string | number | boolean | null (the key of `cli.kind_*`)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return {dict: "object", list: "array", str: "string"}.get(type(value), "null")


def _type_mismatches(default: Dict[str, Any], user: Dict[str, Any], prefix: str = "") -> List[Any]:
    """[(key path, expected kind, found kind)] for user settings whose JSON type differs from the default's. A setting
    left out or set to null is fine: every reader falls back to the default for it."""
    found: List[Any] = []
    for key, base in default.items():
        value = user.get(key)
        if value is None:
            continue
        want = _json_kind(base)
        if _json_kind(value) != want:
            found.append((prefix + key, want, _json_kind(value)))
        elif isinstance(base, dict):
            found += _type_mismatches(base, value, prefix + key + ".")
    return found


def _global_config_problem() -> str:
    """'' when ~/.orkestra/config.json is absent, empty or usable; else what is wrong with it. `orkestra doctor` shows it."""
    path = paths.orkestra_home() / "config.json"
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except OSError as exc:
        return i18n.t("help.lang_unreadable", path=path, error=exc.strerror or exc)
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):                    # not JSON, not UTF-8, absurdly nested
        return i18n.t("cli.config_not_json", path=path)
    if not isinstance(data, dict):
        return i18n.t("cli.config_not_object", path=path)
    wrong = _type_mismatches(paths.DEFAULT_GLOBAL_CONFIG, data)
    if not wrong:
        return ""
    details = "; ".join(i18n.t("cli.config_wrong_type", key=key, expected=i18n.t("cli.kind_" + want),
                               found=i18n.t("cli.kind_" + got)) for key, want, got in wrong[:4])
    return i18n.t("cli.config_bad_values", path=path, details=details)


def _project_config_problem(project: Path) -> str:
    """'' when the project's .orkestra/config.json is absent, empty or usable; else what is wrong with it. Gates drive the
    no-regression rule, so a damaged file must stop `start` and `gate` instead of silently meaning "no gates"."""
    path = paths.project_config_path(project)
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except OSError as exc:
        return i18n.t("help.lang_unreadable", path=path, error=exc.strerror or exc)
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        problem = i18n.t("cli.project_config_json")
    else:
        gates = data.get("gates", []) if isinstance(data, dict) else []
        if not isinstance(data, dict):
            problem = i18n.t("cli.project_config_object")
        elif not isinstance(gates, list) or not all(
                isinstance(g, dict) and isinstance(g.get("name"), str) and isinstance(g.get("cmd"), str) for g in gates):
            problem = i18n.t("cli.project_config_gates")
        else:
            return ""
    return i18n.t("cli.project_config_bad", path=path, problem=problem)


def _require_project_config(project: Path) -> None:
    problem = _project_config_problem(project)
    if problem:
        raise OrkestraError(problem)


# ====================================================================== commands

def cmd_context(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project, required=False)
    if project is None:
        ws = paths.workspace_root()
        _out(f"ORKESTRA CONTEXT — no project resolved from cwd ({Path.cwd()}).")
        _out(f"workspace: {ws}")
        if ws.is_dir():
            projs = sorted(p.name for p in ws.iterdir() if p.is_dir() and not p.name.startswith("."))
            _out("projects: " + ", ".join(projs))
        _out("Resolve the target project from the user's request and pass `-p <name>` to every orkestra command.")
        return 0
    config_problem = _project_config_problem(project)
    cfg = {"gates": []} if config_problem else paths.project_config(project)
    query = ""
    if project_active_run(project):
        active = load_state(project, project_active_run(project))
        query = str(active.get("title") or "") + "\n" + read_text(
            paths.feature_dir(project, str(active.get("run_id") or "")) / "request.md")[:800]
    dg = M.digest(project, query=query) if paths.memory_dir(project).is_dir() else {}
    gates_txt = ", ".join(g["name"] for g in cfg.get("gates") or []) or "none configured"
    if not paths.okdir(project).is_dir():
        gates_txt = ", ".join(g["name"] for g in G.detect(project)) + " (detected, not saved)"
    _out(f"ORKESTRA CONTEXT — project: {project.name} ({project})")
    _out(f"mode: {_mode()} · plugin: {__version__}")
    if dg:
        _out(f"memory: features {dg.get('features_total', '0')} · learnings {dg.get('learnings_total', '0')} · "
             f"specs: {dg.get('specs') or '—'}")
    else:
        _out("memory: NOT INITIALIZED → run onboarding first (`orkestra init` + playbook onboard)")
    _out(f"gates: {gates_txt} · confirmed: {'yes' if cfg.get('gates_confirmed') else 'no'}")
    if config_problem:
        _out("WARNING: " + config_problem)
    rid = project_active_run(project)
    if rid:
        st = load_state(project, rid)
        nt = _next_task(st)
        _out(f"active run: {rid} · \"{st['title']}\" · phase {st['phase']} · status {st['status']} · tasks {_progress(st)}"
             + (f" · next task {nt}" if nt and st['phase'] == 'implement' else ""))
        if st.get("awaiting"):
            _out(f"awaiting user: {st['awaiting'].get('question', '')}")
        if st.get("blocked"):
            _out(f"blocked: {st['blocked'].get('reason', '')}")
        _out(f"phase goal: {phase_goal(st['phase'], T.is_autopilot(st))}")
        for ln in _next_lines(project, st):
            _out(ln)
        if T.is_autopilot(st):
            pass                                    # autopilot: the phase goal above already says what to do instead of asking
        elif _mode() == "headless" and st["phase"] in ("clarify", "approve"):
            _out("headless: no AskUserQuestion — follow `orkestra playbook headless` for questions/approval")
        elif _ASKER == "cursor" and st["phase"] in ("clarify", "approve"):
            _out("cursor: questions and the plan approval follow `orkestra playbook headless` (chat question → "
                 "`orkestra await \"<summary>\"` → end your turn; approval = the user types \"approve\" or \"onay\")")
        _out(f"playbook: `orkestra playbook {st['phase']}`")
        _out(f"artifacts: {paths.feature_dir(project, rid)}")
        _out("this session is not driving the run unless it ran `orkestra start` or `orkestra resume`")
    else:
        _out("active run: none")
    if dg.get("project"):
        _out("--- PROJECT.md (head)\n" + dg["project"])
    if dg.get("features"):
        _out("--- recent features\n" + dg["features"])
    if dg.get("learnings"):
        label = "relevant learnings" if dg.get("learnings_matched") == "1" else "latest learnings"
        _out("--- " + label + "\n" + dg["learnings"])
    if dg:
        problems = M.check(project)
        if problems:
            _out("memory check: " + "; ".join(problems[:3]))
    return 0


def cmd_init(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    created = R.init_project(project)
    _out(f"initialized {project.name}: " + (", ".join(created) if created else "already initialized"))
    cfg = paths.project_config(project)
    _out("detected gates: " + (", ".join(f"{g['name']}=`{g['cmd']}`" for g in cfg.get("gates") or []) or "none"))
    if not cfg.get("gates"):
        _out(i18n.t("cli.init_no_gates", example='{"gates": [{"name": "test", "kind": "test", "cmd": "npm test", "timeout": 600}]}'))
    return 0


def _title_from_request(request: str) -> str:
    """A run title from the request: its first non-empty line, whitespace folded, cut at a word boundary (60 chars)."""
    first = next((ln for ln in request.splitlines() if ln.strip()), "")
    text = " ".join(first.split())
    if len(text) > 60:
        text = text[:60].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…" if " " in text[:60] else text[:60]
    return text


def cmd_start(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    request = a.request or getattr(a, "text", None) or ""
    if a.request_file:
        request = _read_request_file(Path(a.request_file))
    if not request.strip():
        raise OrkestraError(i18n.t("cli.start_need_request"))
    title = (a.title or "").strip() or _title_from_request(request)
    mode = "headless" if (a.headless or _mode() == "headless") else "interactive"
    try:
        engine = EN.normalize(getattr(a, "engine", None) or _ASKER, EN.default_engine(paths.global_config()))
    except EN.EngineError as exc:
        raise OrkestraError(str(exc))
    _require_cursor_hooks()
    approval_mode = a.approval_mode or os.environ.get("ORKESTRA_APPROVAL_MODE") or (
        "manual" if intent.manual_approval_requested(request) else "autopilot")
    if approval_mode not in ("autopilot", "manual"):
        raise OrkestraError("ORKESTRA_APPROVAL_MODE must be autopilot or manual")
    size_arg = getattr(a, "size", "auto") or "auto"
    size = intent.resolve_size(size_arg, request)
    if size not in intent.SIZES:
        raise OrkestraError(f"invalid size {size!r}")
    _require_project_config(project)
    _require_usable_home()
    st = R.start_run(project, title, request, size, mode, engine, approval_mode)
    rid = st["run_id"]
    kept = _bind_here(project, rid)
    _out(f"run started: {rid} in {project}" + ("" if engine == "claude" else f" (engine: {engine})"))
    if size_arg == "auto":
        size_note = " (inferred from request; override with --size XS|S|M|L)"
    elif size != size_arg:
        order = ("XS", "S", "M", "L")
        if order.index(size) > order.index(size_arg):
            size_note = f" (raised from --size {size_arg}: the request is a risky change)"
        else:
            size_note = f" (kept at {size}: a typo or a question does not grow to --size {size_arg})"
    else:
        size_note = ""
    _out(f"size: {size}{size_note}")
    _out(f"baseline: git={st['baseline']['git']} head={str(st['baseline'].get('head'))[:12]} "
         f"uncommitted_files={st['baseline']['dirty_files']}")
    if not st["baseline"]["git"]:
        _out(i18n.t("runs.no_git_rollback"))
    if st["baseline"]["dirty_files"]:
        _out("NOTE: the working tree already had uncommitted changes. They are backed up and will never be "
             "attributed to this run. Tell the user.")
    _out(f"artifacts: {paths.feature_dir(project, rid)}  (short path: .orkestra/current/ — no globs needed)")
    others = R.other_open_runs(project)
    if len(others) >= 2:
        _out(f"WARNING: {len(others)} other runs are open on this machine ({', '.join(others)}). They share CPU with this "
             f"one (tests, builds, simulators) and every run gets slower. Tell the user in one line; continue if they want.")
    _out(f"next action: {phase_goal('intake')}")
    if paths.memory_dir(project).is_dir():
        lessons = M.digest(project, query=request)
        if lessons.get("learnings_matched") == "1" and lessons.get("learnings"):
            _out("lessons for this request:\n" + lessons["learnings"])
    if kept:
        _out(kept)
    _out(bind_marker(project, rid))
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    _require_cursor_hooks()
    from . import state as ST
    rid0 = a.run or project_active_run(project) or ""
    prev = str((read_json(ST.state_path(project, rid0)) or {}).get("status") or "") if rid0 else ""
    actor = ST.take_actor(project, rid0) if rid0 else {}        # which chat asked (left by the PreToolUse hook)
    st = R.resume_run(project, a.run)
    if prev == "paused" and st.get("status") == "active":       # the visible trace of the flip (who, from, to)
        R.record_status_row(project, st["run_id"], prev, "active", actor or {"agent_type": "cli"})
    kept = _bind_here(project, st["run_id"])
    _out(f"resumed {st['run_id']} · phase {st['phase']} · status {st['status']} · tasks {_progress(st)}")
    # A resume is the user saying "continue from here". Subagent records with no sign of life for a lease period
    # belong to a window that is gone: waiting for them would park the run in the very first answer.
    from . import activity as A
    from .state import ledger
    lease = float((paths.global_config().get("watchdog") or {}).get("lease_seconds") or 120)
    _, lost = T.live_agents(project, st["run_id"], wd=dict(T.wd_cfg(), silent_seconds=lease, agent_silent_seconds=lease))
    # …but only when the window that launched them is silent too. Called from the SAME live window (its heartbeat
    # was refreshed by this very command), a quiet agent is just thinking: the normal silent_seconds rule applies.
    ages = A.session_ages(project, st["run_id"])
    lost = [g for g in lost if not g.get("sid") or ages.get(A._safe_sid(str(g["sid"])), lease + 1) > lease]
    if lost:
        gone = A.agents_purge(project, st["run_id"], ids=[str(g.get("id")) for g in lost])
        if gone:
            who = ", ".join(f"{A.short_agent(str(g.get('type') or ''))} {'/'.join(g.get('tasks') or [])}".strip() for g in gone[:6])
            ledger(project, st["run_id"], f"resume: dropped {len(gone)} subagent record(s) with no sign of life ({who})")
            _out(f"NOTE: dropped {len(gone)} subagent record(s) with no sign of life for {T.dur(lease)}+ ({who}) — if their "
                 f"work is unfinished, the next step below dispatches it again")
    for ln in _next_lines(project, st):
        _out(ln)
    _out(f"playbook: `orkestra playbook {st['phase']}`")
    if kept:
        _out(kept)
    _out(bind_marker(project, st["run_id"]))
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    st = load_state(project, rid)
    if a.json:
        _out(json.dumps(st, indent=2, ensure_ascii=False))
        return 0
    _out(f"{rid} · \"{st['title']}\" · phase {st['phase']} · status {st['status']} · size {st['size']} · mode {st['mode']}"
         f" · approval {st.get('approval_mode', 'autopilot')}"
         + (f" · engine {st['engine']}" if (st.get("engine") or "claude") != "claude" else ""))
    for tid in sorted(st.get("tasks") or {}):
        t = st["tasks"][tid]
        ev = ("R" if (t.get("red") or {}).get("ok") else "-") + ("G" if (t.get("green") or {}).get("ok") else "-")
        _out(f"  {tid} [{t.get('status')}] tdd={t.get('tdd')} evidence={ev} {t.get('title')}")
    appr = read_json(paths.run_dir(project, rid) / "approval.json")
    _out(f"approval: {appr.get('decision')} via {appr.get('source')} at {appr.get('at')}" if isinstance(appr, dict) else "approval: none")
    for ln in _next_lines(project, st):
        _out(ln)
    return 0


def cmd_phase(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    if a.action == "show":
        st = load_state(project, rid)
        _out(" → ".join(f"[{p}]" if p == st["phase"] else p for p in PHASES))
        return 0
    if a.action == "check":
        st = load_state(project, rid)
        target = PHASES[min(PHASES.index(st["phase"]) + 1, len(PHASES) - 1)]
        problems = R.preconditions(project, st, target)
        _out(f"{st['phase']} → {target}: " + ("READY" if not problems else "NOT READY\n  - " + "\n  - ".join(problems)))
        return 0 if not problems else 1
    if a.action == "next":
        cur, nxt = R.phase_next(project, rid, a.note or "")
        _out(f"phase {cur} → {nxt}")
        if nxt == "done":
            _out(unbind_marker(project, rid))
            _out("Run complete. Remind the user: nothing was committed; the changes are in the working tree.")
        else:
            _out(f"next action: {phase_goal(nxt, T.is_autopilot(load_state(project, rid)))}")
            _out(f"playbook: `orkestra playbook {nxt}`")
            if nxt == "approve":
                note = _large_plan_note(read_text(paths.feature_dir(project, rid) / "plan.md"))
                if note:
                    _out(note)
            _out(_tempo(project, rid))
        return 0
    if a.action == "back":
        if not a.target or not a.reason:
            raise OrkestraError("usage: orkestra phase back <target> --reason \"...\"")
        cur, nxt = R.phase_back(project, rid, a.target, a.reason)
        _out(f"phase back {cur} → {nxt}")
        return 0
    raise OrkestraError(f"unknown phase action {a.action}")


def cmd_status_change(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    mapping = {"await": "awaiting_user", "block": "blocked", "unblock": "active", "pause": "paused", "abort": "aborted"}
    status = mapping[a.cmd]
    reason = " ".join(a.reason or []).strip()
    flagged = (getattr(a, "reason_opt", None) or "").strip()        # `--reason TEXT`: same thing as the words
    if reason and flagged:
        raise OrkestraError(f"orkestra {a.cmd}: give the reason once, as words or with --reason, not both")
    reason = flagged or reason
    if a.cmd in ("await", "block", "abort") and not reason:
        raise OrkestraError(f"orkestra {a.cmd} needs a reason/question text (words, or --reason TEXT)")
    from . import state as ST
    actor = ST.take_actor(project, rid)                 # the chat the PreToolUse hook saw ask for this ({} = a terminal)
    changed = R.set_status(project, rid, status, reason, who=actor or {"agent_type": "cli"})
    if changed:
        _out(f"{rid}: status → {status}" + (f" ({reason})" if reason else ""))
    else:
        _out(i18n.t("cli.status_unchanged", run=rid, status=status))
    if status in ("aborted", "paused"):
        # Pause used to leave the session bound: Stop could still force-continue the run.
        _unbind_here(project, rid)
        _out(unbind_marker(project, rid))
        if status == "aborted":
            _out("Run aborted. Files are untouched; use /orkestra:rollback if the user wants changes reverted.")
    return 0


GATE_OUT_CAP = 8 * 1024             # failing-gate tails printed to the model in one command (the logs hold the rest)


def cap_gate_text(text: str, cap: int = GATE_OUT_CAP) -> str:
    """Keep the gate table and as many failing tails as fit in `cap` characters; list the rest by log path."""
    if len(text) <= cap:
        return text
    head, *tails = text.split("\n\n--- ")
    out, left = head, []
    for blk in tails:
        block = "\n\n--- " + blk
        log = re.search(r"full log: (\S+)", blk)
        label = f"{blk.split(' ', 1)[0]}: {log.group(1) if log else '?'}"
        room = cap - len(out)
        if len(block) <= room:
            out += block
        elif room > 500 and not left:
            first, _, body = blk.partition("\n")
            keep = max(0, room - len(first) - 120)
            out += "\n\n--- " + first + "\n…\n" + body[-keep:]
            left.append(label + " (tail cut)")
        else:
            left.append(label)
    if left:
        out += f"\n\n(output capped at {cap // 1024} KB; open the full logs: " + "; ".join(left) + ")"
    return out


def cmd_gate(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    _require_project_config(project)
    cfg = paths.project_config(project)
    if a.action == "detect":
        det = G.detect(project)
        for g in det:
            _out(f"- {g['name']} ({g['kind']}): `{g['cmd']}`")
        if a.write:
            cfg["gates"] = det
            paths.save_project_config(project, cfg)
            _out(f"saved {len(det)} gates to {paths.project_config_path(project)}")
        return 0
    if a.action == "list":
        for g in cfg.get("gates") or []:
            _out(f"- {g['name']} ({g.get('kind')}): `{g['cmd']}` timeout={g.get('timeout', G.DEFAULT_TIMEOUT)}s")
        if not cfg.get("gates"):
            _out("no gates configured — edit .orkestra/config.json or run `orkestra gate detect --write`")
        return 0
    if a.action == "confirm":
        if not cfg.get("gates"):
            _out(i18n.t("cli.gate_confirm_none"))
            return 0
        cfg["gates_confirmed"] = True
        paths.save_project_config(project, cfg)
        _out("gates marked as confirmed")
        return 0
    rid = _run_id(project, a.run)
    if a.action == "baseline":
        results, text = R.gates_baseline(project, rid)
        _out(cap_gate_text(text) if results else "no gates configured → empty baseline recorded (RISK: no automated regression net)")
        return 1 if G.all_env_error(results) else 0
    if a.action == "run":
        gates = cfg.get("gates") or []
        results = G.run_all(project, gates, paths.run_dir(project, rid) / "evidence", "adhoc", only=a.names or None)
        _out(cap_gate_text(G.format_results(results)) if results else "no matching gates")
        return 0 if all(r["ok"] for r in results) else 1
    if a.action == "verify":
        comparison, text = R.gates_verify(project, rid, resume=bool(getattr(a, "resume", False)),
                                            full=bool(getattr(a, "all", False)))
        _out(cap_gate_text(text))
        rs = (read_json(paths.run_dir(project, rid) / "gates-verify.json") or {}).get("results") or []
        return 0 if comparison["ok"] and not G.all_env_error(rs) else 1
    if a.action == "last":
        rdir = paths.run_dir(project, rid)
        last = read_json(rdir / "gates-verify.json")
        if not isinstance(last, dict):
            _out("no `orkestra gate verify` result yet")
            return 1
        base = (read_json(rdir / "gates-baseline.json") or {}).get("results") or []
        _out(f"last verify at {last.get('at')}")
        _out(cap_gate_text(G.format_results(last.get("results") or [], last.get("comparison"), base)))
        newest = R._newest_change_mtime(project, rdir)
        if newest and newest > float(last.get("ts", 0)):
            _out("WARNING: files changed after this verify — it is stale; the orchestrator must re-run it")
        return 0 if (last.get("comparison") or {}).get("ok") else 1
    raise OrkestraError(f"unknown gate action {a.action}")


def cmd_tdd(a: argparse.Namespace) -> int:
    cmd = split_remainder(a, list(a.command or []))
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    rec = R.tdd_record(project, rid, a.task, a.kind, cmd, getattr(a, "why", "") or "")
    want = "a FAILING test (red)" if a.kind == "red" else "PASSING tests (green)"
    _out(f"{a.task} tdd {a.kind}: exit {rec['exit']} → {'OK' if rec['ok'] else 'NOT OK — expected ' + want}")
    if not rec["ok"] or a.kind == "red":
        _out(rec["tail"])
    _out(f"log: {rec['log']}")
    return 0 if rec["ok"] else 1


def cmd_evidence(a: argparse.Namespace) -> int:
    cmd = split_remainder(a, list(a.command or []))
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    rec = R.run_evidence(project, rid, a.label, cmd)
    _out(f"evidence {a.label}: exit {rec['exit']} · cite this in verification.md: {rec['rel']}")
    _out(rec["tail"])
    return 0


def cmd_integrity(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    problems = E.integrity_problems(project, rid)
    _out("integrity check: " + ("OK (no removed tests, no skip/only markers, no new suppressions)" if not problems
                                else "FAIL\n  - " + "\n  - ".join(problems)))
    return 0 if not problems else 1


def cmd_report(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    if a.write:
        path = E.write_block(project, rid)
        _out(f"machine evidence block written into {path} (write your narrative around it; never edit the block)")
    else:
        _out(E.evidence_block(project, rid))
    return 0


def cmd_task(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    text = " ".join(a.text or []).strip()
    if a.action == "list":
        st = load_state(project, rid)
        for tid in sorted(st.get("tasks") or {}):
            t = st["tasks"][tid]
            _out(f"{tid} [{t.get('status')}] {t.get('title')} · files: {', '.join(t.get('files') or [])}")
        nt = _next_task(st)
        _out(f"next: {nt or '—'}")
        minors = [(tid, [n for n in (st["tasks"][tid].get("notes") or []) if str(n).lower().startswith("minor")])
                  for tid in sorted(st.get("tasks") or {})]
        minors = [(tid, ns) for tid, ns in minors if ns]
        if minors:
            _out("carried minors (triage them in the final review — they never reopen a task):")
            for tid, ns in minors:
                for n in ns:
                    _out(f"  {tid}: {n}")
        return 0
    if not a.task:
        raise OrkestraError("task id required")
    force = (getattr(a, "force", "") or "").strip()
    if a.action == "start":
        for w in R.task_start(project, rid, a.task, a.agent or "", force):
            _out(f"NOTE: {w}")
    elif a.action == "done":
        if not R.task_done(project, rid, a.task, text):
            _out(f"NOTE: {a.task} was already done — nothing changed.")
    elif a.action in ("block", "defer"):
        if not text:
            raise OrkestraError(f"orkestra task {a.action} {a.task} \"reason\"")
        R.task_set(project, rid, a.task, "blocked" if a.action == "block" else "deferred", text)
    elif a.action == "reopen":
        R.task_reopen(project, rid, a.task, text or force or "another round", force)
    elif a.action == "add":
        if not text:
            raise OrkestraError("orkestra task add FIX-R001 \"title\" [--no-tdd] [--files a.py,b.py]")
        files = [x.strip() for x in (getattr(a, "files", "") or "").replace(";", ",").split(",") if x.strip()]
        R.task_add(project, rid, a.task, text, "n/a" if a.no_tdd else "required", files)
    elif a.action == "note":
        R.task_note(project, rid, a.task, text)
    _out(f"{a.task}: {a.action} ok")
    if a.action in ("done", "defer", "reopen"):
        _out(_tempo(project, rid))
        for ln in _next_lines(project, load_state(project, rid), deep=False)[:1]:
            _out(ln)
    return 0


def cmd_next(a: argparse.Namespace) -> int:
    """The single next step, derived from state + evidence + review files (same answer every time it is asked)."""
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    st = load_state(project, rid)
    from . import activity as A
    n = T.next_action(project, st, deep=True)
    fl = T.flags(project, st, A.read_rows(project, rid))      # deny loops and repeated commands surface here too
    if a.json:
        _out(json.dumps(dict(n, flags=fl), indent=2, ensure_ascii=False, default=str))
        return 0
    for ln in T.next_lines(n, fl):
        _out(ln)
    if st.get("phase") == "implement" and st.get("approval_mode", "autopilot") == "autopilot":
        from .parallel_workers import launch_ready
        for ln in launch_ready(project, st):
            _out(ln)
    return 0


def cmd_wait(a: argparse.Namespace) -> int:
    """Headless: wait for the subagents INSIDE this process (the model is not asked anything while it waits), then print
    the same verdict `orkestra next` would. An interactive session never blocks here: its hand-backs arrive as messages."""
    from . import activity as A
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    st = load_state(project, rid)
    if paths.is_headless(st):
        res = T.wait_for(project, rid, a.max)
    else:
        res = {"reason": "interactive", "waited": 0.0}
    st = load_state(project, rid)
    n = T.next_action(project, st, deep=True)
    fl = T.flags(project, st, A.read_rows(project, rid))
    _out(T.next_lines(n, fl)[0])
    why = {"idle": "no subagent is running", "reports": "a subagent report arrived", "handback": "a hand-back arrived",
           "status": f"the run is {st.get('status')}", "artifact": "a review or report file changed",
           "timeout": "cap reached; call `orkestra wait` again if the verdict still says running",
           "interactive": "interactive session: not blocking, end your turn instead of waiting"}.get(res["reason"], res["reason"])
    _out(f"wait: {why} (waited {T.dur(res['waited'])})")
    return 0


def cmd_agents(a: argparse.Namespace) -> int:
    """Background subagents the hooks registered for the run; `clear` drops records of agents that are gone."""
    from . import activity as A
    from .state import ledger
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    running, lost = T.live_agents(project, rid)
    if a.action == "clear":
        gone = A.agents_purge(project, rid)
        if gone:
            ledger(project, rid, f"pending subagent records cleared by CLI ({len(gone)}): "
                                 + ", ".join(f"{g.get('type')} {'/'.join(g.get('tasks') or [])}".strip() for g in gone[:8]))
        _out(f"cleared {len(gone)} pending subagent record(s)")
        for ln in _next_lines(project, load_state(project, rid), deep=False)[:1]:
            _out(ln)
        return 0
    if a.json:
        _out(json.dumps({"running": running, "lost": lost}, indent=2, ensure_ascii=False, default=str))
        return 0
    if not running and not lost:
        _out("no background subagent is registered as running")
    for tag, items in (("running", running), ("LOST? (no sign of life)", lost)):
        for g in items:
            _out(f"{tag}: {g.get('type')} {'/'.join(g.get('tasks') or [])} · {T.dur(g.get('age'))} · {g.get('desc') or ''}".rstrip(" ·"))
    if lost:
        _out("`orkestra agents clear` drops the lost records (use it only when those agents are really gone)")
    return 0


def cmd_tempo(a: argparse.Namespace) -> int:
    """Where the time went: phases, tasks, review rounds, measured dead wait (read-only)."""
    from . import activity as A
    project = _resolve_project(a.project)
    assert project is not None
    rid = a.run or project_active_run(project)
    if not rid:
        runs = sorted(p.name for p in paths.runs_dir(project).iterdir()) if paths.runs_dir(project).is_dir() else []
        if not runs:
            raise OrkestraError(f"no run in {project.name}")
        rid = runs[-1]
    st = load_state(project, rid)
    tm = T.timings(st)
    rows = A.read_all_rows(project, rid)
    dw = T.dead_wait_share(rows)
    cls = T.classify(st, A.read_rows(project, rid), A.agents_pending(project, rid),
                     reports_due=A.agents_reports_due(project, rid, max_age=3600.0))
    from . import scorecard as SC
    card = SC.compute(project, st, rows)
    if a.json:
        _out(json.dumps({"run": rid, "timings": tm, "dead_wait": dw, "stall": cls, "flags": T.flags(project, st, rows[-200:]),
                         "scorecard": card},
                        indent=2, ensure_ascii=False, default=str))
        return 0
    for line in T.report_lines(rid, st, tm, dw, cls, list(dict.fromkeys(T.flags(project, st, rows[-200:]) + cls.get("loops", []))), card):
        _out(line)
    return 0


def cmd_watchdog(a: argparse.Namespace) -> int:
    from . import watchdog
    if a.launchd and sys.platform != "darwin":
        raise OrkestraError(i18n.t("cli.launchd_macos_only"))        # a LaunchAgent means nothing on Linux or Windows
    if a.launchd:
        plist = Path("~/Library/LaunchAgents/com.orkestra.watchdog.plist").expanduser()
        plist.parent.mkdir(parents=True, exist_ok=True)
        path_env = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
        logs = paths.logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.orkestra.watchdog</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>{PLUGIN_ROOT / 'bin' / 'orkestra'}</string><string>watchdog</string><string>--quiet</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_env}</string><key>LANG</key><string>{i18n.job_locale()}</string></dict>
  <key>StartInterval</key><integer>60</integer><key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{logs}/watchdog.out</string>
  <key>StandardErrorPath</key><string>{logs}/watchdog.err</string>
</dict></plist>
""")
        _out(i18n.t("cli.watchdog_launchd_wrote", plist=plist))
        return 0
    if a.loop or a.daemon or a.status:
        from . import activity as A
        from . import fastwatch
        if a.status:
            st = fastwatch.status()
            if a.json:
                _out(json.dumps(st, ensure_ascii=False, indent=2, default=str))
                return 0
            if not st["running"]:
                _out(i18n.t("watchdog.fast_status_stopped", autostart="on" if st["autostart"] else "off"))
                return 0
            ago = A.human_duration(max(0, time.time() - float(st.get("ts") or time.time())))
            _out(i18n.t("watchdog.fast_status_breaker") if st["breaker_open"] else
                 i18n.t("watchdog.fast_status_running", pid=st["pid"], interval=st.get("interval") or "?",
                        runs=st.get("open_runs") if st.get("open_runs") is not None else "?", ago=ago))
            for key, info in sorted((st.get("runs") or {}).items()):
                if info.get("flags"):
                    _out(i18n.t("watchdog.fast_flag_line", run=key, flags=", ".join(info["flags"])))
            return 0
        if a.daemon:
            if fastwatch.is_running():
                _out(i18n.t("watchdog.fast_daemon_running", pid=fastwatch.holder_pid()))
            elif os.environ.get("ORKESTRA_NO_DAEMON") == "1" or not fastwatch.status()["supported"]:
                _out(i18n.t("watchdog.fast_daemon_disabled"))
            else:
                _out(i18n.t("watchdog.fast_daemon_started", pid=fastwatch.start_daemon()))
            return 0
        return fastwatch.run_forever(idle_exit=a.idle_exit or None)
    return watchdog.main(once=True, as_json=a.json, dry_run=a.dry_run, quiet=a.quiet)


REVIEW_DIFF_LINES = 400         # the diff in a review packet (a bigger change is read from `orkestra diff` by file)
REVIEW_TAIL_LINES = 40          # red / green evidence tail


def _evidence_tail(rec: Dict[str, Any]) -> str:
    """Last REVIEW_TAIL_LINES lines of the evidence log (falls back to the short tail kept in state)."""
    text = read_text(Path(str(rec.get("log") or ""))) if rec.get("log") else ""
    return "\n".join((text or str(rec.get("tail") or "")).splitlines()[-REVIEW_TAIL_LINES:])


def review_packet(project: Path, rid: str, tid: str) -> str:
    """Everything the task reviewer needs in one call: the task section with its acceptance, red/green tails, the
    implementer's report, deviations and the diff of the task's own files (capped). Saves the ~18 exploratory calls."""
    fdir = paths.feature_dir(project, rid)
    st = load_state(project, rid)
    t = (st.get("tasks") or {}).get(tid)
    if not t:
        raise OrkestraError(f"unknown task {tid}")
    plan = read_text(fdir / "plan.md")
    task = next((x for x in PP.parse_tasks(plan) if x.id == tid), None)
    out = [f"# Review packet — {tid} · run {rid} · project {project.name} · round {T.reopen_count(t) + 1}",
           f"artifacts: {fdir}  (write the review to reviews/{tid}.review.md; end with VERDICT: PASS | PASS_WITH_NOTES | FAIL)"]
    out.append("## Task\n" + (PP.task_section(plan, tid) or f"### {tid} — {t.get('title')} (ad-hoc, no plan section)\n").strip())
    acc = task.fields.get("acceptance") if task else ""
    out.append("## Acceptance\n" + (acc or "(none recorded: judge against the task title and spec)"))
    devs: List[str] = []
    for kind in ("red", "green"):
        rec = t.get(kind) or {}
        if not rec:
            out.append(f"## {kind.upper()} evidence\nnone recorded")
            continue
        out.append(f"## {kind.upper()} evidence (exit {rec.get('exit')}, {'ok' if rec.get('ok') else 'NOT ok'}) `{rec.get('cmd')}`\n"
                   f"log: {rec.get('log')}\n" + _evidence_tail(rec))
        devs += [f"{kind}: {d}" for d in rec.get("deviations") or []]
        if rec.get("foreign_note"):
            devs.append(f"{kind}: {rec['foreign_note']}")
    rep_path = fdir / "tasks" / f"{tid}.report.md"
    rep = read_text(rep_path)
    out.append(f"## Implementer report ({rep_path})\n" + ("\n".join(rep.strip().splitlines()[:30]) if rep.strip() else "no report written"))
    out.append("## Deviations\n" + ("\n".join(f"- {d}" for d in devs) if devs else "none recorded"))
    prior = T.task_review(fdir, tid)
    if prior:
        out.append(f"## Earlier review\n{prior.get('path')} (verdict {prior.get('verdict')}): check that its blocker/major rows are fixed")
    notes = [n for n in (t.get("notes") or [])[-5:]]
    if notes:
        out.append("## Task notes\n" + "\n".join(f"- {n}" for n in notes))
    files = [f for f in (t.get("files") or []) if f]
    body: List[str] = []
    if S.is_git_repo(project) and files:
        rc, diff = S._git(project, "--no-pager", "diff", "--", *files)
        body = (diff or "").splitlines() if rc == 0 else []
    for f in files:                              # new files are not in `git diff`: show their head
        fp = project / f
        if fp.is_file() and not any(ln.startswith("diff --git") and f in ln for ln in body):
            head = read_text(fp).splitlines()
            body += [f"new/untracked file: {f} ({len(head)} lines)"] + [f"+{ln}" for ln in head[:60]]
    cut = len(body) > REVIEW_DIFF_LINES
    out.append(f"## Diff of the task's files ({', '.join(files) or 'no files recorded'})\n"
               + ("\n".join(body[:REVIEW_DIFF_LINES]) if body else "(no tracked diff)")
               + (f"\n… {len(body) - REVIEW_DIFF_LINES} more diff lines: `orkestra diff {T.p_arg(project)}`" if cut else ""))
    out.append("Start from this packet; open more only to verify a doubt (callers of changed functions, a log you need in full).")
    return "\n\n".join(out)


def cmd_brief(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    if getattr(a, "review", False):
        _out(review_packet(project, rid, a.task))
        return 0
    fdir = paths.feature_dir(project, rid)
    plan = read_text(fdir / "plan.md")
    spec = read_text(fdir / "spec.md")
    sec = PP.task_section(plan, a.task)
    if not sec:
        raise OrkestraError(f"{a.task} not found in plan.md")
    st = load_state(project, rid)
    ids = sorted(set(PP.ID_RE.findall(sec)))
    spec_lines = [ln for ln in spec.splitlines() if any(i in ln for i in ids)]
    constraints = PP.heading_body(plan, "global_constraints") or ""
    bounds = PP.heading_body(spec, "boundaries") or ""
    gates = ", ".join(f"{g['name']}=`{g['cmd']}`" for g in paths.project_config(project).get("gates") or [])
    _out(f"# Task brief — {a.task} · run {rid} · project {project.name} ({project})")
    _out(f"artifacts: {fdir}  (context.md has the code map; read only what you need)")
    if constraints.strip():
        _out("## Global constraints\n" + constraints.strip())
    if bounds.strip():
        _out("## Boundaries (spec)\n" + bounds.strip())
    _out("## Task\n" + sec.strip())
    if spec_lines:
        _out("## Referenced requirements\n" + "\n".join(spec_lines))
    _out(f"## Gates\n{gates or 'none'}")
    t = (st.get("tasks") or {}).get(a.task) or {}
    if t.get("notes"):
        _out("## Notes from earlier attempts\n" + "\n".join(f"- {n}" for n in t["notes"][-5:]))
    rv = T.task_review(fdir, a.task) if t.get("status") != "done" else {}
    if rv and (rv.get("verdict") == "FAIL" or float(rv.get("mtime") or 0) <= float(t.get("review_seen") or 0)):
        _out(f"## Review to fix (read it first; fix only the blocker/major rows of its latest round)\n{rv['path']}")
    _out(f"## Report file\n{fdir / 'tasks' / (a.task + '.report.md')}")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    fdir = paths.feature_dir(project, rid)
    spec = read_text(fdir / "spec.md")
    if a.cmd == "plan":
        problems = PP.check_plan(spec, read_text(fdir / "plan.md"), project,
                                  work_kind=kind_of(load_state(project, rid)), run_id=rid)
    elif a.cmd == "trace":
        ver = read_text(fdir / "verification.md")
        gate_names = [g["name"] for g in paths.project_config(project).get("gates") or []]
        problems = PP.check_trace(spec, ver) + E.verification_problems(project, rid, spec, ver, gate_names)
    elif a.cmd == "deps":
        problems = R.deps_problems(project, load_state(project, rid))
    elif a.cmd == "review":
        tri = read_text(fdir / "reviews" / "triage.md")
        problems = ["reviews/triage.md missing"] if not tri.strip() else \
            [f"open: {x}" for x in PP.open_triage_items(tri)]
    else:
        raise OrkestraError("unknown check")
    _out(f"{a.cmd} check: " + ("OK" if not problems else "FAIL\n  - " + "\n  - ".join(problems)))
    if a.cmd == "plan" and not problems:
        plan_text = read_text(fdir / "plan.md")
        for n in PP.plan_notes(PP.parse_tasks(plan_text)):
            _out(n)
        note = _large_plan_note(plan_text)
        if note:
            _out(note)
    return 0 if not problems else 1


LARGE_PLAN_TASKS = 20


def _large_plan_note(plan_text: str) -> str:
    n = len(PP.parse_tasks(plan_text))
    if n <= LARGE_PLAN_TASKS:
        return ""
    return (f"NOTE: LARGE PLAN — {n} tasks (> {LARGE_PLAN_TASKS}). One run of this size takes many hours and its context "
            f"grows with every task. In the approval summary, recommend splitting it into {max(2, round(n / 15))} runs by "
            f"capability (state which tasks go first); the user decides.")


def cmd_changes(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    items = S.changed_files(project, paths.run_dir(project, rid))
    if a.json:
        _out(json.dumps(items, indent=2))
    else:
        if not items:
            _out("no changes attributable to this run")
        for it in items:
            _out(f"{it['change']:9} {it['path']}")
        from . import hints
        books = hints.playbooks_for([it["path"] for it in items], project)
        if books:
            _out(f"playbooks: {', '.join(books)}  (`orkestra playbook <name>` — task-scope review: only the first; "
                 f"final review: all)")
    return 0


def cmd_diff(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    items = S.changed_files(project, paths.run_dir(project, rid))
    mods = [it["path"] for it in items if it["change"] != "created"]
    created = [it["path"] for it in items if it["change"] == "created"]
    if S.is_git_repo(project) and mods:
        args = ["git", "-C", str(project), "--no-pager", "diff"] + (["--stat"] if a.stat else []) + ["--"] + mods
        try:        # bounded and without the index lock (S.git_env); git writes UTF-8, whatever the locale
            cp = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=S.GIT_TIMEOUT,
                                env=S.git_env(), stdin=subprocess.DEVNULL)
            _out(cp.stdout or "(no tracked diff)")
        except (OSError, subprocess.SubprocessError):
            _out("(no tracked diff)")
    for c in created:
        _out(f"new file: {c}")
    return 0


def cmd_clean(a: argparse.Namespace) -> int:
    """Remove regenerable Playwright/Android dumps under the project (same set as post-done cleanup)."""
    from . import artifacts as ART
    from . import i18n
    project = _resolve_project(a.project)
    assert project is not None
    if not ART.cleanup_enabled(project) and not getattr(a, "gradle_transforms", False):
        _out(i18n.t("cli.clean_disabled"))
        return 0
    reports = []
    if ART.cleanup_enabled(project):
        reports.append(ART.clean_project(project, dry_run=bool(a.dry_run)))
    if getattr(a, "gradle_transforms", False):
        reports.append(ART.clean_gradle_transforms(dry_run=bool(a.dry_run)))
    removed = []
    errors = []
    freed = 0
    for r in reports:
        removed.extend(r.get("removed") or [])
        errors.extend(r.get("errors") or [])
        freed += int(r.get("freed_bytes") or 0)
    if not removed and not errors:
        _out(i18n.t("cli.clean_none"))
        return 0
    mode = i18n.t("cli.clean_mode_dry") if a.dry_run else i18n.t("cli.clean_mode_done")
    _out(i18n.t("cli.clean_header", mode=mode, count=len(removed),
                freed=i18n.t("cli.clean_bytes", size=freed / (1024 ** 3))))
    for item in removed:
        _out(i18n.t("cli.clean_line", path=paths.display_path(item["path"]),
                    size=i18n.t("cli.clean_bytes", size=int(item.get("bytes") or 0) / (1024 ** 3))))
    for err in errors:
        _out(i18n.t("cli.clean_error", path=paths.display_path(err["path"]), error=err["error"]))
    return 1 if errors and not removed else 0


def cmd_rollback(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    rid = _run_id(project, a.run)
    actions = S.rollback(project, paths.run_dir(project, rid), dry_run=not a.yes, include_all=a.all)
    if not actions:
        _out("nothing to roll back")
        return 0
    run_items = [x for x in actions if x["scope"] == "run"]
    extra = [x for x in actions if x["scope"] == "extra"]
    _out(("APPLIED" if a.yes else "DRY RUN (nothing changed yet)") + f" — {len(actions)} file(s)")
    if run_items:
        _out("Changed by orkestra tools during the run:")
        for act in run_items:
            _out(f"- {act['path']}: {act['how']}")
    if extra:
        _out("Other differences since the run started (maybe yours, or from an undetected command) — "
             + ("included (--all):" if a.all else "left alone unless --all:"))
        for act in extra:
            _out(f"- {act['path']}: {act['how']}")
    if a.yes:
        saved = [x for x in actions if x.get("saved")]
        if saved:
            _out(f"Current contents were saved before overwriting: {Path(saved[0]['saved']).parent}")
        from .state import ledger
        ledger(project, rid, f"rollback applied ({len(actions)} file(s), all={a.all})")
    return 0


def cmd_memory(a: argparse.Namespace) -> int:
    project = _resolve_project(a.project)
    assert project is not None
    text = " ".join(a.text or []).strip()
    if a.action not in ("show", "check"):
        R.init_project(project)
    if a.action == "check":
        problems = M.check(project)
        if problems:
            _out("\n".join(problems))
            return 1
        _out("memory check: OK")
        return 0
    if a.action == "observe":
        dest = paths.memory_dir(project) / "PROJECT.md"
        blank = (not dest.is_file()) or M.is_template(read_text(dest))
        R.init_project(project)
        _out("memory observe: " + ("wrote PROJECT.md" if blank else "PROJECT.md already filled"))
        return 0
    if a.action == "show":
        if not paths.memory_dir(project).is_dir():
            _out("memory not initialized for this project")
            return 0
        dg = M.digest(project, max_features=50, learnings_limit=50)
        for k, v in dg.items():
            _out(f"--- {k}\n{v}")
        return 0
    rid = a.run or project_active_run(project) or "manual"
    if a.action == "feature-add":
        if not a.title or not a.summary:
            raise OrkestraError("orkestra memory feature-add --title ... --summary ... [--capabilities a,b]")
        caps = [c.strip() for c in (a.capabilities or "").split(",") if c.strip()]
        M.feature_add(project, rid, a.title, a.summary, caps)
    elif a.action in ("decision", "learn"):
        if not text:
            raise OrkestraError(f"orkestra memory {a.action} \"text\"")
        M.append_entry(project, "DECISIONS.md" if a.action == "decision" else "LEARNINGS.md", rid, text)
    _out(f"memory {a.action}: ok")
    return 0


HEADLESS_WAIT = ("HEADLESS: wherever the text below tells you to stop after dispatching subagents, do NOT end your turn "
                 "— this process exits with the turn and kills them. Wait with ONE blocking call: `orkestra wait` (Bash "
                 "timeout 600000; it waits inside the process, costs no tokens and prints the next step as soon as the "
                 "subagents are done; never `sleep` + `orkestra next` polling). The turn ends only after `orkestra "
                 "await` (manual runs) or `orkestra block`, or when the run is done.")
HEADLESS_WAIT_AUTO = HEADLESS_WAIT.replace("`orkestra await` (manual runs) or `orkestra block`", "`orkestra block`")


def _open_run_state(a: argparse.Namespace):
    """State of the run this call belongs to, or None (playbooks are also read before / outside a run)."""
    try:
        project = _resolve_project(getattr(a, "project", None), required=False)
        if project is None:
            return None
        return load_state(project, _run_id(project, getattr(a, "run", None)))
    except (OrkestraError, OSError, ValueError, AssertionError):
        return None


def cmd_playbook(a: argparse.Namespace) -> int:
    name = a.name
    st = _open_run_state(a)
    lead = []
    if not name:
        if not st or st.get("phase") in (None, "done"):
            avail = sorted(p.stem for p in PLAYBOOKS.glob("*.md"))
            raise OrkestraError(f"no open run here, so there is no current phase. Name one: run, {', '.join(avail)}")
        name = str(st["phase"])
    elif st and st.get("phase") not in (None, "done") and name != st["phase"] and (PLAYBOOKS / f"phase-{name}.md").exists():
        lead.append(f"NOTE: run {st.get('run_id')} is in phase `{st['phase']}`, not `{name}`. This is the `{name}` playbook "
                    f"you asked for; the one to follow now is `orkestra playbook` (no argument).")
    if st and paths.is_headless(st) and st.get("phase") not in (None, "done"):
        try:                    # said once per run: the next playbook call of this run does not repeat it
            proj = _resolve_project(getattr(a, "project", None), required=False)
            first = proj is None or T.once_per_run(proj, str(st.get("run_id")), "headless_wait")
        except (OrkestraError, OSError):
            first = True
        if first:
            lead.append(HEADLESS_WAIT_AUTO if T.is_autopilot(st) else HEADLESS_WAIT)
    if lead:
        _out("\n".join(lead) + "\n")
    path = _inside_dir(PLAYBOOKS, f"phase-{name}.md") or _inside_dir(PLAYBOOKS, f"{name}.md")
    if name == "run":           # the run skill itself: how an engine without the Claude Code plugin gets its start
        text = read_text(PLUGIN_ROOT / "skills" / "run" / "SKILL.md")
        text = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text else text
    elif path is None:
        avail = sorted(p.stem for p in PLAYBOOKS.glob("*.md"))
        raise OrkestraError(f"no playbook '{name}'. available: run, {', '.join(avail)}")
    else:
        text = read_text(path)
    voice = _ASKER or (str(st.get("engine")) if st else "")
    if voice in ("cursor", "copilot"):      # header, then the document in that engine's words
        sys.stdout.write(EN.with_notes(text, voice).rstrip("\n") + "\n")
        return 0
    _out(text)
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    """Start a run from the terminal with the engine of your choice (default: claude). Opens that engine's own
    interactive UI in the project with the request submitted — questions and the plan approval happen there.
    Opens the tracked run first (delivery or advisory) so `orkestra dashboard` / bridge flows can follow it."""
    import shlex
    if _ASKER:
        raise OrkestraError("`orkestra run` opens an agent UI: use it from your terminal. Inside an agent session "
                            "start the run with the run skill (/orkestra:run).")
    project = _resolve_project(a.project)
    assert project is not None
    request = " ".join(a.request or []).strip()
    if len(request) < 3:
        raise OrkestraError(i18n.t("cli.run_usage"))
    cfg = paths.global_config()
    try:
        engine = EN.normalize(a.engine, EN.default_engine(cfg))
    except EN.EngineError as exc:
        raise OrkestraError(str(exc))
    binary = EN.binary(engine, cfg)
    if not binary:
        raise OrkestraError(f"{engine} CLI not found on PATH" + (" (Cursor CLI: https://cursor.com/cli)" if engine == "cursor" else ""))
    if engine == "cursor":
        from . import cursorkit as CK
        if not CK.hooks_active(project):            # without them the agent would run unguarded
            raise OrkestraError("orkestra's hooks are not installed for Cursor — run `orkestra cursor install` once "
                                "(it keeps your own ~/.cursor/hooks.json entries), then try again.")
    copilot_flags = {"model": getattr(a, "model", None), "reasoning_effort": getattr(a, "effort", None),
                     "context": getattr(a, "context", None), "allow_all": getattr(a, "allow_all", None)}
    if engine != "copilot" and any(v is not None for v in copilot_flags.values()):
        raise OrkestraError(i18n.t("cli.run_copilot_flags_only"))
    plugin_dir = ""
    if engine == "copilot":
        plugin_dir = EN.copilot_plugin_dir(cfg)
        if not EN.copilot_hooks_active(plugin_dir):
            raise OrkestraError("Orkestra's Copilot hooks are not installed. Install the Orkestra plugin with "
                                "UserPromptSubmit, PreToolUse and PostToolUse hooks before starting a guarded Copilot run.")
    if engine == "pi":
        from . import pikit as PK
        if not PK.skills_installed() or not PK.guards_installed():
            raise OrkestraError(i18n.t("cli.pi_skills_required"))
        import uuid
        opts_pi_sid = uuid.uuid4().hex
    else:
        opts_pi_sid = ""
    opts = dict(((cfg.get("engine") or {}).get(engine)) or {})
    if engine == "copilot":
        try:                                        # flag > request words > config > default (engines.copilot_resolve)
            opts = EN.copilot_resolve(cfg, request, copilot_flags)
        except EN.EngineError as exc:
            raise OrkestraError(str(exc))
    if opts_pi_sid:
        opts["session_id"] = opts_pi_sid
        from . import pikit as PK
        opts["extension"] = str(PK.extension_path())
    if plugin_dir:
        opts["plugin_dir"] = plugin_dir
    approval_mode = a.approval_mode or ("manual" if intent.manual_approval_requested(request) else "autopilot")
    opts["approval_mode"] = approval_mode
    argv = EN.interactive_command(engine, binary, EN.run_prompt(engine, request), opts)
    if a.print:
        _out(f"cd {shlex.quote(str(project))} && " + " ".join(shlex.quote(x) for x in argv))
        return 0
    _require_project_config(project)
    _require_usable_home()
    st = R.open_run_for_request(project, request, engine=engine, mode="interactive", approval_mode=approval_mode)
    rid = st["run_id"]
    if engine == "copilot":                         # a resume (watchdog, bridge) reuses what this run started with
        from . import state as ST
        with ST.StateTxn(project, rid) as run_st:
            run_st["copilot_profile"] = EN.copilot_profile_of(opts)
    if opts_pi_sid:
        from . import activity as A
        from . import state as ST
        sid = EN.session_key("pi", opts_pi_sid)
        ST.bind_session(sid, project, rid, engine="pi")
        A.record(project, rid, {"session_id": sid}, "bind", "pi session")
    _out(i18n.t("cli.run_opened", run=rid, project=project.name, size=st.get("size") or ""))
    _out(f"artifacts: {paths.feature_dir(project, rid)}  (short path: .orkestra/current/)")
    _out(i18n.t("cli.run_dashboard_hint"))
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(str(project))
    env = EN.child_env(engine)
    if opts_pi_sid:
        env["ORKESTRA_PI_SESSION"] = opts_pi_sid
    env.pop("ORKESTRA_HEADLESS", None)              # this is the interactive UI: the user answers in it
    env["ORKESTRA_APPROVAL_MODE"] = approval_mode
    os.execve(binary, argv, env)
    return 0  # pragma: no cover


def cmd_cursor(a: argparse.Namespace) -> int:
    """Cursor side: build the generated skills/subagents, install them and the hooks into ~/.cursor, or check."""
    from . import cursorkit as CK
    if _ASKER and a.action != "build":          # `build` only regenerates this checkout's committed tree (AGENTS.md rule 2)
        raise OrkestraError("`orkestra cursor …` changes the Cursor configuration: run it from your terminal.")
    try:
        if a.action == "build":
            dry = bool(getattr(a, "dry_run", False))
            done = CK.build(dry=dry)
            _out(i18n.t(("cli.cursor_assets_would" if dry else "cli.cursor_assets_written") if done else "cli.cursor_assets_current",
                        count=len(done), files=", ".join(done)))
        elif a.action == "install":
            done = CK.install(copy=a.copy, dry=a.dry_run, fail_closed=a.fail_closed)
            for line in done:
                _out(i18n.t("cli.kit_would", action=line) if a.dry_run else line)
            _out(i18n.t("cli.cursor_nothing_to_change" if not done else
                        ("cli.cursor_dry_run" if a.dry_run else "cli.cursor_installed")))
        elif a.action == "uninstall":
            done = CK.uninstall(dry=a.dry_run)
            for line in done:
                _out(i18n.t("cli.kit_would", action=line) if a.dry_run else line)
            _out(i18n.t("cli.cursor_nothing_installed" if not done else
                        ("cli.cursor_uninstall_dry" if a.dry_run else "cli.cursor_removed")))
        if a.action in ("status", "install"):
            ok = True
            for good, label, detail in CK.status(paths.global_config()):
                ok = ok and good is not False
                _out(f"[{'i ' if good is None else ('OK' if good else '!!')}] {label}" + (f" — {detail}" if detail else ""))
            if a.action == "install":               # the install itself worked (or raised); the lines above say what
                return 0                            # else is still missing (Cursor CLI, PATH)
            return 0 if ok else 1
    except EN.EngineError as exc:
        raise OrkestraError(str(exc))
    return 0


def cmd_pi(a: argparse.Namespace) -> int:
    """Pi side: install/uninstall/status for skills and the tool extension under ~/.pi/agent."""
    from . import pikit as PK
    if _ASKER:
        raise OrkestraError("`orkestra pi …` changes the Pi configuration: run it from your terminal.")
    try:
        if a.action == "install":
            done = PK.install(dry=a.dry_run)
            for line in done:
                _out(i18n.t("cli.kit_would", action=line) if a.dry_run else line)
            if a.dry_run:
                summary = "cli.pi_dry_run"
            elif not done:
                summary = "cli.pi_nothing"
            elif PK.skills_installed() and PK.guards_installed():
                summary = "cli.pi_installed"
            else:
                summary = "cli.pi_partial"
            _out(i18n.t(summary))
        elif a.action == "uninstall":
            done = PK.uninstall(dry=a.dry_run)
            for line in done:
                _out(i18n.t("cli.kit_would", action=line) if a.dry_run else line)
            _out(i18n.t("cli.pi_nothing_installed" if not done else
                        ("cli.pi_dry_run" if a.dry_run else "cli.pi_removed")))
        if a.action in ("status", "install"):
            ok = True
            for good, label, detail in PK.status():
                ok = ok and good is not False
                _out(f"[{'i ' if good is None else ('OK' if good else '!!')}] {label}" + (f" — {detail}" if detail else ""))
            if a.action == "install":
                return 0
            return 0 if ok else 1
    except EN.EngineError as exc:
        raise OrkestraError(str(exc))
    return 0


def cmd_copilot(a: argparse.Namespace) -> int:
    """Copilot side: install/uninstall/status for hooks under ~/.copilot/plugins/orkestra."""
    from . import copilotkit as CK
    if _ASKER and not (a.action == "vscode" and not a.apply):
        raise OrkestraError("`orkestra copilot …` changes the Copilot configuration: run it from your terminal.")
    try:
        if a.action == "vscode":
            return _copilot_vscode(a, CK)
        if a.action == "install":
            done = CK.install(dry=a.dry_run)
            for line in done:
                _out(i18n.t("cli.kit_would", action=line) if a.dry_run else line)
            _out(i18n.t("cli.copilot_nothing_to_change" if not done else
                        ("cli.copilot_dry_run" if a.dry_run else "cli.copilot_installed")))
        elif a.action == "uninstall":
            done = CK.uninstall(dry=a.dry_run)
            for line in done:
                _out(i18n.t("cli.kit_would", action=line) if a.dry_run else line)
            _out(i18n.t("cli.copilot_nothing_installed" if not done else
                        ("cli.copilot_uninstall_dry" if a.dry_run else "cli.copilot_removed")))
        if a.action in ("status", "install"):
            ok = True
            for good, label, detail in CK.status(paths.global_config()):
                ok = ok and good is not False
                _out(f"[{'i ' if good is None else ('OK' if good else '!!')}] {label}" + (f" — {detail}" if detail else ""))
            if a.action == "install":
                return 0
            return 0 if ok else 1
    except EN.EngineError as exc:
        raise OrkestraError(str(exc))
    return 0


def _copilot_vscode(a: argparse.Namespace, CK: Any) -> int:
    """Show (default) or apply (--apply) the documented VS Code settings; say plainly what cannot be pinned."""
    if a.apply and bool(a.workspace) == bool(a.user):
        raise OrkestraError(i18n.t("cli.copilot_vscode_target"))
    if a.effort and not a.model:
        raise OrkestraError(i18n.t("cli.copilot_vscode_effort_needs_model"))
    plugin = EN.copilot_plugin_dir(paths.global_config()) or str(CK.plugin_dir())
    user_cfg = paths.read_json(paths.orkestra_home() / "config.json", default={}) or {}
    prof = EN.copilot_resolve(user_cfg if isinstance(user_cfg, dict) else {}, flags={"model": a.model})
    default_cfg = CK.DEFAULT_CONFIGURATION if prof["allow_all"] else None
    target = CK.vscode_workspace_settings(a.workspace) if a.workspace else CK.vscode_user_settings()
    dry = not a.apply or a.dry_run
    done = CK.vscode_settings(target, plugin, a.permissions, a.remove, dry, model=prof["model"],
                              default_config=default_cfg, plugin_locations=not a.workspace)
    if a.model and a.workspace:
        done += CK.vscode_agent(a.workspace, a.model, a.effort, a.remove, dry)
    elif a.remove and a.workspace:
        done += CK.vscode_agent(a.workspace, "", None, True, dry)
    for line in done:
        _out(i18n.t("cli.kit_would", action=line) if dry else line)
    _out(i18n.t("cli.copilot_nothing_to_change" if not done else
                ("cli.copilot_vscode_plan" if dry else "cli.copilot_vscode_done"), path=target))
    if not a.remove:
        if a.workspace:
            _out(i18n.t("cli.copilot_vscode_machine_scope"))
        if a.model and not a.workspace:
            _out(i18n.t("cli.copilot_vscode_agent_workspace"))
        _out(i18n.t("cli.copilot_vscode_limits"))
        if a.permissions or default_cfg:
            _out(i18n.t("cli.copilot_vscode_warn"))
    return 0


def cmd_mcp(a: argparse.Namespace) -> int:
    """Install or report MCP servers. An agent shell is refused before any install or status call."""
    if _ASKER:
        raise OrkestraError(i18n.t("cli.mcp_refused"))
    from . import mcpinstall
    names = tuple(a.only) if a.only else None
    try:
        if a.action == "install":
            rows = mcpinstall.install(a.engine, names)
        else:
            rows = mcpinstall.status(a.engine, names)
    except mcpinstall.McpError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    for row in rows:
        _out(i18n.t("cli.mcp_row", host=row["host"], name=row["name"], state=row["state"]))
    return 0


def _ask_yn(question: str, default: str = "y") -> bool:
    """Interactive yes/no for setup (same defaults shape as install.sh ask())."""
    hint = i18n.t("cli.ask_yes_default" if default == "y" else "cli.ask_no_default")
    try:
        raw = input(f"{question} [{hint}] ").strip()
    except EOFError:
        raw = ""
    ans = (raw or default).strip().lower()
    # the whole word, like install.sh and install.py: `exit` or `yeah` is not a yes, and neither is the Turkish `yok`
    # or `yapma`; `e` / `evet` answer the Turkish prompt only
    return ans in ("y", "yes") or (ans in ("e", "evet") and i18n.lang() == "tr")


def _setup_detect() -> Dict[str, bool]:
    """Which host engines look present (Claude on PATH, Cursor/Copilot home dirs)."""
    from . import cursorkit as Cur
    from . import copilotkit as Cop
    from . import pikit as PiK
    return {
        "claude": shutil.which("claude") is not None,
        "cursor": Cur.cursor_home().is_dir(),
        "copilot": Cop.copilot_home().is_dir(),
        "pi": PiK.detected(),
    }


def _setup_install_claude() -> bool:
    """Claude Code marketplace + plugin from this checkout (repo root next to plugins/). True only when every step
    worked; a marketplace that is registered from another location is registered again for this checkout."""
    root = PLUGIN_ROOT.parent.parent

    def step(*args: str) -> bool:
        try:
            return subprocess.run(["claude", *args], check=False, timeout=120).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    try:
        listed = subprocess.run(["claude", "plugin", "marketplace", "list"], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=120)
        text = (listed.stdout or "") if listed.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        text = ""
    ok = True
    registered = "orkestra-local" in text
    if registered and not any(form in text for form in {str(root), os.path.realpath(str(root))}):
        _out(i18n.t("cli.setup_marketplace_moved", name="orkestra-local", root=root))
        ok = step("plugin", "marketplace", "remove", "orkestra-local")
        registered = False
    ok = (step("plugin", "marketplace", "update", "orkestra-local") if registered
          else step("plugin", "marketplace", "add", str(root))) and ok
    ok = step("plugin", "install", "orkestra@orkestra-local", "-s", "user") and ok
    # An install over an existing install keeps the cached copy; `update` refreshes it. "Already up to date" is a
    # non-zero exit on some versions, so a failed update never fails the setup.
    if ok and step("plugin", "update", "orkestra@orkestra-local"):
        _out(i18n.t("cli.setup_claude_restart"))
    return ok


def cmd_setup(a: argparse.Namespace) -> int:
    """Detect Claude/Cursor/Copilot and install Orkestra for each (ask, or --yes for all detected)."""
    from . import cursorkit as Cur
    from . import copilotkit as Cop
    if _ASKER:
        raise OrkestraError(i18n.t("cli.setup_refused_agent"))
    # FR-003: PATH shim always (no engine / declined engines); agent refuse above skips this.
    Cur.ensure_path_shim()
    detected = _setup_detect()
    if not any(detected.values()):
        _out(i18n.t("cli.setup_no_engine"))
        return 1
    yes = bool(getattr(a, "yes", False))

    def state(key: str) -> str:
        return i18n.t("cli.setup_state_" + key)

    failed = False
    if detected["claude"]:
        if yes or _ask_yn(i18n.t("cli.setup_ask_claude"), "y"):
            if _setup_install_claude() is False:        # the claude commands printed why; "done" would be a lie
                failed = True
                _out(i18n.t("cli.setup_line", engine="Claude marketplace/plugin", state=state("failed")))
                _out(i18n.t("cli.setup_claude_manual", root=PLUGIN_ROOT.parent.parent))
            else:
                _out(i18n.t("cli.setup_line", engine="Claude marketplace/plugin", state=state("done")))
        else:
            _out(i18n.t("cli.setup_line", engine="Claude", state=state("skipped")))
    if detected["cursor"]:
        if yes or _ask_yn(i18n.t("cli.setup_ask_cursor"), "y"):
            try:
                done = Cur.install()
            except EN.EngineError as exc:
                raise OrkestraError(str(exc))
            for line in done:
                _out(line)
            _out(i18n.t("cli.setup_line", engine="Cursor", state=state("current" if not done else "installed")))
        else:
            _out(i18n.t("cli.setup_line", engine="Cursor", state=state("skipped")))
    if detected.get("pi"):
        from . import pikit as PiK
        if yes or _ask_yn(i18n.t("cli.setup_ask_pi"), "y"):
            try:
                done = PiK.install()
            except EN.EngineError as exc:
                raise OrkestraError(str(exc))
            for line in done:
                _out(line)
            _out(i18n.t("cli.setup_line", engine="Pi", state=state("current" if not done else "installed")))
        else:
            _out(i18n.t("cli.setup_line", engine="Pi", state=state("skipped")))
    if detected["copilot"]:
        if yes or _ask_yn(i18n.t("cli.setup_ask_copilot"), "y"):
            try:
                done = Cop.install()
            except EN.EngineError as exc:
                raise OrkestraError(str(exc))
            for line in done:
                _out(line)
            _out(i18n.t("cli.setup_line", engine="Copilot", state=state("current" if not done else "installed")))
        else:
            _out(i18n.t("cli.setup_line", engine="Copilot", state=state("skipped")))
    return 1 if failed else 0


def _inside_dir(root: Path, name: str) -> Optional[Path]:
    """`root/name` when it is an existing file that stays inside `root`, else None. A name is a lookup key, not a path:
    `../../x`, an absolute path or a symlink pointing out must never reach a file the document folders do not hold."""
    try:
        path = (root / name).resolve()
        base = root.resolve()
    except (OSError, ValueError):
        return None
    return path if path.is_file() and (path == base or base in path.parents) else None


def cmd_template(a: argparse.Namespace) -> int:
    root = i18n.template_dir(TEMPLATES)
    path = _inside_dir(root, a.name) or _inside_dir(root, f"{a.name}.md")
    if path is None:
        avail = sorted(str(p.relative_to(root)) for p in root.rglob("*.md"))
        raise OrkestraError(f"no template '{a.name}'. available: {', '.join(avail)}")
    _out(read_text(path))
    return 0


def cmd_watch(a: argparse.Namespace) -> int:
    from . import monitor
    project = _resolve_project(a.project) if a.project else None
    return monitor.watch(project, max(0.5, a.interval), a.once, a.all, a.json)


def cmd_dashboard(a: argparse.Namespace) -> int:
    from . import monitor
    project = _resolve_project(a.project) if a.project else None
    return monitor.dashboard(project, a.port, a.open, replace=getattr(a, "replace", False))


# ====================================================================== language

_LANG_FLAG: Optional[str] = None        # the value of `--lang` while main() runs it (it travels as ORKESTRA_LANG)


def _lang_origin(source: str) -> str:
    """Where the language comes from, for humans: `env (--lang)`, `env (ORKESTRA_LANG)`, `config (<file>)`, `locale`,
    `default`."""
    if source == "env":
        return "env (--lang)" if _LANG_FLAG else "env (ORKESTRA_LANG)"
    if source == "config":
        return f"config ({paths.orkestra_home() / 'config.json'})"
    return source


def _backup_aside(path: Path, raw: bytes) -> Path:
    """Keep the bytes of a config file we are about to replace; never overwrites an earlier backup."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(100):
        dest = path.with_name(f"{path.name}.corrupt-{stamp}" + (f"-{n}" if n else ""))
        try:
            # 0600 like the config.json that replaces it: the file can hold notify tokens and webhook URLs
            fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            return dest
        except FileExistsError:
            continue
        except OSError as exc:
            raise OrkestraError(i18n.t("help.lang_unwritable", path=dest, error=exc))
    raise OrkestraError(i18n.t("help.lang_unwritable", path=path, error="too many backups"))


def _save_lang(value: str) -> Any:
    """Persist `lang` in the global config.json: atomic, every other key kept, created when missing. A file that is
    not a JSON object is copied aside first (never silently destroyed) and then replaced. Returns (path, backup)."""
    from .util import write_json_atomic
    path = paths.orkestra_home() / "config.json"
    data: Dict[str, Any] = {}
    backup: Optional[Path] = None
    try:
        raw: Optional[bytes] = path.read_bytes()
    except FileNotFoundError:
        raw = None
    except OSError as exc:                              # a directory, no permission, ...
        raise OrkestraError(i18n.t("help.lang_unreadable", path=path, error=exc))
    if raw is not None and raw.strip():
        try:
            parsed: Any = json.loads(raw.decode("utf-8-sig"))     # a BOM (some Windows editors) is not corruption
        except (ValueError, RecursionError):                     # not JSON, not UTF-8, absurdly nested
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
        else:
            backup = _backup_aside(path, raw)
    data["lang"] = value
    target = path.resolve() if path.is_symlink() else path      # a dotfiles-managed symlink must stay a symlink
    try:
        write_json_atomic(target, data)
    except OSError as exc:
        raise OrkestraError(i18n.t("help.lang_unwritable", path=path, error=exc))
    return path, backup


def _announce_saved_lang(value: str) -> None:
    path, backup = _save_lang(value)
    _out(i18n.t("help.lang_saved_auto" if value == "auto" else "help.lang_saved", lang=value, path=path))
    if backup:
        _out(i18n.t("help.lang_backup", path=backup))
    code, source = i18n.lang_source()
    if source == "env" and (value == "auto" or code != value):
        _out(i18n.t("help.lang_override", origin="--lang" if _LANG_FLAG else "ORKESTRA_LANG", lang=code))


def cmd_lang(a: argparse.Namespace) -> int:
    """`orkestra lang [show|en|tr|auto]`: where the language of human-facing text comes from, or save a choice."""
    value = getattr(a, "value", None) or "show"
    if value == "show":
        code, source = i18n.lang_source()
        _out(f"language: {code}")
        _out(f"source: {_lang_origin(source)}")
        _out(i18n.t("help.lang_hint"))
        return 0
    _announce_saved_lang(value)
    return 0


def _tree_digest(root: Path) -> str:
    """Digest of the code that runs the guards (hooks + library) under `root`; '' when there is none."""
    import hashlib
    h = hashlib.sha1()
    found = False
    for pattern in ("hooks/*.py", "hooks/*.json", "hooks/*.sh", "lib/orkestra_core/*.py", "lib/orkestra_core/messages/*.py"):
        for f in sorted(root.glob(pattern)):
            try:
                data = f.read_bytes()
            except OSError:
                continue
            found = True
            h.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0" + hashlib.sha1(data).digest())
    return h.hexdigest() if found else ""


def _claude_copies() -> List[Tuple[str, str, bool]]:
    """(install path, version, same code as this checkout) of every other copy Claude Code registered for orkestra.
    Claude Code runs hooks from its own cached copy, so a checkout edited after the install is NOT what the guards
    run until the plugin is updated. Read-only; never touches ~/.claude."""
    reg = read_json(Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "plugins"
                    / "installed_plugins.json", default={}) or {}
    mine = _tree_digest(PLUGIN_ROOT)
    out: List[Tuple[str, str, bool]] = []
    plugins = reg.get("plugins") if isinstance(reg, dict) else None
    for name, entries in (plugins.items() if isinstance(plugins, dict) else []):
        if not str(name).startswith("orkestra@"):
            continue
        for e in entries if isinstance(entries, list) else []:
            raw = str(e.get("installPath") or "") if isinstance(e, dict) else ""
            path = Path(raw).expanduser()
            if not raw or not path.is_dir() or os.path.realpath(path) == os.path.realpath(PLUGIN_ROOT):
                continue
            out.append((str(path), str(e.get("version") or "?"), bool(mine) and _tree_digest(path) == mine))
    return out


def _claude_plugin_registered() -> bool:
    """True when Claude Code's plugin registry lists orkestra (read-only; never touches ~/.claude)."""
    reg = read_json(Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "plugins"
                    / "installed_plugins.json", default={}) or {}
    plugins = reg.get("plugins") if isinstance(reg, dict) else None
    return any(str(name).startswith("orkestra@") and entries
               for name, entries in (plugins.items() if isinstance(plugins, dict) else []))


def _repair_damaged_runs(project_arg: Optional[str] = None) -> int:
    """doctor --repair: restore state.json.prev, else move the run aside to runs/<id>.damaged. Never deletes."""
    from . import state as ST
    project = _resolve_project(project_arg, required=False)
    if project is None:
        _out(i18n.t("cli.repair_no_project"))
        return 1
    rdir = paths.runs_dir(project)
    damaged = [d for d in sorted(rdir.iterdir()) if d.is_dir() and not ST.is_aside_copy(d.name)
               and ST.is_damaged(d)] if rdir.is_dir() else []
    if not damaged:
        _out(i18n.t("cli.repair_none", project=project))
        return 0
    code = 0
    for d in damaged:
        try:
            if ST.restore_prev(d):
                _out(i18n.t("cli.repair_restored", run_id=d.name))
                continue
            dest = d.with_name(d.name + ".damaged")
            n = 1
            while dest.exists():
                n += 1
                dest = d.with_name(f"{d.name}.damaged{n}")
            d.rename(dest)
            _out(i18n.t("cli.repair_moved", run_id=d.name, dest=dest))
        except OSError as exc:
            _out(i18n.t("cli.repair_failed", run_id=d.name, error=exc))
            code = 1
    return code


def cmd_doctor(a: argparse.Namespace) -> int:
    if getattr(a, "repair", False):
        return _repair_damaged_runs(getattr(a, "project", None))
    ok = True

    def line(status: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and status
        _out(f"[{'OK' if status else '!!'}] {label}" + (f" — {detail}" if detail else ""))

    def soft(found: bool, label: str, detail: str = "") -> None:
        _out(f"[{'OK' if found else '--'}] {label}" + (f" — {detail}" if detail else ""))

    py = sys.version_info
    line(py >= (3, 9), "python >= 3.9", platform.python_version())
    line(shutil.which("git") is not None, "git", shutil.which("git") or i18n.t("cli.doctor_not_found"))
    soft(shutil.which("claude") is not None, "claude", shutil.which("claude") or i18n.t("cli.doctor_claude_missing"))
    if shutil.which("claude") is not None:
        registered = _claude_plugin_registered()
        soft(registered, "claude plugin", i18n.t("cli.doctor_plugin_registered" if registered
                                                 else "cli.doctor_plugin_missing"))
    for tool in ("node", "npx", "uv", "uvx", "gh", "jq"):
        soft(shutil.which(tool) is not None, tool, shutil.which(tool) or i18n.t("cli.doctor_not_found_optional"))
    config_problem = _global_config_problem()
    if config_problem:                              # a wrong-typed setting must not stop the report that names it
        ws = Path(os.environ.get("ORKESTRA_WORKSPACE") or paths.DEFAULT_GLOBAL_CONFIG["workspace_root"]).expanduser()
        cfg = dict(paths.DEFAULT_GLOBAL_CONFIG)
    else:
        ws = paths.workspace_root()
        cfg = paths.global_config()
    soft(ws.is_dir(), "workspace root", str(ws) if ws.is_dir() else i18n.t("cli.doctor_workspace_missing", path=ws))
    home = paths.orkestra_home()
    try:
        paths.ensure_global_config()
    except OSError as exc:                          # a file where the folder should be, no permission, a full disk
        line(False, "orkestra home", i18n.t("cli.home_unusable", path=home, error=_home_reason(home, exc)))
    else:
        line(home.is_dir(), "orkestra home", str(home))
    if config_problem:
        line(False, "config.json", config_problem)
    try:                                            # informational, never a failure: `orkestra lang` changes it
        code, source = i18n.lang_source()
        _out(f"[OK] language: {code} ({_lang_origin(source)})")
    except Exception as exc:  # pragma: no cover
        _out(f"[--] language: unknown ({type(exc).__name__})")
    line((PLUGIN_ROOT / "hooks" / "hooks.json").exists(), "plugin root", str(PLUGIN_ROOT))
    # hooks/hooks.json starts hooks/run_hook.sh, which tries python3, then python, then `py -3`: only when none of them
    # resolves do the Claude Code hooks fail open
    py3 = shutil.which("python3")
    fallback = None if py3 else (shutil.which("python") or shutil.which("py"))
    soft(py3 is not None, "python3 on PATH (plugin hooks)",
         py3 or (i18n.t("cli.doctor_python3_fallback", fallback=fallback) if fallback else
                 i18n.t("cli.doctor_python3_none")))
    for path, version, same in _claude_copies():
        soft(same, "claude plugin copy",
             i18n.t("cli.doctor_copy_same" if same else "cli.doctor_copy_differs", path=path, version=version))
    # hook self-test: a forbidden command must be denied, a harmless one allowed
    disp = PLUGIN_ROOT / "hooks" / "dispatch.py"
    try:
        ev = {"hook_event_name": "PreToolUse", "session_id": "doctor", "tool_name": "Bash",
              "tool_input": {"command": "git push --force origin main"}, "cwd": str(Path.cwd())}
        cp = subprocess.run([sys.executable, str(disp), "PreToolUse"], input=json.dumps(ev), capture_output=True,
                            encoding="utf-8", errors="replace", timeout=20)
        denied = '"deny"' in cp.stdout
        line(denied, "hook self-test (deny force-push)",
             i18n.t("cli.doctor_hook_denied") if denied else (cp.stdout.strip()[:80] or cp.stderr.strip()[:80]))
    except Exception as exc:  # pragma: no cover
        line(False, "hook self-test", str(exc))
    from . import cursorkit as CK
    claude_ready = shutil.which("claude") is not None
    cursor_ready = CK.hooks_active()
    copilot_dir = EN.copilot_plugin_dir(cfg)
    copilot_ready = EN.copilot_hooks_active(copilot_dir)
    soft(cursor_ready, "cursor hooks", i18n.t("cli.doctor_active" if cursor_ready else "cli.doctor_cursor_not_installed"))
    soft(copilot_ready, "copilot hooks", i18n.t("cli.doctor_active" if copilot_ready else "cli.doctor_copilot_not_installed"))
    from . import copilotkit as Cop2
    profile_detail, context_ok = Cop2.profile_info(cfg, EN.binary("copilot", cfg))
    soft(True, "copilot profile", profile_detail)
    if context_ok is False:
        soft(False, "copilot context", i18n.t("cli.kit_context_unsupported"))
    from . import pikit as PiK
    pi_rows = {label: (good, detail) for good, label, detail in PiK.status()}
    pi_skills_ok, pi_skills_detail = pi_rows["pi skills"]
    soft(bool(pi_skills_ok), "pi skills", pi_skills_detail)
    pi_guard_ok, pi_guard_detail = pi_rows["pi extension"]
    soft(bool(pi_guard_ok), "pi extension", pi_guard_detail)
    pi_bin_ok, pi_bin_detail = pi_rows["pi"]
    soft(bool(pi_bin_ok), "pi", pi_bin_detail)
    try:                                            # informational: the fast stall watcher (POSIX only)
        from . import fastwatch
        fw = fastwatch.status()
        soft(fw["running"], i18n.t("watchdog.fast_doctor_name"),
             i18n.t("watchdog.fast_doctor_on", pid=fw["pid"]) if fw["running"]
             else i18n.t("watchdog.fast_doctor_off", autostart="on" if fw["autostart"] else "off"))
    except Exception as exc:  # pragma: no cover
        _out(f"[--] fast watcher: unknown ({type(exc).__name__})")
    engines_ready = sum((claude_ready, cursor_ready, copilot_ready, bool(pi_skills_ok and pi_guard_ok and pi_bin_ok)))
    line(engines_ready >= 1, "engine ready",
         i18n.t("cli.doctor_engines_ready", count=engines_ready) if engines_ready else i18n.t("cli.doctor_engines_none"))
    if claude_ready:
        try:
            cp = subprocess.run(["claude", "mcp", "list"], capture_output=True, encoding="utf-8", errors="replace",
                                timeout=30, stdin=subprocess.DEVNULL)
            out = cp.stdout.lower()
            for name in ("context7", "playwright", "serena"):
                soft(name in out, f"MCP {name}", i18n.t("cli.doctor_configured" if name in out else "cli.doctor_not_configured"))
        except Exception:
            soft(False, "claude mcp list", i18n.t("cli.doctor_mcp_failed"))
    _out(i18n.t("cli.doctor_all_passed" if ok else "cli.doctor_some_failed"))
    if ok:
        _out(i18n.t("cli.doctor_next"))
    return 0 if ok else 1


# ====================================================================== parser

HANDLERS = {
    "context": cmd_context, "init": cmd_init, "start": cmd_start, "resume": cmd_resume, "status": cmd_status,
    "phase": cmd_phase, "await": cmd_status_change, "block": cmd_status_change, "unblock": cmd_status_change,
    "pause": cmd_status_change, "abort": cmd_status_change, "gate": cmd_gate, "tdd": cmd_tdd, "task": cmd_task,
    "brief": cmd_brief, "plan": cmd_check, "trace": cmd_check, "deps": cmd_check, "review": cmd_check,
    "changes": cmd_changes, "diff": cmd_diff, "rollback": cmd_rollback, "clean": cmd_clean, "memory": cmd_memory,
    "playbook": cmd_playbook, "template": cmd_template, "doctor": cmd_doctor, "pi": cmd_pi,
    "watch": cmd_watch, "dashboard": cmd_dashboard,
    "evidence": cmd_evidence, "integrity": cmd_integrity, "report": cmd_report,
    "next": cmd_next, "wait": cmd_wait, "tempo": cmd_tempo, "watchdog": cmd_watchdog, "agents": cmd_agents,
    "run": cmd_run, "cursor": cmd_cursor, "copilot": cmd_copilot, "mcp": cmd_mcp, "setup": cmd_setup, "lang": cmd_lang,
}


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        _utf8_stdio()                                   # run as the command itself: its streams are ours to configure
    try:
        rc = _main(argv)
        sys.stdout.flush()                              # a reader that went away (`| head`) shows up here, not at exit
        return rc
    except BrokenPipeError:
        # Not an error of ours. Point stdout at the null device so the flush at interpreter exit stays quiet too.
        with contextlib.suppress(Exception):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _main(argv: Optional[List[str]] = None) -> int:
    global _LANG_FLAG
    raw = list(sys.argv[1:] if argv is None else argv)
    flag = requested_lang(raw)
    if not flag:
        return _dispatch(raw)
    # `--lang` travels as ORKESTRA_LANG, set BEFORE the parser is built: --help, the output of this command and every
    # child process (hooks, engines) speak that language. An embedding caller gets its environment back.
    saved = os.environ.get("ORKESTRA_LANG")
    os.environ["ORKESTRA_LANG"] = flag
    _LANG_FLAG = flag
    try:
        return _dispatch(raw)
    finally:
        _LANG_FLAG = None
        if saved is None:
            os.environ.pop("ORKESTRA_LANG", None)
        else:
            os.environ["ORKESTRA_LANG"] = saved


def _dispatch(raw: List[str]) -> int:
    parser = build_parser()
    a = parse_argv(parser, raw)
    if not a.cmd:
        parser.print_help()
        return 0
    if a.cmd == "version":
        _out(f"orkestra {__version__}")
        return 0
    try:
        rc = HANDLERS[a.cmd](a)
        if rc == 0 and a.cmd == "setup" and getattr(a, "lang", None):
            _announce_saved_lang(a.lang)                # `setup --lang tr`: the install-time choice is kept
        return rc
    except OrkestraError as exc:
        sys.stderr.write(f"orkestra: {exc}\n")
        return 2
    except BrokenPipeError:
        raise                                           # main() handles the vanished reader
    except OSError as exc:                              # a folder that cannot be written, a full disk, a vanished file
        sys.stderr.write("orkestra: " + i18n.t("cli.io_error", detail=_io_detail(exc)) + "\n")
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:                            # a bug, or a hand-edited file nobody expected: one line, not a traceback
        if os.environ.get("ORKESTRA_DEBUG") == "1":
            raise
        sys.stderr.write("orkestra: " + i18n.t("cli.internal_error", error=f"{type(exc).__name__}: {exc}") + "\n")
        return 2                                        # not 1: scripts read 1 as "the check failed" (gate, doctor)
