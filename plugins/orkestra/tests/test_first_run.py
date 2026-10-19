"""The first five minutes of a brand-new user, end to end, on a machine where orkestra was never run.

Every scenario drives the real CLI (and the real hook dispatcher) in a subprocess against a sandbox: an empty or missing
ORKESTRA_HOME, a workspace folder that does not exist, a project OUTSIDE any workspace root, no agent installed. The
invariants checked on every call: no traceback ever, an exit code of 0, 1 or 2 (the documented ones), and, for the
failures, a message that names what is wrong and what to do. After a whole session nothing exists outside the project's
`.orkestra/` and ORKESTRA_HOME.

The sandbox also replaces HOME, USERPROFILE, COPILOT_HOME and CLAUDE_CONFIG_DIR, so nothing here can read or write the
real user's agent configuration, and puts a stub `claude` first on PATH so `orkestra doctor` never starts the real one.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers

TRACEBACK = "Traceback (most recent call last)"
GIT = shutil.which("git")
AGENT_ENV = ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "CURSOR_INVOKED_AS", "ORKESTRA_PROJECT",
             "ORKESTRA_HEADLESS", "ORKESTRA_APPROVAL_MODE", "ORKESTRA_CURSOR_BIN", "ORKESTRA_CLAUDE_BIN",
             "ORKESTRA_COPILOT_BIN", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
GATE_CODE = "import sys; sys.exit(0)"
UNICODE_NAME = "Müşteri Portalı (v2) ✓"            # spaces, brackets, Turkish letters and a symbol
UNICODE_PARENT = "ünï côde dir"


def gate_command() -> str:
    """A gate that passes on any machine: the interpreter running the suite, quoted for the platform's shell."""
    argv = [sys.executable, "-c", GATE_CODE]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def write_claude_stub(directory: Path) -> None:
    """A `claude` that answers every question with nothing: doctor finds a CLI without starting the real one."""
    if os.name == "nt":
        (directory / "claude.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
        return
    stub = directory / "claude"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)


def can_deny_writes() -> bool:
    """True when chmod really makes a folder read-only for this process (POSIX, not root, honouring file system)."""
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        return False
    probe = Path(tempfile.mkdtemp(prefix="orkestra-ro-probe-"))
    try:
        probe.chmod(0o500)
        try:
            (probe / "x").write_text("x")
            return False
        except OSError:
            return True
    finally:
        probe.chmod(0o700)
        shutil.rmtree(probe, ignore_errors=True)


