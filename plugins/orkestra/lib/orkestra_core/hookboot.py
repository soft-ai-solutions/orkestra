"""Start-up of the Orkestra hooks: what hooks/dispatch.py runs before (and instead of) the real hook code.

A hook process costs ~70 ms of CPU, of which only 0.2 to 7 ms is the hook's own work: the rest is the interpreter, compiling
the 1200-line entry script on every call (a `__main__` script is never cached) and importing modules the event does not
need. So the entry script is a stub and this module does the two things that make a hook cheap:

  1. `is_quiet`: events that provably do nothing (no binding file for the session, and no guard that applies to a
     session without a run) end here, after importing nothing beyond json, os, paths and state.
  2. The code of every other event lives in `orkestra_core/hookmain.py`, loaded as a cached .pyc and executed in the
     namespace of dispatch.py, so `import dispatch` still gives tests and the Cursor / Copilot adapters every name.

Read-only installs (no writable __pycache__) still work, only slower: set PYTHONPYCACHEPREFIX to a writable directory
(for example `~/.orkestra/pycache`) and Python keeps the bytecode there."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

# PreToolUse tools some global guard (secrets, home, git, store, ...) looks at without any run: never skipped.
GUARDED_PRE_TOOLS = frozenset(("Bash", "PowerShell", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep"))
# Events whose handler does nothing at all for a session that has no binding file (binding_for / session_binding /
# heal_binding all find nothing). PostToolUse of a Bash call is the exception handled in is_quiet: its output may
# carry a bind marker, which binds a session that has no binding yet.
NO_BINDING_NO_OP = frozenset(("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStop", "Notification",
                              "StopFailure", "SessionEnd"))


def utf8_stdio() -> None:
    """Hooks speak UTF-8 JSON whatever the console code page or locale is. Windows pipes default to cp125x and some
    POSIX locales to latin-1: a strict codec would turn a deny reason containing `→` into an exception, the hook
    would fail open and the call would pass unchecked. Streams that cannot be reconfigured (tests) are left alone."""
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def is_quiet(name: str, ev: Any, raw: str) -> bool:
    """True when running the hook can neither answer nor change anything, so it may exit at once.

    Only for a session WITHOUT a binding file. With one (or whenever anything is unusual) the answer is False and the
    full hook runs exactly as before. Guards stay as strict as ever: every tool a global guard looks at (shell, file
    tools, MCP tools, which may be edits or shells) is never skipped."""
    if name not in NO_BINDING_NO_OP or not isinstance(ev, dict):
        return False
    sid = ev.get("session_id") or ""
    if not isinstance(sid, str):
        return False
    tool = ev.get("tool_name") or ""
    if not isinstance(tool, str):
        return False
    if name == "PreToolUse" and (tool in GUARDED_PRE_TOOLS or tool.startswith("mcp__")):
        return False
    if name == "PostToolUse" and tool in ("Bash", "PowerShell") and "orkestra:" in raw:
        return False                                    # a `[orkestra:bind]` line may be in the output
    if sid:
        from . import paths, state
        try:
            if os.path.exists(paths.sessions_dir() / f"{state._safe(sid)}.json"):
                return False
        except (OSError, ValueError):
            return False
    return True


def load_main_code():
    """The code object of hookmain.py, from the bytecode cache when it is valid (compiled and cached otherwise)."""
    from importlib.machinery import SourceFileLoader
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hookmain.py")
    return SourceFileLoader("orkestra_core.hookmain", path).get_code("orkestra_core.hookmain")


def start(namespace: Dict[str, Any]) -> None:
    """Called by hooks/dispatch.py with its globals. Executed as a script: decide whether the event needs the hook at
    all, and if so run it and exit with its code. Imported (tests, adapters): only load the hook code into the
    namespace."""
    run = namespace.get("__name__") == "__main__"
    raw: Optional[str] = None
    if run:
        utf8_stdio()
        try:
            raw = sys.stdin.read()
            name = sys.argv[1] if len(sys.argv) > 1 else ""
            ev = json.loads(raw) if raw.strip() else {}
            if isinstance(ev, dict):
                if ev.get("cursor_version"):
                    sys.exit(0)                         # Cursor's copy of this hook: hooks/cursor_hook.py answers it
                name = name or ev.get("hook_event_name") or ""
            if is_quiet(name, ev, raw):
                sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            pass                                        # anything odd: the full hook decides (and fails open itself)
    exec(load_main_code(), namespace)
    if run:
        sys.exit(namespace["main"](raw))
