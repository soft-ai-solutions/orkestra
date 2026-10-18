"""Shared test helpers (stdlib unittest only — the suite must run on a bare macOS python3)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
LIB = PLUGIN / "lib"
BIN = PLUGIN / "bin" / "orkestra"
DISPATCH = PLUGIN / "hooks" / "dispatch.py"
CURSOR_HOOK = PLUGIN / "hooks" / "cursor_hook.py"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
# Human-facing text is localized (i18n.py). The suite must not depend on the developer's locale or config: it is pinned
# to Turkish, the language its legacy assertions were written in; tests of the English side set ORKESTRA_LANG=en.
os.environ["ORKESTRA_LANG"] = "tr"
# The suite never spawns the detached fast watcher (SessionStart calls fastwatch.ensure_running; this switch makes it a no-op).
os.environ["ORKESTRA_NO_DAEMON"] = "1"


REF_NOMINAL = 0.03      # CPU seconds the reference loop takes on an idle, current machine
REF_CAP = 6.0           # a real regression here is 10x and more: the allowance never grows enough to hide one


def _reference_cpu() -> float:
    t, x = time.process_time(), 0
    for i in range(300_000):
        x += i * i % 7
    return time.process_time() - t


def assert_cpu_under(test, seconds: float, fn, msg: str = ""):
    """Run fn() and require its CPU time under a budget — "this must stay linear" checks.

    Wall-clock limits failed on the machine they were written for: 2026-10-05, load average 100+, one of them
    measured 1.82 s against 1.5 s and stopped install.sh. CPU time ignores waiting for a core; what a busy machine
    still slows down (shared caches, throttling) is measured with a fixed reference loop and widens the budget, at
    most REF_CAP times."""
    before = _reference_cpu()
    t = time.process_time()
    result = fn()
    used = time.process_time() - t
    factor = min(REF_CAP, max(1.0, max(before, _reference_cpu()) / REF_NOMINAL))
    test.assertLess(used, seconds * factor, f"{msg} (cpu {used:.2f}s, budget {seconds}s x {factor:.1f})")
    return result


_AGENT_ENV = ("CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "CURSOR_INVOKED_AS", "CLAUDECODE")


def run_cursor_hook(event: str, payload_json: str, timeout: float = 60.0,
                    *, keep_agent_env: bool = False) -> subprocess.CompletedProcess:
    """Invoke cursor_hook.py as the Cursor host would: not under the agent shell tree.

    When the suite runs inside Cursor's agent shell, a plain subprocess still has an ancestor whose command
    contains `builtin eval \"$1\" < /dev/null`, so started_by_an_agent_command refuses GUARDED_ORIGIN events.
    Double-fork reparents the hook away from that tree (CI / a plain terminal needs no reparent; fork still works).

    keep_agent_env=True: leave CURSOR_AGENT / CLAUDECODE markers in the child env (tests for the
    hooks_carry_agent_env escape hatch). Default strips them so the suite looks like the real host.
    """
    env = {k: v for k, v in os.environ.items() if keep_agent_env or k not in _AGENT_ENV}
    out_path = Path(tempfile.mkdtemp(prefix="orkestra-chook-")) / "out.json"
    try:
        if os.name == "nt":
            spawn = (
                "flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)\n"
                "cp = subprocess.run([sys.executable, hook, event], input=payload,\n"
                "                    capture_output=True, text=True, env=env, creationflags=flags)\n"
                "body = (cp.stdout if cp.returncode == 0 and cp.stdout.strip() else "
                "('__ERR__\\n' + str(cp.returncode) + '\\n' + (cp.stderr or '') + '\\n' + (cp.stdout or '')))\n"
                "tmp = out + '.tmp'\n"
                "with open(tmp, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(body); fh.flush(); os.fsync(fh.fileno())\n"
                "os.replace(tmp, out)\n"
            )
        else:
            spawn = (
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    if os.fork() == 0:\n"
                "        cp = subprocess.run([sys.executable, hook, event], input=payload,\n"
                "                            capture_output=True, text=True, env=env)\n"
                "        body = (cp.stdout if cp.returncode == 0 and cp.stdout.strip() else "
                "('__ERR__\\n' + str(cp.returncode) + '\\n' + (cp.stderr or '') + '\\n' + (cp.stdout or '')))\n"
                "        tmp = out + '.tmp'\n"
                "        with open(tmp, 'w', encoding='utf-8') as fh:\n"
                "            fh.write(body); fh.flush(); os.fsync(fh.fileno())\n"
                "        os.replace(tmp, out)\n"
                "        os._exit(0)\n"
                "    os._exit(0)\n"
                "os.wait()\n"
            )
        script = (
            "import os, sys, subprocess\n"
            f"out = {str(out_path)!r}\n"
            f"event = {event!r}\n"
            f"hook = {str(CURSOR_HOOK)!r}\n"
            f"payload = {payload_json!r}\n"
            "env = dict(os.environ)\n"
            + spawn
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=timeout)
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            if out_path.exists():
                text = out_path.read_text(encoding="utf-8")
                if text.strip():
                    break
            time.sleep(0.02)
        if not text.strip():
            raise AssertionError(f"cursor hook {event}: no output file")
        if text.startswith("__ERR__\n"):
            parts = text.split("\n", 3)
            code = int(parts[1]) if len(parts) > 1 else 1
            err = parts[2] if len(parts) > 2 else ""
            out = parts[3] if len(parts) > 3 else ""
            return subprocess.CompletedProcess([str(CURSOR_HOOK), event], code, out, err)
        return subprocess.CompletedProcess([str(CURSOR_HOOK), event], 0, text, "")
    finally:
        try:
            out_path.unlink(missing_ok=True)
            out_path.parent.rmdir()
        except OSError:
            pass


def driver_prompt(engine: str, session_id: str, prompt: str, cwd: str):
    """engines.driver_prompt outside the agent process tree (the API itself refuses under an agent shell)."""
    env = {k: v for k, v in os.environ.items() if k not in _AGENT_ENV}
    out_path = Path(tempfile.mkdtemp(prefix="orkestra-drv-")) / "out.txt"
    try:
        if os.name == "nt":
            spawn = (
                "import subprocess\n"
                "flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)\n"
                "from orkestra_core import engines\n"
                f"r = engines.driver_prompt({engine!r}, {session_id!r}, {prompt!r}, {cwd!r})\n"
                "open(out, 'w', encoding='utf-8').write('' if r is None else ('\\0' + r))\n"
            )
        else:
            spawn = (
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    if os.fork() == 0:\n"
                "        from orkestra_core import engines\n"
                f"        r = engines.driver_prompt({engine!r}, {session_id!r}, {prompt!r}, {cwd!r})\n"
                "        open(out, 'w', encoding='utf-8').write('' if r is None else ('\\0' + r))\n"
                "        os._exit(0)\n"
                "    os._exit(0)\n"
                "os.wait()\n"
            )
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(LIB)!r})\n"
            f"out = {str(out_path)!r}\n"
            + spawn
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
        deadline = time.time() + 60
        while time.time() < deadline and not out_path.exists():
            time.sleep(0.02)
        if not out_path.exists():
            raise AssertionError("driver_prompt: no output")
        text = out_path.read_text(encoding="utf-8")
        if text == "":
            return None
        return text[1:] if text.startswith("\0") else text
    finally:
        try:
            out_path.unlink(missing_ok=True)
            out_path.parent.rmdir()
        except OSError:
            pass


class Sandbox(unittest.TestCase):
    """Isolated ORKESTRA_HOME + workspace + a git project with a tiny passing test gate."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-test-")).resolve()
        self.home = self.tmp / "home"
        self.ws = self.tmp / "Projects"
        self.ws.mkdir()
        # the suite may be started from inside an agent's shell (Claude Code / Cursor): the CLI must not see that
        agent_env = ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "ORKESTRA_CURSOR_HOME", "ORKESTRA_SHIM_DIR",
                     "ORKESTRA_CURSOR_BIN", "ORKESTRA_CLAUDE_BIN", "CLAUDE_CONFIG_DIR", "ORKESTRA_PI_HOME")
        self._env_backup = {k: os.environ.get(k) for k in ("ORKESTRA_HOME", "ORKESTRA_WORKSPACE", "ORKESTRA_PROJECT",
                                                           "ORKESTRA_HEADLESS", "ORKESTRA_PARALLEL_CLI") + agent_env}
        os.environ["ORKESTRA_PARALLEL_CLI"] = "0"          # unit tests must not spawn a real engine CLI
        os.environ["ORKESTRA_HOME"] = str(self.home)
        os.environ["ORKESTRA_WORKSPACE"] = str(self.ws)
        os.environ.pop("ORKESTRA_PROJECT", None)
        os.environ.pop("ORKESTRA_HEADLESS", None)
        for k in agent_env:
            os.environ.pop(k, None)
        os.environ["ORKESTRA_CURSOR_HOME"] = str(self.tmp / "cursor-home")      # never the user's ~/.cursor
        os.environ["ORKESTRA_PI_HOME"] = str(self.tmp / "pi-home")              # never the user's ~/.pi
        os.environ["ORKESTRA_SHIM_DIR"] = str(self.tmp / "shim-bin")

    def tearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ fixtures
    def make_project(self, name: str = "demo", git: bool = True) -> Path:
        p = self.ws / name
        p.mkdir()
        (p / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (p / "tests").mkdir()
        (p / "tests" / "test_calc.py").write_text(
            "import unittest, sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
            "from calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        (p / "Makefile").write_text("test:\n\tpython3 -m unittest discover -s tests -q\n")
        if git:
            self.git(p, "init", "-q")
            self.git(p, "config", "user.email", "t@example.com")
            self.git(p, "config", "user.name", "t")
            self.git(p, "add", "-A")
            self.git(p, "commit", "-q", "-m", "init")
        return p

    @staticmethod
    def git(p: Path, *args: str) -> str:
        cp = subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True)
        if cp.returncode != 0:
            raise AssertionError(f"git {args} failed: {cp.stderr}")
        return cp.stdout

    def cli(self, *args: str, cwd: Path = None, check: bool = True, env: dict = None) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        if env:
            e.update(env)
        cp = subprocess.run([sys.executable, str(BIN), *args], cwd=str(cwd or self.tmp), capture_output=True,
                            text=True, env=e)
        if check and cp.returncode != 0:
            raise AssertionError(f"orkestra {' '.join(args)} → {cp.returncode}\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}")
        return cp

    def hook(self, event: str, payload: dict) -> dict:
        payload = dict(payload)
        payload.setdefault("hook_event_name", event)
        cp = subprocess.run([sys.executable, str(DISPATCH), event], input=json.dumps(payload), capture_output=True,
                            text=True, env=dict(os.environ))
        if cp.returncode != 0:
            raise AssertionError(f"hook {event} exit {cp.returncode}: {cp.stderr}")
        return json.loads(cp.stdout) if cp.stdout.strip() else {}

    @staticmethod
    def denied(out: dict) -> bool:
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
