import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import helpers
from orkestra_core import state as ST
from orkestra_core import engines as EN
from test_flow import FlowBase

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "hooks"))

import copilot_hook


class TestCopilotHookAdapter(FlowBase):
    def copilot(self, event_name, **fields):
        payload = {"hook_event_name": event_name, "session_id": self.sid, "cwd": str(self.proj), **fields}
        return copilot_hook.handle_event(event_name, payload)

    def start(self, title="Subtract", approval_mode="manual"):
        out = self.cli("start", "-p", "demo", "--title", title, "--request", "sub fonksiyonu ekle", "--size", "S",
                       "--engine", "copilot", "--approval-mode", approval_mode).stdout
        self.rid = out.split("run=")[-1].split()[0]
        self.fdir = self.proj / ".orkestra" / "features" / self.rid
        self.copilot("PostToolUse", tool_name="bash", tool_input={"command": "orkestra start"},
                     tool_result={"result_type": "success", "text_result_for_llm": out})
        return out

    def approve(self, text="onaylıyorum"):
        return self.copilot("UserPromptSubmit", prompt=text, prompt_id="p-approve")

    def test_pre_tool_denial_uses_copilot_output_shape(self):
        self.start()
        out = self.copilot("PreToolUse", tool_name="Edit",
                           tool_input={"file_path": str(self.proj / "calc.py")})
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("[orkestra:", out.get("permissionDecisionReason", ""))
        # VS Code reads the nested shape, the CLI the flat one: both are present
        specific = out.get("hookSpecificOutput") or {}
        self.assertEqual(specific.get("hookEventName"), "PreToolUse")
        self.assertEqual(specific.get("permissionDecision"), "deny")
        self.assertEqual(specific.get("permissionDecisionReason"), out["permissionDecisionReason"])

    def test_copilot_runtime_tool_names_map_to_claude_names(self):
        for runtime, claude in (("rg", "Grep"), ("web_fetch", "WebFetch"), ("web_search", "WebSearch"),
                                ("update_todo", "TodoWrite")):
            self.assertEqual(copilot_hook.TOOL_NAMES[runtime], claude)

    def test_user_prompt_submit_gets_no_one_chat_reminder_but_session_start_does(self):
        self.assertEqual(copilot_hook._mention_one_chat("UserPromptSubmit", {}), {})
        out = copilot_hook._mention_one_chat("SessionStart", {})
        self.assertIn("this chat", out["additionalContext"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_a_closed_deadline_answer_of_an_earlier_run_does_not_silence_this_one(self):
        # in-process callers (this adapter) never pass through dispatch.main(), which is what re-opens the answer
        copilot_hook.dispatch._ANSWER.update(closed=True, emitted=True)
        self.addCleanup(copilot_hook.dispatch._ANSWER.update, closed=False, emitted=False)
        self.start()
        out = self.copilot("PreToolUse", tool_name="Edit", tool_input={"file_path": str(self.proj / "calc.py")})
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_registered_hook_entrypoint_emits_copilot_json(self):
        self.start()
        payload = {"hook_event_name": "PreToolUse", "session_id": self.sid, "cwd": str(self.proj),
                   "tool_name": "Edit", "tool_input": {"file_path": str(self.proj / "calc.py")}}
        cp = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "copilot_hook.py"), "PreToolUse"],
                            input=json.dumps(payload), capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        out = json.loads(cp.stdout)
        self.assertEqual(out.get("permissionDecision"), "deny")

    def test_post_tool_use_binds_the_copilot_session_from_cli_output(self):
        started = self.cli("start", "-p", "demo", "--title", "Copilot", "--request", "sub fonksiyonu ekle",
                           "--size", "S").stdout
        out = self.copilot("PostToolUse", tool_name="bash", tool_input={"command": "orkestra start"},
                           tool_result={"result_type": "success", "text_result_for_llm": started})
        self.assertEqual(out, {})
        binding = ST.session_binding(EN.session_key("copilot", self.sid))
        self.assertEqual(binding["project"], str(self.proj))
        self.assertEqual(binding["engine"], "copilot")
        state = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertEqual(state["engine"], "copilot")
        feature = self.proj / ".orkestra" / "features" / binding["run_id"]
        self.assertIn("- motor: copilot", (feature / "request.md").read_text())

    def test_vscode_terminal_tool_alias_binds_resume_output(self):
        started = self.cli("start", "-p", "demo", "--title", "Copilot", "--request", "sub fonksiyonu ekle",
                           "--size", "S").stdout
        out = self.copilot("PostToolUse", tool_name="execute/runInTerminal",
                           tool_input={"command": "orkestra resume"},
                           tool_result={"result_type": "success", "text_result_for_llm": started})
        self.assertEqual(out, {})
        self.assertEqual(ST.session_binding(EN.session_key("copilot", self.sid))["project"], str(self.proj))

    def test_unrecognized_terminal_tool_binds_explicit_cli_marker(self):
        started = self.cli("start", "-p", "demo", "--title", "Copilot", "--request", "sub fonksiyonu ekle",
                           "--size", "S").stdout
        out = copilot_hook.handle_event("PostToolUse", {
            "hook_event_name": "PostToolUse", "session_id": self.sid, "cwd": str(self.proj),
            "tool_name": "vscode/customTerminal", "tool_input": {"command": "orkestra start"},
            "tool_result": {"result_type": "success", "text_result_for_llm": started},
        })
        self.assertEqual(out, {})
        binding = ST.session_binding(EN.session_key("copilot", self.sid))
        self.assertEqual(binding["project"], str(self.proj))

    def test_explicit_resume_prompt_binds_named_project_and_releases_user_wait(self):
        self.cli("start", "-p", "demo", "--title", "Copilot", "--request", "sub fonksiyonu ekle", "--size", "S",
                 "--approval-mode", "manual")
        self.cli("await", "-p", "demo", "İlk dilimi seç")
        copilot_hook.handle_event("UserPromptSubmit", {
            "hook_event_name": "UserPromptSubmit", "session_id": "copilot-resume-session",
            "cwd": str(self.ws), "prompt_id": "resume-prompt",
            "prompt": "/orkestra:resume demo — çizim + boyama temeli",
        })
        binding = ST.session_binding("copilot-resume-session")
        self.assertIsNotNone(binding)
        self.assertEqual(binding["project"], str(self.proj))
        self.assertEqual(binding["engine"], "copilot")
        status = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertEqual(status["status"], "active")

    def test_direct_user_approval_is_recorded(self):
        self.start()
        self.advance_to_approve()
        out = self.copilot("UserPromptSubmit", prompt="onaylıyorum", timestamp="2026-10-09T10:00:00Z")
        self.assertIn("APPROVED", out.get("additionalContext", ""))
        self.assertNotIn("Could not resolve the project", out.get("additionalContext", ""))
        self.phase_next()

    def test_user_can_switch_an_active_run_to_manual_approval_mode(self):
        self.start(approval_mode="autopilot")

        self.copilot("UserPromptSubmit", prompt="Approve aşamasında onayı bana sor; ben karar verici olayım.")

        state = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertEqual(state["approval_mode"], "manual")

    def test_session_start_context_uses_copilot_output_shape(self):
        self.start()
        out = copilot_hook.handle_event("SessionStart", {
            "hook_event_name": "SessionStart", "session_id": "copilot-other-session",
            "cwd": str(self.proj), "source": "new",
        })
        self.assertIn("additionalContext", out)
        self.assertIn("open orkestra run", out["additionalContext"])

    def test_subagent_start_stays_in_the_one_chat(self):
        self.start()
        self.advance_to_approve()
        self.approve()
        self.phase_next()
        child_id = "copilot-child-scout"
        out = copilot_hook.handle_event("subagentStart", {
            "hook_event_name": "subagentStart", "sessionId": child_id,
            "cwd": str(self.proj), "agentName": "Orkestra Scout",
        })
        # subagentStart can only add context (a deny is ignored by Copilot); the `task` PreToolUse deny below is the gate
        self.assertNotIn("permissionDecision", out)
        self.assertIn("this chat", out.get("additionalContext", ""))
        self.assertIsNone(ST.child_binding(child_id))
        task = copilot_hook.handle_event("PreToolUse", {
            "hook_event_name": "PreToolUse", "session_id": self.sid, "cwd": str(self.proj),
            "tool_name": "task", "tool_input": {"prompt": "open a scout"},
        })
        self.assertEqual(task.get("permissionDecision"), "deny")
        self.assertIn("this chat", task.get("permissionDecisionReason", ""))

    def test_subagent_stop_of_a_refused_chat_stays_empty(self):
        self.start()
        self.advance_to_approve()
        self.approve()
        self.phase_next()
        child_id = "copilot-child-scout"
        copilot_hook.handle_event("subagentStart", {
            "hook_event_name": "subagentStart", "sessionId": child_id,
            "cwd": str(self.proj), "agentName": "Orkestra Scout",
        })
        copilot_hook.handle_event("SubagentStop", {
            "hook_event_name": "SubagentStop", "session_id": child_id,
            "cwd": str(self.proj), "agent_id": "scout-1", "agent_type": "Orkestra Scout",
        })
        self.assertIsNone(ST.child_binding(child_id))
        self.assertIsNone(copilot_hook._active_role(child_id))

    def test_session_start_says_to_stay_in_this_chat(self):
        self.start()
        out = copilot_hook.handle_event("SessionStart", {
            "hook_event_name": "SessionStart", "session_id": self.sid,
            "cwd": str(self.proj), "source": "new",
        })
        self.assertIn("this chat", out.get("additionalContext", ""))

    def test_stop_block_is_preserved_for_copilot(self):
        self.start()
        out = self.copilot("Stop", stop_hook_active=False)
        self.assertEqual(out.get("decision"), "block")
        self.assertTrue(out.get("reason"))

    def test_pre_tool_handler_failure_fails_closed(self):
        def fail(_event):
            raise RuntimeError("synthetic guard failure")

        original = copilot_hook.dispatch.HANDLERS["PreToolUse"]
        copilot_hook.dispatch.HANDLERS["PreToolUse"] = fail
        try:
            out = self.copilot("PreToolUse", tool_name="Bash", tool_input={"command": "true"})
        finally:
            copilot_hook.dispatch.HANDLERS["PreToolUse"] = original
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("hook failure", out.get("permissionDecisionReason", "").lower())


if __name__ == "__main__":
    unittest.main()
