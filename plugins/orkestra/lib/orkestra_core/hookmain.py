"""The code of every Orkestra hook (Claude Code events), run by hooks/dispatch.py:  python3 dispatch.py <EventName>
(event JSON on stdin).

This file is NOT executed as a script: orkestra_core.hookboot compiles it once into a cached .pyc and executes it in
the namespace of hooks/dispatch.py, so `import dispatch` still gives every name below (tests and the Cursor / Copilot
adapters patch and call them there) while a hook process no longer recompiles ~1200 lines on every call.

Design rules
  * Deterministic, stdlib only, fast. No network. A module is imported when the code that needs it runs
    (`LazyModule`), not at start-up: a hook of an event that needs no run state pays for json, os and paths only.
  * Deny decisions are explicit JSON (permissionDecision=deny) with an actionable reason.
  * Any internal error FAILS OPEN (exit 0) and is logged to ~/.orkestra/logs/hooks.log,
    so a bug here can never brick the user's Claude Code session.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# sys.path already holds the lib directory (dispatch.py puts it there before anything is imported).
from orkestra_core import paths  # noqa: E402
from orkestra_core import state as ST  # noqa: E402
from orkestra_core.hookboot import utf8_stdio  # noqa: E402,F401  (re-exported: the Copilot adapter calls dispatch.utf8_stdio)
from orkestra_core.i18n import t as _t  # noqa: E402  (`t` is a common local name in this file)
from orkestra_core.lazy import LazyModule  # noqa: E402
from orkestra_core.util import file_lock, flock_held_elsewhere, now_iso, read_json, resolve_path, write_json_atomic  # noqa: E402

# Imported on first use; call sites and tests treat them as the modules (see orkestra_core/lazy.py).
guards = LazyModule("guards", "orkestra_core")
sp = LazyModule("shellparse", "orkestra_core")
activity = LazyModule("activity", "orkestra_core")
intent = LazyModule("intent", "orkestra_core")
runs = LazyModule("runs", "orkestra_core")
snapshot = LazyModule("snapshot", "orkestra_core")
tempo = LazyModule("tempo", "orkestra_core")
CP = LazyModule("cliparse", "orkestra_core")


def short_sid(session_id: str) -> str:
    """engines.short_sid, imported when a log line or note needs it (engines pulls in subprocess, shutil, tempfile)."""
    from orkestra_core.engines import short_sid as _short_sid
    return _short_sid(session_id)

# `home=` (the ORKESTRA_HOME of the CLI that printed the line) is absent on lines of older CLIs: they keep working
BIND_RE = re.compile(r"^\[orkestra:bind\] project=(.+?) run=(\S+)(?: home=(.+?))?\s*$", re.M)
UNBIND_RE = re.compile(r"^\[orkestra:unbind\] project=(.+?) run=(\S+)(?: home=(.+?))?\s*$", re.M)
# The CLI subcommands that print each marker. A marker in the output of any other command (cat of a log, grep, a test
# run, an echo) is text, not the CLI speaking: it neither binds nor unbinds.
BIND_COMMANDS = ("start", "resume")
UNBIND_COMMANDS = ("pause", "abort", "phase:next")
MAX_STOP_BLOCKS_PER_PROMPT = 2       # per progress state (see progress_key), not per prompt id
STOP_NUDGE_CEILING = 6               # and at most this many Stop nudges per run ...
STOP_NUDGE_WINDOW = 1800.0           # ... in this many seconds, whatever the progress does
# deny escalation: identical refusals of one actor are counted; 2nd = short note, 3rd = "do not retry", 8th in 10 min = blocked
DENY_NOTE_AT, DENY_SHORT_AT = 2, 3
DENY_BLOCK_HITS, DENY_BLOCK_WINDOW, DENY_WINDOW, DENY_MAX_KEYS = 8, 600.0, 1800.0, 200
DENY_FAMILY_AT = 6              # refusals of ONE guard to ONE actor, however the call is varied, that count as a loop
STATUS_LINE_RE = re.compile(r"\b(?:STATUS|VERDICT):\s*[A-Z_]+[^\n]{0,80}")


GUARD_DEADLINE_SECONDS = 9          # wall-clock hard stop of one hook; hooks.json gives each hook 15 s
HOOK_WALL_SECONDS = GUARD_DEADLINE_SECONDS
HOOK_CPU_SECONDS = 6.0              # CPU time one hook may burn: a loaded machine is slow, not "complex"


class _Deadline(BaseException):
    """The hook ran out of time. A BaseException on purpose: the many `except Exception` blocks of the guards must
    never swallow it (a swallowed alarm let a command through with a wrong default). kind: "cpu" | "wall"."""

    def __init__(self, kind: str = "wall"):
        super().__init__(kind)
        self.kind = kind


def _run_in_thread(seconds: float, fn):
    """Run fn() in a daemon worker thread and stop waiting after `seconds` (the fallback where SIGALRM does not exist).

    A thread cannot be interrupted: on a timeout the worker is abandoned, which is safe here because a hook answers
    and exits right after (a daemon thread does not keep the process alive) and `fn` only computes a decision."""
    import threading
    box: dict = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:                 # handed to the caller's thread below
            box["error"] = exc

    worker = threading.Thread(target=work, name="orkestra-deadline", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise _Deadline()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _with_deadline(seconds: int, fn):
    """Run fn(); raise _Deadline if it takes longer.

    POSIX main thread: SIGALRM interrupts fn in place. No SIGALRM (Windows): fn runs in a worker thread and the call
    is abandoned after `seconds`. POSIX but not the main thread (signal() needs it): no limit, as before."""
    import signal
    if not hasattr(signal, "SIGALRM"):
        return _run_in_thread(seconds, fn)

    def on_alarm(*_):
        raise _Deadline()

    try:
        old = signal.signal(signal.SIGALRM, on_alarm)
    except ValueError:                              # not the main thread
        return fn()
    signal.alarm(seconds)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


_ANSWER = {"closed": False, "emitted": False}


def emit(obj: dict) -> None:
    if _ANSWER["closed"]:               # the deadline already answered (an abandoned worker thread must stay silent)
        return
    _ANSWER["emitted"] = True
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def run_with_hook_deadline(fn, cpu=None, wall=None):
    """Run fn() under ONE deadline for the whole hook: `cpu` seconds of CPU time (SIGPROF) and `wall` seconds of wall
    clock (SIGALRM, the hard stop well inside the host's 15 s timeout). Raises _Deadline(kind). Without setitimer
    (Windows) fn runs in a worker thread that is abandoned after `wall`; off the main thread there is no limit."""
    import signal
    _ANSWER["cpu0"] = time.process_time()
    cpu = HOOK_CPU_SECONDS if cpu is None else cpu
    wall = HOOK_WALL_SECONDS if wall is None else wall
    if not (hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")):
        return _run_in_thread(wall, fn)
    fired: list = []
    prof = hasattr(signal, "SIGPROF") and hasattr(signal, "ITIMER_PROF")

    def on_signal(signum, _frame):
        if fired:                       # one-shot: a second signal while unwinding must not raise again
            return
        fired.append(1)
        raise _Deadline("cpu" if prof and signum == signal.SIGPROF else "wall")

    try:
        old_alrm = signal.signal(signal.SIGALRM, on_signal)
    except ValueError:                  # not the main thread
        return fn()
    old_prof = signal.signal(signal.SIGPROF, on_signal) if prof else None
    signal.setitimer(signal.ITIMER_REAL, wall)
    if prof:
        signal.setitimer(signal.ITIMER_PROF, cpu)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if prof:
            signal.setitimer(signal.ITIMER_PROF, 0)
            signal.signal(signal.SIGPROF, old_prof)
        signal.signal(signal.SIGALRM, old_alrm)


def deadline_denial(exc: "_Deadline"):
    """The PreToolUse answer when the deadline hit: refusing is the only safe answer (a timed-out hook lets the call
    run unchecked). CPU deadline = the command itself is too complex; wall deadline with little CPU = a busy machine."""
    if exc.kind == "wall" and time.process_time() - _ANSWER.get("cpu0", 0.0) < HOOK_WALL_SECONDS / 2:
        return guards.deny("busy", "The machine is busy and orkestra could not check this call in time. Retry the "
                                   "same call once; do not rewrite it.")
    return guards.deny("complex", f"This call could not be analysed within {HOOK_CPU_SECONDS:g} s of CPU. Split the "
                                  f"command, or put it in a script file under the project and run that.")


def log(msg: str) -> None:
    try:
        d = paths.logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hooks.log", "a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


def context_out(event_name: str, text: str) -> None:
    if text:
        emit({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}})


# ---------------------------------------------------------------- helpers

def binding_for(ev: dict):
    sid = ev.get("session_id") or ""
    b = ST.session_binding(sid)
    if b:
        ST.touch_session(sid)               # heartbeat (lease for takeover / watchdog)
        return b
    try:                                    # nothing valid: a binding file that points nowhere is dropped now (it would
        why = ST.heal_binding(sid)          # otherwise sit there until the 21-day prune): run gone / closed, project gone
        if why:
            log(f"dropped the binding of session {short_sid(sid)}: {why}")
    except Exception as exc:                # healing is a convenience: never let it break the event
        log(f"heal_binding failed: {exc}")
    return None


def is_subagent_event(ev: dict) -> bool:
    """Did a subagent (not the main chat) cause this event? Claude Code reports a subagent's tool calls under the
    PARENT session id plus agent_id (the field its own schema names as THE sign of a subagent) and agent_type; the
    Cursor adapter sets both, the Copilot adapter agent_type. A lone agent_type is also what the main thread of a
    session started with `--agent` carries: orkestra has always treated that as an agent too (guards._agent_role). The
    keys are read in both spellings, so a host that renames them still counts."""
    return any(ev.get(k) for k in ("agent_type", "agent_id", "agentType", "agentId"))


def _same_home(text: str) -> bool:
    """Is `home=` of a marker this hook's own ORKESTRA_HOME? (the marker of a CLI run with another home is for that home)"""
    def norm(p: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.expanduser(p)))
    try:
        return norm(text) == norm(str(paths.orkestra_home()))
    except (OSError, ValueError):
        return False


