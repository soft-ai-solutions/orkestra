"""The installers: install.py (the real implementation, every OS), install.sh (macOS/Linux) and install.ps1 (forwards
to install.py), plus the CI workflow that runs them.

Every run happens in a sandbox: HOME, USERPROFILE, ORKESTRA_HOME, COPILOT_HOME, the PATH-shim folder and the Claude and
Cursor homes all live in a temp directory, agent-host variables are removed, and PATH is stripped of any folder holding
`claude`, `agent`, `copilot` or `orkestra`, so a developer's own installation is never seen, let alone changed.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import i18n

ROOT = helpers.PLUGIN.parent.parent
INSTALL_PY = ROOT / "install.py"
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
BASH = [b for b in dict.fromkeys(filter(None, ("/bin/bash", shutil.which("bash")))) if os.path.exists(b)]
NEEDS_BASH = unittest.skipUnless(os.name != "nt" and BASH, "install.sh is a bash script (Windows uses install.py)")
EM_DASH = chr(0x2014)
HOST_TOOLS = ("claude", "claude.exe", "claude.cmd", "agent", "agent.exe", "cursor-agent", "copilot", "copilot.exe",
              "orkestra", "orkestra.cmd")


def load_installer():
    spec = importlib.util.spec_from_file_location("orkestra_install_under_test", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".copilot").mkdir(parents=True)           # a Copilot home: hooks can be installed without the Cursor tree
        self.oh = self.tmp / "oh"
        self.shim = self.tmp / "shim"
        self.shim.mkdir()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.toolbin = self.tmp / "toolbin"
        self.toolbin.mkdir()
        if os.name != "nt":
            for name, target in (("python3", sys.executable), ("python", sys.executable), ("git", shutil.which("git"))):
                if target:
                    os.symlink(target, self.toolbin / name)

    # ------------------------------------------------------------------ environment
    def path(self, restricted=None):
        if restricted is not None:
            return str(self.toolbin)
        keep = [d for d in os.environ.get("PATH", "").split(os.pathsep)
                if d and not any(os.path.exists(os.path.join(d, n)) for n in HOST_TOOLS)]
        return os.pathsep.join([str(self.toolbin)] + keep)

    def env(self, lang="en", restricted=None, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ORKESTRA_", "CLAUDE", "CURSOR_", "COPILOT_", "LC_")) and k not in ("LANG", "LANGUAGE")}
        env.update({
            "PATH": self.path(restricted), "HOME": str(self.home), "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"), "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "ORKESTRA_HOME": str(self.oh), "ORKESTRA_WORKSPACE": str(self.ws), "ORKESTRA_SHIM_DIR": str(self.shim),
            "COPILOT_HOME": str(self.home / ".copilot"), "ORKESTRA_CURSOR_HOME": str(self.home / ".cursor"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "ORKESTRA_LANG": lang,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        })
        env.update(extra)
        return env

    def snapshot(self):
        """{relative path: content hash or link target} of everything in the sandbox, hook logs and caches aside."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "logs", "toolbin")]
            for name in filenames:
                p = Path(dirpath) / name
                rel = str(p.relative_to(self.tmp))
                out[rel] = os.readlink(p) if p.is_symlink() else hashlib.sha1(p.read_bytes()).hexdigest()
        return out

    # ------------------------------------------------------------------ runners
    def run_py(self, *args, lang="en", stdin="", restricted=None, **extra):
        return subprocess.run([sys.executable, str(INSTALL_PY), *args], input=stdin, capture_output=True, text=True,
                              encoding="utf-8", env=self.env(lang, restricted, **extra), timeout=600)

    def run_sh(self, *args, lang="en", stdin="", restricted=None, bash=None, **extra):
        return subprocess.run([bash or BASH[0], str(INSTALL_SH), *args], input=stdin, capture_output=True, text=True,
                              encoding="utf-8", env=self.env(lang, restricted, **extra), timeout=600)

    def hooks_file(self):
        return self.home / ".copilot" / "plugins" / "orkestra" / "com.github.copilot" / "hooks" / "hooks.json"

    def shim_files(self):
        return sorted(p.name for p in self.shim.iterdir() if p.name.startswith("orkestra"))

    def assertEnglish(self, text):
        self.assertIsNone(TR_LETTERS.search(text), f"Turkish letters in English output: {text[:300]!r}")


# ---------------------------------------------------------------- install.py

