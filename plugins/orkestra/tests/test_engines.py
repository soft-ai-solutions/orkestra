"""Engines (1.5.0): one state machine and one set of guards, driven by Claude Code (default) or Cursor.

The Cursor payload shapes below are the ones `agent` 2026.10.01 sent on 2026-10-05 (see fixtures/cursor): print mode
has generation_id == conversation_id, `transcript_path` is null on a conversation's first tool call, a subagent is a
conversation of its own, a follow-up runs under a new generation id.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

import helpers
from test_flow import FlowBase
from orkestra_core import engines as EN

CONV = "11111111-2222-4333-8444-555555555555"
CHILD = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
OTHER = "77777777-0000-4000-8000-000000000001"


def make_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestEngineUnits(unittest.TestCase):
    def test_names_and_session_keys(self):
        self.assertEqual(EN.normalize(None), "claude")
        self.assertEqual(EN.normalize(" Cursor "), "cursor")
        self.assertEqual(EN.normalize(" Copilot "), "copilot")
        self.assertRaises(EN.EngineError, EN.normalize, "gemini")
        self.assertEqual(EN.default_engine({}), "claude")
        self.assertEqual(EN.default_engine({"engine": {"default": "cursor"}}), "cursor")
        self.assertEqual(EN.default_engine({"engine": {"default": "nope"}}), "claude")
        self.assertEqual(EN.session_key("claude", "abc"), "abc")
        self.assertEqual(EN.session_key("cursor", "abc"), "cursor-abc")
        self.assertEqual(EN.session_key("cursor", "cursor-abc"), "cursor-abc")
        self.assertEqual(EN.session_key("copilot", "abc"), "copilot-abc")
        self.assertEqual(EN.raw_session_id("cursor-abc"), "abc")
        self.assertEqual(EN.raw_session_id("copilot-abc"), "abc")
        self.assertEqual(EN.raw_session_id("abc"), "abc")
        self.assertEqual(EN.session_engine("cursor-abc"), "cursor")
        self.assertEqual(EN.session_engine("copilot-abc"), "copilot")
        self.assertEqual(EN.session_engine("abc"), "claude")

    def test_environment_tells_which_agent_is_asking(self):
        self.assertIsNone(EN.detect_env({}))
        self.assertEqual(EN.detect_env({"CLAUDECODE": "1"}), "claude")
        self.assertEqual(EN.detect_env({"CURSOR_AGENT": "1", "CURSOR_CONVERSATION_ID": "c"}), "cursor")
        self.assertEqual(EN.detect_env({"ORKESTRA_ENGINE": "copilot"}), "copilot")
        self.assertEqual(EN.env_session_id({"CURSOR_AGENT": "1", "CURSOR_CONVERSATION_ID": "c"}), "c")
        self.assertEqual(EN.env_session_id({"CLAUDECODE": "1"}), "")

    def test_claude_command_line_is_what_the_bridge_always_sent(self):
        spec = EN.headless_command("claude", "/bin/claude", "S1", "/orkestra:run x", True,
                                   {"permission_mode": "auto", "max_turns": 400, "extra_args": ["--foo"]})
        self.assertEqual(spec["argv"], ["/bin/claude", "-p", "--session-id", "S1", "--permission-mode", "auto",
                                        "--output-format", "json", "--max-turns", "400", "--foo"])
        self.assertEqual(spec["stdin"], "/orkestra:run x")
        again = EN.headless_command("claude", "/bin/claude", "S1", "p", False, {})
        self.assertEqual(again["argv"][2:4], ["--resume", "S1"])

    def test_cursor_command_line(self):
        spec = EN.headless_command("cursor", "/bin/agent", "C1", "do it", True, {"model": "gpt-x"})
        self.assertEqual(spec["argv"], ["/bin/agent", "-p", "--output-format", "json", "--force", "--trust",
                                        "--resume", "C1", "--model", "gpt-x", "do it"])
        self.assertIsNone(spec["stdin"])                        # the prompt is the last argument, never stdin
        self.assertNotIn("--model", EN.headless_command("cursor", "/bin/agent", "C1", "p", False, {})["argv"])

    def test_copilot_model_and_effort_from_request_text(self):
        self.assertEqual(EN.copilot_model_for_request("use gpt-6-astra with reasoning effort high"), "gpt-6-astra")
        self.assertEqual(EN.copilot_effort_for_request("use gpt-6-astra with reasoning effort high"), "high")
        self.assertEqual(EN.copilot_model_for_request("no model here"), "gpt-6-luna")
        self.assertEqual(EN.copilot_effort_for_request("no effort here"), "max")
        self.assertEqual(EN.copilot_model_for_request("use gpt-6-luna and gpt-6-astra"), "gpt-6-luna")  # ambiguous

    def test_copilot_command_line_uses_the_requested_model_and_autopilot_policy(self):
        opts = {"model": "gpt-6-luna", "reasoning_effort": "max", "plugin_dir": "/plugins/orkestra",
                "approval_mode": "autopilot", "max_autopilot_continues": 7}
        spec = EN.headless_command("copilot", "/bin/copilot", "copilot-S1", "do it", True, opts)
        self.assertEqual(spec["argv"], ["/bin/copilot", "--output-format", "json", "--allow-all-tools",
                                        "--allow-all-paths", "--allow-all-urls",
                                        "--excluded-tools", EN.COPILOT_EXCLUDED_TOOLS,
                                        "--autopilot", "--no-ask-user", "--model", "gpt-6-luna",
                                        "--reasoning-effort", "max", "--max-autopilot-continues", "7",
                                        "--plugin-dir", "/plugins/orkestra", "--session-id=S1", "--prompt=do it"])
        self.assertNotIn("-p", spec["argv"])             # a bare -p would swallow the next option as its value
        dash = EN.headless_command("copilot", "/bin/copilot", "copilot-S1", "- fix it", True, opts)["argv"]
        self.assertEqual(dash[-1], "--prompt=- fix it")
        self.assertNotIn("task", spec["argv"][-1])
        for name in ("task", "list_agents", "read_agent", "write_agent"):
            self.assertIn(name, EN.COPILOT_EXCLUDED_TOOLS)
        self.assertIsNone(spec["stdin"])
        manual = EN.headless_command("copilot", "/bin/copilot", "copilot-S1", "p", False,
                                     dict(opts, approval_mode="manual"))["argv"]
        self.assertNotIn("--autopilot", manual)
        self.assertNotIn("--no-ask-user", manual)
        self.assertIn("--resume=S1", manual)

    def test_copilot_interactive_command_has_autopilot_model_and_effort(self):
        argv = EN.interactive_command("copilot", "/bin/copilot", "start", {
            "model": "gpt-6-luna", "reasoning_effort": "max", "plugin_dir": "/plugins/orkestra",
            "approval_mode": "autopilot",
        })
        self.assertEqual(argv, ["/bin/copilot", "-i", "start", "--allow-all-tools",
                                "--allow-all-paths", "--allow-all-urls",
                                "--excluded-tools", EN.COPILOT_EXCLUDED_TOOLS,
                                "--autopilot", "--no-ask-user",
                                "--model", "gpt-6-luna", "--reasoning-effort", "max", "--plugin-dir", "/plugins/orkestra"])
        self.assertNotIn("--fleet", argv)

    def test_prompts(self):
        self.assertEqual(EN.run_prompt("claude", "x yap"), "/orkestra:run x yap")
        self.assertIn("orkestra playbook run", EN.run_prompt("cursor", "x yap"))
        self.assertTrue(EN.run_prompt("cursor", "x yap").endswith("İstek: x yap"))
        self.assertEqual(EN.resume_prompt("claude", "demo"), "/orkestra:resume demo")
        self.assertEqual(EN.resume_prompt("claude"), "/orkestra:resume")
        self.assertIn("orkestra resume -p demo", EN.resume_prompt("cursor", "demo"))
        copilot = EN.run_prompt("copilot", "x yap")
        self.assertIn("Do not open another chat", copilot)
        self.assertIn("orkestra playbook run", copilot)
        self.assertTrue(copilot.endswith("İstek: x yap") or copilot.endswith("Request: x yap"))

    def test_text_is_renamed_only_for_cursor(self):
        src = 'use subagent_type "orkestra:reviewer", then /orkestra:resume — AskUserQuestion once'
        self.assertEqual(EN.adapt_text(src, "claude"), src)
        self.assertEqual(EN.adapt_text(src, None), src)
        out = EN.adapt_text(src, "cursor")
        self.assertIn('"orkestra-reviewer"', out)
        self.assertIn("/orkestra-resume", out)
        self.assertEqual(out.count(EN.ASK_FOOTNOTE), 1)
        self.assertEqual(EN.adapt_text(out, "cursor").count(EN.ASK_FOOTNOTE), 1)       # never twice
        self.assertNotIn(EN.ASK_FOOTNOTE, EN.adapt_text("orkestra:scout", "cursor"))
        self.assertEqual(EN.adapt_text("orkestra:unknownthing x", "cursor"), "orkestra:unknownthing x")   # only real names
        doc = EN.with_notes(src, "cursor")
        self.assertTrue(doc.startswith(EN.NOTES_MARK))
        self.assertNotIn(EN.ASK_FOOTNOTE, doc)                  # the header already says it
        self.assertEqual(EN.with_notes(src, "claude"), src)
        copilot_doc = EN.with_notes(src, "copilot")
        self.assertTrue(copilot_doc.startswith(EN.COPILOT_MARK))
        self.assertIn("Do not open another chat", copilot_doc)
        self.assertIn(src, copilot_doc)
        self.assertNotIn("--fleet", copilot_doc)

    def test_subagent_names(self):
        self.assertEqual(EN.agent_type_from_cursor("orkestra-plan-critic"), "orkestra:plan-critic")
        self.assertEqual(EN.agent_type_from_cursor("explore"), "cursor:explore")
        self.assertEqual(EN.agent_type_from_cursor("orkestra-nope"), "cursor:orkestra-nope")
        self.assertEqual(EN.agent_type_from_cursor(""), "cursor:agent")

    def test_result_parsing(self):
        self.assertEqual(EN.parse_result('{"type":"result","result":"ok","session_id":"s"}')["session_id"], "s")
        noisy = 'warming up\n{"type":"x"}\n{"type":"result","result":"son","session_id":"s2"}\n'
        self.assertEqual(EN.parse_result(noisy)["result"], "son")
        self.assertEqual(EN.parse_result("not json"), {})
        self.assertEqual(EN.parse_result(""), {})

    def test_child_environment(self):
        base = {"PATH": "/usr/bin", "CURSOR_AGENT": "1", "CURSOR_CONVERSATION_ID": "x"}
        c = EN.child_env("claude", base)
        self.assertEqual((c["ORKESTRA_HEADLESS"], c["PATH"]), ("1", "/usr/bin"))
        k = EN.child_env("cursor", base)
        self.assertEqual(k["PATH"].split(os.pathsep)[0], str(helpers.PLUGIN / "bin"))   # `orkestra` for Cursor's shell
        self.assertNotIn("CURSOR_AGENT", k)
        self.assertNotIn("CURSOR_CONVERSATION_ID", k)
        self.assertEqual(EN.child_env("cursor", k)["PATH"], k["PATH"])                  # not added twice


class TestBinaries(helpers.Sandbox):
    @unittest.skipUnless(os.name != "nt", "#!/bin/sh make_exec fixture (FR-007)")
    def test_binary_lookup_and_new_session(self):
        fake = make_exec(self.tmp / "bin" / "agent", "#!/bin/sh\n[ \"$1\" = create-chat ] && echo "
                         "'Created chat: 0a1b2c3d-1111-4222-8333-444455556666'\n")
        self.assertEqual(EN.binary("cursor", {}, hint=str(fake)), str(fake))
        self.assertEqual(EN.binary("cursor", {"engine": {"cursor": {"bin": str(fake)}}}), str(fake))
        os.environ["ORKESTRA_CURSOR_BIN"] = str(fake)
        self.assertEqual(EN.binary("cursor"), str(fake))
        self.assertEqual(EN.new_session("cursor", str(fake), str(self.tmp)), "0a1b2c3d-1111-4222-8333-444455556666")
        self.assertRegex(EN.new_session("claude", "claude", str(self.tmp)), r"^[0-9a-f-]{36}$")
        bad = make_exec(self.tmp / "bin" / "agent2", "#!/bin/sh\necho no id here\n")
        self.assertRaises(EN.EngineError, EN.new_session, "cursor", str(bad), str(self.tmp))
        self.assertRaises(EN.EngineError, EN.new_session, "cursor", str(self.tmp / "missing"), str(self.tmp))

    def test_copilot_hooks_must_cover_prompt_and_tool_guards(self):
        plugin = self.tmp / "plugins" / "orkestra"
        hooks_path = plugin / "com.github.copilot" / "hooks" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks = {event: [{"command": f"python copilot_hook.py {event}"}]
                 for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse")}
        hooks_path.write_text(json.dumps({"hooks": hooks}))
        self.assertFalse(EN.copilot_hooks_active(str(plugin)))      # no plugin.json: Copilot would not load it
        (plugin / "plugin.json").write_text("{}")
        self.assertTrue(EN.copilot_hooks_active(str(plugin)))

        hooks.pop("PreToolUse")
        hooks_path.write_text(json.dumps({"hooks": hooks}))
        self.assertFalse(EN.copilot_hooks_active(str(plugin)))


class CursorBase(FlowBase):
    """A run driven from a Cursor conversation; hook events go through hooks/cursor_hook.py."""

    def setUp(self):
        super().setUp()
        self.use(CONV)

    def use(self, conv):                            # which Cursor conversation drives the run in this test
        self.conv, self.sid = conv, "cursor-" + conv
        self.shell_env = {"CURSOR_AGENT": "1", "CURSOR_CONVERSATION_ID": conv, "CURSOR_INVOKED_AS": "agent"}

    def chook(self, event, conv=None, gen="gen-1", *, keep_agent_env=False, **kw):
        conv = conv or self.conv
        payload = {"conversation_id": conv, "generation_id": gen, "model": "default", "session_id": conv,
                   "hook_event_name": event, "cursor_version": "2026.10.01-14929f9",
                   "workspace_roots": [str(self.proj)], "user_email": "user@example.com", "transcript_path": None}
        payload.update(kw)
        cp = helpers.run_cursor_hook(event, json.dumps(payload), keep_agent_env=keep_agent_env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue(cp.stdout.strip(), "Cursor needs a JSON answer for every hook")
        return json.loads(cp.stdout)

    def tool(self, name, ti, conv=None, gen="gen-1", **kw):
        kw.setdefault("cwd", "")
        kw.setdefault("tool_use_id", "tu-1")
        return self.chook("preToolUse", conv, gen, tool_name=name, tool_input=ti, **kw)

    def after(self, name, ti, output, conv=None, gen="gen-1", **kw):
        kw.setdefault("tool_use_id", "tu-1")
        return self.chook("postToolUse", conv, gen, tool_name=name, tool_input=ti, cwd="",
                          tool_output=json.dumps(output) if not isinstance(output, str) else output, duration=12.5, **kw)

    def shell(self, command, **kw):
        return self.tool("Shell", {"command": command, "cwd": "", "timeout": 30000}, **kw)

    def say(self, text, gen="gen-1", conv=None, **kw):
        return self.chook("beforeSubmitPrompt", conv, gen, prompt=text, attachments=[], **kw)

    @staticmethod
    def no(out):
        return out.get("permission") == "deny"

    def start(self, title="Subtract"):              # `orkestra start` typed by the agent into Cursor's shell
        self.assertEqual(self.shell(f'orkestra start -p demo --title "{title}" --request "sub fonksiyonu ekle" --size S'),
                         {"permission": "allow"})   # the preToolUse hook sees the command before it runs
        out = self.cli("start", "-p", "demo", "--title", title, "--request", "sub fonksiyonu ekle", "--size", "S",
                   "--approval-mode", "manual",
                       env=self.shell_env).stdout
        self.rid = out.split("run=")[-1].split()[0]
        self.fdir = self.proj / ".orkestra" / "features" / self.rid
        return out

    def approve(self, text="onay"):
        return self.say(text, gen="gen-approve")

    def state(self):
        return json.loads(self.cli("status", "--json", "-p", "demo").stdout)

    def to_implement(self):
        self.start()
        self.advance_to_approve()
        self.approve()
        self.phase_next()


class TestCursorSession(CursorBase):
    def test_start_from_cursors_shell_binds_the_conversation_and_records_the_engine(self):
        out = self.start()
        self.assertIn("[orkestra:bind]", out)
        b = json.loads((self.home / "sessions" / f"{self.sid}.json").read_text())
        self.assertEqual((b["run_id"], b["engine"]), (self.rid, "cursor"))
        self.assertEqual(self.state()["engine"], "cursor")
        self.assertIn("- motor: cursor", (self.fdir / "request.md").read_text())
        self.assertEqual(json.loads(self.cli("status", "--json", "-p", "demo").stdout)["engine"], "cursor")
        # a Claude Code session id can never collide with it (no binding under the bare id)
        self.assertFalse((self.home / "sessions" / f"{CONV}.json").exists())

    def test_bind_line_in_shell_output_binds_too(self):
        out = self.cli("start", "-p", "demo", "--title", "T", "--request", "bir şey yap", "--size", "S").stdout
        self.assertFalse((self.home / "sessions" / f"{self.sid}.json").exists())
        self.after("Shell", {"command": "orkestra start", "cwd": ""}, {"output": out, "exitCode": 0})
        b = json.loads((self.home / "sessions" / f"{self.sid}.json").read_text())
        self.assertEqual(b["engine"], "cursor")

    def test_the_same_guards_answer_in_cursors_dialect(self):
        self.start()
        out = self.tool("Write", {"file_path": str(self.proj / "calc.py"), "content": "x"})
        self.assertTrue(self.no(out), out)
        self.assertIn("[orkestra:phase]", out["user_message"])              # the model sees "Rejected: <user_message>"
        self.assertEqual(out["user_message"], out["agent_message"])
        self.assertTrue(self.no(self.tool("Delete", {"file_path": str(self.proj / "calc.py")})))
        self.assertTrue(self.no(self.shell("echo x > calc.py")))
        self.assertTrue(self.no(self.shell("git push --force origin main")))
        self.assertTrue(self.no(self.tool("Read", {"file_path": str(self.proj / ".env")})))
        self.assertTrue(self.no(self.shell("cat .env.local")))
        self.assertEqual(self.tool("Read", {"file_path": str(self.proj / "calc.py")}), {"permission": "allow"})
        self.assertEqual(self.tool("Write", {"file_path": str(self.fdir / "context.md"), "content": "x"}),
                         {"permission": "allow"})
        self.assertEqual(self.shell("orkestra context -p demo"), {"permission": "allow"})
        self.assertEqual(self.tool("Grep", {"pattern": "add", "path": "."}), {"permission": "allow"})
        self.assertEqual(self.tool("MCP:context7_get_docs", {"id": "x"}), {"permission": "allow"})
        kinds = [json.loads(x)["kind"] for x in
                 (self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl").read_text().splitlines()]
        self.assertIn("deny", kinds)
        self.assertIn("tool", kinds)

    def test_a_chat_outside_a_run_is_left_alone_unless_the_user_opted_in(self):
        for cmd in ("git push --force origin main", "git commit -m x", "cat .env"):
            self.assertEqual(self.shell(cmd, conv=OTHER), {"permission": "allow"}, cmd)
        self.assertEqual(self.tool("Write", {"file_path": str(self.proj / "calc.py")}, conv=OTHER), {"permission": "allow"})
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"engine": {"cursor": {"global_guards": True}}}))
        self.assertTrue(self.no(self.shell("git push --force origin main", conv=OTHER)))
        self.assertTrue(self.no(self.shell("cat .env", conv=OTHER)))
        self.assertEqual(self.tool("Write", {"file_path": str(self.proj / "calc.py")}, conv=OTHER), {"permission": "allow"})

    def test_plan_approval_is_the_user_typing_onay(self):
        self.start()
        self.advance_to_approve()
        self.phase_next(expect_ok=False)                                    # nobody approved yet
        self.assertEqual(self.say("bu plan ne kadar sürer?"), {"continue": True})
        self.phase_next(expect_ok=False)                                    # a question approves nothing
        self.assertEqual(self.approve("onay"), {"continue": True})
        appr = json.loads((self.proj / ".orkestra" / "runs" / self.rid / "approval.json").read_text())
        self.assertEqual((appr["decision"], appr["source"]), ("approve", "prompt"))
        # Cursor cannot attach context to a prompt: it rides on the next tool result, once
        out = self.after("Shell", {"command": "orkestra next", "cwd": ""}, {"output": "x", "exitCode": 0}, gen="gen-approve")
        self.assertIn("APPROVED", out["additional_context"])
        self.assertNotIn("additional_context", self.after("Shell", {"command": "ls", "cwd": ""}, {"output": "", "exitCode": 0},
                                                          gen="gen-approve"))
        self.phase_next()
        self.assertEqual(self.tool("Write", {"file_path": str(self.proj / "calc.py")}, gen="gen-approve"), {"permission": "allow"})

    def test_change_request_is_not_an_approval(self):
        self.start()
        self.advance_to_approve()
        self.say("onaylamıyorum, T002'yi çıkar")
        appr = json.loads((self.proj / ".orkestra" / "runs" / self.rid / "approval.json").read_text())
        self.assertEqual(appr["decision"], "reject")
        self.phase_next(expect_ok=False)

    def test_waiting_for_the_user_and_the_reply(self):
        self.start()
        self.cli("await", "-p", "demo", "Hangi renk?", env=self.shell_env)
        self.assertEqual(self.state()["status"], "awaiting_user")
        self.assertEqual(self.chook("stop", status="completed", loop_count=0), {})     # waiting: no nudge
        self.say("mavi", gen="gen-2")
        self.assertEqual(self.state()["status"], "active")

    def test_git_permission_belongs_to_one_user_prompt_across_generations(self):
        self.to_implement()
        commit = "git add -A && git commit -m wip"
        self.assertTrue(self.no(self.shell(commit, gen="gen-approve")))                 # "onay" grants no commit
        self.say("şimdi commit et", gen="g-A")
        self.assertEqual(self.shell(commit, gen="g-A"), {"permission": "allow"})
        self.assertTrue(self.no(self.shell(commit, gen="g-stranger")))                  # a generation nobody announced
        # the agent stops early → our follow-up → Cursor continues under a NEW generation id: still the same prompt
        out = self.chook("stop", gen="g-A", status="completed", loop_count=0)
        self.assertIn("followup_message", out)
        self.assertEqual(self.shell(commit, gen="g-B"), {"permission": "allow"})
        self.assertTrue(self.no(self.shell(commit, gen="g-C")))                         # only ONE generation is adopted
        self.say("testleri de çalıştır", gen="g-D")                                     # the next message asks no commit
        self.assertTrue(self.no(self.shell(commit, gen="g-D")))
        self.assertTrue(self.no(self.shell(commit, gen="g-A")))

    def test_print_mode_the_driver_announces_the_prompt_and_session_end_closes_it(self):
        self.to_implement()
        commit = "git add -A && git commit -m wip"
        # `agent -p`: no prompt hook, generation id == conversation id on every turn
        self.assertTrue(self.no(self.shell(commit, gen=CONV)))
        ctx = helpers.driver_prompt("cursor", CONV, "commitle", str(self.proj))
        self.assertEqual(ctx, "")
        self.assertEqual(self.shell(commit, gen=CONV), {"permission": "allow"})
        self.assertEqual(self.chook("sessionEnd", gen=CONV, reason="completed", final_status="completed"), {})
        self.assertTrue(self.no(self.shell(commit, gen=CONV)))                          # a hand-started later turn
        helpers.driver_prompt("cursor", "cursor-" + CONV, "/orkestra-resume demo", str(self.proj))
        self.assertTrue(self.no(self.shell(commit, gen=CONV)))                          # an automatic turn asks nothing
        helpers.driver_prompt("claude", CONV, "commitle", str(self.proj))               # no-op for claude
        self.assertTrue(self.no(self.shell(commit, gen=CONV)))

    def test_driver_gets_the_approval_note_for_its_prompt(self):
        self.start()
        self.advance_to_approve()
        ctx = helpers.driver_prompt("cursor", CONV, "onaylıyorum", str(self.proj))
        self.assertIn("APPROVED", ctx)
        self.assertIn("orkestra phase next", ctx)
        self.assertNotIn("additional_context", self.after("Shell", {"command": "ls", "cwd": ""}, {"output": "", "exitCode": 0},
                                                          gen=CONV))                    # not delivered a second time
        self.phase_next()

    def test_stop_becomes_a_followup_at_most_twice_per_prompt(self):
        self.start()
        self.say("devam", gen="g1")
        self.shell("ls", gen="g1")                                                      # this turn must drive the run
        out = self.chook("stop", gen="g1", status="completed", loop_count=0)
        self.assertIn("still in phase", out["followup_message"])
        self.assertNotIn("/orkestra:", out["followup_message"])
        self.assertNotIn("orkestra:implementer", out["followup_message"])
        self.assertEqual(out["followup_message"].count(EN.ASK_FOOTNOTE), 1)             # it mentions AskUserQuestion
        self.shell("ls", gen="g2")                                                      # the follow-up generation works
        self.assertIn("followup_message", self.chook("stop", gen="g2", status="completed", loop_count=1))
        self.shell("ls", gen="g3")
        self.assertEqual(self.chook("stop", gen="g3", status="completed", loop_count=2), {})   # limit: the turn may end
        self.say("devam et", gen="g4")                                                  # a new prompt, a new budget
        self.shell("ls", gen="g4")
        self.assertIn("followup_message", self.chook("stop", gen="g4", status="completed", loop_count=0))
        for status in ("aborted", "error"):
            self.say("x", gen="g5")
            self.assertEqual(self.chook("stop", gen="g5", status=status, loop_count=0), {})    # never push those on

    def test_stop_no_followup_when_transcript_last_human_is_off_topic(self):
        """Cursor Stop follow-ups skip beforeSubmitPrompt — transcript off-topic must unbind, no nudge."""
        self.start()
        self.say("devam", gen="g-drive")
        self.shell("ls", gen="g-drive")
        self.assertTrue((self.home / "sessions" / f"{self.sid}.json").exists())
        root = self.tmp / "cursor-home" / "projects" / "tmp-demo" / "agent-transcripts"
        tp = root / CONV / f"{CONV}.jsonl"
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(
            json.dumps({"role": "user", "message": {"content": [
                {"type": "text", "text": "<user_query>\ndevam\n</user_query>"}]}}) + "\n"
            + json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}) + "\n"
            + json.dumps({"role": "user", "message": {"content": [
                {"type": "text", "text":
                 # 2026-10-12: an ordinary message keeps the binding; this one says to let go of the run
                 "<user_query>\nnever mind this run, prepare orkestra for public github, scrub local paths\n</user_query>"}]}}) + "\n",
            encoding="utf-8")
        out = self.chook("stop", gen="g-off", status="completed", loop_count=0, transcript_path=str(tp))
        self.assertEqual(out, {})
        self.assertFalse((self.home / "sessions" / f"{self.sid}.json").exists())

    def test_session_start_tells_a_chat_about_the_run(self):
        out = self.chook("sessionStart", gen=CONV, is_background_agent=False)
        self.assertEqual(out, {"env": {"ORKESTRA_ENGINE": "cursor"}})
        self.start()
        out = self.chook("sessionStart", gen=CONV, is_background_agent=False)
        self.assertIn(self.rid, out["additional_context"])
        other = self.chook("sessionStart", conv=OTHER, gen=OTHER, is_background_agent=False)
        self.assertIn("/orkestra-resume", other["additional_context"])                  # Cursor's skill name
        self.assertIn("NOT driving", other["additional_context"])


class TestCursorSubagents(CursorBase):
    PROMPT = "Review task T001 of run X: check spec compliance of calc.py and report in reviews/T001.md"

    def launch(self, sub="orkestra-reviewer", prompt=None, tdir=""):
        ti = {"description": "Review T001", "prompt": prompt or self.PROMPT, "model": "", "subagent_type": sub,
              "run_in_background": False}
        tp = os.path.join(tdir, CONV, CONV + ".jsonl") if tdir else None
        return self.tool("Task", ti, gen="g1", transcript_path=tp)

    def age_launches(self, seconds):
        p = self.proj / ".orkestra" / "runs" / self.rid / "agents.json"
        data = json.loads(p.read_text())
        for x in data.get("cursor_launches") or []:
            x["at"] -= seconds
        p.write_text(json.dumps(data))

    def transcript(self, conv, text, root=None):
        root = root or (self.tmp / "cursor-home" / "projects" / "tmp-demo" / "agent-transcripts")
        p = root / conv / f"{conv}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"role": "user", "message": {"content": [
            {"type": "text", "text": f"<user_query>\n{text}\n</user_query>"}]}}) + "\n")
        return str(p)

    def test_a_subagent_conversation_gets_its_role_from_the_parents_task_call(self):
        self.to_implement()
        self.assertEqual(self.launch(), {"permission": "allow"})
        tp = self.transcript(CHILD, self.PROMPT)
        src = {"file_path": str(self.proj / "calc.py"), "content": "x"}
        out = self.tool("Write", src, conv=CHILD, gen=CHILD, transcript_path=tp)
        self.assertTrue(self.no(out), out)
        self.assertIn("read-only", out["user_message"])
        self.assertIn("Agent orkestra-reviewer is read-only", out["user_message"])      # named the way Cursor knows it
        child = json.loads((self.home / "sessions" / "children" / f"{CHILD}.json").read_text())
        self.assertEqual((child["parent"], child["agent_type"], child["run_id"]), (self.sid, "orkestra:reviewer", self.rid))
        # from now on it is known without the transcript
        self.assertTrue(self.no(self.tool("Write", src, conv=CHILD, gen=CHILD)))
        self.assertEqual(self.tool("Write", {"file_path": str(self.fdir / "reviews" / "T001.md")}, conv=CHILD, gen=CHILD),
                         {"permission": "allow"})
        self.assertTrue(self.no(self.shell("orkestra phase next -p demo", conv=CHILD, gen=CHILD)))
        self.assertEqual(self.shell("orkestra changes -p demo", conv=CHILD, gen=CHILD), {"permission": "allow"})
        # the main conversation keeps its own rights
        self.assertEqual(self.tool("Write", src, gen="g1"), {"permission": "allow"})
        # the child's events are logged under the run, as that agent
        rows = [json.loads(x) for x in (self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl").read_text().splitlines()]
        self.assertTrue(any(r.get("agent") == "orkestra:reviewer" and r.get("kind") == "deny" for r in rows))
        # nothing a subagent "says" counts as the user
        self.assertEqual(self.say("onay, commit et", conv=CHILD, gen=CHILD), {"continue": True})
        self.assertTrue(self.no(self.shell("git commit -m x", gen="g1")))
        self.assertEqual(self.chook("stop", conv=CHILD, gen=CHILD, status="completed", loop_count=0), {})

    def test_the_transcript_is_found_without_a_path_in_the_event(self):
        self.to_implement()
        self.launch()
        self.transcript(CHILD, self.PROMPT)                         # under <cursor home>/projects/*/agent-transcripts
        out = self.tool("Write", {"file_path": str(self.proj / "calc.py")}, conv=CHILD, gen=CHILD)
        self.assertTrue(self.no(out))
        self.assertTrue((self.home / "sessions" / "children" / f"{CHILD}.json").exists())

    def test_first_call_without_any_transcript_is_guarded_but_nothing_is_written_down(self):
        self.to_implement()
        self.launch()
        out = self.tool("Write", {"file_path": str(self.proj / "calc.py")}, conv=CHILD, gen=CHILD)
        self.assertTrue(self.no(out), out)                          # treated as the reviewer that was just launched
        self.assertFalse((self.home / "sessions" / "children" / f"{CHILD}.json").exists())
        self.assertEqual(self.tool("Read", {"file_path": str(self.proj / "calc.py")}, conv=CHILD, gen=CHILD),
                         {"permission": "allow"})

    def test_an_unrelated_chat_in_the_same_project_is_never_claimed(self):
        self.to_implement()
        self.launch()
        tp = self.transcript(OTHER, "README'yi güncelle lütfen")
        readme = {"file_path": str(self.proj / "README.md")}
        # a chat whose own message fits no launch is not a waiting subagent, even while the launch is open
        self.assertEqual(self.tool("Write", readme, conv=OTHER, gen="x1", transcript_path=tp), {"permission": "allow"})
        self.assertFalse((self.home / "sessions" / "children" / f"{OTHER}.json").exists())
        # the real subagent finds its launch…
        tp2 = self.transcript(CHILD, self.PROMPT)
        self.assertTrue(self.no(self.tool("Write", {"file_path": str(self.proj / "calc.py")}, conv=CHILD, gen=CHILD,
                                          transcript_path=tp2)))
        self.assertTrue((self.home / "sessions" / "children" / f"{CHILD}.json").exists())
        # …and from then on the other chat is nobody's subagent
        self.assertEqual(self.tool("Write", readme, conv=OTHER, gen="x1", transcript_path=tp), {"permission": "allow"})
        self.assertFalse((self.home / "sessions" / "children" / f"{OTHER}.json").exists())

    def test_implementer_may_write_source_readonly_agents_may_not(self):
        self.to_implement()
        self.launch("orkestra-implementer", "Implement T001: add sub(a, b) to calc.py with a failing test first")
        self.launch("orkestra-verifier", "Verify the run goal backwards and write verification.md")
        a = self.transcript(CHILD, "Implement T001: add sub(a, b) to calc.py with a failing test first")
        b = self.transcript(OTHER, "Verify the run goal backwards and write verification.md")
        src = {"file_path": str(self.proj / "calc.py")}
        self.assertEqual(self.tool("Write", src, conv=CHILD, gen=CHILD, transcript_path=a), {"permission": "allow"})
        self.assertTrue(self.no(self.tool("Write", src, conv=OTHER, gen=OTHER, transcript_path=b)))
        self.assertTrue(self.no(self.shell("orkestra phase next -p demo", conv=CHILD, gen=CHILD)))   # orchestrator only
        self.assertTrue(self.no(self.tool("Write", {"file_path": str(self.proj / ".orkestra" / "config.json")},
                                          conv=CHILD, gen=CHILD)))

    def test_a_returned_task_is_closed_in_the_log_when_the_parent_moves_on(self):
        """Cursor fires nothing when a foreground subagent ends; the parent's next call is the proof that it did."""
        from orkestra_core import activity
        self.to_implement()
        rows = lambda: [json.loads(x) for x in (self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl").read_text().splitlines()]
        self.launch()
        tp = self.transcript(CHILD, self.PROMPT)
        self.tool("Read", {"file_path": str(self.proj / "calc.py")}, conv=CHILD, gen=CHILD, transcript_path=tp)
        self.assertEqual(activity.running_agents(rows()), ["orkestra:reviewer"])
        self.launch("orkestra-adversary", "Attack the change: edge cases and failure modes of sub()")      # a parallel Task
        self.assertEqual(len(activity.running_agents(rows())), 2)                   # another Task call closes nothing
        self.shell("ls", gen="g1")                  # a sibling call sent in the same message: the Tasks just started
        self.assertEqual(len(activity.running_agents(rows())), 2)
        self.age_launches(30)
        self.shell("orkestra next -p demo", gen="g1")                               # the parent is back
        self.assertEqual(activity.running_agents(rows()), [])
        done = [r for r in rows() if r["kind"] == "agent_done"]
        self.assertEqual([(r["agent"], r.get("aid")) for r in done], [("orkestra:reviewer", CHILD)])
        n = len(rows())
        self.shell("ls", gen="g1")
        self.assertEqual(len([r for r in rows() if r["kind"] in ("result", "agent_done")]),
                         len([r for r in rows()[:n] if r["kind"] in ("result", "agent_done")]))    # reported once
        # the late first call of the other subagent is still recognised (closing is bookkeeping for the log only)
        tp2 = self.transcript(OTHER, "Attack the change: edge cases and failure modes of sub()")
        out = self.tool("Write", {"file_path": str(self.proj / "calc.py")}, conv=OTHER, gen=OTHER, transcript_path=tp2)
        self.assertTrue(self.no(out))
        self.launch("orkestra-verifier", "Verify the goal")
        self.assertIn("orkestra:verifier", activity.running_agents(rows()))
        self.age_launches(30)
        self.chook("stop", gen="g1", status="completed", loop_count=0)              # the turn ending closes it too
        self.assertEqual(activity.running_agents(rows()), [])

    def test_builtin_subagents_are_foreign(self):
        self.to_implement()
        self.launch("explore", "Find where add() is used in this repository and list the call sites")
        tp = self.transcript(CHILD, "Find where add() is used in this repository and list the call sites")
        self.tool("Read", {"file_path": str(self.proj / "calc.py")}, conv=CHILD, gen=CHILD, transcript_path=tp)
        child = json.loads((self.home / "sessions" / "children" / f"{CHILD}.json").read_text())
        self.assertEqual(child["agent_type"], "cursor:explore")

    def test_subagent_start_event_binds_the_child_when_cursor_sends_it(self):
        self.to_implement()
        out = self.chook("subagentStart", subagent_id=CHILD, subagent_type="orkestra-scout", parent_conversation_id=CONV)
        self.assertEqual(out, {"permission": "allow"})
        child = json.loads((self.home / "sessions" / "children" / f"{CHILD}.json").read_text())
        self.assertEqual((child["parent"], child["agent_type"]), (self.sid, "orkestra:scout"))


class TestRecordedCursorRun(CursorBase):
    """Payloads recorded from real runs (agent 2026.10.01, 2026-10-05; tests/fixtures/cursor — e-mail and home
    directory replaced, nothing else): a headless run through the bridge (`print_*`, `child_*`) and the terminal UI
    (`ui_*`). The project path is mapped onto the sandbox project."""
    FX = Path(__file__).resolve().parent / "fixtures" / "cursor"
    RECORDED = "/private/tmp/orkestra-e2e/ws/demo"

    def fx(self, name, **over):
        data = json.loads((self.FX / f"{name}.json").read_text(encoding="utf-8").replace(self.RECORDED, str(self.proj)))
        data.update(over)
        return data

    def send(self, name, **over):
        data = self.fx(name, **over)
        # Host-like origin (double-fork): under Cursor's agent shell a plain subprocess is refused.
        cp = helpers.run_cursor_hook(data["hook_event_name"], json.dumps(data))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return json.loads(cp.stdout)

    def test_nothing_personal_in_the_fixtures(self):
        names = sorted(p.name for p in self.FX.glob("*.json"))
        self.assertGreaterEqual(len(names), 15)
        for p in self.FX.glob("*.json"):
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/Users/(?!u/)", p.name)
            for mail in __import__("re").findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
                self.assertIn(mail, ("user@example.com", "t@example.com", "e2e@example.com"), p.name)

    def test_every_recorded_event_gets_a_well_formed_answer(self):
        for p in sorted(self.FX.glob("*.json")):
            if "transcript" in p.name:
                continue
            self.assertIn("hook_event_name", self.fx(p.stem), p.name)
            out = self.send(p.stem)
            event = self.fx(p.stem)["hook_event_name"]
            if event == "preToolUse":
                self.assertEqual(out, {"permission": "allow"}, p.name)          # no run here: left alone
            elif event == "beforeSubmitPrompt":
                self.assertEqual(out, {"continue": True}, p.name)
            elif event == "sessionStart":
                self.assertEqual(out["env"], {"ORKESTRA_ENGINE": "cursor"}, p.name)
            else:
                self.assertIsInstance(out, dict, p.name)

    def test_headless_run_as_it_happened(self):
        main = self.fx("print_pre_task")["conversation_id"]
        child = self.fx("child_pre_write")["conversation_id"]
        self.use(main)
        out = self.cli("start", "-p", "demo", "--title", "Add sub function", "--request", "sub ekle",
                       "--size", "XS", "--approval-mode", "manual").stdout
        self.rid = out.split("run=")[-1].split()[0]
        self.assertEqual(self.rid, "001-add-sub-function")                      # the recorded bind line names this run
        self.fdir = self.proj / ".orkestra" / "features" / self.rid
        self.assertEqual(self.send("print_pre_shell_first"), {"permission": "allow"})   # transcript_path: null, cwd: ""
        self.send("print_post_shell_bind")                                      # `orkestra start` output → bound
        b = json.loads((self.home / "sessions" / f"{self.sid}.json").read_text())
        self.assertEqual((b["run_id"], b["engine"]), (self.rid, "cursor"))
        self.advance_to_approve()
        self.assertTrue(self.no(self.send("child_pre_write", conversation_id=main, session_id=main)))    # not approved yet
        self.assertIn("APPROVED", helpers.driver_prompt("cursor", main, "onay", str(self.proj)))
        self.phase_next()
        self.assertEqual(self.send("print_pre_task"), {"permission": "allow"})
        # the subagent's first batch of calls arrives before its transcript exists
        first = self.fx("child_pre_first_no_transcript")
        self.assertIsNone(first["transcript_path"])
        self.assertEqual(self.send("child_pre_first_no_transcript"), {"permission": "allow"})
        self.assertFalse((self.home / "sessions" / "children" / f"{child}.json").exists())
        tdir = self.tmp / "cursor-home" / "projects" / "p" / "agent-transcripts" / child
        tdir.mkdir(parents=True)
        (tdir / f"{child}.jsonl").write_text(json.dumps(self.fx("child_transcript_first_line"), ensure_ascii=False) + "\n")
        self.assertEqual(self.send("child_pre_write"), {"permission": "allow"})  # the implementer writes its test
        bound = json.loads((self.home / "sessions" / "children" / f"{child}.json").read_text())
        self.assertEqual((bound["parent"], bound["agent_type"]), (self.sid, "orkestra:implementer"))
        self.assertIsInstance(self.send("child_post_write"), dict)
        self.assertEqual(self.send("child_pre_shell_tdd"), {"permission": "allow"})
        self.assertTrue(self.no(self.shell("orkestra phase next -p demo", conv=child, gen=child)))
        self.assertTrue(self.no(self.shell("git commit -m x", conv=child, gen=child)))
        rows = [json.loads(x) for x in (self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl").read_text().splitlines()]
        self.assertTrue(any(r.get("agent") == "orkestra:implementer" and r.get("aid") == child for r in rows))
        self.assertEqual(self.send("print_session_end"), {})

    def test_terminal_ui_prompt_stop_and_followup_as_they_happened(self):
        stop = self.fx("ui_stop_before_followup")
        conv, gen = stop["conversation_id"], stop["generation_id"]
        after = self.fx("ui_pre_after_followup")
        self.assertNotEqual(after["generation_id"], gen)                        # the follow-up got a new generation id
        self.use(conv)
        self.assertEqual(self.send("ui_before_submit_prompt", conversation_id=conv, session_id=conv, generation_id=gen,
                                   prompt="şunu yap ve commit et"), {"continue": True})
        self.to_implement()                                                     # (its "onay" is another prompt)
        self.send("ui_before_submit_prompt", conversation_id=conv, session_id=conv, generation_id=gen,
                  prompt="şunu yap ve commit et")
        commit = {"command": "git add -A && git commit -m wip", "cwd": "", "timeout": 30000}
        self.assertEqual(self.send("ui_pre_after_followup", generation_id=gen, tool_name="Shell", tool_input=commit),
                         {"permission": "allow"})
        out = self.send("ui_stop_before_followup")                              # status completed, loop_count 0
        self.assertIn("still in phase 'implement'", out["followup_message"])
        self.assertEqual(self.send("ui_pre_after_followup"), {"permission": "allow"})
        self.assertEqual(self.send("ui_pre_after_followup", tool_name="Shell", tool_input=commit), {"permission": "allow"})
        self.assertTrue(self.no(self.send("ui_pre_after_followup", generation_id="someone-else", tool_name="Shell",
                                          tool_input=commit)))

    def test_typed_onay_in_the_terminal_ui_as_it_happened(self):
        onay = self.fx("ui_before_submit_prompt_onay")
        self.assertEqual(onay["prompt"], "onay")
        conv = onay["conversation_id"]
        self.use(conv)
        self.start()
        self.advance_to_approve()
        self.cli("await", "-p", "demo", "Plan onayı bekleniyor", env=self.shell_env)
        self.assertEqual(self.send("ui_stop_completed", conversation_id=conv, session_id=conv), {})      # waiting: no nudge
        self.assertEqual(self.send("ui_before_submit_prompt_onay"), {"continue": True})
        appr = json.loads((self.proj / ".orkestra" / "runs" / self.rid / "approval.json").read_text())
        self.assertEqual((appr["decision"], appr["detail"]), ("approve", "onay"))
        self.assertEqual(self.state()["status"], "active")
        self.phase_next()


class TestCursorRobustness(CursorBase):
    def raw(self, event, stdin):
        cp = subprocess.run([sys.executable, str(helpers.CURSOR_HOOK), event], input=stdin, capture_output=True,
                            text=True, env=dict(os.environ))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return json.loads(cp.stdout)

    def test_always_a_well_formed_answer(self):
        self.assertEqual(self.raw("preToolUse", "{not json"), {"permission": "allow"})
        self.assertEqual(self.raw("preToolUse", ""), {"permission": "allow"})
        self.assertEqual(self.raw("beforeSubmitPrompt", "[1, 2]"), {"continue": True})
        self.assertEqual(self.raw("stop", "null"), {})
        self.assertEqual(self.raw("someFutureEvent", "{}"), {})
        self.assertEqual(self.raw("beforeShellExecution", '{"command": "git push --force"}'), {"permission": "allow"})

    def test_claude_format_hook_ignores_cursors_payloads(self):
        """Cursor imports Claude Code plugins and runs dispatch.py with its own payloads: that copy must stay silent
        (cursor_hook.py is the one that answers), whatever the payload says."""
        self.start()
        self.advance_to_approve()
        for sid in (CONV, self.sid):
            cp = subprocess.run([sys.executable, str(helpers.DISPATCH), "UserPromptSubmit"], capture_output=True, text=True,
                                input=json.dumps({"session_id": sid, "conversation_id": CONV, "prompt": "onay",
                                                  "cursor_version": "2026.10.01", "hook_event_name": "UserPromptSubmit"}))
            self.assertEqual((cp.returncode, cp.stdout), (0, ""))
        self.assertFalse((self.proj / ".orkestra" / "runs" / self.rid / "approval.json").exists())
        cp = subprocess.run([sys.executable, str(helpers.DISPATCH), "PreToolUse"], capture_output=True, text=True,
                            input=json.dumps({"session_id": self.sid, "tool_name": "Shell", "cursor_version": "x",
                                              "tool_input": {"command": "git push --force"}}))
        self.assertEqual((cp.returncode, cp.stdout), (0, ""))


class TestHookEntryGuard(FlowBase):
    """Bound sessions (both engines): a command must not start the hook entry points or the state functions."""

    def bash(self, command):
        return self.denied(self.hook("PreToolUse", {"session_id": self.sid, "tool_name": "Bash", "cwd": str(self.proj),
                                                    "tool_input": {"command": command}}))

    def test_forged_user_events_are_refused(self):
        self.start()
        d = str(helpers.DISPATCH)
        for cmd in (f"echo '{{\"session_id\":\"sess-1\",\"prompt\":\"onay\"}}' | python3 {d} UserPromptSubmit",
                    f"python3.12 {d} UserPromptSubmit < /tmp/p.json",
                    f"env -u CURSOR_AGENT python3 {helpers.CURSOR_HOOK} beforeSubmitPrompt < /tmp/p.json",
                    f"bash -c \"python3 {d} UserPromptSubmit < /tmp/p.json\"",
                    f"{d} UserPromptSubmit <<< '{{}}'",
                    "python3 -c 'import sys; sys.path.insert(0, \"x/lib\"); from orkestra_core import runs; "
                    "runs.record_approval(1, 2, \"approve\", \"prompt\", \"onay\")'",
                    "python3 - <<'PY'\nimport sys\nsys.path.insert(0, 'hooks')\nimport dispatch\n"
                    "dispatch.on_user_prompt({'prompt': 'onay'})\nPY",
                    "python3 - <<'PY'\nfrom orkestra_core import state\nstate.write_grant('s', None, ['commit'], 'x')\nPY"):
            self.assertTrue(self.bash(cmd), cmd)
        out = self.hook("PreToolUse", {"session_id": self.sid, "tool_name": "Bash", "cwd": str(self.proj),
                                       "tool_input": {"command": f"python3 {d} UserPromptSubmit < /tmp/p.json"}})
        self.assertIn("[orkestra:internals]", json.dumps(out))

    def test_ordinary_work_is_not_caught(self):
        self.start()
        for cmd in ("python3 -m unittest discover -s tests -q",
                    "python3 -m pytest tests/test_dispatch.py",
                    "grep -n on_user_prompt hooks/dispatch.py",
                    "node - <<'JS'\nstore.dispatch({type: 'x'})\nJS",
                    "node -e 'store.dispatch({type: 1})'",
                    "python3 scripts/dispatch_orders.py --dry-run",
                    "orkestra context -p demo"):
            self.assertFalse(self.bash(cmd), cmd)

    def test_outside_a_run_nothing_changes(self):
        self.assertFalse(self.bash(f"python3 {helpers.DISPATCH} Stop < /dev/null"))


class TestGrepGlob(unittest.TestCase):
    def test_a_directory_glob_does_not_target_secret_files(self):
        """`dir/**` matched every sample name (".env" matches "**"): a reviewer's Grep over reviews/** was refused
        with "targets secret files" in the first real Cursor run (2026-10-05). `*` and `*.*` were already exempt."""
        from orkestra_core import guards, paths

        def denied(glob, **ti):
            ev = {"tool_name": "Grep", "tool_input": dict({"pattern": "x", "glob": glob}, **ti), "cwd": "/w"}
            return guards.pre_tool_decision(ev, paths.DEFAULT_GLOBAL_CONFIG, None, []).denied

        for g in ("**/reviews/**", "src/**", "**", "*", "*.*", "**/*.md", "tests/**/*.py", ".env.example"):
            self.assertFalse(denied(g), g)
        for g in (".env*", "**/.env", "**/*.env", "*.pem", "config/**/.env.*", "**/id_rsa*", "*.p8"):
            self.assertTrue(denied(g), g)
        self.assertTrue(denied("**", path="/w/.env"))                   # the path itself is still checked


class TestCliEngines(CursorBase):
    def test_engine_of_a_run_default_flag_and_config(self):
        out = self.cli("start", "-p", "demo", "--title", "T", "--request", "bir şey yap", "--size", "S").stdout
        rid = out.split("run=")[-1].split()[0]
        st = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertEqual(st["engine"], "claude")                                    # the default
        self.assertNotIn("engine", self.cli("status", "-p", "demo").stdout)
        self.assertIn("- motor: claude", (self.proj / ".orkestra" / "features" / rid / "request.md").read_text())
        self.cli("abort", "-p", "demo", "test")
        self.cli("start", "-p", "demo", "--title", "T2", "--request", "bir şey yap", "--size", "S", "--engine", "cursor")
        self.assertEqual(json.loads(self.cli("status", "--json", "-p", "demo").stdout)["engine"], "cursor")
        self.cli("abort", "-p", "demo", "test")
        cp = self.cli("start", "-p", "demo", "--title", "T3", "--request", "bir şey yap", "--size", "S", "--engine", "gemini",
                      check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"engine": {"default": "cursor"}}))
        self.cli("start", "-p", "demo", "--title", "T4", "--request", "bir şey yap", "--size", "S")
        self.assertEqual(json.loads(self.cli("status", "--json", "-p", "demo").stdout)["engine"], "cursor")
        self.cli("abort", "-p", "demo", "test")
        # the agent that types the command wins over the configured default
        self.cli("start", "-p", "demo", "--title", "T5", "--request", "bir şey yap", "--size", "S", env={"CLAUDECODE": "1"})
        self.assertEqual(json.loads(self.cli("status", "--json", "-p", "demo").stdout)["engine"], "claude")

    def test_instructions_are_served_in_the_askers_vocabulary(self):
        plain = self.cli("playbook", "implement").stdout
        self.assertIn("orkestra:implementer", plain)
        self.assertNotIn(EN.NOTES_MARK, plain)
        cur = self.cli("playbook", "implement", env=self.shell_env).stdout
        self.assertTrue(cur.startswith(EN.NOTES_MARK))
        self.assertIn("orkestra-implementer", cur)
        self.assertNotIn("orkestra:implementer", cur)
        self.assertNotIn(EN.ASK_FOOTNOTE, cur)
        run = self.cli("playbook", "run", env=self.shell_env).stdout               # the run skill, for a first prompt
        self.assertIn("orkestra start", run)
        self.assertIn('subagent_type "orkestra-<name>"', run)                       # the header explains the mapping
        self.assertIn('`subagent_type: "orkestra-<name>"`', run)                    # …and the document uses it
        self.assertEqual(run.count(EN.NOTES_MARK), 1)

    def test_next_step_mentions_what_askuserquestion_means_once(self):
        self.start()
        self.advance_to_approve()
        out = self.cli("context", "-p", "demo", env=self.shell_env).stdout
        self.assertEqual(out.count(EN.ASK_FOOTNOTE), 1)
        self.assertIn("orkestra playbook headless", out)
        self.assertNotIn(EN.ASK_FOOTNOTE, self.cli("context", "-p", "demo").stdout)

    @unittest.skipUnless(os.name != "nt", "#!/bin/sh make_exec fixture (FR-007)")
    def test_terminal_launcher(self):
        claude = make_exec(self.tmp / "bin" / "claude", "#!/bin/sh\nexit 0\n")
        agent = make_exec(self.tmp / "bin" / "agent", "#!/bin/sh\nexit 0\n")
        env = {"ORKESTRA_CLAUDE_BIN": str(claude), "ORKESTRA_CURSOR_BIN": str(agent)}
        cp = self.cli("run", "-p", "demo", "--engine", "cursor", "--print", "toplama ekle", env=env, check=False)
        self.assertNotEqual(cp.returncode, 0)                                       # never an unguarded Cursor agent
        self.assertIn("orkestra cursor install", cp.stderr)
        from orkestra_core import cursorkit
        cursorkit.install()                                                         # into the sandbox's Cursor home
        out = self.cli("run", "-p", "demo", "--print", "toplama", "ekle", env=env).stdout
        self.assertIn(str(claude), out)
        self.assertIn("/orkestra:run toplama ekle", out)
        self.assertIn(f"cd {self.proj}", out)
        out = self.cli("run", "-p", "demo", "--engine", "cursor", "--print", "toplama ekle", env=env).stdout
        self.assertNotIn("--force", out)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"engine": {"cursor": {"interactive_args": ["--force"], "model": "m1"}}}))
        out = self.cli("run", "-p", "demo", "--engine", "cursor", "--print", "toplama ekle", env=env).stdout
        self.assertIn(f"{agent} --model m1 --force ", out)                          # the user's own flags for the UI
        self.assertIn(str(agent), out)
        self.assertIn("orkestra playbook run", out)
        self.assertIn("toplama ekle", out)
        for inside in ({"CLAUDECODE": "1"}, self.shell_env):                        # never from inside an agent
            cp = self.cli("run", "-p", "demo", "--print", "toplama ekle", env=dict(env, **inside), check=False)
            self.assertNotEqual(cp.returncode, 0)
            cp = self.cli("cursor", "install", "--dry-run", env=dict(env, **inside), check=False)
            self.assertNotEqual(cp.returncode, 0)
        cp = self.cli("run", "-p", "demo", "--engine", "cursor", "--print", "toplama ekle",
                      env={"ORKESTRA_CURSOR_BIN": str(self.tmp / "nope"), "PATH": "/usr/bin:/bin", "HOME": str(self.tmp)},
                      check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("cursor CLI not found", cp.stderr + cp.stdout)

    @unittest.skipUnless(os.name != "nt", "#!/bin/sh make_exec fixture (FR-007)")
    def test_launcher_really_starts_the_engine_in_the_project(self):
        fake = make_exec(self.tmp / "bin" / "agent", "#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$@\" > \"$PWD/launched.txt\"\n"
                                                      "command -v orkestra >> \"$PWD/launched.txt\"\n"
                                                      "echo \"headless=${ORKESTRA_HEADLESS:-}\" >> \"$PWD/launched.txt\"\n")
        # hooks kept in the project's own .cursor/hooks.json count as well
        from orkestra_core import cursorkit
        (self.proj / ".cursor").mkdir()
        (self.proj / ".cursor" / "hooks.json").write_text(json.dumps(cursorkit.merge_hooks(None)))
        self.cli("run", "-p", "demo", "--engine", "cursor", "çıkarma ekle", env={"ORKESTRA_CURSOR_BIN": str(fake)})
        got = (self.proj / "launched.txt").read_text().splitlines()
        self.assertEqual(Path(got[0]).resolve(), self.proj.resolve())
        self.assertIn("İstek: çıkarma ekle", "\n".join(got))
        self.assertIn(str(helpers.PLUGIN / "bin" / "orkestra"), got)                # Cursor's shell can call the CLI
        self.assertIn("headless=", got)                                             # interactive: the user answers there


class TestCursorKit(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        # the assertions below read the English wording of the kit's output (the suite itself is pinned to Turkish);
        # test_output_follows_the_language checks the Turkish side
        saved = os.environ.get("ORKESTRA_LANG")
        os.environ["ORKESTRA_LANG"] = "en"
        self.addCleanup(lambda: os.environ.__setitem__("ORKESTRA_LANG", saved) if saved is not None
                        else os.environ.pop("ORKESTRA_LANG", None))
        self.chome = Path(os.environ["ORKESTRA_CURSOR_HOME"])
        self.shim = Path(os.environ["ORKESTRA_SHIM_DIR"])
        self.shim.mkdir()
        # a PATH without any `orkestra` yet (the shim directory is on it, as ~/.local/bin usually is)
        self.env = {"PATH": os.pathsep.join([str(self.shim), "/usr/bin", "/bin", str(Path(sys.executable).parent)]),
                    "ORKESTRA_CURSOR_BIN": str(make_exec(self.tmp / "bin" / "agent", "#!/bin/sh\nexit 0\n"))}

    def kit(self, *args, check=True, lang=None):
        return self.cli("cursor", *args, check=check, env=dict(self.env, **({"ORKESTRA_LANG": lang} if lang else {})))

    def test_output_follows_the_language_but_the_row_names_do_not(self):
        """r2-12: the action lines, the summary and the details were English in a Turkish session."""
        english = self.kit("install", "--dry-run", lang="en").stdout
        turkish = self.kit("install", "--dry-run", lang="tr").stdout
        self.assertIn("would link ", english)
        self.assertRegex(english, r"would update \S*hooks\.json: orkestra entries for")
        self.assertIn("cursor: dry run, nothing written", english)
        self.assertIn("şunu yapardı: bağla ", turkish)
        self.assertRegex(turkish, r"şunu yapardı: \S*hooks\.json güncelle: ")
        self.assertIn("cursor: deneme modu, hiçbir şey yazılmadı", turkish)
        for word in ("link ", "update hooks.json", "dry run", "would "):
            self.assertNotIn(word, turkish, word)
        self.assertFalse(self.chome.exists(), "a dry run creates nothing")
        self.kit("install")
        for lang, word in (("en", "installed"), ("tr", "kuruldu")):
            status = self.kit("status", lang=lang, check=False).stdout
            for label in ("[OK] hooks.json entries", "[OK] skills and subagents installed", "[OK] `orkestra` on PATH",
                          "[OK] generated assets current"):
                self.assertIn(label, status, f"{lang}: the row name is a stable identifier")
        self.assertIn("8/8 events", self.kit("status", lang="en", check=False).stdout)
        self.assertIn("8/8 olay", self.kit("status", lang="tr", check=False).stdout)
        again = self.kit("install", lang="tr").stdout
        self.assertIn("değiştirilecek bir şey yok", again)
        gone = self.kit("uninstall", lang="tr").stdout
        self.assertIn("kaldır ", gone)
        self.assertIn("cursor: kaldırıldı", gone)

    def test_build_is_allowed_from_an_agent_shell_but_install_is_not(self):
        """`cursor build` writes only the checkout's generated tree, so contributors' agents may run it (AGENTS.md);
        everything that touches the user's Cursor configuration stays terminal-only."""
        import argparse
        from unittest import mock
        from orkestra_core import cli as CLI
        from orkestra_core import cursorkit as CK
        out = self.tmp / "assets"
        with mock.patch.object(CLI, "_ASKER", "claude"), mock.patch.object(CK, "ASSETS", out):
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(CLI.cmd_cursor(argparse.Namespace(action="build")), 0)
            self.assertTrue(any(out.rglob("*.md")))
            for action in ("install", "uninstall", "status"):
                with self.assertRaises(CLI.OrkestraError):
                    CLI.cmd_cursor(argparse.Namespace(action=action, copy=False, dry_run=True))

    def test_build_dry_run_reports_the_files_and_writes_nothing(self):
        """`--dry-run` on `cursor build` is honoured: an agent that believes it only looks must not rewrite the tree."""
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from orkestra_core import cli as CLI
        from orkestra_core import cursorkit as CK
        out = self.tmp / "assets"
        with mock.patch.object(CLI, "_ASKER", "claude"), mock.patch.object(CK, "ASSETS", out):
            for lang in ("en", "tr"):
                with mock.patch.dict(os.environ, {"ORKESTRA_LANG": lang}), redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(CLI.cmd_cursor(argparse.Namespace(action="build", dry_run=True)), 0)
                self.assertFalse(out.exists() and any(out.rglob("*")), "a dry run must not create the tree")
                self.assertIn(str(len(CK.expected())), buf.getvalue())
                self.assertIn("dry run" if lang == "en" else "deneme modu", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(CLI.cmd_cursor(argparse.Namespace(action="build", dry_run=False)), 0)
            self.assertEqual(CK.stale(out), [])
            victim = next(out.rglob("*.md"))
            victim.unlink()
            with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"}), redirect_stdout(io.StringIO()) as buf:
                CLI.cmd_cursor(argparse.Namespace(action="build", dry_run=True))
            self.assertFalse(victim.exists(), "the missing file was recreated by a dry run")
            self.assertIn(victim.relative_to(out).as_posix(), buf.getvalue())

    def test_generated_assets_are_current(self):
        from orkestra_core import cursorkit as CK
        self.assertEqual(CK.stale(), [], "run `orkestra cursor build` and commit plugins/orkestra/cursor")
        self.assertEqual(len(CK.expected()), len(EN.SKILL_NAMES) + len(EN.AGENT_NAMES))
        agent = (CK.ASSETS / "agents" / "orkestra-reviewer.md").read_text()
        head = agent.split("---")[1]
        self.assertIn("name: orkestra-reviewer", head)
        self.assertIn("readonly: false", head)              # a Cursor "readonly" subagent has no shell: guards do the limiting
        self.assertIn("is_background: false", head)
        self.assertIn(EN.NOTES_MARK, agent)
        self.assertNotIn("orkestra:reviewer", agent.split("---", 2)[2])
        for sentence in ("own account, not proof", "does not count", "neither restored nor intentionally retired"):
            self.assertIn(sentence, agent)
        verifier = (CK.ASSETS / "agents" / "orkestra-verifier.md").read_text()
        self.assertIn("is not proof: mark the row FAIL", verifier)
        adversary = (CK.ASSETS / "agents" / "orkestra-adversary.md").read_text()
        self.assertIn("the spec does not retire it", adversary)
        skill = (CK.ASSETS / "skills" / "orkestra-run" / "SKILL.md").read_text()
        self.assertIn("name: orkestra-run", skill.split("---")[1])
        self.assertNotIn("$ARGUMENTS", skill)
        self.assertIn('`subagent_type: "orkestra-<name>"`', skill)
        self.assertIn("A subagent `orkestra:<name>` is the Cursor subagent `orkestra-<name>`", skill)
        # FR-008: onboard skill heads + cursor build mention setup / copilot install
        onboard = (CK.ASSETS / "skills" / "orkestra-onboard" / "SKILL.md").read_text()
        self.assertIn("name: orkestra-onboard", onboard.split("---")[1])
        self.assertRegex(onboard, r"\bsetup\b")
        self.assertIn("copilot install", onboard)
        # FR-008: ORKESTRA.md Scope / ORKESTRA.tr.md Kapsam: Copilot engine in scope (bridge-Copilot may stay out)
        for name, heading in (("ORKESTRA.md", "## Scope"), ("ORKESTRA.tr.md", "## Kapsam")):
            doc = (helpers.PLUGIN.parent.parent / name).read_text(encoding="utf-8")
            section = doc.split(heading, 1)[1].split("## ", 1)[0]
            self.assertNotIn("Copilot, claude.ai", section)  # old bundled "out of scope" list
            self.assertRegex(section, r"\bsetup\b")
            self.assertIn("copilot install", section)

    @unittest.skipUnless(os.name != "nt", "POSIX symlink shim assertions; Win .cmd covered separately (FR-007)")
    def test_ensure_path_shim_creates_fixes_dangling_and_noops(self):
        """FR-001 / E1–E3 / SC-001: ensure_path_shim is the single PATH shim API; install uses it."""
        from orkestra_core import cursorkit as CK
        import inspect
        prev_path = os.environ.get("PATH")
        path_no_orkestra = os.pathsep.join([str(self.shim), "/usr/bin", "/bin", str(Path(sys.executable).parent)])
        try:
            os.environ["PATH"] = path_no_orkestra
            self.assertIsNone(shutil.which("orkestra"))

            # SC-001 / happy: shim dir on PATH, no orkestra → symlink to BIN
            done = CK.ensure_path_shim()
            self.assertTrue((self.shim / "orkestra").is_symlink())
            self.assertEqual(os.path.realpath(self.shim / "orkestra"), os.path.realpath(helpers.BIN))
            self.assertTrue(any("link" in a and str(helpers.BIN) in a for a in done))
            # second call is harmless (idempotent)
            self.assertEqual(CK.ensure_path_shim(), [])

            # E2: dangling symlink is renewed
            (self.shim / "orkestra").unlink()
            os.symlink(self.tmp / "gone" / "orkestra", self.shim / "orkestra")
            self.assertTrue((self.shim / "orkestra").is_symlink())
            self.assertFalse((self.shim / "orkestra").exists())
            fixed = CK.ensure_path_shim()
            self.assertEqual(os.path.realpath(self.shim / "orkestra"), os.path.realpath(helpers.BIN))
            self.assertTrue(any("link" in a for a in fixed))

            # E1: parent missing → no-op, no crash
            missing_parent = self.tmp / "no-parent-dir" / "bin"
            os.environ["ORKESTRA_SHIM_DIR"] = str(missing_parent)
            self.assertFalse(missing_parent.exists())
            self.assertEqual(CK.ensure_path_shim(), [])
            self.assertFalse((missing_parent / "orkestra").exists())
            os.environ["ORKESTRA_SHIM_DIR"] = str(self.shim)

            # E3: orkestra already on PATH → new link not required
            other = self.tmp / "other-bin"
            other.mkdir()
            target = other / "orkestra"
            os.symlink(helpers.BIN, target)
            os.environ["PATH"] = os.pathsep.join([str(other), path_no_orkestra])
            (self.shim / "orkestra").unlink(missing_ok=True)
            self.assertIsNotNone(shutil.which("orkestra"))
            self.assertEqual(CK.ensure_path_shim(), [])
            self.assertFalse(os.path.lexists(self.shim / "orkestra"))

            # install uses the same API
            self.assertIn("ensure_path_shim", inspect.getsource(CK.install))
        finally:
            if prev_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = prev_path

    def test_ensure_path_shim_windows_writes_cmd(self):
        """FR-002 / E4–E5 / SC-002: on Windows write orkestra.cmd instead of requiring symlink."""
        from orkestra_core import cursorkit as CK
        from unittest import mock

        prev_path = os.environ.get("PATH")
        path_no_orkestra = os.pathsep.join([str(self.shim), str(Path(sys.executable).parent)])
        try:
            os.environ["PATH"] = path_no_orkestra
            (self.shim / "orkestra").unlink(missing_ok=True)
            (self.shim / "orkestra.cmd").unlink(missing_ok=True)

            with mock.patch.object(CK, "_is_windows", return_value=True):
                done = CK.ensure_path_shim()
            cmd = self.shim / "orkestra.cmd"
            self.assertTrue(cmd.is_file(), "Windows shim must be orkestra.cmd")
            text = cmd.read_text(encoding="utf-8")
            self.assertIn(str(helpers.BIN), text)
            self.assertIn(sys.executable, text)
            self.assertTrue(any("write" in a and "orkestra.cmd" in a for a in done))
            with mock.patch.object(CK, "_is_windows", return_value=True):
                self.assertEqual(CK.ensure_path_shim(), [])  # idempotent
        finally:
            if prev_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = prev_path

    @unittest.skipUnless(os.name != "nt", "POSIX symlink PATH shim assertions (FR-007)")
    def test_install_keeps_the_users_own_hooks_and_is_idempotent(self):
        mine = {"version": 1, "hooks": {
            "beforeSubmitPrompt": [{"command": "./hooks/safety-gate.py", "failClosed": True}],
            "stop": [{"command": "python3 ~/hindsight/stop.py"}],
            "beforeMCPExecution": [{"command": "./hooks/safety-gate.py"}]}, "extra": {"keep": 1}}
        self.chome.mkdir(parents=True)
        (self.chome / "hooks.json").write_text(json.dumps(mine))
        dry = self.kit("install", "--dry-run").stdout
        self.assertIn("would", dry)
        self.assertEqual(json.loads((self.chome / "hooks.json").read_text()), mine)         # dry run writes nothing
        self.assertFalse((self.chome / "skills").exists())
        out = self.kit("install").stdout
        doc = json.loads((self.chome / "hooks.json").read_text())
        self.assertEqual(doc["extra"], {"keep": 1})
        self.assertEqual(doc["hooks"]["beforeMCPExecution"], mine["hooks"]["beforeMCPExecution"])
        self.assertEqual(doc["hooks"]["beforeSubmitPrompt"][0], mine["hooks"]["beforeSubmitPrompt"][0])    # theirs first
        self.assertEqual(doc["hooks"]["stop"][0], mine["hooks"]["stop"][0])
        for event in ("sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse", "postToolUseFailure", "stop",
                      "subagentStart", "sessionEnd"):
            ours = [e for e in doc["hooks"][event] if "cursor_hook.py" in e["command"]]
            self.assertEqual(len(ours), 1, event)
            self.assertTrue(ours[0]["command"].endswith(event))
            self.assertIn(str(helpers.CURSOR_HOOK), ours[0]["command"])
            self.assertEqual(doc["hooks"][event][-1], ours[0])
        backups = list(self.chome.glob("hooks.json.orkestra-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), mine)
        for n in EN.SKILL_NAMES:
            self.assertTrue((self.chome / "skills" / f"orkestra-{n}" / "SKILL.md").is_file(), n)
        for n in EN.AGENT_NAMES:
            self.assertTrue((self.chome / "agents" / f"orkestra-{n}.md").is_file(), n)
        self.assertTrue((self.shim / "orkestra").is_symlink())                               # CLI for Cursor's shell
        self.assertEqual(os.path.realpath(self.shim / "orkestra"), os.path.realpath(helpers.BIN))
        self.assertIn("[OK] hooks.json entries", out)
        self.assertIn("[OK] skills and subagents installed", out)
        self.assertIn("[OK] `orkestra` on PATH", out)
        # the installed hook command works as Cursor would start it
        cmd = [e for e in doc["hooks"]["preToolUse"] if "cursor_hook.py" in e["command"]][0]["command"]
        cp = subprocess.run(cmd, shell=True, input='{"tool_name": "Read", "tool_input": {}}', capture_output=True, text=True)
        self.assertEqual(json.loads(cp.stdout), {"permission": "allow"})
        # again: nothing to do, no second backup
        again = self.kit("install").stdout
        self.assertIn("nothing to change", again)
        self.assertEqual(len(list(self.chome.glob("hooks.json.orkestra-backup-*"))), 1)
        from orkestra_core import cursorkit as CK
        self.assertTrue(CK.hooks_installed())
        # uninstall removes ours and only ours
        self.kit("uninstall")
        doc = json.loads((self.chome / "hooks.json").read_text())
        self.assertEqual(doc["hooks"], mine["hooks"])
        self.assertEqual(doc["extra"], {"keep": 1})
        self.assertFalse((self.chome / "skills" / "orkestra-run").exists())
        self.assertFalse((self.shim / "orkestra").exists())
        self.assertFalse(CK.hooks_installed())

    def test_install_into_an_empty_home_and_copy_mode(self):
        out = self.kit("install", "--copy").stdout
        self.assertIn("installed", out)
        doc = json.loads((self.chome / "hooks.json").read_text())
        self.assertEqual(doc["version"], 1)
        self.assertEqual(len(doc["hooks"]), 8)
        self.assertEqual(list(self.chome.glob("hooks.json.orkestra-backup-*")), [])           # nothing to back up
        f = self.chome / "agents" / "orkestra-scout.md"
        self.assertTrue(f.is_file() and not f.is_symlink())
        self.assertFalse((self.chome / "skills" / "orkestra-run").is_symlink())

    def test_broken_hooks_json_is_not_touched(self):
        self.chome.mkdir(parents=True)
        (self.chome / "hooks.json").write_text("{ not json")
        cp = self.kit("install", check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not valid JSON", cp.stderr + cp.stdout)
        self.assertEqual((self.chome / "hooks.json").read_text(), "{ not json")
        self.assertFalse((self.chome / "skills").exists())

    def test_an_old_copy_in_claudes_plugin_registry_is_pointed_out(self):
        from orkestra_core import cursorkit as CK
        cdir = self.tmp / "claude-config"
        old = cdir / "plugins" / "cache" / "orkestra-local" / "orkestra" / "1.0.0"
        (old / "hooks").mkdir(parents=True)
        (old / "hooks" / "dispatch.py").write_text("# old\n")
        reg = {"version": 2, "plugins": {"orkestra@orkestra-local": [{"scope": "user", "installPath": str(old), "version": "1.0.0"}],
                                         "other@x": [{"installPath": str(self.tmp)}]}}
        (cdir / "plugins" / "installed_plugins.json").write_text(json.dumps(reg))
        os.environ["CLAUDE_CONFIG_DIR"] = str(cdir)
        try:
            self.assertIn("1.0.0", CK.stale_claude_copy())
            out = self.kit("status", check=False).stdout
            self.assertIn("[i ] an older orkestra copy", out)
            self.assertIn("claude plugin update orkestra@orkestra-local", out)
            (old / "hooks" / "cursor_hook.py").write_text("# a refreshed copy knows Cursor\n")
            self.assertEqual(CK.stale_claude_copy(), "")
            reg["plugins"]["orkestra@orkestra-local"][0]["installPath"] = str(helpers.PLUGIN)        # loaded in place
            (cdir / "plugins" / "installed_plugins.json").write_text(json.dumps(reg))
            self.assertEqual(CK.stale_claude_copy(), "")
            (cdir / "plugins" / "installed_plugins.json").write_text("{ broken")
            self.assertEqual(CK.stale_claude_copy(), "")
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_status_reports_what_is_missing(self):
        cp = self.kit("status", check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("[!!] hooks.json entries — 0/8 events", cp.stdout)
        self.assertIn("[OK] generated assets current", cp.stdout)


class TestCopilotKit(helpers.Sandbox):
    """FR-003 / SC-002 / E3 / E4: materialize Copilot hooks under $COPILOT_HOME/plugins/orkestra."""

    def setUp(self):
        super().setUp()
        self.cophome = self.tmp / "copilot-home"
        self._copilot_home = os.environ.get("COPILOT_HOME")
        os.environ["COPILOT_HOME"] = str(self.cophome)

    def tearDown(self):
        if self._copilot_home is None:
            os.environ.pop("COPILOT_HOME", None)
        else:
            os.environ["COPILOT_HOME"] = self._copilot_home
        super().tearDown()

    def hooks_path(self) -> Path:
        return self.cophome / "plugins" / "orkestra" / "com.github.copilot" / "hooks" / "hooks.json"

    def test_install_activates_hooks_idempotent_dry_run_and_uninstall(self):
        from orkestra_core import copilotkit as CK

        mine = {"version": 1, "hooks": {
            "UserPromptSubmit": [{"command": "./hooks/other.py"}],
            "SessionStart": [{"command": "echo keep"}],
        }, "extra": {"keep": 1}}
        self.hooks_path().parent.mkdir(parents=True)
        self.hooks_path().write_text(json.dumps(mine))

        dry = CK.install(dry=True)
        self.assertTrue(dry)
        self.assertEqual(json.loads(self.hooks_path().read_text()), mine)

        rows = CK.status()
        labels = {label: ok for ok, label, _ in rows}
        self.assertFalse(labels.get("copilot hooks"))

        actions = CK.install()
        self.assertTrue(actions)
        plugin = EN.copilot_plugin_dir()
        self.assertTrue(plugin)
        self.assertTrue(EN.copilot_hooks_active(plugin))
        self.assertTrue(CK.hooks_active())
        doc = json.loads(self.hooks_path().read_text())
        self.assertEqual(doc["extra"], {"keep": 1})
        self.assertEqual(doc["hooks"]["UserPromptSubmit"][0], mine["hooks"]["UserPromptSubmit"][0])
        self.assertEqual(doc["hooks"]["SessionStart"][0], mine["hooks"]["SessionStart"][0])
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            ours = [e for e in doc["hooks"][event] if "copilot_hook.py" in (e.get("bash") or e.get("command") or "")]
            self.assertEqual(len(ours), 1, event)
            self.assertEqual(ours[0]["type"], "command")
            self.assertTrue(ours[0]["bash"].endswith(event))
            self.assertEqual(ours[0]["powershell"], "& " + ours[0]["bash"])      # PowerShell needs the call operator
            self.assertEqual(doc["hooks"][event][-1], ours[0])
        backups = list(self.hooks_path().parent.glob("hooks.json.orkestra-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), mine)

        again = CK.install()
        self.assertEqual(again, [])
        self.assertEqual(len(list(self.hooks_path().parent.glob("hooks.json.orkestra-backup-*"))), 1)
        self.assertTrue(EN.copilot_hooks_active(EN.copilot_plugin_dir()))

        rows = CK.status()
        labels = {label: ok for ok, label, _ in rows}
        self.assertTrue(labels.get("copilot hooks"))

        root = CK.plugin_dir()
        self.assertEqual(json.loads((root / "plugin.json").read_text())["name"], "orkestra")
        skill = (root / "skills" / "orkestra-run" / "SKILL.md").read_text()
        self.assertIn("name: orkestra-run", skill)
        self.assertIn(EN.COPILOT_MARK, skill)
        self.assertEqual(len(list((root / "skills").iterdir())), len(EN.SKILL_NAMES))

        CK.uninstall()
        self.assertFalse((root / "plugin.json").exists())
        self.assertFalse((root / "skills").exists())
        doc = json.loads(self.hooks_path().read_text())
        self.assertEqual(doc["hooks"], mine["hooks"])
        self.assertEqual(doc["extra"], {"keep": 1})
        self.assertFalse(EN.copilot_hooks_active(EN.copilot_plugin_dir()))
        self.assertFalse(CK.hooks_active())

    def test_broken_hooks_json_is_not_touched(self):
        from orkestra_core import copilotkit as CK

        self.hooks_path().parent.mkdir(parents=True)
        self.hooks_path().write_text("{ not json")
        before = self.hooks_path().read_bytes()
        with self.assertRaises(EN.EngineError):
            CK.install(dry=True)
        self.assertEqual(self.hooks_path().read_bytes(), before)
        with self.assertRaises(EN.EngineError):
            CK.install()
        self.assertEqual(self.hooks_path().read_bytes(), before)

    def test_cli_install_and_asker_refusal(self):
        """FR-003 / E5: terminal install|status|uninstall; agent shell must refuse."""
        from orkestra_core import cliparse as CP
        from orkestra_core import copilotkit as CK
        from orkestra_core import cli as CLI

        self.assertIn("copilot", CP.SUB_ACTION)
        self.assertIs(CLI.HANDLERS.get("copilot"), CLI.cmd_copilot)

        missing = self.cli("copilot", "status", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("[!!] copilot hooks", missing.stdout)

        out = self.cli("copilot", "install").stdout
        self.assertIn("copilot", out.lower())
        self.assertTrue(EN.copilot_hooks_active(EN.copilot_plugin_dir()))
        self.assertTrue(CK.hooks_active())

        ok = all(good is not False for good, _, _ in CK.status())
        status = self.cli("copilot", "status", check=False)
        self.assertEqual(status.returncode, 0 if ok else 1)
        self.assertIn("[OK] copilot hooks", status.stdout)

        self.cli("copilot", "uninstall")
        self.assertFalse(CK.hooks_active())

        refused = self.cli("copilot", "install", check=False, env={"ORKESTRA_ENGINE": "copilot"})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("terminal", (refused.stderr + refused.stdout).lower())
        self.assertFalse(CK.hooks_active())


FAKE_AGENT = """#!/usr/bin/env python3
import json, os, sys
open(os.path.join(os.getcwd(), "resumed.json"), "w").write(json.dumps(
    {"args": sys.argv[1:], "stdin": sys.stdin.read(), "headless": os.environ.get("ORKESTRA_HEADLESS"),
     "path0": os.environ.get("PATH", "").split(os.pathsep)[0], "agent_env": os.environ.get("CURSOR_AGENT")}))
print(json.dumps({"type": "result", "result": "ok"}))
"""


class TestWatchdogCursor(CursorBase):
    def setUp(self):
        super().setUp()
        from orkestra_core import watchdog as W
        self.W = W
        import uuid
        self.use(str(uuid.uuid4()))         # the watchdog looks for live processes by this id: never a shared constant
        os.environ["ORKESTRA_CURSOR_BIN"] = str(make_exec(self.tmp / "bin" / "agent", FAKE_AGENT))
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"watchdog": {"macos_notify": False}}))

    def scan(self, in_future=0.0):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.W.main(as_json=True, now=time.time() + in_future)
        return json.loads(buf.getvalue())

    def start_headless(self):
        self.shell_env = dict(self.shell_env, ORKESTRA_HEADLESS="1")
        self.start()
        f = self.home / "sessions" / f"{self.sid}.json"
        os.utime(f, (time.time() - 1200, time.time() - 1200))

    def test_a_silent_headless_cursor_run_is_resumed_with_cursor_on_its_own_chat(self):
        self.start_headless()
        v = self.scan(in_future=1200)[0]
        self.assertIn("hooks are not installed", json.dumps(v))                     # never an unguarded Cursor agent
        self.assertFalse((self.proj / "resumed.json").exists())
        from orkestra_core import cursorkit
        cursorkit.install()                                                         # into the sandbox's Cursor home
        self.say("commit et", gen="g-old")                                          # a permission from an older message…
        f = self.home / "sessions" / f"{self.sid}.json"
        os.utime(f, (time.time() - 1200, time.time() - 1200))                       # …and then silence
        v = self.scan(in_future=1200 + 6 * 60)[0]
        self.assertIn("otomatik devam ettirildi", v["message"])
        for _ in range(50):
            if (self.proj / "resumed.json").exists():
                break
            time.sleep(0.1)
        got = json.loads((self.proj / "resumed.json").read_text())
        self.assertEqual(got["args"][:7], ["-p", "--output-format", "json", "--force", "--trust", "--resume", self.conv])
        self.assertIn("orkestra resume -p demo", got["args"][-1])                   # the prompt is the last argument
        self.assertEqual((got["stdin"], got["headless"], got["agent_env"]), ("", "1", None))
        self.assertEqual(got["path0"], str(helpers.PLUGIN / "bin"))
        self.assertIn(f"(cursor session c:{self.conv[:8]}", (self.fdir / "ledger.md").read_text())    # told apart by id
        self.assertTrue(self.no(self.shell("git commit -m x", gen=self.conv)))           # …does not reach the automatic turn

    def test_bridge_job_and_live_process_checks_know_cursor_ids(self):
        self.start_headless()
        jobs = self.home / "bridge" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "abc.json").write_text(json.dumps({"id": "abc", "session_id": self.conv, "engine": "cursor", "status": "running"}))
        self.assertTrue(self.W._bridge_owns([self.sid]))
        (jobs / "abc.json").write_text(json.dumps({"id": "abc", "session_id": self.conv, "status": "running"}))    # a claude job
        self.assertFalse(self.W._bridge_owns([self.sid]))
        self.assertTrue(self.W._bridge_owns([self.conv]))
        v = self.scan(in_future=1200)[0]
        self.assertNotIn("still alive", v["auto_resume"]["why"])                    # that claude job is another session


class TestWinForkFreeParity(unittest.TestCase):
    """FR-004 / E7: Windows spawn paths must not use os.fork; use detached process group flags."""

    def _assert_nt_fork_free(self, path: Path, *needles: str):
        src = path.read_text(encoding="utf-8")
        self.assertIn('os.name == "nt"', src, path.name)
        self.assertIn("CREATE_NEW_PROCESS_GROUP", src, path.name)
        self.assertIn("DETACHED_PROCESS", src, path.name)
        for n in needles:
            self.assertIn(n, src, f"{path.name} missing {n}")

    def test_engines_helpers_fake_agent_engines2_nt_branches(self):
        root = helpers.PLUGIN.parent.parent
        self._assert_nt_fork_free(Path(EN.__file__), "driver_prompt")
        self._assert_nt_fork_free(Path(helpers.__file__).resolve())
        self._assert_nt_fork_free(root / "bridge" / "tests" / "fake_agent.py")
        self._assert_nt_fork_free(Path(__file__).resolve().parent / "test_engines2.py", "CREATE_NEW_PROCESS_GROUP")


if __name__ == "__main__":
    unittest.main()
