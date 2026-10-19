"""scripts/release.sh + scripts/release.py: one version everywhere, a dated CHANGELOG, a deterministic tar.gz with
SHA256SUMS, and a printed (never executed) `gh release create` command.

Every run happens on a throw-away git repository built in a temp directory from copies of the real version-carrying
files; the real checkout is only read. Git runs with an empty global config; no commit, tag or push ever touches the
real repository, and a fake `gh` on PATH proves the script never starts it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import helpers
from orkestra_core import __version__

ROOT = helpers.PLUGIN.parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
SH = ROOT / "scripts" / "release.sh"
EM_DASH = chr(0x2014)
NEW = "99.0.0"


def load():
    spec = importlib.util.spec_from_file_location("orkestra_release_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REL = load()


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        for rel, _ in REL.CARRIERS:
            dst = self.repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dst)
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n\n### Added\n- a thing\n\n## %s - 2026-01-01\n\nold\n" % __version__,
            encoding="utf-8")
        (self.repo / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.repo / "tool.sh").chmod(0o755)
        (self.repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.marker = self.tmp / "gh-was-run"
        gh = self.fake / "gh"
        gh.write_text("#!/bin/sh\necho ran > '%s'\n" % self.marker, encoding="utf-8")
        gh.chmod(0o755)
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")

    def env(self, lang="en"):
        env = dict(os.environ)
        env.update({"ORKESTRA_LANG": lang, "HOME": str(self.tmp / "home"), "ORKESTRA_HOME": str(self.tmp / "oh"),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": str(self.fake) + os.pathsep + env.get("PATH", "")})
        return env

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo)] + list(args), env=self.env(), check=True,
                              capture_output=True, text=True).stdout

    def release(self, *args, lang="en"):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.repo), "--date", "2026-10-20"] + list(args),
                              env=self.env(lang), capture_output=True, text=True, encoding="utf-8", timeout=120)

    def snapshot(self):
        out = {}
        for p in sorted(self.repo.rglob("*")):
            if p.is_file() and ".git" not in p.relative_to(self.repo).parts:
                out[str(p.relative_to(self.repo))] = p.read_bytes()
        return out


class TestVersionDrift(Fixture):
    def test_the_real_checkout_carries_one_version_everywhere(self):
        """Fails when any file that names the release number (pyproject, READMEs, ORKESTRA, architecture pages, issue
        forms) disagrees with orkestra_core.__version__: the number is bumped by scripts/release.sh, not by hand."""
        self.assertEqual(REL.check(str(ROOT)), __version__)
        self.assertEqual(set(REL.versions(str(ROOT)).values()), {__version__})

    def test_one_stale_file_is_named(self):
        readme = self.repo / "README.tr.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("Sürüm " + __version__, "Sürüm 0.0.1", 1),
                          encoding="utf-8")
        with self.assertRaises(REL.ReleaseError) as ctx:
            REL.check(str(self.repo))
        self.assertIn("README.tr.md", str(ctx.exception))
        cp = self.release("--check")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("README.tr.md", cp.stderr)

    def test_a_carrier_that_lost_its_version_is_an_error_not_a_pass(self):
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        cp = self.release("--check")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("pyproject.toml", cp.stderr)

    def test_check_passes_on_a_consistent_tree_in_both_languages(self):
        en = self.release("--check")
        tr = self.release("--check", lang="tr")
        self.assertEqual((en.returncode, tr.returncode), (0, 0), en.stderr + tr.stderr)
        self.assertIn("all %d files that carry the version say %s" % (len(REL.CARRIERS), __version__), en.stdout)
        self.assertIn("sürümü taşıyan", tr.stdout)

    def test_the_plugin_manifests_stay_unversioned(self):
        """The release number lives in the carriers only (test_docs_drift pins the same policy for the manifests)."""
        import json
        for rel in ("plugins/orkestra/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
            self.assertNotIn('"version"', (ROOT / rel).read_text(encoding="utf-8"), rel)
        json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))


class TestDryRunAndRefusals(Fixture):
    def test_without_apply_nothing_is_written_and_the_gh_command_is_only_printed(self):
        before = self.snapshot()
        for lang, word in (("en", "Dry run finished"), ("tr", "Deneme bitti")):
            cp = self.release(NEW, lang=lang)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn(word, cp.stdout)
            self.assertIn("gh release create v%s" % NEW, cp.stdout)
            self.assertNotIn(EM_DASH, cp.stdout + cp.stderr)
            self.assertEqual(self.snapshot(), before)
        explicit = self.release(NEW, "--dry-run")
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertFalse(self.marker.exists(), "gh must never be started by the release script")
        self.assertFalse((self.repo / "dist").exists())

    def test_a_dirty_tree_is_refused_unless_allowed(self):
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8")
        refused = self.release(NEW, "--apply")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("uncommitted changes", refused.stderr)
        self.assertIn("--allow-dirty", refused.stderr)
        self.assertEqual(REL.check(str(self.repo)), __version__, "a refusal writes nothing")
        tr = self.release(NEW, lang="tr")
        self.assertIn("commitlenmemiş", tr.stderr)
        allowed = self.release(NEW, "--apply", "--allow-dirty")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_bad_old_or_missing_versions_are_refused(self):
        for args, text in (((), "X.Y.Z"), (("1.2",), "1.2 is not a version"), (("v2.0.0",), "v2.0.0 is not a version"),
                           ((__version__,), "must be newer"), (("0.0.1",), "must be newer"),
                           ((NEW, "--apply", "--dry-run"), "exclude each other")):
            cp = self.release(*args)
            self.assertEqual(cp.returncode, 1, args)
            self.assertIn(text, cp.stderr, args)
        self.assertEqual(REL.check(str(self.repo)), __version__)

    def test_an_existing_tag_is_refused(self):
        self.git("tag", "v" + NEW)
        cp = self.release(NEW)
        self.assertEqual(cp.returncode, 1)
        self.assertIn("tag v%s already exists" % NEW, cp.stderr)

    def test_an_empty_unreleased_section_is_refused(self):
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n## 1.0.0 - 2026-01-01\n\nold\n",
                                                encoding="utf-8")
        self.git("commit", "-q", "-am", "empty")
        cp = self.release(NEW)
        self.assertEqual(cp.returncode, 1)
        self.assertIn("Unreleased", cp.stderr)

    def test_a_folder_that_is_not_a_git_repository_is_refused(self):
        shutil.rmtree(self.repo / ".git")
        cp = self.release(NEW)
        self.assertEqual(cp.returncode, 1)
        self.assertIn("not a git repository", cp.stderr)


class TestApply(Fixture):
    def apply(self):
        cp = self.release(NEW, "--apply")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp

    def test_every_carrier_gets_the_new_version(self):
        self.apply()
        self.assertEqual(REL.check(str(self.repo)), NEW)
        self.assertIn('version = "%s"' % NEW, (self.repo / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('__version__ = "%s"' % NEW,
                      (self.repo / "plugins/orkestra/lib/orkestra_core/__init__.py").read_text(encoding="utf-8"))
        self.assertIn("Version %s " % NEW, (self.repo / "README.md").read_text(encoding="utf-8"))

    def test_the_changelog_unreleased_becomes_a_dated_section_and_a_new_unreleased_opens(self):
        self.apply()
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?s)^# Changelog\n\n## Unreleased\n\n## %s - 2026-10-20\n\n### Added\n- a thing\n\n## %s"
                         % (re.escape(NEW), re.escape(__version__)))
        self.assertNotIn(EM_DASH, text.split("## " + __version__)[0])
        notes = (self.repo / "dist" / ("release-notes-%s.md" % NEW)).read_text(encoding="utf-8")
        self.assertEqual(notes, "### Added\n- a thing\n")

    def test_the_archive_and_sha256sums_match(self):
        cp = self.apply()
        dist = self.repo / "dist"
        archive = dist / ("orkestra-%s.tar.gz" % NEW)
        sums = (dist / "SHA256SUMS").read_text(encoding="utf-8")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(sums, "%s  %s\n" % (digest, archive.name))
        self.assertRegex(sums, r"^[0-9a-f]{64}  orkestra-")
        with tarfile.open(str(archive)) as tar:
            names = tar.getnames()
            self.assertIn("orkestra-%s/pyproject.toml" % NEW, names)
            self.assertIn("orkestra-%s/CHANGELOG.md" % NEW, names)
            self.assertFalse([n for n in names if "/dist/" in n or "/.git/" in n], names)
            member = tar.getmember("orkestra-%s/tool.sh" % NEW)
            self.assertTrue(member.mode & stat.S_IXUSR)
            self.assertEqual((member.mtime, member.uid, member.gid), (0, 0, 0))
            body = tar.extractfile("orkestra-%s/pyproject.toml" % NEW).read().decode("utf-8")
            self.assertIn('version = "%s"' % NEW, body, "the archive holds the bumped files")
        self.assertIn("gh release create v%s" % NEW, cp.stdout)
        self.assertIn("--draft", cp.stdout)
        self.assertFalse(self.marker.exists(), "gh must never be started by the release script")
        self.assertNotIn("md5", sums.lower())

    def test_the_archive_is_reproducible(self):
        self.apply()
        first = (self.repo / "dist" / "SHA256SUMS").read_text(encoding="utf-8")
        REL.build_archive(str(self.repo), NEW, str(self.repo / "dist"))
        self.assertEqual((self.repo / "dist" / "SHA256SUMS").read_text(encoding="utf-8"), first)

    def test_the_output_folder_can_be_chosen(self):
        out = self.tmp / "elsewhere"
        cp = self.release(NEW, "--apply", "--out", str(out))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue((out / ("orkestra-%s.tar.gz" % NEW)).is_file())
        self.assertFalse((self.repo / "dist").exists())

    def test_the_turkish_run_speaks_turkish_without_an_em_dash(self):
        cp = self.release(NEW, "--apply", lang="tr")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("sürüm değiştirildi", cp.stdout)
        self.assertIn("kendiniz yayınlayın", cp.stdout)
        self.assertNotIn(EM_DASH, cp.stdout)

    def test_the_slug_comes_from_the_origin_remote_only_when_there_is_one(self):
        self.assertNotIn("--repo", self.release(NEW).stdout)
        self.git("remote", "add", "origin", "https://github.com/example-org/example-repo.git")
        self.assertIn("--repo example-org/example-repo", self.release(NEW).stdout)


@unittest.skipIf(os.name == "nt", "release.sh is a POSIX script")
class TestShellWrapper(unittest.TestCase):
    def test_release_sh_runs_the_python_helper(self):
        env = dict(os.environ, ORKESTRA_LANG="en")
        cp = subprocess.run(["sh", str(SH), "--check"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn(__version__, cp.stdout)

    def test_the_script_never_contains_a_publishing_command_it_could_run(self):
        for path in (SH, SCRIPT):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("twine", "npm publish", "brew ", "git push", "git tag", "git commit"):
                self.assertNotIn(forbidden, text, f"{path.name}: {forbidden}")
        self.assertNotIn("subprocess.run([\"gh\"", SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
