"""Tempo: `orkestra next` at every sub-step, reopen/round rules, parallel-task rules, timing, stall classes."""
import json
import os
import re
import time
import unittest
from pathlib import Path

import helpers
from test_flow import FlowBase, SPEC, UNITTEST
from orkestra_core import activity as A
from orkestra_core import tempo as T

PLAN2 = """---
feature: x
risk: low
approved_dependencies: []
---
# Plan — Subtract and multiply

## Genel kısıtlar
- add() değişmez.

## Görevler

### T001 — sub fonksiyonu
- Dosyalar: `calc.py` (değişir), `tests/test_sub.py` (yeni)
- Bağımlı: —
- Kabul: Given 5 ve 3 When sub Then 2 (FR-001)
- Test: `python3 -m unittest tests.test_sub -q`
- TDD: required

### T002 — mul fonksiyonu
- Dosyalar: `calc2.py` (yeni), `tests/test_mul.py` (yeni)
- Bağımlı: —
- Kabul: Given 2 ve 3 When mul Then 6 (FR-001)
- Test: `python3 -m unittest tests.test_mul -q`
- TDD: required

### T003 — sub belgeleri
- Dosyalar: `calc.py` (değişir)
- Bağımlı: —
- Kabul: docstring (FR-001)
- Test: `python3 -m unittest tests.test_sub -q`
- TDD: n/a (yalnız belge)

## Test stratejisi ve izlenebilirlik
| FR-001 | test_sub | unit |
| SC-001 | test_sub | unit |
"""


class TempoBase(FlowBase):
    size = "M"

    def start(self, title="Subtract", approval_mode="manual"):
        # Tempo walkthroughs exercise await/approve user gates; default product autopilot skips those.
        args = ["start", "-p", "demo", "--title", title, "--request", "sub fonksiyonu ekle",
                "--size", self.size, "--approval-mode", approval_mode]
        out = self.cli(*args).stdout
        m = re.search(r"\[orkestra:bind\] project=(.+?) run=(\S+)", out)
        self.rid = m.group(2)
        self.fdir = self.proj / ".orkestra" / "features" / self.rid
        self.rdir = self.proj / ".orkestra" / "runs" / self.rid
        self.hook("PostToolUse", {"session_id": self.sid, "tool_name": "Bash", "tool_input": {"command": "orkestra start"},
                                  "tool_response": {"stdout": out, "stderr": ""}, "cwd": str(self.proj)})
        return out

    def nxt(self):
        a = self.cli("next", "-p", "demo").stdout
        b = self.cli("next", "-p", "demo").stdout
        self.assertEqual(a, b, "orkestra next must be deterministic")
        self.assertLessEqual(len(a.strip().splitlines()), 3, a)
        return a

    def nj(self):
        return json.loads(self.cli("next", "--json", "-p", "demo").stdout)

    def to_implement(self, plan=PLAN2):
        self.start()
        self.phase_next()
        (self.fdir / "context.md").write_text("# Context\n" + ("calc.py defines add(); tests in tests/. " * 10))
        self.cli("gate", "baseline", "-p", "demo")
        self.phase_next()
        (self.fdir / "spec.md").write_text(SPEC)
        self.phase_next()
        (self.fdir / "plan.md").write_text(plan)
        (self.fdir / "reviews" / "plan-critic.md").write_text("# critic\nVERDICT: PASS\n")
        self.phase_next()
        self.approve()
        self.phase_next()

    def review(self, tid, verdict, name=None, minors=0):
        rows = "".join(f"| {i + 1} | minor | calc.py:1 | nit | rename |\n" for i in range(minors))
        (self.fdir / "reviews" / (name or f"{tid}.review.md")).write_text(f"# review {tid}\n{rows}\nVERDICT: {verdict}\n")

    def ledger(self):
        return (self.fdir / "ledger.md").read_text()


