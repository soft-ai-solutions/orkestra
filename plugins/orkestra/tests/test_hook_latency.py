"""Hook start-up cost: what a hook process imports, how much CPU it burns, and proof that being cheaper changed no decision.

  * guards stay identical: tests/data/guard_decisions.json (several thousand recorded PreToolUse decisions, see
    guard_corpus.py) is replayed in-process, plus a sample through real hook subprocesses;
  * the events that provably do nothing exit early, and that early exit equals the full path (no output, no files);
  * the import graph: an unbound no-op event loads json, os, paths and state only; a guarded one never loads runs,
    tempo, evidence, snapshot, engines, argparse, traceback, dataclasses, subprocess, shutil, tempfile, hashlib ...;
  * CPU: the guarded hook and the no-op hook are at least 35 % cheaper than the frozen pre-change entry script, measured
    interleaved in the same run; an absolute budget scaled by a reference loop (helpers.assert_cpu_under style) backs it.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
import guard_corpus

try:
    import resource
except ImportError:                                    # Windows: no per-child CPU accounting
    resource = None

PLUGIN = helpers.PLUGIN
LIB = PLUGIN / "lib"
DISPATCH = helpers.DISPATCH
BASELINE = PLUGIN / "tests" / "data" / "dispatch_pre_c2.py.txt"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
from orkestra_core import guards, hookboot, i18n, util  # noqa: E402
from orkestra_core import state as ST  # noqa: E402
from orkestra_core.lazy import LazyModule  # noqa: E402

POSIX = os.name == "posix"
# what `import orkestra_core.*` may load for an event that does nothing: the early exit and what it reads
QUIET_MODULES = {"orkestra_core", "orkestra_core.hookboot", "orkestra_core.paths", "orkestra_core.state",
                 "orkestra_core.util", "orkestra_core.i18n", "orkestra_core.lazy"}
HEAVY_STDLIB = ("argparse", "traceback", "dataclasses", "inspect", "subprocess", "shutil", "tempfile", "hashlib", "difflib",
                "uuid", "random")
# The frozen entry script (tests/data/dispatch_pre_c2.py.txt) runs against TODAY's libraries, which are lazier than they
# were. So that "before" is the real pre-change start-up, this is added after its imports: what util, gates, guards,
# shellparse, planparse and cliparse used to import at start-up and what intent used to compile.
LEGACY_IMPORTS_AFTER = "from orkestra_core import activity, guards, intent, paths, runs, snapshot, tempo  # noqa: E402\n"
LEGACY_IMPORTS = LEGACY_IMPORTS_AFTER + """\
import argparse, dataclasses, hashlib, shutil, subprocess, tempfile  # noqa: E402,F401
from orkestra_core import cliparse  # noqa: E402,F401
for _value in list(vars(intent).values()):
    if _value.__class__.__name__ == "_LazyRe":
        _value.pattern                      # the ~40 vocabulary regexes were compiled at import