def cli_keys(command: str) -> set:
    """The orkestra subcommands a Bash command line runs (`start`, `phase:next`, ...), read with the CLI's own parser.
    Sees through `cd x && orkestra ...`, env prefixes, `timeout`, `bash -c '...'` and `python3 <path>/orkestra ...`."""
    keys: set = set()
    for seg in sp.segments(command):
        args = list(seg.args)
        if not guards.is_orkestra_word(seg.program):
            rest = guards._orkestra_argv(seg)
            if rest is None:
                continue
            args = rest                     # `python3 [-u ...] /path/bin/orkestra start ...`
        key = CP.inspect(args)[0] or _loose_key(args)
        if key:
            keys.add(key)
    return keys


def _loose_key(args: list) -> str:
    """The subcommand of an `orkestra` call that does not parse (abbreviated in a test, or cut off by the host): the CLI
    would have refused it and printed no marker, so this only matters for fake events; read the words, accept nothing
    that is not a subcommand name."""
    rest = CP.hoist_globals(list(args))
    while rest and (rest[0] in CP.GLOBAL_VALUE_OPTS or rest[0].startswith(CP.GLOBAL_EQ_PREFIXES)):
        rest = rest[2:] if rest[0] in CP.GLOBAL_VALUE_OPTS else rest[1:]
    words = [w for w in rest if not w.startswith("-")]
    if not words:
        return ""
    if words[0] in CP.SUB_ACTION and len(words) > 1:
        return f"{words[0]}:{words[1]}"
    return words[0]


def next_hint(project: Path, st: dict, rows=None) -> str:
    """The deterministic next step for hook messages (cheap: small files only). Empty on any error."""
    try:
        fresh = ST.load_state(project, st["run_id"])
        lines = tempo.next_lines(tempo.next_action(project, fresh, deep=False), tempo.flags(project, fresh, rows))
        return "\n".join(lines[:2])
    except Exception as exc:
        log(f"next_hint failed: {exc}")
        return ""


def release_hook_wait(project: Path, st: dict, why: str, min_age: float = 0.0) -> dict:
    """`awaiting_user` set by the AskUserQuestion hook ends with that tool call. If the question was dismissed (no
    PostToolUse) the status used to stay: no hint, no nudge, `watch` kept saying "waiting for you"."""
    aw = st.get("awaiting") or {}
    if st.get("status") != "awaiting_user" or aw.get("source") != "hook":
        return st
    at = tempo._ts(aw.get("at")) or 0.0
    if time.time() - at < min_age:                  # tool calls sent in the same message as the question
        return st
    try:
        runs.set_status(project, st["run_id"], "active", why)
        return ST.load_state(project, st["run_id"])
    except Exception as exc:
        log(f"release_hook_wait failed: {exc}")
        return st


def run_summary(project: Path, st: dict) -> str:
    tasks = st.get("tasks") or {}
    done = sum(1 for t in tasks.values() if t.get("status") in ("done", "deferred"))
    prog = f" · tasks {done}/{len(tasks)}" if tasks else ""
    hint = next_hint(project, st)
    # The next hint already names the one command. Asking for a full context dump and the phase playbook on every
    # resume made the model re-read both.
    text = (f"[orkestra] Active run {st['run_id']} \"{st.get('title', '')}\" in {project} — phase {st['phase']}, "
            f"status {st['status']}{prog}.")
    return text + (f"\n{hint}" if hint else f" Next: `orkestra next -p {project}`.")


# ---------------------------------------------------------------- events

def _ensure_watcher(project) -> None:
    """Start the fast watcher when none runs (it detects stalls and recovers them without spending tokens). Imported
    here, not at start-up: only SessionStart pays for it, and never when the daemon is switched off."""
    if os.environ.get("ORKESTRA_NO_DAEMON") == "1":         # the switch fastwatch itself honours (the test suite sets it)
        return
    try:
        if flock_held_elsewhere(paths.watchdog_pid_path()):     # the common case: skip the 70 modules fastwatch pulls in
            return
        from orkestra_core import fastwatch
        fastwatch.ensure_running(project)
    except Exception as exc:                                # a convenience: never let it break the event
        log(f"fastwatch.ensure_running failed: {exc}")


def on_session_start(ev: dict) -> None:
    b = binding_for(ev)
    if b:
        _ensure_watcher(Path(b["project"]))
        if ev.get("source") != "compact":
            # startup / resume / clear: this is a new process (or a wiped context) — background subagents of the old
            # one died with it, so their registry entries would only park the run ("waiting for subagents")
            gone = activity.agents_purge(Path(b["project"]), b["run_id"], session=ev.get("session_id") or "")
            if gone:
                who = ", ".join(sorted({activity.short_agent(str(g.get("type") or "")) for g in gone}))
                activity.record(Path(b["project"]), b["run_id"], ev, "note",
                                _t("dispatch.restart_note", count=len(gone), who=who))
                try:
                    ST.ledger(Path(b["project"]), b["run_id"], f"session restart ({ev.get('source') or '?'}): dropped "
                                                                 f"{len(gone)} pending subagent record(s) ({who})")
                except Exception as exc:
                    log(f"ledger note failed: {exc}")
        context_out("SessionStart", run_summary(Path(b["project"]), b["state"]))
        return
    project = paths.find_project_root(ev.get("cwd") or os.getcwd())
    _ensure_watcher(project)
    if project:
        rid = ST.project_active_run(project)
        if rid:
            try:
                st = ST.load_state(project, rid)
            except ST.OrkestraError:
                return
            context_out("SessionStart",
                        f"[orkestra] This project has an open orkestra run {rid} (phase {st['phase']}, status "
                        f"{st['status']}). If the user wants to continue it, use /orkestra:resume. Do not start a new run "
                        f"in this project until it is finished or aborted. This session is NOT driving that run: edits "
                        f"made here are outside its plan, tests and rollback — avoid changing files it is working on, "
                        f"and tell the user if they ask for such an edit.")


