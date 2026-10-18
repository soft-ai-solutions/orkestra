"""FR-003, E4, SC-003: project_active_run must not treat a done or aborted index id as open."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import paths, state, util
from orkestra_core.runs import resume_run, set_status, start_run
from orkestra_core.state import OPEN_STATUSES, OrkestraError


class TestActiveIndex(helpers.Sandbox):
    def _put(self, project: Path, run_id: str, status: str, updated: str = "2020-01-01T00:00:00Z") -> None:
        st = state.new_state(project, run_id, run_id, "S", "delivery")
        st["status"] = status
        st["updated"] = updated
        util.write_json_atomic(state.state_path(project, run_id), st)

    def _put_raw(self, project: Path, run_id: str, body: str) -> None:
        path = state.state_path(project, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _watched_call(self, project: Path):
        """Run project_active_run while recording file_lock paths and set_active_run calls."""
        locks = []
        calls = []
        real = state.file_lock

        @contextlib.contextmanager
        def spy(path, *args, **kwargs):
            locks.append(Path(path))
            with real(path, *args, **kwargs):
                yield

        def record(*args, **kwargs):
            calls.append(args)

        with mock.patch.object(state, "file_lock", spy), mock.patch.object(state, "set_active_run", record):
            got = state.project_active_run(project)
        return got, locks, calls

    def test_terminal_index_is_dropped_and_the_open_scan_wins(self) -> None:
        """FR-003, E4, SC-003: done/aborted is not returned; the entry is removed under one active-runs lock."""
        for status in ("done", "aborted"):
            with self.subTest(status=status):
                project = self.make_project("term-" + status)
                key = str(Path(project))
                self._put(project, "001-term", status, "2099-01-01T00:00:00Z")
                self._put(project, "002-old", "paused", "2020-01-01T00:00:00Z")
                self._put(project, "003-new", "active", "2021-06-01T00:00:00Z")
                state.set_active_run(project, "001-term")
                got, locks, calls = self._watched_call(project)
                self.assertEqual(got, "003-new")
                self.assertEqual(calls, [], "set_active_run must not run while the active-runs lock is held")
                self.assertEqual(locks, [paths.active_runs_path()])
                self.assertNotIn(key, state.active_runs())

    def test_terminal_index_is_dropped_when_no_run_is_open(self) -> None:
        """FR-003, E4: with only a terminal run, the id is not returned and the index entry is removed."""
        for status in ("done", "aborted"):
            with self.subTest(status=status):
                project = self.make_project("alone-" + status)
                key = str(Path(project))
                self._put(project, "001-term", status, "2099-01-01T00:00:00Z")
                state.set_active_run(project, "001-term")
                got, locks, calls = self._watched_call(project)
                self.assertIsNone(got)
                self.assertEqual(calls, [])
                self.assertEqual(locks, [paths.active_runs_path()])
                self.assertNotIn(key, state.active_runs())

    def test_newer_open_id_under_the_lock_is_not_deleted(self) -> None:
        """FR-003, E4: a different id written while the lock is held stays in the index."""
        project = self.make_project("race")
        key = str(Path(project))
        self._put(project, "001-done", "done", "2099-01-01T00:00:00Z")
        self._put(project, "002-live", "active", "2021-01-01T00:00:00Z")
        state.set_active_run(project, "001-done")
        real = state.file_lock
        depth = {"n": 0}

        @contextlib.contextmanager
        def spy(path, *args, **kwargs):
            depth["n"] += 1
            try:
                if depth["n"] > 1:
                    raise AssertionError("file_lock does not nest; set_active_run must not be called under it")
                with real(path, *args, **kwargs):
                    if Path(path) == paths.active_runs_path():
                        util.write_json_atomic(paths.active_runs_path(), {key: "002-live"})
                    yield
            finally:
                depth["n"] -= 1

        with mock.patch.object(state, "file_lock", spy):
            got = state.project_active_run(project)
        self.assertEqual(got, "002-live")
        self.assertEqual(state.active_runs().get(key), "002-live")

    def test_non_dict_state_keeps_the_indexed_id(self) -> None:
        """FR-003, E4: read_json yields None, a list, or a string; not-a-dict keeps the id and the index."""
        project = self.make_project("broken")
        key = str(Path(project))
        cases = (
            ("invalid", "{", None),
            ("list", "[1]", [1]),
            ("string", '"done"', "done"),
        )
        for label, body, parsed in cases:
            with self.subTest(label=label):
                rid = "001-" + label
                self._put_raw(project, rid, body)
                self._put(project, "009-open", "active", "2099-01-01T00:00:00Z")
                state.set_active_run(project, rid)
                self.assertEqual(util.read_json(state.state_path(project, rid)), parsed)
                self.assertEqual(state.project_active_run(project), rid)
                self.assertEqual(state.active_runs().get(key), rid)

    def test_open_status_returns_the_index_and_skips_the_scan(self) -> None:
        """Other statuses keep today's early return, even when a newer open run is on disk."""
        for status in OPEN_STATUSES:
            with self.subTest(status=status):
                project = self.make_project("open-" + status)
                key = str(Path(project))
                self._put(project, "001-indexed", status, "2020-01-01T00:00:00Z")
                self._put(project, "002-newer", "active", "2099-01-01T00:00:00Z")
                state.set_active_run(project, "001-indexed")
                self.assertEqual(state.project_active_run(project), "001-indexed")
                self.assertEqual(state.active_runs().get(key), "001-indexed")

    def test_missing_state_file_still_scans_and_leaves_the_index(self) -> None:
        project = self.make_project("missing")
        key = str(Path(project))
        self._put(project, "002-live", "active", "2021-01-01T00:00:00Z")
        state.set_active_run(project, "001-missing")
        self.assertFalse(state.state_path(project, "001-missing").exists())
        self.assertEqual(state.project_active_run(project), "002-live")
        self.assertEqual(state.active_runs().get(key), "001-missing")

    def test_resume_and_set_status_still_refuse_a_terminal_run(self) -> None:
        """FR-003, E4, SC-003: resume_run and set_status still refuse; runs.py is not part of this change."""
        project = self.make_project("closed")
        for status in ("done", "aborted"):
            rid = "001-" + status
            self._put(project, rid, status)
            with self.subTest(status=status, call="resume"):
                with self.assertRaises(OrkestraError) as cm:
                    resume_run(project, rid)
                self.assertIn(f"is {status}", str(cm.exception))
            with self.subTest(status=status, call="set_status"):
                with self.assertRaises(OrkestraError) as cm:
                    set_status(project, rid, "active", "reopen")
                self.assertIn(f"is {status}", str(cm.exception))
            self.assertEqual(util.read_json(state.state_path(project, rid))["status"], status)

    def test_start_does_not_treat_a_terminal_index_as_open(self) -> None:
        """FR-003, SC-003: start_run already asks project_active_run, so a done index must not block it."""
        project = self.make_project("restart")
        self._put(project, "001-done", "done")
        state.set_active_run(project, "001-done")
        try:
            started = start_run(project, "next", "add a line", "S", "delivery")
        except OrkestraError as exc:
            self.fail(f"a done index still counts as an open run: {exc}")
        self.assertNotEqual(started["run_id"], "001-done")
        self.assertEqual(started["status"], "active")

    def test_open_scan_skips_null_updated_and_missing_run_id(self) -> None:
        """A terminal index falls through to the scan. A sibling with updated null or no run_id must not raise."""
        project = self.make_project("hostile")
        self._put(project, "001-term", "done", "2099-01-01T00:00:00Z")
        self._put(project, "002-live", "active", "2021-01-01T00:00:00+03:00")
        self._put_raw(project, "003-bad", json.dumps(
            {"status": "active", "updated": None, "run_id": "003-bad"}))
        self._put_raw(project, "004-norun", json.dumps(
            {"status": "active", "updated": "2024-01-01T00:00:00+03:00"}))
        state.set_active_run(project, "001-term")
        self.assertEqual(state.project_active_run(project), "002-live")