class TestInstallPyDryRun(Sandbox):
    def test_the_dry_run_plans_in_both_languages_and_changes_nothing(self):
        before = self.snapshot()
        en = self.run_py("--dry-run", "--yes", "--skip-tests")
        tr = self.run_py("--dry-run", "--yes", "--skip-tests", lang="tr")
        self.assertEqual((en.returncode, tr.returncode), (0, 0), en.stderr + tr.stderr)
        self.assertEqual(self.snapshot(), before, "a dry run must not touch the disk")
        self.assertIn("Dry run: nothing is changed", en.stdout)
        self.assertIn("would run:", en.stdout)
        self.assertIn("Copilot detected", en.stdout)
        self.assertIn("hooks.json", en.stdout)
        self.assertEnglish(en.stdout)
        self.assertIn("Deneme modu", tr.stdout)
        self.assertIn("çalıştırılırdı", tr.stdout)
        self.assertNotIn(EM_DASH, en.stdout + tr.stdout)

    def test_the_tests_are_skipped_by_default_and_run_with_with_tests(self):
        skipped = "Tests skipped (add --with-tests to run them)"
        self.assertIn(skipped, self.run_py("--dry-run").stdout)
        self.assertIn(skipped, self.run_py("--dry-run", "--skip-tests").stdout, "--skip-tests is a no-op alias")
        plan = self.run_py("--dry-run", "--with-tests").stdout
        self.assertIn("would run the plugin and bridge test suites", plan)
        self.assertNotIn("Tests skipped", plan)
        self.assertIn("--with-tests ekleyin", self.run_py("--dry-run", lang="tr").stdout)
        self.assertIn("--with-tests", self.run_py("--help").stdout)

    def test_uninstall_dry_run_changes_nothing_even_after_a_real_install(self):
        self.assertEqual(self.run_py("--yes", "--skip-tests").returncode, 0)
        before = self.snapshot()
        cp = self.run_py("--uninstall", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("would", cp.stdout)
        self.assertIn("Dry run finished", cp.stdout)

    def test_every_plan_line_is_a_sentence_in_english(self):
        """`would` + the kit's noun phrase read 'would hooks.json: orkestra entries ...': the actions start with a verb."""
        install_plan = self.run_py("--dry-run", "--yes", "--skip-tests").stdout
        self.assertRegex(install_plan, r"would update \S*hooks\.json: orkestra entries for")
        self.assertEqual(self.run_py("--yes", "--skip-tests").returncode, 0)
        uninstall_plan = self.run_py("--uninstall", "--dry-run").stdout
        self.assertRegex(uninstall_plan, r"would update \S*hooks\.json: orkestra entries removed")
        for text in (install_plan, uninstall_plan):
            self.assertNotRegex(text, r"would hooks\.json")
            for line in text.splitlines():
                if line.startswith("  - would "):
                    self.assertRegex(line, r"^  - would (run|update|remove|link|copy|write|backup|register|delete)\b", line)

    def test_dry_run_plans_pi_when_a_pi_home_exists(self):
        (self.home / ".pi" / "agent").mkdir(parents=True)
        out = self.run_py("--dry-run", "--yes", "--skip-tests").stdout
        self.assertIn("Pi detected", out)
        self.assertRegex(out, r"would \w+ .*\.pi")

    def test_every_flag_install_sh_has_is_still_accepted(self):
        cp = self.run_py("--dry-run", "-y", "--yes", "--no-mcp", "--skip-tests")
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_purge_only_goes_with_uninstall(self):
        for lang, text in (("en", "--purge only works together with --uninstall"), ("tr", "yalnız --uninstall ile")):
            cp = self.run_py("--purge", lang=lang)
            self.assertEqual(cp.returncode, 2)
            self.assertIn(text, cp.stderr)
        self.assertEqual(self.snapshot(), {})


class TestInstallPyLifecycle(Sandbox):
    def test_install_twice_then_uninstall_twice_keeps_the_users_own_things(self):
        # what the user already had: settings, a hook of their own, a foreign tool in the shim folder, a project
        self.oh.mkdir()
        (self.oh / "config.json").write_text(json.dumps({"lang": "en", "marker": "keep-me"}), encoding="utf-8")
        self.hooks_file().parent.mkdir(parents=True)
        self.hooks_file().write_text(json.dumps({"version": 1, "hooks": {"Stop": [{"command": "./mine.sh"}]}}), encoding="utf-8")
        (self.shim / "other-tool").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.ws / "proj" / ".orkestra").mkdir(parents=True)
        (self.ws / "proj" / ".orkestra" / "state.txt").write_text("a run", encoding="utf-8")

        first = self.run_py("--yes", "--skip-tests")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("Orkestra install.py finished.", first.stdout)
        doc = json.loads(self.hooks_file().read_text(encoding="utf-8"))
        for event in ("SessionStart", "PreToolUse", "Stop"):
            self.assertTrue(any("copilot_hook.py" in (e.get("bash") or e.get("command") or "") for e in doc["hooks"][event]), event)
        self.assertIn({"command": "./mine.sh"}, doc["hooks"]["Stop"])
        self.assertTrue(self.shim_files(), "the PATH shim")
        self.assertEqual(json.loads((self.oh / "config.json").read_text(encoding="utf-8"))["marker"], "keep-me")

        installed = self.snapshot()
        second = self.run_py("--yes", "--skip-tests")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.snapshot(), installed, "a second install must change nothing")

        gone = self.run_py("--uninstall")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertIn("Orkestra was uninstalled.", gone.stdout)
        doc = json.loads(self.hooks_file().read_text(encoding="utf-8"))
        self.assertFalse(any("copilot_hook.py" in e.get("command", "") for es in doc["hooks"].values() for e in es))
        self.assertEqual(doc["hooks"]["Stop"], [{"command": "./mine.sh"}])
        self.assertEqual(self.shim_files(), [], "our shim is gone")
        self.assertTrue((self.shim / "other-tool").exists(), "somebody else's tool is not ours to remove")
        self.assertEqual(json.loads((self.oh / "config.json").read_text(encoding="utf-8"))["marker"], "keep-me")
        self.assertTrue((self.ws / "proj" / ".orkestra" / "state.txt").exists())
        self.assertIn("Kept", gone.stdout)
        self.assertIn("--purge", gone.stdout)

        after = self.snapshot()
        again = self.run_py("--uninstall")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("Nothing to remove", again.stdout)
        self.assertEqual(self.snapshot(), after, "uninstall is idempotent")

        kept = self.run_py("--uninstall", "--purge", stdin="n\n")
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertTrue((self.oh / "config.json").exists(), "declined: nothing deleted")
        self.assertIn(f"{self.oh} kept", kept.stdout)

        purged = self.run_py("--uninstall", "--purge", "--yes")
        self.assertEqual(purged.returncode, 0, purged.stderr)
        self.assertFalse(self.oh.exists(), "--purge removes the Orkestra home")
        self.assertTrue((self.ws / "proj" / ".orkestra" / "state.txt").exists(), "per-project run data is never purged")
        self.assertIn("Per-project .orkestra folders", purged.stdout)

    def test_purge_asks_in_the_users_language_and_accepts_a_turkish_yes(self):
        self.oh.mkdir()
        (self.oh / "config.json").write_text("{}", encoding="utf-8")
        cp = self.run_py("--uninstall", "--purge", lang="tr", stdin="e\n")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(self.oh.exists())
        self.assertIn("[e/H]", cp.stdout)
        self.assertRegex(cp.stdout, "silinemedi|silindi")

    def test_no_terminal_means_no_deletion(self):
        self.oh.mkdir()
        (self.oh / "config.json").write_text("{}", encoding="utf-8")
        cp = self.run_py("--uninstall", "--purge", stdin="")             # EOF on stdin
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue(self.oh.exists())

    def test_purge_never_deletes_a_folder_that_is_not_an_orkestra_home(self):
        """ORKESTRA_HOME is whatever the environment says: pointing it at Documents or at `.` used to delete them."""
        docs = self.tmp / "Documents"
        (docs / "thesis").mkdir(parents=True)
        (docs / "thesis" / "chapter1.txt").write_text("years of work", encoding="utf-8")
        for flags in (["--yes"], ["--yes", "--dry-run"]):
            cp = self.run_py("--uninstall", "--purge", *flags, ORKESTRA_HOME=str(docs))
            self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
            self.assertIn("was not deleted", cp.stdout)
            self.assertIn("does not look like an Orkestra home", cp.stdout)
            self.assertNotIn("Orkestra was uninstalled", cp.stdout)
            self.assertEqual((docs / "thesis" / "chapter1.txt").read_text(encoding="utf-8"), "years of work")
        tr = self.run_py("--uninstall", "--purge", "--yes", lang="tr", ORKESTRA_HOME=str(docs))
        self.assertEqual(tr.returncode, 1)
        self.assertIn("silinmedi", tr.stdout)
        self.assertTrue((docs / "thesis" / "chapter1.txt").exists())

    def test_purge_with_a_relative_orkestra_home_never_empties_the_current_directory(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        (proj / "main.py").write_text("print('mine')\n", encoding="utf-8")
        cp = subprocess.run([sys.executable, str(INSTALL_PY), "--uninstall", "--purge", "--yes"], cwd=str(proj),
                            capture_output=True, text=True, encoding="utf-8", env=self.env("en", ORKESTRA_HOME="."),
                            timeout=120)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("the current directory", cp.stdout)
        self.assertEqual((proj / "main.py").read_text(encoding="utf-8"), "print('mine')\n")

    def test_purge_expands_a_tilde_like_orkestra_does_and_deletes_the_real_home(self):
        real = self.home / ".orkx"
        real.mkdir()
        (real / "config.json").write_text("{}", encoding="utf-8")
        cp = self.run_py("--uninstall", "--purge", "--yes", ORKESTRA_HOME="~/.orkx")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse(real.exists())

    def test_an_empty_orkestra_home_and_one_with_only_markers_can_be_purged(self):
        self.oh.mkdir()
        cp = self.run_py("--uninstall", "--purge", "--yes")
        self.assertEqual((cp.returncode, self.oh.exists()), (0, False), cp.stdout + cp.stderr)
        for marker in ("sessions", "bridge", "grants"):
            (self.oh / marker).mkdir(parents=True)
            (self.oh / marker / "x.json").write_text("{}", encoding="utf-8")
            (self.oh / "unrelated.txt").write_text("a leftover next to the markers", encoding="utf-8")
            cp = self.run_py("--uninstall", "--purge", "--yes")
            self.assertEqual((cp.returncode, self.oh.exists()), (0, False), (marker, cp.stdout + cp.stderr))

    def test_the_blocker_names_the_dangerous_places(self):
        mod = load_installer()
        work = self.tmp / "work"
        work.mkdir()
        afile = self.tmp / "afile"
        afile.write_text("x", encoding="utf-8")
        env = {"ORKESTRA_LANG": "en", "HOME": str(self.home), "USERPROFILE": str(self.home)}
        with mock.patch.dict(os.environ, env), mock.patch.object(mod.os, "getcwd", return_value=str(work)):
            for place, why in ((Path(work.anchor), "root of a drive"), (self.home, "your user folder"),
                               (self.tmp, "your user folder"), (ROOT, "contains this Orkestra checkout"),
                               (ROOT.parent, "contains this Orkestra checkout"), (work, "current directory"),
                               (afile, "not a folder")):
                self.assertIn(why, mod._purge_blocker(place), str(place))
            (self.oh / "sessions").mkdir(parents=True)
            self.assertEqual(mod._purge_blocker(self.oh), "", "a folder with an Orkestra marker is fine")

    def test_a_purge_that_cannot_delete_fails_and_does_not_claim_success(self):
        self.oh.mkdir()
        (self.oh / "config.json").write_text("{}", encoding="utf-8")
        mod = load_installer()
        out, err = io.StringIO(), io.StringIO()
        # everything that could reach the developer's own machine is stubbed: this runs in-process
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en", "ORKESTRA_HOME": str(self.oh)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(mod, "_which", lambda name: None), mock.patch.object(mod, "_kits", lambda: (None, None, None)), \
                mock.patch.object(mod, "_remove_launch_agent", lambda dry: False), \
                mock.patch.object(mod.shutil, "rmtree", side_effect=OSError("busy")):
            rc = mod.main(["--uninstall", "--purge", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("could not delete", out.getvalue())
        self.assertNotIn("Orkestra was uninstalled", out.getvalue())
        self.assertTrue((self.oh / "config.json").exists())

    def test_uninstall_on_a_machine_that_never_had_orkestra_is_quiet_and_successful(self):
        cp = self.run_py("--uninstall")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("Nothing to remove", cp.stdout)
        tr = self.run_py("--uninstall", lang="tr")
        self.assertIn("Kaldırılacak bir şey yok", tr.stdout)
        self.assertEnglish(cp.stdout)

    def test_a_hooks_file_we_cannot_read_is_reported_and_left_alone(self):
        self.hooks_file().parent.mkdir(parents=True)
        self.hooks_file().write_text('{"version": 1, "hooks": [1]}', encoding="utf-8")      # "hooks" is not an object
        before = self.snapshot()
        cp = self.run_py("--uninstall")
        # reported and left alone; and since the hooks are still registered it is no "uninstalled" and no exit 0
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Copilot: could not remove Orkestra", cp.stdout)
        self.assertNotIn("Orkestra was uninstalled", cp.stdout)
        dry = self.run_py("--uninstall", "--dry-run")
        self.assertIn("could not plan the Copilot step", dry.stdout)
        self.assertEqual(self.snapshot(), before)


class TestInstallPyGates(Sandbox):
    def setUp(self):
        super().setUp()
        self.mod = load_installer()

    def main(self, argv, lang="en", **patches):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": lang}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(mock.patch.object(self.mod, name, value))
                rc = self.mod.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_python_older_than_39_is_refused_with_the_version_and_where_to_get_one(self):
        for platform, hint in (("windows", "winget install Python.Python.3.12"), ("macos", "brew install python"),
                               ("linux", "sudo apt install python3")):
            rc, out, err = self.main(["--dry-run"], _python_version=lambda: (3, 8, 10), _platform=lambda p=platform: p)
            self.assertEqual(rc, 2, platform)
            self.assertEqual(out, "", "nothing else may run")
            for needle in ("3.9", "3.8.10", hint):
                self.assertIn(needle, err, (platform, needle))
            self.assertEnglish(err)
        rc, out, err = self.main(["--dry-run"], lang="tr", _python_version=lambda: (3, 8, 10), _platform=lambda: "linux")
        self.assertEqual(rc, 2)
        self.assertIn("gerekli", err)
        self.assertRegex(err, TR_LETTERS)

    def test_the_gate_comes_before_argument_parsing_so_help_on_an_old_python_still_explains(self):
        rc, out, err = self.main(["--help"], _python_version=lambda: (3, 6, 15))
        self.assertEqual(rc, 2)
        self.assertIn("3.6.15", err)

    def test_the_minimum_itself_is_accepted(self):
        for version in ((3, 9, 0), (3, 9, 18), (3, 14, 1), (4, 0, 0)):
            with mock.patch.object(self.mod, "_python_version", lambda v=version: v):
                self.assertIsNone(self.mod.check_python(), version)
        with mock.patch.object(self.mod, "_python_version", lambda: (3, 8, 99)):
            self.assertIsNotNone(self.mod.check_python())

    def test_missing_git_is_refused_with_an_install_hint_per_platform(self):
        real_which = shutil.which
        for platform, hint in (("windows", "winget install Git.Git"), ("macos", "xcode-select --install"),
                               ("linux", "sudo apt install git")):
            rc, out, err = self.main(["--dry-run", "--skip-tests"], _platform=lambda p=platform: p,
                                     _which=lambda name: None if name == "git" else real_which(name))
            self.assertEqual(rc, 2, platform)
            self.assertIn("git was not found on PATH", err)
            self.assertIn(hint, err)
            self.assertNotIn("Dry run finished", out)
        rc, out, err = self.main(["--dry-run"], lang="tr", _platform=lambda: "linux",
                                 _which=lambda name: None if name == "git" else real_which(name))
        self.assertEqual(rc, 2)
        self.assertIn("PATH içinde bulunamadı", err)

    def test_install_py_exits_non_zero_when_the_setup_of_a_detected_engine_fails_end_to_end(self):
        hooks = self.hooks_file()
        hooks.parent.mkdir(parents=True)
        hooks.write_text("{ \"hooks\": [", encoding="utf-8")
        cp = self.run_py("--yes", "--skip-tests")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertNotIn("install.py finished", cp.stdout)
        self.assertRegex(cp.stderr, "setup. failed|not ready")

    def test_the_install_stops_when_the_tests_fail(self):
        calls = []

        def fake_run(argv, check=True, dry=False):
            calls.append(list(map(str, argv)))
            return 1 if "unittest" in map(str, argv) else 0

        rc, out, err = self.main(["--yes", "--with-tests"], run=fake_run)
        self.assertEqual(rc, 1)
        self.assertIn("The tests failed: install stopped", err)
        self.assertEqual(len(calls), 1, "no setup after a red test suite")

    def test_a_default_install_runs_no_test_suite(self):
        for flags in ([], ["--skip-tests"]):
            calls = []

            def fake_run(argv, check=True, dry=False, calls=calls):
                calls.append(list(map(str, argv)))
                return 0

            rc, out, err = self.main(["--yes"] + flags, run=fake_run)
            self.assertEqual(rc, 0, err)
            self.assertFalse([c for c in calls if "unittest" in c], calls)
            self.assertTrue([c for c in calls if "setup" in c], "the setup still runs")

    def test_a_failed_setup_or_health_check_with_an_engine_present_is_a_failed_install(self):
        """`python install.py --yes && next-step` in CI or a provisioning script must not go on when nothing was set up."""
        with_copilot = {"Claude Code": False, "Cursor": False, "Copilot": True}
        for fails, needle in (("setup", "`orkestra setup` failed"), ("doctor", "Orkestra is not ready")):
            def fake_run(argv, check=True, dry=False, fails=fails):
                return 2 if fails in map(str, argv) else 0
            rc, out, err = self.main(["--yes", "--skip-tests"], run=fake_run, _engines=lambda: with_copilot)
            self.assertEqual(rc, 1, fails)
            self.assertIn(needle, err)
            self.assertNotIn("install.py finished", out, "no success line after a failure")
        rc, out, err = self.main(["--yes", "--skip-tests"], lang="tr", _engines=lambda: with_copilot,
                                 run=lambda argv, check=True, dry=False: 2 if "setup" in map(str, argv) else 0)
        self.assertEqual(rc, 1)
        self.assertIn("başarısız", err)

    def test_without_any_engine_the_installer_still_succeeds_because_the_path_shim_was_the_point(self):
        none = {"Claude Code": False, "Cursor": False, "Copilot": False}
        rc, out, err = self.main(["--yes", "--skip-tests"], run=lambda argv, check=True, dry=False: 1, _engines=lambda: none)
        self.assertEqual(rc, 0, err)
        self.assertIn("install.py finished", out)

    def test_language_is_resolved_like_orkestra_does_with_and_without_the_library(self):
        cases = [{}, {"ORKESTRA_LANG": "tr"}, {"ORKESTRA_LANG": "TR_tr.UTF-8"}, {"ORKESTRA_LANG": "en", "LANG": "tr_TR.UTF-8"},
                 {"ORKESTRA_LANG": "de", "LANG": "tr_TR.UTF-8"}, {"ORKESTRA_LANG": "auto", "LANG": "tr_TR.UTF-8"},
                 {"LANG": "tr_TR.UTF-8"}, {"LANG": "en_US.UTF-8"}, {"LC_ALL": "C", "LANG": "tr_TR.UTF-8"},
                 {"LC_ALL": "tr_TR.UTF-8", "LANG": "en_US.UTF-8"}, {"LANGUAGE": "de:tr:en"}, {"LANG": "de_DE.UTF-8"}]
        for env in cases:
            with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.tmp)}, clear=True):
                os.environ.update(env)
                with mock.patch.object(i18n, "_windows_locale_name", return_value=""):
                    want = i18n.lang()
                    self.assertEqual(self.mod._lang(), want, f"with the library {env}")
                    with mock.patch.dict(sys.modules, {"orkestra_core": None, "orkestra_core.i18n": None}):
                        self.assertEqual(self.mod._lang(), want, f"fallback resolver {env}")

    def test_the_fallback_reads_the_language_from_config_json(self):
        (self.tmp / "config.json").write_text(json.dumps({"lang": "tr"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.tmp)}, clear=True), \
                mock.patch.dict(sys.modules, {"orkestra_core": None, "orkestra_core.i18n": None}):
            self.assertEqual(self.mod._lang(), "tr")
            (self.tmp / "config.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(self.mod._lang(), "en")


class TestInstallPyMessages(unittest.TestCase):
    def setUp(self):
        self.mod = load_installer()
        self.src = INSTALL_PY.read_text(encoding="utf-8")

    def test_every_message_has_both_languages_with_the_same_fields(self):
        field = re.compile(r"\{(\w+)\}")
        self.assertGreaterEqual(len(self.mod._MESSAGES), 40)
        for key, (en, tr) in self.mod._MESSAGES.items():
            self.assertTrue(en.strip() and tr.strip(), key)
            self.assertEqual(sorted(field.findall(en)), sorted(field.findall(tr)), key)
            self.assertIsNone(TR_LETTERS.search(en), f"{key}: Turkish letters in the English text")
            self.assertNotIn(EM_DASH, en + tr, f"{key}: no em dash in user-visible text")
            en.format(**{f: "x" for f in field.findall(en)})
            tr.format(**{f: "x" for f in field.findall(tr)})

    def test_turkish_texts_are_translations_not_copies(self):
        same = [k for k, (en, tr) in self.mod._MESSAGES.items()
                if en == tr and len(re.sub(r"\{\w+\}|[^A-Za-z]", "", en)) > 12]
        self.assertEqual(same, [])

    def test_the_message_keys_used_in_the_code_exist_and_none_is_dead(self):
        used = {k for k in re.findall(r'_m\("(\w+)"', self.src) if not k.endswith("_")}      # hint_<what>_ + platform is built
        dynamic = {f"hint_{what}_{plat}" for what in ("python", "git") for plat in ("windows", "macos", "linux")}
        table = set(self.mod._MESSAGES)
        self.assertEqual(sorted(used - table), [], "used but not defined")
        self.assertEqual(sorted(table - used - dynamic), [], "defined but never used")
        self.assertTrue(dynamic <= table)

    def test_the_file_is_plain_ascii_and_parses_as_old_python(self):
        self.src.encode("ascii")
        for version in ((3, 6), (3, 7), (3, 9)):
            ast.parse(self.src, filename="install.py", feature_version=version)
        self.assertIsNone(re.search(r"^from __future__", self.src, re.M),
                          "a SyntaxError on Python 3.6 would hide the version message")
        self.assertNotRegex(self.src, r":=|f\"|f'", "no walrus and no f-strings: the version gate must be reachable")

    def test_windows_and_launchd_notes_remain(self):
        self.assertIn('os.name == "nt"', self.src)
        self.assertIn("LaunchAgent", self.src)
        self.assertIn("skip", self.src.casefold())


# ---------------------------------------------------------------- install.sh

@NEEDS_BASH
class TestInstallSh(Sandbox):
    def test_it_parses_in_every_bash(self):
        for bash in BASH:
            cp = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True)
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), bash)

    def test_help_lists_every_flag_and_points_windows_users_to_install_py(self):
        cp = self.run_sh("--help")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for flag in ("--yes", "--no-mcp", "--with-tests", "--skip-tests", "--dry-run", "--uninstall", "--purge", "--refresh-vendor",
                     "install.py"):
            self.assertIn(flag, cp.stdout)
        self.assertEqual(self.snapshot(), {})

    def test_an_unknown_option_never_starts_a_real_install(self):
        for lang, text in (("en", "unknown option(s): --dryrun"), ("tr", "bilinmeyen seçenek(ler): --dryrun")):
            cp = self.run_sh("--dryrun", "--yes", lang=lang)
            self.assertEqual(cp.returncode, 1, cp.stdout)
            self.assertIn(text, cp.stderr)
        self.assertEqual(self.snapshot(), {})

    def test_purge_alone_is_refused(self):
        cp = self.run_sh("--purge")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("--purge only works together with --uninstall", cp.stderr)

    def test_dry_run_changes_nothing_and_prints_the_plan_in_both_languages(self):
        before = self.snapshot()
        en = self.run_sh("--dry-run", "--skip-tests")
        tr = self.run_sh("--dry-run", "--skip-tests", lang="tr")
        self.assertEqual((en.returncode, tr.returncode), (0, 0), en.stderr + tr.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Dry run: nothing is changed", en.stdout)
        self.assertIn("would run:", en.stdout)
        self.assertIn("Dry run finished. Nothing was changed.", en.stdout)
        self.assertNotIn("Orkestra is ready", en.stdout, "no verdict about an install that did not happen")
        self.assertIn("Deneme modu", tr.stdout)
        self.assertIn("Deneme bitti", tr.stdout)

    def test_dry_run_never_asks_a_question(self):
        cp = self.run_sh("--dry-run", "--skip-tests", stdin="")
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_the_tests_are_skipped_by_default_and_with_tests_plans_them(self):
        self.assertNotIn("test suites", self.run_sh("--dry-run").stdout)
        self.assertNotIn("test suites", self.run_sh("--dry-run", "--skip-tests").stdout)
        self.assertIn("run the plugin and bridge Python test suites", self.run_sh("--dry-run", "--with-tests").stdout)
        self.assertIn("bridge Python testlerini çalıştır", self.run_sh("--dry-run", "--with-tests", lang="tr").stdout)

    def fake_claude(self):
        """A `claude` and an `npx` that do nothing: the MCP steps run (as a plan) as if Claude Code were installed."""
        fake = self.tmp / "fakeclaude"
        fake.mkdir(exist_ok=True)
        for name in ("claude", "npx"):
            (fake / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake / name).chmod(0o755)
        return {"PATH": str(fake) + os.pathsep + self.path()}

    def test_playwright_is_pinned_and_the_context7_endpoint_is_stated(self):
        text = INSTALL_SH.read_text(encoding="utf-8")
        self.assertNotIn("@latest", text)
        pin = re.search(r'(?m)^PLAYWRIGHT_MCP_VERSION="(\d+\.\d+\.\d+)"$', text)
        self.assertIsNotNone(pin, "an exact @playwright/mcp version")
        extra = self.fake_claude()
        before = self.snapshot()
        for lang, words in (("en", ("third-party", "https://mcp.context7.com/mcp", "sent to it as queries", "--no-mcp")),
                            ("tr", ("üçüncü taraf", "https://mcp.context7.com/mcp", "sorgu olarak", "--no-mcp"))):
            cp = self.run_sh("--dry-run", "--yes", lang=lang, **extra)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            for word in words:
                self.assertIn(word, cp.stdout, lang)
            self.assertIn("npx @playwright/mcp@" + pin.group(1), cp.stdout)
            self.assertNotIn("@latest", cp.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_no_mcp_skips_every_mcp_step_and_the_notice(self):
        cp = self.run_sh("--dry-run", "--yes", "--no-mcp", **self.fake_claude())
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("mcp add", cp.stdout)
        self.assertNotIn("Context7", cp.stdout)
        with_mcp = self.run_sh("--dry-run", "--yes", **self.fake_claude())
        self.assertIn("mcp add --transport http context7", with_mcp.stdout)

    def test_uninstall_is_delegated_to_install_py(self):
        before = self.snapshot()
        cp = self.run_sh("--uninstall", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Dry run finished. Nothing was changed.", cp.stdout)
        self.assertIn("Removing what this installer set up", cp.stdout, "the text comes from install.py --uninstall")
        tr = self.run_sh("--uninstall", "--dry-run", lang="tr")
        self.assertIn("Deneme bitti", tr.stdout)
        self.assertIn("Bu kurulumun ekledikleri", tr.stdout)

    def test_install_twice_then_uninstall_with_the_shell_script(self):
        first = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(self.hooks_file().exists())
        self.assertTrue(self.shim_files())
        installed = self.snapshot()
        second = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.snapshot(), installed, "install.sh is safe to run again")
        gone = self.run_sh("--uninstall", "--yes")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertEqual(self.shim_files(), [])
        doc = json.loads(self.hooks_file().read_text(encoding="utf-8"))
        self.assertFalse(any("copilot_hook.py" in e.get("command", "") for es in doc["hooks"].values() for e in es))
        self.assertTrue((self.oh / "config.json").exists(), "settings stay without --purge")

    def test_it_survives_a_missing_home_variable(self):
        """Containers, cron and `env -i` have no HOME: `set -u` made the installer die with 'HOME: unbound variable'."""
        env = self.env()
        del env["HOME"], env["USERPROFILE"]
        cp = subprocess.run([BASH[0], str(INSTALL_SH), "--dry-run", "--yes", "--skip-tests"], capture_output=True,
                            text=True, encoding="utf-8", env=env, timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("unbound variable", cp.stderr)
        self.assertIn("Dry run finished", cp.stdout)

    def test_a_checkout_path_with_an_apostrophe_still_gets_its_path_shim(self):
        link = self.tmp / "o'brien"
        os.symlink(str(ROOT), str(link))
        cp = subprocess.run([BASH[0], str(link / "install.sh"), "--dry-run", "--yes", "--skip-tests"], capture_output=True,
                            text=True, encoding="utf-8", env=self.env(), timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("SyntaxError", cp.stderr)
        self.assertNotIn("PATH shim failed", cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout, r"(link|write) .*orkestra")                 # the dry-run plan names the shim

    def answer(self, reply, default, lang="en"):
        """YES or NO: what the installer's own `ask` makes of one typed reply (the function is taken from the script)."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        ask = re.search(r"^ask\(\)\s*\{.*?^\}", text, re.M | re.S).group(0)
        script = ("YES=0; DRY=0; LANG_CODE=%s\nL() { if [ \"$LANG_CODE\" = tr ]; then printf '%%s' \"$2\"; "
                  "else printf '%%s' \"$1\"; fi; }\n%s\nif ask 'question?' %s; then echo YES; else echo NO; fi\n"
                  % (lang, ask, default))
        cp = subprocess.run([BASH[0], "-c", script], input=reply + "\n", capture_output=True, text=True, encoding="utf-8",
                            env=self.env(lang), timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip().splitlines()[-1]

    def test_a_yes_is_a_whole_word_and_exit_or_enable_are_not_one(self):
        for reply in ("y", "Y", "yes", "YES", " Yes ", "y\t"):
            self.assertEqual(self.answer(reply, "n"), "YES", reply)
        for reply in ("exit", "enable", "everything", "e", "evet", "no", "n", "quit", "yep", "yes please", "0"):
            self.assertEqual(self.answer(reply, "y" if reply in ("", "x") else "n"), "NO", reply)

    def test_the_default_applies_to_an_empty_answer_and_turkish_keeps_its_yes(self):
        self.assertEqual(self.answer("", "y"), "YES")
        self.assertEqual(self.answer("", "n"), "NO")
        for reply in ("e", "E", "evet", "EVET", "y", "yes"):
            self.assertEqual(self.answer(reply, "n", lang="tr"), "YES", reply)
        for reply in ("h", "hayir", "exit", "enable", "everything", "no"):
            self.assertEqual(self.answer(reply, "y" if reply == "no" else "n", lang="tr"), "NO", reply)

    def test_a_detected_engine_whose_setup_failed_is_a_failed_install(self):
        """Exit 0 let `bash install.sh --yes && next-step` go on although nothing had been installed."""
        self.hooks_file().parent.mkdir(parents=True)
        self.hooks_file().write_text("{ \"hooks\": [", encoding="utf-8")                  # a hooks.json nobody can parse
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("Orkestra is not ready", cp.stdout + cp.stderr)

    def test_a_healthy_install_is_reported_ready_not_a_pipefail_false_alarm(self):
        """`copilot status | grep -q` under pipefail killed the status command with SIGPIPE: 'not ready' on a good install."""
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Orkestra is ready.", cp.stdout)
        self.assertNotIn("not ready", cp.stdout + cp.stderr)

    def test_with_no_engine_at_all_the_exit_code_stays_zero(self):
        import shutil as _shutil
        _shutil.rmtree(self.home / ".copilot")
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_python_older_than_39_is_refused_with_an_actionable_message(self):
        old = self.toolbin / "python3"
        old.unlink()
        old.write_text('#!/bin/sh\ncase "$1" in -V) echo "Python 3.8.10";; *) exit 1;; esac\n', encoding="utf-8")
        old.chmod(0o755)
        for lang, needle in (("en", "python3 >= 3.9 is required (Python 3.8.10)"), ("tr", "python3 >= 3.9 gerekli (Python 3.8.10)")):
            cp = self.run_sh("--skip-tests", lang=lang)
            self.assertEqual(cp.returncode, 1, cp.stdout)
            self.assertIn(needle, cp.stderr)
            self.assertRegex(cp.stderr, r"brew install python|apt install python3")
        self.assertEqual(self.snapshot(), {})

    def test_missing_git_is_refused_with_an_install_hint(self):
        for tool in ("dirname", "tr", "sed", "head", "uname", "awk"):
            found = shutil.which(tool, path="/usr/bin:/bin")
            if found:
                os.symlink(found, self.toolbin / tool)
        (self.toolbin / "git").unlink()
        for lang, needle in (("en", "git not found"), ("tr", "git bulunamadı")):
            cp = self.run_sh("--skip-tests", lang=lang, restricted=True)
            self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
            self.assertIn(needle, cp.stderr)
            self.assertRegex(cp.stderr, r"xcode-select --install|apt install git")
        self.assertEqual(self.snapshot(), {})

    def test_the_existing_structure_other_tests_rely_on_is_intact(self):
        text = INSTALL_SH.read_text(encoding="utf-8")
        for flag in ("--yes|-y)", "--no-mcp)", "--refresh-vendor)", "--skip-tests)", "-h|--help)"):
            self.assertIn(flag, text)
        self.assertRegex(text, r"[Ww]indows.*(install\.py|install\.ps1)")


# ---------------------------------------------------------------- second review round

class Round2(Sandbox):
    """Failures the installers used to report as success, and a dry run that wrote into the checkout."""

    def fake_claude(self, listing="", failing=()):
        """A `claude` that logs its arguments, lists `listing` as the registered marketplaces and fails `failing`."""
        bindir = self.tmp / "fakeclaude"
        bindir.mkdir(exist_ok=True)
        self.claude_log = self.tmp / "claude.log"
        self.claude_log.write_text("")
        body = "#!/bin/sh\necho \"$*\" >> '%s'\ncase \"$*\" in\n  \"plugin marketplace list\") printf '%%s\\n' '%s';;\n  --version) echo 9;;\nesac\n" % (
            self.claude_log, listing.replace("'", ""))
        body += "".join('case "$*" in "%s"*) exit 1;; esac\n' % f for f in failing) + "exit 0\n"
        (bindir / "claude").write_text(body, encoding="utf-8")
        (bindir / "claude").chmod(0o755)
        return os.pathsep.join([str(bindir), self.path()])

    def claude_calls(self):
        return [line for line in self.claude_log.read_text().splitlines()
                if line not in ("plugin marketplace list", "--version")]

    def break_the_config(self):
        self.oh.mkdir(parents=True, exist_ok=True)
        (self.oh / "config.json").write_text('{"guards": "yes"}', encoding="utf-8")     # doctor: a required check fails


class TestRound2InstallPy(Round2):
    def test_a_failed_health_check_is_a_failed_install_that_does_not_blame_the_engines(self):
        self.break_the_config()
        for lang, claim in (("en", "failed required checks"), ("tr", "başarısız gerekli kontroller")):
            cp = self.run_py("--yes", "--skip-tests", lang=lang)
            self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
            self.assertIn(claim, cp.stderr)
            self.assertNotIn("no engine is usable", cp.stdout + cp.stderr)
            self.assertNotIn("finished", cp.stdout)

    @NEEDS_BASH
    def test_a_failing_claude_command_fails_the_install(self):
        path = self.fake_claude(failing=("plugin install", "plugin marketplace add"))
        cp = self.run_py("--yes", "--skip-tests", PATH=path)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertNotIn("install.py finished", cp.stdout)
        self.assertNotIn("Claude marketplace/plugin done", cp.stdout)
        self.assertIn("FAILED", cp.stdout)

    def test_uninstall_with_a_hooks_json_that_does_not_parse_changes_nothing_and_fails(self):
        (self.home / ".cursor").mkdir()
        self.assertEqual(self.run_py("--yes", "--skip-tests").returncode, 0)
        cursor_hooks = self.home / ".cursor" / "hooks.json"
        for hooks in (cursor_hooks, self.hooks_file()):
            hooks.write_text("// my hooks\n" + hooks.read_text(encoding="utf-8"), encoding="utf-8")
        before = {p: p.read_text(encoding="utf-8") for p in (cursor_hooks, self.hooks_file())}
        skills = sorted(p.name for p in (self.home / ".cursor" / "skills").iterdir())
        self.assertTrue(skills)
        for lang, done in (("en", "Orkestra was uninstalled"), ("tr", "Orkestra kaldırıldı")):
            cp = self.run_py("--uninstall", "--yes", lang=lang)
            self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
            self.assertNotIn(done, cp.stdout)
            self.assertIn("hooks.json", cp.stdout + cp.stderr)
            self.assertEqual({p: p.read_text(encoding="utf-8") for p in before}, before)
            self.assertEqual(sorted(p.name for p in (self.home / ".cursor" / "skills").iterdir()), skills,
                             "the skills must stay while their hooks stay")

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_purge_through_a_symlinked_home_deletes_the_target_and_the_link(self):
        real = self.tmp / "dotfiles" / "orkestra-home"
        real.mkdir(parents=True)
        (real / "config.json").write_text("{}", encoding="utf-8")
        link = self.tmp / "link-home"
        os.symlink(str(real), str(link))
        cp = self.run_py("--uninstall", "--purge", "--yes", ORKESTRA_HOME=str(link))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("Errno", cp.stdout + cp.stderr)
        self.assertFalse(real.exists(), "the folder the link points to is the home")
        self.assertFalse(os.path.lexists(str(link)), "and the link goes with it")
        self.assertTrue((self.tmp / "dotfiles").is_dir(), "only the home is deleted, not what holds it")

    def test_a_dry_run_leaves_no_bytecode_in_the_checkout(self):
        co = self.tmp / "checkout"
        shutil.copytree(ROOT / "plugins", co / "plugins", ignore=shutil.ignore_patterns("__pycache__", "tests"))
        shutil.copy2(INSTALL_PY, co / "install.py")
        cp = subprocess.run([sys.executable, str(co / "install.py"), "--dry-run", "--yes", "--skip-tests"], capture_output=True,
                            text=True, encoding="utf-8", env=self.env(PYTHONDONTWRITEBYTECODE=""), timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(sorted(str(p.relative_to(co)) for p in co.rglob("__pycache__")), [])

    def test_the_no_mcp_help_does_not_claim_a_windows_only_behaviour(self):
        for lang in ("en", "tr"):
            cp = self.run_py("--help", lang=lang)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertNotIn("Windows", cp.stdout.split("--no-mcp", 1)[1].split("--dry-run", 1)[0])


@NEEDS_BASH
class TestRound2InstallSh(Round2):
    def test_a_failed_required_health_check_is_not_reported_ready(self):
        self.break_the_config()
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertNotIn("Orkestra is ready.", cp.stdout)
        self.assertIn("Orkestra is not ready", cp.stdout + cp.stderr)
        tr = self.run_sh("--yes", "--skip-tests", "--no-mcp", lang="tr")
        self.assertEqual(tr.returncode, 1)
        self.assertNotIn("Orkestra hazır.", tr.stdout)

    def test_a_failing_claude_command_is_a_warning_and_a_failed_install(self):
        path = self.fake_claude(failing=("plugin install", "plugin marketplace add"))
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp", PATH=path)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertNotIn("Orkestra is ready.", cp.stdout)
        self.assertNotIn("loaded in place", cp.stdout)
        self.assertNotIn("marketplace added", cp.stdout)
        self.assertIn("plugin install failed", cp.stdout + cp.stderr)
        self.assertIn("marketplace add failed", cp.stdout + cp.stderr)

    def test_a_marketplace_from_another_checkout_is_registered_again_for_this_one(self):
        path = self.fake_claude(listing="orkestra-local\n  Source: Directory (/somewhere/else)")
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp", PATH=path)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.claude_calls()[:3], ["plugin marketplace remove orkestra-local",
                                                   "plugin marketplace add %s" % ROOT,
                                                   "plugin install orkestra@orkestra-local -s user"])
        self.assertIn("loaded in place", cp.stdout)
        path = self.fake_claude(listing="orkestra-local\n  Source: Directory (%s)" % ROOT)
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp", PATH=path)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.claude_calls()[:2], ["plugin marketplace update orkestra-local",
                                                   "plugin install orkestra@orkestra-local -s user"])

    def test_a_missing_shim_folder_is_said_not_celebrated(self):
        missing = self.tmp / "missing" / "bin"
        for lang, hint in (("en", "is not on PATH in this shell yet"), ("tr", "PATH içinde değil")):
            cp = self.run_sh("--yes", "--skip-tests", "--no-mcp", lang=lang, ORKESTRA_SHIM_DIR=str(missing))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(hint, cp.stdout + cp.stderr)
            self.assertNotIn("PATH shim (ensure_path_shim", cp.stdout)
            self.assertFalse(missing.parent.exists(), "nothing is created for it")

    def test_a_created_shim_is_reported_and_a_dry_run_stays_quiet_about_path(self):
        cp = self.run_sh("--yes", "--skip-tests", "--no-mcp")
        self.assertIn("PATH shim (ensure_path_shim", cp.stdout)
        self.assertTrue(self.shim_files())
        dry = self.run_sh("--dry-run", "--yes", "--skip-tests", "--no-mcp")
        self.assertNotIn("is not on PATH", dry.stdout)

    def test_a_dry_run_leaves_no_bytecode_in_the_checkout(self):
        co = self.tmp / "checkout"
        shutil.copytree(ROOT / "plugins", co / "plugins", ignore=shutil.ignore_patterns("__pycache__", "tests"))
        shutil.copy2(INSTALL_SH, co / "install.sh")
        cp = subprocess.run([BASH[0], str(co / "install.sh"), "--dry-run", "--yes", "--skip-tests", "--no-mcp"],
                            capture_output=True, text=True, encoding="utf-8", env=self.env(PYTHONDONTWRITEBYTECODE=""),
                            timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(sorted(str(p.relative_to(co)) for p in co.rglob("__pycache__")), [])

    def test_help_comes_in_the_installers_language(self):
        en = self.run_sh("--help")
        tr = self.run_sh("--help", lang="tr")
        for cp in (en, tr):
            self.assertEqual(cp.returncode, 0, cp.stderr)
            for flag in ("--yes", "--no-mcp", "--with-tests", "--skip-tests", "--dry-run", "--uninstall", "--purge", "--refresh-vendor", "install.py"):
                self.assertIn(flag, cp.stdout)
        self.assertIsNone(TR_LETTERS.search(en.stdout))
        self.assertRegex(tr.stdout, TR_LETTERS)
        self.assertNotIn(EM_DASH, tr.stdout)


# ---------------------------------------------------------------- install.ps1

class TestInstallPs1(unittest.TestCase):
    def setUp(self):
        self.raw = INSTALL_PS1.read_bytes()
        self.text = self.raw.decode("ascii")        # plain ASCII: Windows PowerShell 5.1 reads a BOM-less file as ANSI

    def test_it_only_forwards_to_install_py_and_keeps_the_exit_code(self):
        self.assertIn('"$Root\\install.py"', self.text)
        self.assertIn("@args", self.text)
        self.assertIn("exit $LASTEXITCODE", self.text)

    def test_it_looks_for_py_python_and_python3_and_probes_them_for_39(self):
        for needle in ('Exe = "py"; Pre = @("-3")', 'Exe = "python"', 'Exe = "python3"', "sys.version_info >= (3, 9)",
                       "Get-Command", "winget install Python.Python.3.12"):
            self.assertIn(needle, self.text)
        self.assertLess(self.text.index('Exe = "py"'), self.text.index('Exe = "python"'))
        self.assertIn('$ErrorActionPreference = "Continue"', self.text, "a failing stub must not abort the probe")

    def test_the_turkish_message_is_escaped_and_unescapes_to_real_turkish(self):
        match = re.search(r"'(Python 3\.9 veya [^']*)'", self.text)
        self.assertIsNotNone(match)
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), match.group(1))
        self.assertIn("üstü bulunamadı", decoded)
        self.assertIn("[regex]::Unescape", self.text)

    def test_no_em_dash_and_balanced_braces(self):
        self.assertNotIn(EM_DASH, self.text)
        code = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'|#[^\n]*', "", self.text)
        self.assertEqual(code.count("{"), code.count("}"))
        self.assertEqual(code.count("("), code.count(")"))


# ---------------------------------------------------------------- CI workflow

class TestWorkflow(unittest.TestCase):
    """No YAML library in the standard library: structural checks on the text, plus the indentation rules."""

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_indentation_is_two_spaces_without_tabs(self):
        for n, line in enumerate(self.text.splitlines(), 1):
            self.assertNotIn("\t", line, f"line {n}")
            if line.strip() and not line.startswith(" " * 10):          # the inline script has its own indentation
                self.assertEqual((len(line) - len(line.lstrip(" "))) % 2, 0, f"line {n}: {line!r}")

    def test_matrix_covers_three_systems_and_four_pythons_without_fail_fast(self):
        self.assertRegex(self.text, r"os: \[ubuntu-latest, macos-latest, windows-latest\]")
        body = self.text.split("\njobs:\n", 1)[1]
        unit = body.split("\n  smoke:\n", 1)[0]
        smoke = body.split("\n  smoke:\n", 1)[1].split("\n  static:\n", 1)[0]
        self.assertRegex(unit, r'python: \["3\.9", "3\.14"\]')
        self.assertEqual(re.findall(r'\{os: ([\w-]+), python: "([\d.]+)"\}', smoke),
                         [("ubuntu-latest", "3.11"), ("ubuntu-latest", "3.13"),
                          ("windows-latest", "3.9"), ("windows-latest", "3.14")])
        self.assertGreaterEqual(self.text.count("fail-fast: false"), 4)
        self.assertNotIn("fail-fast: true", self.text)

    def test_superseded_runs_are_cancelled(self):
        self.assertRegex(self.text, r"concurrency:\n  group: [^\n]+\n  cancel-in-progress: true")

    def test_the_four_jobs_and_their_checks(self):
        jobs = re.findall(r"^  (\w+):\n", self.text.split("\njobs:\n", 1)[1], re.M)
        self.assertEqual(jobs, ["unit", "smoke", "static", "installer", "openclaw"])
        for needle in ("plugins/orkestra/tests", "bridge/tests", "compileall", "feature_version=(3, 9)", "test_i18n",
                       "install.py --dry-run", "install.py --uninstall --dry-run", "install.sh --dry-run", "install.ps1 --dry-run",
                       "npm test", "bash -n install.sh", "run_hook.sh"):
            self.assertIn(needle, self.text)

    def test_every_job_has_a_timeout(self):
        self.assertEqual(self.text.count("timeout-minutes:"), 5)

    def test_actions_are_pinned_to_a_major_version(self):
        for use in re.findall(r"uses: (\S+)", self.text):
            self.assertRegex(use, r"@v\d+$", use)

    def test_the_workflow_ran_by_the_other_suites_still_matches(self):
        for needle in ("windows-latest", "ubuntu-latest", "macos-latest"):
            self.assertIn(needle, self.text)


if __name__ == "__main__":
    unittest.main()