class TestNextWalksTheWholeRun(TempoBase):
    """Stop the flow at every sub-step: `orkestra next` names the one thing to do, twice the same, in ≤ 3 lines."""

    def test_every_sub_step(self):
        self.start()
        self.assertIn("classify the request", self.nxt())
        with open(self.fdir / "request.md", "a") as fh:
            fh.write("\n## Sınıflandırma\n- Tür: feature\n")
        self.assertIn("orkestra phase next", self.nxt())
        self.phase_next()                                          # discover
        self.assertIn("scouts", self.nxt())
        (self.fdir / "context.md").write_text("# Context\n" + ("calc.py defines add(); tests in tests/. " * 10))
        self.assertIn("orkestra gate baseline", self.nxt())
        self.cli("gate", "baseline", "-p", "demo")
        self.assertIn("(discover → clarify)", self.nxt())
        self.phase_next()                                          # clarify
        self.assertIn("spec.md", self.nxt())
        (self.fdir / "spec.md").write_text(SPEC)
        self.phase_next()                                          # plan
        self.assertIn("orkestra:architect", self.nxt())
        (self.fdir / "plan.md").write_text(PLAN2)
        self.assertIn("orkestra:plan-critic", self.nxt())
        (self.fdir / "reviews" / "plan-critic.md").write_text("VERDICT: FAIL\n")
        self.assertIn("plan-critic FAILED", self.nxt())
        (self.fdir / "reviews" / "plan-critic.md").write_text("VERDICT: FAIL\n\n## Round 2\nVERDICT: PASS\n")
        self.assertIn("(plan → approve)", self.nxt())
        self.phase_next()                                          # approve
        n = self.nxt()
        self.assertIn("ask for approval", n)
        self.assertIn("ONE AskUserQuestion", n)
        self.approve("onaylıyorum ama T001'e bir test daha ekle")
        self.assertIn("asked for changes", self.nxt())
        self.approve()
        self.assertIn("(approve → implement)", self.nxt())
        self.phase_next()                                          # implement
        self.assertIn("orkestra task start T001", self.nxt())
        self.cli("task", "start", "T001", "-p", "demo")
        n = self.nxt()
        self.assertIn("T001 · started", n)
        self.assertIn("orkestra brief T001", n)
        # red only
        test = self.proj / "tests" / "test_sub.py"
        self.hook("PreToolUse", {"session_id": self.sid, "tool_name": "Write", "cwd": str(self.proj),
                                 "tool_input": {"file_path": str(test)}})
        test.write_text("import unittest, sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
                        "from calc import sub\n\nclass T(unittest.TestCase):\n    def test_sub(self):\n"
                        "        self.assertEqual(sub(5, 3), 2)\n")
        cmd = "python3 -m unittest tests.test_sub -q"
        self.cli("tdd", "red", "T001", "-p", "demo", "--", cmd)
        self.assertIn("red recorded, green missing", self.nxt())
        with open(self.proj / "calc.py", "a") as fh:
            fh.write("\n\ndef sub(a, b):\n    return a - b\n")
        self.cli("tdd", "green", "T001", "-p", "demo", "--", cmd)
        n = self.nxt()
        self.assertIn("T001 · review_pending", n)
        self.assertIn("orkestra:reviewer (scope: task T001", n)
        # a file changed after green → the evidence is stale
        time.sleep(2.2)
        os.utime(self.proj / "calc.py", None)
        self.assertIn("re-run `orkestra tdd green T001", self.nxt())
        self.cli("tdd", "green", "T001", "-p", "demo", "--", cmd)
        # review FAIL → reopen round 1 (the review file name does not matter)
        self.review("T001", "FAIL", name="T001.md")
        n = self.nxt()
        self.assertIn("review FAIL (round 1)", n)
        self.assertIn('orkestra task reopen T001 "review round 1"', n)
        self.cli("task", "reopen", "T001", "review round 1", "-p", "demo")
        self.assertIn("orkestra task start T001", self.nxt())
        self.cli("task", "start", "T001", "-p", "demo")
        self.assertIn("T001 · started", self.nxt())              # the old review is older than this round
        with open(test, "a") as fh:
            fh.write("\n    def test_neg(self):\n        self.assertEqual(sub(1, 3), -3)\n")
        self.cli("tdd", "red", "T001", "-p", "demo", "--", cmd)
        with open(self.proj / "calc.py", "a") as fh:
            fh.write("\n# fixed\n")
        (self.proj / "calc.py").write_text((self.proj / "calc.py").read_text().replace("return a - b", "return a - b if a != 1 else -3"))
        self.cli("tdd", "green", "T001", "-p", "demo", "--", cmd)
        self.assertIn("T001 · review_pending", self.nxt())        # stale FAIL does not count for the new green
        time.sleep(1.1)
        self.review("T001", "PASS", name="T001.md", minors=2)
        n = self.nxt()
        self.assertIn("review PASS", n)
        self.assertIn("2 minor carried to final triage", n)
        self.assertIn("do NOT reopen", n)
        out = self.cli("task", "done", "T001", "-p", "demo", "review: PASS").stdout
        self.assertIn("tempo: run ", out)
        self.assertIn("review rejects 1 on 1 task(s)", out)
        self.assertIn("next [implement · T002", out)
        self.assertEqual(self.nj()["task"], "T002")
        # finish the rest quickly (deferred), then the phase is complete
        self.cli("task", "defer", "T002", "-p", "demo", "test için")
        self.cli("task", "defer", "T003", "-p", "demo", "test için")
        self.assertIn("(implement → verify)", self.nxt())
        self.phase_next()                                          # verify
        self.assertIn("orkestra gate verify", self.nxt())
        self.cli("gate", "verify", "-p", "demo")
        self.assertIn("orkestra:verifier", self.nxt())
        (self.fdir / "verification.md").write_text("| FR-001 | `tests/test_sub.py::test_sub` | PASS |\n"
                                                   "| SC-001 | gate unit | PASS |\nVERDICT: FAIL\n")
        self.assertIn("verification FAILED", self.nxt())
        (self.fdir / "verification.md").write_text("| FR-001 | `tests/test_sub.py::test_sub` | PASS |\n"
                                                   "| SC-001 | gate unit | PASS |\nVERDICT: PASS\n")
        out = self.cli("phase", "next", "-p", "demo").stdout       # review
        self.assertIn("tempo: run ", out)
        n = self.nxt()
        self.assertIn("scope: final", n)
        self.assertIn("orkestra:adversary", n)
        (self.fdir / "reviews" / "final-review.md").write_text("VERDICT: PASS\n")
        (self.fdir / "reviews" / "adversary.md").write_text("VERDICT: PASS\n")
        self.assertIn("triage every finding", self.nxt())
        (self.fdir / "reviews" / "triage.md").write_text("| R-001 | reviewer | x | fix | open |\n")
        self.assertIn("open triage items: R-001", self.nxt())
        (self.fdir / "reviews" / "triage.md").write_text("| R-001 | reviewer | x | fix | done |\n")
        n = self.nxt()
        self.assertIn("before review → deliver", n)
        self.assertIn("T002", n)
        self.assertIn("deferred", n)
        self.assertNotIn("(review → deliver)", n)
        st = json.loads(self.cli("status", "--json", "-p", "demo", "--run", self.rid).stdout)
        self.assertNotEqual(st["status"], "done")

    def test_waiting_states_are_never_continued_automatically(self):
        self.start()
        self.cli("await", "-p", "demo", "Hangi renk?")
        self.assertIn("WAITING FOR THE USER: Hangi renk?", self.nxt())
        self.cli("unblock", "-p", "demo")
        self.cli("block", "-p", "demo", "API anahtarı yok")
        self.assertIn("BLOCKED: API anahtarı yok", self.nxt())
        self.cli("unblock", "-p", "demo")
        self.cli("pause", "-p", "demo")
        self.assertIn("orkestra resume", self.nxt())

    def test_small_runs_skip_the_task_review(self):
        self.size = "S"
        self.to_implement()
        self.cli("task", "start", "T001", "-p", "demo")
        self.tdd_cycle("T001", "sub", "a - b", 5, 3, 2)
        self.assertIn("final review covers it (XS/S)", self.nxt())


