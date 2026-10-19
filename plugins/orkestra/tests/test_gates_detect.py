"""Gate detection per ecosystem: each row is a project layout and the gate commands it must produce."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from orkestra_core import gates as G


def _w(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# (label, files {path: text}, expected {gate name: command})
ROWS = [
    ("pnpm workspace without root scripts",
     {"pnpm-workspace.yaml": "packages:\n  - apps/*\n", "package.json": json.dumps({"name": "r"}),
      "pnpm-lock.yaml": "", "apps/web/package.json": json.dumps({"scripts": {"test": "vitest"}}),
      "apps/web/pnpm-lock.yaml": ""},
     {"web:test": "pnpm run test"}),
    ("npm workspaces without root scripts",
     {"package.json": json.dumps({"workspaces": ["packages/*"]}),
      "packages/a/package.json": json.dumps({"scripts": {"test": "jest"}})},
     {"a:test": "npm test"}),
    ("packageManager without a lockfile",
     {"package.json": json.dumps({"packageManager": "pnpm@9.1.0", "scripts": {"test": "vitest"}})},
     {"test": "pnpm run test"}),
    ("gradle with settings only and no wrapper",
     {"settings.gradle": "rootProject.name='x'"},
     {"gradle-test": "gradle test --quiet"}),
    ("gradle with a wrapper keeps ./gradlew",
     {"build.gradle": "", "gradlew": ""},
     {"gradle-test": "./gradlew test --quiet"}),
    ("gradle build without wrapper",
     {"build.gradle.kts": ""},
     {"gradle-test": "gradle test --quiet"}),
    ("android folder only",
     {"android/settings.gradle": "", "android/gradlew": ""},
     {"android:gradle-test": "./gradlew test --quiet"}),
    ("python root test files, unittest",
     {"test_a.py": "import unittest\nclass T(unittest.TestCase): pass\n"},
     {"unittest": "python3 -m unittest discover -s . -q"}),
    ("python src tests with pytest dependency",
     {"requirements.txt": "pytest\n", "src/pkg/tests/test_a.py": "def test_a(): pass\n"},
     {"pytest": "python3 -m pytest -q"}),
    ("python unittest in tests dir, pytest absent",
     {"requirements.txt": "requests\n", "tests/test_a.py": "import unittest\n"},
     {"unittest": "python3 -m unittest discover -s tests -q"}),
    ("python tests dir, framework unknown keeps pytest",
     {"requirements.txt": "requests\n", "tests/test_a.py": "def test_a(): pass\n"},
     {"pytest": "python3 -m pytest -q"}),
    ("python .venv interpreter",
     {"requirements.txt": "pytest\n", "tests/test_a.py": "", ".venv/bin/python": ""},
     {"pytest": ".venv/bin/python -m pytest -q"}),
    ("python venv interpreter",
     {"requirements.txt": "pytest\n", "tests/test_a.py": "", "venv/bin/python": ""},
     {"pytest": "venv/bin/python -m pytest -q"}),
    ("ruby rspec", {"Gemfile": "", "spec/a_spec.rb": ""}, {"rspec": "bundle exec rspec"}),
    ("ruby rake", {"Gemfile": "", "Rakefile": ""}, {"rake-test": "bundle exec rake test"}),
    ("elixir", {"mix.exs": ""}, {"mix-test": "mix test"}),
    ("deno", {"deno.json": "{}"}, {"deno-test": "deno test"}),
    ("cmake with tests", {"CMakeLists.txt": "enable_testing()\nadd_test(a a)\n"},
     {"ctest": "cmake -S . -B build && cmake --build build && ctest --test-dir build --output-on-failure"}),
    ("make check", {"Makefile": "check:\n\t./run\n"}, {"make-check": "make check"}),
    ("sln one level down", {"src/App.sln": ""},
     {"dotnet-build": "dotnet build src/App.sln --nologo -v q", "dotnet-test": "dotnet test src/App.sln --nologo -v q"}),
    ("csproj one level down", {"App/App.csproj": ""},
     {"dotnet-build": "dotnet build App/App.csproj --nologo -v q"}),
]

# Rows that existed before this widening: they must not change.
KEEP = [
    ({"package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}})},
     {"lint": "npm run lint", "test": "npm test"}),
    ({"Cargo.toml": ""}, {"cargo-build": "cargo build --quiet", "cargo-test": "cargo test --quiet"}),
    ({"go.mod": ""}, {"go-build": "go build ./...", "go-vet": "go vet ./...", "go-test": "go test ./..."}),
    ({"pom.xml": "", "mvnw": ""}, {"maven-test": "./mvnw -q test"}),
    ({"pom.xml": ""}, {"maven-test": "mvn -q test"}),
    ({"composer.json": json.dumps({"scripts": {"test": "phpunit"}})}, {"composer-test": "composer test"}),
    ({"Makefile": "test:\n\ttrue\n"}, {"make-test": "make test"}),
    ({"pubspec.yaml": "dependencies:\n  flutter:\n    sdk: flutter\n", "test/a_test.dart": ""},
     {"flutter-analyze": "flutter analyze", "flutter-test": "flutter test"}),
    ({"App.sln": ""}, {"dotnet-build": "dotnet build --nologo -v q", "dotnet-test": "dotnet test --nologo -v q"}),
]


class TestDetectRows(unittest.TestCase):
    def detect(self, files):
        td = Path(tempfile.mkdtemp(prefix="orkestra-det-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        for rel, text in files.items():
            _w(td, rel, text)
        return {g["name"]: g["cmd"] for g in G.detect(td)}

    def test_each_layout_yields_its_gates(self):
        for label, files, expected in ROWS:
            got = self.detect(files)
            for name, cmd in expected.items():
                self.assertEqual(got.get(name), cmd, f"{label}: {got}")

    def test_existing_rows_are_unchanged(self):
        for files, expected in KEEP:
            got = self.detect(files)
            for name, cmd in expected.items():
                self.assertEqual(got.get(name), cmd, f"{files}: {got}")

    def test_pytest_style_tests_with_unittest_mock_stay_pytest(self):
        got = self.detect({"requirements.txt": "", "tests/test_a.py":
                           "from unittest.mock import patch\nimport unittest.mock\n\ndef test_a():\n    pass\n"})
        self.assertEqual(got, {"pytest": "python3 -m pytest -q"})
        got = self.detect({"requirements.txt": "", "tests/test_a.py":
                           "from unittest import mock\n\ndef test_a():\n    pass\n"})
        self.assertEqual(got, {"pytest": "python3 -m pytest -q"})

    def test_stray_root_python_test_in_a_js_repo_adds_no_python_gate(self):
        got = self.detect({"package.json": json.dumps({"scripts": {"test": "jest"}}),
                           "test_scratch.py": "import unittest\nclass T(unittest.TestCase): pass\n"})
        self.assertEqual(got, {"test": "npm test"})

    def test_sln_plus_test_project_yields_no_overlapping_dotnet_gates(self):
        got = self.detect({"src/App.sln": "", "tests/X/X.Tests.csproj": ""})
        self.assertEqual(got, {"dotnet-test": "dotnet test tests/X --nologo -v q"})

    def test_nothing_detected_for_an_empty_folder(self):
        self.assertEqual(self.detect({}), {})


if __name__ == "__main__":
    unittest.main()