"""
HEAVY_LIB = ("runs", "tempo", "evidence", "snapshot", "engines", "planparse", "intent", "cliparse", "monitor", "gates_run")


class Sandbox(unittest.TestCase):
    """A private ORKESTRA_HOME / workspace / HOME and a project, and the environment that makes a hook use them."""

    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="orkestra-latency-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home, self.ws, self.user = self.root / "home", self.root / "ws", self.root / "user"
        self.proj = self.ws / "proj"
        for d in (self.home / "sessions", self.proj / "src", self.proj / ".git", self.user):
            d.mkdir(parents=True, exist_ok=True)
        (self.proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        self.env = dict(os.environ, ORKESTRA_HOME=str(self.home), ORKESTRA_WORKSPACE=str(self.ws), HOME=str(self.user),
                        ORKESTRA_LANG="en", ORKESTRA_NO_DAEMON="1")
        for k in ("ORKESTRA_HEADLESS", "CLAUDE_PLUGIN_ROOT", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX",
                  "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "CLAUDECODE"):
            self.env.pop(k, None)

    def event(self, name, **kw):
        base = {"hook_event_name": name, "session_id": "sess-1", "cwd": str(self.proj)}
        base.update(kw)
        return base

    def bind(self, sid="sess-1", phase="implement"):
        """A session bound to an open run (the files a real `orkestra start` + bind leave)."""
        rdir = self.proj / ".orkestra" / "runs" / "001-x"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "state.json").write_text(json.dumps({"run_id": "001-x", "title": "t", "phase": phase, "status": "active",
                                                     "approved": {"deps": []}, "tasks": {}}))
        (self.home / "sessions" / f"{ST._safe(sid)}.json").write_text(
            json.dumps({"project": str(self.proj), "run_id": "001-x", "bound_at": "2026-10-13T00:00:00+03:00"}))

    def run_hook(self, entry, name, payload, env=None, python=None):
        return subprocess.run([python or sys.executable, str(entry), name], input=json.dumps(payload), capture_output=True,
                              text=True, encoding="utf-8", env=env or self.env, timeout=60)

    def tree(self):
        """Every file under the sandbox with size and mtime: a hook that changed nothing leaves this equal."""
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "Caches" not in p.parts:              # (Apple's python keeps its bytecode in ~/Library/Caches)
                st = p.stat()
                out[str(p.relative_to(self.root))] = (st.st_size, st.st_mtime_ns)
        return out


# ====================================================================================== guards stay identical

class TestGuardDecisionsAreUnchanged(Sandbox):
    def test_the_corpus_is_large_and_varied(self):
        rows = guard_corpus.load()
        self.assertGreaterEqual(len(rows), 2500)
        tools = {r[0] for r in rows}
        for tool in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob", "Agent", "AskUserQuestion"):
            self.assertIn(tool, tools)
        guards_seen = {r[9] for r in rows if r[8]}
        for g in ("git", "secrets", "internals", "roles", "deploy", "deps", "destructive", "isolation", "phase", "store",
                  "home", "rollback", "dialogs", "wait", "approval"):
            self.assertIn(g, guards_seen)
        self.assertEqual({r[6] for r in rows}, {"tr", "en"})
        self.assertGreater(len({r[3] for r in rows}), 5)                      # agent roles
        self.assertGreater(len({r[4] for r in rows}), 5)                      # bound and unbound sessions, several phases
        self.assertTrue(any(r[2].startswith("C:\\") for r in rows) and any("C:\\\\" in json.dumps(r[1]) for r in rows))
        self.assertTrue(any(r[0] == "Bash" and "bash -c" in r[1]["command"] for r in rows))
        self.assertTrue(any(r[0] == "Bash" and "$(" in r[1]["command"] for r in rows))
        self.assertTrue(any(r[0] == "Bash" and "<<" in r[1]["command"] for r in rows))
        self.assertTrue(0.15 < sum(r[8] for r in rows) / len(rows) < 0.6)     # neither all denied nor all allowed

    def test_every_recorded_decision_is_identical_in_process(self):
        bad = guard_corpus.check()
        shown = "\n".join(f"{r[0]} {json.dumps(r[1])[:100]} bind={r[4]} role={r[3]} grants={r[5]} -> {g} (recorded {r[8:]})"
                          for r, g in bad[:15])
        self.assertEqual(bad, [], f"{len(bad)} decision(s) changed:\n{shown}")

    def test_a_sample_decides_the_same_through_real_hook_processes(self):
        rows = [r for r in guard_corpus.load()
                if r[4] == "none" and not r[3] and not re.search(r"\b(checkout|switch|push)\b", json.dumps(r[1]))]
        sample = rows[::max(1, len(rows) // 40)][:40]
        self.assertGreaterEqual(len(sample), 30)
        self.assertTrue(any(r[8] for r in sample) and any(not r[8] for r in sample))
        sb = guard_corpus.Sandbox()
        try:
            env = dict(self.env, HOME=str(sb.U), ORKESTRA_HOME=str(sb.H), ORKESTRA_WORKSPACE=str(sb.root / "ws"))
            for r in sample:
                with self.subTest(tool=r[0], input=json.dumps(r[1])[:80]):
                    env["ORKESTRA_LANG"] = r[6]
                    ev = {"hook_event_name": "PreToolUse", "tool_name": r[0], "tool_input": sb.fill(r[1]), "cwd": sb.fill(r[2]),
                          "session_id": "s1", "prompt_id": "p1"}
                    cp = self.run_hook(DISPATCH, "PreToolUse", ev, env=env)
                    self.assertEqual(cp.returncode, 0, cp.stderr)
                    out = json.loads(cp.stdout)["hookSpecificOutput"] if cp.stdout.strip() else {}
                    denied = out.get("permissionDecision") == "deny"
                    self.assertEqual(int(denied), r[8], cp.stdout[:200])
                    if denied:
                        self.assertRegex(out["permissionDecisionReason"], r"^\[orkestra:" + re.escape(r[9]) + r"\]")
        finally:
            shutil.rmtree(sb.root, ignore_errors=True)


# ====================================================================================== the early exit

class TestQuietEvents(Sandbox):
    SIMPLE = (("Stop", {}), ("SubagentStop", {"agent_type": "orkestra:implementer", "agent_id": "a1"}),
              ("Notification", {"notification_type": "permission_prompt", "message": "x"}), ("StopFailure", {"error": "rate"}),
              ("SessionEnd", {"reason": "clear"}), ("PostToolUseFailure", {"tool_name": "Bash", "tool_input": {"command": "ls"}}))

    def cases(self):
        """(name, payload, expected quiet) for a session WITHOUT a binding file."""
        p = str(self.proj)
        out = [(n, self.event(n, **kw), True) for n, kw in self.SIMPLE]
        for tool, ti in (("Edit", {"file_path": p + "/a.ts", "old_string": "a", "new_string": "b"}),
                         ("Write", {"file_path": p + "/a.ts", "content": "x"}), ("Agent", {"subagent_type": "x"}),
                         ("Task", {"subagent_type": "x"}), ("SendMessage", {"to": "a", "message": "m"}),
                         ("AskUserQuestion", {"questions": []}), ("TodoWrite", {"todos": []}),
                         ("mcp__serena__replace_symbol_body", {"relative_path": "a.ts"}), ("Read", {"file_path": p + "/a.ts"})):
            out.append(("PostToolUse", self.event("PostToolUse", tool_name=tool, tool_input=ti, tool_response={"ok": 1}), True))
        out.append(("PostToolUse", self.event("PostToolUse", tool_name="Bash", tool_input={"command": "ls"},
                                              tool_response={"stdout": "hello", "stderr": ""}), True))
        for tool in ("Agent", "Task", "SendMessage", "AskUserQuestion", "WebFetch", "WebSearch", "Glob", "TodoWrite"):
            out.append(("PreToolUse", self.event("PreToolUse", tool_name=tool, tool_input={"x": 1}), True))
        # never skipped: every tool some global guard looks at, and a Bash output that may carry a bind marker
        for tool in ("Bash", "PowerShell", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep", "mcp__serena__execute_shell_command",
                     "mcp__github__push_files"):
            out.append(("PreToolUse", self.event("PreToolUse", tool_name=tool, tool_input={"command": "ls", "file_path": p}),
                        False))
        out.append(("PostToolUse", self.event("PostToolUse", tool_name="Bash", tool_input={"command": "orkestra start"},
                                              tool_response={"stdout": f"[orkestra:bind] project={p} run=001-x\n"}), False))
        out.append(("PostToolUse", self.event("PostToolUse", tool_name="Bash", tool_input={"command": "x"},
                                              tool_response={"stdout": "[orkestra:unbind] project=/p run=r\n"}), False))
        out += [("SessionStart", self.event("SessionStart", source="startup"), False),
                ("UserPromptSubmit", self.event("UserPromptSubmit", prompt="hi", prompt_id="p"), False),
                ("NoSuchEvent", self.event("NoSuchEvent"), False)]
        return out

    def test_is_quiet_matrix_unbound(self):
        for name, payload, quiet in self.cases():
            with self.subTest(event=name, tool=payload.get("tool_name")):
                with mock.patch.dict(os.environ, self.env):
                    self.assertEqual(hookboot.is_quiet(name, payload, json.dumps(payload)), quiet)

    def test_a_session_with_a_binding_file_is_never_quiet(self):
        self.bind()
        for name, payload, _ in self.cases():
            with self.subTest(event=name, tool=payload.get("tool_name")):
                with mock.patch.dict(os.environ, self.env):
                    self.assertFalse(hookboot.is_quiet(name, payload, json.dumps(payload)))

    def test_even_a_broken_binding_file_takes_the_full_path(self):
        (self.home / "sessions" / "sess-1.json").write_text("{not json")
        with mock.patch.dict(os.environ, self.env):
            self.assertFalse(hookboot.is_quiet("Stop", self.event("Stop"), "{}"))

    def test_odd_payloads_are_not_quiet(self):
        with mock.patch.dict(os.environ, self.env):
            for ev in (None, [], "x", {"session_id": 5}, {"session_id": "s", "tool_name": 7}):
                self.assertFalse(hookboot.is_quiet("Stop", ev, "{}"), ev)

    def load_dispatch(self):
        spec = importlib.util.spec_from_file_location("dispatch_quiet_parity", str(DISPATCH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_early_exit_equals_the_full_path_no_output_and_no_files(self):
        """Every event is_quiet skips, run through the real handlers: no answer, no new or changed file anywhere."""
        D = self.load_dispatch()
        for name, payload, quiet in self.cases():
            if not quiet:
                continue
            with self.subTest(event=name, tool=payload.get("tool_name")):
                before = self.tree()
                out = io.StringIO()
                with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", ["dispatch.py", name]), \
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                    self.assertEqual(D.main(), 0)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(self.tree(), before)

    def test_through_a_real_process_quiet_events_leave_nothing_behind(self):
        before = self.tree()
        for name, payload, quiet in self.cases():
            if not quiet:
                continue
            cp = self.run_hook(DISPATCH, name, payload)
            self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""), f"{name} {payload.get('tool_name')}")
        self.assertEqual(self.tree(), before)

    def test_a_cursor_payload_is_answered_by_cursor_hook_only(self):
        cp = self.run_hook(DISPATCH, "PreToolUse", self.event("PreToolUse", cursor_version="2.0", tool_name="Bash",
                                                              tool_input={"command": "git push"}))
        self.assertEqual((cp.returncode, cp.stdout), (0, ""))

    def test_the_hook_event_name_of_the_payload_counts_when_there_is_no_argument(self):
        p = subprocess.run([sys.executable, str(DISPATCH)], input=json.dumps(self.event("PreToolUse", tool_name="Bash",
                           tool_input={"command": "git push origin main"})), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")      # not skipped

    def test_a_marker_in_a_bash_output_still_binds_an_unbound_session(self):
        """The one PostToolUse of an unbound session that does work: `orkestra start` printing its bind line."""
        rdir = self.proj / ".orkestra" / "runs" / "001-x"
        rdir.mkdir(parents=True)
        (rdir / "state.json").write_text(json.dumps({"run_id": "001-x", "title": "t", "phase": "intake", "status": "active"}))
        marker = f"[orkestra:bind] project={self.proj} run=001-x home={self.home}\n"
        cp = self.run_hook(DISPATCH, "PostToolUse", self.event("PostToolUse", tool_name="Bash",
                           tool_input={"command": f"orkestra start --title x -p {self.proj}"},
                           tool_response={"stdout": marker, "stderr": ""}))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        bound = json.loads((self.home / "sessions" / "sess-1.json").read_text())
        self.assertEqual((bound["run_id"], bound["project"]), ("001-x", str(self.proj)))


# ====================================================================================== what a hook imports

RUNNER = r"""
import io, json, os, runpy, sys
name, entry, payload = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = ["dispatch.py", name]
sys.stdin = io.StringIO(payload)
try:
    runpy.run_path(entry, run_name="__main__")