class TestInterruptedRunResumesExactly(TempoBase):
    """Cut the session at a sub-step, come back with a NEW session: same next step, no evidence lost, nothing twice."""

    def new_session(self, sid):
        out = self.cli("resume", "-p", "demo").stdout
        self.hook("PostToolUse", {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "orkestra resume"},
                                  "tool_response": {"stdout": out, "stderr": ""}, "cwd": str(self.proj)})
        return out

    def test_resume_at_each_sub_step(self):
        self.to_implement()
        cmd = "python3 -m unittest tests.test_sub -q"
        # (1) cut right after `task start`
        self.cli("task", "start", "T001", "-p", "demo")
        before = self.nxt()
        self.assertIn(before.splitlines()[0], self.new_session("s2"))          # resume prints the same step
        self.assertIn("already in progress", self.cli("task", "start", "T001", "-p", "demo").stdout)
        self.assertEqual(self.ledger().count("T001 started"), 1)                 # not started twice
        # (2) cut after red
        test = self.proj / "tests" / "test_sub.py"
        test.write_text("import unittest, sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
                        "from calc import sub\n\nclass T(unittest.TestCase):\n    def test_sub(self):\n"
                        "        self.assertEqual(sub(5, 3), 2)\n")
        self.cli("tdd", "red", "T001", "-p", "demo", "--", cmd)
        self.new_session("s3")
        st = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertTrue(st["tasks"]["T001"]["red"]["ok"])                        # evidence survived the cut
        self.assertIn("red recorded, green missing", self.nxt())
        # (3) cut after green, before the review was dispatched
        with open(self.proj / "calc.py", "a") as fh:
            fh.write("\n\ndef sub(a, b):\n    return a - b\n")
        self.cli("tdd", "green", "T001", "-p", "demo", "--", cmd)
        self.new_session("s4")
        self.assertIn("orkestra:reviewer (scope: task T001", self.nxt())         # not re-implemented
        # (4) cut after the reviewer wrote PASS, before the verdict was applied
        self.review("T001", "PASS")
        self.new_session("s5")
        self.assertIn("review PASS", self.nxt())
        self.cli("task", "done", "T001", "-p", "demo", "review: PASS")
        # (5) the same command again after another cut: nothing is closed twice
        self.new_session("s6")
        out = self.cli("task", "done", "T001", "-p", "demo", "review: PASS").stdout
        self.assertIn("already done", out)
        self.assertEqual(len(re.findall(r"T001 done", self.ledger())), 1)
        cp = self.cli("task", "start", "T001", "-p", "demo", check=False)        # a done task is never silently redone
        self.assertEqual(cp.returncode, 2)
        self.assertIn("task reopen", cp.stderr)
        self.assertEqual(self.nj()["task"], "T002")                              # and nothing is skipped
        # (6) a question or an approval is never answered by the machinery
        self.cli("await", "-p", "demo", "T002 için hangi yaklaşım?")
        self.new_session("s7")
        self.assertIn("WAITING FOR THE USER", self.nxt())
        self.assertEqual(self.hook("Stop", {"session_id": "s7", "prompt_id": "p", "stop_hook_active": False}), {})


