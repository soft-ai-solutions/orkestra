"""The visible allow-all notice: shown by `copilot status` and the `run --engine copilot` start lines while it is on."""
import json
import os
import unittest

import helpers
from test_copilot_profile import fake_cli


class TestAllowAllNotice(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake_cli(bindir, "  --context <c>\n  --allow-all")
        self.env = {"COPILOT_HOME": str(self.tmp / "copilot-home"), "HOME": str(self.tmp / "h"),
                    "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
        self.make_project("demo")
        self.cli("init", "-p", "demo", env=self.env)
        self.assertEqual(self.cli("copilot", "install", env=self.env).returncode, 0)

    def go(self, lang, *cmd):
        return self.cli(*cmd, env=dict(self.env, ORKESTRA_LANG=lang), check=False)

    def test_notice_in_both_languages_when_on(self):
        for lang, word in (("en", "allow-all is ON"), ("tr", "allow-all AÇIK")):
            st = self.go(lang, "copilot", "status")
            self.assertIn(word, st.stdout)
            self.assertIn("--no-allow-all", st.stdout)
            run = self.go(lang, "run", "--engine", "copilot", "-p", "demo", "add a feature")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn(word, run.stdout)
            self.go("en", "abort", "-p", "demo", "next language")

    def test_no_notice_when_off(self):
        run = self.go("en", "run", "--engine", "copilot", "-p", "demo", "--no-allow-all", "add a feature")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("allow-all is ON", run.stdout)
        cfgp = self.home / "config.json"
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"engine": {"copilot": {"allow_all": False}}}))
        self.assertNotIn("allow-all is ON", self.go("en", "copilot", "status").stdout)


if __name__ == "__main__":
    unittest.main()
