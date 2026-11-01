"""Filesystem layout: global home, project detection, feature/run directories."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .util import read_json, write_json_atomic

# ---------------------------------------------------------------- global home

DEFAULT_GLOBAL_CONFIG: Dict[str, Any] = {
    "workspace_root": "~/Projects",
    "lang": "auto",         # language of human-facing text: auto (OS locale, else English) | en | tr — see i18n.py
    "guards": {
        "git": True,        # block git/gh writes unless the user's current prompt asks for them
        "secrets": True,    # block reading/writing secret files (.env, *.p8, SA keys ...)
        "store": True,      # block EAS/expo.dev cloud build/submit/update
        "isolation": True,  # during a run: no writes outside the run's project
        "deploy": True,     # during a run: no deploy/publish/migrate-prod commands
        "deps": True,       # during a run: no new dependency unless approved in the plan
        "phase": True,      # during a run: no source edits before plan approval
        "wait": True,       # during a run: the main session must not sleep/poll for subagent results
        "dialogs": True,    # during a run: no command that opens a macOS system dialog on the user's screen
        "test_syntax": False,   # opt-in: read `[[ a > b ]]` / `(( n > 100 ))` as comparisons, not redirects (shellparse)
    },
    "tempo": {"stop_while_agents": True, "task_review_required": True, "max_dispatches": 3},   # M/L: `task done` needs a PASS review;   # the main turn may end while background subagents are still running
    # which agent drives a run started without an explicit choice (bridge, `orkestra run`): claude | cursor.
    # cursor.global_guards: outside a run, apply the every-session guards (git, secrets, store) to Cursor chats too.
    "engine": {"default": "claude", "claude": {"bin": "", "interactive_args": []},
                                    "cursor": {"bin": "", "model": "", "interactive_args": [], "global_guards": False,
                                               "hooks_carry_agent_env": False},
                                    "copilot": {"bin": "", "model": "gpt-6-luna", "reasoning_effort": "max",
                                                "context": "long_context", "allow_all": True,
                                                "plugin_dir": "", "max_autopilot_continues": 20,
                                                "interactive_args": []}},
    # stall detection (`orkestra watch`, `orkestra watchdog`); auto_resume: off | headless | all
    "watchdog": {"dead_wait_seconds": 90, "hung_command_seconds": 900, "gate_command_seconds": 1800,
                 "silent_seconds": 600, "user_wait_notify_seconds": 300, "loop_repeats": 3, "agent_max_minutes": 60,
                 "agent_silent_seconds": 150,   # a registered subagent with no sign of life this long is lost (hooks, next)
                 "lease_seconds": 120, "health_check_seconds": 120, "auto_resume": "headless",
                 "max_auto_resumes": 3, "renotify_minutes": 30,
                 "macos_notify": True,
                 "autostart": True,           # SessionStart may start the fast watcher (fastwatch.ensure_running); POSIX only
                 "daemon_idle_minutes": 30},  # the detached fast watcher exits after this long with no open run and no live session
    "secrets": {"extra_patterns": [], "allow": []},
    "gates_parallel": 2,    # >1: typecheck/lint/test gates run concurrently (<= min(N, cpus/2)); a project config.json value wins
    "gates_rerun_failed_baseline": False,   # True: `gate verify` re-runs gates that failed/timed out at baseline (same as --all)
    "notify": {"webhook_url": "", "token": ""},
    "headless": {"permission_mode": "auto", "max_turns": 400,
                 "max_budget_usd": 0},   # >0: passed to `claude -p --max-budget-usd` (0 = off, no spend ceiling)
    # bridge turns (bridge/orkestra_bridge.py): wall-clock budget of one headless turn, and how long a turn may produce
    # no run activity (no activity row, no state change, no running Bash row) before it is interrupted and resumed
    "bridge": {"turn_timeout_seconds": 7200, "turn_idle_seconds": 1800},
    # After phase → done: wipe regenerable tool dumps under the project (test-results, android/**/build, …).
    # Project `.orkestra/config.json` may set "cleanup_artifacts": false to skip for that repo.
    "cleanup_artifacts": True,
}

MARKERS = (
    "package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml",
    "pubspec.yaml", "composer.json", "Gemfile", "Package.swift", "build.gradle",
    "build.gradle.kts", "pom.xml", "CMakeLists.txt", "Makefile", "project.godot", "AGENTS.md",
)


def orkestra_home() -> Path:
    return Path(os.environ.get("ORKESTRA_HOME") or "~/.orkestra").expanduser()


def watchdog_pid_path() -> Path:
    """The singleton lock file of the fast watcher (fastwatch holds an exclusive flock on it while it runs)."""
    return orkestra_home() / "watchdog.pid"


def _same_kind(default: Any, value: Any) -> bool:
    """Is `value` usable where the default config holds `default`? (`engine: "x"` instead of an object, `"yes"` for a
    flag, ... fall back to the default instead of crashing a hook later. None and unknown keys are always accepted.)"""
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in out and not _same_kind(out[k], v):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def global_config() -> Dict[str, Any]:
    user = read_json(orkestra_home() / "config.json", default={}) or {}
    return _deep_merge(DEFAULT_GLOBAL_CONFIG, user if isinstance(user, dict) else {})


def ensure_global_config() -> Path:
    path = orkestra_home() / "config.json"
    if not path.exists():
        write_json_atomic(path, DEFAULT_GLOBAL_CONFIG)
    return path


def workspace_root() -> Path:
    env = os.environ.get("ORKESTRA_WORKSPACE")
    raw = env or global_config().get("workspace_root") or DEFAULT_GLOBAL_CONFIG["workspace_root"]
    return Path(os.path.expanduser(raw)).resolve(strict=False)


def display_path(path: os.PathLike | str) -> str:
    """Portable form of a path for human-facing text.

    Paths under the user's home become `~/…` so a dashboard, CLI message or log never prints
    `/Users/<name>/…` or `/home/<name>/…`. Paths outside the home stay absolute. Separators are
    forward slashes so the same string is readable on every OS.
    """
    raw = str(path or "").strip()
    if not raw:
        return raw
    if raw == "~" or raw.startswith("~/") or raw.startswith("~\\"):
        return raw.replace("\\", "/")
    try:
        abs_path = Path(os.path.expanduser(raw)).resolve(strict=False)
        home = Path.home().resolve(strict=False)
        rel = abs_path.relative_to(home)
    except (OSError, ValueError, RuntimeError):
        return raw.replace("\\", "/")
    parts = rel.as_posix()
    return "~" if parts in (".", "") else f"~/{parts}"


def workspace_display() -> str:
    """Configured workspace as people should see it (prefer `~/Projects`, never an expanded home path)."""
    env = os.environ.get("ORKESTRA_WORKSPACE")
    if env not in (None, ""):
        return display_path(env)
    raw = global_config().get("workspace_root") or DEFAULT_GLOBAL_CONFIG["workspace_root"]
    return display_path(str(raw))


# ---------------------------------------------------------------- projects

def _same_path(a: Path, b: Path) -> bool:
    """Equal paths, also on a case-insensitive file system (macOS default, Windows) where `Projects` and `projects`
    name one directory. Names that differ by more than case are never compared on disk (no stat in the common case)."""
    if a == b:
        return True
    if str(a).casefold() != str(b).casefold():
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def find_project_root(start: Optional[os.PathLike] = None) -> Optional[Path]:
    """Nearest ancestor that is a project.

    Priority: an ancestor containing `.orkestra/` > nearest `.git` > nearest marker file.
    The workspace root itself (~/Projects) is never a project.
    """
    start_path = Path(start or os.getcwd()).expanduser()
    try:
        start_path = start_path.resolve(strict=False)
    except Exception:  # pragma: no cover
        pass
    if start_path.is_file():
        start_path = start_path.parent
    ws = workspace_root()
    chain = [start_path] + list(start_path.parents)
    chain = [p for p in chain if not _same_path(p, ws) and p != Path(p.anchor)]
    home = Path.home().resolve(strict=False)
    chain = [p for p in chain if not _same_path(p, home)]

    for p in chain:
        if (p / ".orkestra").is_dir():
            return p
    for p in chain:
        if (p / ".git").exists():
            return p
    for p in chain:
        if any((p / m).exists() for m in MARKERS):
            return p
    return None


def project_name(project: Path) -> str:
    return Path(project).name


# ---------------------------------------------------------------- per-project layout

def okdir(project: Path) -> Path:
    return Path(project) / ".orkestra"


def memory_dir(project: Path) -> Path:
    return okdir(project) / "memory"


def features_dir(project: Path) -> Path:
    return okdir(project) / "features"


def feature_dir(project: Path, run_id: str) -> Path:
    return features_dir(project) / run_id


def runs_dir(project: Path) -> Path:
    return okdir(project) / "runs"


def run_dir(project: Path, run_id: str) -> Path:
    return runs_dir(project) / run_id


def project_config_path(project: Path) -> Path:
    return okdir(project) / "config.json"


def project_config(project: Path) -> Dict[str, Any]:
    data = read_json(project_config_path(project), default={}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("gates", [])
    return data


def save_project_config(project: Path, cfg: Dict[str, Any]) -> None:
    write_json_atomic(project_config_path(project), cfg)


# ---------------------------------------------------------------- global registries

def sessions_dir() -> Path:
    return orkestra_home() / "sessions"


def grants_dir() -> Path:
    return orkestra_home() / "grants"


def stops_dir() -> Path:
    return orkestra_home() / "stops"


def active_runs_path() -> Path:
    return orkestra_home() / "active-runs.json"


def logs_dir() -> Path:
    return orkestra_home() / "logs"


def is_headless(st: Optional[Dict[str, Any]] = None) -> bool:
    """A run driven by `claude -p`: started headless, OR continued by the bridge / watchdog (they set
    ORKESTRA_HEADLESS=1 for every process they start, also for a run that began in an interactive window).
    Such a process exits with its turn, so nothing may tell it to end the turn while subagents run."""
    return bool(st and st.get("mode") == "headless") or os.environ.get("ORKESTRA_HEADLESS") == "1"