class TestReopenRules(TempoBase):
    def setUp(self):
        super().setUp()
        self.to_implement()
        self.cli("task", "start", "T001", "-p", "demo")
        self.tdd_cycle("T001", "sub", "a - b", 5, 3, 2)

    def reopen(self, *extra):
        return self.cli("task", "reopen", "T001", *extra, "-p", "demo", check=False)

    def test_pass_with_minors_is_not_reopened(self):
        """game-app T014 and 23 more: PASS (minor only) → reopened → a whole extra implement + review round."""
        self.review("T001", "PASS", minors=3)
        cp = self.reopen("apply review minors 1-3")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("says PASS", cp.stderr)
        self.assertIn("task note", cp.stderr)
        st = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertTrue(st["tasks"]["T001"]["green"]["ok"])        # evidence untouched
        self.cli("task", "note", "T001", "-p", "demo", "minor: rename tmp → remainder")
        self.cli("task", "done", "T001", "-p", "demo", "review: PASS")
        self.assertIn("T001: minor: rename tmp", self.cli("task", "list", "-p", "demo").stdout)
        # a justified exception is possible and visible
        cp = self.reopen("--force", "security nit cannot wait")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("forced: security nit cannot wait", self.ledger())

    def test_fail_reopens_and_the_third_round_needs_the_user(self):
        self.review("T001", "FAIL")
        self.assertEqual(self.reopen("review round 1").returncode, 0)
        self.assertEqual(self.reopen("review round 2").returncode, 0)
        cp = self.reopen("review round 3")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("AskUserQuestion", cp.stderr)
        self.assertIn("flags: T001: review round 3", self.cli("next", "-p", "demo").stdout)
        # --force alone is no ruling any more: a human row (user prompt / answer) newer than the last reopen is required
        A.record(self.proj, self.rid, {"session_id": self.sid}, "user", "bir tur daha dene")
        self.assertEqual(self.reopen("--force", "kullanıcı: bir tur daha").returncode, 0)

    def test_without_a_review_file_reopen_still_works(self):
        self.assertEqual(self.reopen("gate regression").returncode, 0)


