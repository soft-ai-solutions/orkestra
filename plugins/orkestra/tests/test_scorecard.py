"""Scorecard: first-pass rate, reopens, red mix, tokens and cost from synthetic runs; the evidence lines; XS/S verify."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path + env)
from orkestra_core import evidence as E
from orkestra_core import i18n
from orkestra_core import scorecard as SC
from orkestra_core import tempo as T

USAGE = {"input": 1000, "output": 500, "cache_write": 1000, "cache_read": 8000, "total": 10500,
         "phases": {"implement": 9000, "verify": 1500}}


def run_state(**over):
    st = {"run_id": "r1", "phase": "verify", "status": "active", "size": "M", "created": "2026-10-01T10:00:00",
          "history": [{"to": "implement", "at": "2026-10-01T10:05:00"},
                      {"to": "implement", "at": "2026-10-01T10:30:00", "note": "back: gate lint regressed"}],
          "tasks": {
              "T001": {"status": "done", "red": {"at": "2026-10-01T10:08:00", "reason": "assertion"}, "notes": []},
              "T002": {"status": "done", "red": {"at": "2026-10-01T10:20:00", "reason": "missing_symbol"},
                       "notes": ["reopened: review round 1"]},
              "T003": {"status": "deferred", "notes": []},
              "T004": {"status": "blocked", "notes": []}},
          "usage": dict(USAGE)}
    st.update(over)
    return st


class ScoreMath(unittest.TestCase):
    def test_card_from_synthetic_run(self):
        card = SC.compute(Path("/nonexistent"), run_state())
        self.assertEqual((card["first_pass"], card["done"]), (1, 2))
        self.assertAlmostEqual(card["first_pass_rate"], 0.5)
        self.assertEqual(card["reopens"], 1)
        self.assertAlmostEqual(card["reopens_per_task"], 0.25)
        self.assertEqual(card["red_reasons"], {"assertion": 1, "missing_symbol": 1})
        self.assertEqual(card["regressions_caught"], 1)
        self.assertEqual(card["time_to_first_red"], 8 * 60)
        self.assertEqual((card["deferred"], card["blocked"]), (1, 1))
        self.assertEqual(card["phase_tokens"], {"implement": 9000, "verify": 1500})
        self.assertAlmostEqual(card["cache_read_share"], 0.8)
        self.assertIsNone(card["cost_usd"])           # no price configured: never a made-up number

    def test_empty_run_has_unknowns_not_errors(self):
        card = SC.compute(Path("/nonexistent"), {"run_id": "r", "tasks": {}, "usage": None}, with_usage=False)
        self.assertIsNone(card["first_pass_rate"])
        self.assertIsNone(card["time_to_first_red"])
        self.assertEqual(len(SC.lines(card, lambda k, **kw: k.format(**kw) if kw else k)), 2)

    def test_cost_needs_prices(self):
        prices = {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.3}
        self.assertAlmostEqual(SC.cost_usd(USAGE, prices), 0.003 + 0.0075 + 0.00375 + 0.0024)
        self.assertIsNone(SC.cost_usd(USAGE, {}))
        self.assertIsNone(SC.cost_usd(None, prices))

    def test_warning_is_off_by_default_and_fires_over_the_limit(self):
        card = {"cost_usd": 5.0}
        with mock.patch.object(SC, "_cfg", return_value={}):
            self.assertIsNone(SC.warning(card, i18n.t))
        with mock.patch.object(SC, "_cfg", return_value={"cost_warn_usd": 2}):
            self.assertIn("5.00", SC.warning(card, i18n.t))
            self.assertIsNone(SC.warning({"cost_usd": 1.0}, i18n.t))
            self.assertIsNone(SC.warning({"cost_usd": None}, i18n.t))


class ScoreText(unittest.TestCase):
    def render(self, lang):
        card = SC.compute(Path("/nonexistent"), run_state())
        card["cost_usd"] = 1.5
        old = os.environ.get("ORKESTRA_LANG")
        os.environ["ORKESTRA_LANG"] = lang
        try:
            return SC.lines(card, i18n.t)
        finally:
            os.environ["ORKESTRA_LANG"] = old

    def test_four_lines_in_both_languages(self):
        en, tr = self.render("en"), self.render("tr")
        self.assertEqual((len(en), len(tr)), (4, 4))
        self.assertIn("first-pass review 50%", en[0])
        self.assertIn("red reasons: assertion 1, missing_symbol 1", en[1])
        self.assertIn("cache read 80% of input", en[2])
        self.assertIn("$1.50", en[3])
        self.assertIn("Karne", tr[0])
        self.assertIn("kırmızı nedenleri", tr[1])
        for line in en + tr:
            self.assertNotIn("—", line)


class Evidence(unittest.TestCase):
    def test_scorecard_lines_do_not_break_block_currency(self):
        block = "\n".join(["a", "Scorecard x " + SC.MARK, "tail"])
        self.assertEqual(E._strip_scorecard(block), "a\ntail")
        self.assertEqual(SC.MARK, E.SC_MARK)


class XsSVerify(unittest.TestCase):
    def verify_action(self, size):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ORKESTRA_HOME": tmp + "/home"}):
            proj = Path(tmp) / "proj"
            st = run_state(size=size, tasks={}, phase="verify")
            rdir = proj / ".orkestra" / "runs" / "r1"
            rdir.mkdir(parents=True)
            (rdir / "gates-verify.json").write_text(json.dumps({"results": [], "comparison": {"ok": True}}))
            return T.next_action(proj, st, deep=False)

    def test_xs_and_s_write_verification_themselves(self):
        for size in ("XS", "S"):
            act = self.verify_action(size)
            self.assertEqual(act["action"], "work", size)
            self.assertNotIn("dispatch orkestra:verifier", act["text"])
            self.assertIn("orkestra trace check", act["text"])

    def test_m_still_dispatches_the_verifier(self):
        self.assertEqual(self.verify_action("M")["action"], "dispatch")


if __name__ == "__main__":
    unittest.main()
