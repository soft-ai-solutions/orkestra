"""Prompt contracts: verdict tokens, reply caps, read-only agents, XS/S carve-out wording, byte budgets."""
from __future__ import annotations

import os
import re
import unittest

import helpers
from orkestra_core import intent

AGENTS = helpers.PLUGIN / "agents"
SKILL_DIR = helpers.PLUGIN / "skills" / "run"
REFS = SKILL_DIR / "references"
READ_ONLY = ("reviewer", "plan-critic", "verifier", "adversary", "researcher", "scout")
VERDICTS = {
    "reviewer": ("VERDICT: PASS", "VERDICT: FAIL"),
    "plan-critic": ("VERDICT: PASS", "VERDICT: PASS_WITH_NOTES", "VERDICT: FAIL"),
    "verifier": ("VERDICT: PASS", "VERDICT: FAIL"),
    "adversary": ("VERDICT: PASS", "VERDICT: FAIL"),
}


def read(path) -> str:
    return path.read_text(encoding="utf-8")


class PromptContract(unittest.TestCase):
    def setUp(self):
        self._lang = os.environ.get("ORKESTRA_LANG")

    def tearDown(self):
        if self._lang is None:
            os.environ.pop("ORKESTRA_LANG", None)
        else:
            os.environ["ORKESTRA_LANG"] = self._lang

    def each_lang(self, fn):
        for lang in ("en", "tr"):
            os.environ["ORKESTRA_LANG"] = lang
            with self.subTest(lang=lang):
                fn()

    def test_verdict_tokens(self):
        def check():
            for name, tokens in VERDICTS.items():
                text = read(AGENTS / f"{name}.md")
                for token in tokens:
                    self.assertIn(token, text, f"{name}: {token}")
        self.each_lang(check)

    def test_every_agent_caps_its_reply(self):
        def check():
            for path in sorted(AGENTS.glob("*.md")):
                self.assertRegex(read(path), r"(?i)(reply|answer)[^\n]*(≤|at most) ?\d+ lines|Reply \(≤ \d+ lines\)", path.name)
        self.each_lang(check)

    def test_read_only_agents_cannot_edit(self):
        def check():
            for name in READ_ONLY:
                head = read(AGENTS / f"{name}.md").split("---", 2)[1]
                disallowed = re.search(r"^disallowedTools:(.*)$", head, re.M)
                self.assertTrue(disallowed, name)
                for tool in ("Edit", "MultiEdit", "NotebookEdit"):
                    self.assertIn(tool, disallowed.group(1), f"{name}: {tool}")
        self.each_lang(check)

    def test_xs_s_carve_out_matches_intent(self):
        def check():
            for size in ("XS", "S", "M", "L"):
                self.assertEqual(intent.skips_plan_critic(size), size in ("XS", "S"))
                self.assertEqual(intent.skips_adversary(size), size in ("XS", "S"))
            self.assertIn("XS/S skip the critic", read(REFS / "phase-plan.md"))
            self.assertIn("XS/S: reviewer only", read(REFS / "phase-review.md"))
            self.assertIn("skip plan-critic", read(SKILL_DIR / "SKILL.md"))
            self.assertIn("skip adversary", read(SKILL_DIR / "SKILL.md"))
        self.each_lang(check)

    def test_byte_budgets(self):
        def check():
            self.assertLessEqual(len(read(SKILL_DIR / "SKILL.md").encode()), 12 * 1024)
            for path in sorted(REFS.glob("phase-*.md")):
                self.assertLessEqual(len(path.read_bytes()), 4 * 1024, path.name)
            for path in sorted(REFS.glob("stack-*.md")):
                self.assertLessEqual(len(read(path).splitlines()), 41, path.name)
        self.each_lang(check)

    def test_new_contract_lines_are_present(self):
        def check():
            self.assertIn("Self-check", read(AGENTS / "implementer.md"))
            self.assertIn("drop any finding you cannot point to", read(AGENTS / "reviewer.md"))
            self.assertIn("T001 the test that reproduces the bug", read(AGENTS / "architect.md"))
            for name in ("stack-python", "stack-go", "stack-jvm"):
                self.assertIn(name, read(AGENTS / "reviewer.md"))
                self.assertIn(name, read(AGENTS / "adversary.md"))
        self.each_lang(check)


if __name__ == "__main__":
    unittest.main()
