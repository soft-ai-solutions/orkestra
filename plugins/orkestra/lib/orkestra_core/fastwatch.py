"""Fast stall watcher: one singleton process that finds DETERMINISTIC stalls within seconds and costs no tokens.

`orkestra watchdog` is a periodic whole-state scan from a subprocess (minute resolution, inferred classes). This module
is its fast companion: one resident loop that stats the open runs' files every few seconds and parses a file only
when its (mtime_ns, size) changed. A thinking model is NOT a stall: every predicate below is a plain function of
rows, registry files and clocks (unit-tested on synthetic timelines), never of "the model has been quiet".

Process model
  * singleton: an flock'd pid file (ORKESTRA_HOME/watchdog.pid); the kernel drops the lock when the holder dies, so a
    pid file left by kill -9 is simply taken over. Hosted by the bridge as a thread when the bridge runs, else a
    detached daemon started by `ensure_running()` (SessionStart) that exits after `watchdog.daemon_idle_minutes` with
    no open run and no live session.
  * cadence: 5 s tick, 1 s while a candidate is about to fire, 15 s with no open run. Open runs are rediscovered every
    30 s. Wall time is used for stored stamps, the injected clock for everything else (tests drive it).

What it may write: only the HEAL_ACTIONS below (stale bindings to missing / closed runs, an honest synthetic `end` row
for a command a later row of the same agent outlived, dead entries of the subagent registry, one ledger line for a
pause/resume storm) plus its own files (pid, status, incident book, bounded audit log). Everything else is a
notification. It never signals a process it did not start; the only model process it can cause is the existing
capped `claude -p --resume` of watchdog._spawn_resume (headless runs, 3 per run, 10 s / 60 s / 300 s ladder).

After 3 consecutive tick failures the circuit breaker opens: heals and resumes stop, notifications continue.

Windows has no flock and no detached-daemon story here: `ensure_running` is a no-op there.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import activity as A
from . import paths
from . import state as ST
from . import tempo as T
from . import watchdog as W
from .i18n import t
from .util import LockTimeout, append_record, file_lock, read_json, set_lock_policy, write_json_atomic

try:                                               # POSIX only; Windows runs without a fast watcher
    import fcntl
except ImportError:                                # pragma: no cover
    fcntl = None                                   # type: ignore[assignment]

_IS_WINDOWS = os.name == "nt"                      # a module constant so tests can exercise the Windows branch

HEAL_ACTIONS = frozenset({"unbind_stale", "close_open_command", "purge_agents", "freeze_storm"})

# thresholds (seconds) of the deterministic predicates; see the module docstring of the audit design
DENY_REPEATS, DENY_WINDOW = 3, 120.0               # the same refusal this often inside the window: a deny loop
DENY_BLOCK_REPEATS, DENY_BLOCK_WINDOW = 8, 600.0   # ... this often inside the long window: the run should be blocked
FLIP_COUNT, FLIP_WINDOW = 3, 600.0                 # status changes (pause/resume) per window: a storm
PAUSED_TOOL_AGE, PAUSED_SETTLE = 60.0, 5.0         # a paused run whose agents still run tools this recently
REPORT_OVERDUE = float(T.DUE_REPORT_SECONDS)       # a stopped subagent's report that never arrived (60 s)
IDLE_SETTLE = 20.0                                 # autopilot run, turn ended, work left: settle time
PERM_WAIT = 30.0                                   # a permission prompt nobody answered
SOFT_SILENCE = 180.0                               # dashboard amber only
PROBE_GAP = 30.0                                   # two CPU samples this far apart before a command is called hung
HOOK_ERRORS, HOOK_WINDOW = 3, 60.0                 # ERROR lines in hooks.log per window
RETRY_DELAYS = (10.0, 60.0, 300.0)                 # StopFailure ladder (first delay, then after each attempt)
FAST_MIN_AGE = 10.0                                # a session must be this quiet before a fast resume
ACTIONABLE = ("work", "cli", "dispatch")           # tempo.next_action actions that mean "there is work to do"
TICK, TICK_HOT, TICK_IDLE, DISCOVER = 5.0, 1.0, 15.0, 30.0
HOT_WITHIN = 6.0                                   # a candidate due this soon switches to the 1 s cadence
RECHECK = 30.0                                     # an unchanged run is re-evaluated at least this often
BREAKER_FAILS = 3
AUDIT_MAX_BYTES = 1024 * 1024
LIVE_SESSION = 300.0                               # a binding touched this recently counts as a live session (idle exit)
SPAWN_COOLDOWN = 20.0                              # ensure_running does not respawn a booting daemon sooner
# message key of every finding kind and of the bridge's turn messages (literal keys: the catalog audit greps the sources)
KIND_KEYS = {"deny_loop": "watchdog.fast_deny_loop", "control_flips": "watchdog.fast_control_flips",
             "paused_working": "watchdog.fast_paused_working", "report_overdue": "watchdog.fast_report_overdue",
             "idle_active": "watchdog.fast_idle_active", "permission_wait": "watchdog.fast_permission_wait",
             "hung_command": "watchdog.fast_hung_command", "stop_failure": "watchdog.fast_stop_failure",
             "dead_session": "watchdog.fast_dead_session", "soft_silence": "watchdog.fast_soft_silence",
             "hooks_degraded": "watchdog.fast_hooks_degraded"}
BRIDGE_KEYS = {"timeout": "watchdog.turn_timeout", "idle": "watchdog.turn_idle", "driver_alive": "watchdog.driver_alive"}
_GATE_RE = r"\borkestra\s+(\S+\s+)*(gate|tdd|evidence)\b"
_ERR_LINE = re.compile(r"^\S+ ERROR in ")
_TB_FILE = re.compile(r'File "([^"]+)", line (\d+)')
_BIN = Path(__file__).resolve().parent.parent.parent / "bin" / "orkestra"


# ===================================================================== predicates (pure)

def _f(kind: str, since: float, level: str = "notify", wake: bool = False, **data: Any) -> Dict[str, Any]:
    return {"kind": kind, "level": level, "since": float(since), "wake": wake, "data": data}


class RunCtx:
    """Everything a predicate may look at: no I/O happens through it."""

    def __init__(self, project: Path, st: Dict[str, Any], rows: List[Dict[str, Any]], now: float,
                 wd: Optional[Dict[str, Any]] = None, *, pending: Any = None, reports_due: Any = None,
                 handback_at: float = 0.0, sessions: Any = None, state_mtime: float = 0.0,
                 next_action: Optional[Callable[[], Dict[str, Any]]] = None, proc_alive: Optional[bool] = None,
                 probe_pending: bool = False, pid_alive: Optional[Callable[[int], bool]] = None):
        self.project, self.st, self.rows, self.now = Path(project), st, rows, float(now)
        self.wd = wd or T.wd_cfg()
        self.pending = list(pending or [])
        self.reports_due = dict(reports_due or {})
        self.handback_at = float(handback_at or 0.0)
        self.sessions = list(sessions or [])
        self.state_mtime = float(state_mtime or 0.0)
        self.next_action = next_action
        self.proc_alive = proc_alive
        self.probe_pending = probe_pending
        self.pid_alive = pid_alive
        self.due_hints: List[float] = []           # times at which a pending predicate would fire (cadence + recheck)
        self._running: Optional[List[Tuple[str, float, str]]] = None
        self._last: Optional[float] = None
        self._cand: Any = False

    @property
    def running(self) -> List[Tuple[str, float, str]]:
        """Open commands (tempo's lenient reading), computed once per evaluation."""
        if self._running is None:
            self._running = T._running_commands(self.rows)
        return self._running

    @property
    def last_act(self) -> float:
        if self._last is None:
            self._last = T.last_activity(self.rows)
        return self._last

    def hint(self, at: float) -> None:
        if at > self.now:
            self.due_hints.append(float(at))

    @property
    def status(self) -> str:
        return str(self.st.get("status") or "")

    @property
    def headless(self) -> bool:
        return self.st.get("mode") == "headless"


def _ts(r: Dict[str, Any]) -> float:
    try:
        return float(r.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def deny_loop(c: RunCtx) -> Optional[Dict[str, Any]]:
    """The same refusal over and over (one agent hit the same denial 235 times): >= 3 in 120 s, or >= 8 in 600 s."""
    groups: Dict[str, List[float]] = collections.defaultdict(list)
    for r in c.rows:
        ts = _ts(r)
        if r.get("kind") == "deny" and 0 <= c.now - ts <= DENY_BLOCK_WINDOW:
            groups[str(r.get("key") or r.get("what") or "")].append(ts)
    best: Optional[Tuple[int, str, List[float]]] = None
    for key, stamps in groups.items():
        short = [s for s in stamps if c.now - s <= DENY_WINDOW]
        if len(short) >= DENY_REPEATS or len(stamps) >= DENY_BLOCK_REPEATS:
            if best is None or len(stamps) > best[0]:
                best = (len(stamps), key, stamps)
    if best is None:
        return None
    what = next((str(r.get("what") or "") for r in reversed(c.rows) if r.get("kind") == "deny"
                 and str(r.get("key") or r.get("what") or "") == best[1]), "")
    return _f("deny_loop", min(best[2]), count=best[0], block=best[0] >= DENY_BLOCK_REPEATS, what=what[:80])


def control_flips(c: RunCtx) -> Optional[Dict[str, Any]]:
    """Pause/resume flipping (any agent's printed bind line rebinds the shared session): >= 3 status rows in 10 min."""
    stamps = [_ts(r) for r in c.rows if r.get("kind") == "status" and 0 <= c.now - _ts(r) <= FLIP_WINDOW]
    if len(stamps) < FLIP_COUNT:
        return None
    return _f("control_flips", min(stamps), level="freeze", flips=len(stamps), count=len(stamps))


def paused_working(c: RunCtx) -> Optional[Dict[str, Any]]:
    """A paused run whose agents still ran a tool in the last minute, after the pause had time to settle."""
    if c.status != "paused":
        return None
    live = [_ts(r) for r in c.rows if r.get("kind") in ("tool", "result")
            and 0 <= c.now - _ts(r) <= PAUSED_TOOL_AGE and _ts(r) > c.state_mtime + PAUSED_SETTLE]
    return _f("paused_working", max(live)) if live else None


def _main_progress(rows: List[Dict[str, Any]], after: float) -> bool:
    return any((r.get("agent") or "main") == "main" and _ts(r) > after
               and r.get("kind") in ("tool", "answer", "deny", "result", "user") and not T._is_wait_row(r) for r in rows)


def report_overdue(c: RunCtx) -> Optional[Dict[str, Any]]:
    """A background subagent stopped, its report was due (agents.json `reports_due`) and no hand-back, no pending
    agent and no main action followed for 60 s of whole-run silence. Keyed on the registry record, not on 'main idle'."""
    if c.status != "active" or not c.reports_due or c.pending:
        return None
    handback = max([c.handback_at] + [_ts(r) for r in c.rows if r.get("kind") == "handback"])
    open_due = [float(v.get("at") or 0) for v in c.reports_due.values() if float(v.get("at") or 0) > handback]
    if not open_due:
        return None
    since = min(open_due)
    if _main_progress(c.rows, since):
        return None
    quiet_since = max(since, T.last_activity(c.rows, ignore_end=True))
    if c.now - quiet_since < REPORT_OVERDUE:
        c.hint(quiet_since + REPORT_OVERDUE)
        return None
    return _f("report_overdue", since, wake=True, count=len(open_due))


def idle_active(c: RunCtx) -> Optional[Dict[str, Any]]:
    """An active autopilot run whose main session ended its turn with work left and nobody to continue it."""
    if c.status != "active" or not T.is_autopilot(c.st) or c.pending or c.next_action is None:
        return None
    main = [r for r in c.rows if (r.get("agent") or "main") == "main" and r.get("kind") not in ("end", "nudge")]
    if not main or main[-1].get("kind") != "idle":
        return None
    idle_ts = _ts(main[-1])
    if c.state_mtime > idle_ts:
        return None                                # the state moved on after the turn ended: somebody works
    if c.now - idle_ts < IDLE_SETTLE:
        c.hint(idle_ts + IDLE_SETTLE)
        return None
    try:
        action = str((c.next_action() or {}).get("action") or "")
    except Exception:
        return None
    if action not in ACTIONABLE:
        return None
    return _f("idle_active", idle_ts, wake=True, action=action, interactive=not c.headless)


def permission_wait(c: RunCtx) -> Optional[Dict[str, Any]]:
    """The host shows a permission prompt nobody answered: tell the human which tool. Never approves anything."""
    last = next((r for r in reversed(c.rows) if r.get("kind") == "perm_wait"), None)
    if last is None:
        return None
    ts = _ts(last)
    if any(_ts(r) > ts and r.get("kind") not in A.ASIDE_KINDS for r in c.rows):
        return None                                # something happened after it: it was answered
    if c.now - ts < PERM_WAIT:
        c.hint(ts + PERM_WAIT)
        return None
    return _f("permission_wait", ts, what=str(last.get("what") or "")[:140])


def hung_candidate(c: RunCtx) -> Optional[Tuple[str, float, str]]:
    """The oldest open command past its limit while NO agent produced a row since it started (else: slow, not hung)."""
    if c.status != "active":
        return None
    if c._cand is not False:
        return c._cand
    best = None
    last = c.last_act
    for agent, ts, what in c.running:
        limit = float(c.wd["gate_command_seconds"] if re.search(_GATE_RE, what) else c.wd["hung_command_seconds"])
        if agent == "main" and T.WAIT_CMD_RE.search(what):
            continue                               # the main session's own wait command is waiting, not hanging
        if c.now - ts <= limit:
            c.hint(ts + limit)
            continue
        if last > ts:
            continue
        if best is None or ts < best[1]:
            best = (agent, ts, what)
    c._cand = best
    return best


def hung_command(c: RunCtx) -> Optional[Dict[str, Any]]:
    cand = hung_candidate(c)
    if cand is None or c.proc_alive is True or c.probe_pending:
        return None
    agent, ts, what = cand
    return _f("hung_command", ts, what=what[:80], agent=agent, verified=c.proc_alive is False)


def stop_failure(c: RunCtx) -> Optional[Dict[str, Any]]:
    """The turn died on a model/API error and nothing happened since (retry ladder: see resume_due)."""
    if c.status != "active":
        return None
    last = next((r for r in reversed(c.rows) if r.get("kind") == "api_error"), None)
    if last is None:
        return None
    ts = _ts(last)
    if any(_ts(r) > ts and r.get("kind") not in A.ASIDE_KINDS for r in c.rows):
        return None
    return _f("stop_failure", ts, wake=True, what=str(last.get("what") or "")[:80])


def soft_silence(c: RunCtx) -> Optional[Dict[str, Any]]:
    """No row from any agent for 180 s with no command running: amber in the dashboard only, never an incident."""
    if c.status != "active" or c.running:
        return None
    last = max(c.last_act, c.state_mtime)
    if not last:
        return None
    if c.now - last < SOFT_SILENCE:
        c.hint(last + SOFT_SILENCE)
        return None
    return _f("soft_silence", last, level="flag")


def dead_session(c: RunCtx) -> Optional[Dict[str, Any]]:
    """Every bound session is gone: its recorded host pid is dead (notify), or it has been silent beyond the lease
    (a flag only: a user who is merely reading is not a dead session). A dead pid younger than one health check
    is not announced: a restart often looks like that for a few seconds and then binds again."""
    if c.status != "active" or not c.sessions:
        return None
    pids = [s.get("host_pid") for s in c.sessions]
    confirm = float(c.wd.get("health_check_seconds") or 120)
    if c.pid_alive is not None and all(isinstance(p, int) and p > 0 for p in pids) \
            and not any(c.pid_alive(p) for p in pids):
        age = min(float(s.get("age") or 0) for s in c.sessions)
        if age < confirm:
            return None
        return _f("dead_session", c.now - age, pid=pids[0])
    lease = float(c.wd.get("lease_seconds") or 120)
    if all(float(s.get("age") or 0) > lease for s in c.sessions):
        return _f("dead_session", c.now - min(float(s.get("age") or 0) for s in c.sessions), level="flag")
    return None


def hooks_degraded(events: List[Tuple[float, int]], now: float, where: str = "") -> Optional[Dict[str, Any]]:
    """`events` = (observed at, number of new ERROR lines): >= 3 inside 60 s means the hooks fail open."""
    recent = [(ts, n) for ts, n in events if 0 <= now - ts <= HOOK_WINDOW]
    total = sum(n for _, n in recent)
    if total < HOOK_ERRORS:
        return None
    return _f("hooks_degraded", min(ts for ts, _ in recent), errors=total, count=total, what=where)


def stale_commands(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bash calls without an end row that a LATER row of the same agent outlived (hooks never fire for interrupted
    calls). tempo already treats them as closed; this lists them so the rows can be healed for every other reader."""
    strict: Dict[str, Tuple[str, str, float, str]] = {}
    later: Dict[str, float] = {}
    for r in rows:
        agent = str(r.get("agent") or "main")
        aid = str(r.get("aid") or "")
        key = aid or agent
        kind, ts = r.get("kind"), _ts(r)
        if r.get("tool") == "Bash" and kind == "tool":
            strict[key] = (agent, aid, ts, str(r.get("what") or ""))
            later.pop(key, None)
        elif kind in ("end", "deny") and r.get("tool") == "Bash":
            strict.pop(key, None)
            later.pop(key, None)
        elif kind == "agent_done":
            for k in (key, agent):
                if k in strict:
                    strict.pop(k, None)
                    later.pop(k, None)
                    break
        elif agent == "main" and kind == "idle":
            strict.clear()
            later.clear()
        elif agent == "main" and kind in ("wait_agents", "user"):
            strict.pop("main", None)
            later.pop("main", None)
        elif kind not in ("nudge", "status", "bind") and key in strict and ts > strict[key][2] + T.BATCH_MARGIN \
                and key not in later:
            later[key] = ts
    return [{"agent": strict[k][0], "aid": strict[k][1], "started": strict[k][2], "closed_at": later[k],
             "what": strict[k][3]} for k in strict if k in later]


RUN_PREDICATES = (deny_loop, control_flips, paused_working, report_overdue, idle_active, permission_wait,
                  hung_command, stop_failure, soft_silence, dead_session)


def analyze(c: RunCtx) -> List[Dict[str, Any]]:
    out = []
    for p in RUN_PREDICATES:
        f = p(c)
        if f:
            out.append(f)
    return out


def resume_due(count: int, last: float, since: float, now: float, first_delay: float = 0.0, cap: int = 3,
               delays: Tuple[float, ...] = RETRY_DELAYS) -> bool:
    """The fast resume ladder: attempt 1 at since + first_delay, then 60 s and 300 s after the previous attempt;
    never more than `cap` (the existing watchdog.max_auto_resumes)."""
    if count >= cap:
        return False
    if count <= 0:
        return now >= since + first_delay
    return now >= last + delays[min(count, len(delays) - 1)]


# ===================================================================== singleton

def _pid_path() -> Path:
    return paths.orkestra_home() / "watchdog.pid"


def _status_path() -> Path:
    return paths.orkestra_home() / "watchdog-fast.json"


class Singleton:
    """flock on watchdog.pid: held for the life of the process, released by the kernel when it dies."""

    def __init__(self) -> None:
        self.fh: Any = None

    def acquire(self) -> bool:
        if _IS_WINDOWS or fcntl is None:
            return False
        path = _pid_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+")
        except OSError:
            return False
        # is_running() probes with a transient shared lock: a few short retries so that a boot which collides with a
        # probe does not give up (and leave no watcher until the next SessionStart); a real holder is refused in ~100 ms
        for attempt in range(6):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == 5:
                    fh.close()
                    return False
                time.sleep(0.02)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()} {int(time.time())}\n")
            fh.flush()
        except OSError:
            pass
        self.fh = fh
        return True

    def release(self) -> None:
        fh, self.fh = self.fh, None
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fh.close()


def is_running() -> bool:
    """Does another holder have the singleton lock? A shared-lock probe: nothing is written."""
    if _IS_WINDOWS or fcntl is None:
        return False
    try:
        with open(_pid_path(), "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def holder_pid() -> Optional[int]:
    try:
        return int(_pid_path().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _autostart() -> bool:
    return bool((paths.global_config().get("watchdog") or {}).get("autostart", True))


def start_daemon() -> Optional[int]:
    """Start the detached watcher now (no checks of autostart). Returns the child's pid or None."""
    if _IS_WINDOWS:
        return None
    cfg = paths.global_config().get("watchdog") or {}
    idle = int(float(cfg.get("daemon_idle_minutes") or 30) * 60)
    argv = [sys.executable, str(_BIN), "watchdog", "--loop", "--quiet", "--idle-exit", str(idle)]
    home = paths.orkestra_home()
    home.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True, start_new_session=True, cwd=str(home), env=dict(os.environ))
    return int(proc.pid)


def ensure_running(project: Optional[Path] = None) -> None:
    """Make sure one fast watcher runs (called lazily from SessionStart). Never raises; < 5 ms when one already runs.

    A no-op on Windows (no flock, no detached daemon here), with ORKESTRA_NO_DAEMON=1 (the test suite pins it) and
    with `watchdog.autostart: false`. A freshly spawned daemon is not respawned for SPAWN_COOLDOWN seconds."""
    try:
        if os.environ.get("ORKESTRA_NO_DAEMON") == "1" or _IS_WINDOWS or fcntl is None:
            return
        if is_running():
            return
        if not _autostart():
            return
        stamp = paths.orkestra_home() / "watchdog.spawn"
        try:
            if time.time() - stamp.stat().st_mtime < SPAWN_COOLDOWN:
                return
        except OSError:
            pass
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(time.time())))
        start_daemon()
    except Exception:
        return