except SystemExit:
    pass
sys.stderr.write("\nMODULES:" + json.dumps(sorted(sys.modules)))
"""


class TestImportGraph(Sandbox):
    def modules(self, name, payload):
        cp = subprocess.run([sys.executable, "-c", RUNNER, name, str(DISPATCH), json.dumps(payload)], capture_output=True,
                            text=True, env=self.env, timeout=60)
        self.assertIn("MODULES:", cp.stderr, cp.stderr[-400:])
        return set(json.loads(cp.stderr.split("MODULES:", 1)[1])), cp

    def assertLean(self, mods, extra_forbidden=(), allow=()):
        forbidden = (set(HEAVY_STDLIB) | {f"orkestra_core.{m}" for m in HEAVY_LIB} | set(extra_forbidden)) - set(allow)
        self.assertEqual(sorted(mods & forbidden), [])

    def test_a_no_op_event_loads_json_os_paths_and_state_only(self):
        p = str(self.proj)
        for name, payload in (
                ("Stop", self.event("Stop")),
                ("SubagentStop", self.event("SubagentStop", agent_type="orkestra:implementer", agent_id="a")),
                ("PostToolUse", self.event("PostToolUse", tool_name="Edit", tool_input={"file_path": p + "/a"}, tool_response={})),
                ("PostToolUse", self.event("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": "x"})),
                ("PreToolUse", self.event("PreToolUse", tool_name="Agent", tool_input={"prompt": "x"})),
                ("PreToolUse", self.event("PreToolUse", tool_name="TodoWrite", tool_input={}))):
            with self.subTest(event=name, tool=payload.get("tool_name")):
                mods, _ = self.modules(name, payload)
                self.assertLessEqual({m for m in mods if m.startswith("orkestra_core.")}, QUIET_MODULES)
                self.assertLean(mods, ("orkestra_core.guards", "orkestra_core.shellparse", "orkestra_core.activity"))

    def test_a_guarded_pre_event_loads_what_the_guards_need_and_nothing_else(self):
        p = str(self.proj)
        for payload in (self.event("PreToolUse", tool_name="Bash", tool_input={"command": "ls -la src"}),
                        self.event("PreToolUse", tool_name="Bash", tool_input={"command": "cd src && git status | head"}),
                        self.event("PreToolUse", tool_name="Edit", tool_input={"file_path": p + "/a.ts", "old_string": "a", "new_string": "b"}),
                        self.event("PreToolUse", tool_name="Read", tool_input={"file_path": p + "/a.ts"}),
                        self.event("PreToolUse", tool_name="Grep", tool_input={"pattern": "x", "path": p})):
            with self.subTest(tool=payload["tool_name"], input=json.dumps(payload["tool_input"])[:40]):
                mods, _ = self.modules("PreToolUse", payload)
                self.assertIn("orkestra_core.guards", mods)
                self.assertLean(mods)

    def test_a_denied_call_does_not_load_the_heavy_modules_either(self):
        mods, cp = self.modules("PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "cat .env"}))
        self.assertIn("deny", cp.stdout)
        self.assertLean(mods, allow=("hashlib", "orkestra_core.engines"))     # the refusal's id (sha1) and a log line's short id

    def test_orkestra_cli_lines_skip_the_parser_unless_the_decision_needs_it(self):
        for cmd in ("orkestra status && orkestra next", "orkestra task start T001", "orkestra phase next",
                    "cd src && orkestra gate run unit", "python3 /x/bin/orkestra context"):
            with self.subTest(cmd=cmd):
                mods, _ = self.modules("PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": cmd}))
                self.assertNotIn("argparse", mods)
                self.assertNotIn("orkestra_core.cliparse", mods)
        for cmd in ("orkestra tdd green T001 -- npm test", "orkestra evidence a -- ls", "orkestra pause -p /x",
                    "orkestra rollback --yes"):
            with self.subTest(cmd=cmd):              # the positive control: the parser IS loaded when the decision needs it
                mods, _ = self.modules("PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": cmd}))
                self.assertIn("orkestra_core.cliparse", mods)

    def test_a_bound_pre_event_and_stop_stay_lean(self):
        self.bind()
        p = str(self.proj)
        mods, _ = self.modules("PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
        self.assertIn("orkestra_core.activity", mods)
        self.assertLean(mods)
        mods, _ = self.modules("PreToolUse", self.event("PreToolUse", tool_name="Edit",
                                                        tool_input={"file_path": p + "/a.ts", "old_string": "a", "new_string": "b"}))
        self.assertLean(mods, allow=("orkestra_core.snapshot",))        # the backup before the first edit of a file
        mods, _ = self.modules("SubagentStop", self.event("SubagentStop", agent_type="general-purpose", agent_id="a"))
        self.assertLean(mods)
        self.assertNotIn("orkestra_core.guards", mods)

    def test_a_hook_never_loads_the_help_bridge_monitor_cli_or_install_messages(self):
        self.bind()
        p = str(self.proj)
        allowed = {"orkestra_core.messages", "orkestra_core.messages.common", "orkestra_core.messages.dispatch",
                   "orkestra_core.messages.activity", "orkestra_core.messages.tempo", "orkestra_core.messages.runs",
                   "orkestra_core.messages.evidence", "orkestra_core.messages.protocol", "orkestra_core.messages.watchdog"}
        for lang in ("en", "tr"):
            self.env["ORKESTRA_LANG"] = lang
            for name, payload in (("Stop", self.event("Stop", stop_hook_active=False)),
                                  ("SubagentStop", self.event("SubagentStop", agent_type="orkestra:implementer", agent_id="a")),
                                  ("UserPromptSubmit", self.event("UserPromptSubmit", prompt="continue", prompt_id="p")),
                                  ("SessionStart", self.event("SessionStart", source="startup")),
                                  ("PostToolUse", self.event("PostToolUse", tool_name="Edit", tool_input={"file_path": p + "/a.ts"},
                                                             tool_response={})),
                                  ("PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "cat .env"}))):
                with self.subTest(lang=lang, event=name):
                    mods, _ = self.modules(name, payload)
                    loaded = {m for m in mods if m.startswith("orkestra_core.messages")}
                    self.assertLessEqual(loaded, allowed)
                    for area in ("help", "bridge", "monitor", "cli", "install"):
                        self.assertNotIn(f"orkestra_core.messages.{area}", mods)

    def test_every_lazy_handle_resolves(self):
        """A lazy import must fail where it is used, never be broken without anyone noticing: import each target."""
        spec = importlib.util.spec_from_file_location("dispatch_lazy_targets", str(DISPATCH))
        D = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(D)
        from orkestra_core import gates, snapshot
        handles = [(D, n) for n in ("guards", "sp", "activity", "intent", "runs", "snapshot", "tempo", "CP")]
        handles += [(util, n) for n in ("hashlib", "random", "shutil", "unicodedata")]
        handles += [(gates, "shutil"), (gates, "subprocess"), (snapshot, "shutil"), (snapshot, "subprocess"), (guards, "CP")]
        sys.path.insert(0, str(PLUGIN / "hooks"))
        try:
            import copilot_hook
        finally:
            sys.path.remove(str(PLUGIN / "hooks"))
        handles += [(copilot_hook, "EN"), (copilot_hook, "hashlib")]
        for owner, attr in handles:
            with self.subTest(owner=getattr(owner, "__name__", owner), attr=attr):
                handle = getattr(owner, attr)
                self.assertTrue(isinstance(handle, LazyModule), "a lazy handle")
                self.assertTrue(handle.__name__)                            # importing it works


class TestLazyModule(unittest.TestCase):
    def test_import_happens_on_first_use_not_at_creation(self):
        sys.modules.pop("colorsys", None)
        handle = LazyModule("colorsys")
        self.assertNotIn("colorsys", sys.modules)
        self.assertEqual(handle.rgb_to_hsv(1, 1, 1), (0.0, 0.0, 1))
        self.assertIn("colorsys", sys.modules)

    def test_a_missing_module_fails_at_use_with_the_import_error(self):
        handle = LazyModule("orkestra_no_such_module_xyz")
        with self.assertRaises(ImportError):
            handle.anything

    def test_patching_through_the_handle_changes_the_real_module_and_is_undone(self):
        import shutil as real
        handle = LazyModule("shutil")
        original = real.which
        with mock.patch.object(handle, "which", return_value="/x") as which:
            self.assertEqual(real.which("python3"), "/x")
            self.assertEqual(handle.which("python3"), "/x")
            which.assert_called()
        self.assertIs(real.which, original)

    def test_a_sibling_module_of_the_package(self):
        handle = LazyModule("hints", "orkestra_core")
        self.assertEqual(handle.__name__, "orkestra_core.hints")
        self.assertIn("(", repr(LazyModule("json")) + "(")


# ====================================================================================== the guards' own laziness

class TestParserOnlyWhenTheDecisionNeedsIt(unittest.TestCase):
    def decide(self, cmd, agent_type=None, grants=()):
        from orkestra_core import paths
        guards._CLI_CACHE.clear()
        ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/w/proj", "session_id": "s1"}
        if agent_type:
            ev["agent_type"] = agent_type
            ev["agent_id"] = "a1"
        b = {"project": "/w/proj", "run_id": "001-x", "state": {"phase": "implement", "status": "active", "approved": {"deps": []}}}
        cfg = paths.DEFAULT_GLOBAL_CONFIG
        return guards.pre_tool_decision(ev, cfg, b, grants)

    def parses(self, cmd, **kw):
        calls = []
        real = guards.CP.inspect

        def counting(argv):
            calls.append(list(argv))
            return real(argv)
        with mock.patch.object(guards.CP, "inspect", counting):
            d = self.decide(cmd, **kw)
        return len(calls), d

    def test_the_orchestrator_parses_nothing_for_plain_commands(self):
        for cmd in ("orkestra status", "orkestra next -p /w/proj", "orkestra task start T001", "orkestra phase next",
                    "orkestra gate run unit && orkestra status", "orkestra context"):
            with self.subTest(cmd=cmd):
                n, d = self.parses(cmd)
                self.assertEqual(n, 0)
                self.assertFalse(d.denied, d.reason[:100])

    def test_tdd_and_evidence_are_parsed_and_their_inner_command_is_still_judged(self):
        n, d = self.parses("orkestra tdd green T001 -- npm test")
        self.assertGreaterEqual(n, 1)
        self.assertFalse(d.denied)
        n, d = self.parses("orkestra tdd green T001 -- git push origin main")
        self.assertTrue(d.denied)
        self.assertEqual(d.guard, "git")
        n, d = self.parses("orkestra evidence add -- rm -rf src")
        self.assertTrue(d.denied)

    def test_rollback_yes_is_parsed_and_still_needs_the_users_word(self):
        n, d = self.parses("orkestra rollback --yes")
        self.assertGreaterEqual(n, 1)
        self.assertEqual((d.denied, d.guard), (True, "rollback"))
        n, d = self.parses("orkestra rollback --yes", grants=("discard",))
        self.assertFalse(d.denied)
        n, d = self.parses("orkestra rollback")                      # the dry run: no --yes, nothing to look up
        self.assertEqual(n, 0)
        self.assertFalse(d.denied)

    def test_control_commands_are_parsed_for_the_driver_check(self):
        n, _ = self.parses("orkestra pause -p /w/proj")
        self.assertGreaterEqual(n, 1)

    def test_agent_roles_are_always_parsed_so_an_unparseable_line_is_still_refused(self):
        for cmd in ("orkestra status", "orkestra phase --no show next", "orkestra -- task start T001", "orkestra bogus-subcommand"):
            with self.subTest(cmd=cmd):
                n, d = self.parses(cmd, agent_type="orkestra:implementer")
                self.assertGreaterEqual(n, 1)
        n, d = self.parses("orkestra bogus-subcommand", agent_type="orkestra:implementer")
        self.assertEqual((d.denied, d.guard), (True, "roles"))
        n, d = self.parses("orkestra phase next", agent_type="orkestra:reviewer")
        self.assertEqual((d.denied, d.guard), (True, "roles"))

    def test_the_cheap_filters_cannot_miss_what_the_parser_would_find(self):
        """The filter says "cannot parse to tdd/evidence/pause/...": for many argument lists the parser must agree."""
        from orkestra_core import cliparse as CP
        self.assertEqual(guards._RUNS_A_COMMAND, tuple(CP.RUNS_A_COMMAND))
        names = ["status", "next", "tdd", "evidence", "pause", "abort", "resume", "unblock", "block", "await", "rollback",
                 "task", "gate", "phase", "plan", "context", "start", "doctor", "help"]
        lists = []
        for n in names:
            lists += [[n], [n, "x"], ["-p", "/w", n], ["--project=/w", n], [n, "--", "ls"], ["--lang", "en", n, "a"],
                      [n, "-p", "/w", "--", "npm", "test"], ["task", n, "T1"], ["--no", n], ["--yes", n], [n + "x"], [n[:3]]]
        lists += [["tdd", "green", "T1", "--", "pause"], ["evidence", "a", "--", "orkestra", "pause"], ["pa"], ["--", "pause"],
                  ["pause=1"], ["--pause"], ["rol", "--yes"], ["rollback", "--yes"], ["task", "note", "T1", "pause"]]
        controls = set(guards.CONTROL_COMMANDS)
        for argv in lists:
            key, rest, _err = CP.inspect(list(argv))
            if rest:
                self.assertTrue(guards._mentions(argv, guards._RUNS_A_COMMAND), argv)
            if key in controls:
                self.assertTrue(guards._mentions(argv, controls), argv)
            if key == "rollback":
                self.assertIn("rollback", argv)


# ====================================================================================== i18n, util

class TestCatalogAreas(unittest.TestCase):
    def setUp(self):
        self._saved = (i18n._catalogs, dict(i18n._by_area))
        i18n._catalogs = None
        i18n._by_area.clear()
        self.addCleanup(self.restore)

    def restore(self):
        i18n._catalogs = self._saved[0]
        i18n._by_area.clear()
        i18n._by_area.update(self._saved[1])

    def test_a_hook_message_loads_only_its_own_area(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"}):
            self.assertNotEqual(i18n.t("dispatch.turn_done"), "dispatch.turn_done")
            self.assertEqual(sorted(i18n._by_area), ["dispatch"])
            self.assertIsNone(i18n._catalogs)
            i18n.t("tempo.dur_s", n=3)
            self.assertEqual(sorted(i18n._by_area), ["dispatch", "tempo"])
            self.assertIsNone(i18n._catalogs)
            for area in ("help", "bridge", "monitor"):
                self.assertNotIn(area, i18n._by_area)

    def test_the_answer_is_the_one_the_full_catalog_gives_for_every_key_in_both_languages(self):
        full = {code: dict(cat) for code, cat in i18n._load().items()}
        i18n._catalogs = None
        i18n._by_area.clear()
        n = 0
        for code in ("en", "tr"):
            with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code}):
                for key, text in full[code].items():
                    n += 1
                    self.assertEqual(i18n.t(key), text, f"{code} {key}")
        self.assertGreater(n, 1000)
        self.assertIsNone(i18n._catalogs)

    def test_an_unknown_key_and_unknown_area_still_answer(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "tr"}):
            self.assertEqual(i18n.t("dispatch.no_such_key_xyz"), "dispatch.no_such_key_xyz")
            self.assertEqual(i18n.t("nowhere.key"), "nowhere.key")
            self.assertEqual(i18n.t("plain"), "plain")

    def test_a_replaced_table_is_still_what_t_reads(self):
        cats = {"en": {"dispatch.turn_done": "EN"}, "tr": {"dispatch.turn_done": "TR"}}
        with mock.patch.object(i18n, "_catalogs", cats), mock.patch.dict(os.environ, {"ORKESTRA_LANG": "tr"}):
            self.assertEqual(i18n.t("dispatch.turn_done"), "TR")

    def test_every_area_module_holds_only_keys_of_its_own_area(self):
        """The invariant the per-area loading rests on."""
        import importlib
        for area in i18n.AREAS:
            mod = importlib.import_module(f"orkestra_core.messages.{area}")
            for code in i18n.SUPPORTED:
                for key in getattr(mod, code.upper(), {}):
                    self.assertEqual(key.partition(".")[0], area, f"{code} {key}")


class TestAtomicWriteWithoutTempfile(unittest.TestCase):
    def test_write_text_atomic_writes_replaces_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "sub" / "a.json"
            util.write_json_atomic(target, {"a": 1})
            util.write_json_atomic(target, {"a": 2, "b": "ü"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 2, "b": "ü"})
            self.assertEqual([p.name for p in target.parent.iterdir()], ["a.json"])

    def test_mkstemp_makes_a_private_unique_file_in_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            made = [util._mkstemp(".tmp-", d) for _ in range(20)]
            try:
                names = {name for _, name in made}
                self.assertEqual(len(names), 20)
                for fd, name in made:
                    self.assertEqual(os.path.dirname(name), os.path.abspath(d))
                    self.assertTrue(os.path.basename(name).startswith(".tmp-"))
                    if POSIX:
                        self.assertEqual(os.stat(name).st_mode & 0o777, 0o600)
            finally:
                for fd, _ in made:
                    os.close(fd)

    def test_a_name_clash_is_retried_and_a_full_clash_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.open
            seen = []

            def clash_once(path, flags, mode=0o777):
                seen.append(path)
                if len(seen) == 1:
                    raise FileExistsError(17, "exists", path)
                return real(path, flags, mode)
            with mock.patch.object(os, "open", clash_once):
                fd, name = util._mkstemp(".tmp-", d)
            os.close(fd)
            self.assertEqual(len(seen), 2)
            with mock.patch.object(os, "open", side_effect=FileExistsError(17, "exists")):
                with self.assertRaises(FileExistsError):
                    util._mkstemp(".tmp-", d)

    def test_plain_classes_keep_the_dataclass_behaviour(self):
        from orkestra_core import planparse, shellparse as sp
        a, b = sp.Segment("git", ["push"]), sp.Segment(program="git", args=["push"])
        self.assertEqual(a, b)
        self.assertNotEqual(a, sp.Segment("git", ["pull"]))
        self.assertEqual(sp.Segment("ls").args, [])
        self.assertIsNot(sp.Segment("ls").args, sp.Segment("ls").args)
        self.assertIn("Segment(program='git'", repr(a))
        self.assertEqual(a.argv(), ["git", "push"])
        g = sp.GitCall("push", ["origin"])
        self.assertEqual((g.globals, g), ([], sp.GitCall("push", ["origin"], [])))
        self.assertEqual(guards.Decision(), guards.ALLOW)
        self.assertEqual(guards.deny("git", "r"), guards.Decision("deny", "r", "git"))
        self.assertTrue(guards.deny("git", "r").denied and not guards.ALLOW.denied)
        t1, t2 = planparse.Task("T1", "a", "b"), planparse.Task(id="T1", title="a", body="b", fields={})
        self.assertEqual(t1, t2)
        self.assertEqual(planparse.Task("T1", "a", "b", {"files": "`x`"}).files, ["x"])
        for obj in (a, g, t1, guards.ALLOW):
            with self.assertRaises(TypeError):
                hash(obj)


# ====================================================================================== the stub

class TestDispatchStub(Sandbox):
    def load(self):
        spec = importlib.util.spec_from_file_location("dispatch_stub_under_test", str(DISPATCH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_names_other_code_and_tests_rely_on_are_all_there(self):
        D = self.load()
        for name in ("HANDLERS", "main", "emit", "log", "binding_for", "run_with_hook_deadline", "deadline_denial", "_Deadline",
                     "utf8_stdio", "on_pre_tool", "on_post_tool", "on_stop", "on_user_prompt", "on_session_start",
                     "apply_markers", "git_context", "escalate_denial", "context_out", "cli_keys", "next_hint", "short_sid",
                     "activity", "guards", "intent", "runs", "snapshot", "tempo", "paths", "sp", "ST", "CP", "BIND_RE",
                     "UNBIND_RE", "GUARD_DEADLINE_SECONDS", "HOOK_CPU_SECONDS", "_ANSWER"):
            self.assertTrue(hasattr(D, name), name)
        self.assertEqual(sorted(D.HANDLERS), sorted(["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
                                                     "SubagentStop", "PostToolUseFailure", "Notification", "StopFailure",
                                                     "SessionEnd"]))

    def test_the_handlers_run_in_the_stubs_namespace_so_patching_it_works(self):
        D = self.load()
        self.assertIs(D.context_out.__globals__, vars(D))
        got = []
        with mock.patch.object(D, "emit", got.append):
            D.context_out("Stop", "hello")
        self.assertEqual(got, [{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "hello"}}])
        logged = []
        with mock.patch.object(D, "log", logged.append), mock.patch.dict(os.environ, self.env):
            with mock.patch.object(D.ST, "heal_binding", side_effect=RuntimeError("boom")):
                (self.home / "sessions" / "x.json").write_text("{}")
                D.binding_for({"session_id": "x"})
        self.assertTrue(any("heal_binding failed: boom" in m for m in logged), logged)

    def test_short_sid_is_the_engines_function(self):
        from orkestra_core import engines
        D = self.load()
        for sid in ("", "abcdefghijkl", "cursor-1234567890abcdef", "copilot-1234567890abcdef"):
            self.assertEqual(D.short_sid(sid), engines.short_sid(sid))

    def test_the_stub_runs_as_a_script_and_reports_its_exit_code(self):
        cp = self.run_hook(DISPATCH, "PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "git push"}))
        self.assertEqual(cp.returncode, 0)
        self.assertIn('"permissionDecision": "deny"', cp.stdout)
        cp = self.run_hook(DISPATCH, "PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
        self.assertEqual((cp.returncode, cp.stdout), (0, ""))

    def test_garbage_on_stdin_fails_open_and_is_logged(self):
        cp = subprocess.run([sys.executable, str(DISPATCH), "PreToolUse"], input="{not json", capture_output=True, text=True,
                            env=self.env)
        self.assertEqual((cp.returncode, cp.stdout), (0, ""))
        self.assertIn("ERROR in PreToolUse", (self.home / "logs" / "hooks.log").read_text(encoding="utf-8"))

    def test_a_read_only_install_keeps_its_bytecode_under_orkestra_home(self):
        if not POSIX or os.geteuid() == 0:
            self.skipTest("needs a non-root POSIX user to make a directory read-only")
        if sys.pycache_prefix:
            self.skipTest("this interpreter already keeps its bytecode elsewhere (pycache_prefix is set)")
        ro = self.root / "ro"
        (ro / "hooks").mkdir(parents=True)
        shutil.copy(DISPATCH, ro / "hooks" / "dispatch.py")
        shutil.copytree(LIB, ro / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for d in [ro / "lib" / "orkestra_core", ro / "lib" / "orkestra_core" / "messages", ro / "lib", ro / "hooks", ro]:
            os.chmod(d, 0o555)
        self.addCleanup(lambda: [os.chmod(p, 0o755) for p in [ro, *ro.rglob("*")] if p.is_dir()])
        cp = self.run_hook(ro / "hooks" / "dispatch.py", "PreToolUse",
                           self.event("PreToolUse", tool_name="Bash", tool_input={"command": "git push"}))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("deny", cp.stdout)
        cached = list((self.home / "pycache").rglob("*.pyc"))
        self.assertTrue(any("hookmain" in p.name for p in cached) and any("guards" in p.name for p in cached), cached[:5])
        self.assertFalse(list((ro / "lib").rglob("*.pyc")))

    def test_a_writable_install_keeps_using_its_own_pycache(self):
        cp = self.run_hook(DISPATCH, "PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
        self.assertEqual(cp.returncode, 0)
        self.assertFalse((self.home / "pycache").exists())

    def test_a_users_own_pycache_prefix_wins(self):
        mine = self.root / "mine"
        env = dict(self.env, PYTHONPYCACHEPREFIX=str(mine))
        cp = self.run_hook(DISPATCH, "PreToolUse", self.event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), env=env)
        self.assertEqual(cp.returncode, 0)
        self.assertFalse((self.home / "pycache").exists())


class TestSessionStartStartsTheFastWatcher(Sandbox):
    def load(self):
        spec = importlib.util.spec_from_file_location("dispatch_watcher_under_test", str(DISPATCH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def start(self, D, env, **ev):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
            D.on_session_start(self.event("SessionStart", source="startup", **ev))
        return out.getvalue()

    def env_with_daemon(self):
        env = dict(self.env)
        env.pop("ORKESTRA_NO_DAEMON")
        return env

    def test_an_unbound_session_in_a_project_asks_for_a_watcher(self):
        from orkestra_core import fastwatch
        D = self.load()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(fastwatch, "ensure_running") as ensure:
            os.environ.pop("ORKESTRA_NO_DAEMON")
            self.start(D, {})
        ensure.assert_called_once()
        self.assertEqual(Path(ensure.call_args[0][0]).resolve(), self.proj.resolve())

    def test_a_bound_session_asks_for_a_watcher_with_its_project(self):
        from orkestra_core import fastwatch
        self.bind()
        D = self.load()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(fastwatch, "ensure_running") as ensure:
            os.environ.pop("ORKESTRA_NO_DAEMON")
            out = self.start(D, {})
        ensure.assert_called_once()
        self.assertEqual(Path(ensure.call_args[0][0]), self.proj)
        self.assertIn("Active run 001-x", out)                     # the context is still delivered

    def test_the_switch_that_turns_the_daemon_off_skips_the_call(self):
        from orkestra_core import fastwatch
        D = self.load()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(fastwatch, "ensure_running") as ensure:
            self.start(D, {})
        ensure.assert_not_called()

    def test_a_watcher_that_fails_never_breaks_session_start(self):
        from orkestra_core import fastwatch
        self.bind()
        D = self.load()
        logged = []
        with mock.patch.dict(os.environ, self.env), mock.patch.object(D, "log", logged.append), \
                mock.patch.object(fastwatch, "ensure_running", side_effect=RuntimeError("no daemon")):
            os.environ.pop("ORKESTRA_NO_DAEMON")
            out = self.start(D, {})
        self.assertIn("Active run 001-x", out)
        self.assertTrue(any("ensure_running failed: no daemon" in m for m in logged), logged)


# ====================================================================================== the launcher command

@unittest.skipUnless(POSIX and shutil.which("sh"), "needs a POSIX shell")
class TestHooksJsonLauncher(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="orkestra-launch-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "sh.log"
        self.write("python3", '#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho "RAN python3 $*"\n')
        # a `sh` found by NAME is a second shell: the direct path must never need one
        self.write("sh", f'#!/bin/sh\necho "SH $*" >> "{self.log}"\nexec /bin/sh "$@"\n')
        self.doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    def write(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def commands(self):
        for event, groups in self.doc["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    yield event, hook["command"]

    def run_command(self, command, **env):
        full = {"PATH": f"{self.bin}:/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "HOME": str(self.root)}
        full.update(env)
        if self.log.exists():
            self.log.unlink()
        cp = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=full, timeout=30)
        return cp, (self.log.read_text() if self.log.exists() else "")

    def test_on_posix_with_python3_the_hook_is_exec_d_directly(self):
        n = 0
        for event, command in self.commands():
            n += 1
            cp, shells = self.run_command(command)
            self.assertEqual(cp.stdout.strip(), f"RAN python3 {PLUGIN}/hooks/dispatch.py {event}", cp.stderr)
            self.assertEqual(shells, "", f"{event}: the launcher (a second sh) ran")
        self.assertEqual(n, 10)

    def test_without_python3_it_falls_back_to_the_launcher_and_its_message(self):
        (self.bin / "python3").unlink()
        cp, shells = self.run_command(next(c for e, c in self.commands() if e == "Stop"), PATH=str(self.bin))
        self.assertIn("run_hook.sh", shells)
        self.assertEqual(cp.returncode, 127)
        self.assertIn("no Python 3.9+ found", cp.stderr)

    def test_windows_git_bash_keeps_using_the_launcher(self):
        for marker in ({"OS": "Windows_NT"}, {"MSYSTEM": "MINGW64"}):
            cp, shells = self.run_command(next(c for e, c in self.commands() if e == "Stop"), **marker)
            self.assertIn("run_hook.sh", shells, marker)

    def test_the_command_survives_a_plugin_path_with_spaces(self):
        spaced = self.root / "My Plugin"
        (spaced / "hooks").mkdir(parents=True)
        cp, shells = self.run_command(next(c for e, c in self.commands() if e == "Stop"), CLAUDE_PLUGIN_ROOT=str(spaced))
        self.assertEqual(cp.stdout.strip(), f"RAN python3 {spaced}/hooks/dispatch.py Stop")
        self.assertEqual(shells, "")


# ====================================================================================== CPU

def _bare_python_cpu_ms(env):
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    subprocess.run([sys.executable, "-c", "pass"], env=env, capture_output=True)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime) * 1000


@unittest.skipUnless(POSIX and resource is not None, "per-child CPU accounting needs POSIX getrusage")
class TestHookCpu(Sandbox):
    """Cold-process CPU, old entry script vs the current one, interleaved so a busy machine hits both the same way."""
    ROUNDS = 15
    MIN_SAVING = 0.35

    def setUp(self):
        super().setUp()
        self.old = self.root / "old" / "dispatch.py"
        self.old.parent.mkdir()
        text = BASELINE.read_text(encoding="utf-8")
        self.assertEqual(text.count(LEGACY_IMPORTS_AFTER), 1)
        self.old.write_text(text.replace(LEGACY_IMPORTS_AFTER, LEGACY_IMPORTS), encoding="utf-8")
        self.env["ORKESTRA_TEST_LIB"] = str(LIB)

    def cpu_ms(self, entry, name, payload):
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cp = self.run_hook(entry, name, payload)
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.assertEqual(cp.returncode, 0, cp.stderr[-300:])
        return (r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime) * 1000, cp.stdout

    def measure(self, name, payload):
        for entry in (self.old, DISPATCH, DISPATCH):                  # warm the bytecode caches of both
            self.cpu_ms(entry, name, payload)
        old, new, bare = [], [], []
        for _ in range(self.ROUNDS):
            old.append(self.cpu_ms(self.old, name, payload)[0])
            new.append(self.cpu_ms(DISPATCH, name, payload)[0])
            bare.append(_bare_python_cpu_ms(self.env))
        return statistics.median(old), statistics.median(new), statistics.median(bare)

    def factor(self):
        before = helpers._reference_cpu()
        return min(helpers.REF_CAP, max(1.0, max(before, helpers._reference_cpu()) / helpers.REF_NOMINAL))

    def check(self, label, name, payload, absolute_ms, vs_bare):
        old, new, bare = self.measure(name, payload)
        report = f"{label}: pre-change {old:.1f} ms, now {new:.1f} ms, bare python {bare:.1f} ms"
        print("\n    " + report, file=sys.stderr)
        self.assertLess(new, absolute_ms * self.factor(), "absolute CPU budget (scaled by the reference loop). " + report)
        if os.environ.get("ORKESTRA_PERF") != "1":
            # The two ratios compare short process runs of a shared CI runner (cold caches, noisy neighbours, CPU
            # frequency steps); they are exact on a quiet machine only. Run them with ORKESTRA_PERF=1.
            return
        self.assertLess(new, old * (1 - self.MIN_SAVING), "at least 35 % cheaper than the pre-change entry. " + report)
        self.assertLess(new, bare * vs_bare, "close to the interpreter's own start-up. " + report)

    def test_a_guarded_pre_hook_is_at_least_35_percent_cheaper(self):
        self.check("PreToolUse Bash", "PreToolUse",
                   self.event("PreToolUse", tool_name="Bash", tool_input={"command": "cd src && grep -rn TODO . | head -5"}),
                   absolute_ms=75, vs_bare=3.0)

    def test_an_unbound_no_op_hook_is_at_least_35_percent_cheaper(self):
        self.check("Stop (no run)", "Stop", self.event("Stop"), absolute_ms=55, vs_bare=2.4)

    def test_an_unbound_post_edit_hook_is_at_least_35_percent_cheaper(self):
        self.check("PostToolUse Edit (no run)", "PostToolUse",
                   self.event("PostToolUse", tool_name="Edit", tool_input={"file_path": str(self.proj / "a.ts")},
                              tool_response={"success": True}), absolute_ms=55, vs_bare=2.4)

    def test_the_old_entry_and_the_new_one_give_the_same_answer(self):
        """The baseline measures the same job: same output for a denied and an allowed call."""
        for cmd in ("git push origin main", "ls -la"):
            payload = self.event("PreToolUse", tool_name="Bash", tool_input={"command": cmd})
            old, new = (json.loads(self.cpu_ms(e, "PreToolUse", payload)[1] or "{}") for e in (self.old, DISPATCH))
            first = lambda o: (o.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "").split("\n")[0]  # noqa: E731
            self.assertEqual(first(old), first(new))                  # (the second identical denial adds a "denied 2x" line)
            self.assertEqual(bool(old), bool(new))


if __name__ == "__main__":
    unittest.main()
