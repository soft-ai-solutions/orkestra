"""Guard fixes and the two tempo guards (wait, dialogs) — every case below comes from a real run on 2026-10-04."""
import copy
import os
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path)
from orkestra_core import guards
from orkestra_core import shellparse as sp
from orkestra_core.paths import DEFAULT_GLOBAL_CONFIG

CFG = copy.deepcopy(DEFAULT_GLOBAL_CONFIG)


def bash(cmd, cwd="/w/proj", agent=None):
    ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}
    if agent:
        ev["agent_type"] = agent
    return ev


def binding(phase="implement", status="active", project="/w/proj"):
    return {"project": project, "run_id": "001-x", "state": {"phase": phase, "status": status, "approved": {"deps": []}}}


def decide(ev, b=None, cfg=CFG):
    return guards.pre_tool_decision(ev, cfg, b, ())


class TestParserFixes(unittest.TestCase):
    def test_fd_number_is_not_an_argument(self):
        seg = sp.segments("orkestra --help 2>&1 | head")[0]
        self.assertEqual(seg.args, ["--help"])
        self.assertEqual(guards._cli_subcommand(seg), "help")
        self.assertEqual(sp.segments("npm i -D ts-node 2>&1 | tail -3")[0].args, ["i", "-D", "ts-node"])
        self.assertEqual(sp.segments("cmd 2>/dev/null 1>>log")[0].redirects, [(">", "/dev/null"), (">>", "log")])
        # a spaced number is a real argument; quoted text is untouched
        self.assertEqual(sp.segments("echo 2 > f.txt")[0].args, ["2"])
        self.assertEqual(sp.segments("echo '2>&1'")[0].args, ["2>&1"])

    def test_readonly_agent_may_ask_for_help_and_next(self):
        for cmd in ("orkestra --help 2>&1 | head -40", "orkestra next -p /w/proj", "orkestra task list -p /w/proj",
                    "orkestra integrity check -p /w/proj", "orkestra phase check -p /w/proj"):
            self.assertFalse(decide(bash(cmd, agent="orkestra:reviewer"), binding()).denied, cmd)
        self.assertTrue(decide(bash("orkestra task done T001", agent="orkestra:reviewer"), binding()).denied)
        self.assertTrue(decide(bash("orkestra phase next", agent="orkestra:reviewer"), binding()).denied)

    def test_deps_guard_ignores_redirect_fd(self):
        b = binding()
        b["state"]["approved"]["deps"] = ["ts-node"]
        self.assertFalse(decide(bash("npm i -D ts-node 2>&1 | tail -3"), b).denied)

    def test_bsd_sed_in_place_targets_the_file_not_the_script(self):
        seg = sp.segments("sed -i '' '/not.toContain(\"ab\");/d' __tests__/a.test.ts")[0]
        self.assertEqual(guards.bash_write_targets(seg, "/w/proj"), ["/w/proj/__tests__/a.test.ts"])
        self.assertFalse(decide(bash("sed -i '' '/@ts-expect-error duplex is required/d' src/a.ts",
                                     agent="orkestra:implementer"), binding()).denied)
        # GNU form still works, and a real outside write is still blocked
        self.assertEqual(guards.bash_write_targets(sp.segments("sed -i 's/a/b/' x.py")[0], "/w/proj"), ["/w/proj/x.py"])
        self.assertTrue(decide(bash("sed -i '' 's/a/b/' /w/other/x.py"), binding()).denied)

    def test_globbed_temp_path_is_temp(self):
        self.assertFalse(decide(bash("rm -rf /private/tmp/m14/*", agent="orkestra:implementer"), binding()).denied)
        self.assertFalse(decide(bash("rm -rf /tmp/rev-T043/*", agent="orkestra:reviewer"), binding()).denied)
        self.assertTrue(decide(bash("rm -rf /w/other/*"), binding()).denied)

    def test_windows_temp_paths_are_temp(self):
        """FR-006 / E9: TEMP, TMP, LOCALAPPDATA\\Temp are temp roots (E10 /tmp still covered above)."""
        env = {
            "TEMP": r"C:\Users\ci\AppData\Local\Temp",
            "TMP": r"C:\Users\ci\AppData\Local\Temp",
            "LOCALAPPDATA": r"C:\Users\ci\AppData\Local",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(guards._is_temp(r"C:\Users\ci\AppData\Local\Temp\orkestra-x\a"))
            self.assertTrue(guards._is_temp(r"C:\Users\ci\AppData\Local\Temp\probe"))
            self.assertTrue(guards._is_temp_subdir(r"C:\Users\ci\AppData\Local\Temp\scratch"))
            # E10 regression: POSIX roots untouched
            self.assertTrue(guards._is_temp("/tmp/x"))
            self.assertTrue(guards._is_temp("/private/tmp/y"))

    @unittest.skipIf(os.name == "nt", "a backslash is a path separator on Windows")
    def test_a_backslash_in_a_posix_file_name_does_not_make_it_temp(self):
        """On POSIX `/tmp\\evil` is a file called `tmp\\evil` directly under `/`, not a file inside /tmp."""
        for p in ("/tmp\\evil", "/private/tmp\\x", "/var/folders\\x", "/tmp\\evil/sub"):
            with self.subTest(path=p):
                self.assertFalse(guards._is_temp(p))
                self.assertFalse(guards._is_temp_subdir(p))
        for tool, ti in (("Write", {"file_path": "/tmp\\evil", "content": "x"}),
                         ("Bash", {"command": "rm -rf '/tmp\\evil'/*"}),
                         ("Write", {"file_path": "/var/folders\\evil", "content": "x"})):
            with self.subTest(tool=tool, input=ti):
                d = guards.pre_tool_decision({"tool_name": tool, "tool_input": ti, "cwd": "/w/proj"}, CFG, binding(), ())
                self.assertTrue(d.denied, ti)
        with mock.patch.dict(os.environ, {"TEMP": "C:\\Users\\ci\\AppData\\Local\\Temp"}, clear=False):
            self.assertTrue(guards._is_temp("C:\\Users\\ci\\AppData\\Local\\Temp\\a"))     # drive paths still normalise

    def test_windows_orkestra_path_markers(self):
        """FR-009 / E11: backslash `.orkestra` paths match the same class as POSIX."""
        from orkestra_core import hints
        win_cwd = r"C:\proj\.orkestra\runs\001-x"
        self.assertIn("/.orkestra/", guards._norm_path_markers(win_cwd))
        self.assertTrue("/.orkestra/runs" in (guards._norm_path_markers(win_cwd).casefold() + "/"))
        # hints drops run-artifact paths using sep-normalized membership
        got = hints.playbooks_for(
            [r"C:\proj\.orkestra\runs\001-x\evidence\log.txt", r"C:\proj\src\auth.ts"],
            Path("/w/proj"),
        )
        self.assertIn("security", got)
        self.assertEqual(
            hints.playbooks_for([r"C:\proj\.orkestra\runs\001-x\evidence\log.txt"], Path("/w/proj")),
            [],
        )

    def test_recursive_project_deletion_is_blocked_but_plan_commands_stay_allowed(self):
        for cmd in ("rm -rf src", "rm -r -f src", "rm --recursive --force src", "command rm -rf src",
                    "env rm -rf src", "bash -c 'rm -rf src'", "bash -lc 'rm -rf src'"):
            d = decide(bash(cmd), binding())
            self.assertTrue(d.denied and d.guard == "destructive", cmd)
        self.assertFalse(decide(bash("rm src/obsolete.ts"), binding()).denied)
        self.assertFalse(decide(bash("rm -rf /tmp/orkestra-probe/*"), binding()).denied)
        self.assertFalse(decide(bash("npm test"), binding()).denied)

    def test_scripts_may_read_run_state_but_not_write_it(self):
        b = binding()
        read = "python3 -c \"print(open('.orkestra/runs/001-x/evidence/T001-red-1.log').read()[-400:])\" 2>/dev/null"
        self.assertFalse(decide(bash(read, agent="orkestra:reviewer"), b).denied)
        for w in ("python3 -c \"open('.orkestra/runs/001-x/state.json','w').write('{}')\"",
                  "node -e \"require('fs').writeFileSync('.orkestra/runs/001-x/approval.json','{}')\"",
                  "python3 -c \"import os; os.replace('a', '.orkestra/runs/001-x/state.json')\""):
            self.assertTrue(decide(bash(w), b).denied, w)
        gates = ("python3 -c \"import json; d=json.load(open('.orkestra/runs/001-x/gates-verify.json')); "
                 "print(sorted(d))\"")
        self.assertFalse(decide(bash(gates), b).denied)
        self.assertTrue(decide(bash("python3 -c \"import orkestra_core\""), b).denied)

    def test_rm_of_a_literal_temp_variable_is_allowed(self):
        b = binding()
        ok = 'STATE=/tmp/sakabaz-wrangler-sc006; rm -rf "$STATE"'
        self.assertFalse(decide(bash(ok), b).denied)
        self.assertTrue(decide(bash('rm -rf "$STATE"'), b).denied)
        self.assertTrue(decide(bash("rm -rf src"), b).denied)

    def test_readonly_denial_tells_where_to_experiment(self):
        d = decide(bash("cp src/a.ts src/a.ts.bak", agent="orkestra:reviewer"), binding())
        self.assertTrue(d.denied)
        self.assertIn("/tmp/", d.reason)


class TestWaitGuard(unittest.TestCase):
    """The main session must not sleep/poll for subagent reports (29 % of game-app's wall time was this)."""

    POLLS = [
        ('for i in $(seq 1 100); do s=$(orkestra status -p . 2>&1 | grep -E "^ *T044 "); echo "$s" | grep -q '
         '"evidence=RG" && break; sleep 5; done; echo "$s"', "/w/proj"),
        ('until orkestra status -p . 2>&1 | grep -q "T012 .*evidence=RG"; do sleep 2; done; sed -n 1,5p '
         '.orkestra/current/tasks/T012.report.md', "/w/proj"),
        ("cd /w/proj/.orkestra/current && sleep 20; sed -n '/^## Round 2/,$p' reviews/T010.md", "/w/proj"),
        ('sleep 15; grep -E "VERDICT" reviews/T011.md', "/w/proj/.orkestra/current"),
        ("python3 - <<'EOF'\nimport os, time\nt0=time.time(); p='T016.report.md'\nwhile time.time()-t0<590:\n"
         "    time.sleep(5)\nEOF", "/w/proj/.orkestra/current/tasks"),
    ]
    WAITS = [       # waiting as such: only wrong while a subagent is registered as running
        ("W=/private/tmp/claude-501/x/scratchpad/waitany.sh; sleep 90; echo waited", "/w/proj"),
        ("until grep -q '^EXIT' /private/tmp/claude-501/x/tasks/bfrn7po72.output 2>/dev/null; do sleep 5; done", "/w/proj"),
        ("sleep 45", "/w/proj"),
        ("sleep 300; ls", "/w/proj"),
        ("python3 -c 'import time; time.sleep(600)'", "/w/proj"),
    ]
    ALLOW = [
        'pkill -f "next start -p 3499" ; sleep 1; curl -s -o /dev/null http://localhost:3499/login; '
        "cat > .orkestra/current/runtime-check.md <<'EOF'\nwaiting for the server while it boots, done\nEOF",
        "npm run dev & sleep 15; curl -s localhost:3000 | head",
        "npx vitest run tests/a.test.ts",
        "orkestra gate verify -p .",
        "sleep 5; orkestra status -p .",
        'echo "waiting for the reviewer"; orkestra next -p .',
        "for f in src/*.ts; do wc -l $f; done",
        # app code that merely has "reviews/" in its path is not a run artifact (review finding 10)
        "sleep 15 && cat app/routes/reviews/index.tsx",
        "for i in 1 2 3; do curl -sf localhost:3000/api/reviews/ && break; sleep 2; done",
    ]

    def setUp(self):
        import os, tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-wait-")).resolve()
        self.proj = self.tmp / "proj"
        (self.proj / ".orkestra" / "runs" / "001-x").mkdir(parents=True)
        self._home = os.environ.get("ORKESTRA_HOME")
        os.environ["ORKESTRA_HOME"] = str(self.tmp / "home")

    def tearDown(self):
        import os, shutil
        if self._home is None:
            os.environ.pop("ORKESTRA_HOME", None)
        else:
            os.environ["ORKESTRA_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def b(self, **kw):
        return binding(project=str(self.proj), **kw)

    def at(self, cwd):
        return cwd.replace("/w/proj", str(self.proj))

    def agent_running(self):
        from orkestra_core import activity as A
        A.agent_launched(self.proj, "001-x", {"id": "a1", "type": "orkestra:implementer", "name": "", "desc": "T001", "tasks": ["T001"]})

    def test_polling_the_run_is_denied_and_says_what_to_do_instead(self):
        for cmd, cwd in self.POLLS:
            d = decide(bash(cmd, cwd=self.at(cwd)), self.b())
            self.assertTrue(d.denied and d.guard == "wait", cmd)
            self.assertIn("orkestra next", d.reason)           # nothing registered: waiting changes nothing
        self.agent_running()
        for cmd, cwd in self.POLLS:
            d = decide(bash(cmd, cwd=self.at(cwd)), self.b())
            self.assertTrue(d.denied and d.guard == "wait", cmd)
            self.assertIn("End your turn", d.reason)

    def test_plain_waits_are_denied_only_while_a_subagent_runs(self):
        for cmd, cwd in self.WAITS:
            self.assertFalse(decide(bash(cmd, cwd=self.at(cwd)), self.b()).denied, cmd)    # e.g. a dev server booting
        self.agent_running()
        for cmd, cwd in self.WAITS:
            d = decide(bash(cmd, cwd=self.at(cwd)), self.b())
            self.assertTrue(d.denied and d.guard == "wait", cmd)
            self.assertIn("End your turn", d.reason)

    def test_ordinary_commands_and_short_sleeps_are_allowed(self):
        for cmd in self.ALLOW:
            self.assertFalse(decide(bash(cmd, cwd=str(self.proj)), self.b()).denied, cmd)
        self.agent_running()
        for cmd in self.ALLOW:
            self.assertFalse(decide(bash(cmd, cwd=str(self.proj)), self.b()).denied, cmd)

    def test_scope_is_the_main_session_of_an_active_run(self):
        cmd, cwd = self.POLLS[1]
        cwd = self.at(cwd)
        self.assertFalse(decide(bash(cmd, cwd=cwd, agent="orkestra:implementer"), self.b()).denied)   # subagent
        self.assertFalse(decide(bash(cmd, cwd=cwd), None).denied)                                      # no run
        self.assertFalse(decide(bash(cmd, cwd=cwd), self.b(status="awaiting_user")).denied)
        off = copy.deepcopy(CFG)
        off["guards"]["wait"] = False
        self.assertFalse(decide(bash(cmd, cwd=cwd), self.b(), off).denied)

    def test_guard_stays_fast_on_hostile_input(self):
        """The old loop regex was super-linear: 17 KB of `while do sleep 1` took 3.7 s inside the hook."""
        cmd = "echo " + "while do sleep 1 " * 3000
        helpers.assert_cpu_under(self, 1.0, lambda: decide(bash(cmd, cwd=str(self.proj)), self.b()))


class TestDialogGuard(unittest.TestCase):
    def test_commands_that_open_a_system_dialog(self):
        for cmd in ("security show-keychain-info build.keychain", "security unlock-keychain build.keychain",
                    "security find-generic-password -s svc -w", "osascript -e 'display dialog \"hi\"'",
                    "xcode-select --install", "bash -c 'security show-keychain-info $KC'"):
            for agent in (None, "orkestra:implementer", "orkestra:reviewer"):
                d = decide(bash(cmd, agent=agent), binding())
                self.assertTrue(d.denied and d.guard == "dialogs", (cmd, agent))

    def test_non_interactive_forms_are_allowed(self):
        for cmd in ('security unlock-keychain -p "$PW" build.keychain', "security find-identity -v -p codesigning",
                    'security create-keychain -p "" /tmp/t.keychain', "security list-keychains",
                    "osascript -e 'tell application \"Simulator\" to activate'"):
            self.assertFalse(decide(bash(cmd, agent="orkestra:implementer"), binding()).denied, cmd)

    def test_only_during_a_run_and_switchable(self):
        self.assertFalse(decide(bash("security show-keychain-info x"), None).denied)
        off = copy.deepcopy(CFG)
        off["guards"]["dialogs"] = False
        self.assertFalse(decide(bash("security show-keychain-info x"), binding(), off).denied)


class TestPowerShellTool(unittest.TestCase):
    """Claude Code's PowerShell tool (Windows) is a shell: it must meet the same guards as Bash."""

    def test_normalize_event_maps_powershell_to_bash(self):
        ev = {"tool_name": "PowerShell", "tool_input": {"command": "git status"}, "cwd": "/w/proj"}
        out = guards.normalize_event(ev)
        self.assertEqual(out["tool_name"], "Bash")
        self.assertEqual(out["tool_input"], {"command": "git status"})
        self.assertEqual(ev["tool_name"], "PowerShell")                # the caller's event is not mutated

    def test_a_git_commit_through_powershell_is_denied_like_bash(self):
        bash_ev = bash("git commit -m x")
        ps_ev = dict(bash_ev, tool_name="PowerShell")
        self.assertTrue(decide(bash_ev).denied)
        d = decide(ps_ev)
        self.assertTrue(d.denied)
        self.assertEqual(d.guard, decide(bash_ev).guard)

    def test_the_plugin_matchers_name_powershell(self):
        import json
        doc = json.loads((Path(guards.__file__).resolve().parents[2] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            self.assertIn("PowerShell", doc["hooks"][event][0]["matcher"].split("|"), event)


if __name__ == "__main__":
    unittest.main()
