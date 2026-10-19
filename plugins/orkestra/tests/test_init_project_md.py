"""`orkestra init` fills PROJECT.md deterministically from manifests and the tree; user text is never overwritten."""
import json
import tempfile
import unittest
from pathlib import Path

import helpers
from orkestra_core import memory as M


class Observed(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp(prefix="orkestra-pmd-")).resolve()
        (self.proj / ".orkestra" / "memory").mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.proj, ignore_errors=True)

    def text(self):
        M.observe(self.proj)
        return (self.proj / ".orkestra" / "memory" / "PROJECT.md").read_text(encoding="utf-8")

    def test_package_json_name_description_and_readme_title(self):
        (self.proj / "package.json").write_text(json.dumps({"name": "shop-api", "description": "Orders for the shop"}))
        (self.proj / "README.md").write_text("# Shop API\n\nSome words.\n")
        (self.proj / "src").mkdir()
        text = self.text()
        self.assertIn("- Name: shop-api (`package.json`)", text)
        self.assertIn("- Title: Shop API (`README.md`)", text)
        self.assertIn("- Purpose: Orders for the shop (`package.json`)", text)
        self.assertIn("- `src`", text)
        self.assertEqual(M.check(self.proj), [])

    def test_pyproject_cargo_and_go_mod(self):
        (self.proj / "pyproject.toml").write_text('[build-system]\nname = "no"\n[project]\nname = "pytool"\n'
                                                  'description = "A tool"  # c\n')
        text = self.text()
        self.assertIn("- Name: pytool (`pyproject.toml`)", text)
        self.assertIn("- Purpose: A tool (`pyproject.toml`)", text)
        (self.proj / "pyproject.toml").unlink()
        (self.proj / "Cargo.toml").write_text('[package]\nname = "crate-x"\ndescription = "Crate"\n')
        (self.proj / ".orkestra" / "memory" / "PROJECT.md").unlink()
        self.assertIn("- Name: crate-x (`Cargo.toml`)", self.text())
        (self.proj / "Cargo.toml").unlink()
        (self.proj / "go.mod").write_text("module example.com/svc\n\ngo 1.22\n")
        (self.proj / ".orkestra" / "memory" / "PROJECT.md").unlink()
        text = self.text()
        self.assertIn("- Name: example.com/svc (`go.mod`)", text)
        self.assertEqual(M.check(self.proj), [])

    def test_user_written_project_md_is_kept(self):
        dest = self.proj / ".orkestra" / "memory" / "PROJECT.md"
        dest.write_text("# Mine\n\n## Summary\n- my own words\n")
        (self.proj / "package.json").write_text(json.dumps({"name": "x", "description": "y"}))
        self.assertFalse(M.observe(self.proj))
        self.assertEqual(dest.read_text(), "# Mine\n\n## Summary\n- my own words\n")

    def test_no_manifest_adds_no_name_line(self):
        self.assertNotIn("- Name:", self.text())


class InitCommand(helpers.Sandbox):
    def test_init_fills_it_and_says_so_in_both_languages(self):
        p = self.make_project("demo")
        (p / "package.json").write_text(json.dumps({"name": "demo-pkg", "description": "A demo"}))
        for lang, word in (("en", "PROJECT.md was filled"), ("tr", "PROJECT.md bu klasördeki")):
            with self.subTest(lang=lang):
                md = p / ".orkestra" / "memory" / "PROJECT.md"
                if md.exists():
                    md.unlink()
                out = self.cli("init", "-p", "demo", env={"ORKESTRA_LANG": lang}).stdout
                self.assertIn(word, out)
                self.assertIn("- Name: demo-pkg (`package.json`)", md.read_text(encoding="utf-8"))
        again = self.cli("init", "-p", "demo", env={"ORKESTRA_LANG": "en"}).stdout
        self.assertNotIn("PROJECT.md was filled", again)

    def test_context_hint_names_init(self):
        p = self.make_project("demo")
        out = self.cli("context", "-p", "demo").stdout
        self.assertIn("NOT INITIALIZED", out)
        self.assertIn("`orkestra init`", out)
        self.assertNotIn("playbook onboard", out)


if __name__ == "__main__":
    unittest.main()