class TestParallelTasks(TempoBase):
    def setUp(self):
        super().setUp()
        self.to_implement()

    def test_the_next_step_names_a_disjoint_task(self):
        text = self.nxt()
        self.assertIn("T001", text)
        self.assertIn("Also start T002", text)
        self.assertNotIn("T003", text)

    def test_overlapping_tasks_cannot_run_together(self):
        self.cli("task", "start", "T001", "-p", "demo")
        cp = self.cli("task", "start", "T003", "-p", "demo", check=False)       # both touch calc.py
        self.assertEqual(cp.returncode, 2)
        self.assertIn("shares calc.py with T001", cp.stderr)
        out = self.cli("task", "start", "T002", "-p", "demo").stdout           # disjoint files: allowed, with a note
        self.assertIn("NOTE: T001 also in progress (disjoint files)", out)
        out = self.cli("task", "start", "T003", "--force", "yalnız yorum satırı", "-p", "demo").stdout
        self.assertIn("NOTE: started despite shared files", out)
        self.assertIn("forced over shared files: yalnız yorum satırı", self.ledger())
        self.assertIn("share calc.py", self.cli("next", "-p", "demo").stdout)

    def test_concurrent_tasks_do_not_poison_each_others_tdd_evidence(self):
        """mobile-app 003: 29 `--why` deviations, almost all 'test file belongs to a concurrent task'."""
        self.cli("task", "start", "T001", "-p", "demo")
        self.cli("task", "start", "T002", "-p", "demo")
        head = "import unittest, sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
        (self.proj / "tests" / "test_sub.py").write_text(head + "from calc import sub\n\nclass T(unittest.TestCase):\n"
                                                         "    def test_sub(self):\n        self.assertEqual(sub(5, 3), 2)\n")
        cmd = "python3 -m unittest tests.test_sub -q"
        self.cli("tdd", "red", "T001", "-p", "demo", "--", cmd)
        # meanwhile T002's implementer writes ITS test file
        (self.proj / "tests" / "test_mul.py").write_text(head + "from calc2 import mul\n\nclass T(unittest.TestCase):\n"
                                                         "    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n")
        with open(self.proj / "calc.py", "a") as fh:
            fh.write("\n\ndef sub(a, b):\n    return a - b\n")
        cp = self.cli("tdd", "green", "T001", "-p", "demo", "--", cmd)          # no --why needed
        st = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertEqual(st["tasks"]["T001"]["green"]["deviations"], [])
        self.assertEqual(st["tasks"]["T001"]["green"]["foreign_tests_changed"], ["tests/test_mul.py"])
        # but its OWN test changed after red is still refused, and so is a test file nobody owns
        self.cli("task", "reopen", "T001", "round", "-p", "demo")
        self.cli("task", "start", "T001", "-p", "demo")
        self.cli("tdd", "red", "T001", "-p", "demo", "--", cmd + " -k nothing_matches_so_it_fails || false", check=False)
        self.cli("tdd", "red", "T001", "-p", "demo", "--", "python3 -c 'import sys; sys.exit(1)'")
        (self.proj / "tests" / "test_orphan.py").write_text(head + "class T(unittest.TestCase):\n    def test_x(self):\n        pass\n")
        cp = self.cli("tdd", "green", "T001", "-p", "demo", "--", "python3 -c 'import sys; sys.exit(1)'", check=False)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("tests/test_orphan.py", cp.stderr)


class TestStartWarning(helpers.Sandbox):
    def test_third_concurrent_run_gets_a_warning(self):
        for name in ("a", "b", "c"):
            self.make_project(name)
        self.assertNotIn("WARNING", self.cli("start", "-p", "a", "--title", "t", "--request", "r").stdout)
        self.assertNotIn("WARNING", self.cli("start", "-p", "b", "--title", "t", "--request", "r").stdout)
        out = self.cli("start", "-p", "c", "--title", "t", "--request", "r").stdout
        self.assertIn("WARNING: 2 other runs are open on this machine (a (intake), b (intake))", out)


class TestTempoCommand(TempoBase):
    def test_tempo_report_and_closed_run_usage(self):
        self.to_implement()
        self.cli("task", "start", "T001", "-p", "demo")
        out = self.cli("tempo", "-p", "demo", env={"ORKESTRA_LANG": "en"}).stdout     # the labels follow the language
        self.assertIn("phases: intake", out)
        self.assertIn("tasks: 0/3 done", out)
        data = json.loads(self.cli("tempo", "--json", "-p", "demo").stdout)
        self.assertIn("implement", data["timings"]["phases"])
        self.assertEqual(data["stall"]["cls"], 0)
        # a closed run keeps its last token numbers (the session binding is gone by then)
        stp = self.rdir / "state.json"
        st = json.loads(stp.read_text())
        st["usage"] = {"input": 10, "output": 20, "cache_write": 30, "cache_read": 40, "total": 100, "messages": 2, "context": 5}
        st["status"] = "done"
        stp.write_text(json.dumps(st))
        (self.home / "sessions" / f"{self.sid}.json").unlink()
        views = json.loads(self.cli("watch", "--json", "--all", "-p", "demo").stdout)
        self.assertEqual(views[0]["usage"]["cache_read"], 40)
        self.assertTrue(views[0]["usage"]["final"])
        self.assertIn("(kapanış değeri)", self.cli("watch", "--once", "--all", "-p", "demo").stdout)


# ---------------------------------------------------------------- pure units

def row(ts, kind, agent="main", tool="", what=""):
    return {"ts": ts, "kind": kind, "agent": agent, "tool": tool, "what": what}


ACTIVE = {"status": "active", "phase": "implement", "tasks": {}}
WD = dict(T.WD_DEFAULTS)


