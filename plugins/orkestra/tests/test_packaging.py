"""pyproject.toml + packaging/orkestra_cli: `pipx install git+<repo>` / `uvx --from git+<repo> orkestra` / `pip install .`.

The wheel ships the plugin tree as the data package `orkestra_plugin` (same layout as plugins/orkestra, minus tests) next
to the entry package `orkestra_cli`. The default tests prove the entry point on a copy of exactly that layout in a temp
directory (no network, no build backend needed); the real build + install into a temp venv runs when
ORKESTRA_TEST_WHEEL=1 (it downloads the build backend, setuptools, from PyPI).
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from orkestra_core import __version__

ROOT = helpers.PLUGIN.parent.parent
PYPROJECT = ROOT / "pyproject.toml"
ENTRY = ROOT / "packaging" / "orkestra_cli"


def sandbox_env(tmp: Path, **extra):
    env = dict(os.environ)
    env.update({"HOME": str(tmp / "home"), "USERPROFILE": str(tmp / "home"), "ORKESTRA_HOME": str(tmp / "oh"),
                "COPILOT_HOME": str(tmp / "cop"), "ORKESTRA_LANG": "en", "ORKESTRA_NO_DAEMON": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.pop("PYTHONPATH", None)
    env.update(extra)
    (tmp / "home").mkdir(exist_ok=True)
    return env


class TestPyproject(unittest.TestCase):
    def setUp(self):
        self.text = PYPROJECT.read_text(encoding="utf-8")

    def test_it_is_stdlib_only_python_39_mit(self):
        self.assertRegex(self.text, r"(?m)^dependencies = \[\]$")
        self.assertRegex(self.text, r'(?m)^requires-python = ">=3\.9"$')
        self.assertRegex(self.text, r'(?m)^license = \{ text = "MIT" \}$')
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text(encoding="utf-8")[:200])

    def test_the_version_is_the_one_in_the_code(self):
        self.assertRegex(self.text, r'(?m)^version = "%s"$' % re.escape(__version__))

    def test_the_console_script_points_at_an_importable_entry(self):
        self.assertRegex(self.text, r'(?m)^orkestra = "orkestra_cli:main"$')
        sys.path.insert(0, str(ROOT / "packaging"))
        try:
            import orkestra_cli
            self.assertTrue(callable(orkestra_cli.main))
            self.assertEqual(Path(orkestra_cli.plugin_root()).resolve(), helpers.PLUGIN.resolve(),
                             "from a checkout the entry uses plugins/orkestra")
        finally:
            sys.path.remove(str(ROOT / "packaging"))
            sys.modules.pop("orkestra_cli", None)

    def test_the_plugin_tree_ships_without_its_tests(self):
        self.assertIn('orkestra_plugin = "plugins/orkestra"', self.text)
        self.assertIn('orkestra_plugin = ["*", "**/*"]', self.text)
        self.assertRegex(self.text, r'exclude-package-data\]\s+orkestra_plugin = \[[^\]]*"tests/\*\*/\*"')
        for needed in ("hooks/hooks.json", "agents", "skills", "templates", "vendor", "bin/orkestra",
                       "lib/orkestra_core/cli.py", ".claude-plugin/plugin.json"):
            self.assertTrue((helpers.PLUGIN / needed).exists(), needed)

    def test_the_entry_package_is_stdlib_only(self):
        for path in ENTRY.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for name in names:
                    top = (name or "").split(".")[0]
                    self.assertIn(top, ("os", "sys", "orkestra_core"), f"{path.name} imports {name}")


class TestInstalledLayout(unittest.TestCase):
    """A copy of the wheel's layout: <site>/orkestra_cli and <site>/orkestra_plugin (no tests, no checkout nearby)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.site = cls.tmp / "site"
        cls.site.mkdir()
        shutil.copytree(ENTRY, cls.site / "orkestra_cli", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(helpers.PLUGIN, cls.site / "orkestra_plugin",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "orkestra_cli"] + list(args), cwd=str(self.site),
                              env=sandbox_env(self.tmp), capture_output=True, text=True, encoding="utf-8", timeout=300)

    def test_version_comes_from_the_installed_tree(self):
        cp = self.run_cli("--version")
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "orkestra " + __version__), cp.stderr)

    def test_doctor_finds_hooks_agents_and_skills_in_the_installed_location(self):
        cp = self.run_cli("doctor")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        plugin = str(self.site / "orkestra_plugin")
        self.assertRegex(cp.stdout, r"\[OK\] plugin root .*" + re.escape(os.path.realpath(plugin)))
        self.assertIn("[OK] hook self-test", cp.stdout)
        for folder in ("agents", "skills", "templates", "vendor", "hooks", "cursor"):
            self.assertTrue((self.site / "orkestra_plugin" / folder).is_dir(), folder)
        self.assertFalse((self.site / "orkestra_plugin" / "tests").exists())

    def test_a_playbook_is_read_from_the_installed_tree(self):
        cp = self.run_cli("playbook", "security")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        expected = (self.site / "orkestra_plugin" / "skills" / "run" / "references" / "security.md")
        self.assertTrue(expected.is_file())
        self.assertIn(expected.read_text(encoding="utf-8").strip().splitlines()[0], cp.stdout)


@unittest.skipUnless(os.environ.get("ORKESTRA_TEST_WHEEL") == "1", "set ORKESTRA_TEST_WHEEL=1 (downloads setuptools)")
class TestRealInstall(unittest.TestCase):
    def test_pip_install_into_a_temp_venv_then_run_the_console_script(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            src = tmp / "src"
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "dist", "node_modules", "__pycache__",
                                                                     "build", "*.egg-info", "internal", ".orkestra"))
            env = sandbox_env(tmp)
            subprocess.run([sys.executable, "-m", "venv", str(tmp / "v")], check=True, env=env)
            bindir = tmp / "v" / ("Scripts" if os.name == "nt" else "bin")
            subprocess.run([str(bindir / "python"), "-m", "pip", "install", "-q", str(src)], check=True, env=env,
                           timeout=600)
            exe = str(bindir / ("orkestra.exe" if os.name == "nt" else "orkestra"))
            version = subprocess.run([exe, "--version"], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(version.stdout.strip(), "orkestra " + __version__, version.stderr)
            doctor = subprocess.run([exe, "doctor"], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
            self.assertIn("orkestra_plugin", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