def interpreters():
    """[(major.minor, path)] of every distinct Python >= 3.9 this machine offers, the running one first."""
    names = [sys.executable] + ["python3.%d" % minor for minor in range(9, 16)] + ["python3", "python"]
    seen, found = set(), []
    for name in names:
        path = name if os.path.isabs(name) else shutil.which(name)
        if not path:
            continue
        try:
            cp = subprocess.run([path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        version = cp.stdout.strip()
        if cp.returncode != 0 or not re.match(r"^\d+\.\d+$", version):
            continue
        if tuple(int(x) for x in version.split(".")) < (3, 9) or version in seen:
            continue
        seen.add(version)
        found.append((version, path))
    return found


class Result:
    """One orkestra call: argv, exit code, stdout, stderr."""

    def __init__(self, args, completed):
        self.args = list(args)
        self.rc = completed.returncode
        self.out = completed.stdout
        self.err = completed.stderr

    @property
    def text(self) -> str:
        return self.out + self.err

    def __str__(self) -> str:
        return "orkestra %s -> exit %s\nSTDOUT:\n%s\nSTDERR:\n%s" % (" ".join(self.args), self.rc, self.out, self.err)


class FreshMachine(helpers.Sandbox):
    """A machine on which orkestra never ran. ORKESTRA_HOME does not exist yet, the workspace root does not exist at
    all, and the user's own home folder, Copilot home and Claude config are empty folders inside the sandbox."""

    def setUp(self):
        super().setUp()
        self.userhome = self.mkdir("userhome")
        self.work = self.mkdir("work")                          # projects live here: not under any workspace root
        self.stub = self.mkdir("stub-bin")
        self.cwd = self.mkdir("cwd")                            # where a command runs when no project is meant
        write_claude_stub(self.stub)
        self.nowhere = self.tmp / "no-such-workspace"
        self.checkpoint()

    # ------------------------------------------------------------------ environment
    def mkdir(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        return path

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in AGENT_ENV}
        env.update(
            HOME=str(self.userhome), USERPROFILE=str(self.userhome), ORKESTRA_HOME=str(self.home),
            ORKESTRA_WORKSPACE=str(self.nowhere), ORKESTRA_CURSOR_HOME=str(self.tmp / "cursor-home"),
            ORKESTRA_SHIM_DIR=str(self.tmp / "shim-bin"), COPILOT_HOME=str(self.tmp / "copilot-home"),
            CLAUDE_CONFIG_DIR=str(self.tmp / "claude-config"), ORKESTRA_LANG="en", PYTHONDONTWRITEBYTECODE="1",
            NO_COLOR="1", GIT_CONFIG_NOSYSTEM="1", PATH=os.pathsep.join([str(self.stub), env.get("PATH", "")]))
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def orc(self, *args, cwd=None, python=None, expect=None, typed=None, **env) -> Result:
        """The CLI as a user runs it (`typed`: what the user types at its prompts). The invariants of every call are
        checked here."""
        interpreter = [python] if isinstance(python, str) else list(python or [sys.executable])
        argv = interpreter + [str(helpers.BIN)] + list(args)
        cp = subprocess.run(argv, cwd=str(cwd or self.cwd), env=self.env(**env), capture_output=True, encoding="utf-8",
                            errors="replace", input=typed, stdin=None if typed is not None else subprocess.DEVNULL,
                            timeout=180)
        res = Result(args, cp)
        self.assertNotIn(TRACEBACK, res.err, str(res))
        self.assertNotIn("Exception ignored", res.err, str(res))
        self.assertIn(res.rc, (0, 1, 2), "exit codes are 0, 1 or 2\n" + str(res))
        if expect is not None:
            self.assertEqual(res.rc, expect, str(res))
        return res

    def refused(self, *args, cwd=None, mention=(), **env) -> Result:
        """A call that must be refused with exit 2 and an explanation on stderr that holds every `mention`."""
        res = self.orc(*args, cwd=cwd, expect=2, **env)
        self.assertTrue(res.err.strip(), "a refusal must say why\n" + str(res))
        low = res.err.lower()
        for needle in mention:
            self.assertIn(needle.lower(), low, "the message should mention %r\n%s" % (needle, res))
        return res

    def hook(self, event, payload, **env):
        payload = dict(payload, hook_event_name=event)
        cp = subprocess.run([sys.executable, str(helpers.DISPATCH), event], input=json.dumps(payload),
                            cwd=str(self.cwd), env=self.env(**env), capture_output=True, encoding="utf-8",
                            errors="replace", timeout=180)
        self.assertEqual(cp.returncode, 0, "a hook fails open: %s\n%s" % (cp.returncode, cp.stderr))
        self.assertNotIn(TRACEBACK, cp.stderr)
        return json.loads(cp.stdout) if cp.stdout.strip() else {}

    # ------------------------------------------------------------------ fixtures
    def git_in(self, project: Path, *args: str) -> str:
        cp = subprocess.run(["git", "-C", str(project), *args], env=self.env(), capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, "git %s: %s" % (args, cp.stderr))
        return cp.stdout

    def project(self, name="demo", *, git=True, gate=True, parent=None) -> Path:
        """A small project: source, a test, and (gate=True) a Makefile that makes orkestra detect a test gate."""
        path = (parent or self.work) / name
        path.mkdir(parents=True)
        (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        if gate:
            (path / "Makefile").write_text("test:\n\tpython3 -m unittest discover -s tests -q\n", encoding="utf-8")
        if git:
            self.git_in(path, "init", "-q")
            self.git_in(path, "config", "user.email", "t@example.com")
            self.git_in(path, "config", "user.name", "t")
            self.git_in(path, "config", "commit.gpgsign", "false")
            self.git_in(path, "add", "-A")
            self.git_in(path, "commit", "-q", "-m", "init")
        self.checkpoint()
        return path

    def start(self, project: Path, title="First change", request="add a subtract function", **env) -> str:
        res = self.orc("start", "--title", title, "--request", request, cwd=project, expect=0, **env)
        found = re.search(r"\[orkestra:bind\] project=(.+?) run=(\S+)", res.out)
        self.assertIsNotNone(found, str(res))
        return found.group(2)

    def feature(self, project: Path, run_id: str) -> Path:
        return project / ".orkestra" / "features" / run_id

    def run_state(self, project: Path, run_id: str) -> Path:
        return project / ".orkestra" / "runs" / run_id / "state.json"

    # ------------------------------------------------------------------ "nothing outside the project and the home"
    def world(self):
        """{relative path: (kind, size, mtime)} of everything in the sandbox except ORKESTRA_HOME and the projects'
        own `.orkestra` and `.git` folders: those are the only places orkestra may write."""
        snap = {}
        for dirpath, dirs, files in os.walk(self.tmp):
            here = Path(dirpath)
            dirs[:] = sorted(d for d in dirs if d not in (".git", ".orkestra") and here / d != self.home)
            for name in dirs + sorted(files):
                full = here / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                kind = "d" if full.is_dir() and not full.is_symlink() else "f"
                snap[full.relative_to(self.tmp).as_posix()] = (kind, st.st_size if kind == "f" else 0,
                                                              st.st_mtime_ns if kind == "f" else 0)
        return snap

    def checkpoint(self) -> None:
        self.before = self.world()

    def assert_nothing_outside(self) -> None:
        after = self.world()
        changed = sorted(set(after) ^ set(self.before)) + sorted(k for k in after if k in self.before
                                                                 and after[k] != self.before[k])
        self.assertEqual(changed, [], "orkestra wrote outside the project's .orkestra and ORKESTRA_HOME")


@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestFirstSession(FreshMachine):
    """`doctor`, `init`, `start`, `status`, `next`, `context`, templates, `pause`, `resume`, `report`, `abort`."""

    def first_session(self, project: Path, *, marked: bool) -> None:
        """`marked`: the folder has a .git or a project file, so orkestra finds it from inside (else `-p .` is needed)."""
        here = ("-p", ".") if not marked else ()
        self.assertFalse(self.home.exists(), "a brand-new machine has no ORKESTRA_HOME")
        self.assertFalse(self.nowhere.exists())

        # nothing is set up yet and the tool still introduces itself
        self.assertRegex(self.orc("--version", cwd=project, expect=0).out, r"^orkestra \d+\.\d+")
        self.assertIn("usage", self.orc("--help", cwd=project, expect=0).out.lower())

        doctor = self.orc("doctor", cwd=project, expect=0)
        for needle in ("python", "git", "orkestra home", "language", "all required checks passed"):
            self.assertIn(needle, doctor.out)
        self.assertTrue((self.home / "config.json").is_file(), "doctor creates the global config on first use")

        # no project here: the answer says how to point at one
        ctx = self.orc("context", cwd=self.cwd, expect=0)
        self.assertIn("no project resolved", ctx.out)
        self.assertIn("-p", ctx.out)
        self.refused("status", cwd=self.cwd, mention=("not inside a project", "--project"))

        # a project that was never initialised
        if marked:
            self.refused("status", cwd=project, mention=("no run in", "orkestra run"))
            self.refused("next", cwd=project, mention=("no run in", "orkestra run"))
            self.assertIn("NOT INITIALIZED", self.orc("context", cwd=project, expect=0).out)
        else:
            self.refused("status", cwd=project, mention=("not inside a project", "orkestra init -p ."))

        init = self.orc(*here, "init", cwd=project, expect=0)
        self.assertIn("initialized", init.out)
        self.assertIn("make-test" if (project / "Makefile").exists() else "detected gates: none", init.out)
        self.assertIn("already initialized", self.orc("init", cwd=project, expect=0).out)
        self.assertTrue((project / ".orkestra" / "memory" / "PROJECT.md").is_file())

        # the user's own gate (any machine can run the interpreter that runs the suite)
        if (project / "Makefile").exists():
            cfg_path = project / ".orkestra" / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["gates"] = [{"name": "unit", "cmd": gate_command(), "kind": "test", "timeout": 120, "cwd": "."}]
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertIn("unit", self.orc("gate", "list", cwd=project, expect=0).out)

        title, request = "First change", "add a subtract function"
        rid = self.start(project, title=title, request=request)
        request_md = (self.feature(project, rid) / "request.md").read_text(encoding="utf-8")
        self.assertIn("## Raw request", request_md)
        self.assertIn(request, request_md)
        self.refused("start", "--title", "Second", "--request", "x", cwd=project,
                     mention=("already has an open run", "orkestra resume", "orkestra abort"))

        status = self.orc("status", cwd=project, expect=0)
        self.assertIn(rid, status.out)
        self.assertIn(title, status.out)
        self.assertIn("phase intake", status.out)
        self.assertIn("next [intake]", self.orc("next", cwd=project, expect=0).out)
        self.assertIn("active run: " + rid, self.orc("context", cwd=project, expect=0).out)
        self.assertEqual(json.loads(self.orc("status", "--json", cwd=project, expect=0).out)["run_id"], rid)

        # templates
        for name, marker in (("spec", "## Purpose and value"), ("plan", "## Tasks"), ("report", "## Evidence"),
                             ("triage", "verdict")):
            self.assertIn(marker, self.orc("template", name, cwd=project, expect=0).out, name)
        self.refused("template", "no-such-template", cwd=project, mention=("no template", "available", "spec.md"))
        self.refused("brief", "T001", cwd=project, mention=("T001", "plan.md"))

        # the gate and the first phase change, with the precondition spelled out
        self.orc("phase", "next", cwd=project, expect=0)
        # intentional change: the baseline precondition moved from clarify to implement (it may run in the background)
        self.refused("phase", "next", cwd=project, mention=("context.md",))
        baseline = self.orc("gate", "baseline", cwd=project, expect=0)
        self.assertTrue("PASS" in baseline.out or "no gates configured" in baseline.out, str(baseline))
        (self.feature(project, rid) / "context.md").write_text("# Context\n" + "calc.py defines add(). " * 20,
                                                                encoding="utf-8")
        self.assertIn("discover → clarify", self.orc("phase", "next", cwd=project, expect=0).out)

        # the report block exists from the first minute
        self.assertIn("Machine evidence", self.orc("report", "evidence", cwd=project, expect=0).out)

        # pause, resume, abort
        paused = self.orc("pause", "lunch", cwd=project, expect=0)
        self.assertIn("paused", paused.out)
        self.assertIn("[orkestra:unbind]", paused.out)
        self.assertIn("status paused", self.orc("status", cwd=project, expect=0).out)
        self.assertIn("resume", self.orc("next", cwd=project, expect=0).out)
        resumed = self.orc("resume", cwd=project, expect=0)
        self.assertIn("[orkestra:bind]", resumed.out)
        self.assertIn("status active", self.orc("status", cwd=project, expect=0).out)
        aborted = self.orc("abort", "changed my mind", cwd=project, expect=0)
        self.assertIn("aborted", aborted.out)
        self.refused("status", cwd=project, mention=("no run in",))
        self.assertIn("status aborted", self.orc("status", "--run", rid, cwd=project, expect=0).out)

        # and a fresh run can follow on the same project
        second = self.start(project, title="Second try")
        self.assertNotEqual(second, rid)
        self.assertTrue(second.startswith("002-"), second)

        self.assert_nothing_outside()
        if (project / ".git").exists():
            porcelain = [ln for ln in self.git_in(project, "status", "--porcelain").splitlines() if ln.strip()]
            self.assertEqual(porcelain, ["?? .orkestra/"], "orkestra leaves the working tree alone")

    def test_git_project_with_a_test_gate(self):
        self.first_session(self.project("shop", git=True, gate=True), marked=True)

    def test_git_project_without_a_gate(self):
        self.first_session(self.project("notes", git=True, gate=False), marked=True)

    def test_plain_folder_with_a_gate_and_no_git(self):
        self.first_session(self.project("scripts", git=False, gate=True), marked=True)

    def test_plain_folder_with_nothing_to_recognise(self):
        """No .git, no project file: orkestra cannot guess the folder, and says which command marks it."""
        self.first_session(self.project("sketch", git=False, gate=False), marked=False)

    def test_path_with_spaces_and_non_ascii_letters(self):
        try:
            parent = self.work / UNICODE_PARENT
            parent.mkdir()
            project = self.project(UNICODE_NAME, git=True, gate=True, parent=parent)
        except (OSError, UnicodeError) as exc:
            self.skipTest("this file system cannot hold the fixture path: %s" % exc)
        self.orc("init", cwd=project, expect=0)
        title = "Çok güzel özellik ✓"                      # Çok güzel özellik ✓
        rid = self.start(project, title=title, request="türkçe karakterler: ğüşiöç")
        self.assertIn(title, self.orc("status", cwd=project, expect=0).out)
        self.assertIn(UNICODE_NAME, self.orc("context", cwd=project, expect=0).out)
        # the hook that binds the session reads the path back out of the marker line
        marker = self.orc("resume", cwd=project, expect=0).out
        self.hook("PostToolUse", {"session_id": "s-unicode", "tool_name": "Bash", "cwd": str(project),
                                  "tool_input": {"command": "orkestra resume"},
                                  "tool_response": {"stdout": marker, "stderr": ""}})
        bound = json.loads((self.home / "sessions" / "s-unicode.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(bound["project"]), project)
        self.assertEqual(bound["run_id"], rid)
        self.hook("UserPromptSubmit", {"session_id": "s-unicode", "prompt": "devam et", "cwd": str(project)})
        # the guards compare paths with spaces and non-ASCII letters correctly: a source edit before approval is refused,
        # a write outside the project during the run is refused too
        edit = self.hook("PreToolUse", {"session_id": "s-unicode", "tool_name": "Edit", "cwd": str(project),
                                        "tool_input": {"file_path": str(project / "calc.py")}})
        self.assertEqual(edit["hookSpecificOutput"]["permissionDecision"], "deny")
        # (only decided here, never written: the target is a file name in the real home folder, which is not a temp dir)
        outside = self.hook("PreToolUse", {"session_id": "s-unicode", "tool_name": "Write", "cwd": str(project),
                                           "tool_input": {"file_path": str(Path.home() / "orkestra-isolation-probe.txt")}})
        self.assertEqual(outside["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("isolation", outside["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("phase intake", self.orc("status", cwd=project, expect=0).out)
        self.orc("pause", "güle güle", cwd=project, expect=0)
        self.orc("resume", cwd=project, expect=0)
        self.orc("abort", "done here", cwd=project, expect=0)
        self.assert_nothing_outside()

    def test_an_existing_but_empty_orkestra_home_is_the_same_machine(self):
        self.home.mkdir()
        project = self.project("shop")
        self.orc("doctor", cwd=project, expect=0)
        self.orc("init", cwd=project, expect=0)
        self.start(project)
        self.assert_nothing_outside()

    def test_a_run_driven_by_a_name_from_another_folder(self):
        """`-p <path>` and ORKESTRA_PROJECT point at a project from anywhere (an agent often sits one level up)."""
        project = self.project("shop")
        self.orc("init", "-p", str(project), cwd=self.cwd, expect=0)
        rid = self.start(project)
        self.assertIn(rid, self.orc("status", "-p", str(project), cwd=self.cwd, expect=0).out)
        self.assertIn(rid, self.orc("status", cwd=self.cwd, expect=0, ORKESTRA_PROJECT=project).out)
        self.refused("status", "-p", str(self.work / "missing"), cwd=self.cwd, mention=("not found",))
        self.assert_nothing_outside()

    def test_run_print_shows_the_command_without_starting_anything(self):
        project = self.project("shop")
        res = self.orc("run", "--print", "add a subtract function", cwd=project, expect=0)
        self.assertIn("cd ", res.out)
        self.assertIn("/orkestra:run", res.out)
        self.assertFalse((project / ".orkestra").exists(), "--print must not touch the project")

    def test_run_opens_a_tracked_run_before_the_engine_ui(self):
        """Even advisory asks must appear on the dashboard: `orkestra run` opens the run before execve."""
        project = self.project("shop")
        self.orc("init", cwd=project, expect=0)
        # Stub `claude` on PATH is exec'd (exits 0); the tracked run must already exist.
        res = self.orc("run", "explain how the dashboard tracks runs", cwd=project, expect=0)
        self.assertIn("run opened for tracking", res.out)
        runs = list((project / ".orkestra" / "runs").glob("*/state.json"))
        self.assertEqual(len(runs), 1, runs)
        st = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "active")
        self.assertEqual(st["phase"], "intake")
        self.assertIn("explain", st["title"].lower())
        self.assertEqual(st.get("size"), "XS")

    def test_playbooks_are_readable_before_any_run(self):
        project = self.project("shop")
        self.assertIn("orkestra", self.orc("playbook", "run", cwd=project, expect=0).out.lower())
        self.assertIn("phase", self.orc("playbook", "intake", cwd=project, expect=0).out.lower())
        self.refused("playbook", cwd=project, mention=("no open run", "run"))


@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestHooksOnAFreshMachine(FreshMachine):
    """The guards work from the first prompt, before any run exists, and never break the agent's session."""

    EVENTS = (
        ("SessionStart", {}),
        ("UserPromptSubmit", {"prompt": "hello there"}),
        ("UserPromptSubmit", {"prompt": "merhaba, bir sey sorabilir miyim?"}),
        ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}),
        ("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": "notes.txt"}}),
        ("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {"stdout": "", "stderr": ""}}),
        ("Stop", {}),
        ("SubagentStop", {}),
    )

    def test_every_event_answers_and_writes_only_to_the_home(self):
        project = self.project("shop")
        for event, extra in self.EVENTS:
            payload = dict(extra, session_id="s-fresh", cwd=str(project))
            if "file_path" in payload.get("tool_input", {}):
                payload["tool_input"] = {"file_path": str(project / "notes.txt")}
            out = self.hook(event, payload)
            self.assertIsInstance(out, dict, event)
            self.assertNotEqual((out.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny",
                                "%s: nothing here is forbidden: %s" % (event, out))
        self.assert_nothing_outside()

    def test_a_forbidden_command_is_denied_even_before_any_setup(self):
        project = self.project("shop")
        forbidden = "git push " + "--force origin main"
        out = self.hook("PreToolUse", {"session_id": "s-fresh", "tool_name": "Bash", "cwd": str(project),
                                       "tool_input": {"command": forbidden}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(out["hookSpecificOutput"]["permissionDecisionReason"].strip())

    def test_hooks_fail_open_when_the_home_is_unusable_but_still_deny(self):
        self.home.write_text("a file where the folder should be", encoding="utf-8")
        project = self.project("shop")
        for event, extra in self.EVENTS:
            self.hook(event, dict(extra, session_id="s-broken", cwd=str(project)))
        forbidden = "git push " + "--force origin main"
        out = self.hook("PreToolUse", {"session_id": "s-broken", "tool_name": "Bash", "cwd": str(project),
                                       "tool_input": {"command": forbidden}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_run_binds_through_the_hooks_with_the_home_created_on_demand(self):
        project = self.project("shop")
        self.orc("init", cwd=project, expect=0)
        rid = self.start(project)
        started = self.orc("resume", cwd=project, expect=0).out
        self.hook("PostToolUse", {"session_id": "s-bind", "tool_name": "Bash", "cwd": str(project),
                                  "tool_input": {"command": "orkestra resume"},
                                  "tool_response": {"stdout": started, "stderr": ""}})
        bound = json.loads((self.home / "sessions" / "s-bind.json").read_text(encoding="utf-8"))
        self.assertEqual(bound["run_id"], rid)
        # while a run is open and still before approval, a source edit is refused with the way forward
        out = self.hook("PreToolUse", {"session_id": "s-bind", "tool_name": "Edit", "cwd": str(project),
                                       "tool_input": {"file_path": str(project / "calc.py")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("orkestra", out["hookSpecificOutput"]["permissionDecisionReason"])



@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestEnvironmentProblems(FreshMachine):
    """A folder that cannot be written, a home that is a file, no git: the message says what and where."""

    START = ("start", "--title", "t", "--request", "r")

    def assert_home_refused(self, project: Path) -> None:
        doctor = self.orc("doctor", cwd=project, expect=1)
        self.assertRegex(doctor.out, r"\[!!\] orkestra home")
        self.assertIn(str(self.home), doctor.out)
        self.assertIn("hook self-test", doctor.out, "the rest of the report still runs")
        self.assertIn("some required checks FAILED", doctor.out)
        self.refused(*self.START, cwd=project, mention=("ORKESTRA_HOME", str(self.home)))
        self.assertFalse((project / ".orkestra").exists(), "a refused start leaves the project untouched")
        self.assertIn("language: en", self.orc("lang", cwd=project, expect=0).out)
        self.refused("lang", "tr", cwd=project, mention=("config.json",))
        self.assertIn("usage", self.orc("--help", cwd=project, expect=0).out.lower())

    def test_orkestra_home_that_is_a_file(self):
        self.home.write_text("not a folder", encoding="utf-8")
        self.assert_home_refused(self.project("shop"))

    @unittest.skipUnless(can_deny_writes(), "needs chmod to deny writes (POSIX, not root)")
    def test_orkestra_home_without_write_permission(self):
        self.home.mkdir()
        self.home.chmod(0o500)
        try:
            self.assert_home_refused(self.project("shop"))
        finally:
            self.home.chmod(0o700)

    @unittest.skipUnless(can_deny_writes(), "needs chmod to deny writes (POSIX, not root)")
    def test_read_only_project(self):
        project = self.project("shop")
        project.chmod(0o500)
        try:
            self.refused("init", cwd=project, mention=(str(project),))
            self.refused(*self.START, cwd=project, mention=(str(project),))
            self.assertIn("NOT INITIALIZED", self.orc("context", cwd=project, expect=0).out)
            self.refused("status", cwd=project, mention=("no run in",))
        finally:
            project.chmod(0o700)

    def test_dot_orkestra_that_is_a_file(self):
        project = self.project("shop")
        (project / ".orkestra").write_text("someone's note", encoding="utf-8")
        self.refused("init", cwd=project, mention=(".orkestra",))
        self.refused(*self.START, cwd=project, mention=(".orkestra",))
        self.assertEqual((project / ".orkestra").read_text(encoding="utf-8"), "someone's note")

    def test_a_machine_without_git_still_works(self):
        """Fresh Windows machines often have no git yet: doctor says so, everything else keeps working."""
        project = self.project("shop", git=True, gate=True)
        nothing = self.mkdir("empty-bin")
        doctor = self.orc("doctor", cwd=project, expect=1, PATH=nothing)
        self.assertRegex(doctor.out, r"\[!!\] git")
        self.orc("init", cwd=project, expect=0, PATH=nothing)
        started = self.orc(*self.START, cwd=project, expect=0, PATH=nothing)
        self.assertIn("[orkestra:bind]", started.out)
        self.orc("status", cwd=project, expect=0, PATH=nothing)
        self.orc("changes", cwd=project, expect=0, PATH=nothing)
        self.orc("pause", "x", cwd=project, expect=0, PATH=nothing)

    def test_start_is_enough_on_its_own(self):
        """A new user may never run `init`: `start` creates the project memory the first time."""
        project = self.project("shop", git=False, gate=False)
        (project / "README.md").write_text("hi\n", encoding="utf-8")           # no marker file: name the project
        self.orc("start", "-p", ".", "--title", "t", "--request", "r", cwd=project, expect=0)
        self.assertTrue((project / ".orkestra" / "memory" / "PROJECT.md").is_file())
        self.assertTrue((self.home / "active-runs.json").is_file())


@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestDamagedFiles(FreshMachine):
    """Hand-edited or half-written files: every command says what is wrong; none of them crashes."""

    BAD_GLOBAL = {
        "not json": b"{this is not json",
        "a list": b"[]",
        "a string": b'"tr"',
        "null": b"null",
        "binary": b"\xff\xfe\x00\x01 garbage",
    }
    WRONG_TYPES = {
        "engine is a string": {"engine": "claude"},
        "guards is a list": {"guards": ["git"]},
        "watchdog is a number": {"watchdog": 5},
        "lang is a number": {"lang": 5},
    }

    def write_global(self, content) -> Path:
        self.home.mkdir(exist_ok=True)
        path = self.home / "config.json"
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
        return path

    def test_unusable_global_config_is_reported_by_doctor_and_defaults_apply(self):
        for label, content in self.BAD_GLOBAL.items():
            with self.subTest(label):
                project = self.project("shop-" + re.sub(r"\W", "", label))
                path = self.write_global(content)
                doctor = self.orc("doctor", cwd=project, expect=1)
                self.assertRegex(doctor.out, r"\[!!\] config\.json", doctor.out)
                self.assertIn(str(path), doctor.out)
                self.assertIn("JSON", doctor.out)
                self.assertIn("hook self-test", doctor.out, "the rest of the report still runs")
                # nothing else depends on it: the defaults apply and the work goes on
                self.orc("init", cwd=project, expect=0)
                rid = self.start(project)
                self.assertIn(rid, self.orc("status", cwd=project, expect=0).out)
                self.orc("context", cwd=project, expect=0)
                self.orc("watch", "--once", "-p", str(project), cwd=project, expect=0)
                self.orc("abort", "next case", cwd=project, expect=0)

    def test_an_empty_or_bom_prefixed_global_config_is_fine(self):
        project = self.project("shop")
        for content in (b"", b"  \n", b"\xef\xbb\xbf{}", b"\xef\xbb\xbf" + json.dumps({"lang": "en"}).encode("utf-8")):
            self.write_global(content)
            doctor = self.orc("doctor", cwd=project, expect=0)
            self.assertNotRegex(doctor.out, r"\[!!\] config\.json", doctor.out)

    def test_wrong_types_in_the_global_config_are_named_and_never_a_traceback(self):
        for label, content in self.WRONG_TYPES.items():
            with self.subTest(label):
                project = self.project("shop-" + re.sub(r"\W", "", label))
                self.write_global(content)
                doctor = self.orc("doctor", cwd=project)
                self.assertEqual(doctor.rc, 1, str(doctor))
                key = next(iter(content))
                self.assertRegex(doctor.out, r"\[!!\] config\.json[^\n]*" + key, doctor.out)
                # the commands that read the section either work or point at doctor: never a traceback
                for args in (("init",), ("start", "--title", "t", "--request", "r"), ("status",), ("context",)):
                    res = self.orc(*args, cwd=project)
                    if "unexpected error" in res.err:
                        self.assertEqual(res.rc, 2, "a crash is never a failed check (exit 1)\n" + str(res))
                        self.assertIn("orkestra doctor", res.err, str(res))

    BAD_PROJECT = {
        "not json": "{oops",
        "a list": "[]",
        "gates is a string": '{"gates": "make test"}',
        "gates hold strings": '{"gates": ["make test"]}',
        "a gate without a command": '{"gates": [{"name": "unit"}]}',
    }

    def test_a_damaged_project_config_stops_the_work_that_needs_it(self):
        for label, text in self.BAD_PROJECT.items():
            with self.subTest(label):
                project = self.project("shop-" + re.sub(r"\W", "", label))
                self.orc("init", cwd=project, expect=0)
                cfg = project / ".orkestra" / "config.json"
                cfg.write_text(text, encoding="utf-8")
                for args in (("gate", "list"), ("gate", "detect", "--write"), ("gate", "confirm"),
                             ("start", "--title", "t", "--request", "r")):
                    res = self.refused(*args, cwd=project, mention=("config.json",))
                    self.assertIn(str(cfg), res.err)
                self.assertEqual(cfg.read_text(encoding="utf-8"), text, "the damaged file is never overwritten")
                self.assertIn("config.json", self.orc("context", cwd=project, expect=0).out)

    def test_a_damaged_project_config_does_not_trap_a_run_that_is_already_open(self):
        project = self.project("shop")
        rid = self.start(project)
        (project / ".orkestra" / "config.json").write_text("{oops", encoding="utf-8")
        self.assertIn(rid, self.orc("status", cwd=project, expect=0).out)
        self.orc("next", cwd=project, expect=0)
        self.orc("pause", "x", cwd=project, expect=0)
        self.orc("resume", cwd=project, expect=0)
        self.orc("abort", "bye", cwd=project, expect=0)

    BAD_STATE = {
        "not json": b"{broken",
        "empty file": b"",
        "truncated": b'{"run_id": "001-t", "phase": "int',
        "a list": b"[]",
        "binary": b"\x00\x01\x02\xff",
        "no phase": b'{"run_id": "001-t", "status": "active", "title": "t"}',
        "unknown phase": b'{"run_id": "001-t", "status": "active", "title": "t", "phase": "teleport"}',
    }

    def test_a_damaged_state_file_is_named_not_reported_as_missing(self):
        for label, content in self.BAD_STATE.items():
            with self.subTest(label):
                project = self.project("shop-" + re.sub(r"\W", "", label))
                rid = self.start(project, title="t")
                state = self.run_state(project, rid)
                state.write_bytes(content)
                for args in (("status",), ("next",), ("context",), ("phase", "next"), ("pause", "x"), ("resume",),
                             ("abort", "x"), ("tempo",), ("report", "evidence"), ("task", "list"),
                             ("start", "--title", "again", "--request", "r")):
                    res = self.refused(*args, cwd=project, mention=("state.json", rid))
                    self.assertNotIn("not found", res.err, str(res))
                    self.assertRegex(res.err, r"(?i)restore|remove|delete", "say how to get out\n" + str(res))
                self.orc("watch", "--once", "-p", str(project), cwd=project, expect=0)
                self.orc("watch", "--once", "--json", "-p", str(project), cwd=project, expect=0)
                self.assertEqual(state.read_bytes(), content, "the damaged state is never overwritten")

    def test_damaged_registries_and_session_files_are_rebuilt_from_the_runs_on_disk(self):
        project = self.project("shop")
        rid = self.start(project)
        (self.home / "active-runs.json").write_text("garbage", encoding="utf-8")
        (self.home / "sessions").mkdir(exist_ok=True)
        (self.home / "sessions" / "s-x.json").write_text("{", encoding="utf-8")
        (self.home / "grants").mkdir(exist_ok=True)
        (self.home / "grants" / "s-x.json").write_text("[1,", encoding="utf-8")
        self.assertIn(rid, self.orc("status", cwd=project, expect=0).out)
        self.hook("UserPromptSubmit", {"session_id": "s-x", "prompt": "continue", "cwd": str(project)})
        self.hook("PreToolUse", {"session_id": "s-x", "tool_name": "Bash", "cwd": str(project),
                                 "tool_input": {"command": "ls"}})
        self.orc("pause", "x", cwd=project, expect=0)
        self.assertIn(rid, self.orc("resume", cwd=project, expect=0).out)
        self.orc("abort", "bye", cwd=project, expect=0)
        self.assertIn("status aborted", self.orc("status", "--run", rid, cwd=project, expect=0).out)


@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestInvocationShapes(FreshMachine):
    """Where, how often and with what text orkestra is called."""

    def test_from_a_subdirectory_of_the_project(self):
        project = self.project("shop")
        rid = self.start(project)
        deep = project / "src" / "deep" / "er"
        deep.mkdir(parents=True)
        self.checkpoint()
        shown = {args: self.orc(*args, cwd=deep, expect=0).out
                 for args in (("status",), ("next",), ("context",), ("phase", "show"), ("tempo",), ("gate", "list"))}
        self.assertIn(rid, shown[("status",)])
        self.assertIn("active run: " + rid, shown[("context",)])
        self.assertIn("[intake]", shown[("phase", "show")])
        self.assertIn("make-test", shown[("gate", "list")])
        self.orc("pause", "x", cwd=deep, expect=0)
        self.orc("resume", cwd=deep, expect=0)
        self.assertFalse((deep / ".orkestra").exists(), "state belongs to the project root, not to the folder you stood in")
        self.assert_nothing_outside()

    def test_two_status_commands_at_the_same_time(self):
        project = self.project("shop")
        self.start(project)
        argv = [sys.executable, str(helpers.BIN), "status"]
        procs = [subprocess.Popen(argv, cwd=str(project), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace") for _ in range(6)]
        results = [(p.communicate(), p.returncode) for p in procs]
        for (out, err), rc in results:
            self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(len({out for (out, _), _ in results}), 1, "every reader sees the same state")

    @unittest.skipIf(os.name == "nt", "Windows: os.replace fails while a reader holds the file open; util.write_text_atomic "
                                      "has to retry first (reported to the portability work)")
    def test_a_status_read_while_the_run_is_being_changed(self):
        project = self.project("shop")
        self.start(project)
        stop = threading.Event()
        failures = []

        def reader():
            while not stop.is_set():
                cp = subprocess.run([sys.executable, str(helpers.BIN), "status"], cwd=str(project), env=self.env(),
                                    capture_output=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
                if cp.returncode != 0 or cp.stderr:
                    failures.append(cp.stdout + cp.stderr)

        worker = threading.Thread(target=reader)
        worker.start()
        try:
            for i in range(6):
                self.orc("pause", "round %d" % i, cwd=project, expect=0)
                self.orc("resume", cwd=project, expect=0)
        finally:
            stop.set()
            worker.join(60)
        self.assertEqual(failures[:1], [], "a reader never sees a half-written state file")

    def test_parallel_starts_open_exactly_one_run(self):
        project = self.project("shop")
        self.orc("init", cwd=project, expect=0)
        procs = [subprocess.Popen([sys.executable, str(helpers.BIN), "start", "--title", "job %d" % i, "--request", "r"],
                                  cwd=str(project), env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace") for i in range(4)]
        results = [(p.communicate(), p.returncode) for p in procs]
        codes = sorted(rc for _, rc in results)
        self.assertEqual(codes, [0, 2, 2, 2], [r for r in results])
        for (_, err), rc in results:
            if rc == 2:
                self.assertIn("already has an open run", err)
            self.assertNotIn(TRACEBACK, err)
        self.assertEqual(len(list((project / ".orkestra" / "runs").iterdir())), 1)
        self.assertEqual(len(list((project / ".orkestra" / "features").iterdir())), 1)

    TEXTS = {
        "quotes and shell syntax": "He said \"stop\" and 'go' and `ls` and $HOME and $(touch pwned) ; echo x > pwned2",
        "line breaks and tabs": "line one\n\nline three\r\nline four\n\ttabbed",
        "emoji and scripts": "ship it \U0001f680 \u2705 \u2014 \u00ab\u00fcn\u00efc\u00f6d\u00e9\u00bb \u00e7\u011f\u0131\u015f\u00f6 \u65e5\u672c\u8a9e \u0627\u0644\u0639\u0631\u0628\u064a\u0629",
        "protocol look-alikes": "# not a title\n<!-- orkestra:evidence:begin -->\n{{title}} {run_id} %s %(x)s {0} {}",
        "leading dashes": "--force -rf -- end",
        "backslashes and percent": "C:\\Users\\me\\file.txt \\n \\\\ %PATH% ^& | <redirect>",
    }

    @staticmethod
    def same_text(a: str, b: str) -> bool:
        norm = lambda text: re.sub(r"\r*\n", "\n", text).strip()          # noqa: E731  (CRLF is translated on Windows)
        return norm(a) == norm(b)

    def test_request_and_title_text_survives_verbatim_and_is_never_executed(self):
        project = self.project("shop")
        self.orc("init", cwd=project, expect=0)
        for label, text in self.TEXTS.items():
            with self.subTest(label):
                title = "T: " + text.splitlines()[0][:60]
                res = self.orc("start", "--title=" + title, "--request=" + text, cwd=project, expect=0)
                rid = re.search(r"run=(\S+)", res.out).group(1)
                saved = (self.feature(project, rid) / "request.md").read_text(encoding="utf-8")
                self.assertTrue(self.same_text(saved.split("## Raw request", 1)[1], text), saved)
                state = json.loads(self.orc("status", "--json", cwd=project, expect=0).out)
                self.assertEqual(state["title"], title)
                self.orc("next", cwd=project, expect=0)
                self.orc("watch", "--once", "-p", str(project), cwd=project, expect=0)
                self.orc("abort", "next text", cwd=project, expect=0)
        for stray in ("pwned", "pwned2"):
            self.assertFalse((project / stray).exists() or (self.cwd / stray).exists(), "request text is never executed")

    def test_a_multi_line_title_does_not_break_the_ledger_or_the_views(self):
        project = self.project("shop")
        rid = self.start(project, title="first line\nsecond line \U0001f680")
        ledger = (self.feature(project, rid) / "ledger.md").read_text(encoding="utf-8")
        self.assertIn("run started", ledger)
        self.orc("status", cwd=project, expect=0)
        self.orc("watch", "--once", "-p", str(project), cwd=project, expect=0)
        self.orc("tempo", cwd=project, expect=0)

    def test_a_very_long_request_and_title(self):
        project = self.project("shop")
        text = "word " * 60000                                               # 300 kB
        res = self.orc("start", "--title=" + "L" * 3000, "--request=" + text, cwd=project, expect=0)
        rid = re.search(r"run=(\S+)", res.out).group(1)
        self.assertLessEqual(len(rid), 60, "the run id stays a usable folder name")
        saved = (self.feature(project, rid) / "request.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(saved), len(text.strip()))
        self.orc("status", cwd=project, expect=0)
        self.orc("next", cwd=project, expect=0)
        self.orc("abort", "enough", cwd=project, expect=0)
        # the same through a file
        big = project / "big.txt"
        big.write_text("line of a long request\n" * 40000, encoding="utf-8")
        res = self.orc("start", "--title", "from a file", "--request-file", str(big), cwd=project, expect=0)
        rid = re.search(r"run=(\S+)", res.out).group(1)
        self.assertGreater((self.feature(project, rid) / "request.md").stat().st_size, big.stat().st_size - 10)

    def request_file(self, project: Path, content: bytes) -> Result:
        path = project / "request.txt"
        path.write_bytes(content)
        return self.orc("start", "--title", "from a file", "--request-file", str(path), cwd=project)

    def test_request_files_in_the_encodings_users_really_have(self):
        text = "T\u00fcrk\u00e7e istek: \u011f\u00fc\u015fi\u00f6\u00e7 \u2713\nsecond line"
        cases = {
            "utf-8": text.encode("utf-8"),
            "utf-8 with a byte order mark": b"\xef\xbb\xbf" + text.encode("utf-8"),
            "utf-16 (PowerShell 5 redirection)": b"\xff\xfe" + text.encode("utf-16-le"),
        }
        project = self.project("shop")
        for label, raw in cases.items():
            with self.subTest(label):
                res = self.request_file(project, raw)
                self.assertEqual(res.rc, 0, str(res))
                rid = re.search(r"run=(\S+)", res.out).group(1)
                saved = (self.feature(project, rid) / "request.md").read_text(encoding="utf-8")
                self.assertIn(text.splitlines()[0], saved)
                self.assertNotIn("\ufeff", saved)
                self.assertNotIn("\x00", saved)
                self.orc("abort", "next case", cwd=project, expect=0)

    def test_request_files_that_cannot_be_used_are_named(self):
        project = self.project("shop")
        missing = self.orc("start", "--title", "t", "--request-file", str(project / "nope.txt"), cwd=project, expect=2)
        self.assertIn("nope.txt", missing.err)
        self.assertNotIn("--request or --request-file is required", missing.err, "the file was given: say it is missing")
        folder = self.orc("start", "--title", "t", "--request-file", str(project), cwd=project, expect=2)
        self.assertIn(str(project), folder.err)
        empty = self.request_file(project, b"  \n")
        self.assertEqual(empty.rc, 2, str(empty))
        self.assertIn("request", empty.err)
        legacy = self.request_file(project, "caf\u00e9 \u00fcr\u00fcn".encode("latin-1"))
        self.assertEqual(legacy.rc, 2, str(legacy))
        self.assertIn("UTF-8", legacy.err)
        self.assertIn("request.txt", legacy.err)
        self.assertFalse((project / ".orkestra" / "runs").exists() and any((project / ".orkestra" / "runs").iterdir()),
                         "a refused request must not open a run")

    def test_a_narrow_terminal_encoding_never_breaks_the_output(self):
        """Windows pipes default to cp1252, some POSIX locales to ASCII: arrows, dots and emoji must not crash."""
        project = self.project("shop")
        self.start(project, title="\u00c7ok \U0001f680", request="r")
        for encoding in ("ascii", "latin-1", "cp1252"):
            for args in (("status",), ("phase", "next"), ("context",), ("template", "spec"), ("next",), ("doctor",)):
                with self.subTest(encoding=encoding, args=args):
                    cp = subprocess.run([sys.executable, str(helpers.BIN), *args], cwd=str(project),
                                        env=self.env(PYTHONIOENCODING=encoding), capture_output=True,
                                        stdin=subprocess.DEVNULL)
                    self.assertNotIn(TRACEBACK.encode(), cp.stderr, cp.stderr.decode("utf-8", "replace"))
                    self.assertIn(cp.returncode, (0, 1, 2))
                    cp.stdout.decode("utf-8")                       # strict: what we print is always valid UTF-8

    def test_a_plain_c_locale_with_a_non_ascii_project_path(self):
        """A server with LANG=C and no UTF-8 mode: paths and titles with letters of any script still print."""
        try:
            project = self.project(UNICODE_NAME, git=True, gate=False)
        except (OSError, UnicodeError) as exc:
            self.skipTest("this file system cannot hold the fixture path: %s" % exc)
        c_locale = dict(LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0", PYTHONIOENCODING=None)
        self.orc("init", cwd=project, expect=0, **c_locale)
        self.orc("start", "--title", "\u00c7ok \U0001f680", "--request", "r", cwd=project, expect=0, **c_locale)
        for args in (("status",), ("context",), ("next",), ("pause", "x"), ("resume",), ("abort", "y")):
            self.orc(*args, cwd=project, expect=0, **c_locale)

    @unittest.skipIf(os.name == "nt", "closing the read end of a pipe is a POSIX scenario")
    def test_a_reader_that_goes_away_is_not_an_error_of_ours(self):
        """`orkestra template spec | head -1`: the pipe closes before the output is delivered."""
        project = self.project("shop")
        for args in (("template", "spec"), ("playbook", "run"), ("memory", "show")):
            read_end, write_end = os.pipe()
            os.close(read_end)
            try:
                cp = subprocess.run([sys.executable, str(helpers.BIN), *args], cwd=str(project), env=self.env(),
                                    stdout=write_end, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    encoding="utf-8", errors="replace")
            finally:
                os.close(write_end)
            self.assertEqual(cp.stderr, "", args)
            self.assertIn(cp.returncode, (0, 1), args)


@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestEveryPythonThisMachineHas(FreshMachine):
    """Python 3.9 to 3.14: the same first session, with every warning switched on."""

    def test_the_first_session_on_each_interpreter(self):
        found = interpreters()
        self.assertTrue(found, "at least the running interpreter")
        for version, path in found:
            with self.subTest(python=version):
                project = self.project("shop-py" + version.replace(".", ""))
                py = [path, "-W", "always"]
                self.assertIn("orkestra", self.orc("--version", python=py, cwd=project, expect=0).out)
                self.orc("init", python=py, cwd=project, expect=0)
                rid = self.start_with(py, project)
                self.assertIn(rid, self.orc("status", python=py, cwd=project, expect=0).out)
                self.orc("next", python=py, cwd=project, expect=0)
                self.orc("context", python=py, cwd=project, expect=0)
                self.orc("template", "plan", python=py, cwd=project, expect=0)
                self.orc("watch", "--once", "-p", str(project), python=py, cwd=project, expect=0)
                self.orc("lang", "--lang", "tr", python=py, cwd=project, expect=0)
                self.orc("pause", "x", python=py, cwd=project, expect=0)
                self.orc("resume", python=py, cwd=project, expect=0)
                self.orc("abort", "bye", python=py, cwd=project, expect=0)
                doctor = self.orc("doctor", python=py, cwd=project)
                self.assertIn(doctor.rc, (0, 1))
                self.assertRegex(doctor.out, r"python >= 3\.9\D+" + re.escape(version), "doctor names the interpreter in use")

    def start_with(self, python, project: Path) -> str:
        res = self.orc("start", "--title", "t", "--request", "r", python=python, cwd=project, expect=0)
        return re.search(r"run=(\S+)", res.out).group(1)

    def test_warnings_in_any_stage_are_errors_in_every_source_file(self):
        """A SyntaxWarning (a bad escape in a regex) or a DeprecationWarning at compile time is a bug on 3.12+."""
        roots = [helpers.PLUGIN / "lib", helpers.PLUGIN / "hooks", helpers.PLUGIN / "bin"]
        files = sorted(str(p) for root in roots for p in root.rglob("*")
                       if p.is_file() and (p.suffix == ".py" or p.name == "orkestra") and "__pycache__" not in p.parts
                       and "vendor" not in p.parts)
        self.assertGreater(len(files), 20)
        script = ("import sys, warnings\nwarnings.simplefilter('error')\nbad = []\nfor name in sys.argv[1:]:\n"
                  "    try:\n        compile(open(name, encoding='utf-8').read(), name, 'exec')\n"
                  "    except (SyntaxError, Warning) as exc:\n        bad.append('%s: %s' % (name, exc))\nprint('\\n'.join(bad))\n")
        for version, path in interpreters():
            with self.subTest(python=version):
                cp = subprocess.run([path, "-c", script] + files, capture_output=True, text=True, timeout=300)
                self.assertEqual((cp.returncode, cp.stdout.strip()), (0, ""), cp.stderr)



@unittest.skipUnless(GIT, "git is needed to build the fixture projects")
class TestAgentToolingOnABareMachine(FreshMachine):
    """No Claude, Cursor or Copilot configuration anywhere: the status and preview commands say what is missing."""

    def test_status_and_preview_commands(self):
        project = self.project("shop")
        for args in (("cursor", "status"), ("copilot", "status"), ("copilot", "install", "--dry-run"),
                     ("cursor", "uninstall", "--dry-run"), ("copilot", "uninstall", "--dry-run"),
                     ("watchdog", "--dry-run"), ("watchdog", "--json", "--dry-run"), ("watchdog", "--quiet", "--dry-run"),
                     ("watch", "--once"), ("lang",), ("version",)):
            with self.subTest(args=args):
                res = self.orc(*args, cwd=project)
                self.assertTrue(res.text.strip() or args == ("watchdog", "--quiet", "--dry-run"),
                                "only the quiet scan may say nothing\n" + str(res))
        self.assertFalse((self.tmp / "cursor-home").exists(), "status and dry runs create nothing")
        self.assertFalse((self.tmp / "copilot-home").exists())
        self.assert_nothing_outside()

    def test_launchd_is_refused_where_there_is_no_launchd(self):
        import argparse
        from orkestra_core import cli as CLI
        from orkestra_core.state import OrkestraError
        args = argparse.Namespace(launchd=True, json=False, dry_run=False, quiet=False)
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    mock.patch.dict(os.environ, {"HOME": str(self.userhome), "USERPROFILE": str(self.userhome)}):
                with self.assertRaises(OrkestraError) as ctx:
                    CLI.cmd_watchdog(args)
                self.assertIn("orkestra watchdog --quiet", str(ctx.exception))
        self.assertFalse((self.userhome / "Library").exists(), "no macOS folders on other systems")

    @unittest.skipIf(os.name == "nt", "the LaunchAgent path is a macOS path")
    def test_the_watchdog_agent_keeps_the_language_launchd_would_drop(self):
        """launchd starts a job without the user's locale: the plist carries LANG, and the result line is localized."""
        import argparse
        from orkestra_core import cli as CLI
        args = argparse.Namespace(launchd=True, json=False, dry_run=False, quiet=False)
        plist = self.userhome / "Library" / "LaunchAgents" / "com.orkestra.watchdog.plist"
        for lang, locale, wrote in (("tr", "tr_TR.UTF-8", "yazıldı"), ("en", "en_US.UTF-8", "wrote")):
            with self.subTest(lang=lang), mock.patch.object(sys, "platform", "darwin"), \
                    mock.patch.dict(os.environ, {"HOME": str(self.userhome), "USERPROFILE": str(self.userhome),
                                                 "ORKESTRA_LANG": lang}), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(CLI.cmd_watchdog(args), 0)
                self.assertIn(f"<key>LANG</key><string>{locale}</string>", plist.read_text(encoding="utf-8"))
                self.assertIn("<key>PATH</key>", plist.read_text(encoding="utf-8"))
                self.assertIn(wrote, out.getvalue())
                self.assertIn("launchctl load -w", out.getvalue())


if __name__ == "__main__":
    unittest.main()