def on_user_prompt(ev: dict) -> None:
    sid = ev.get("session_id") or ""
    prompt = ev.get("prompt") or ""
    synthetic = activity.is_synthetic_prompt(prompt)
    if synthetic:
        # subagent hand-backs / task notifications are model output: they never grant git permission, never approve
        # or reject a plan and never count as "the user replied". The turn they start is not the user's message
        # either, so a permission given earlier ends here — explicitly, with or without prompt ids ("commit only
        # when asked in that message").
        ST.clear_grant(sid)
    else:
        cats = intent.prompt_grants(prompt)         # git permissions and run-control words (pause / abort / resume)
        if cats:
            ST.write_grant(sid, ev.get("prompt_id"), cats, prompt)
        else:
            ST.clear_grant(sid)
    b = binding_for(ev)
    if not b:
        return
    st = b["state"]
    project = Path(b["project"])
    if synthetic:
        m = re.search(r'from="([^"]{1,80})"', prompt[:400])
        key = STATUS_LINE_RE.search(prompt)
        label = _t("dispatch.task_notification") if prompt.lstrip().startswith("<task-notification") else \
            _t("dispatch.subagent_report", who=(m.group(1) if m else '?')[:12])
        activity.record(project, st["run_id"], ev, "handback",
                        _t("dispatch.handback", label=label, status=key.group(0)) if key else label)
        if m and not prompt.lstrip().startswith("<task-notification"):
            activity.agent_handed_back(project, st["run_id"], m.group(1))
        st = release_hook_wait(project, st, "a subagent report arrived: the question is no longer on screen")
        if st.get("status") == "active":
            hint = next_hint(project, st, activity.read_rows(project, st["run_id"]))
            if hint:
                context_out("UserPromptSubmit", "[orkestra] The message above is a subagent report / task notification, "
                                                "not the user (it approves nothing and grants nothing).\n" + hint)
        return
    if (st.get("phase") != "approve"
            and st.get("status") != "awaiting_user"
            and not intent.manual_approval_requested(prompt)
            and intent.off_topic_vs_project(prompt, project, st["run_id"], title=str(st.get("title") or ""))):
        ST.unbind_session(sid)
        activity.record(project, st["run_id"], ev, "note", _t("dispatch.off_topic_unbound", project=project.name))
        context_out("UserPromptSubmit",
                    f"[orkestra] This chat was bound to {st['run_id']} in {project.name}, but your message "
                    f"is about another project. Unbound this session so Stop will not continue that run. "
                    f"Use /orkestra-resume {project.name} only if you want to continue it.")
        return
    activity.record(project, st["run_id"], ev, "user", " ".join(prompt.split())[:160])
    manual_mode_changed = False
    if intent.manual_approval_requested(prompt):
        try:
            manual_mode_changed = runs.set_approval_mode(project, st["run_id"], "manual", prompt)
            st["approval_mode"] = "manual"
        except Exception as exc:
            log(f"manual approval mode change failed: {exc}")
    if st.get("approval_mode", "autopilot") == "manual" and st.get("phase") == "approve" and not prompt.strip().startswith("/"):
        decision = intent.approval(prompt)
        if decision:
            runs.record_approval(project, st["run_id"], decision, "prompt", prompt[:300])
            if decision == "approve":
                context_out("UserPromptSubmit", f"[orkestra] User APPROVED the plan for {st['run_id']} (recorded by hook). "
                                                f"Run `orkestra phase next` and continue with `orkestra playbook implement`.")
            else:
                context_out("UserPromptSubmit", f"[orkestra] User did NOT approve the plan for {st['run_id']}; treat the "
                                                f"message as change requests: revise spec/plan (plan-critic again), then ask "
                                                f"for approval again.")
            return
    if manual_mode_changed:
        context_out("UserPromptSubmit", "[orkestra] Manual plan-approval mode is now enabled for this run. "
                                        "Continue autonomously through analysis and verification, then ask the user only at the plan approval gate.")
    if st.get("status") == "awaiting_user":
        # the user answered: the run is active again (Stop-hook continuation applies)
        runs.set_status(project, st["run_id"], "active", "user replied")


GIT_CONTEXT_BUDGET_SECONDS = 1.5       # all git calls of one hook together (they run inside the hook deadline too)
_GIT_WRITE_RE = re.compile(r"\bgit\b[^\n;|&]*?\b(checkout|switch|push)\b")


def _branch_from_head(cwd: str):
    """The checked-out branch read from .git/HEAD (no process): the name, "HEAD" when detached, None when unreadable
    (no repository, a worktree/submodule `.git` file, ...)."""
    cur = Path(cwd)
    for d in [cur, *cur.parents][:40]:
        git = d / ".git"
        if git.is_dir():
            if not ((git / "objects").is_dir() and (git / "refs").is_dir()):
                continue                    # not a repository (a planted `.git/HEAD`): real git skips it too
            try:
                head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            if head.startswith("ref:"):
                ref = head[4:].strip()
                return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            return "HEAD" if re.fullmatch(r"[0-9a-fA-F]{40,64}", head) else None
        if git.exists():
            return None
    return None


def git_context(ev: dict) -> dict:
    """Branch facts some git rules need. Computed only for a real `git checkout|switch|push` line; a push needs just
    the branch (read from .git/HEAD, no process), checkout/switch also the branch list and the tree state."""
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    if ev.get("tool_name") != "Bash":
        return {}
    kinds = {m.group(1) for m in _GIT_WRITE_RE.finditer(cmd)}
    if not kinds:
        return {}
    cwd = ev.get("cwd") or os.getcwd()
    ctx: dict = {}
    try:
        import subprocess
        started = time.monotonic()

        def g(*a):
            left = GIT_CONTEXT_BUDGET_SECONDS - (time.monotonic() - started)
            if left <= 0:
                raise subprocess.TimeoutExpired("git", 0)
            return subprocess.run(["git", "-C", cwd, *a], capture_output=True, encoding="utf-8", errors="replace",
                                  timeout=left, env=snapshot.git_env())
        branch = _branch_from_head(cwd)
        if branch is None:
            cp = g("rev-parse", "--abbrev-ref", "HEAD")
            branch = cp.stdout.strip() if cp.returncode == 0 else None
        ctx["current_branch"] = branch
        if kinds & {"checkout", "switch"}:
            cp = g("for-each-ref", "--format=%(refname:short)", "refs/heads")
            ctx["branches"] = cp.stdout.split() if cp.returncode == 0 else []
            cp = g("status", "--porcelain", "--untracked-files=no")
            ctx["clean"] = cp.returncode == 0 and not cp.stdout.strip()
    except Exception:
        return {}                           # out of time or git unusable: the guards take their strict path
    return ctx


def note_control_actor(ev: dict) -> None:
    """The guard let `orkestra pause|abort|resume ...` through: leave a note of WHICH chat asked, for the status row the
    CLI writes (state.take_actor) -- the CLI cannot tell, only this hook can."""
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    if ev.get("tool_name") != "Bash" or "orkestra" not in cmd:
        return
    segs = guards.expand_cli_commands(sp.segments(cmd))
    for _key, project, run_id in guards.control_targets(segs, ev.get("cwd") or os.getcwd()):
        ST.note_actor(project, run_id, ev.get("session_id") or "", str(ev.get("agent_type") or ""),
                      str(ev.get("agent_id") or ""))


def deny_key(guard: str, ev: dict) -> str:
    """8-char id of a refused call: sha1(guard | tool | normalised target)."""
    import hashlib
    ti = ev.get("tool_input")
    if not isinstance(ti, dict):
        target = str(ti or "")
    elif ev.get("tool_name") == "Bash":
        target = str(ti.get("command") or "")
    else:
        target = next((str(ti[k]) for k in ("file_path", "path", "notebook_path", "url", "pattern", "subagent_type", "to")
                       if ti.get(k)), "") or json.dumps(ti, sort_keys=True, ensure_ascii=False, default=str)
    norm = " ".join(target.split())[:200]
    return hashlib.sha1(f"{guard}|{ev.get('tool_name') or ''}|{norm}".encode("utf-8", "replace")).hexdigest()[:8]


