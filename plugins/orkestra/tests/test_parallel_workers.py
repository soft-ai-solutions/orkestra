"""Disjoint tasks start on a background CLI. Overlap, a live pid, and a missing binary do not."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from unittest import mock

import helpers
from orkestra_core import engines as EN
from orkestra_core.parallel_workers import launch_ready, worker_prompt
from orkestra_core.runs import start_run
from orkestra_core.state import load_state, save_state


class TestParallelWorkers(helpers.Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "argv.log"
        script = "\n".join([
            "#!" + sys.executable,
            "import json, os, sys, time",
            f"fh = open({str(self.log)!r}, 'a', encoding='utf-8'); fh.write(json.dumps({{'argv': sys.argv, 'parallel': os.environ.get('ORKESTRA_PARALLEL_CLI')}}) + '\\n'); fh.flush()",
            "time.sleep(30)",
            "",
        ])
        exe = self.bin / "claude"
        exe.write_text(script, encoding="utf-8")
        exe.chmod(0o755)
        self._path = os.environ.get("PATH")
        os.environ["PATH"] = str(self.bin) + os.pathsep + (self._path or "")
        os.environ.pop("ORKESTRA_CLAUDE_BIN", None)
        os.environ["ORKESTRA_PARALLEL_CLI"] = "1"
        self.pids: list[int] = []

    def tearDown(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if self._path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._path
        super().tearDown()

    def _run(self, tasks: dict, *, mode: str = "autopilot", kind: str = "software"):
        project = self.make_project("demo")
        st = start_run(project, "demo", "sub fonksiyonu ekle", "M", "interactive", "claude", mode)
        st["phase"] = "implement"
        st["work_kind"] = kind
        st["tasks"] = tasks
        save_state(project, st)
        return project, st

    def test_a_disjoint_task_starts_once_and_an_overlap_does_not(self) -> None:
        tasks = {
            "T001": {"status": "pending", "files": ["a.py"], "title": "a", "tdd": "required", "depends": []},
            "T002": {"status": "pending", "files": ["b.py"], "title": "b", "tdd": "required", "depends": []},
            "T003": {"status": "pending", "files": ["a.py"], "title": "c", "tdd": "required", "depends": []},
        }
        project, st = self._run(tasks)
        lines = launch_ready(project, st)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("started T002", lines[0])
        self.assertNotIn("T003", lines[0])
        self.assertEqual(load_state(project, st["run_id"])["tasks"]["T002"]["status"], "in_progress")
        self.assertEqual(load_state(project, st["run_id"])["tasks"]["T001"]["status"], "pending")
        record = json.loads((project / ".orkestra" / "runs" / st["run_id"] / "parallel.json").read_text())
        self.pids.append(int(record["T002"]["pid"]))
        again = launch_ready(project, load_state(project, st["run_id"]))
        self.assertEqual(again, [])
        for _ in range(40):
            if self.log.is_file():
                break
            time.sleep(0.05)
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parallel"], "0")

    def test_manual_mode_and_a_missing_binary_start_nothing(self) -> None:
        tasks = {
            "T001": {"status": "pending", "files": ["a.py"], "title": "a", "tdd": "required", "depends": []},
            "T002": {"status": "pending", "files": ["b.py"], "title": "b", "tdd": "required", "depends": []},
        }
        project, st = self._run(tasks, mode="manual")
        self.assertEqual(launch_ready(project, st), [])
        st["approval_mode"] = "autopilot"
        save_state(project, st)
        os.environ["ORKESTRA_CLAUDE_BIN"] = str(self.tmp / "missing-claude")
        os.environ["PATH"] = str(self.tmp / "empty-bin")
        self.assertEqual(launch_ready(project, load_state(project, st["run_id"])), [])

    def test_the_prompt_matches_the_run_kind(self) -> None:
        code = worker_prompt({"work_kind": "software", "tasks": {"T002": {"files": ["b.py"]}}}, "T002", "demo")
        self.assertIn("failing test first", code)
        self.assertIn("b.py", code)
        prose = worker_prompt({"work_kind": "writing", "tasks": {"T002": {"files": []}}}, "T002", "demo")
        self.assertIn("content check", prose)
        self.assertNotIn("failing test first", prose)

    def test_copilot_workers_get_the_runs_profile(self) -> None:
        (self.bin / "copilot").write_text((self.bin / "claude").read_text(encoding="utf-8"), encoding="utf-8")
        (self.bin / "copilot").chmod(0o755)
        tasks = {
            "T001": {"status": "pending", "files": ["a.py"], "title": "a", "tdd": "required", "depends": []},
            "T002": {"status": "pending", "files": ["b.py"], "title": "b", "tdd": "required", "depends": []},
        }
        project, st = self._run(tasks)
        st["engine"] = "copilot"
        st["copilot_profile"] = {"model": "gpt-6-sol", "reasoning_effort": "high", "context": "default",
                                 "allow_all": False}
        save_state(project, st)
        with mock.patch.object(EN, "copilot_cli_has", return_value=True):
            lines = launch_ready(project, load_state(project, st["run_id"]))
        self.assertEqual(len(lines), 1, lines)
        for _ in range(40):
            if self.log.is_file():
                break
            time.sleep(0.05)
        argv = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6-sol")
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")
        self.assertEqual(argv[argv.index("--context") + 1], "default")
        self.assertIn("--allow-all-tools", argv)
        self.assertNotIn("--allow-all-paths", argv)
