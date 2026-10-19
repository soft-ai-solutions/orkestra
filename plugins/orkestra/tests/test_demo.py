"""`orkestra demo`: a zero-model local proof in an isolated temp project (EN and TR)."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from orkestra_core.messages import cli as mcli
from orkestra_core.messages import help as mhelp

DEMO_KEYS = ("cli.demo_intro", "cli.demo_part_run", "cli.demo_classified", "cli.demo_part_guards", "cli.demo_before_approval",
             "cli.demo_denied", "cli.demo_not_denied", "cli.demo_part_evidence", "cli.demo_part_watch", "cli.demo_more",
             "cli.demo_step_failed", "cli.demo_failed", "cli.demo_done", "cli.demo_kept", "cli.demo_next")


class CatalogTests(unittest.TestCase):
    def test_keys_in_both_catalogs_without_em_dash(self):
        for key in DEMO_KEYS:
            self.assertIn(key, mcli.EN)
            self.assertIn(key, mcli.TR)
            self.assertEqual(set(re.findall(r"\{(\w+)\}", mcli.EN[key])), set(re.findall(r"\{(\w+)\}", mcli.TR[key])), key)
            self.assertNotIn("—", mcli.EN[key] + mcli.TR[key])
        for key in ("help.cmd.demo", "help.opt.demo_keep"):
            self.assertIn(key, mhelp.EN)
            self.assertIn(key, mhelp.TR)
            self.assertNotIn("—", mhelp.EN[key] + mhelp.TR[key])


class DemoRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-demotest-")).resolve()
        self.real_home = self.tmp / "real-orkestra-home"          # stands for the user's own ORKESTRA_HOME
        self.real_user = self.tmp / "real-user-home"
        self.scratch = self.tmp / "scratch"                        # TMPDIR of the demo: what it leaves behind shows here
        for d in (self.real_user, self.scratch):
            d.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def demo(self, lang, *extra):
        env = dict(os.environ, ORKESTRA_LANG=lang, ORKESTRA_HOME=str(self.real_home), HOME=str(self.real_user),
                   TMPDIR=str(self.scratch), ORKESTRA_NO_DAEMON="1", PYTHONDONTWRITEBYTECODE="1")
        for k in ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "ORKESTRA_PROJECT"):
            env.pop(k, None)
        return subprocess.run([sys.executable, str(helpers.BIN), "demo", *extra], cwd=str(self.tmp), env=env,
                              capture_output=True, encoding="utf-8", errors="replace", timeout=180)

    def check_isolated(self):
        self.assertFalse(self.real_home.exists(), "the demo wrote into the user's ORKESTRA_HOME")
        self.assertEqual(list(self.real_user.iterdir()), [], "the demo wrote into the user's HOME")

    def test_english_run_proves_guards_and_dashboard_and_cleans_up(self):
        cp = self.demo("en")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout
        self.assertEqual(out.count("DENIED:"), 2, out)
        self.assertIn("[orkestra:git]", out)                        # the real git guard's own words
        self.assertIn("[orkestra:phase]", out)                      # the real phase guard's own words
        self.assertIn("evidence tests: exit 0", out)
        self.assertIn("orkestra watch --once", out)
        self.assertRegex(out, r"phase\s+intake")                    # the dashboard text of the run
        self.assertEqual(out.strip().splitlines()[-1], 'Next: orkestra run "<what you want>"')
        # the guards' own words, the evidence table and the dashboard `gates` row are the product's renderers (an
        # em dash is their empty-cell mark); everything else is the demo's narration
        own = [ln for ln in out.splitlines()
               if not ln.lstrip().startswith(("[orkestra:", "|", "gates"))]
        self.assertTrue(own)
        self.assertEqual([ln for ln in own if "\u2014" in ln], [], "the demo's own lines carry an em dash")
        self.check_isolated()
        self.assertEqual(list(self.scratch.glob("orkestra-demo-*")), [], "the temp project was not removed")

    def test_turkish_run(self):
        cp = self.demo("tr")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.count("REDDEDİLDİ:"), 2, cp.stdout)
        self.assertTrue(cp.stdout.strip().splitlines()[-1].startswith("Sonraki: orkestra run"), cp.stdout)
        self.check_isolated()
        self.assertEqual(list(self.scratch.glob("orkestra-demo-*")), [])

    def test_keep_retains_the_project(self):
        cp = self.demo("en", "--keep")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        kept = re.search(r"The demo project was kept: (\S+)", cp.stdout)
        self.assertIsNotNone(kept, cp.stdout)
        root = Path(kept.group(1))
        self.assertTrue((root / "greeter" / ".orkestra" / "features").is_dir())
        self.assertTrue(str(root).startswith(str(self.scratch)) or str(root.resolve()).startswith(str(self.scratch.resolve())))
        self.check_isolated()

    def test_registered_in_the_one_parser(self):
        from orkestra_core import cliparse
        self.assertEqual(cliparse.inspect(["demo", "--keep"])[0], "demo")
        cp = self.demo("en", "--help")
        self.assertIn("--keep", cp.stdout)


if __name__ == "__main__":
    unittest.main()
