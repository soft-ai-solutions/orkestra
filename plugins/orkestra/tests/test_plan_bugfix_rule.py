"""N3: a bugfix or perf request needs a repro-first task and T001 tied to a requirement."""
import os
import unittest

import helpers
from orkestra_core import paths
from orkestra_core import planparse as PP
from orkestra_core import runs as R
from orkestra_core.runs import start_run
from orkestra_core.state import load_state
from orkestra_core.util import write_json_atomic

SPEC = "# S\n\n## Requirements\n- **FR-001**: crash on empty input\n- **FR-002**: other\n\n## Success criteria\n- **SC-001**: y\n"


def _plan(t1_acc="Given empty When run Then no crash (FR-001)", tdd="required"):
    return ("---\napproved_dependencies: []\n---\n# Plan\n\n## Tasks\n\n### T001 - fix\n- Files: `a.py`\n"
            f"- Acceptance: {t1_acc}\n- Test: `python3 -m unittest -q`\n- TDD: {tdd}\n\n"
            "### T002 - more\n- Files: `b.py`\n- Acceptance: Given a When b Then c (FR-002)\n"
            "- Test: `python3 -m unittest -q`\n- TDD: n/a (docs only)\n\n"
            "## Test strategy\n| FR-001 | t | unit |\n| FR-002 | t | unit |\n| SC-001 | t | unit |\n")


class BugfixRuleTest(helpers.Sandbox):
    def _set_lang(self, lang):
        # restore the process env afterwards so other modules keep their default language
        self.addCleanup(os.environ.pop, "ORKESTRA_LANG", None) if "ORKESTRA_LANG" not in os.environ else \
            self.addCleanup(os.environ.__setitem__, "ORKESTRA_LANG", os.environ["ORKESTRA_LANG"])
        os.environ["ORKESTRA_LANG"] = lang

    def _problems(self, kind, plan, heading="Classification", key="Type", lang="en"):
        self._set_lang(lang)
        proj = self.make_project("bf" + kind + lang)
        rid = start_run(proj, "T", "fix a crash", "S", "delivery")["run_id"]
        req = paths.feature_dir(proj, rid) / "request.md"
        if kind:
            req.write_text(req.read_text() + f"\n## {heading}\n- {key}: {kind}\n- Size: S\n")
        (paths.feature_dir(proj, rid)).joinpath("plan.md").write_text(plan)
        return [p for p in PP.check_plan(SPEC, plan, proj, run_id=rid) if "bugfix" in p or "perf" in p
                or "hata" in p.lower() or "TDD" in p]

    def test_valid_bugfix_plan_passes(self):
        self.assertEqual(self._problems("bugfix", _plan()), [])

    def test_no_tdd_task_is_an_error_with_hint(self):
        plan = _plan(tdd="n/a (docs only)")
        got = self._problems("bugfix", plan)
        self.assertEqual(len(got), 1)
        self.assertIn("repro-first", got[0])
        self.assertIn("`- TDD: required`", got[0])

    def test_t001_must_reference_fr(self):
        got = self._problems("perf", _plan(t1_acc="Given empty When run Then fast"))
        self.assertTrue(any("T001" in p and "FR-001" in p for p in got), got)

    def test_turkish_catalog_and_heading(self):
        got = self._problems("bugfix", _plan(tdd="n/a (docs only)"), heading="Sınıflandırma", key="Tür", lang="tr")
        self.assertTrue(any("önce yeniden üretim" in p for p in got), got)

    def test_other_types_and_missing_classification_unaffected(self):
        bad = _plan(t1_acc="Given empty When run Then fast", tdd="n/a (docs only)")
        self.assertEqual(self._problems("feature", bad), [])
        self.assertEqual(self._problems("", bad), [])

    def test_fr_match_uses_a_word_boundary(self):
        spec = SPEC
        tasks = PP.parse_tasks(_plan(t1_acc="Given empty When run Then no crash (FR-0010)"))
        got = PP.bugfix_plan_problems(tasks, spec, "bugfix")
        self.assertTrue(any("T001" in p and "FR-001" in p for p in got), got)
        ok = PP.parse_tasks(_plan(t1_acc="Given empty When run Then no crash (FR-001)"))
        self.assertEqual(PP.bugfix_plan_problems(ok, spec, "bugfix"), [])

    def test_rule_applies_on_approve_but_not_to_an_already_approved_run(self):
        self._set_lang("en")
        proj = self.make_project("bfapproved")
        rid = start_run(proj, "T", "fix a crash", "S", "delivery")["run_id"]
        fdir = paths.feature_dir(proj, rid)
        req = fdir / "request.md"
        req.write_text(req.read_text() + "\n## Classification\n- Type: bugfix\n- Size: S\n")
        plan = _plan(tdd="n/a (docs only)")
        (fdir / "spec.md").write_text(SPEC)
        (fdir / "plan.md").write_text(plan)
        st = load_state(proj, rid)
        self.assertTrue(any("repro-first" in p for p in R.preconditions(proj, st, "approve")))
        write_json_atomic(paths.run_dir(proj, rid) / "approval.json",
                          {"decision": "approve", "hash": R.spec_plan_hash(proj, rid)})
        self.assertFalse(any("repro-first" in p for p in R.preconditions(proj, st, "implement")))


if __name__ == "__main__":
    unittest.main()