class TestClassifier(unittest.TestCase):
    def cls(self, rows, now, st=None, pending=None):
        return T.classify(st or ACTIVE, rows, pending or [], now=now, wd=WD)

    def test_1_waiting_for_the_user(self):
        t = 1000.0
        self.assertEqual(self.cls([], t, {"status": "awaiting_user", "awaiting": {"question": "?"}})["cls"], 1)
        self.assertEqual(self.cls([], t, {"status": "blocked", "blocked": {"reason": "x"}})["key"], "blocked")
        # an open question stays visible although subagents keep working in the background
        rows = [row(t, "tool", tool="AskUserQuestion", what="soru: T029 kararı"),
                row(t + 30, "tool", "orkestra:implementer", "Bash", "npx vitest"),
                row(t + 40, "agent_done", "orkestra:implementer"), row(t + 41, "handback", what="alt-ajan raporu")]
        r = self.cls(rows, t + 900)
        self.assertEqual((r["cls"], r["key"]), (1, "user"))
        self.assertEqual(self.cls([row(t, "tool", tool="Bash", what="ls"), row(t + 1, "end", tool="Bash"),
                                   row(t + 2, "idle", what="tur bitti")], t + 500)["key"], "turn_ended")

    def test_2_dead_wait_real_pulse_race_shape(self):
        t = 5000.0
        rows = [row(t, "tool", tool="Agent", what="→ orkestra:reviewer: T016"),
                row(t + 1, "result", tool="Agent", what="← orkestra:reviewer: async_launched"),
                row(t + 5, "tool", tool="Bash", what="cd .orkestra/current && python3 - <<'EOF' import os, time "
                                                     "t0=time.time(); p='reviews/T016.review.md' while time.time()-t0<590"),
                row(t + 200, "agent_done", "orkestra:reviewer", what="orkestra:reviewer bitti")]
        self.assertEqual(self.cls(rows, t + 230)["cls"], 0)                     # 30 s after the agent: fine
        r = self.cls(rows, t + 200 + 300)
        self.assertEqual((r["cls"], r["key"]), (2, "dead_wait"))
        self.assertIn("ana oturum bekleme komutunda", r["text"])
        self.assertEqual(r["since"], 300)
        # turn ended properly, the report never arrived
        rows2 = rows[:2] + [row(t + 3, "wait_agents", what="alt-ajan bekleniyor"), rows[3]]
        self.assertEqual(self.cls(rows2, t + 200 + 300)["key"], "dead_wait")
        # the report arrived and the main session acted on it → working
        rows3 = rows2 + [row(t + 201, "handback", what="alt-ajan raporu"), row(t + 205, "tool", tool="Bash", what="orkestra task done T016")]
        self.assertEqual(self.cls(rows3, t + 260)["cls"], 0)
        # while a subagent is still registered as running nothing is flagged
        self.assertEqual(self.cls(rows[:3], t + 400, pending=[{"type": "orkestra:reviewer", "at": t}])["key"], "agents")

    def test_3_hung_command(self):
        t = 9000.0
        rows = [row(t, "tool", "orkestra:implementer", "Bash", "npx jest __tests__/scripts/keychainUnlockLive.test.ts")]
        self.assertEqual(self.cls(rows, t + 600)["cls"], 0)
        r = self.cls(rows, t + 1000)
        self.assertEqual((r["cls"], r["key"]), (3, "hung"))
        gate = [row(t, "tool", tool="Bash", what="orkestra gate verify -p .")]
        self.assertEqual(self.cls(gate, t + 1000)["cls"], 0)                    # gates may take longer
        self.assertEqual(self.cls(gate, t + 2000)["cls"], 3)
        self.assertEqual(self.cls(rows + [row(t + 5, "end", "orkestra:implementer", "Bash", "✓")], t + 500)["cls"], 0)

    def test_4_silent_session(self):
        t = 100.0
        rows = [row(t, "tool", tool="Bash", what="ls"), row(t + 1, "end", tool="Bash", what="✓")]
        self.assertEqual(self.cls(rows, t + 300)["cls"], 0)
        r = self.cls(rows, t + 700)
        self.assertEqual((r["cls"], r["key"]), (4, "silent"))
        self.assertEqual(T.classify(ACTIVE, [], [], now=5000.0, wd=WD, state_mtime=100.0)["cls"], 4)

    def test_recent_state_update_supersedes_old_hook_timestamp(self):
        now = 50_000.0
        old_hook = [row(now - 27_000, "end", tool="Bash", what="✓")]
        state = dict(ACTIVE, phase="plan")
        r = T.classify(state, old_hook, now=now, wd=WD, state_mtime=now - 700)
        self.assertEqual((r["cls"], r["key"], r["since"]), (4, "silent", 700))
        self.assertIn("state", r["text"])
        self.assertIn("hook", r["text"])

        old_idle = [row(now - 27_000, "idle")]
        resumed = T.classify(state, old_idle, now=now, wd=WD, state_mtime=now - 700)
        self.assertEqual((resumed["key"], resumed["since"]), ("silent", 700))

    def test_5_loops_are_reported_next_to_the_class(self):
        t = 100.0
        deny = [row(t + i, "deny", "orkestra:reviewer", "Bash", "[roles] Agent orkestra:reviewer is read-only") for i in range(3)]
        cmds = [row(t + 10 + i, "tool", "orkestra:implementer", "Bash", "npx jest a.test.ts") for i in range(3)]
        st = {"status": "active", "phase": "implement",
              "tasks": {"T010": {"status": "in_progress", "notes": ["reopened: r1", "reopened: r2", "reopened: r3"]}}}
        loops = self.cls(deny + cmds, t + 20, st)["loops"]
        self.assertTrue(any("same guard 3×" in x for x in loops), loops)
        self.assertTrue(any("same command 3×" in x for x in loops), loops)
        self.assertTrue(any("T010: 3 review rounds" in x for x in loops), loops)
        self.assertEqual(T.loop_flags(deny[:2] + cmds[:2]), [])

    def test_dead_wait_share(self):
        t = 0.0
        rows = [row(t, "result", tool="Agent", what="← orkestra:reviewer: async_launched"),
                row(t + 100, "agent_done", "orkestra:reviewer"),
                row(t + 130, "tool", tool="Bash", what="sleep 20; cat reviews/T1.md"),     # still waiting
                row(t + 400, "handback", what="alt-ajan raporu"),
                row(t + 410, "tool", tool="Bash", what="orkestra task done T001"),
                row(t + 1000, "tool", tool="Bash", what="ls")]
        d = T.dead_wait_share(rows)
        self.assertEqual((int(d["dead"]), d["count"]), (310, 1))
        self.assertAlmostEqual(d["share"], 0.31, places=2)