def _deny_counts_path(ev: dict, b):
    if b:
        return paths.run_dir(Path(b["project"]), b["run_id"]) / "deny-counts.json"
    sid = ev.get("session_id") or ""
    if sid == "doctor":
        return None             # `orkestra doctor` self-test: the refusal is still issued, but no counter file is left behind
    return paths.orkestra_home() / "denies" / f"{ST._safe(sid)}.json" if sid else None


def bump_denial(path: Path, ckey: str, now: float):
    """Count one more refusal under `ckey`: (hits inside DENY_WINDOW, hits inside DENY_BLOCK_WINDOW). Bounded file."""
    fresh = not path.exists()
    with file_lock(path):
        data = read_json(path, default={}) or {}
        keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
        hits = [float(x) for x in (keys.get(ckey) or []) if isinstance(x, (int, float)) and now - float(x) <= DENY_WINDOW]
        hits = (hits + [now])[-(DENY_BLOCK_HITS + 4):]
        keys.pop(ckey, None)
        keys[ckey] = hits                                   # newest last: the trim below drops the oldest keys
        if len(keys) > DENY_MAX_KEYS:
            keys = dict(list(keys.items())[-DENY_MAX_KEYS:])
        write_json_atomic(path, {"v": 1, "keys": keys})
    if fresh and "denies" in str(path.parent):
        ST._prune(path.parent, days=3)
    return len(hits), sum(1 for x in hits if now - x <= DENY_BLOCK_WINDOW)


def _cli_rewrite(command: str) -> str:
    """`python3 <plugin>/bin/orkestra a b` -> `orkestra a b` (the documented form): "" when the line is not that."""
    import shlex
    segs = sp.segments(command)
    for seg in segs:
        if guards.INTERPRETER_RE.match(seg.program or "") and seg.args and guards.is_orkestra_word(seg.args[0]):
            return "orkestra " + " ".join(shlex.quote(a) for a in seg.args[1:])
    return ""


def deny_alternative(d, ev: dict, b) -> str:
    """What to do instead, when the guard knows (deterministic, no model needed)."""
    auto = bool(b) and b["state"].get("approval_mode", "autopilot") != "manual"
    stuck = 'run `orkestra block "<why>"`' if auto else 'ask the user, or run `orkestra block "<why>"`'
    if d.guard == "approval":
        return "record a conservative assumption in the plan notes and continue without asking"
    if d.guard == "phase":
        return "run `orkestra next` and do what it says"
    if d.guard == "internals":
        cmd = (ev.get("tool_input") or {}).get("command") if isinstance(ev.get("tool_input"), dict) else ""
        again = _cli_rewrite(cmd or "")
        return f"run exactly: {again}" if again else f"touch run state only through the `orkestra` CLI; if that is not possible, {stuck}"
    if d.guard == "isolation" and b:
        return (f"this chat is bound to {b['project']}: work inside it; if the user changed topic run "
                f"`orkestra pause -p {b['project']}`, otherwise open a separate chat for the other project")
    if d.guard == "roles" and ev.get("agent_type"):
        return "this action belongs to the orchestrator: end with STATUS: BLOCKED and the reason"
    return stuck


def escalate_denial(ev: dict, b, d):
    """(model-facing text, key) of a refusal. The decision never changes; only the text does, so a model cannot loop on
    it forever: 2nd identical refusal adds a note, the 3rd replaces the long text by a short "do not retry", the 8th
    within 10 minutes blocks the run."""
    text = f"[orkestra:{d.guard}] {d.reason}"
    key = deny_key(d.guard, ev)
    head, sep, tail = d.reason.rpartition("\nunblock:")
    way_out = "\nunblock:" + tail if sep and "\n" not in tail else ""      # the run guards' last line: every text keeps it
    if d.guard == "busy":
        # a busy machine is not the model's fault: never counted toward blocking the run, but the same call refused
        # over and over ends in a short "stop retrying" instead of the same "retry once" forever
        try:
            path = _deny_counts_path(ev, b)
            if path is not None:
                total, _fast = bump_denial(path, f"{key}:{ev.get('agent_id') or ev.get('agent_type') or 'main'}", time.time())
                if total >= DENY_SHORT_AT:
                    return (f"[orkestra:busy] denied {total}x: the machine stays too busy to check this call. Do not retry "
                            f"it; tell the user and wait for their reply."), key
        except Exception as exc:
            log(f"deny escalation failed: {exc}")
        return text, key
    try:
        path = _deny_counts_path(ev, b)
        if path is None:
            return text, key
        actor = str(ev.get("agent_id") or ev.get("agent_type") or "main")
        now = time.time()
        total, fast = bump_denial(path, f"{key}:{actor}", now)
        # a retry with a small variation (another commit message, an extra flag) has a new key every time: the same
        # guard refusing the same actor again and again is counted under one family key as well
        fam_total, fam_fast = bump_denial(path, f"~{d.guard}:{actor}", now)
        if fam_total >= DENY_FAMILY_AT and total < DENY_SHORT_AT:
            if not (b and fam_fast >= DENY_BLOCK_HITS):
                note = (f"\n(denied {fam_total}x by [{d.guard}] for varied calls: do not retry variations of it. Change the "
                        f"approach, or end your turn and tell the user.)")
                return (f"[orkestra:{d.guard}] {head}{note}{way_out}" if way_out else text + note), key
            total, fast = fam_total, fam_fast            # the run blocks below, as for an identical loop
        if total < DENY_NOTE_AT:
            return text, key
        if total < DENY_SHORT_AT:
            note = f"\n(denied {total}x: the same call is refused again. Change the approach; do not repeat it.)"
            if way_out:
                return f"[orkestra:{d.guard}] {head}{note}{way_out}", key
            return text + note, key
        alt = deny_alternative(d, ev, b)
        if b and fast >= DENY_BLOCK_HITS:
            project, rid = Path(b["project"]), b["run_id"]
            try:
                changed = runs.set_status(project, rid, "blocked", _t("dispatch.deny_loop_reason", guard=d.guard, count=fast),
                                          who={"session_id": ev.get("session_id") or "", "agent_type": ev.get("agent_type") or "",
                                               "agent_id": ev.get("agent_id") or ""})
                if changed:
                    activity.record(project, rid, ev, "loop", _t("dispatch.deny_loop_row", guard=d.guard, count=fast), key=key)
                    log(f"deny loop: run {rid} blocked after {fast} identical [{d.guard}] refusals ({key})")
            except Exception as exc:
                log(f"deny loop block failed: {exc}")
            return (f"[orkestra:{d.guard}] denied {total}x: this run is now blocked. Do not retry. End your turn and "
                    f"tell the user which call was refused and why.{way_out}"), key
        return f"[orkestra:{d.guard}] denied {total}x: do not retry; {alt}{way_out}", key
    except Exception as exc:                # counting is a convenience: the plain refusal still stands
        log(f"deny escalation failed: {exc}")
        return text, key


