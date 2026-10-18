"""FR-001 / SC-001: M/L approval is fresh only when plan-critic.md is at least as new as spec.md and plan.md."""
import os
import unittest

import helpers
from orkestra_core import paths
from orkestra_core.runs import phase_back, phase_next, preconditions, spec_plan_hash, start_run
from orkestra_core.state import OrkestraError, load_state, save_state

NS = 1_700_000_000_000_000_000
FRESH_PHRASE = "older than spec.md or plan.md"

SPEC = """---
feature: x
status: draft
---
# Subtract

## Purpose
The user can subtract two numbers.

## Requirements
- **FR-001**: `sub(a, b)` returns a-b.

## Success criteria
- **SC-001**: `sub(5, 3) == 2` passes.

## Boundaries
- Always: existing add behavior stays.
- Never: add a dependency.
"""

PLAN = """---
feature: x
risk: low
approved_dependencies: []
---
# Plan: Subtract

## Tasks

### T001 - sub function
- Files: `calc.py`, `tests/test_sub.py` (new)
- Acceptance: Given 5 and 3 When sub Then 2 (FR-001)
- Test: `python3 -m unittest tests.test_sub -q`
- TDD: required

## Test strategy
| FR-001 | test_sub | unit |
| SC-001 | test_sub | unit |
"""


