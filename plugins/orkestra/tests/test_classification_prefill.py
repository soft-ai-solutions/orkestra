"""`orkestra start` suggests the Type / Size / Risk of `## Classification`; intake only verifies it."""
import re
import unittest

import helpers
from orkestra_core import intent
from orkestra_core import runs as R


class Prefill(unittest.TestCase):
    def test_types(self):
        cases = {
            "add a dark mode toggle": "feature",
            "fix the crash when the cart is empty": "bugfix",
            "refactor the parser into two modules": "refactor",
            "make the search faster": "perf",
            "update the README with install steps": "docs",
            "what does the watchdog do?": "question",
            "explain how the tempo hint works": "advice",
            "change the button colour on the login screen": "ui",
            "giriş hatasını düzelt": "bugfix",
        }
        for request, kind in cases.items():
            with self.subTest(request=request):
                size = intent.infer_size(request)
                self.assertEqual(R.classification_prefill(request, size)["type"], kind)

    def test_docs_win_over_bugfix_and_light_theme_is_not_security(self):
        cases = {
            "fix typo in README": "docs",
            "fix the docs link": "docs",
            "README'deki yazım hatasını düzelt": "docs",
            "açık tema ekle": "ui",
            "fix the XSS hole in the comment form": "security",
            "güvenlik açığını kapat": "security",
        }
        for request, kind in cases.items():
            with self.subTest(request=request):
                self.assertEqual(R.classification_prefill(request, "S")["type"], kind)

    def test_risk_follows_the_size_floor(self):
        risky = R.classification_prefill("add stripe payment checkout", intent.infer_size("add stripe payment checkout"))
        self.assertEqual(risky["risk"], "high")
        self.assertIn(risky["size"], ("M", "L"))
        self.assertEqual(R.classification_prefill("fix the typo in the footer", "XS")["risk"], "low")
        self.assertEqual(R.classification_prefill("add a dark mode toggle", "M")["risk"], "medium")

    def test_size_is_the_runs_size(self):
        self.assertEqual(R.classification_prefill("anything", "L")["size"], "L")


class StartPrints(helpers.Sandbox):
    def test_start_prints_the_suggestion_and_leaves_the_classification_to_intake(self):
        self.make_project("demo")
        for lang in ("en", "tr"):
            with self.subTest(lang=lang):
                out = self.cli("start", "-p", "demo", "--request", "add stripe payment checkout",
                               env={"ORKESTRA_LANG": lang}).stdout
                line = [ln for ln in out.splitlines() if ln.startswith("suggested classification")][0]
                self.assertRegex(line, r"Type: feature · Size: [ML] · Risk: high")
                request = (self.ws / "demo" / ".orkestra" / "current" / "request.md").read_text(encoding="utf-8")
                # the block is NOT written: its heading is what tells the tempo hint that the model classified
                self.assertNotRegex(request, r"(?m)^## (Classification|Sınıflandırma)")
                nxt = self.cli("next", "-p", "demo", env={"ORKESTRA_LANG": "en"}).stdout
                self.assertIn("classify the request", nxt)
                self.cli("abort", "test over", "-p", "demo")


if __name__ == "__main__":
    unittest.main()
