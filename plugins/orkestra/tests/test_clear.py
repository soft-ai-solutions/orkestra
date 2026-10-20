"""`orkestra clear`: stale open runs are aborted, recent / live ones and closed history are left alone."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import clear as CL
from orkestra_core import paths, state, util

HOUR = 3600


class TestClear(helpers.Sandbox):
    def _put(self, project: Path, rid: str, status: str, age_h: float) -> Path:
        st = state.new_state(project, rid, rid, "S", "delivery")
        st["status"] = status
        if status == "blocked":
            st["blocked"] = {"reason": "stuck", "at": "2020-01-01T00:00:00Z"}
        util.write_json_atomic(state.state_path(project, rid), st)
        self._age(project, rid, age_h)
        return state.state_path(project, rid).parent

    def _age(self, project: Path, rid: str, age_h: float) -> None:
        rdir = state.state_path(project, rid).parent
        t = time.time() - age_h * HOUR
        for f in rdir.glob("*"):
            os.utime(f, (t, t))

    def _status(self, project: Path, rid: str) -> str:
        return util.read_json(state.state_path(project, rid))["status"]

    def test_stale_open_runs_are_aborted_of_any_open_status(self) -> None:
        p = self.make_project("a")
        for rid, status in (("001-b", "blocked"), ("002-p", "paused"), ("003-w", "awaiting_user"), ("004-x", "active")):
            self._put(p, rid, status, 30)
        res = CL.clear(24)
        self.assertEqual(len(res["closed"]), 4)
        for rid in ("001-b", "002-p", "003-w", "004-x"):
            self.assertEqual(self._status(p, rid), "aborted")
        self.assertTrue((state.state_path(p, "001-b")).exists(), "run history stays on disk")

    def test_recent_and_closed_runs_are_untouched(self) -> None:
        p = self.make_project("a")
        self._put(p, "001-fresh", "blocked", 2)
        self._put(p, "002-done", "done", 100)
        res = CL.clear(24)
        self.assertEqual(res["closed"], [])
        self.assertEqual(self._status(p, "001-fresh"), "blocked")
        self.assertEqual(self._status(p, "002-done"), "done")

    def test_fresh_activity_feed_keeps_an_old_state_file(self) -> None:
        p = self.make_project("a")
        rdir = self._put(p, "001-busy", "blocked", 40)
        (rdir / "activity.jsonl").write_text("{}\n")
        self.assertEqual(CL.clear(24)["closed"], [])

    def test_live_agent_process_keeps_the_run(self) -> None:
        p = self.make_project("a")
        self._put(p, "001-live", "blocked", 40)
        sdir = paths.sessions_dir()
        sdir.mkdir(parents=True)
        s = sdir / "sess1.json"
        util.write_json_atomic(s, {"project": str(p), "run_id": "001-live", "bound_at": "x"})
        os.utime(s, (time.time() - 40 * HOUR,) * 2)
        with mock.patch("orkestra_core.watchdog._process_alive", return_value=True):
            res = CL.clear(24)
        self.assertEqual(res["closed"], [])
        self.assertEqual(res["kept"][0]["why"], "alive")
        self.assertEqual(self._status(p, "001-live"), "blocked")

    def test_dry_run_changes_nothing(self) -> None:
        p = self.make_project("a")
        self._put(p, "001-b", "blocked", 30)
        res = CL.clear(24, dry_run=True)
        self.assertEqual(len(res["closed"]), 1)
        self.assertEqual(self._status(p, "001-b"), "blocked")

    def test_closing_a_stale_run_keeps_the_projects_newer_active_run_indexed(self) -> None:
        p = self.make_project("a")
        self._put(p, "001-old", "blocked", 30)
        self._put(p, "002-new", "active", 1)
        state.set_active_run(p, "002-new")
        CL.clear(24)
        self.assertEqual(state.active_runs().get(str(p)), "002-new")
        self.assertEqual(self._status(p, "002-new"), "active")

    def test_dangling_index_and_binding_files_are_dropped(self) -> None:
        p = self.make_project("a")
        self._put(p, "001-old", "blocked", 30)
        state.set_active_run(p, "001-old")
        sdir = paths.sessions_dir()
        sdir.mkdir(parents=True)
        orphan = sdir / "gone.json"
        util.write_json_atomic(orphan, {"project": str(p), "run_id": "999-missing"})
        os.utime(orphan, (time.time() - 40 * HOUR,) * 2)
        young = sdir / "young.json"
        util.write_json_atomic(young, {"project": str(p), "run_id": "999-missing"})
        res = CL.clear(24)
        self.assertNotIn(str(p), state.active_runs())
        self.assertFalse(orphan.exists())
        self.assertTrue(young.exists(), "a binding younger than the limit is kept")
        self.assertTrue(res["dropped"])

    def test_cli_runs_in_both_languages_and_json(self) -> None:
        p = self.make_project("a")
        self._put(p, "001-b", "blocked", 30)
        out = self.cli("clear", "--dry-run", env={"ORKESTRA_LANG": "en"}).stdout
        self.assertIn("DRY RUN", out)
        self.assertIn("a/001-b", out)
        out = self.cli("clear", "--json").stdout
        self.assertEqual(json.loads(out)["closed"][0]["run_id"], "001-b")
        self.assertEqual(self._status(p, "001-b"), "aborted")
        tr = self.cli("clear").stdout
        self.assertIn("atıl bir şey yok", tr)

    def test_negative_limit_is_a_clean_error(self) -> None:
        cp = self.cli("clear", "--older-than", "-1", check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("Traceback", cp.stderr)
