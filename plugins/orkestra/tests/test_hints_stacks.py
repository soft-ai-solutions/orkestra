"""hints.playbooks_for: Python, Go and JVM diffs load their own checklist, and each checklist exists."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers
from orkestra_core import hints

REFS = helpers.PLUGIN / "skills" / "run" / "references"


class StackHints(unittest.TestCase):
    def test_extensions_map_to_stacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.assertEqual(hints.playbooks_for(["lib/util.py"], project), ["stack-python"])
            self.assertEqual(hints.playbooks_for(["cmd/main.go"], project), ["stack-go"])
            for name in ("A.java", "B.kt", "build.gradle.kts", "C.scala"):
                self.assertEqual(hints.playbooks_for(["src/" + name], project), ["stack-jvm"], name)
            self.assertEqual(hints.playbooks_for(["a.py", "b.go", "c.ts"], project), ["stack-web", "stack-python", "stack-go"])
            self.assertEqual(hints.playbooks_for(["README.md", "notes.txt"], project), [])

    def test_every_hinted_playbook_exists_and_is_short(self):
        for name in hints.ORDER:
            path = REFS / f"{name}.md"
            self.assertTrue(path.is_file(), name)
            if name.startswith("stack-"):
                self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 41, name)


if __name__ == "__main__":
    unittest.main()
