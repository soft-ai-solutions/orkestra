import os
import unittest

import helpers  # noqa: F401
from orkestra_core import planparse as PP

PLAN = """---
approved_dependencies: []
---
### T001 — One
- Files: `a.py`
- Acceptance: it works
- Test: `pytest`
### T002 — Two
- Files: `b.py`
- Acceptance: Given a user, when they log in, then a token is issued
- Test: `pytest tests/test_b.py -k login`
"""
PLAN_TR = PLAN.replace("Given a user, when they log in, then", "Verilen kullanıcı için, ise")


class PlanNotes(unittest.TestCase):
    def _run(self, lang, plan):
        old = os.environ.get("ORKESTRA_LANG")
        os.environ["ORKESTRA_LANG"] = lang
        try:
            return PP.plan_notes(PP.parse_tasks(plan))
        finally:
            if old is None:
                os.environ.pop("ORKESTRA_LANG", None)
            else:
                os.environ["ORKESTRA_LANG"] = old

    def test_en(self):
        notes = self._run("en", PLAN)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all("T001" in n for n in notes))
        self.assertTrue(notes[0].startswith("NOTE"))

    def test_tr(self):
        notes = self._run("tr", PLAN_TR)
        self.assertEqual(len(notes), 2)
        self.assertTrue(notes[0].startswith("NOT"))

    def test_clean_plan_has_no_notes(self):
        self.assertEqual(self._run("en", PLAN.replace("it works", "Given x, then y").replace("`pytest`", "`pytest t.py`")), [])

    def test_check_plan_unchanged(self):
        self.assertEqual(PP.check_plan("# s", PLAN), PP.check_plan("# s", PLAN))


if __name__ == "__main__":
    unittest.main()
