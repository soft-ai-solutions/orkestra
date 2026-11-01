"""Gate speed and robustness: process groups, streaming, idle/timeout kills, parallel waves, baseline skip,
resume, carry-forward and the baseline placement (clarify -> implement)."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import gates as G
from orkestra_core import runs as R
from orkestra_core.state import load_state

PY = shlex.quote(sys.executable)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, seconds: float = 6.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class TestRunnerProcessGroups(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="orkestra-gates-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))

    def _spawner(self):
        # a grandchild that outlives the shell unless the whole group is killed
        return f"(sleep 41 & echo $! > {shlex.quote(str(self.td / 'pid'))}); sleep 42"

    def test_timeout_kills_the_grandchild_too(self):
        t0 = time.time()
        res = G.run_gate(self.td, {"name": "g", "cmd": self._spawner(), "timeout": 1}, self.td / "logs", "t")
        self.assertTrue(res["timed_out"])
        self.assertEqual(res["exit"], 124)
        self.assertLess(time.time() - t0, 10)
        pid = int((self.td / "pid").read_text().strip())
        self.assertTrue(_wait_gone(pid), "the grandchild survived the timeout")

    def test_interrupt_kills_the_group_and_keeps_finished_results(self):
        seen = []
        gates = [{"name": "quick", "cmd": "true", "kind": "test"},
                 {"name": "hang", "cmd": self._spawner(), "kind": "test", "timeout": 60}]
        stop = threading.Event()

        def interrupt_once_the_hanging_gate_runs():       # event based: a fixed delay is flaky on a loaded machine
            end = time.time() + 60
            while not stop.is_set() and time.time() < end:
                if (self.td / "pid").exists() and (self.td / "pid").read_text().strip():
                    time.sleep(0.3)
                    __import__("_thread").interrupt_main()
                    return
                time.sleep(0.05)

        import signal
        # a process started in the background by a shell (nohup sh -c '...' &) inherits SIGINT=ignored: interrupt_main
        # would then do nothing. Make the test independent of how the suite was started.
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        watcher = threading.Thread(target=interrupt_once_the_hanging_gate_runs, daemon=True)
        watcher.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                G.run_all(self.td, gates, self.td / "logs", "t", on_result=seen.append)
        finally:
            stop.set()
            signal.signal(signal.SIGINT, previous)
        self.assertEqual([r["name"] for r in seen[-1]], ["quick"])
        pid = int((self.td / "pid").read_text().strip())
        self.assertTrue(_wait_gone(pid), "an interrupted run left an orphan")
        self.assertIn("INTERRUPTED", (self.td / "logs" / "t-hang.log").read_text())

    def test_output_reaches_the_log_while_the_gate_runs(self):
        out = {}
        th = threading.Thread(target=lambda: out.update(
            res=G.run_gate(self.td, {"name": "g", "cmd": "echo first-line; sleep 3", "timeout": 30}, self.td / "logs", "t")))
        th.start()
        log = self.td / "logs" / "t-g.log"
        seen = False
        end = time.time() + 2.5
        while time.time() < end:
            if log.exists() and "first-line" in log.read_text():
                seen = True
                break
            time.sleep(0.05)
        alive_then = th.is_alive()
        th.join()
        self.assertTrue(seen and alive_then, "output was not streamed while the gate was running")
        self.assertTrue(out["res"]["ok"])

    def test_idle_timeout_kills_a_silent_gate(self):
        t0 = time.time()
        res = G.run_gate(self.td, {"name": "g", "cmd": "echo hi; sleep 30", "timeout": 60, "idle_timeout": 1},
                         self.td / "logs", "t")
        self.assertLess(time.time() - t0, 12)
        self.assertTrue(res["timed_out"])
        self.assertTrue(res.get("idle_timeout"))
        self.assertIn("TIMEOUT-IDLE", res["tail"])

    def test_idle_timeout_is_off_by_default(self):
        res = G.run_gate(self.td, {"name": "g", "cmd": "echo hi; sleep 2; echo bye", "timeout": 30},
                         self.td / "logs", "t")
        self.assertTrue(res["ok"])
        self.assertNotIn("idle_timeout", res)

    def test_partial_output_is_in_the_log_after_a_timeout(self):
        res = G.run_gate(self.td, {"name": "g", "cmd": "echo partial-out; sleep 30", "timeout": 1}, self.td / "logs", "t")
        self.assertTrue(res["timed_out"])
        text = Path(res["log"]).read_text()
        self.assertIn("partial-out", text)
        self.assertIn("TIMEOUT", text)
        self.assertIn("[exit 124]", text)

    def test_stderr_and_stdout_are_both_captured(self):
        res = G.run_gate(self.td, {"name": "g", "cmd": "echo to-out; echo to-err 1>&2; exit 3"}, self.td / "logs", "t")
        self.assertEqual(res["exit"], 3)
        self.assertIn("to-out", res["tail"])
        self.assertIn("to-err", res["tail"])

    def test_a_leftover_background_child_does_not_hang_or_survive(self):
        t0 = time.time()
        res = G.run_gate(self.td, {"name": "g", "cmd": f"(sleep 43 & echo $! > {shlex.quote(str(self.td / 'pid'))}); echo done"},
                         self.td / "logs", "t")
        self.assertTrue(res["ok"])
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(_wait_gone(int((self.td / "pid").read_text().strip())))


class TestParallelWave(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="orkestra-gates-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))

    def _rec(self, name, sleep=0.0, code=0):
        d = self.td / "rec"
        d.mkdir(exist_ok=True)
        script = (f"import time,sys; f=open({str(d / name)!r},'w'); f.write(str(time.time())); f.flush(); "
                  f"time.sleep({sleep}); f.write(' '+str(time.time())); f.close(); sys.exit({code})")
        return f'{PY} -c "{script}"'

    def _span(self, name):
        a, b = (self.td / "rec" / name).read_text().split()
        return float(a), float(b)

    def test_effective_parallel_is_capped_by_half_the_cpus(self):
        with mock.patch.object(G.os, "cpu_count", return_value=8):
            self.assertEqual(G.effective_parallel(2), 2)
            self.assertEqual(G.effective_parallel(16), 4)
            self.assertEqual(G.effective_parallel(0), 1)
            self.assertEqual(G.effective_parallel("x"), 1)
        with mock.patch.object(G.os, "cpu_count", return_value=1):
            self.assertEqual(G.effective_parallel(4), 1)

    def test_a_unittest_command_without_kind_is_a_test_gate(self):
        self.assertTrue(G._concurrent({"cmd": "python3 -m unittest discover -s plugins/orkestra/tests -q"}))
        self.assertTrue(G._concurrent({"cmd": "pytest -q"}))
        self.assertFalse(G._concurrent({"cmd": "true"}))
        self.assertFalse(G._concurrent({"cmd": "python3 -m unittest discover -s x -q", "exclusive": True}))
        self.assertFalse(G._concurrent({"cmd": "python3 -m unittest discover -s x -q", "parallel": False}))

    def test_three_gates_overlap_and_keep_config_order(self):
        gates = [{"name": n, "cmd": "sleep 1", "kind": "test"} for n in ("a", "b", "c")]
        with mock.patch.object(G.os, "cpu_count", return_value=16):
            t0 = time.time()
            res = G.run_all(self.td, gates, self.td / "logs", "t", parallel=3)
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual([r["name"] for r in res], ["a", "b", "c"])
        self.assertTrue(all(r["ok"] for r in res))

    def test_parallel_one_is_the_serial_config_order(self):
        order = self.td / "order"
        gates = [{"name": n, "cmd": f"echo {n} >> {shlex.quote(str(order))}", "kind": k}
                 for n, k in (("e2e", "e2e"), ("test", "test"), ("build", "build"))]
        G.run_all(self.td, gates, self.td / "logs", "t", parallel=1)
        self.assertEqual(order.read_text().split(), ["e2e", "test", "build"])

    def test_a_failing_gate_does_not_cancel_the_others(self):
        gates = [{"name": "bad", "cmd": self._rec("bad", 0.1, 1), "kind": "lint"},
                 {"name": "good", "cmd": self._rec("good", 0.4), "kind": "test"}]
        with mock.patch.object(G.os, "cpu_count", return_value=16):
            res = G.run_all(self.td, gates, self.td / "logs", "t", parallel=2)
        self.assertEqual([(r["name"], r["ok"]) for r in res], [("bad", False), ("good", True)])

    def test_build_then_e2e_run_after_the_wave_and_exclusive_never_overlaps(self):
        gates = [{"name": "e2e", "cmd": self._rec("e2e"), "kind": "e2e"},
                 {"name": "t1", "cmd": self._rec("t1", 0.5), "kind": "test"},
                 {"name": "lint", "cmd": self._rec("lint", 0.5), "kind": "lint"},
                 {"name": "build", "cmd": self._rec("build"), "kind": "build"},
                 {"name": "ex", "cmd": self._rec("ex", 0.3), "kind": "test", "exclusive": True}]
        with mock.patch.object(G.os, "cpu_count", return_value=16):
            res = G.run_all(self.td, gates, self.td / "logs", "t", parallel=4)
        self.assertEqual([r["name"] for r in res], ["e2e", "t1", "lint", "build", "ex"])
        wave_end = max(self._span("t1")[1], self._span("lint")[1])
        self.assertLess(abs(self._span("t1")[0] - self._span("lint")[0]), 0.4)            # they overlapped
        for name in ("ex", "build", "e2e"):
            self.assertGreaterEqual(self._span(name)[0], wave_end - 0.01, name)
        self.assertLessEqual(self._span("build")[1], self._span("e2e")[0] + 0.01)         # build before e2e

    def test_on_result_is_called_after_every_gate_in_config_order(self):
        seen = []
        gates = [{"name": n, "cmd": "true", "kind": "test"} for n in ("a", "b")]
        G.run_all(self.td, gates, self.td / "logs", "t", on_result=lambda rs: seen.append([r["name"] for r in rs]))
        self.assertEqual(seen, [["a"], ["a", "b"]])

    def test_reuse_is_not_executed(self):
        marker = self.td / "ran"
        gates = [{"name": "a", "cmd": f"touch {shlex.quote(str(marker))}", "kind": "test"}]
        canned = {"name": "a", "cmd": "x", "kind": "test", "exit": 0, "ok": True, "timed_out": False, "duration_s": 0,
                  "log": "", "tail": "", "at": "now", "carried_from": "v"}
        res = G.run_all(self.td, gates, self.td / "logs", "t", reuse={"a": canned})
        self.assertEqual(res, [canned])
        self.assertFalse(marker.exists())


class TestGlob(unittest.TestCase):
    def test_glob_semantics(self):
        for pat, rel, want in (("docs/**", "docs/a/b.md", True), ("docs/**", "src/a.md", False),
                               ("src/*.ts", "src/a.ts", True), ("src/*.ts", "src/x/a.ts", False),
                               ("**/*.py", "a.py", True), ("**/*.py", "x/y/a.py", True), ("**", "anything/at/all", True),
                               ("src/?.ts", "src/a.ts", True), ("./src/a.ts", "src/a.ts", True)):
            self.assertEqual(G.glob_match(pat, rel), want, (pat, rel))


class RunBase(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.proj = self.make_project("demo")
        self.cli("init", "-p", "demo")
        out = self.cli("start", "-p", "demo", "--title", "Gates", "--request", "gates", "--size", "S",
                       "--approval-mode", "manual").stdout
        self.rid = re.search(r"run=(\S+)", out).group(1)
        self.rdir = self.proj / ".orkestra" / "runs" / self.rid

    def set_gates(self, gates):
        p = self.proj / ".orkestra" / "config.json"
        cfg = json.loads(p.read_text())
        cfg["gates"] = gates
        p.write_text(json.dumps(cfg))

    def verify_doc(self):
        return json.loads((self.rdir / "gates-verify.json").read_text())

    def baseline_doc(self):
        return json.loads((self.rdir / "gates-baseline.json").read_text())


class TestBaselineSkip(RunBase):
    def gates(self):
        return [{"name": "A", "cmd": "true", "kind": "test", "timeout": 30},
                {"name": "B", "cmd": "touch ran.marker; sleep 5; exit 1", "kind": "e2e", "timeout": 2}]

    def test_a_gate_that_timed_out_at_baseline_is_not_re_executed(self):
        self.set_gates(self.gates())
        R.gates_baseline(self.proj, self.rid)
        self.assertTrue(self.baseline_doc()["results"][1]["timed_out"])
        (self.proj / "ran.marker").unlink()
        t0 = time.time()
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"}):
            comparison, text = R.gates_verify(self.proj, self.rid)
        self.assertLess(time.time() - t0, 1.5)
        self.assertFalse((self.proj / "ran.marker").exists())
        self.assertTrue(comparison["ok"])
        self.assertEqual(comparison["pre_existing_failures"], ["B"])
        self.assertEqual(comparison["not_run"], [])
        b = [r for r in self.verify_doc()["results"] if r["name"] == "B"][0]
        self.assertEqual(b["skipped"], "failed at baseline")
        self.assertEqual(b["exit"], 124)
        self.assertTrue(b["timed_out"])
        self.assertFalse(b["ok"])
        self.assertIn("B", text.split("gates NOT protecting")[1].split("\n")[0])

    def test_full_flag_env_and_config_force_the_re_run(self):
        self.set_gates(self.gates())
        R.gates_baseline(self.proj, self.rid)
        for how in ("flag", "env", "config"):
            with self.subTest(how=how):
                (self.proj / "ran.marker").unlink(missing_ok=True)
                env = {"ORKESTRA_GATES_FULL": "1"} if how == "env" else {}
                with mock.patch.dict(os.environ, env):
                    if how == "config":
                        cfg = json.loads((self.proj / ".orkestra" / "config.json").read_text())
                        cfg["gates_rerun_failed_baseline"] = True
                        (self.proj / ".orkestra" / "config.json").write_text(json.dumps(cfg))
                    R.gates_verify(self.proj, self.rid, full=(how == "flag"))
                self.assertTrue((self.proj / "ran.marker").exists(), how)
                b = [r for r in self.verify_doc()["results"] if r["name"] == "B"][0]
                self.assertNotIn("skipped", b)

    def test_a_skipped_gate_never_masks_a_regression_in_another_gate(self):
        self.set_gates([{"name": "A", "cmd": "test ! -f broken", "kind": "test", "timeout": 30}, self.gates()[1]])
        R.gates_baseline(self.proj, self.rid)
        (self.proj / "broken").write_text("x")
        comparison, _ = R.gates_verify(self.proj, self.rid)
        self.assertFalse(comparison["ok"])
        self.assertEqual(comparison["regressions"], ["A"])
        self.assertEqual(comparison["pre_existing_failures"], ["B"])

    def test_a_fast_baseline_failure_still_runs_and_can_be_fixed(self):
        self.set_gates([{"name": "F", "cmd": "test -f flag", "kind": "test", "timeout": 900}])
        R.gates_baseline(self.proj, self.rid)
        (self.proj / "flag").write_text("x")
        comparison, _ = R.gates_verify(self.proj, self.rid)
        self.assertEqual(comparison["fixed"], ["F"])
        self.assertNotIn("skipped", self.verify_doc()["results"][0])

    def test_a_slow_baseline_failure_is_skipped_by_duration(self):
        self.set_gates([{"name": "S", "cmd": "touch ran.marker; exit 1", "kind": "test", "timeout": 900}])
        R.gates_baseline(self.proj, self.rid)
        doc = self.baseline_doc()
        doc["results"][0]["duration_s"] = 130.0
        (self.rdir / "gates-baseline.json").write_text(json.dumps(doc))
        (self.proj / "ran.marker").unlink()
        comparison, _ = R.gates_verify(self.proj, self.rid)
        self.assertFalse((self.proj / "ran.marker").exists())
        self.assertEqual(comparison["pre_existing_failures"], ["S"])

    def test_the_budget_of_a_gate_that_failed_fast_at_baseline_is_bounded(self):
        self.set_gates([{"name": "F", "cmd": "exit 1", "kind": "test", "timeout": 900},
                        {"name": "P", "cmd": "true", "kind": "test", "timeout": 900}])
        R.gates_baseline(self.proj, self.rid)
        seen = {}
        real = G.run_all

        def spy(project, gates, *a, **kw):
            seen.update({g["name"]: g["timeout"] for g in gates})
            return real(project, gates, *a, **kw)

        with mock.patch.object(G, "run_all", side_effect=spy):
            R.gates_verify(self.proj, self.rid)
        self.assertEqual(seen["F"], 60)                # min(900, max(60, 1.5 x ~0s))
        self.assertEqual(seen["P"], 900)               # a passing baseline keeps the full budget
        seen.clear()
        with mock.patch.object(G, "run_all", side_effect=spy):
            R.gates_verify(self.proj, self.rid, full=True)
        self.assertEqual(seen["F"], 900)               # --all means the full budget

    def test_format_and_catalogs_in_both_languages(self):
        self.set_gates(self.gates())
        R.gates_baseline(self.proj, self.rid)
        for lang, needle in (("en", "NOT protecting"), ("tr", "KORUMAYAN")):
            with self.subTest(lang=lang), mock.patch.dict(os.environ, {"ORKESTRA_LANG": lang}):
                _, text = R.gates_verify(self.proj, self.rid)
                self.assertIn(needle, text)
                self.assertNotIn("—", text.split("|---|")[0] + text.split(needle)[1].split("\n")[0])


class TestResume(RunBase):
    def test_an_interrupted_verify_keeps_finished_results_and_resume_runs_only_the_rest(self):
        self.set_gates([{"name": "X", "cmd": "echo x >> ran-x", "kind": "test"},
                        {"name": "Y", "cmd": "echo y >> ran-y", "kind": "test"}])
        R.gates_baseline(self.proj, self.rid)
        for f in ("ran-x", "ran-y"):
            (self.proj / f).unlink(missing_ok=True)
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
        real = G.run_gate

        def boom(project, gate, *a, **kw):
            if gate["name"] == "Y":
                raise KeyboardInterrupt()
            return real(project, gate, *a, **kw)

        with mock.patch.object(G, "run_gate", side_effect=boom):
            with self.assertRaises(KeyboardInterrupt):
                R.gates_verify(self.proj, self.rid)
        self.assertFalse((self.rdir / "gates-verify.json").exists())
        part = json.loads((self.rdir / "gates-verify.partial.json").read_text())
        self.assertEqual([r["name"] for r in part["results"]], ["X"])
        (self.proj / "ran-x").unlink()
        comparison, _ = R.gates_verify(self.proj, self.rid, resume=True)
        self.assertTrue(comparison["ok"])
        self.assertFalse((self.proj / "ran-x").exists(), "X was run again on resume")
        self.assertTrue((self.proj / "ran-y").exists())
        res = {r["name"]: r for r in self.verify_doc()["results"]}
        self.assertTrue(res["X"].get("resumed"))
        self.assertFalse((self.rdir / "gates-verify.partial.json").exists())

    def test_resume_is_ignored_when_the_tree_changed(self):
        self.set_gates([{"name": "X", "cmd": "echo x >> ran-x", "kind": "test"},
                        {"name": "Y", "cmd": "echo y >> ran-y", "kind": "test"}])
        R.gates_baseline(self.proj, self.rid)
        real = G.run_gate

        def boom(project, gate, *a, **kw):
            if gate["name"] == "Y":
                raise KeyboardInterrupt()
            return real(project, gate, *a, **kw)

        with mock.patch.object(G, "run_gate", side_effect=boom):
            with self.assertRaises(KeyboardInterrupt):
                R.gates_verify(self.proj, self.rid)
        (self.proj / "calc.py").write_text("def add(a, b):\n    return b + a\n")
        (self.proj / "ran-x").unlink()
        R.gates_verify(self.proj, self.rid, resume=True)
        self.assertTrue((self.proj / "ran-x").exists())

    def test_without_resume_everything_runs_again(self):
        self.set_gates([{"name": "X", "cmd": "echo x >> ran-x", "kind": "test"}])
        R.gates_baseline(self.proj, self.rid)
        R.gates_verify(self.proj, self.rid)
        R.gates_verify(self.proj, self.rid)
        self.assertEqual((self.proj / "ran-x").read_text().split(), ["x", "x", "x"])

    def test_an_interrupted_baseline_leaves_no_truncated_final_file(self):
        self.set_gates([{"name": "X", "cmd": "true", "kind": "test"}, {"name": "Y", "cmd": "true", "kind": "test"}])
        real = G.run_gate

        def boom(project, gate, *a, **kw):
            if gate["name"] == "Y":
                raise KeyboardInterrupt()
            return real(project, gate, *a, **kw)

        with mock.patch.object(G, "run_gate", side_effect=boom):
            with self.assertRaises(KeyboardInterrupt):
                R.gates_baseline(self.proj, self.rid)
        self.assertFalse((self.rdir / "gates-baseline.json").exists())
        self.assertTrue((self.rdir / "gates-baseline.partial.json").exists())
        st = load_state(self.proj, self.rid)
        msg = " ".join(R.preconditions(self.proj, st, "implement"))
        self.assertIn("still running or interrupted", msg)
        R.gates_baseline(self.proj, self.rid)
        self.assertTrue((self.rdir / "gates-baseline.json").exists())
        self.assertFalse((self.rdir / "gates-baseline.partial.json").exists())


class TestCarryForward(RunBase):
    def setUp(self):
        super().setUp()
        (self.proj / "docs").mkdir()
        (self.proj / "src").mkdir()
        self.set_gates([
            {"name": "docs", "cmd": "echo d >> ran-docs", "kind": "lint", "paths": ["docs/**"]},
            {"name": "test", "cmd": "echo t >> ran-test", "kind": "test", "paths": ["src/**"]},
        ])
        R.gates_baseline(self.proj, self.rid)
        for f in ("ran-docs", "ran-test"):
            (self.proj / f).unlink(missing_ok=True)

    def runs(self, name):
        p = self.proj / f"ran-{name}"
        return len(p.read_text().split()) if p.exists() else 0

    def results(self):
        return {r["name"]: r for r in self.verify_doc()["results"]}

    def test_only_the_gate_whose_paths_changed_runs_again(self):
        R.gates_verify(self.proj, self.rid)
        self.assertEqual((self.runs("docs"), self.runs("test")), (0, 0))  # baseline stands
        (self.proj / "docs" / "x.md").write_text("hello")
        R.gates_verify(self.proj, self.rid)
        self.assertEqual((self.runs("docs"), self.runs("test")), (1, 0))
        res = self.results()
        self.assertEqual(res["test"]["carried_from"].startswith("verify-"), True)
        self.assertNotIn("carried_from", res["docs"])
        (self.proj / "src" / "a.ts").write_text("export {}")
        R.gates_verify(self.proj, self.rid)
        self.assertEqual((self.runs("docs"), self.runs("test")), (1, 1))
        self.assertIn("carried_from", self.results()["docs"])

    def test_a_revert_is_a_change_too(self):
        (self.proj / "src" / "a.ts").write_text("export {}")
        R.gates_verify(self.proj, self.rid)
        (self.proj / "src" / "a.ts").unlink()
        R.gates_verify(self.proj, self.rid)
        self.assertEqual(self.runs("test"), 2)

    def test_all_forces_every_gate_and_a_failed_result_is_never_carried(self):
        R.gates_verify(self.proj, self.rid)
        R.gates_verify(self.proj, self.rid, full=True)
        self.assertEqual((self.runs("docs"), self.runs("test")), (1, 1))
        self.set_gates([{"name": "docs", "cmd": "echo d >> ran-docs; exit 1", "kind": "lint", "paths": ["docs/**"]}])
        R.gates_verify(self.proj, self.rid)
        n = self.runs("docs")
        R.gates_verify(self.proj, self.rid)
        self.assertEqual(self.runs("docs"), n + 1)                  # the failure re-ran

    def test_gates_without_paths_always_run(self):
        self.set_gates([{"name": "plain", "cmd": "echo p >> ran-plain", "kind": "test"}])
        R.gates_baseline(self.proj, self.rid)
        R.gates_verify(self.proj, self.rid)
        R.gates_verify(self.proj, self.rid)
        self.assertEqual(self.runs("plain"), 3)                    # the gate writes into the tree, so each verify runs

    def test_deliver_demands_one_full_verify_when_gates_were_carried(self):
        (self.proj / "docs" / "x.md").write_text("hello")
        R.gates_verify(self.proj, self.rid)
        R.gates_verify(self.proj, self.rid)                        # the untouched gate is carried
        st = load_state(self.proj, self.rid)
        self.assertIn("carried gates forward", " ".join(R.preconditions(self.proj, st, "deliver")))
        R.gates_verify(self.proj, self.rid, full=True)
        self.assertNotIn("carried gates forward", " ".join(R.preconditions(self.proj, st, "deliver")))

    def test_an_unchanged_tree_reuses_the_baseline_and_may_deliver(self):
        R.gates_verify(self.proj, self.rid)
        self.assertEqual((self.runs("docs"), self.runs("test")), (0, 0))
        self.assertTrue(all(r.get("unchanged_tree") for r in self.verify_doc()["results"]))
        st = load_state(self.proj, self.rid)
        self.assertNotIn("carried gates forward", " ".join(R.preconditions(self.proj, st, "deliver")))

    def test_carried_gates_are_reported_in_both_languages(self):
        R.gates_verify(self.proj, self.rid)
        for lang, needle in (("en", "carried forward"), ("tr", "taşınan")):
            with self.subTest(lang=lang), mock.patch.dict(os.environ, {"ORKESTRA_LANG": lang}):
                _, text = R.gates_verify(self.proj, self.rid)
                self.assertIn(needle, text)


class TestBaselinePlacement(RunBase):
    def test_clarify_no_longer_needs_the_baseline_implement_does(self):
        st = load_state(self.proj, self.rid)
        fdir = self.proj / ".orkestra" / "features" / self.rid
        (fdir / "context.md").write_text("# Context\n" + "calc.py defines add(); tests in tests/. " * 10)
        self.assertEqual([p for p in R.preconditions(self.proj, st, "clarify") if "baseline" in p], [])
        impl = " ".join(R.preconditions(self.proj, st, "implement"))
        self.assertIn("baseline gates not recorded", impl)
        self.set_gates([])
        R.gates_baseline(self.proj, self.rid)
        self.assertNotIn("baseline", " ".join(R.preconditions(self.proj, st, "implement")))

    def test_the_cli_accepts_resume_and_all(self):
        self.set_gates([{"name": "ok", "cmd": "true", "kind": "test"}])
        self.cli("gate", "baseline", "-p", "demo")
        cp = self.cli("gate", "verify", "-p", "demo", "--resume", "--all")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