def on_pre_tool(ev: dict) -> None:
    cfg = paths.global_config()
    b = binding_for(ev)
    grants = ST.current_grant(ev.get("session_id") or "", ev.get("prompt_id"))
    ctx = git_context(ev)
    if ctx:
        ev = dict(ev, _git_ctx=ctx)
    if b and ev.get("tool_name") == "AskUserQuestion" and b["state"].get("approval_mode", "autopilot") != "manual":
        d = guards.deny("approval", "This run is in autopilot mode. Record a conservative assumption and continue; do not wait for the user.")
    else:
        d = guards.pre_tool_decision(ev, cfg, b, grants)       # the hook-wide deadline (main) covers it
    if d.denied:
        text, key = escalate_denial(ev, b, d)
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": text}})
        log(f"DENY [{d.guard}] {key} session={short_sid(ev.get('session_id', ''))} agent={ev.get('agent_type', '-')} "
            f"tool={ev.get('tool_name')} input={json.dumps(ev.get('tool_input'), ensure_ascii=False)[:300]}")
        if b:
            activity.record(Path(b["project"]), b["run_id"], ev, "deny", f"[{d.guard}] {d.reason}"[:200], key=key)
        return
    if b:
        activity.record(Path(b["project"]), b["run_id"], ev, "tool")
    try:
        note_control_actor(ev)
    except Exception as exc:                # the note only names the chat in a status row: never block the call for it
        log(f"note_control_actor failed: {exc}")
    # backups before the first modification of a file during a run (enables exact rollback)
    if not b:
        return
    st = b["state"]
    if not ev.get("agent_type"):
        if ev.get("tool_name") == "AskUserQuestion":
            if st.get("status") == "active":
                heads = activity.ask_heads(ev)[:160] or _t("dispatch.question")
                try:    # the run is now legitimately waiting for the user (watch / watchdog / bridge read this)
                    runs.set_status(Path(b["project"]), b["run_id"], "awaiting_user", heads, source="hook")
                except Exception as exc:
                    log(f"awaiting_user not set: {exc}")
        elif st.get("status") == "awaiting_user":
            # the main session is calling another tool: the question call has returned (answered or dismissed)
            st = release_hook_wait(Path(b["project"]), st, "the question was closed without an answer", min_age=10.0)
    if st.get("phase") not in guards.CODE_PHASES:
        return
    project = Path(b["project"])
    rdir = paths.run_dir(project, b["run_id"])
    ev = guards.normalize_event(ev)          # MCP editors (e.g. Serena) → Write/Bash semantics
    cwd = ev.get("cwd") or str(project)
    targets = []
    tool = ev.get("tool_name")
    if tool in guards.WRITE_TOOLS:
        targets = [str(resolve_path(p, cwd)) for p in guards._tool_paths(ev)]
    elif tool == "Bash":
        segs = sp.segments((ev.get("tool_input") or {}).get("command") or "")
        for seg, scwd in guards.with_cwd(segs, cwd):
            targets += [t for t in guards.bash_write_targets(seg, scwd) if not t.endswith("/*")]
    for t in list(dict.fromkeys(targets))[:400]:
        try:
            snapshot.backup_before_edit(project, rdir, Path(t))
        except Exception as exc:  # backups must never block work
            log(f"backup failed for {t}: {exc}")


def _bind_targets(command: str, cwd: str):
    """[(project, run id or None)] the `orkestra start|resume` calls of a command line act on, read the way the CLI
    resolves -p / --run. None when that cannot be told (an unresolvable project, ORKESTRA_PROJECT in the command):
    the caller then does not filter."""
    if "ORKESTRA_PROJECT" in command:
        return None
    out = []
    for seg, scwd in guards.with_cwd(sp.segments(command), cwd):
        argv = guards._orkestra_argv(seg)
        if argv is None:
            continue
        key = CP.inspect(argv)[0] or _loose_key(argv)
        if key not in BIND_COMMANDS:
            continue
        proj_arg, run_arg = CP.global_values(argv)
        project = guards._cli_project(proj_arg, scwd)
        if project is None:
            return None
        out.append((project, run_arg or None))
    return out or None


def apply_markers(ev: dict, sid: str, text: str) -> list:
    """Obey the bind / unbind markers the orkestra CLI printed in a Bash output; returns the warnings for the model.

    A marker line is just text, and any agent of a session can print one (a `cat` of a log, a sandbox run of the CLI),
    so it counts only when ALL of this holds:
      * the main chat ran it: a subagent shares the parent's session id and must never rebind (or unbind) it;
      * the command was the orkestra CLI itself: `start` / `resume` for a bind, `pause` / `abort` / `phase next` for an
        unbind (a host that sends no command at all is tolerated: the host builds the event, an agent cannot);
      * it names this hook's own ORKESTRA_HOME (the CLI of a sandbox home prints a line for THAT home; none = older CLI);
      * the run exists, is open and the project path is absolute (ST.bind_problem);
      * the chat is not already driving another live run (ST.try_bind): that binding is kept, with a notice."""
    takeover_noted = set()
    binds, unbinds = list(BIND_RE.finditer(text)), list(UNBIND_RE.finditer(text))
    if not binds and not unbinds:
        return []
    if is_subagent_event(ev):
        log(f"ignored {len(binds) + len(unbinds)} bind/unbind marker(s) printed by a subagent of session {short_sid(sid)}")
        return []
    command = (ev.get("tool_input") or {}).get("command")
    keys = cli_keys(command) if isinstance(command, str) and command.strip() else None      # None: no command sent

    def from_cli(wanted: tuple) -> bool:
        return keys is None or bool(keys & set(wanted))

    def own_home(m) -> bool:
        if m.group(3) and not _same_home(m.group(3)):
            log(f"ignored a marker for another ORKESTRA_HOME ({m.group(3)}) in session {short_sid(sid)}")
            return False
        return True

    if unbinds and from_cli(UNBIND_COMMANDS):
        for m in unbinds:
            rec = ST.binding_record(sid)        # the file as written: a closed run (abort, done) hides behind `cur`
            if own_home(m) and rec and rec.get("run_id") == m.group(2) and Path(rec.get("project", "")) == Path(m.group(1)):
                # the CLI prints the line only after it closed or paused the run: a line for a run that is still being
                # driven is forged text (a `cat` of a file) and must not release the chat
                live = (ST.read_json(ST.state_path(Path(m.group(1)), m.group(2))) or {}).get("status") in ST.DRIVEN_STATUSES
                if live:
                    log(f"ignored an unbind marker for the still open run {m.group(2)} in session {short_sid(sid)}")
                    continue
                ST.unbind_session(sid)
    warnings: list = []
    if not binds or not from_cli(BIND_COMMANDS):
        if binds:
            log(f"ignored bind marker(s) in the output of a command that is not `orkestra start|resume` ({str(command)[:80]})")
        return warnings
    lease = int((paths.global_config().get("watchdog") or {}).get("lease_seconds") or 120)
    targets = None
    if keys is not None and isinstance(command, str):
        try:
            targets = _bind_targets(command, str(ev.get("cwd") or os.getcwd()))
        except Exception as exc:
            log(f"bind target lookup failed: {exc}")
    ours = [m for m in binds if own_home(m)]
    if targets is None and keys is not None and len({(m.group(1), m.group(2)) for m in ours}) > 1:
        # the command's own target cannot be read (-p "$X", ORKESTRA_PROJECT): several different markers mean at least
        # one is not the CLI's. Only the project of the working directory is believed, none when it matches nothing.
        cwd_project = None
        try:
            cwd_project = guards._cli_project(None, str(ev.get("cwd") or os.getcwd()))
        except Exception:
            pass
        ours = [m for m in ours if cwd_project is not None and resolve_path(m.group(1)) == resolve_path(str(cwd_project))]
        if not ours:
            log(f"ignored several bind markers: the command's target is unknown and none names {cwd_project}")
    for m in ours:
        bproj, brun = Path(m.group(1)), m.group(2)
        if targets is not None and not any(
                resolve_path(str(bproj)) == resolve_path(str(tp)) and (tr is None or tr == brun) for tp, tr in targets):
            log(f"ignored a bind marker for {bproj} ({brun}): the command did not start or resume it")
            continue
        why = ST.bind_problem(bproj, brun)
        if why:
            log(f"ignored a bind marker: {why}")
            continue
        if (ST.read_json(ST.state_path(bproj, brun)) or {}).get("status") not in ST.DRIVEN_STATUSES:
            continue  # never lease a chat to a paused/aborted/done run
        others = [s for s in ST.run_sessions(bproj, brun) if s["id"] != ST._safe(sid)]
        engine = str(ev.get("_engine") or "")
        res = ST.try_bind(sid, bproj, brun, engine=engine, lease=lease)
        if res["result"] == "kept":
            prev = res["prev"]
            if not res["duplicate"]:                # once per refusal, not once per marker
                try:
                    ST.ledger(bproj, brun, _t("dispatch.bind_kept", sid=short_sid(sid), kept_run=prev["run_id"],
                                              kept_project=prev["project"], age=res["age"]))
                except Exception as exc:
                    log(f"bind-kept note failed: {exc}")
                warnings.append(
                    f"[orkestra] WARNING: run {brun} ({bproj}) was started or resumed here, but this chat is NOT bound to "
                    f"it: it still drives run {prev['run_id']} ({prev['project']}), active {res['age']} s ago. {brun} has no "
                    f"driver now, so its guards and the Stop hook do not apply from this chat. To drive {brun} from here: "
                    f"`orkestra pause -p {prev['project']}`, then `orkestra resume -p {bproj}`. Otherwise tell the user.")
            continue
        if res["result"] != "bound":
            log(f"bind refused: {res.get('why')}")
            continue
        if engine:
            runs.record_engine_origin(bproj, brun, engine)
        activity.record(bproj, brun, ev, "bind", _t("dispatch.bound", sid=short_sid(sid)))
        if others and (brun, others[0]["id"]) not in takeover_noted:      # once per run and other session, not per marker
            o = others[0]
            takeover_noted.add((brun, o["id"]))
            try:
                if o["age"] < lease:
                    ST.ledger(bproj, brun, f"WARNING second session {short_sid(sid)} bound while {short_sid(o['id'])} is active "
                                           f"({o['age']}s ago)")
                    warnings.append(
                        f"[orkestra] WARNING: another session ({short_sid(o['id'])}) drove run {brun} {o['age']} s ago and "
                        f"may still be working. Two sessions must never drive one run (double execution). Do "
                        f"NOT continue here unless the user confirms the other window is closed.")
                else:
                    ST.ledger(bproj, brun, f"session {short_sid(sid)} took over (previous {short_sid(o['id'])} silent "
                                           f"{o['age'] // 60} min)")
            except Exception as exc:
                log(f"takeover note failed: {exc}")
    return warnings


