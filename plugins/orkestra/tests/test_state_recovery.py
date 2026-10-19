"""state.json.prev, is_damaged / restore_prev, the DAMAGED watchdog row and non-Latin run ids."""
import os
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from orkestra_core import state as ST
from orkestra_core.util import slugify


class StateRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name)
        self.st = ST.new_state(self.proj, "001-x", "x", "M", "full")
        ST.save_state(self.proj, self.st)
        self.rdir = ST.state_path(self.proj, "001-x").parent

    def test_prev_and_restore(self):
        self.assertFalse((self.rdir / "state.json.prev").exists())
        with ST.StateTxn(self.proj, "001-x") as st:
            st["phase"] = "plan"
        self.assertTrue((self.rdir / "state.json.prev").exists())
        self.assertFalse(ST.is_damaged(self.rdir))
        (self.rdir / "state.json").write_text("{broken", encoding="utf-8")
        self.assertTrue(ST.is_damaged(self.rdir))
        self.assertTrue(ST.restore_prev(self.rdir))
        self.assertFalse(ST.is_damaged(self.rdir))

    def test_damaged_state_never_overwrites_prev(self):
        ST.save_state(self.proj, self.st)
        prev = (self.rdir / "state.json.prev").read_text(encoding="utf-8")
        (self.rdir / "state.json").write_text("{broken", encoding="utf-8")
        ST.save_state(self.proj, self.st)
        self.assertEqual((self.rdir / "state.json.prev").read_text(encoding="utf-8"), prev)

    def test_restore_without_prev(self):
        self.assertFalse(ST.restore_prev(self.rdir))

    def test_slugify_non_latin_is_distinct(self):
        a, b = slugify("日本語のタイトル"), slugify("!!!")
        self.assertTrue(a.startswith("feature-") and b.startswith("feature-"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, slugify("日本語のタイトル"))
        self.assertEqual(slugify("Merhaba Dünya"), "merhaba-dunya")


class DoctorRepair(unittest.TestCase):
    """`orkestra doctor --repair` restores state.json.prev or moves the run aside; it never deletes."""

    def _project(self, with_prev):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        (proj / ".orkestra").mkdir(parents=True)
        st = ST.new_state(proj, "001-x", "x", "M", "full")
        ST.save_state(proj, st)
        if with_prev:
            ST.save_state(proj, st)
        rdir = ST.state_path(proj, "001-x").parent
        (rdir / "state.json").write_text("{broken", encoding="utf-8")
        return tmp, proj, rdir

    def _doctor(self, tmp, proj, lang, *args, cwd=None):
        import subprocess
        import sys
        env = dict(os.environ, ORKESTRA_LANG=lang, ORKESTRA_HOME=str(Path(tmp.name) / "h"),
                   COPILOT_HOME=str(Path(tmp.name) / "c"), ORKESTRA_NO_DAEMON="1")
        env.pop("ORKESTRA_PROJECT", None)
        return subprocess.run([sys.executable, str(helpers.BIN), *args, "doctor", "--repair"],
                              cwd=str(cwd or proj), env=env, capture_output=True, encoding="utf-8", timeout=60)

    def test_repair_honours_the_project_flag(self):
        tmp, proj, rdir = self._project(True)
        outside = Path(tmp.name) / "elsewhere"
        outside.mkdir()
        cp = self._doctor(tmp, proj, "en", "-p", str(proj), cwd=outside)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("restored", cp.stdout)
        self.assertFalse(ST.is_damaged(rdir))

    def test_a_second_repair_does_not_rename_an_aside_copy_again(self):
        self.assertTrue(ST.is_aside_copy("009-x.damaged") and ST.is_aside_copy("009-x.damaged2"))
        self.assertFalse(ST.is_aside_copy("009-x") or ST.is_aside_copy("009-x.damaged2.bak"))
        tmp, proj, rdir = self._project(False)
        first = rdir.parent / "001-x.damaged"
        first.mkdir()
        (first / "state.json").write_text("{broken", encoding="utf-8")        # an earlier aside copy, still unreadable
        self.assertEqual(self._doctor(tmp, proj, "en").returncode, 0)
        second = rdir.parent / "001-x.damaged2"
        self.assertTrue(second.is_dir() and first.is_dir() and not rdir.exists())
        cp = self._doctor(tmp, proj, "en")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no damaged runs", cp.stdout)
        self.assertEqual(sorted(p.name for p in rdir.parent.iterdir()), ["001-x.damaged", "001-x.damaged2"])

    def _watchdog(self, tmp, lang, *args):
        import subprocess
        import sys
        env = dict(os.environ, ORKESTRA_LANG=lang, ORKESTRA_HOME=str(Path(tmp.name) / "h"),
                   COPILOT_HOME=str(Path(tmp.name) / "c"), ORKESTRA_NO_DAEMON="1",
                   ORKESTRA_WORKSPACE=str(Path(tmp.name)))
        env.pop("ORKESTRA_PROJECT", None)
        return subprocess.run([sys.executable, str(helpers.BIN), "watchdog", *args], cwd=tmp.name, env=env,
                              capture_output=True, encoding="utf-8", timeout=60)

    def test_watchdog_damaged_text_names_the_repair_and_the_missing_prev(self):
        for lang, repair, noprev in (("en", "orkestra doctor --repair", "no state.json.prev"),
                                     ("tr", "orkestra doctor --repair", "state.json.prev yok")):
            tmp, proj, rdir = self._project(False)
            out = self._watchdog(tmp, lang).stdout
            self.assertIn(repair, out)
            self.assertIn(noprev, out)
            (rdir.parent / "keep.damaged2").mkdir()
            (rdir.parent / "keep.damaged2" / "state.json").write_text("{broken", encoding="utf-8")
            self.assertNotIn("keep.damaged2", self._watchdog(tmp, lang).stdout)
            self.assertNotIn("001-x", self._watchdog(tmp, lang, "--quiet").stdout)
        tmp, proj, rdir = self._project(True)
        self.assertNotIn("no state.json.prev", self._watchdog(tmp, "en").stdout)
        self.assertIn("001-x", self._watchdog(tmp, "en").stdout)

    def test_restores_prev_in_both_languages(self):
        for lang, word in (("en", "restored"), ("tr", "geri yüklendi")):
            tmp, proj, rdir = self._project(True)
            cp = self._doctor(tmp, proj, lang)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(word, cp.stdout)
            self.assertFalse(ST.is_damaged(rdir))

    def test_moves_aside_without_backup(self):
        for lang, word in (("en", "moved aside"), ("tr", "taşındı")):
            tmp, proj, rdir = self._project(False)
            cp = self._doctor(tmp, proj, lang)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(word, cp.stdout)
            self.assertFalse(rdir.exists())
            self.assertTrue((rdir.parent / "001-x.damaged" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
