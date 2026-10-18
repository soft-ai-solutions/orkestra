"""Pi is an optional engine: JSON mode argv, a safe session id, and no stdin."""
from __future__ import annotations

import contextlib
import io
import json

import helpers
from orkestra_core import cliparse, engines as EN


class TestPiEngine(helpers.Sandbox):
    def test_engines_include_pi_and_keep_the_original_three(self) -> None:
        self.assertEqual(EN.ENGINES, ("claude", "cursor", "copilot", "pi"))

    def test_headless_pi_uses_json_mode_and_session_id(self) -> None:
        from orkestra_core import pikit as PK
        missing = EN.headless_command("pi", "/bin/pi", "pi-S1", "do it", True, {})
        self.assertNotIn("--extension", missing["argv"])
        PK.install()
        spec = EN.headless_command("pi", "/bin/pi", "pi-S1", "do it", True, {})
        self.assertEqual(spec["argv"][:5], ["/bin/pi", "--mode", "json", "--session-id", "S1"])
        self.assertIn("--extension", spec["argv"])
        self.assertLess(spec["argv"].index("--session-id"), spec["argv"].index("--extension"))
        self.assertEqual(spec["argv"][-1], "do it")
        self.assertIsNone(spec["stdin"])
        again = EN.headless_command("pi", "/bin/pi", "S1", "again", False, {})
        self.assertEqual(again["argv"][:5], ["/bin/pi", "--mode", "json", "--session-id", "S1"])

    def test_pi_prompt_strips_nul_and_escapes_a_leading_dash(self) -> None:
        argv = EN.headless_command("pi", "/bin/pi", "S", "a\x00b", True, {})["argv"]
        self.assertEqual(argv[-1], "ab")
        dashed = EN.headless_command("pi", "/bin/pi", "S", "- x", True, {})["argv"][-1]
        self.assertTrue(dashed.startswith("\n"))
        self.assertIn("- x", dashed)

    def test_pi_session_id_drops_characters_pi_rejects(self) -> None:
        spec = EN.headless_command("pi", "/bin/pi", "pi-ab cd", "ok", True, {})
        self.assertEqual(spec["argv"][4], "ab-cd")
        self.assertNotIn(" ", spec["argv"][4])

    def test_session_key_round_trip(self) -> None:
        self.assertEqual(EN.session_key("pi", "abc"), "pi-abc")
        self.assertEqual(EN.session_engine("pi-abc"), "pi")
        self.assertEqual(EN.raw_session_id("pi-abc"), "abc")

    def test_pi_assistant_text_skips_a_bad_line_and_keeps_the_last_assistant(self) -> None:
        stream = "\n".join([
            "not json",
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "one"}]}}),
            json.dumps({"type": "message_end", "message": {"role": "user", "content": "ignore"}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": "two"}}),
        ])
        self.assertEqual(EN.pi_assistant_text(stream), "two")

    def test_pi_assistant_text_of_an_empty_stream_is_empty(self) -> None:
        self.assertEqual(EN.pi_assistant_text(""), "")
        self.assertEqual(EN.pi_assistant_text("{"), "")

    def test_pi_assistant_text_keeps_unicode_separators_inside_the_json_string(self) -> None:
        for sep in ("\u2028", "\u2029", "\u0085"):
            body = "A" + sep + "B"
            line = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": body}},
                              ensure_ascii=False)
            self.assertEqual(EN.pi_assistant_text(line + "\n"), body)

    def test_pi_turn_message_ignores_a_tool_result_and_an_empty_follow_up(self) -> None:
        rows = [
            {"type": "message_end", "message": {"role": "assistant", "content": "keep"}},
            {"type": "tool_execution_end", "result": {"content": [{"type": "text", "text": "a.txt"}]}},
            {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "toolCall"}]}},
            {"type": "message_end", "message": {"role": "assistant", "content": "Found a.txt"}},
            {"type": "agent_settled", "aborted": False},
        ]
        stream = "\n".join(json.dumps(row) for row in rows) + "\n"
        self.assertEqual(EN.pi_assistant_text(stream), "Found a.txt")
        self.assertEqual(EN.pi_turn_message(stream), "Found a.txt")

    def test_pi_session_id_does_not_keep_a_dot_dot_path(self) -> None:
        spec = EN.headless_command("pi", "/bin/pi", "../etc/passwd", "ok", True, {})
        self.assertEqual(spec["argv"][4], "etc-passwd")

    def test_claude_headless_argv_is_unchanged(self) -> None:
        spec = EN.headless_command("claude", "/bin/claude", "S1", "x", True, {})
        self.assertEqual(spec["argv"][:4], ["/bin/claude", "-p", "--session-id", "S1"])
        self.assertEqual(spec["stdin"], "x")

    def test_start_and_run_accept_pi_and_mcp_does_not(self) -> None:
        parser = cliparse.build_parser()
        start = parser.parse_args(["start", "--title", "t", "--engine", "pi"])
        run = parser.parse_args(["run", "--engine", "pi", "hello"])
        self.assertEqual(start.engine, "pi")
        self.assertEqual(run.engine, "pi")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["mcp", "install", "--engine", "pi"])