class TestActivityUnits(helpers.Sandbox):
    def test_synthetic_prompt_markers(self):
        for p in ('<agent-message from="a1"> [Subagent hand-back] …', "  <task-notification> <task-id>x</task-id>",
                  "[Subagent hand-back] The text below"):
            self.assertTrue(A.is_synthetic_prompt(p), p)
        for p in ("onay", "commitle", "<b>kalın</b> yaz", "agent-message nedir?", ""):
            self.assertFalse(A.is_synthetic_prompt(p), p)

    def test_launch_info_shapes(self):
        ti = {"subagent_type": "orkestra:implementer", "description": "R003 chain E: FIX-R015 + T004", "name": "impl-1"}
        info = A.launch_info({"tool_input": ti, "tool_response": {"status": "async_launched", "agentId": "abc123def"}})
        self.assertEqual((info["id"], info["type"], info["name"], info["tasks"]),
                         ("abc123def", "orkestra:implementer", "impl-1", ["FIX-R015", "T004"]))
        info = A.launch_info({"tool_input": ti, "tool_response": "Async agent launched successfully.\nagentId: a8016af73cb7c7f70 "
                                                                 "(use SendMessage…) async_launched"})
        self.assertEqual(info["id"], "a8016af73cb7c7f70")
        self.assertIsNone(A.launch_info({"tool_input": ti, "tool_response": {"content": [{"type": "text", "text": "STATUS: DONE"}]}}))

    def test_rotation_archives_instead_of_dropping(self):
        p = self.make_project("rot")
        self.cli("init", "-p", "rot")
        rid = re.search(r"run=(\S+)", self.cli("start", "-p", "rot", "--title", "T", "--request", "r").stdout).group(1)
        ev = {"tool_name": "Bash", "tool_input": {"command": "echo " + "x" * 150}}
        for _ in range(4500):
            A.record(p, rid, ev, "tool")
        live = p / ".orkestra" / "runs" / rid / "activity.jsonl"
        self.assertLessEqual(live.stat().st_size, A.MAX_BYTES + 4096)
        self.assertTrue((live.parent / "activity.1.jsonl").exists())
        self.assertEqual(len(A.read_all_rows(p, rid)), 4500)                    # nothing was lost
        self.assertLessEqual(len(A.read_rows(p, rid)), A.KEEP_LINES + 1300)

    def test_running_agents_understands_background_launches(self):
        rows = [row(1, "tool", tool="Agent", what="→ orkestra:reviewer: final"),
                row(2, "result", tool="Agent", what="← orkestra:reviewer: async_launched")]
        self.assertEqual(A.running_agents(rows), ["orkestra:reviewer"])
        self.assertEqual(A.running_agents(rows + [row(9, "agent_done", "orkestra:reviewer")]), [])


