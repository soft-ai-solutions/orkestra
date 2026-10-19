"""N2: plan-time size escalation. XS/S rises to M when the plan is risky or large; nothing is ever lowered."""
import os
import unittest

import helpers
from orkestra_core import paths
from orkestra_core.runs import escalate_size_at_plan, phase_next, start_run
from orkestra_core.state import OrkestraError, load_state, save_state
from orkestra_core import planparse as PP

SPEC = "# S\n\n## Requirements\n- **FR-001**: x\n\n## Success criteria\n- **SC-001**: y\n"


def _plan(files_per_task):
    out = "---\napproved_dependencies: []\n---\n# Plan\n\n## Tasks\n\n"
    for i, files in enumerate(files_per_task, 1):
        out += (f"### T{i:03d} - t\n- Files: {', '.join('`%s`' % f for f in files)}\n"
                "- Acceptance: Given a When b Then c (FR-001)\n- Test: `python3 -m unittest -q`\n- TDD: required\n\n")
    return out + "## Test strategy\n| FR-001 | t | unit |\n| SC-001 | t | unit |\n"


class EscalationTest(helpers.Sandbox):
    def _set_lang(self, lang):
        # restore the process env afterwards so other modules keep their default language
        self.addCleanup(os.environ.pop, "ORKESTRA_LANG", None) if "ORKESTRA_LANG" not in os.environ else \
            self.addCleanup(os.environ.__setitem__, "ORKESTRA_LANG", os.environ["ORKESTRA_LANG"])
        os.environ["ORKESTRA_LANG"] = lang

    def _run(self, size, plan, lang="en"):
        self._set_lang(lang)
        self._n = getattr(self, "_n", 0) + 1
        proj = self.make_project(f"esc-{size.lower()}{lang}{self._n}")
        rid = start_run(proj, "T", "add a thing", size, "delivery", approval_mode="autopilot")["run_id"]
        fdir = paths.feature_dir(proj, rid)
        (fdir / "spec.md").write_text(SPEC)
        (fdir / "plan.md").write_text(plan)
        st = load_state(proj, rid)
        st["phase"] = "plan"
        save_state(proj, st)
        return proj, rid

    def _ledger(self, proj, rid):
        return (paths.feature_dir(proj, rid) / "ledger.md").read_text()

    def test_risky_path_raises_s_to_m_with_ledger_en_and_tr(self):
        for lang, word in (("en", "size raised S"), ("tr", "boyut plan aşamasında yükseltildi S")):
            proj, rid = self._run("S", _plan([["src/auth/login.py"]]), lang)
            self.assertTrue(escalate_size_at_plan(proj, rid))
            self.assertEqual(load_state(proj, rid)["size"], "M")
            self.assertIn(word, self._ledger(proj, rid))
            self.assertFalse(escalate_size_at_plan(proj, rid))          # idempotent
            self.assertEqual(self._ledger(proj, rid).count(word), 1)

    def test_data_path_raises_xs(self):
        proj, rid = self._run("XS", _plan([["db/schema.sql"]]))
        self.assertTrue(escalate_size_at_plan(proj, rid))
        self.assertEqual(load_state(proj, rid)["size"], "M")

    def test_more_than_three_tasks_or_eight_files(self):
        proj, rid = self._run("S", _plan([["a.py"], ["b.py"], ["c.py"], ["d.py"]]))
        self.assertTrue(escalate_size_at_plan(proj, rid))
        proj, rid = self._run("S", _plan([["f%d.py" % i for i in range(9)]]))
        self.assertTrue(escalate_size_at_plan(proj, rid))
        self.assertIn("9 files", self._ledger(proj, rid))

    def test_small_plan_stays_and_boundaries_do_not_trigger(self):
        proj, rid = self._run("S", _plan([["a.py"], ["b.py"], ["c.py"]]))
        self.assertFalse(escalate_size_at_plan(proj, rid))
        proj, rid = self._run("S", _plan([["f%d.py" % i for i in range(8)]]))
        self.assertFalse(escalate_size_at_plan(proj, rid))
        self.assertEqual(load_state(proj, rid)["size"], "S")

    def test_never_touches_m_l_and_not_outside_plan_phase(self):
        for size in ("M", "L"):
            proj, rid = self._run(size, _plan([["src/auth/login.py"]]))
            self.assertFalse(escalate_size_at_plan(proj, rid))
            self.assertEqual(load_state(proj, rid)["size"], size)
        proj, rid = self._run("S", _plan([["src/auth/login.py"]]))
        st = load_state(proj, rid)
        st["phase"] = "clarify"
        save_state(proj, st)
        self.assertFalse(escalate_size_at_plan(proj, rid))

    def test_phase_next_escalates_and_then_requires_critic(self):
        proj, rid = self._run("S", _plan([["src/auth/login.py"]]))
        with self.assertRaises(OrkestraError) as caught:
            phase_next(proj, rid)
        self.assertIn("plan-critic", str(caught.exception))
        self.assertEqual(load_state(proj, rid)["size"], "M")

    def test_reason_helper(self):
        self._set_lang("en")
        self.assertEqual(PP.size_escalation_reason(PP.parse_tasks(_plan([["README.md"]]))), "")


if __name__ == "__main__":
    unittest.main()
