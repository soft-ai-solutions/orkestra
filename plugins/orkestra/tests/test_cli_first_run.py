"""First-run CLI UX: no-run hint, `start "<request>"`, grouped help, typo hint, doctor ending, empty gates."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from orkestra_core import cli, cliparse
from orkestra_core.messages import cli as mcli, help as mhelp


def run_cli(*args, lang="en", cwd=None, env_extra=None):
    tmp = Path(tempfile.mkdtemp(prefix="orkestra-fr-"))
    env = dict(os.environ, ORKESTRA_LANG=lang, ORKESTRA_HOME=str(tmp / "home"), COPILOT_HOME=str(tmp / "cop"),
               CLAUDE_CONFIG_DIR=str(tmp / "claude"), ORKESTRA_NO_DAEMON="1", PYTHONDONTWRITEBYTECODE="1")
    for k in ("ORKESTRA_PROJECT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CURSOR_AGENT",
              "CURSOR_CONVERSATION_ID", "CURSOR_INVOKED_AS", "CURSOR_TRACE_ID"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(helpers.BIN), *args], cwd=cwd or str(tmp), env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=120)


class CatalogTests(unittest.TestCase):
    def test_new_keys_in_both_catalogs(self):
        for key in ("cli.no_open_run", "cli.start_need_request", "cli.doctor_next", "cli.doctor_plugin_missing",
                    "cli.setup_refused_agent", "cli.gate_confirm_none", "cli.init_no_gates", "help.group_start",
                    "help.group_advanced", "help.err.unknown_command", "help.err.unknown_command_hint"):
            self.assertIn(key, mcli.EN if key.startswith("cli.") else mhelp.EN)
            self.assertIn(key, mcli.TR if key.startswith("cli.") else mhelp.TR)
            for text in (mcli.EN.get(key) or mhelp.EN.get(key),
                         mcli.TR.get(key) or mhelp.TR.get(key)):
                self.assertNotIn("—", text)


class ParseTests(unittest.TestCase):
    def test_start_positional_and_title_optional(self):
        p = cliparse.build_parser(localize=False)
        a = cliparse.parse(p, ["start", "add dark mode"])
        self.assertEqual(a.text, "add dark mode")
        self.assertIsNone(a.title)
        a = cliparse.parse(p, ["start", "--title", "T", "--request", "R"])
        self.assertEqual((a.title, a.request, a.text), ("T", "R", None))

    def test_title_from_request(self):
        self.assertEqual(cli._title_from_request("add a toggle\nmore"), "add a toggle")
        long = cli._title_from_request("word " * 40)
        self.assertLessEqual(len(long), 61)


class HelpTests(unittest.TestCase):
    def test_groups_and_typo_en_tr(self):
        for lang, start, adv, did in (("en", "start here:", "advanced:", "Did you mean status?"),
                                      ("tr", "buradan başla:", "gelişmiş:", "status")):
            out = run_cli("--help", lang=lang).stdout
            self.assertIn(start, out)
            self.assertIn(adv, out)
            self.assertLess(out.index(start), out.index("doctor"))
            self.assertLess(out.index("doctor"), out.index(adv))
            self.assertLess(out.index(adv), out.index("integrity"))
            cp = run_cli("stauts", lang=lang)
            self.assertEqual(cp.returncode, 2)
            self.assertIn(did, cp.stderr)
            self.assertIn("stauts", cp.stderr)
        cp = run_cli("zzzzqq", lang="en")
        self.assertIn("Unknown command zzzzqq", cp.stderr)
        self.assertNotIn("Did you mean", cp.stderr)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp(prefix="orkestra-frp-"))
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)

    def test_no_run_hint(self):
        for lang, word in (("en", "No run in"), ("tr", "açık run yok")):
            cp = run_cli("status", lang=lang, cwd=str(self.proj))
            self.assertEqual(cp.returncode, 2)
            self.assertIn(word, cp.stderr)
            self.assertIn('orkestra run "', cp.stderr)

    def test_start_positional_derives_title(self):
        env = {"HOME": str(self.proj / "h")}
        cp = run_cli("-p", str(self.proj), "start", "add a dark mode toggle", cwd=str(self.proj), env_extra=env)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        self.assertIn("run started", cp.stdout)
        cp = run_cli("-p", str(self.proj), "start", cwd=str(self.proj), env_extra=env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("orkestra start", cp.stderr)

    def test_gate_confirm_none_and_init_example(self):
        for lang, word in (("en", "no gates to confirm"), ("tr", "Onaylanacak kapı yok")):
            cp = run_cli("-p", str(self.proj), "init", lang=lang, cwd=str(self.proj))
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn('"gates"', cp.stdout)
            self.assertIn('{"gates": [{"name": "test", "kind": "test", "cmd": "npm test", "timeout": 600}]}', cp.stdout)
            self.assertNotIn("{{", cp.stdout)
            cp = run_cli("-p", str(self.proj), "gate", "confirm", lang=lang, cwd=str(self.proj))
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn(word.lower(), cp.stdout.lower())

    def test_setup_refused_in_agent(self):
        cp = run_cli("setup", env_extra={"CLAUDECODE": "1"})
        self.assertEqual(cp.returncode, 2)
        self.assertIn("terminal", cp.stderr)
        self.assertIn("orkestra setup", cp.stderr)

    def test_doctor_hook_line_and_ending(self):
        cp = run_cli("doctor", cwd=str(self.proj))
        self.assertIn("deny: git push --force", cp.stdout)
        self.assertNotIn('{"hookSpecificOutput', cp.stdout)
        if cp.returncode == 0:
            self.assertIn('Ready. Next: cd <project> && orkestra run "', cp.stdout.strip().splitlines()[-1])


class PlanCheckNotes(unittest.TestCase):
    """`plan check` prints the plan_notes lines after a clean check (non-blocking)."""

    def test_notes_are_printed_in_both_languages(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        plan = ("### T001 - One\n- Files: `a.py`\n- Acceptance: it works\n- Test: `pytest`\n")
        for lang, head in (("en", "NOTE T001"), ("tr", "NOT T001")):
            tmp = Path(tempfile.mkdtemp(prefix="orkestra-pn-"))
            (tmp / "plan.md").write_text(plan, encoding="utf-8")
            old = os.environ.get("ORKESTRA_LANG")
            os.environ["ORKESTRA_LANG"] = lang
            try:
                buf = io.StringIO()
                with mock.patch.object(cli, "_resolve_project", return_value=tmp), \
                        mock.patch.object(cli, "_run_id", return_value="r"), \
                        mock.patch.object(cli.paths, "feature_dir", return_value=tmp), \
                        mock.patch.object(cli, "load_state", return_value={}), \
                        mock.patch.object(cli, "kind_of", return_value="feature"), \
                        mock.patch.object(cli.PP, "check_plan", return_value=[]), \
                        redirect_stdout(buf):
                    rc = cli.cmd_check(argparse.Namespace(project=None, run=None, cmd="plan"))
            finally:
                if old is None:
                    os.environ.pop("ORKESTRA_LANG", None)
                else:
                    os.environ["ORKESTRA_LANG"] = old
            self.assertEqual(rc, 0)
            self.assertIn(head, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