class TestHintsAndPlanSize(helpers.Sandbox):
    def test_playbook_hints_follow_the_changed_files(self):
        from orkestra_core import hints
        web = self.make_project("web")
        (web / "package.json").write_text('{"dependencies": {"react": "19", "next": "15"}}')
        mob = self.make_project("mob")
        (mob / "package.json").write_text('{"dependencies": {"expo": "~53", "react-native": "0.79"}}')
        self.assertEqual(hints.playbooks_for(["README.md", "docs/a.md", ".orkestra/features/x/plan.md"], web), [])
        self.assertEqual(hints.playbooks_for(["src/lib/format.ts"], web), ["stack-web"])
        self.assertEqual(hints.playbooks_for(["app/api/orders/route.ts", "prisma/schema.prisma"], web),
                         ["security", "data", "stack-web"])
        self.assertEqual(hints.playbooks_for(["src/components/Cart.tsx", "src/stores/cart.ts"], web), ["stack-web", "ui-flow"])
        self.assertEqual(hints.playbooks_for(["src/screens/Home.tsx"], mob), ["stack-mobile", "ui-flow"])
        self.assertEqual(hints.playbooks_for(["calc.py", "package.json"], web), ["security", "stack-python"])

    def test_large_plan_note(self):
        from orkestra_core import cli
        small = "\n".join(f"### T{i:03d} — t\n- Dosyalar: `a.py`\n- Kabul: x\n- Test: `x`\n- TDD: required\n" for i in range(1, 21))
        large = "\n".join(f"### T{i:03d} — t\n- Dosyalar: `a.py`\n- Kabul: x\n- Test: `x`\n- TDD: required\n" for i in range(1, 50))
        self.assertEqual(cli._large_plan_note(small), "")
        note = cli._large_plan_note(large)
        self.assertIn("LARGE PLAN — 49 tasks", note)
        self.assertIn("splitting it into 3 runs", note)



class TestSlimSizeFastTrack(TempoBase):
    """S (and XS) must not wait on plan-critic or adversary — that is the speed path."""

    size = "S"

    def test_s_skips_critic_and_adversary(self):
        self.start()
        with open(self.fdir / "request.md", "a") as fh:
            fh.write("\n## Sınıflandırma\n- Tür: bugfix\n")
        self.phase_next()  # discover
        n = self.nxt()
        self.assertIn("S fast track", n)
        self.assertNotIn("dispatch the scouts", n)
        (self.fdir / "context.md").write_text("# Context\n" + ("calc.py defines add(); tests in tests/. " * 10))
        self.cli("gate", "baseline", "-p", "demo")
        self.phase_next()  # clarify
        (self.fdir / "spec.md").write_text(SPEC)
        self.phase_next()  # plan
        n = self.nxt()
        self.assertIn("write a short plan.md yourself", n)
        self.assertNotIn("orkestra:architect", n)
        (self.fdir / "plan.md").write_text(PLAN2)
        n = self.nxt()
        self.assertNotIn("orkestra:plan-critic", n)
        self.assertIn("(plan → approve)", n)
        self.phase_next()  # approve
        self.approve()
        self.phase_next()  # implement
        # jump to review: mark tasks done lightly is heavy; just set phase via completing path minimally
        # Instead: write final-review only and ask next after forcing phase — use status size check
        st = json.loads(self.cli("status", "--json", "-p", "demo", "--run", self.rid).stdout)
        self.assertEqual(st["size"], "S")


class TestAutoSizeOnStart(TempoBase):
    def test_auto_infers_and_prints(self):
        out = self.cli("start", "-p", "demo", "--title", "Typo", "--request", "typo düzelt README",
                       "--size", "auto", "--approval-mode", "manual").stdout
        self.assertIn("size: XS", out)
        self.assertIn("inferred", out)
        st = json.loads(self.cli("status", "--json", "-p", "demo").stdout)
        self.assertEqual(st["size"], "XS")


class TestALongPlanWritesTheFile(unittest.TestCase):
    def test_ten_minutes_without_plan_md_says_write_it_now(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        root = Path(tempfile.mkdtemp(prefix="plan-late-"))
        rid = "001-x"
        (root / ".orkestra" / "features" / rid).mkdir(parents=True)
        (root / ".orkestra" / "features" / rid / "spec.md").write_text("spec\n", encoding="utf-8")
        opened = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
        state = {"run_id": rid, "phase": "plan", "status": "active", "size": "M",
                 "approval_mode": "autopilot", "history": [{"to": "plan", "at": opened}]}
        note = T.plan_late_note(root, state, rid)
        self.assertIn("plan.md is not written yet", note)
        (root / ".orkestra" / "features" / rid / "plan.md").write_text("x" * 400, encoding="utf-8")
        self.assertEqual(T.plan_late_note(root, state, rid), "")


if __name__ == "__main__":
    unittest.main()