def on_post_tool(ev: dict) -> None:
    if ev.get("tool_name") == "SendMessage":
        # registered here, not in PreToolUse: PostToolUse only fires when the call went through
        b = binding_for(ev)
        if b and not ev.get("agent_type"):
            ti = ev.get("tool_input") or {}
            resp = ev.get("tool_response")
            text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False, default=str)
            failed = (isinstance(resp, dict) and (resp.get("success") is False or resp.get("error") or resp.get("is_error"))) \
                or bool(re.match(r"\s*\W{0,3}(error|failed|no (such )?(agent|recipient)|unknown|not found)", text[:200], re.I))
            if not failed:
                activity.agent_messaged(Path(b["project"]), b["run_id"], str(ti.get("to") or ti.get("recipient") or ""),
                                        str(ti.get("summary") or ti.get("message") or ""), session=ev.get("session_id") or "")
        return
    if ev.get("tool_name") in ("Agent", "Task"):
        b = binding_for(ev)
        if b:
            info = activity.launch_info(ev)
            if info:        # background launch: the report arrives later as a message; SubagentStop closes the entry
                activity.agent_launched(Path(b["project"]), b["run_id"], info, session=ev.get("session_id") or "")
                activity.record(Path(b["project"]), b["run_id"], ev, "result", f"← {info['type']}: async_launched")
            else:
                activity.record_launch(Path(b["project"]), b["run_id"], activity.launch_from_event(ev))   # foreground: ring only
                activity.record(Path(b["project"]), b["run_id"], ev, "result", agent_result_line(ev))
        return
    if (ev.get("tool_name") or "").startswith("mcp__"):
        ev = guards.normalize_event(ev)
        if ev.get("tool_name") not in guards.WRITE_TOOLS:
            return
    tool = ev.get("tool_name")
    sid = ev.get("session_id") or ""
    if tool == "Bash":
        resp = ev.get("tool_response")
        text = ""
        if isinstance(resp, dict):
            text = str(resp.get("stdout") or "") + "\n" + str(resp.get("stderr") or "")
        elif isinstance(resp, str):
            text = resp
        cur = ST.session_binding(sid)
        if cur:   # marks the end of a Bash call (liveness: "a command is still running" vs "idle")
            activity.record(Path(cur["project"]), cur["run_id"], ev, "end", "✓")
        warnings = apply_markers(ev, sid, text) if "[orkestra:" in text else []
        if warnings:                                # ONE JSON object per hook call (two would be unparseable)
            context_out("PostToolUse", "\n".join(dict.fromkeys(warnings)))
        return
    b = binding_for(ev)
    if not b:
        return
    project = Path(b["project"])
    if tool in guards.WRITE_TOOLS:
        cwd = ev.get("cwd") or str(project)
        for p in guards._tool_paths(ev):
            snapshot.record_touch(project, paths.run_dir(project, b["run_id"]), resolve_path(p, cwd))
        return
    if tool == "AskUserQuestion":
        activity.record(project, b["run_id"], ev, "answer",
                        " | ".join(extract_answers(ev))[:160] or _t("dispatch.answered"))
        recorded = False
        if b["state"].get("phase") == "approve" and is_approval_question(ev):
            decision, appr, others = approval_from_answers(ev)
            if decision:
                extra = "; ".join(f"{q or 'extra question'} = {a}" for q, a in others)[:400]
                runs.record_approval(project, b["run_id"], decision, "AskUserQuestion",
                                     " | ".join(appr) + (f" · other answers: {extra}" if extra else ""))
                recorded = True
                if decision == "approve":
                    context_out("PostToolUse", "[orkestra] Plan approval recorded by hook."
                                + (f" The same call also returned: {extra} — NOT part of the approval. If that changes "
                                   f"scope, update spec/plan first (a changed plan needs approval again); otherwise run "
                                   f"`orkestra phase next`." if extra else " Run `orkestra phase next`."))
                else:
                    context_out("PostToolUse", "[orkestra] The user did not approve as-is: apply their feedback to "
                                               "spec/plan, re-check, and ask for approval again.")
        if not recorded and b["state"].get("status") == "awaiting_user":
            runs.set_status(project, b["run_id"], "active", "user answered")


def agent_result_line(ev: dict) -> str:
    """One line from a finished subagent's reply: its STATUS/VERDICT line if any, else the first line."""
    texts: list = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "text" and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)
        elif isinstance(obj, str) and not texts:
            texts.append(obj)

    walk(ev.get("tool_response"))
    lines = [ln.strip() for ln in "\n".join(texts).splitlines() if ln.strip()]
    sub = (ev.get("tool_input") or {}).get("subagent_type") or "agent"
    key = [ln for ln in lines if re.search(r"\b(STATUS|VERDICT):", ln)]
    line = (key[-1] if key else (lines[0] if lines else _t("dispatch.empty_reply")))
    return f"← {sub}: {line}"[:200]


def _is_approval_text(text: str) -> bool:
    t = (text or "").lower()
    return "onay" in t or "approv" in t


def answer_pairs(ev: dict) -> list:
    """[(question text or "", answer)] from the tool OUTPUT only; tolerant to format differences."""
    pairs: list = []

    def flat(v) -> str:
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v)
        return str(v)

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "answers" and isinstance(v, dict):
                    pairs.extend((str(q), flat(a)) for q, a in v.items())
                elif k == "answers" and isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            q = item.get("question") or item.get("header") or ""
                            a = item.get("answer") if "answer" in item else next(
                                (x for kk, x in item.items() if kk not in ("question", "header") and isinstance(x, (str, list))), "")
                            pairs.append((str(q), flat(a)))
                        else:
                            pairs.append(("", flat(item)))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    walk(ev.get("tool_response"))
    if not pairs:
        raw = ev.get("tool_response")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        text = text.replace('\\"', '"')
        pairs = [(q, a) for q, a in re.findall(r'"([^"\n]{1,300})"\s*=\s*"([^"\n]*)"', text)]
        if not pairs:
            pairs = [("", a) for a in extract_answers(ev)]
    return pairs


