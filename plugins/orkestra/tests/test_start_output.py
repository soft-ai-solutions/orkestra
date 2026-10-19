"""`orkestra start` human output is plain; the machine bind marker keeps the exact format the hooks parse."""
import re
import sys
import unittest
from pathlib import Path

import helpers
from orkestra_core import hookmain
from orkestra_core import state as ST

sys.path.insert(0, str(helpers.PLUGIN / "hooks"))
import copilot_hook  # noqa: E402


class StartOutput(helpers.Sandbox):
    def start(self, lang, git_commit, dirty=False):
        p = self.make_project("demo", git=False)
        self.git(p, "init", "-q")
        if git_commit:
            self.git(p, "config", "user.email", "t@example.com")
            self.git(p, "config", "user.name", "t")
            self.git(p, "add", "-A")
            self.git(p, "commit", "-q", "-m", "init")
        if dirty:
            (p / "calc.py").write_text("# wip\n")
        return self.cli("start", "-p", "demo", "--request", "add a dark mode toggle", env={"ORKESTRA_LANG": lang}).stdout

    def test_no_python_reprs_no_tell_the_user_no_absolute_artifacts(self):
        for lang in ("en", "tr"):
            with self.subTest(lang=lang):
                out = self.start(lang, git_commit=False)
                self.assertNotIn("head=None", out)
                self.assertNotIn("Tell the user", out)
                self.assertIn("baseline: git, no commits yet", out)
                art = [ln for ln in out.splitlines() if ln.startswith("artifacts:")][0]
                self.assertTrue(art.startswith("artifacts: .orkestra/features/"), art)
                self.assertNotIn("—", "".join(ln for ln in out.splitlines() if not ln.startswith("[orkestra:")))
                self.setUp()        # a fresh sandbox for the next language

    def test_committed_and_dirty_baseline_lines(self):
        out = self.start("en", git_commit=True, dirty=True)
        self.assertRegex(out, r"baseline: git [0-9a-f]{12} · uncommitted files: 1")
        self.assertIn("uncommitted changes", out)
        self.assertIn("never be attributed to this run.", out)
        self.assertNotIn("Tell the user", out)

    def test_marker_round_trip_with_the_hooks_regexes(self):
        out = self.start("en", git_commit=True)
        marker = [ln for ln in out.splitlines() if ln.startswith("[orkestra:bind]")]
        self.assertEqual(len(marker), 1, out)
        proj = (self.ws / "demo").resolve()
        rid = re.search(r"run=(\S+)", marker[0]).group(1)
        # the exact stable format
        self.assertEqual(marker[0], f"[orkestra:bind] project={proj} run={rid} home={self.home.resolve()}")
        self.assertEqual(marker[0], ST.bind_marker(proj, rid))
        for regex in (hookmain.BIND_RE, copilot_hook.RUN_MARKER):
            m = regex.search("noise before\n" + out + "\nnoise after")
            self.assertIsNotNone(m, regex.pattern)
            self.assertEqual((Path(m.group(1)), m.group(2)), (proj, rid))
        self.assertEqual(Path(hookmain.BIND_RE.search(out).group(3)), self.home.resolve())
        unbound = ST.unbind_marker(proj, rid)
        self.assertEqual(hookmain.UNBIND_RE.search(unbound).group(2), rid)
        self.assertEqual(copilot_hook.RUN_MARKER.search(unbound).group(2), rid)


if __name__ == "__main__":
    unittest.main()