# ===================================================================== notifications and audit

def bridge_text(kind: str, **fields: Any) -> str:
    """The bridge's human-facing turn messages (timeout, idle cap, driver still alive), in the current language."""
    return t(BRIDGE_KEYS[kind], **fields)


def audit(event: str, **fields: Any) -> None:
    """Bounded JSONL log of everything the watcher notified or did: logs/watchdog.log, rotated at 1 MB."""
    try:
        log = paths.logs_dir() / "watchdog.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log.stat().st_size > AUDIT_MAX_BYTES:
                os.replace(log, log.with_name("watchdog.log.1"))
        except OSError:
            pass
        row = dict({"ts": round(time.time(), 3), "event": event}, **fields)
        append_record(log, (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    except Exception:
        pass


def default_notifier(title: str, text: str, payload: Dict[str, Any]) -> None:
    """The existing channels: desktop notification and webhook (the bridge adds Telegram when it hosts the watcher)."""
    cfg = paths.global_config()
    if (cfg.get("watchdog") or {}).get("macos_notify", True):
        W._notify(title, text)
    W._webhook(cfg, payload)


def _read_tail(path: Path, offset: int, cap: int = 512 * 1024) -> Tuple[bytes, int]:
    """Complete lines appended since `offset`: (bytes, new offset). A trailing partial line stays for the next read."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(cap)
    end = data.rfind(b"\n")
    if end < 0:
        return b"", offset
    return data[:end + 1], offset + end + 1


class _Run:
    """Cached view of one open run: parsed only when its files change."""

    def __init__(self, project: Path, rid: str, rdir: Path):
        self.project, self.rid, self.dir = project, rid, rdir
        self.key = f"{project}|{rid}"
        self.p_state, self.p_act, self.p_agents = (str(rdir / n) for n in ("state.json", "activity.jsonl", "agents.json"))
        self.st: Dict[str, Any] = {}
        self.st_sig: Any = None
        self.rows: List[Dict[str, Any]] = []
        self.act_sig: Any = None
        self.offset = 0
        self.ino: Any = None
        self.agents_sig: Any = None
        self.raw_agents: Dict[str, Any] = {}
        self.changed = True
        self.rows_changed = True
        self.recheck_at = 0.0
        self.findings: List[Dict[str, Any]] = []
        self.hints: List[float] = []
        self.probe: Dict[str, float] = {}
        self.sessions: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self.project.name


class Watcher:
    def __init__(self, notifier: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
                 clock: Optional[Callable[[], float]] = None, probe: Optional[Callable[[Path, Dict[str, Any]], Optional[float]]] = None,
                 spawner: Optional[Callable[..., Any]] = None, alive: Optional[Callable[..., bool]] = None,
                 bridge_owns: Optional[Callable[[List[str]], bool]] = None,
                 next_action: Optional[Callable[[Path, Dict[str, Any], float], Dict[str, Any]]] = None,
                 async_spawn: bool = False):
        self.notifier = notifier or default_notifier
        self.clock = clock or time.time
        self.probe = probe or _default_probe
        self.spawner = spawner or W._spawn_resume
        self.alive = alive or W._process_alive
        self.bridge_owns = bridge_owns or W._bridge_owns
        self.next_action = next_action or (lambda proj, st, now: T.next_action(proj, st, deep=False, now=now))
        self.async_spawn = async_spawn
        self.runs: Dict[str, _Run] = {}
        self.last_discover = -1e18
        self.last_busy: Optional[float] = None
        self.fail_streak = 0
        self.breaker_open = False
        self.ticks = 0
        self.health_checks = 0
        self.last_health = -1e18
        self.started = time.time()
        self.incidents: Dict[str, Dict[str, Any]] = {}
        self.frozen: set = set()
        self.hook_events: List[Tuple[float, int]] = []
        self.hooks_off: Optional[int] = None
        self.hook_where = ""
        self.sess_cache: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
        self.inflight: set = set()
        self.last_status = 0.0
        self.interval = TICK_IDLE
        self._actions: List[Dict[str, Any]] = []
        self._load_incidents()

    # -------------------------------------------------------------- writes: only through act()
    def act(self, name: str, fn: Callable[[], Any], **info: Any) -> bool:
        assert name in HEAL_ACTIONS, f"not a heal action: {name}"
        if self.breaker_open:
            return False
        fn()
        rec = dict({"action": name}, **info)
        self._actions.append(rec)
        audit("heal", **rec)
        return True

    # -------------------------------------------------------------- the tick
    def step(self, now: Optional[float] = None) -> Dict[str, Any]:
        """One tick behind the circuit breaker: 3 failures in a row open it (notify only from then on)."""
        try:
            res = self.tick(now)
            self.fail_streak = 0
            return res
        except Exception as exc:
            self.fail_streak += 1
            audit("tick_failed", error=f"{type(exc).__name__}: {exc}", streak=self.fail_streak)
            if self.fail_streak >= BREAKER_FAILS and not self.breaker_open:
                self.breaker_open = True
                audit("breaker_open", error=str(exc))
                self._send("breaker", "orkestra", "", t("watchdog.fast_breaker", project="orkestra", count=self.fail_streak,
                                                        what=f"{type(exc).__name__}: {exc}"[:120]))
            return {"interval": TICK_IDLE, "actions": [], "findings": 0, "error": str(exc)}

    def tick(self, now: Optional[float] = None, force_discover: bool = False) -> Dict[str, Any]:
        now = self.clock() if now is None else float(now)
        self._actions = []
        cfg = paths.global_config()
        wd = T.wd_cfg(cfg)
        if self.last_busy is None:
            self.last_busy = now
        if force_discover or now - self.last_discover >= DISCOVER:
            self._discover(now)
            self.last_discover = now
        sessions = self._scan_sessions(now)
        self._poll_hooks(now)
        for rc in list(self.runs.values()):
            try:
                self._refresh(rc, sessions, now)
                if rc.key not in self.runs:
                    continue
                if rc.changed or now >= rc.recheck_at:
                    self._evaluate(rc, now, wd)
                self._settle(rc, now, cfg)
            except Exception:
                if not self.breaker_open:
                    raise
                audit("run_failed", run=rc.key)
        self._reconcile(now, cfg)
        busy = bool(self.runs) or any(s["age"] < LIVE_SESSION for ss in sessions.values() for s in ss)
        if busy:
            self.last_busy = now
        self._reap()
        hot = any(h - now <= HOT_WITHIN for rc in self.runs.values() for h in rc.hints if h >= now - 1)
        self.interval = TICK_IDLE if not self.runs else (TICK_HOT if hot else TICK)
        self.ticks += 1
        every = float(wd.get("health_check_seconds") or 120)
        if now - self.last_health >= every:
            self.last_health = now
            self.health_checks += 1
        if now - self.last_status >= 15 or self._actions:
            self._write_status(now)
            self.last_status = now
        return {"interval": self.interval, "actions": list(self._actions),
                "findings": sum(len(r.findings) for r in self.runs.values())}

    # -------------------------------------------------------------- discovery and caches
    def _discover(self, now: float) -> None:
        seen = set()
        for item in W._open_runs():
            proj, st = item["project"], item["state"]
            key = f"{proj}|{st['run_id']}"
            seen.add(key)
            if key not in self.runs:
                self.runs[key] = _Run(Path(proj), st["run_id"], Path(item["dir"]))
        for key in [k for k in self.runs if k not in seen]:
            self.runs.pop(key, None)
        self._heal_bindings()

    def _scan_sessions(self, now: float) -> Dict[str, List[Dict[str, Any]]]:
        """{project|run_id: [{id, age, host_pid}]} from one scandir of sessions/ (content re-read only when a binding
        file was replaced; a heartbeat only touches the mtime)."""
        out: Dict[str, List[Dict[str, Any]]] = {}
        try:
            entries = list(os.scandir(paths.sessions_dir()))
        except OSError:
            entries = []
        live = set()
        for e in entries:
            if not e.name.endswith(".json") or not e.is_file():
                continue
            try:
                stt = e.stat()
            except OSError:
                continue
            live.add(e.name)
            sig = (stt.st_ino, stt.st_size)
            cached = self.sess_cache.get(e.name)
            if cached is None or cached[0] != sig:
                data = read_json(Path(e.path))
                cached = (sig, data if isinstance(data, dict) else {})
                self.sess_cache[e.name] = cached
            data = cached[1]
            if data.get("run_id") and data.get("project"):
                pid = data.get("host_pid")
                out.setdefault(f"{data['project']}|{data['run_id']}", []).append(
                    {"id": e.name[:-5], "age": max(0.0, now - stt.st_mtime), "heartbeat": stt.st_mtime,
                     "host_pid": pid if isinstance(pid, int) else None})
        for k in [k for k in self.sess_cache if k not in live]:
            self.sess_cache.pop(k, None)
        for v in out.values():
            v.sort(key=lambda s: -s["heartbeat"])
        return out

    def _heal_bindings(self) -> None:
        """Drop bindings that can never be right again: their run is missing or closed (ST.heal_binding decides)."""
        try:
            files = [e.name[:-5] for e in os.scandir(paths.sessions_dir()) if e.name.endswith(".json")]
        except OSError:
            return
        for sid in files:
            data = read_json(paths.sessions_dir() / f"{sid}.json")
            if not isinstance(data, dict):
                continue
            project, rid = str(data.get("project") or ""), str(data.get("run_id") or "")
            if not rid or not project:
                continue
            st = read_json(ST.state_path(Path(project), rid)) if ST._is_abs(project) else None
            missing = ST._is_abs(project) and Path(project).is_dir() and not ST.state_path(Path(project), rid).exists()
            closed = isinstance(st, dict) and st.get("status") in ST.TERMINAL_STATUSES
            if missing or closed:
                self.act("unbind_stale", lambda s=sid: ST.heal_binding(s), session=sid, run=rid)
                self.sess_cache.pop(f"{sid}.json", None)

    def _poll_hooks(self, now: float) -> None:
        log = paths.logs_dir() / "hooks.log"
        try:
            size = log.stat().st_size
        except OSError:
            self.hooks_off = None
            return
        if self.hooks_off is None or size < self.hooks_off:
            self.hooks_off = size                  # first look (do not replay history) or a rotated file
            return
        if size == self.hooks_off:
            return
        try:
            data, new_off = _read_tail(log, self.hooks_off, 64 * 1024)
        except OSError:
            return
        self.hooks_off = new_off
        lines = data.decode("utf-8", "replace").splitlines()
        errors = sum(1 for ln in lines if _ERR_LINE.match(ln))
        if errors:
            files = _TB_FILE.findall("\n".join(lines))
            if files:
                self.hook_where = f"{os.path.basename(files[-1][0])}:{files[-1][1]}"
            self.hook_events.append((now, errors))
        self.hook_events = [(ts, n) for ts, n in self.hook_events if now - ts <= 3 * HOOK_WINDOW]

    def _refresh(self, rc: _Run, sessions: Dict[str, List[Dict[str, Any]]], now: float) -> None:
        rc.changed = False
        rc.rows_changed = False
        try:
            stt = os.stat(rc.p_state)
        except OSError:
            self.runs.pop(rc.key, None)
            return
        sig = (stt.st_mtime_ns, stt.st_size)
        if sig != rc.st_sig:
            st = read_json(Path(rc.p_state))
            if not isinstance(st, dict) or st.get("status") not in ST.OPEN_STATUSES:
                self.runs.pop(rc.key, None)
                return
            rc.st, rc.st_sig, rc.changed = st, sig, True
        rc.state_mtime = stt.st_mtime
        try:
            ast = os.stat(rc.p_act)
            asig = (ast.st_mtime_ns, ast.st_size, ast.st_ino)
        except OSError:
            ast, asig = None, None
        if asig != rc.act_sig:
            self._read_rows(rc, ast)
            rc.act_sig, rc.changed, rc.rows_changed = asig, True, True
        try:
            gst = os.stat(rc.p_agents)
            gsig = (gst.st_mtime_ns, gst.st_size)
        except OSError:
            gsig = None
        if gsig != rc.agents_sig:
            rc.raw_agents = A._agents_load(A._agents_path(rc.project, rc.rid)) if gsig else {"pending": {}, "seen": {}}
            rc.agents_sig, rc.changed = gsig, True
        rc.sessions = sessions.get(rc.key, [])

    def _read_rows(self, rc: _Run, stt: Any) -> None:
        path = rc.dir / "activity.jsonl"
        if stt is None:
            rc.rows, rc.offset, rc.ino = [], 0, None
            return
        if stt.st_ino != rc.ino or stt.st_size < rc.offset:
            rc.rows = A.read_rows(rc.project, rc.rid, max_bytes=256 * 1024)       # first look or rotated: tail reload
            rc.offset, rc.ino = stt.st_size, stt.st_ino
            return
        try:
            data, rc.offset = _read_tail(path, rc.offset)
        except OSError:
            return
        for line in data.decode("utf-8", "ignore").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rc.rows.append(row)
        if len(rc.rows) > 1500:
            del rc.rows[:-1500]

    # -------------------------------------------------------------- evaluation
    def _evaluate(self, rc: _Run, now: float, wd: Dict[str, Any]) -> None:
        c = RunCtx(rc.project, rc.st, rc.rows, now, wd, pending=self._live_pending(rc, now, wd),
                   reports_due=rc.raw_agents.get("reports_due") or {}, handback_at=_handback_at(rc.raw_agents),
                   sessions=rc.sessions, state_mtime=rc.state_mtime,
                   next_action=lambda: self.next_action(rc.project, rc.st, now), pid_alive=_pid_exists)
        cand = hung_candidate(c)
        if cand is not None:
            self._sample(rc, c, now)
        else:
            rc.probe = {}
        rc.findings = analyze(c)
        rc.hints = list(c.due_hints)
        rc.recheck_at = min([now + RECHECK] + [h for h in c.due_hints if h > now])
        self._heal(rc, c, now)

    def _live_pending(self, rc: _Run, now: float, wd: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not (rc.raw_agents.get("pending") or {}):
            return []
        return A.agents_pending(rc.project, rc.rid, max_age=float(wd["agent_max_minutes"]) * 60, now=now)

    def _sample(self, rc: _Run, c: RunCtx, now: float) -> None:
        """CPU evidence for a hung-command candidate: two samples PROBE_GAP apart. No evidence (probe gives None)
        leaves the finding unverified; a second sample that shows CPU movement proves life and silences it."""
        cpu = self.probe(rc.project, rc.st)
        if cpu is None:
            return
        first = rc.probe.get("cpu")
        if first is None:
            rc.probe = {"cpu": cpu, "at": now}
            c.probe_pending = True
            c.hint(now + PROBE_GAP)
            return
        if now - rc.probe["at"] < PROBE_GAP:
            c.probe_pending = True
            c.hint(rc.probe["at"] + PROBE_GAP)
            return
        c.proc_alive = cpu > first + 0.01
        rc.probe = {"cpu": cpu, "at": now}

    def _heal(self, rc: _Run, c: RunCtx, now: float) -> None:
        for s in (stale_commands(rc.rows) if rc.rows_changed else []):
            if self.breaker_open:
                break
            self.act("close_open_command", lambda s=s: self._synthetic_end(rc, s), run=rc.key, agent=s["agent"])
        raw = rc.raw_agents.get("pending") or {}
        if raw:
            keep = {str(a.get("id")) for a in c.pending}
            dead = [k for k in raw if k not in keep]
            if dead:
                self.act("purge_agents", lambda d=dead: A.agents_purge(rc.project, rc.rid, ids=d), run=rc.key, ids=dead)
        flips = next((f for f in rc.findings if f["kind"] == "control_flips"), None)
        if flips is None:
            self.frozen.discard(rc.key)
        elif rc.key not in self.frozen and not self.breaker_open:
            self.frozen.add(rc.key)
            self.act("freeze_storm", lambda: ST.ledger(
                rc.project, rc.rid, f"fastwatch: flip storm ({flips['data']['flips']} status changes in "
                                    f"{int(FLIP_WINDOW // 60)} min) recorded as an incident"), run=rc.key)

    def _synthetic_end(self, rc: _Run, s: Dict[str, Any]) -> None:
        """An `end` row stamped with the moment the agent provably moved on (never 'now': that would fake activity)."""
        row = {"ts": s["closed_at"], "kind": "end", "agent": s["agent"], "tool": "Bash",
               "what": "fastwatch: closed (a later row of the same agent outlived it)", "synthetic": True}
        if s["aid"]:
            row["aid"] = s["aid"]
        path = rc.dir / "activity.jsonl"
        # the lock activity._rotate holds while it rewrites the file from a snapshot: an unlocked append in that window is lost
        with file_lock(path, timeout=1.0, on_timeout="proceed", shared=True):
            append_record(path, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))

    # -------------------------------------------------------------- incidents, notifications, resumes
    def _settle(self, rc: _Run, now: float, cfg: Dict[str, Any]) -> None:
        for f in rc.findings:
            if f.get("wake") and not self.breaker_open:
                self._maybe_resume(rc, f, now, cfg)

    def _reconcile(self, now: float, cfg: Dict[str, Any]) -> None:
        base = float((cfg.get("watchdog") or {}).get("renotify_minutes") or 30) * 60
        live = set()
        wanted: List[Tuple[str, str, str, float, Dict[str, Any]]] = []
        for rc in self.runs.values():
            for f in rc.findings:
                if f["level"] in ("notify", "freeze"):
                    wanted.append((f"{rc.key}|{f['kind']}", rc.name, rc.rid, now, f))
        hook = hooks_degraded(self.hook_events, now, self.hook_where)
        if hook:
            wanted.append(("hooks|hooks_degraded", "orkestra", "", now, hook))
        for key, project, rid, _, f in wanted:
            live.add(key)
            rec = self.incidents.get(key)
            if rec is None:
                rec = self.incidents[key] = {"first": now, "last": 0.0, "count": 0}
            if W.renotify_due(int(rec["count"]), float(rec["last"]), now, base):
                self._send(f["kind"], project, rid, self._text(f, project, now))
                rec["last"], rec["count"] = now, int(rec["count"]) + 1
        for key in [k for k in self.incidents if k not in live and not k.endswith("|breaker")]:
            self.incidents.pop(key, None)             # incident over: the next one notifies at once

    def _text(self, f: Dict[str, Any], project: str, now: float) -> str:
        d = f.get("data") or {}
        proof = ""
        if f["kind"] == "hung_command":
            proof = t("watchdog.fast_proof_cpu" if d.get("verified") else "watchdog.fast_proof_none")
        return t(KIND_KEYS[f["kind"]], project=project, count=d.get("count", d.get("errors", 0)),
                 what=str(d.get("what") or d.get("action") or ""), dur=A.human_duration(max(0.0, now - f["since"])),
                 proof=proof)

    def _send(self, kind: str, project: str, rid: str, text: str) -> None:
        audit("notify", kind=kind, project=project, run=rid, text=text[:300])
        payload = {"source": "orkestra-fastwatch", "project": project, "project_name": project, "run_id": rid,
                   "key": kind, "message": text, "next": ""}
        try:
            self.notifier("Orkestra", text, payload)
        except Exception as exc:
            audit("notify_failed", kind=kind, error=str(exc))

    def _maybe_resume(self, rc: _Run, f: Dict[str, Any], now: float, cfg: Dict[str, Any]) -> None:
        wd = T.wd_cfg(cfg)
        mode = str(wd.get("auto_resume") or "off")
        if mode == "off" or (mode == "headless" and not rc.st.get("mode") == "headless") or _IS_WINDOWS:
            return
        if not rc.sessions or rc.sessions[0]["age"] < FAST_MIN_AGE or rc.key in self.inflight:
            return
        book = read_json(W._state_path(), default={}) or {}
        rec = (book.get("resumes") or {}).get(rc.key) or {"count": 0, "last": 0}
        first = RETRY_DELAYS[0] if f["kind"] == "stop_failure" else 0.0
        if not resume_due(int(rec.get("count") or 0), float(rec.get("last") or 0), f["since"], now, first,
                          cap=int(wd.get("max_auto_resumes") or 3)):
            return
        ids = [s["id"] for s in rc.sessions]
        if self.bridge_owns(ids) or self.alive(ids, rec.get("pid")):
            return                                    # a live process / bridge job of this session: never a second driver
        attempt = int(rec.get("count") or 0) + 1
        session = rc.sessions[0]["id"]
        v = {"project": str(rc.project), "run_id": rc.rid, "mode": rc.st.get("mode"),
             "stall": {"text": t(KIND_KEYS[f["kind"]], project=rc.name, count=0, what="", dur="", proof="")}}
        if not self._book_attempt(rc.key, attempt, now):
            return
        self.inflight.add(rc.key)

        def go() -> None:
            try:
                res = self.spawner(v, cfg, session, attempt)
                if isinstance(res, str):
                    # The booked attempt stays: a refusal counts against max_auto_resumes and waits the ladder delay,
                    # otherwise every tick would retry (and a spawn that failed after Popen would start a second agent).
                    audit("resume_refused", run=rc.key, why=res)
                    return
                self._book_attempt(rc.key, attempt, now, pid=res)
                audit("resume", run=rc.key, attempt=attempt, pid=res, kind=f["kind"])
                self._send(f["kind"], rc.name, rc.rid, self._text(f, rc.name, now) + t("watchdog.auto_resumed", attempt=attempt))
            finally:
                self.inflight.discard(rc.key)

        if self.async_spawn:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()

    def _book_attempt(self, key: str, count: int, last: float, pid: Any = None) -> bool:
        """Record the attempt in the shared resume book (same cap as the one-shot watchdog) under a short lock."""
        try:
            with file_lock(W._state_path(), timeout=2):
                book = read_json(W._state_path(), default={}) or {}
                if not isinstance(book, dict):
                    book = {}
                resumes = book.setdefault("resumes", {})
                if count <= 0:
                    resumes.pop(key, None)
                else:
                    resumes[key] = {"count": count, "last": last, "pid": pid}
                write_json_atomic(W._state_path(), book)
            return True
        except LockTimeout:
            return False

    def _reap(self) -> None:
        for child in list(W._children.values()):
            try:
                child.poll()
            except Exception:
                pass

    # -------------------------------------------------------------- lifecycle
    def should_exit(self, now: float, idle_exit: Optional[float]) -> bool:
        if not idle_exit or self.runs:
            return False
        return self.last_busy is not None and now - self.last_busy >= idle_exit

    def run(self, stop: threading.Event, idle_exit: Optional[float] = None) -> None:
        audit("start", pid=os.getpid())
        try:
            while not stop.is_set():
                res = self.step()
                if self.should_exit(self.clock(), idle_exit):
                    audit("idle_exit")
                    break
                stop.wait(res.get("interval", TICK))
        finally:
            self._write_status(self.clock(), stopped=True)
            audit("stop", pid=os.getpid())

    # -------------------------------------------------------------- persistence
    def _load_incidents(self) -> None:
        data = read_json(_status_path())
        inc = (data or {}).get("incidents") if isinstance(data, dict) else None
        if isinstance(inc, dict):
            self.incidents = {k: v for k, v in inc.items() if isinstance(v, dict) and "count" in v}

    def _write_status(self, now: float, stopped: bool = False) -> None:
        runs = {}
        for rc in self.runs.values():
            flags = [f["kind"] for f in rc.findings]
            runs[rc.key] = {"flags": flags, "amber": any(f["level"] == "flag" for f in rc.findings),
                            "incident": any(f["level"] != "flag" for f in rc.findings)}
        try:
            write_json_atomic(_status_path(), {
                "version": 1, "pid": os.getpid(), "started": self.started, "ts": time.time(), "ticks": self.ticks,
                "health_checks": self.health_checks, "health_every": float(T.wd_cfg().get("health_check_seconds") or 120),
                "interval": self.interval, "breaker_open": self.breaker_open, "stopped": stopped,
                "open_runs": len(self.runs), "runs": runs, "incidents": self.incidents})
        except OSError:
            pass


def _handback_at(raw: Dict[str, Any]) -> float:
    hb = raw.get("last_handback")
    try:
        return float(hb.get("at") or 0) if isinstance(hb, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pid_exists(pid: int) -> bool:
    if _IS_WINDOWS:
        return True                                # cannot tell: "alive" is the answer that never claims a death
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _cpu_seconds(text: str) -> float:
    """ps `time` column: [[d-]hh:]mm:ss[.cc]"""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return days * 86400 + secs


def _default_probe(project: Path, st: Dict[str, Any]) -> Optional[float]:
    """Total CPU seconds of the processes that mention the project path and of their descendants (None = no evidence:
    Windows, no `ps`, or no such process). Read-only; it is called only for a hung-command candidate."""
    if _IS_WINDOWS:
        return None
    try:
        cp = subprocess.run(["ps", "-axo", "pid=,ppid=,time=,command="], capture_output=True, encoding="utf-8",
                            errors="replace", timeout=5)
    except Exception:
        return None
    procs: Dict[int, Tuple[int, float, str]] = {}
    for line in cp.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs[int(parts[0])] = (int(parts[1]), _cpu_seconds(parts[2]), parts[3])
        except ValueError:
            continue
    me = os.getpid()
    chosen = {pid for pid, (_, _, cmd) in procs.items() if str(project) in cmd and pid != me
              and not cmd.startswith("ps ")}
    grew = True
    while grew:
        grew = False
        for pid, (ppid, _, _) in procs.items():
            if pid not in chosen and ppid in chosen and pid != me:
                chosen.add(pid)
                grew = True
    if not chosen:
        return None
    return sum(procs[p][1] for p in chosen)


def turn_quiet(project: Path, since: float, now: float, idle_limit: float = 0.0) -> Tuple[float, bool]:
    """For the bridge's activity cap: (seconds since the newest sign of life of any open run of `project`, or `since`
    when there is none, and whether a Bash command is open). stat() only, plus one tail read past `idle_limit`."""
    newest = float(since)
    running = False
    rdir = paths.runs_dir(Path(project))
    try:
        entries = [e for e in os.scandir(rdir) if e.is_dir()]
    except OSError:
        entries = []
    for e in entries:
        st = read_json(Path(e.path) / "state.json")
        if not isinstance(st, dict) or st.get("status") not in ST.OPEN_STATUSES:
            continue
        for name in ("state.json", "activity.jsonl", "agents.json"):
            try:
                newest = max(newest, (Path(e.path) / name).stat().st_mtime)
            except OSError:
                pass
        if now - newest >= idle_limit > 0 and not running:
            running = bool(T._running_commands(A.read_rows(Path(project), e.name, max_bytes=64 * 1024)))
    return max(0.0, now - newest), running


# ===================================================================== entry points

def run_forever(idle_exit: Optional[float] = None, stop: Optional[threading.Event] = None) -> int:
    """Foreground loop (the detached daemon runs this): returns 0 at once when another watcher holds the singleton."""
    single = Singleton()
    if not single.acquire():
        return 0
    set_lock_policy("hook")                           # short lock budgets: a busy lock degrades, it never stalls the loop
    stop = stop or threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):
                pass
    try:
        Watcher(async_spawn=True).run(stop, idle_exit)
    finally:
        for sig, handler in previous.items():          # a caller that embeds the loop keeps its own Ctrl+C behaviour
            try:
                import signal
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        single.release()
    return 0


def start_thread(stop: threading.Event, notifier: Optional[Callable[[str, str, Dict[str, Any]], None]] = None
                 ) -> Optional[threading.Thread]:
    """Host the watcher inside this process (the bridge). None when another watcher holds the singleton, when
    ORKESTRA_NO_DAEMON=1 or on Windows."""
    if os.environ.get("ORKESTRA_NO_DAEMON") == "1" or _IS_WINDOWS:
        return None
    single = Singleton()
    if not single.acquire():
        return None

    def body() -> None:
        try:
            Watcher(notifier=notifier, async_spawn=True).run(stop, None)
        finally:
            single.release()

    th = threading.Thread(target=body, name="orkestra-fastwatch", daemon=True)
    th.start()
    return th


def read_status() -> Dict[str, Any]:
    data = read_json(_status_path())
    return data if isinstance(data, dict) else {}


def status() -> Dict[str, Any]:
    """Machine view for `orkestra watchdog --status [--json]` and doctor."""
    running = is_running()
    data = read_status() if running else {}
    return {"running": running, "pid": holder_pid() if running else None, "autostart": _autostart(),
            "supported": not _IS_WINDOWS and fcntl is not None,
            "disabled_by_env": os.environ.get("ORKESTRA_NO_DAEMON") == "1",
            "ts": data.get("ts"), "ticks": data.get("ticks"), "interval": data.get("interval"),
            "open_runs": data.get("open_runs"), "breaker_open": bool(data.get("breaker_open")),
            "runs": data.get("runs") or {}}


def owns_notifications(max_age: float = 30.0) -> bool:
    """Is a healthy fast watcher announcing dead waits and hung commands right now? (the one-shot watchdog then
    leaves those two classes to it, so a person is not told twice)"""
    try:
        if not is_running():
            return False
        data = read_status()
        return not data.get("breaker_open") and time.time() - float(data.get("ts") or 0) <= max_age
    except Exception:
        return False