def approval_from_answers(ev: dict):
    """(decision | None, approval answers, other (question, answer) pairs).

    Only the answer to the approval question decides. Extra questions asked in the same call (e.g. an "Ek kapsam"
    multi-select) used to turn a clear "Onayla" into a reject — they are now passed along as context instead."""
    pairs = answer_pairs(ev)
    if not pairs:
        return None, [], []
    qs = [q for q in ((ev.get("tool_input") or {}).get("questions") or []) if isinstance(q, dict)]
    approval_keys = set()
    for q in qs:
        if _is_approval_text(f"{q.get('header', '')} {q.get('question', '')}"):
            approval_keys.update(str(q.get(k) or "").strip().lower() for k in ("question", "header"))
    approval_keys.discard("")
    appr = [(q, a) for q, a in pairs if q and (q.strip().lower() in approval_keys or _is_approval_text(q))]
    if not appr:
        if len(qs) > 1 and len(pairs) == len(qs) and not any(q for q, _ in pairs):
            # unlabeled answers in question order: pick the ones at the approval questions' positions
            idx = [i for i, q in enumerate(qs) if _is_approval_text(f"{q.get('header', '')} {q.get('question', '')}")]
            appr = [pairs[i] for i in idx]
        else:
            appr = list(pairs)                      # cannot map → every answer must approve (old, strict behaviour)
    others = [x for x in pairs if x not in appr]
    verdicts = [intent.approval_answer(a) for _, a in appr]
    decision = "approve" if verdicts and all(v == "approve" for v in verdicts) else "reject"
    return decision, [a for _, a in appr], others


def is_approval_question(ev: dict) -> bool:
    qs = (ev.get("tool_input") or {}).get("questions") or []
    for q in qs:
        if isinstance(q, dict):
            text = f"{q.get('header', '')} {q.get('question', '')}".lower()
            if "onay" in text or "approv" in text:
                return True
    return False