def _utime(path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


class PlanFreshnessTest(helpers.Sandbox):
    def _open(self, name: str, size: str):
        proj = self.make_project(name)
        st = start_run(proj, "Subtract", "add a sub function", size, "delivery", approval_mode="autopilot")
        return proj, st["run_id"]

    def _fdir(self, proj, rid):
        return paths.feature_dir(proj, rid)

    def _set_state(self, proj, rid, phase: str, **fields) -> None:
        st = load_state(proj, rid)
        st["phase"] = phase
        for key, value in fields.items():
            if value is None:
                st.pop(key, None)
            else:
                st[key] = value
        save_state(proj, st)

    def _lay(self, proj, rid, *, spec_ns: int, plan_ns: int, verdict: str = None, critic_ns: int = None) -> None:
        fdir = self._fdir(proj, rid)
        (fdir / "spec.md").write_text(SPEC)
        (fdir / "plan.md").write_text(PLAN)
        critic = fdir / "reviews" / "plan-critic.md"
        if verdict is None:
            if critic.exists():
                critic.unlink()
        else:
            critic.write_text(f"VERDICT: {verdict}\n")
            if critic_ns is not None:
                _utime(critic, critic_ns)
        _utime(fdir / "spec.md", spec_ns)
        _utime(fdir / "plan.md", plan_ns)

    def _at_plan(self, proj, rid, size: str) -> None:
        self._set_state(proj, rid, "plan", size=size, plan_review_hash=None)

    def _assert_refused(self, proj, rid) -> None:
        st = load_state(proj, rid)
        problems = preconditions(proj, st, "approve")
        self.assertTrue(any(FRESH_PHRASE in p for p in problems), problems)
        with self.assertRaises(OrkestraError) as caught:
            phase_next(proj, rid)
        self.assertIn(FRESH_PHRASE, str(caught.exception))
        after = load_state(proj, rid)
        self.assertEqual(after["phase"], "plan")
        self.assertNotEqual(after.get("plan_review_hash"), spec_plan_hash(proj, rid))

    def test_ml_missing_or_older_critic_refuses_and_does_not_stamp(self):
        proj, rid = self._open("stale", "M")
        cases = (
            ("M", None, None, NS, NS),
            ("L", None, None, NS, NS),
            ("M", "PASS", NS, NS + 2_000_000_000, NS),
            ("L", "PASS", NS, NS, NS + 2_000_000_000),
            ("M", "PASS_WITH_NOTES", NS, NS + 1_000_000_000, NS + 2_000_000_000),
        )
        for size, verdict, critic_ns, spec_ns, plan_ns in cases:
            with self.subTest(size=size, verdict=verdict, critic_ns=critic_ns):
                self._at_plan(proj, rid, size)
                self._lay(proj, rid, spec_ns=spec_ns, plan_ns=plan_ns, verdict=verdict, critic_ns=critic_ns)
                self._assert_refused(proj, rid)

    def test_fresh_critic_advances_and_stamps_current_bytes(self):
        cases = (
            ("equal-m", "M", "PASS", NS, NS, NS),
            ("newer-l", "L", "PASS_WITH_NOTES", NS + 5_000_000_000, NS, NS + 1_000_000_000),
        )
        for name, size, verdict, critic_ns, spec_ns, plan_ns in cases:
            with self.subTest(name=name):
                proj, rid = self._open(name, size)
                self._at_plan(proj, rid, size)
                self._lay(proj, rid, spec_ns=spec_ns, plan_ns=plan_ns, verdict=verdict, critic_ns=critic_ns)
                problems = preconditions(proj, load_state(proj, rid), "approve")
                self.assertFalse(any(FRESH_PHRASE in p for p in problems), problems)
                cur, nxt = phase_next(proj, rid)
                self.assertEqual((cur, nxt), ("plan", "approve"))
                st = load_state(proj, rid)
                self.assertEqual(st["plan_review_hash"], spec_plan_hash(proj, rid))

    def test_xs_and_s_stamp_current_bytes(self):
        proj, rid = self._open("small-none", "XS")
        self._at_plan(proj, rid, "XS")
        self._lay(proj, rid, spec_ns=NS, plan_ns=NS)
        self.assertFalse(any(FRESH_PHRASE in p for p in preconditions(proj, load_state(proj, rid), "approve")))
        phase_next(proj, rid)
        self.assertEqual(load_state(proj, rid)["plan_review_hash"], spec_plan_hash(proj, rid))

        proj, rid = self._open("small-old", "S")
        self._at_plan(proj, rid, "S")
        self._lay(proj, rid, spec_ns=NS + 3_000_000_000, plan_ns=NS + 4_000_000_000,
                  verdict="PASS", critic_ns=NS)
        self.assertFalse(any(FRESH_PHRASE in p for p in preconditions(proj, load_state(proj, rid), "approve")))
        phase_next(proj, rid)
        self.assertEqual(load_state(proj, rid)["plan_review_hash"], spec_plan_hash(proj, rid))

    def test_phase_back_before_approve_clears_hash(self):
        proj, rid = self._open("back-clear", "M")
        hops = (("approve", "plan"), ("approve", "clarify"), ("implement", "plan"))
        for phase, target in hops:
            with self.subTest(phase=phase, target=target):
                extra = {"approved": {"deps": []}} if phase == "implement" else {}
                self._set_state(proj, rid, phase, plan_review_hash="not-current", **extra)
                phase_back(proj, rid, target, "revise the plan")
                st = load_state(proj, rid)
                self.assertEqual(st["phase"], target)
                self.assertIsNone(st.get("plan_review_hash"))
                if phase == "implement":
                    self.assertIsNone(st.get("approved"))

    def test_phase_back_approve_or_later_keeps_hash(self):
        proj, rid = self._open("back-keep", "L")
        hops = (("verify", "implement"), ("review", "implement"), ("deliver", "review"))
        for phase, target in hops:
            with self.subTest(phase=phase, target=target):
                self._set_state(proj, rid, phase, plan_review_hash="still-here")
                phase_back(proj, rid, target, "recheck")
                st = load_state(proj, rid)
                self.assertEqual(st["phase"], target)
                self.assertEqual(st.get("plan_review_hash"), "still-here")

    def test_in_approve_edit_refuses_with_changed_after_plan_review(self):
        proj, rid = self._open("edited", "M")
        self._at_plan(proj, rid, "M")
        self._lay(proj, rid, spec_ns=NS, plan_ns=NS, verdict="PASS", critic_ns=NS)
        cur, nxt = phase_next(proj, rid)
        self.assertEqual((cur, nxt), ("plan", "approve"))
        old = load_state(proj, rid)["plan_review_hash"]
        plan = self._fdir(proj, rid) / "plan.md"
        plan.write_text(plan.read_text() + "\nnote: edited after review\n")
        st = load_state(proj, rid)
        self.assertEqual(st["phase"], "approve")
        problems = preconditions(proj, st, "approve")
        self.assertFalse(any(FRESH_PHRASE in p for p in problems), problems)
        cp = self.cli("phase", "next", "-p", "edited", check=False)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("changed after plan review", cp.stderr)
        after = load_state(proj, rid)
        self.assertEqual(after["phase"], "approve")
        self.assertEqual(after.get("plan_review_hash"), old)
        self.assertNotEqual(old, spec_plan_hash(proj, rid))
        self.assertFalse((paths.run_dir(proj, rid) / "approval.json").exists())


if __name__ == "__main__":
    unittest.main()
