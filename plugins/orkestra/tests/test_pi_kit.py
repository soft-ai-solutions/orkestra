"""Pi install/status/uninstall and the short interactive prompt."""
from __future__ import annotations

import argparse
import io
from unittest import mock

import helpers
from orkestra_core import cli as CLI
from orkestra_core import engines as EN
from orkestra_core import pikit as PK
from orkestra_core.state import OrkestraError


class TestPiKit(helpers.Sandbox):
    def test_dry_run_writes_nothing(self) -> None:
        actions = PK.install(dry=True)
        self.assertTrue(actions)
        self.assertFalse(PK.skills_dir().exists())

    def test_install_is_idempotent_and_uninstall_keeps_a_foreign_skill(self) -> None:
        foreign = PK.skills_dir() / "mine"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("user\n", encoding="utf-8")
        first = PK.install()
        self.assertTrue(first)
        self.assertTrue(PK.skills_installed())
        skill = PK.skills_dir() / "orkestra-run" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertIn("Pi note:", text)
        self.assertIn("name: orkestra-run", text)
        self.assertIn("tool_call", text)
        self.assertNotIn("does not install a tool hook", text)
        guard = PK.extension_path()
        self.assertTrue(guard.is_file())
        self.assertIn("tool_call", guard.read_text(encoding="utf-8"))
        self.assertIn("block: true", guard.read_text(encoding="utf-8"))
        self.assertEqual(PK.install(), [])
        self.assertTrue(PK.uninstall())
        self.assertFalse(PK.skills_installed())
        self.assertEqual(PK.uninstall(), [])
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "user\n")

    def test_install_does_not_adopt_an_unmarked_directory(self) -> None:
        directory = PK.skills_dir() / "orkestra-run"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("mine\n", encoding="utf-8")
        PK.install()
        self.assertEqual((directory / "SKILL.md").read_text(encoding="utf-8"), "mine\n")
        self.assertFalse((directory / PK.MARKER).exists())
        self.assertFalse(PK.skills_installed())
        removed = PK.uninstall()
        self.assertTrue(all(not str(item).endswith("/orkestra-run") and "skills/orkestra-run" not in str(item)
                            for item in removed))
        self.assertEqual((directory / "SKILL.md").read_text(encoding="utf-8"), "mine\n")

    def test_status_exit_ignores_a_missing_binary(self) -> None:
        self.assertFalse(PK.skills_installed())
        PK.install()
        with mock.patch("shutil.which", return_value=None):
            rows = {label: ok for ok, label, _detail in PK.status()}
        self.assertTrue(rows["pi skills"])
        self.assertIsNone(rows["pi"])

    def test_run_prompt_is_the_short_skill_command(self) -> None:
        prompt = EN.run_prompt("pi", "add a line")
        self.assertTrue(prompt.startswith("/skill:orkestra-run "))
        self.assertLess(len(prompt), 200)
        self.assertNotIn("orkestra playbook", prompt)
        cursor = EN.run_prompt("cursor", "add a line")
        self.assertGreater(len(cursor), len(prompt))
        self.assertIn("orkestra playbook", cursor)
        resume = EN.resume_prompt("pi", "demo")
        self.assertEqual(resume, "/skill:orkestra-resume demo")
        self.assertNotIn("orkestra playbook", resume)

    def test_interactive_prompt_that_starts_with_a_dash_is_not_an_option(self) -> None:
        argv = EN.interactive_command("pi", "/bin/pi", "- x", {"session_id": "S1"})
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "S1")
        self.assertIn("--extension", argv)
        self.assertEqual(argv[-2:], ["--", "- x"])

    def test_run_refuses_until_the_skills_are_installed_then_prints_the_short_command(self) -> None:
        project = self.make_project("demo")
        ns = argparse.Namespace(project=str(project), request=["add", "a", "line"], engine="pi",
                                approval_mode=None, print=True)
        with mock.patch.object(CLI, "_ASKER", None), mock.patch.object(EN, "binary", return_value="/bin/pi"):
            with self.assertRaises(OrkestraError) as cm:
                CLI.cmd_run(ns)
        self.assertIn("orkestra pi install", str(cm.exception))
        PK.install()
        buf = io.StringIO()
        with mock.patch.object(CLI, "_ASKER", None), mock.patch.object(EN, "binary", return_value="/bin/pi"), \
                mock.patch("sys.stdout", buf):
            code = CLI.cmd_run(ns)
        self.assertEqual(code, 0)
        self.assertIn("/skill:orkestra-run", buf.getvalue())
        self.assertIn("--extension", buf.getvalue())
        self.assertIn("--session-id", buf.getvalue())
        self.assertFalse((project / ".orkestra").exists())

    def test_unbound_call_is_allowed_and_bad_json_blocks(self) -> None:
        import importlib.util
        path = __import__("pathlib").Path(__file__).resolve().parents[1] / "hooks" / "pi_hook.py"
        spec = importlib.util.spec_from_file_location("pi_hook", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        allowed = mod.decide({"event": "tool_call", "toolName": "bash", "input": {"command": "echo hi"},
                              "sessionId": "not-bound"})
        self.assertFalse(allowed.get("block"))
        blocked = mod.decide_raw(b"not-json")
        self.assertTrue(blocked.get("block"))

    def test_bound_force_push_is_blocked(self) -> None:
        import importlib.util
        import json
        from orkestra_core import state as ST
        path = __import__("pathlib").Path(__file__).resolve().parents[1] / "hooks" / "pi_hook.py"
        spec = importlib.util.spec_from_file_location("pi_hook_bound", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        project = self.make_project("demo")
        rid = "011-pi-hook"
        run = project / ".orkestra" / "runs" / rid
        run.mkdir(parents=True)
        (run / "state.json").write_text(json.dumps(ST.new_state(project, rid, "t", "S", "interactive", "pi")),
                                        encoding="utf-8")
        self.assertTrue(ST.bind_session(EN.session_key("pi", "S1"), project, rid, engine="pi"))
        out = mod.decide({"event": "tool_call", "toolName": "bash",
                          "input": {"command": "git push --force origin main"}, "sessionId": "S1",
                          "cwd": str(project)})
        self.assertTrue(out.get("block"))
        self.assertTrue(out.get("reason"))

    def test_tool_result_marker_binds_the_session(self) -> None:
        import importlib.util
        import json
        from orkestra_core import state as ST
        path = __import__("pathlib").Path(__file__).resolve().parents[1] / "hooks" / "pi_hook.py"
        spec = importlib.util.spec_from_file_location("pi_hook_marker", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        project = self.make_project("demo")
        rid = "011-marker"
        run = project / ".orkestra" / "runs" / rid
        run.mkdir(parents=True)
        (run / "state.json").write_text(json.dumps(ST.new_state(project, rid, "t", "S", "headless", "pi")),
                                        encoding="utf-8")
        marker = f"[orkestra:bind] project={project} run={rid}\n"
        mod.decide({"event": "tool_result", "toolName": "bash", "input": {"command": "orkestra start"},
                    "output": marker, "sessionId": "S9", "cwd": str(project)})
        self.assertIsNotNone(ST.session_binding(EN.session_key("pi", "S9")))
        blocked = mod.decide({"event": "tool_call", "toolName": "bash",
                              "input": {"command": "git push --force origin main"}, "sessionId": "S9",
                              "cwd": str(project)})
        self.assertTrue(blocked.get("block"))

    def test_pi_next_and_stop_keep_going_while_claude_still_ends_the_turn(self) -> None:
        import importlib.util
        import json
        import time
        from orkestra_core import state as ST
        from orkestra_core import tempo as T
        path = __import__("pathlib").Path(__file__).resolve().parents[1] / "hooks" / "pi_hook.py"
        spec = importlib.util.spec_from_file_location("pi_hook_flow", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        project = self.make_project("demo")
        rid = "011-flow"
        run = project / ".orkestra" / "runs" / rid
        run.mkdir(parents=True)
        pending = {"pending": {"a1": {"id": "a1", "type": "orkestra:implementer", "at": time.time()}}}
        (run / "agents.json").write_text(json.dumps(pending), encoding="utf-8")
        pi_state = ST.new_state(project, rid, "t", "S", "interactive", "pi")
        (run / "state.json").write_text(json.dumps(pi_state), encoding="utf-8")
        pi_next = T.next_action(project, pi_state)["text"]
        self.assertNotIn("END YOUR TURN", pi_next)
        self.assertIn("Do not poll", pi_next)
        claude_state = dict(pi_state, engine="claude")
        (run / "state.json").write_text(json.dumps(claude_state), encoding="utf-8")
        self.assertIn("END YOUR TURN", T.next_action(project, claude_state)["text"])
        (run / "state.json").write_text(json.dumps(pi_state), encoding="utf-8")
        sid = EN.session_key("pi", "S1")
        self.assertTrue(ST.bind_session(sid, project, rid, engine="pi"))
        with (run / "activity.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "kind": "bind", "sid": sid, "agent": "main"}) + "\n")
        settled = mod.decide({"event": "before_settle", "sessionId": "S1", "cwd": str(project)})
        self.assertTrue(settled.get("continue"))
        self.assertTrue(settled.get("message"))