def extract_answers(ev: dict) -> list:
    """AskUserQuestion answers; tolerant to format differences across Claude Code versions."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "answers" and isinstance(v, dict):
                    found.extend(str(x) for x in v.values())
                elif k == "answers" and isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            found.extend(str(x) for x in item.values() if isinstance(x, str))
                        else:
                            found.append(str(item))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    # trust only the tool's OUTPUT (what the UI collected), never the model-authored input
    walk(ev.get("tool_response"))
    if not found:
        raw = ev.get("tool_response")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        found = re.findall(r'"=\s*"?([^"\n]+)', text) or re.findall(r'=\\?"([^"\\]+)', text)
    return found


class _StopCap(Exception):
    """The Stop nudge cap is already spent.

    Raised inside StateTxn before any mutation so a clean return cannot save_state. on_stop records the idle
    row only after the transaction has closed, leaving state.json mtime at or behind that row.
    """


def on_stop(ev: dict) -> None:
    b = binding_for(ev)
    if not b:
        return
    st = b["state"]
    project = Path(b["project"])

    def idle(why: str) -> None:
        activity.record(project, st["run_id"], ev, "idle", why)

    sid = ev.get("session_id") or ""
    if st.get("phase") == "done":
        ST.unbind_session(sid)
        idle(_t("dispatch.turn_done"))
        return
    if st.get("status") == "awaiting_user":
        # Keep the bind: AskUserQuestion answers arrive on the next PostToolUse after Stop.
        idle(_t("dispatch.turn_awaiting"))
        return
    if st.get("status") != "active":
        # Paused/blocked must not keep a chat leased: a later resume would Stop-hijack it.
        ST.unbind_session(sid)
        idle(_t("dispatch.turn_status", status=st.get("status")))
        return
    if ev.get("stop_hook_active"):
        idle(_t("dispatch.turn_plain"))
        return
    last_what = activity.last_user_what(project, st["run_id"], sid)
    if last_what and intent.off_topic_vs_project(
            last_what, project, st["run_id"], title=str(st.get("title") or "")):
        ST.unbind_session(sid)
        idle(_t("dispatch.turn_off_topic"))
        return
    if not activity.turn_drove_run(project, st["run_id"], sid):
        ST.unbind_session(sid)
        idle(_t("dispatch.turn_not_driving"))
        return
    cfg = paths.global_config()
    headless = paths.is_headless(st)        # also true for an interactive run the bridge / watchdog continues
    if (cfg.get("tempo") or {}).get("stop_while_agents", True) and not headless \
            and str(st.get("engine") or "claude") != "pi":
        wd = tempo.wd_cfg(cfg)
        pending, _lost = tempo.live_agents(project, st["run_id"], wd=wd)    # lost = registered, no sign of life
        if pending:
            # Subagents run in the background and hand their report back as a new message, which starts the next
            # turn. Blocking here only forced the model into sleep/poll loops (measured: 8–29 % of wall time).
            who = ", ".join(sorted({activity.short_agent(str(a.get("type") or "")) for a in pending}))
            activity.record(project, st["run_id"], ev, "wait_agents",
                            _t("dispatch.wait_agents", who=who, count=len(pending)))
            return
        # the last background subagent has stopped but its report has not been delivered yet: nudging now would
        # re-dispatch the work it just finished (the watchdog reports a report that never arrives as a dead wait)
        due = activity.agents_reports_due(project, st["run_id"], max_age=float(wd["dead_wait_seconds"]))
        if due:
            activity.record(project, st["run_id"], ev, "wait_agents", _t("dispatch.wait_report", count=len(due)))
            return
    try:
        with ST.StateTxn(project, b["run_id"]) as s:
            raw = s.get("stop_blocks")
            sb = raw if isinstance(raw, dict) else {}
            key = progress_key(ev, s)
            now = time.time()
            recent = [float(x) for x in (sb.get("_nudges") or []) if isinstance(x, (int, float)) and now - float(x) <= STOP_NUDGE_WINDOW]
            count = int(sb.get(key, 0))
            if count >= MAX_STOP_BLOCKS_PER_PROMPT or len(recent) >= STOP_NUDGE_CEILING:
                raise _StopCap()
            live = s.setdefault("stop_blocks", {})
            live[key] = count + 1
            live["_nudges"] = recent + [now]
            if len([k for k in live if k != "_nudges"]) > 50:     # keep the dict small (newest progress states only)
                kept = [(k, v) for k, v in live.items() if k != "_nudges"][-20:]
                s["stop_blocks"] = dict(kept, _nudges=live["_nudges"])
    except _StopCap:
        idle(_t("dispatch.turn_nudge_limit"))
        return
    hint = next_hint(project, st, activity.read_rows(project, st["run_id"]))
    no_user = headless or st.get("approval_mode", "autopilot") != "manual"      # nobody to ask: AskUserQuestion is denied
    if no_user:
        wait_advice = (f"No user is present: do not wait or ask. Decide conservatively and record the assumption; to "
                       f"postpone one task run `orkestra task defer <TID> \"<why>\" -p {project}`; if truly blocked run "
                       f"`orkestra block \"<reason>\" -p {project}` and explain.")
    else:
        wait_advice = (f"If you must wait for the user, ask with AskUserQuestion, or run `orkestra await \"<question>\" "
                       f"-p {project}` (headless). If truly blocked, run `orkestra block \"<reason>\" -p {project}` and "
                       f"explain.")
    emit({"decision": "block", "reason":
          f"[orkestra] Run {st['run_id']} is still in phase '{st['phase']}' (status active)"
          + ("" if headless or str(st.get("engine") or "") == "pi" else " and no subagent is running")
          + ": do not end the turn mid-run.\n"
          + (hint + "\n" if hint else f"Continue with `orkestra next -p {project}`.\n")
          + wait_advice + f" If the user changed topic and this chat drives the run, run `orkestra pause -p {project}` first."})
    activity.record(project, st["run_id"], ev, "nudge", _t("dispatch.nudge_early_stop"))


def progress_key(ev: dict, st: dict) -> str:
    """What the Stop-nudge allowance is keyed by: the run's progress (phase, finished tasks, red/green evidence), not a
    prompt id (Claude Code sends none). An early stop after real progress is nudged again; a model that stops again
    and again without progress gets exactly MAX_STOP_BLOCKS_PER_PROMPT nudges."""
    tasks = [t for t in (st.get("tasks") or {}).values() if isinstance(t, dict)]
    done = sum(1 for t in tasks if t.get("status") in ("done", "deferred"))
    evidence = sum(1 for t in tasks for k in ("red", "green") if t.get(k))
    return f"{ev.get('prompt_id') or 'turn'}@{st.get('phase')}:{done}:{evidence}"


def on_subagent_stop(ev: dict) -> None:
    b = binding_for(ev)
    if not b:
        return
    project = Path(b["project"])

    def finished() -> None:
        activity.agent_finished(project, b["run_id"], str(ev.get("agent_id") or ""), str(ev.get("agent_type") or ""))
        activity.record(project, b["run_id"], ev, "agent_done",
                        _t("dispatch.agent_done", agent=ev.get("agent_type") or "agent"))

    if ev.get("agent_type") != "orkestra:implementer" or ev.get("stop_hook_active"):
        finished()
        return
    st = b["state"]
    in_prog = [(tid, t) for tid, t in (st.get("tasks") or {}).items() if t.get("status") == "in_progress"]
    mine = activity.agent_tasks(project, b["run_id"], str(ev.get("agent_id") or ""))
    if mine:        # a registered implementer is judged on ITS tasks only (parallel implementers must not nudge each other)
        in_prog = [(tid, t) for tid, t in in_prog if tid in mine]
    missing = []
    for tid, t in in_prog:
        notes = t.get("notes") or []
        blocked_note = bool(notes) and str(notes[-1]).upper().startswith("BLOCKED")
        if t.get("tdd") == "required" and not (t.get("green") or {}).get("ok") and not blocked_note:
            missing.append(tid)
    if not missing:
        finished()
        return
    agent_id = ev.get("agent_id") or "?"
    with ST.StateTxn(project, b["run_id"]) as s:
        seen = s.setdefault("subagent_stop_blocks", {})
        already = bool(seen.get(agent_id))
        seen[agent_id] = 1
    if already:
        finished()
        return
    activity.record(project, b["run_id"], ev, "nudge", _t("dispatch.nudge_tdd", tasks=", ".join(missing)))
    emit({"decision": "block", "reason":
          f"[orkestra] Task(s) {', '.join(missing)} have no passing-test evidence. Finish TDD: "
          f"`orkestra tdd green <TID> -- <test cmd>`. If you cannot, run `orkestra task note <TID> \"BLOCKED: <why>\"` "
          f"and end with STATUS: BLOCKED and the reason."})


def _lifecycle_binding(ev: dict):
    """The binding of a lifecycle event, without healing or touching anything (these hooks must stay cheap)."""
    sid = ev.get("session_id") or ""
    return ST.session_binding(sid) if sid else None


def on_post_tool_failure(ev: dict) -> None:
    """A tool call failed or was interrupted: PostToolUse never fires for it, so a Bash row would stay "running" for
    ever (a false hung command). Close it; an interrupt is flagged in the row."""
    b = _lifecycle_binding(ev)
    if not b:
        return
    project, rid = Path(b["project"]), b["run_id"]
    tool = ev.get("tool_name") or ""
    if tool == "Bash":
        intr = bool(ev.get("is_interrupt"))
        activity.record(project, rid, ev, "end", _t("dispatch.cmd_interrupted" if intr else "dispatch.cmd_failed"))
    elif tool in ("Agent", "Task"):
        sub = (ev.get("tool_input") or {}).get("subagent_type") or "agent"
        activity.record(project, rid, ev, "result", f"← {sub}: failed")


def on_notification(ev: dict) -> None:
    """Permission / input prompts: the session waits for the human (a legitimate wait, not a stall)."""
    b = _lifecycle_binding(ev)
    if not b:
        return
    kind = str(ev.get("notification_type") or ev.get("type") or "")
    project, rid = Path(b["project"]), b["run_id"]
    if kind in ("permission_prompt", "elicitation_dialog", "agent_needs_input"):
        what = " ".join(str(ev.get("message") or ev.get("title") or kind).split())[:140]
        activity.record(project, rid, ev, "perm_wait", _t("dispatch.perm_wait", what=what))
    elif kind == "idle_prompt":
        activity.record(project, rid, ev, "idle", _t("dispatch.idle_prompt"))


def on_stop_failure(ev: dict) -> None:
    """The turn died on a model/API error (rate limit, overload, ...): it is not "silent", it is over."""
    b = _lifecycle_binding(ev)
    if not b:
        return
    what = " ".join(str(ev.get("error") or ev.get("error_type") or "unknown").split())[:60]
    extra = " ".join(str(ev.get("error_details") or "").split())[:80]
    activity.record(Path(b["project"]), b["run_id"], ev, "api_error",
                    _t("dispatch.api_error", what=what + (f" ({extra})" if extra else "")))


def on_session_end(ev: dict) -> None:
    """The host process of this session is gone. The binding stays (a `--resume` of the same session id continues the
    run), but its lease is expired at once so a watchdog / second chat does not wait for it, and a binding that points
    at a closed run is dropped."""
    sid = ev.get("session_id") or ""
    if not sid:
        return
    b = ST.session_binding(sid)
    if not b:
        try:
            ST.heal_binding(sid)
        except Exception as exc:
            log(f"heal_binding failed: {exc}")
        return
    reason = " ".join(str(ev.get("reason") or "other").split())[:40]
    activity.record(Path(b["project"]), b["run_id"], ev, "idle", _t("dispatch.session_ended", reason=reason))
    lease = int((paths.global_config().get("watchdog") or {}).get("lease_seconds") or 120)
    try:
        old = time.time() - lease - 5
        os.utime(paths.sessions_dir() / f"{ST._safe(sid)}.json", (old, old))
    except OSError:
        pass


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "PreToolUse": on_pre_tool,
    "PostToolUse": on_post_tool,
    "Stop": on_stop,
    "SubagentStop": on_subagent_stop,
    "PostToolUseFailure": on_post_tool_failure,
    "Notification": on_notification,
    "StopFailure": on_stop_failure,
    "SessionEnd": on_session_end,
}


def answer_deadline(name: str, exc: "_Deadline", ev: Optional[dict] = None) -> None:
    """Out of time: PreToolUse is refused (a hook that times out lets the call through unchecked); every other event
    just ends quietly, its bookkeeping is best effort."""
    log(f"DEADLINE ({exc.kind}) in {name}")
    if name == "PreToolUse" and not _ANSWER["emitted"]:
        d = deadline_denial(exc)
        text = f"[orkestra:{d.guard}] {d.reason}"
        if isinstance(ev, dict):
            try:                            # the same repeat counter as every other refusal (a loop on it must end)
                text, _key = escalate_denial(ev, binding_for(ev), d)
            except Exception as err:
                log(f"deadline escalation failed: {err}")
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": text}})
    _ANSWER["closed"] = True


def main(raw: Optional[str] = None) -> int:
    """Run one hook. `raw` is the event JSON when the caller (hookboot) already read stdin to decide whether this event
    needs any work; None reads stdin here."""
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    utf8_stdio()
    _ANSWER.update(closed=False, emitted=False)
    ev: Optional[dict] = None
    try:
        if raw is None:
            raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
        if isinstance(ev, dict) and ev.get("cursor_version"):
            # Cursor imports Claude Code plugins and runs their hooks with ITS OWN payloads (tool "Shell", event
            # "preToolUse" …). Those are handled by hooks/cursor_hook.py; answering here too would do everything twice
            # and with the wrong vocabulary.
            return 0
        if isinstance(ev, dict) and ev.get("tool_name") == "PowerShell":
            ev = guards.normalize_event(ev)         # the PowerShell tool is a shell: every handler sees a Bash call
        if not name:
            name = ev.get("hook_event_name", "")
        handler = HANDLERS.get(name)
        if handler:
            run_with_hook_deadline(lambda: handler(ev))
    except _Deadline as exc:
        answer_deadline(name, exc, ev)
    except Exception as exc:  # fail open
        import traceback                    # only an error needs it (importing it costs 9 ms)
        log(f"ERROR in {name}: {exc}\n{traceback.format_exc()}")
    return 0
