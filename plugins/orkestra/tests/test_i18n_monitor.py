"""The live monitor in two languages: `orkestra watch` (terminal + JSON), the web dashboard, the stall verdicts and the
human duration format.

The suite is pinned to Turkish (helpers.py); every test here picks its language explicitly. Turkish output must stay what
it always was (frozen below as exact strings), English must read naturally and hold no Turkish letters except where a
user's own text is echoed, and the machine side (JSON keys, enum values, stall classes) must not depend on the language.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import helpers
from dash_support import HAVE_NODE, card_texts, run_dashboard
from orkestra_core import i18n
from orkestra_core import monitor as MON
from orkestra_core import tempo as T
from orkestra_core.messages import monitor as MSG_MONITOR
from orkestra_core.messages import tempo as MSG_TEMPO
from orkestra_core.state import PHASES

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
LIB = helpers.PLUGIN / "lib" / "orkestra_core"
WD = dict(T.WD_DEFAULTS)


def lang(code: str):
    return mock.patch.dict(os.environ, {"ORKESTRA_LANG": code})


def legacy_dur_tr(seconds):
    """The Turkish duration `tempo.dur_tr` printed before localization (the oracle for the Turkish side)."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} sn"
    if s < 3600:
        return f"{s // 60} dk"
    return f"{s // 3600} sa {s % 3600 // 60} dk"


def row(ts, kind, agent="main", tool="", what=""):
    return {"ts": ts, "kind": kind, "agent": agent, "tool": tool, "what": what}


def key_paths(obj, prefix=""):
    """Every key path of a JSON value (list items merged), to compare the shape of two documents."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(f"{prefix}/{k}")
            out |= key_paths(v, f"{prefix}/{k}")
    elif isinstance(obj, list):
        for v in obj:
            out |= key_paths(v, prefix + "[]")
    return out


# ================================================================== durations

class TestDuration(unittest.TestCase):
    def test_table_in_both_languages(self):
        secs = (None, -5, 0, 5, 59, 60, 89, 90, 119, 600, 3599, 3600, 3660, 7500, 90000)
        with lang("en"):
            self.assertEqual([i18n.duration(x) for x in secs],
                             ["?", "0s", "0s", "5s", "59s", "60s", "89s", "1m", "1m", "10m", "59m", "1h 0m", "1h 1m",
                              "2h 5m", "25h 0m"])
            self.assertEqual(T.dur_tr(7500), "2h 5m")
        with lang("tr"):
            self.assertEqual([i18n.duration(x) for x in secs],
                             ["?", "0 sn", "0 sn", "5 sn", "59 sn", "60 sn", "89 sn", "1 dk", "1 dk", "10 dk", "59 dk",
                              "1 sa 0 dk", "1 sa 1 dk", "2 sa 5 dk", "25 sa 0 dk"])

    def test_turkish_equals_the_legacy_formatter_everywhere(self):
        with lang("tr"):
            for s in list(range(0, 400)) + list(range(3500, 3700)) + list(range(0, 400_000, 977)) + [None, -1, 12.9]:
                self.assertEqual(T.dur_tr(s), legacy_dur_tr(s), s)
                self.assertEqual(i18n.duration(s), legacy_dur_tr(s), s)

    def test_dur_tr_follows_the_language_but_dur_stays_english(self):
        with lang("tr"):
            self.assertEqual(T.dur_tr(125), "2 dk")
            self.assertEqual(T.dur(7500), "2h05m")            # the form the model reads never changes
        with lang("en"):
            self.assertEqual(T.dur_tr(125), "2m")
            self.assertEqual(T.dur(7500), "2h05m")

    def test_seconds_threshold_is_adjustable_and_capped_at_one_hour(self):
        with lang("tr"):
            self.assertEqual(i18n.duration(75, sec_until=60), "1 dk")
            self.assertEqual(i18n.duration(30, sec_until=0), "0 dk")
            self.assertEqual(i18n.duration(3000, sec_until=10 ** 6), "3000 sn")
            self.assertEqual(i18n.duration(4000, sec_until=10 ** 6), "1 sa 6 dk")      # seconds never past one hour
            self.assertEqual(i18n.ago(75, sec_until=60), "1 dk önce")
        with lang("en"):
            self.assertEqual(i18n.duration(75, sec_until=60), "1m")
            self.assertEqual(i18n.duration(3000, sec_until=10 ** 6), "3000s")
            self.assertEqual(i18n.duration(4000, sec_until=10 ** 6), "1h 6m")

    def test_ago(self):
        with lang("en"):
            self.assertEqual((i18n.ago(None), i18n.ago(30), i18n.ago(125)), ("?", "30s ago", "2m ago"))
        with lang("tr"):
            self.assertEqual((i18n.ago(None), i18n.ago(30), i18n.ago(125)), ("?", "30 sn önce", "2 dk önce"))

    def test_monitor_terminal_uses_the_same_formatter(self):
        with lang("en"):
            self.assertEqual((MON._dur(None), MON._dur(30), MON._dur(4000)), ("", "30s", "1h 6m"))
        with lang("tr"):
            self.assertEqual((MON._dur(None), MON._dur(30), MON._dur(4000)), ("", "30 sn", "1 sa 6 dk"))


# ================================================================== stall verdicts

class TestStallVerdicts(unittest.TestCase):
    """(name, status dict, rows, pending, state_mtime) at now=5000 → cls/key (language free) + the two sentences."""
    NOW = 5000.0
    ACTIVE = {"status": "active", "phase": "implement", "tasks": {}}
    IMPL = "orkestra:implementer"
    DEAD = [row(NOW - 700, "result", tool="Agent", what="<- x: async_launched"),
            row(NOW - 400, "tool", tool="Bash", what="sleep 20; cat a"), row(NOW - 300, "agent_done", "orkestra:reviewer")]

    def cases(self):
        n, A_ = self.NOW, self.ACTIVE
        return [
            ("closed", {"status": "done", "tasks": {}}, [], [], None, (0, "closed"),
             "kapalı", "closed"),
            ("await_noq", {"status": "awaiting_user", "awaiting": {}, "tasks": {}}, [], [], None, (1, "user"),
             "kullanıcı bekleniyor — soru", "waiting for the user: question"),
            ("await_q", {"status": "awaiting_user", "awaiting": {"question": "Q?"}, "tasks": {}}, [], [], None, (1, "user"),
             "kullanıcı bekleniyor — Q?", "waiting for the user: Q?"),
            ("blocked", {"status": "blocked", "blocked": {"reason": "needs creds"}, "tasks": {}}, [], [], None,
             (1, "blocked"), "bloke — needs creds", "blocked: needs creds"),
            ("paused", {"status": "paused", "tasks": {}}, [], [], None, (1, "paused"), "duraklatıldı", "paused"),
            ("open_question", A_, [row(n - 5, "tool", tool="AskUserQuestion", what="Pick one")], [], None, (1, "user"),
             "kullanıcı bekleniyor — açık soru (Pick one)", "waiting for the user: open question (Pick one)"),
            ("turn_ended", A_, [row(n - 50, "idle", what="x")], [], None, (1, "turn_ended"),
             "tur bitti — kullanıcının mesajı bekleniyor", "turn ended: waiting for the user's message"),
            ("hung", A_, [row(n - 4000, "tool", self.IMPL, "Bash", "npx jest x")], [], None, (3, "hung"),
             "asılı komut (implementer, 1 sa 6 dk): npx jest x — komut takılmış ya da oturum komut sırasında kapanmış "
             "olabilir",
             "hung command (implementer, 1h 6m): npx jest x · the command may be stuck, or the session may have closed "
             "while it was running"),
            ("agents", A_, [row(n - 20, "tool", "orkestra:scout", "Read", "x")],
             [{"type": "orkestra:scout", "at": n - 100}, {"type": "orkestra:reviewer", "at": n - 90}], None,
             (0, "agents"), "alt-ajan çalışıyor: scout, reviewer", "subagents running: scout, reviewer"),
            ("dead_wait_in_command", A_, self.DEAD, [], None, (2, "dead_wait"),
             "ölü bekleme — alt-ajan 5 dk önce bitti, ana oturum bekleme komutunda",
             "dead wait: a subagent finished 5m ago, the main session is stuck in a wait command"),
            ("dead_wait_no_report", A_, [self.DEAD[0], self.DEAD[2]], [], None, (2, "dead_wait"),
             "ölü bekleme — alt-ajan 5 dk önce bitti, alt-ajan raporu ana oturuma ulaşmadı",
             "dead wait: a subagent finished 5m ago, the subagent report never reached the main session"),
            ("silent_state_newer", A_, [row(n - 4000, "tool", tool="Read", what="x")], [], n - 700, (4, "silent"),
             "sessiz oturum — run state 11 dk önce güncellendi; son hook aktivitesi 1 sa 6 dk önce; canlılık hook "
             "kayıtlarından doğrulanamıyor",
             "silent session: run state updated 11m ago; last hook activity 1h 6m ago; liveness cannot be confirmed "
             "from hook records"),
            ("silent_no_hook_rows", A_, [], [], 100.0, (4, "silent"),
             "sessiz oturum — run state 1 sa 21 dk önce güncellendi; henüz hook aktivitesi yok; canlılık hook "
             "kayıtlarından doğrulanamıyor",
             "silent session: run state updated 1h 21m ago; no hook activity yet; liveness cannot be confirmed from "
             "hook records"),
            ("silent_old_rows", A_, [row(n - 3600, "tool", tool="Read", what="x")], [], None, (4, "silent"),
             "sessiz oturum — 1 sa 0 dk kayıt yok; oturum kapanmış ya da çökmüş olabilir",
             "silent session: no records for 1h 0m; the session may have closed or crashed"),
            ("silent_lost_agents", A_, [row(n - 1500, "result", tool="Agent", what="<- x: async_launched")],
             [{"type": self.IMPL, "at": n - 1500}], None, (4, "silent"),
             "sessiz oturum — 25 dk kayıt yok; oturum kapanmış ya da çökmüş olabilir (1 alt-ajan kaydı var ama 25 dk "
             "hiçbir iz yok)",
             "silent session: no records for 25m; the session may have closed or crashed (subagents registered: 1, but "
             "no sign of life for 25m)"),
            ("working", A_, [row(n - 5, "tool", tool="Read", what="x")], [], None, (0, "working"),
             "çalışıyor", "working"),
        ]

    def classify(self, code, st, rows, pending, mtime):
        with lang(code):
            return T.classify(st, rows, pending, now=self.NOW, wd=WD, state_mtime=mtime)

    def test_every_verdict_in_both_languages(self):
        for name, st, rows, pending, mtime, (cls, key), tr_text, en_text in self.cases():
            tr, en = self.classify("tr", st, rows, pending, mtime), self.classify("en", st, rows, pending, mtime)
            self.assertEqual((tr["cls"], tr["key"]), (cls, key), name)
            self.assertEqual((en["cls"], en["key"]), (cls, key), name)
            self.assertEqual(tr["text"], tr_text, f"{name}: the Turkish text changed")
            self.assertEqual(en["text"], en_text, name)
            self.assertEqual((tr["since"], tr["loops"]), (en["since"], en["loops"]), f"{name}: machine fields differ")
            self.assertEqual(sorted(tr), sorted(en))
            self.assertFalse(TR_LETTERS.search(en["text"]), f"{name}: {en['text']}")
            self.assertNotIn("—", en["text"], name)

    def test_the_user_s_own_words_are_echoed_untouched_in_english(self):
        st = {"status": "awaiting_user", "awaiting": {"question": "Şu mu, bu mu?"}, "tasks": {}}
        self.assertEqual(self.classify("en", st, [], [], None)["text"], "waiting for the user: Şu mu, bu mu?")
        blocked = {"status": "blocked", "blocked": {"reason": "kimlik bilgisi gerekli"}, "tasks": {}}
        self.assertEqual(self.classify("en", blocked, [], [], None)["text"], "blocked: kimlik bilgisi gerekli")

    def test_braces_in_user_text_never_break_a_verdict(self):
        st = {"status": "awaiting_user", "awaiting": {"question": "use {name} or {0}?"}, "tasks": {}}
        for code in ("en", "tr"):
            self.assertIn("use {name} or {0}?", self.classify(code, st, [], [], None)["text"])

    def test_loops_stay_with_the_verdict_in_both_languages(self):
        t = 100.0
        deny = [row(t + i, "deny", self.IMPL, "Bash", "[roles] read-only") for i in range(3)]
        for code in ("en", "tr"):
            r = self.classify(code, self.ACTIVE, deny, [], None)
            self.assertTrue(any("same guard 3×" in x for x in r["loops"]), r["loops"])

    def test_language_follows_the_environment_per_call(self):
        st, rows = {"status": "paused", "tasks": {}}, []
        with lang("en"):
            self.assertEqual(T.classify(st, rows, now=1.0, wd=WD)["text"], "paused")
        with lang("tr"):
            self.assertEqual(T.classify(st, rows, now=1.0, wd=WD)["text"], "duraklatıldı")
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "de"}):         # unsupported → English, never a crash
            self.assertEqual(T.classify(st, rows, now=1.0, wd=WD)["text"], "paused")


# ================================================================== fixtures for watch / dashboard

class MonitorBase(helpers.Sandbox):
    """A workspace with hand-written runs (no `orkestra start`, no git, no symlinks: it runs on every platform)."""

    def iso(self, ts):
        import datetime as dt
        return dt.datetime.fromtimestamp(ts).isoformat()

    def make_run(self, name, rid, *, status="active", phase="implement", rows=(), tasks=None, extra=None, gates=None,
                 ledger=(), session=False, updated_ago=30, history_ago=400, mode="interactive", engine=None):
        now = time.time()
        proj = self.ws / name
        run = proj / ".orkestra" / "runs" / rid
        run.mkdir(parents=True)
        (proj / ".orkestra" / "features" / rid).mkdir(parents=True)
        st = {"run_id": rid, "title": f"Title of {rid}", "size": "M", "mode": mode, "phase": phase, "status": status,
              "approved": True, "tasks": tasks or {}, "created": self.iso(now - 7000),
              "updated": self.iso(now - updated_ago), "history": [{"to": phase, "at": self.iso(now - history_ago)}]}
        if engine:
            st["engine"] = engine
        st.update(extra or {})
        (run / "state.json").write_text(json.dumps(st), encoding="utf-8")
        if rows:
            body = "\n".join(json.dumps(dict({k: v for k, v in r.items() if k != "ago"}, ts=now - r["ago"])
                                        if "ago" in r else r) for r in rows)
            (run / "activity.jsonl").write_text(body + "\n", encoding="utf-8")
        for label, data in (gates or {}).items():
            (run / f"gates-{label}.json").write_text(json.dumps(data), encoding="utf-8")
        if ledger:
            (proj / ".orkestra" / "features" / rid / "ledger.md").write_text(
                "\n".join(f"- {x}" for x in ledger) + "\n", encoding="utf-8")
        if session:
            d = self.home / "sessions"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"sess-{rid}.json").write_text(json.dumps({"run_id": rid, "project": str(proj),
                                                             "bound_at": self.iso(now - 6000)}), encoding="utf-8")
        return proj

    @staticmethod
    def ago_row(ago, kind, agent="main", tool="", what=""):
        return {"ago": ago, "kind": kind, "agent": agent, "tool": tool, "what": what}

    def populate(self):
        """One run per interesting state; every free-text field is plain English (no Turkish letters)."""
        r = self.ago_row
        done_usage = {"input": 10, "output": 2500, "cache_write": 30, "cache_read": 1_200_000, "total": 100,
                      "messages": 5, "context": 45200}
        self.make_run("a-hung", "r-hung", mode="headless", engine="cursor", session=True,
                      # the open command is the LAST row of the whole run: a row of any agent after it would prove the run
                      # moves on (tempo.classify: hung = no row since the command started)
                      rows=[r(2300, "tool", tool="Read", what="calc.py"),
                            r(2290, "deny", tool="Bash", what="[roles] Agent is read-only"),
                            r(2280, "deny", tool="Bash", what="[roles] Agent is read-only"),
                            r(2270, "deny", tool="Bash", what="no bracket denial"),
                            r(1700, "tool", agent="orkestra:implementer", tool="Bash", what="npx jest big.test.ts")],
                      tasks={"T001": {"status": "in_progress", "title": "Do it", "red": {"ok": True},
                                      "green": {"ok": True}, "notes": ["reopened: r1"]},
                             "T002": {"status": "done", "title": "Done one"}},
                      gates={"baseline": {"results": [{"ok": True}, {"ok": True}]},
                             "verify": {"results": [{"ok": True}, {"ok": False}],
                                        "comparison": {"ok": False, "regressions": ["unit", "lint"]}}},
                      ledger=["first", "second note"])
        self.make_run("b-wait", "r-wait", status="awaiting_user", phase="clarify",
                      extra={"awaiting": {"question": "Which colour?", "at": self.iso(time.time() - 500)}},
                      rows=[r(600, "tool", tool="AskUserQuestion", what="question: Colour")], history_ago=120)
        self.make_run("c-silent", "r-silent", rows=[r(3000, "tool", tool="Read", what="x.py"),
                                                    r(2990, "end", tool="Bash", what="ok")],
                      updated_ago=2000, history_ago=5000)
        self.make_run("d-dead", "r-dead",
                      rows=[r(900, "tool", tool="Agent", what="-> orkestra:reviewer: T016"),
                            r(899, "result", tool="Agent", what="<- orkestra:reviewer: async_launched"),
                            r(895, "tool", tool="Bash", what="python3 - <<EOF time.sleep(5) EOF"),
                            r(300, "agent_done", agent="orkestra:reviewer", what="reviewer done")],
                      updated_ago=800, history_ago=100)
        self.make_run("e-done", "r-done", status="done", phase="done", extra={"usage": done_usage},
                      rows=[r(100, "tool", tool="Bash", what="ls")], updated_ago=60, history_ago=60,
                      tasks={"T001": {"status": "done", "title": "x", "red": {"ok": True}}})
        self.make_run("f-blocked", "r-blocked", status="blocked",
                      extra={"blocked": {"reason": "needs credentials", "at": self.iso(time.time() - 100)}})
        self.make_run("g-paused", "r-paused", status="paused", phase="plan")
        self.make_run("h-nolog", "r-nolog", phase="intake", updated_ago=10, history_ago=45)

    def views(self, **kw):
        return MON.discover(include_all=True, **kw)

    @staticmethod
    def isolated(views, text="working"):
        """The `live` sentence belongs to the activity module: pin it so these tests judge the monitor's own text."""
        for v in views:
            v["live"] = {"level": v["live"]["level"], "text": text}
        return views

    def run_cli(self, code, *args):
        env = dict(os.environ, ORKESTRA_LANG=code, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        env.pop("NO_COLOR", None)
        cp = subprocess.run([sys.executable, str(helpers.BIN), *args], cwd=str(self.tmp), capture_output=True,
                            encoding="utf-8", env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        return cp.stdout


# ================================================================== terminal view

class TestTerminalView(MonitorBase):
    def view(self, **over):
        v = {"project_name": "demo", "run_id": "r1", "title": "Add sub", "size": "M", "mode": "headless",
             "engine": "cursor", "phase": "implement", "phase_index": 5, "phase_since": time.time() - 400,
             "live": {"level": "run", "text": "working"},
             "stall": {"cls": 3, "key": "hung", "text": "hung text", "loops": ["a loop", "b loop", "c loop"]},
             "timing": {"run": 7000, "task_avg": 360, "reopens": 2, "tasks_reopened": 1},
             "tasks_total": 2, "tasks_done": 1, "current_tasks": [{"id": "T001", "evidence": "RG", "title": "Do it"}],
             "gates": {"baseline": "BASE", "verify": "VERIFY"}, "sessions": [{"session": "abc"}, {"session": "def"}],
             "usage": {"input": 10, "output": 2500, "cache_write": 30, "cache_read": 1_200_000, "context": 45200,
                       "final": True},
             "activity": [{"time": "10:00:00", "agent": "main", "kind": "deny", "tool": "Bash",
                           "what": "[roles] nope", "repeat_count": 3},
                          {"time": "10:00:01", "agent": "main", "kind": "deny", "tool": "Bash",
                           "what": "plain denial", "repeat_count": 1},
                          {"time": "10:00:05", "agent": "implementer", "kind": "tool", "tool": "Read",
                           "what": "calc.py"}],
             "ledger": ["one", "two"]}
        v.update(over)
        return v

    def bare(self):
        return self.view(sessions=[], usage=None, activity=[], ledger=[], timing={"run": 0}, tasks_total=0,
                         current_tasks=[], gates={"baseline": "—", "verify": "—"}, phase_since=None,
                         stall={"cls": 0, "key": "working", "text": "ok", "loops": []})

    def test_turkish_view_is_exactly_what_it_always_was(self):
        with lang("tr"):
            text = MON.render_text([self.view()], color=False, width=120)
            bare = MON.render_text([self.bare()], color=False, width=120)
        self.assertEqual(text.splitlines()[1:], [
            " faz    intake › discover › clarify › plan › approve ›  implement  › verify › review › deliver › done"
            " · bu fazda 6 dk",
            " durum  ● working",
            " DİKKAT ASILI KOMUT — hung text",
            " döngü  a loop",
            " döngü  b loop",
            " tempo  toplam 1 sa 56 dk · görev başına 6 dk · inceleme reddi 2 (1 görev)",
            " süre   Toplam 1 sa 56 dk",
            " görev  1/2 bitti · şu an: T001 RG Do it",
            " kapı   başlangıç BASE · doğrulama VERIFY",
            " oturum 2 bağlı (abc, def)",
            " token  girdi 10 · çıktı 2.5K · cache yazma 30 · cache okuma 1.20M · bağlam 45.2K (kapanış değeri)",
            " son hareketler:",
            "   10:00:00 main        ! Koruma ×3       roles koruması",
            "   10:00:01 main        ! Koruma          genel koruması",
            "   10:00:05 implementer   Read            calc.py",
            " günlük two",
        ])
        self.assertTrue(text.startswith("━━ demo · r1 · \"Add sub\" (M, headless, cursor) ━━"))
        self.assertEqual(bare.splitlines()[1:], [
            " faz    intake › discover › clarify › plan › approve ›  implement  › verify › review › deliver › done",
            " durum  ● working",
            " oturum 0 bağlı — bu çalışmayı süren açık Claude oturumu yok",
            "   (aktivite kaydı yok — kayıt, eklentinin bu sürümü yüklendikten sonraki araç çağrılarıyla başlar)",
        ])

    def test_english_view(self):
        with lang("en"):
            text = MON.render_text([self.view()], color=False, width=120)
            bare = MON.render_text([self.bare()], color=False, width=120)
        self.assertEqual(text.splitlines()[1:], [
            " phase   intake › discover › clarify › plan › approve ›  implement  › verify › review › deliver › done"
            " · in this phase for 6m",
            " status  ● working",
            " ALERT HUNG COMMAND: hung text",
            " loop    a loop",
            " loop    b loop",
            " pace    total 1h 56m · per task 6m · review rejects 2 (tasks affected: 1)",
            " clock   Total 1h 56m",
            " tasks   1/2 done · now: T001 RG Do it",
            " gates   baseline BASE · verify VERIFY",
            " session 2 bound (abc, def)",
            " tokens  in 10 · out 2.5K · cache write 30 · cache read 1.20M · context 45.2K (final value)",
            " recent activity:",
            "   10:00:00 main        ! Guard ×3        roles guard",
            "   10:00:01 main        ! Guard           general guard",
            "   10:00:05 implementer   Read            calc.py",
            " ledger  two",
        ])
        self.assertEqual(bare.splitlines()[1:], [
            " phase   intake › discover › clarify › plan › approve ›  implement  › verify › review › deliver › done",
            " status  ● working",
            " session 0 bound: no open agent session is working on this run",
            "   (no activity logged yet: logging starts with the first tool calls after this plugin version is loaded)",
        ])
        self.assertFalse(TR_LETTERS.search(text + bare))
        self.assertNotIn("\u2014", text + bare)

    def test_the_value_column_lines_up_in_both_languages(self):
        for code, width in (("tr", 8), ("en", 9)):
            with lang(code):
                lines = MON.render_text([self.view()], color=False, width=120).splitlines()[1:]
            labelled = [ln for ln in lines if re.match(r" (faz|durum|döngü|tempo|süre|görev|kapı|oturum|token|günlük|"
                                                       r"phase|status|loop|pace|clock|tasks|gates|session|tokens|ledger) ", ln)]
            self.assertGreaterEqual(len(labelled), 9, code)
            columns = {len(re.match(r" \S+\s+", ln).group(0)) for ln in labelled}
            self.assertEqual(columns, {width}, f"{code}: labels are not aligned: {columns}")

    def test_label_width_is_the_widest_label_plus_one(self):
        with lang("tr"):
            self.assertEqual(MON._label_width(), 7)                # " faz    " is the layout the view always had
        with lang("en"):
            self.assertEqual(MON._label_width(), 8)

    def test_stall_banner_marks_in_both_languages(self):
        expect = {"tr": {2: "ÖLÜ BEKLEME", 3: "ASILI KOMUT", 4: "SESSİZ OTURUM"},
                  "en": {2: "DEAD WAIT", 3: "HUNG COMMAND", 4: "SILENT SESSION"}}
        for code, marks in expect.items():
            for cls, mark in marks.items():
                with lang(code):
                    text = MON.render_text([self.view(stall={"cls": cls, "key": "k", "text": "T", "loops": []})],
                                           width=120)
                self.assertIn(f"{mark}", text, (code, cls))
        for cls in (0, 1):                                       # no banner for working / waiting-for-the-user
            for code in ("en", "tr"):
                with lang(code):
                    text = MON.render_text([self.view(stall={"cls": cls, "key": "k", "text": "T", "loops": []})],
                                           width=120)
                self.assertNotIn("ALERT", text)
                self.assertNotIn("DİKKAT", text)

    def test_empty_workspace_message(self):
        with lang("en"):
            text = MON.render_text([])
        self.assertEqual(text.splitlines()[0], "No open orkestra runs. (To also see runs finished in the last 12 hours: "
                                               "`orkestra watch --all`.)")
        self.assertEqual(text.splitlines()[1], f"Workspace: {MON.paths.workspace_display()}")
        self.assertNotRegex(text, r"^Workspace: /(?:Users|home)/")
        with lang("tr"):
            text = MON.render_text([])
        self.assertEqual(text.splitlines()[0], "Açık orkestra çalışması yok. (Son 12 saatte bitenleri de görmek için "
                                               "`orkestra watch --all`.)")
        self.assertEqual(text.splitlines()[1], f"Çalışma alanı: {MON.paths.workspace_display()}")
        self.assertNotRegex(text, r"^Çalışma alanı: /(?:Users|home)/")

    def test_watch_once_in_process_in_both_languages(self):
        self.populate()
        for code in ("en", "tr"):
            buf = io.StringIO()
            with lang(code), contextlib.redirect_stdout(buf):
                self.assertEqual(MON.watch(None, 2.0, True, True, False), 0)
            out = buf.getvalue()
            for rid in ("r-hung", "r-wait", "r-silent", "r-dead", "r-done", "r-blocked", "r-paused", "r-nolog"):
                self.assertIn(rid, out, (code, rid))
            self.assertIn("HUNG COMMAND" if code == "en" else "ASILI KOMUT", out)
            self.assertIn("DEAD WAIT" if code == "en" else "ÖLÜ BEKLEME", out)
            self.assertIn("SILENT SESSION" if code == "en" else "SESSİZ OTURUM", out)
            if code == "en":
                body = "\n".join(ln for ln in out.splitlines() if not ln.startswith(" status"))   # `live` is activity's
                self.assertFalse(TR_LETTERS.search(body), TR_LETTERS.findall(body))
                self.assertIn("Guard ×2", out)
                self.assertIn("review rejects 1 (tasks affected: 1)", out)
                self.assertIn("1/2 done", out)
                self.assertIn("baseline 2/2 passed · verify 1/2 passed ✗ regression: unit, lint", out)
                self.assertIn("tokens  in 10 · out 2.5K", out)
            else:
                self.assertIn("Koruma ×2", out)
                self.assertIn("inceleme reddi 1 (1 görev)", out)
                self.assertIn("1/2 bitti", out)
                self.assertIn("başlangıç 2/2 geçti · doğrulama 1/2 geçti ✗ gerileme: unit, lint", out)

    def test_watch_with_no_runs_in_both_languages(self):
        for code, first in (("en", "No open orkestra runs."), ("tr", "Açık orkestra çalışması yok.")):
            buf = io.StringIO()
            with lang(code), contextlib.redirect_stdout(buf):
                self.assertEqual(MON.watch(None, 2.0, True, False, False), 0)
            self.assertTrue(buf.getvalue().startswith(first), code)

    def test_cli_watch_once_and_json_in_both_languages(self):
        self.populate()
        outs = {code: self.run_cli(code, "watch", "--once", "--all") for code in ("en", "tr")}
        self.assertIn(" phase ", outs["en"])
        self.assertIn(" faz ", outs["tr"])
        self.assertNotIn(" faz ", outs["en"])
        self.assertNotIn(" phase ", outs["tr"])
        body = "\n".join(ln for ln in outs["en"].splitlines() if not ln.startswith(" status"))
        self.assertFalse(TR_LETTERS.search(body), TR_LETTERS.findall(body))
        docs = {code: json.loads(self.run_cli(code, "watch", "--json", "--all")) for code in ("en", "tr")}
        en, tr = docs["en"], docs["tr"]
        self.assertEqual(len(en), 8)
        self.assertEqual([v["run_id"] for v in en], [v["run_id"] for v in tr])
        self.assertEqual(key_paths(en), key_paths(tr), "the JSON keys must not depend on the language")
        for a, b in zip(en, tr):
            for field in ("run_id", "phase", "status", "phase_index", "approved", "engine", "mode", "size", "tasks_done",
                          "tasks_total", "phase_tr", "status_tr"):
                self.assertEqual(a[field], b[field], (a["run_id"], field))
            self.assertEqual((a["stall"]["cls"], a["stall"]["key"], a["live"]["level"]),
                             (b["stall"]["cls"], b["stall"]["key"], b["live"]["level"]), a["run_id"])
            self.assertEqual(set(a["timing"]), set(b["timing"]))
        by_id = {v["run_id"]: v for v in en}
        self.assertEqual((by_id["r-hung"]["phase_label"], by_id["r-hung"]["status_label"]), ("implementation", "active"))
        self.assertEqual(by_id["r-wait"]["status_label"], "waiting for you")
        self.assertEqual(by_id["r-hung"]["gates"], {"baseline": "2/2 passed",
                                                    "verify": "1/2 passed ✗ regression: unit, lint"})
        self.assertEqual(by_id["r-nolog"]["gates"], {"baseline": "—", "verify": "—"})        # sentinel never translated
        tr_by_id = {v["run_id"]: v for v in tr}
        self.assertEqual((tr_by_id["r-hung"]["phase_label"], tr_by_id["r-hung"]["status_label"]), ("uygulama", "aktif"))
        self.assertEqual(tr_by_id["r-hung"]["gates"], {"baseline": "2/2 geçti",
                                                       "verify": "1/2 geçti ✗ gerileme: unit, lint"})
        self.assertEqual(by_id["r-hung"]["stall"]["cls"], 3)
        self.assertEqual(by_id["r-dead"]["stall"]["cls"], 2)
        self.assertEqual(by_id["r-silent"]["stall"]["cls"], 4)
        self.assertEqual(by_id["r-wait"]["stall"]["cls"], 1)
        self.assertTrue(by_id["r-hung"]["stall"]["text"].startswith("hung command (implementer, "))
        self.assertTrue(tr_by_id["r-hung"]["stall"]["text"].startswith("asılı komut (implementer, "))
        self.assertEqual(by_id["r-done"]["usage"]["cache_read"], 1_200_000)

    def test_legacy_turkish_labels_stay_in_the_json_whatever_the_language(self):
        legacy_phase = {"intake": "giriş", "discover": "keşif", "clarify": "netleştirme", "plan": "plan",
                        "approve": "onay", "implement": "uygulama", "verify": "doğrulama", "review": "inceleme",
                        "deliver": "teslim", "done": "bitti"}
        legacy_status = {"active": "aktif", "awaiting_user": "seni bekliyor", "blocked": "bloke",
                         "paused": "duraklatıldı", "done": "bitti", "aborted": "iptal"}
        self.assertEqual(list(legacy_phase), PHASES)
        for code in ("en", "tr"):
            with lang(code):
                for ph, want in legacy_phase.items():
                    st = {"run_id": "x", "phase": ph, "status": "active", "tasks": {}}
                    v = MON.collect(self.ws / "demo", st)
                    self.assertEqual(v["phase_tr"], want, (code, ph))
                for status, want in legacy_status.items():
                    st = {"run_id": "x", "phase": "plan", "status": status, "tasks": {}}
                    self.assertEqual(MON.collect(self.ws / "demo", st)["status_tr"], want, (code, status))

    def test_unknown_phase_and_status_fall_back_to_the_id(self):
        for code in ("en", "tr"):
            with lang(code):
                v = MON.collect(self.ws / "demo", {"run_id": "x", "phase": "weird", "status": "odd", "tasks": {}})
            self.assertEqual((v["phase_label"], v["phase_tr"], v["status_label"], v["status_tr"]),
                             ("weird", "weird", "", ""))
            self.assertEqual(v["phase_index"], 0)

    def test_phase_and_status_names_exist_in_both_catalogs(self):
        for catalog in (MSG_MONITOR.EN, MSG_MONITOR.TR):
            for phase in PHASES:
                self.assertIn(f"monitor.phase_{phase}", catalog)
            for status in ("active", "awaiting_user", "blocked", "paused", "done", "aborted"):
                self.assertIn(f"monitor.status_{status}", catalog)

    def test_gate_summary_words(self):
        gates = {"baseline": {"results": []}, "verify": {"results": [{"ok": True}], "comparison": {"ok": True}}}
        rdir = self.tmp / "gates-run"
        rdir.mkdir()
        for label, data in gates.items():
            (rdir / f"gates-{label}.json").write_text(json.dumps(data), encoding="utf-8")
        with lang("en"):
            self.assertEqual(MON._gate_summary(rdir), {"baseline": "no gates", "verify": "1/1 passed ✓"})
        with lang("tr"):
            self.assertEqual(MON._gate_summary(rdir), {"baseline": "kapı yok", "verify": "1/1 geçti ✓"})
        with lang("en"):
            self.assertEqual(MON._gate_summary(self.tmp / "missing"), {"baseline": "—", "verify": "—"})

    def test_deny_rows_in_both_languages(self):
        rows = [{"time": "10:00:00", "agent": "main", "kind": "deny", "tool": "Bash", "what": "[phase] nope",
                 "repeat_count": 1}]
        with lang("en"):
            text = MON.render_text([self.view(activity=rows)], width=120)
        self.assertIn("! Guard           phase guard", text)
        with lang("tr"):
            text = MON.render_text([self.view(activity=rows)], width=120)
        self.assertIn("! Koruma          phase koruması", text)

    def test_watch_header_and_loop_in_both_languages(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        for code, want in (("en", "every 2s · quit: Ctrl+C"), ("tr", "her 2 sn · çıkış: Ctrl+C")):
            out = Tty()
            with lang(code), mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(MON.time, "sleep", side_effect=KeyboardInterrupt), \
                    mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertEqual(MON.watch(None, 2.0, False, False, False), 0)
            self.assertIn("orkestra watch · ", out.getvalue())
            self.assertIn(want, out.getvalue(), code)
            self.assertTrue(out.getvalue().startswith("\033[H\033[2J"))

    def test_output_survives_a_terminal_that_cannot_hold_every_character(self):
        self.populate()
        for code in ("en", "tr"):
            raw = io.BytesIO()
            ascii_out = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n")
            with lang(code), mock.patch.object(sys, "stdout", ascii_out):
                self.assertEqual(MON.watch(None, 2.0, True, True, False), 0)      # must not raise UnicodeEncodeError
                ascii_out.flush()
            text = raw.getvalue().decode("ascii")
            self.assertIn("r-hung", text)
            self.assertIn("?", text)                                              # the box characters became '?'
            raw = io.BytesIO()
            ascii_out = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n")
            with lang(code), mock.patch.object(sys, "stdout", ascii_out):
                self.assertEqual(MON.watch(None, 2.0, True, True, True), 0)
                ascii_out.flush()
            data = json.loads(raw.getvalue().decode("ascii"))                    # JSON stays valid (\u escapes)
            self.assertEqual(len(data), 8)
            if code == "tr":
                self.assertEqual({v["run_id"]: v for v in data}["r-wait"]["status_tr"], "seni bekliyor")
                self.assertIn("\\u", raw.getvalue().decode("ascii"))

    def test_json_keeps_readable_utf8_when_the_terminal_can_take_it(self):
        self.populate()
        buf = io.StringIO()
        with lang("tr"), contextlib.redirect_stdout(buf):
            MON.watch(None, 2.0, True, True, True)
        self.assertIn("netleştirme", buf.getvalue())
        self.assertNotIn("\\u", buf.getvalue())

    def test_ansi_is_enabled_only_where_windows_needs_it(self):
        for platform, calls in (("win32", 1), ("linux", 0), ("darwin", 0)):
            with mock.patch.object(sys, "platform", platform), mock.patch.object(MON.os, "system") as system:
                MON._enable_ansi()
            self.assertEqual(system.call_count, calls, platform)
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(MON.os, "system", side_effect=OSError):
            MON._enable_ansi()                                                    # never raises

    def test_dashboard_messages_in_both_languages(self):
        class Busy:
            def __init__(self, *a, **kw):
                raise OSError("Address already in use")

        class Quiet:                                    # serves nothing: Ctrl+C right away
            def __init__(self, *a, **kw):
                pass

            def serve_forever(self, poll_interval=0.5):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        port = 48123
        for code, busy_text, up_text in (
                ("en", "could not open 127.0.0.1:48123 (Address already in use). It may already be running: "
                       "http://127.0.0.1:48123/ · or use another port: `orkestra dashboard --port <n>`",
                 "orkestra dashboard → http://127.0.0.1:48123/  (this computer only · Pause and Abort are on, `--read-only` "
                 "turns them off · stop: Ctrl+C)"),
                ("tr", "127.0.0.1:48123 açılamadı (Address already in use). Zaten açık olabilir: "
                       "http://127.0.0.1:48123/ \u2014 ya da başka port: `orkestra dashboard --port <n>`",
                 "orkestra dashboard → http://127.0.0.1:48123/  (yalnızca bu bilgisayar · Duraklat ve İptal et açık, "
                 "`--read-only` kapatır · kapatmak: Ctrl+C)")):
            err, out = io.StringIO(), io.StringIO()
            with lang(code), mock.patch("http.server.ThreadingHTTPServer", Busy), \
                    mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out), \
                    mock.patch.dict(os.environ, {"CLAUDECODE": ""}):
                self.assertEqual(MON.dashboard(None, port, False), 2)
            self.assertEqual(err.getvalue(), "orkestra: " + busy_text + "\n", code)
            self.assertEqual(out.getvalue(), "")
            err, out = io.StringIO(), io.StringIO()
            with lang(code), mock.patch("http.server.ThreadingHTTPServer", Quiet), \
                    mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", out), \
                    mock.patch.dict(os.environ, {"CLAUDECODE": ""}):
                self.assertEqual(MON.dashboard(None, port, False), 0)
            self.assertEqual(out.getvalue(), up_text + "\n", code)
            ro = io.StringIO()
            with lang(code), mock.patch("http.server.ThreadingHTTPServer", Quiet), \
                    mock.patch.object(sys, "stdout", ro), mock.patch.dict(os.environ, {"CLAUDECODE": ""}):
                self.assertEqual(MON.dashboard(None, port, False, read_only=True), 0)
            self.assertIn("read-only" if code == "en" else "salt-okunur", ro.getvalue(), code)
            if code == "en":
                self.assertFalse(TR_LETTERS.search(err.getvalue() + out.getvalue()))

    def test_dashboard_refuses_to_block_an_agent_in_both_languages(self):
        for code in ("en", "tr"):
            err = io.StringIO()
            with lang(code), mock.patch.object(sys, "stderr", err), mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
                self.assertEqual(MON.dashboard(None, 48123, False), 2)
            self.assertIn("orkestra watch --once", err.getvalue())               # model-facing: English either way


# ================================================================== dashboard page

class TestDashboardPage(MonitorBase):
    def page(self, code):
        with lang(code):
            return MON.page_html()

    @staticmethod
    def labels_of(page):
        m = re.search(r'<script type="application/json" id="labels">(.*?)</script>', page, re.S)
        assert m, "no label table in the page"
        return json.loads(m.group(1))

    @staticmethod
    def script_of(page):
        return re.findall(r"<script>(.*?)</script>", page, re.S)[-1]

    def test_page_language_attribute_title_and_static_texts(self):
        en, tr = self.page("en"), self.page("tr")
        self.assertIn('<html lang="en">', en)
        self.assertIn('<html lang="tr">', tr)
        self.assertIn("<title>Orkestra · live</title>", en)
        self.assertIn("<h1>Orkestra</h1>", en)
        self.assertIn('<span id="conn-text">loading…</span>', en)
        self.assertIn('Include runs older than 12 hours</span></label>', en)       # was "show finished runs too"
        self.assertIn("<title>Orkestra · canlı</title>", tr)                       # the legacy title, without the em dash
        self.assertIn("<h1>Orkestra</h1>", tr)
        self.assertIn('<span id="conn-text">yükleniyor…</span>', tr)
        self.assertIn("12 saatten eski çalışmaları da göster</span></label>", tr)  # was "bitenleri de göster"
        for page, code in ((en, "en"), (tr, "tr")):                                # the switch is plain links: it works without JS
            self.assertIn('href="?lang=en"', page)
            self.assertIn('href="?lang=tr"', page)
            seg = re.search(r'<span class="seg".*?</span>', page, re.S).group(0)
            self.assertEqual(seg.count('aria-current="true"'), 1)
            self.assertRegex(seg, rf'id="lang-{code}"[^>]*aria-current="true"')

    def test_english_page_holds_no_turkish_text_at_all(self):
        en = self.page("en")
        self.assertEqual(TR_LETTERS.findall(en), [])
        self.assertNotIn("—", en)                      # new English copy: no em dash
        for word in ("canlı", "yükleniyor", "bitenleri", "görev", "kapı", "oturum", "Koruma", "tempo:"):
            self.assertNotIn(word, en)

    def test_turkish_page_keeps_the_legacy_labels(self):
        labels = self.labels_of(self.page("tr"))
        self.assertEqual(labels["stall_mark_2"], "ÖLÜ BEKLEME")
        self.assertEqual(labels["stall_mark_3"], "ASILI KOMUT")
        self.assertEqual(labels["stall_mark_4"], "SESSİZ OTURUM")
        self.assertEqual((labels["ui_tasks"], labels["ui_gates"], labels["ui_sessions"], labels["ui_no_sessions"]),
                         ("görevler", "kapılar", "bağlı oturum", "bağlı Claude oturumu yok"))
        self.assertEqual(labels["ui_empty"], "Açık orkestra çalışması yok.")
        self.assertEqual(labels["ui_offline"], "bağlantı yok: `orkestra dashboard` çalışıyor mu?")
        self.assertEqual(labels["ui_locale"], "tr-TR")
        self.assertEqual(labels["dur_s"] + labels["dur_m"] + labels["dur_hm"], "{n} sn{n} dk{h} sa {m} dk")

    def test_label_table_is_valid_json_with_the_same_keys_in_both_languages(self):
        en, tr = self.labels_of(self.page("en")), self.labels_of(self.page("tr"))
        names = {key.split(".", 1)[1] for key in MON.UI_KEYS}
        self.assertEqual(set(en), names)
        self.assertEqual(set(tr), names)
        for name in names:
            self.assertIsInstance(en[name], str)
            self.assertTrue(en[name].strip(), name)
            self.assertEqual(i18n.fields(en[name]), i18n.fields(tr[name]), f"{name}: {{fields}} differ")
        self.assertEqual(en["ui_locale"], "en-US")
        self.assertEqual(len(MON.UI_KEYS), len(set(MON.UI_KEYS)))

    def test_page_script_uses_exactly_the_labels_it_is_given(self):
        page = self.page("en")
        script = self.script_of(page)
        used = set(re.findall(r"\bL\.([A-Za-z0-9_]+)", script))
        have = set(self.labels_of(page))
        self.assertEqual(used - have, set(), "the script reads labels that the server does not send")
        families = ("stall_mark_", "phase_", "ui_g_", "ui_k_", "ui_ts_", "ui_step_", "ui_tests_", "lvl_", "ui_job_status_",
                    "ui_doc_")   # L["<family>" + id]
        dynamic = {n for n in have if n.startswith(families)}
        for family in families:
            self.assertIn(f'L["{family}"', script)
        self.assertEqual(have - used - dynamic, set(), "labels that nothing uses")
        self.assertEqual(TR_LETTERS.findall(script), [])
        for placeholder in ("__LANG__", "__TITLE__", "__LOADING__", "__OLDER__", "__LABELS__", "__META__", "__BUILD__",
                            "__SKIP__", "__SEARCH__", "__SEARCH_PH__", "__LANG_EN__", "__LANG_TR__", "__LANG_LABEL__"):
            self.assertNotIn(placeholder, page)

    def test_label_table_cannot_break_out_of_the_script_element(self):
        cats = {code: dict(i18n.catalog(code)) for code in ("en", "tr")}
        evil = "x</script><script>alert(1)</script><!-- &     'q' \"d\""
        cats["en"]["monitor.ui_empty"] = evil
        cats["en"]["monitor.ui_title"] = "<b>T&C</b>"
        with mock.patch.object(i18n, "_catalogs", cats), lang("en"):
            page = MON.page_html()
        self.assertEqual(page.count("</script>"), 2)                                # the label block + the page script
        self.assertEqual(page.count("<script"), 2)
        self.assertEqual(self.labels_of(page)["ui_empty"], evil)                   # still decodes to the original
        self.assertIn("<title>&lt;b&gt;T&amp;C&lt;/b&gt;</title>", page)

    def test_unsupported_language_gets_the_english_page_and_config_is_honoured(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "de"}):
            self.assertIn('<html lang="en">', MON.page_html())
        env = {k: v for k, v in os.environ.items() if k != "ORKESTRA_LANG"}
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"lang": "tr"}), encoding="utf-8")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIn('<html lang="tr">', MON.page_html())

    def test_catalog_and_page_agree_on_the_ui_keys(self):
        for key in MON.UI_KEYS:
            self.assertIn(key, i18n.catalog("en"), key)
            self.assertIn(key, i18n.catalog("tr"), key)
        for key in ("monitor.ui_title", "monitor.ui_loading", "monitor.ui_older", "monitor.ui_skip", "monitor.ui_search",
                    "monitor.ui_search_ph", "monitor.ui_lang_label", "monitor.ui_lang_en", "monitor.ui_lang_tr"):   # server-side
            self.assertIn(key, i18n.catalog("en"))
            self.assertNotIn(key, MON.UI_KEYS)

    # ---- over HTTP: the language is resolved per request

    def serve(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", port), MON.make_handler(None, port))
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            srv.shutdown()
            srv.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{port}"

    def test_http_serves_the_page_and_api_in_the_current_language(self):
        self.populate()
        base = self.serve()
        for code, html_lang, title in (("en", "en", "Orkestra · live"), ("tr", "tr", "Orkestra · canlı"),
                                       ("en", "en", "Orkestra · live")):           # switching back and forth, no restart
            with lang(code):
                resp = urllib.request.urlopen(base + "/", timeout=5)
                self.assertEqual(resp.headers["Content-Type"], "text/html; charset=utf-8")
                page = resp.read().decode("utf-8")
                api = json.loads(urllib.request.urlopen(base + "/api/runs?all=1", timeout=5).read().decode("utf-8"))
            self.assertIn(f'<html lang="{html_lang}">', page)
            self.assertIn(f"<title>{title}</title>", page)
            by_id = {v["run_id"]: v for v in api}
            self.assertEqual(by_id["r-hung"]["stall"]["text"].split(" (")[0],
                             "hung command" if code == "en" else "asılı komut")
            self.assertEqual(by_id["r-hung"]["gates"]["baseline"], "2/2 passed" if code == "en" else "2/2 geçti")
        req = urllib.request.Request(base + "/", headers={"Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        cm.exception.close()
        self.assertEqual(cm.exception.code, 403)                                    # DNS-rebinding guard unchanged


# ================================================================== the page script, run for real

EXPAND_ALL = {"op": "eval", "code": "H.find(H.byId('main'), n => n.dataset && n.dataset.act === 'expand')"
                                     ".forEach(b => b.fire('click'));"}      # paused / finished rows open their full card


@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestDashboardScript(MonitorBase):
    def run_page(self, code, views, fail=False, build="", headers=None, steps=()):
        """The harness output of the page script run against `views` (server answers carrying `headers`)."""
        with lang(code):
            page = MON.page_html(build)
        return run_dashboard(self.tmp, page, views, [dict(s) for s in steps] + [{"op": "snap", "as": "snap"}],
                             headers=headers, build="", fail=fail)

    def render(self, code, views, fail=False):
        """(connection chip text, {run_id: card text}, snapshot) of the page script run against `views`."""
        out = self.run_page(code, views, fail, steps=[EXPAND_ALL])
        self.assertEqual(out["errors"], [], out["errors"])
        snap = out["results"]["snap"]
        return snap["conn"], card_texts(snap, views), snap

    def test_the_page_reloads_itself_when_the_server_runs_another_build(self):
        for code in ("en", "tr"):
            same = self.run_page(code, [], build="abc", headers={"X-Orkestra-Build": "abc", "X-Orkestra-Stale": "0"})
            self.assertEqual((same["reloads"], same["results"]["snap"]["banners"]["stale"]), (0, False), code)
            newer = self.run_page(code, [], build="abc", headers={"X-Orkestra-Build": "def", "X-Orkestra-Stale": "0"})
            self.assertEqual(newer["reloads"], 1, code)
            self.assertEqual(newer["results"]["snap"]["sections"], [], "nothing is rendered from the old script once a reload is due")
            plain = self.run_page(code, [], build="", headers={"X-Orkestra-Build": "def"})         # page without a build id
            self.assertEqual(plain["reloads"], 0)

    def test_the_page_says_so_when_the_code_on_disk_moved_on(self):
        for code in ("en", "tr"):
            with lang(code):
                want = i18n.t("monitor.ui_stale")
            out = self.run_page(code, [], build="abc", headers={"X-Orkestra-Build": "abc", "X-Orkestra-Stale": "1"})
            banners = out["results"]["snap"]["banners"]
            self.assertEqual((out["reloads"], banners["stale"], banners["staleMsg"]), (0, True, want), code)
            self.assertIn("orkestra dashboard --open", want)

    def test_the_script_renders_every_card_in_both_languages(self):
        self.populate()
        with lang("en"):                                 # the server computes the views in the language it serves
            base_en = self.isolated(self.views())
        with lang("tr"):
            base_tr = self.isolated(self.views())
        en_meta, en, _ = self.render("en", base_en)
        tr_meta, tr, _ = self.render("tr", base_tr)
        self.assertEqual(sorted(en), sorted(tr))
        self.assertEqual(len(en), 8)
        self.assertEqual(en_meta, "Live \u00b7 updated just now", en_meta)
        self.assertEqual(tr_meta, "Canl\u0131 \u00b7 g\u00fcncellendi az \u00f6nce", tr_meta)
        hung_en, hung_tr = en["r-hung"], tr["r-hung"]
        # the legacy technical lines live in the Details of the card, with the same templates as ever
        self.assertIn("HUNG COMMAND: hung command (implementer, ", hung_en)
        self.assertIn("ASILI KOMUT: as\u0131l\u0131 komut (implementer, ", hung_tr)
        self.assertIn("pace: total ", hung_en)
        self.assertIn("tempo: toplam ", hung_tr)
        self.assertIn(" \u00b7 per task ", hung_en)
        self.assertIn(" \u00b7 g\u00f6rev ba\u015f\u0131na ", hung_tr)
        self.assertIn("review rejects 1 (tasks affected: 1)", hung_en)
        self.assertIn("inceleme reddi 1 (1 g\u00f6rev)", hung_tr)
        self.assertIn("1/2 done", hung_en)                                           # was "tasks: 1/2 done": the task line
        self.assertIn("1/2 bitti", hung_tr)
        self.assertIn("gates: baseline 2/2 passed \u00b7 verify 1/2 passed \u2717 regression: unit, lint", hung_en)
        self.assertIn("kap\u0131lar: ba\u015flang\u0131\u00e7 2/2 ge\u00e7ti \u00b7 do\u011frulama 1/2 ge\u00e7ti \u2717 gerileme: unit, lint", hung_tr)
        self.assertIn("Verify 1/2 passed \u2717 regression: unit, lint", hung_en)    # the chip that says it at a glance
        self.assertIn("bound sessions: sess-r-h", hung_en)
        self.assertIn("ba\u011fl\u0131 oturum: sess-r-h", hung_tr)
        self.assertIn("ledger: second note", hung_en)
        self.assertIn("g\u00fcnl\u00fck: second note", hung_tr)
        self.assertIn("tokens: n/a (the bound session's log was not found)", hung_en)
        self.assertIn("token: yok (ba\u011fl\u0131 oturumun kayd\u0131 bulunamad\u0131)", hung_tr)
        # a guard that blocked the same call twice is ONE row (was a table row "Guard x2 | roles guard . 2 repeats")
        self.assertIn("Guard \u00d72", hung_en)
        self.assertIn("roles guard", hung_en)
        self.assertIn("Agent is read-only", hung_en)
        self.assertIn("Koruma \u00d72", hung_tr)
        self.assertIn("roles korumas\u0131", hung_tr)
        self.assertIn("general guard", hung_en)                                      # the bracket-less denial
        self.assertIn("no bracket denial", hung_en)
        self.assertIn("genel korumas\u0131", hung_tr)
        self.assertIn("DEAD WAIT: dead wait: a subagent finished ", en["r-dead"])
        self.assertIn("\u00d6L\u00dc BEKLEME: \u00f6l\u00fc bekleme \u2014 alt-ajan ", tr["r-dead"])
        self.assertIn("SILENT SESSION: silent session: ", en["r-silent"])
        self.assertIn("SESS\u0130Z OTURUM: sessiz oturum \u2014 ", tr["r-silent"])
        self.assertIn("no bound agent session", en["r-wait"])
        self.assertIn("ba\u011fl\u0131 Claude oturumu yok", tr["r-wait"])
        self.assertIn("No activity recorded yet. Logging starts with the first tool call.", en["r-blocked"])  # was ui_no_activity
        self.assertIn("Hen\u00fcz hareket kayd\u0131 yok. Kay\u0131t ilk ara\u00e7 \u00e7a\u011fr\u0131s\u0131yla ba\u015flar.", tr["r-blocked"])
        done_en, done_tr = en["r-done"], tr["r-done"]
        self.assertIn("tokens: in 10 \u00b7 out 2.5K \u00b7 cache write 30 \u00b7 cache read 1.20M \u00b7 context 45.2K (final value)", done_en)
        self.assertIn("token: girdi 10 \u00b7 \u00e7\u0131kt\u0131 2.5K \u00b7 cache yazma 30 \u00b7 cache okuma 1.20M \u00b7 ba\u011flam 45.2K (kapan\u0131\u015f de\u011feri)",
                      done_tr)
        # run ids, sizes, engines and modes are protocol values: identical in both languages
        self.assertIn("r-hung \u00b7 M \u00b7 cursor \u00b7 headless", hung_en)
        self.assertIn("r-hung \u00b7 M \u00b7 cursor \u00b7 headless", hung_tr)
        # no Turkish letters anywhere in the English cards (the fixtures use plain English text)
        joined = "\n".join(en.values())
        self.assertEqual(TR_LETTERS.findall(joined), [], joined)

    def test_the_script_formats_durations_like_the_server(self):
        template = {"project": "/p", "project_name": "p", "run_id": "r", "title": "t", "size": "M", "mode": "interactive",
                    "engine": "claude", "phase_index": 0, "live": {"level": "run", "text": "x"}, "stall": {"cls": 0},
                    "flags": [], "tasks_total": 0, "tasks": [], "gates": {"baseline": "\u2014", "verify": "\u2014"},
                    "sessions": [], "usage": None, "ledger": [], "activity": [], "artifacts": "/x"}
        seconds = (1, 45, 59, 60, 64, 89, 90, 125, 3599, 3600, 7200, 7500, 86399, 86400, 90000, 172800)
        cards = [dict(template, run_id=f"r{n}", key=f"k{n}", timing={"run": n, "task_avg": n, "reopens": 0, "tasks_reopened": 0})
                 for n in seconds]
        for code, label in (("en", "pace: total "), ("tr", "tempo: toplam ")):
            _, texts, _ = self.render(code, cards)                      # views from an older server: no attention object
            for n in seconds:
                with lang(code):
                    want = f"{label}{MON._hum(n)} \u00b7 " + ("per task " if code == "en" else "g\u00f6rev ba\u015f\u0131na ") \
                        + MON._hum(n)
                self.assertIn(want, texts[f"r{n}"], (code, n))

    def test_the_empty_and_error_states(self):
        for code, empty, offline, chip, calm in (
                ("en", "No runs yet", "no connection: is `orkestra dashboard` still running?", "Offline",
                 "No open orkestra runs."),
                ("tr", "Hen\u00fcz \u00e7al\u0131\u015fma yok", "ba\u011flant\u0131 yok: `orkestra dashboard` \u00e7al\u0131\u015f\u0131yor mu?",
                 "\u00c7evrimd\u0131\u015f\u0131", "A\u00e7\u0131k orkestra \u00e7al\u0131\u015fmas\u0131 yok.")):
            meta, texts, snap = self.render(code, [])
            self.assertEqual((texts, snap["sections"]), ({}, []), code)
            self.assertIn(empty, snap["other"][0])
            self.assertTrue(meta.startswith("Live" if code == "en" else "Canl\u0131"), meta)
            meta, _, snap = self.render(code, [], fail=True)
            self.assertEqual(meta, chip, code)
            self.assertTrue(snap["banners"]["offline"], code)
            self.assertEqual(snap["banners"]["offlineText"], offline, code)
        # only finished runs: "no open runs" (the legacy sentence) instead of "nothing needs you"
        self.populate()
        with lang("tr"):
            done_only = [v for v in self.views() if v["status"] == "done"]
        _, _, snap = self.render("tr", done_only)
        self.assertEqual(snap["calm"], "A\u00e7\u0131k orkestra \u00e7al\u0131\u015fmas\u0131 yok.")


# ================================================================== catalog hygiene for this area

class TestCatalogs(unittest.TestCase):
    SOURCES = [LIB / "monitor.py", LIB / "tempo.py", LIB / "i18n.py", LIB / "console.py"]

    def literals(self):
        used, prefixes = set(), set()
        for path in self.SOURCES:
            text = path.read_text(encoding="utf-8")
            for key, tail in re.findall(r"""["']((?:monitor|tempo)\.[a-z0-9_]+)(["'{])""", text):
                (prefixes if key.endswith("_") or tail == "{" else used).add(key)
        return used, prefixes

    def test_every_key_the_code_asks_for_exists_in_both_languages(self):
        used, _ = self.literals()
        self.assertTrue(used)
        for key in used:
            self.assertIn(key, MSG_MONITOR.EN if key.startswith("monitor.") else MSG_TEMPO.EN, key)
            self.assertIn(key, MSG_MONITOR.TR if key.startswith("monitor.") else MSG_TEMPO.TR, key)

    def test_no_catalog_key_is_dead(self):
        used, prefixes = self.literals()
        dead = [k for k in list(MSG_MONITOR.EN) + list(MSG_TEMPO.EN)
                if k not in used and not any(k.startswith(p) for p in prefixes)]
        self.assertEqual(dead, [], "catalog entries nothing uses")

    def test_both_languages_hold_the_same_keys_and_fields(self):
        for en, tr in ((MSG_MONITOR.EN, MSG_MONITOR.TR), (MSG_TEMPO.EN, MSG_TEMPO.TR)):
            self.assertEqual(sorted(set(en) ^ set(tr)), [])
            for key in en:
                self.assertEqual(i18n.fields(en[key]), i18n.fields(tr[key]), key)

    def test_english_copy_has_no_turkish_letters_and_no_em_dash(self):
        for catalog in (MSG_MONITOR.EN, MSG_TEMPO.EN):
            for key, text in catalog.items():
                self.assertFalse(TR_LETTERS.search(text), key)
                self.assertNotIn("—", text, key)

    def test_monitor_and_the_stall_text_hold_no_turkish_letters_in_code(self):
        import inspect
        owned = {"monitor.py": (LIB / "monitor.py").read_text(encoding="utf-8"),
                 "tempo.classify": inspect.getsource(T.classify), "tempo.dur_tr": inspect.getsource(T.dur_tr)}
        for name, text in owned.items():
            self.assertEqual(TR_LETTERS.findall(text), [], f"{name} still holds Turkish text: move it to messages/")

    def test_catalogs_work_when_the_other_language_is_missing_a_key(self):
        cats = {"en": dict(i18n.catalog("en")), "tr": dict(i18n.catalog("tr"))}
        del cats["tr"]["tempo.stall_working"]
        with mock.patch.object(i18n, "_catalogs", cats), lang("tr"):
            self.assertEqual(T.classify({"status": "active", "tasks": {}}, [row(1, "tool", tool="Read", what="x")],
                                        now=2.0, wd=WD)["text"], "working")


class TestDashboardFreshness(unittest.TestCase):
    """A dashboard started days ago must notice that Orkestra was updated (it served the old page for days)."""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="orkestra-fresh-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "pkg"
        (self.pkg / "sub").mkdir(parents=True)
        (self.pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
        (self.pkg / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
        patcher = mock.patch.object(MON, "_CODE_DIR", self.pkg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, rel, text):
        p = self.pkg / rel
        p.write_text(text, encoding="utf-8")
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))        # a coarse file system still sees a change

    def test_signature_and_build_id_follow_the_sources(self):
        sig = MON.code_signature()
        self.assertEqual([name for name, _, _ in sig], ["a.py", os.path.join("sub", "b.py")])
        self.assertEqual(MON.build_id(sig), MON.build_id(MON.code_signature()))
        self.assertRegex(MON.build_id(sig), r"^[0-9a-f]{12}$")
        self.touch("a.py", "x = 22\n")
        self.assertNotEqual(MON.build_id(sig), MON.build_id(MON.code_signature()))
        (self.pkg / "notes.txt").write_text("not code", encoding="utf-8")
        self.assertEqual(len(MON.code_signature()), 2, "only python modules count")

    def test_freshness_reports_stale_once_the_disk_moved_on(self):
        fresh = MON._Freshness()
        self.assertFalse(fresh.stale())
        self.touch("sub/b.py", "y = 3\n")
        with mock.patch.object(MON.time, "monotonic", return_value=time_far()):       # the 1 s cache has expired
            self.assertTrue(fresh.stale())
        self.assertEqual(fresh.build, MON.build_id(fresh.started), "the running build never changes")

    def test_a_broken_save_does_not_count_as_a_reloadable_update(self):
        old = MON.code_signature()
        self.touch("a.py", "def broken(:\n")
        self.assertFalse(MON.sources_compile(old, MON.code_signature()))
        self.touch("a.py", "x = 4\n")
        self.assertTrue(MON.sources_compile(old, MON.code_signature()))
        self.assertTrue(MON.sources_compile(old, old), "nothing changed: nothing to compile")
        (self.pkg / "a.py").write_bytes(b"\xff\xfe not utf-8")
        self.assertFalse(MON.sources_compile(old, MON.code_signature()))

    def test_http_answers_carry_the_build_and_the_stale_flag(self):
        from http.server import ThreadingHTTPServer
        fresh = MON._Freshness()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        srv = ThreadingHTTPServer(("127.0.0.1", port), MON.make_handler(None, port, fresh))
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), thread.join(5)))
        base = f"http://127.0.0.1:{port}"
        with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.tmp / "home"), "ORKESTRA_WORKSPACE": str(self.tmp)}):
            resp = urllib.request.urlopen(base + "/api/runs", timeout=5)
            self.assertEqual((resp.headers["X-Orkestra-Build"], resp.headers["X-Orkestra-Stale"]), (fresh.build, "0"))
            page = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
            self.assertIn(f'const BUILD="{fresh.build}"', page)
            self.touch("a.py", "x = 5\n")
            with mock.patch.object(MON.time, "monotonic", return_value=time_far()):
                resp = urllib.request.urlopen(base + "/api/runs", timeout=5)
            self.assertEqual((resp.headers["X-Orkestra-Build"], resp.headers["X-Orkestra-Stale"]), (fresh.build, "1"))
        # a handler built without freshness (older callers) sends neither header
        plain = MON.make_handler(None, port)
        self.assertTrue(callable(plain))

    def test_the_watcher_stops_the_server_for_a_good_update_and_ignores_a_broken_one(self):
        class Srv:
            stopped = 0

            def shutdown(self):
                self.stopped += 1

        fresh = MON._Freshness()
        srv, flag = Srv(), threading.Event()
        worker = threading.Thread(target=MON._watch_code, args=(srv, fresh, flag, 0.02), daemon=True)
        worker.start()
        self.touch("a.py", "def broken(:\n")
        time.sleep(0.2)
        self.assertFalse(flag.is_set(), "a half-saved file keeps the old dashboard serving")
        self.touch("a.py", "x = 6\n")
        worker.join(5)
        self.assertTrue(flag.is_set())
        self.assertEqual(srv.stopped, 1)

    def test_reexec_is_posix_only_and_can_be_switched_off(self):
        script = self.tmp / "orkestra"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        with mock.patch.object(sys, "argv", [str(script), "dashboard"]):
            with mock.patch.object(MON.os, "name", "posix"):
                self.assertTrue(MON._can_reexec())
                with mock.patch.dict(os.environ, {"ORKESTRA_DASHBOARD_NO_RELOAD": "1"}):
                    self.assertFalse(MON._can_reexec())
            with mock.patch.object(MON.os, "name", "nt"):
                self.assertFalse(MON._can_reexec(), "Windows: the page banner tells the user instead")
        with mock.patch.object(sys, "argv", ["-c"]), mock.patch.object(MON.os, "name", "posix"):
            self.assertFalse(MON._can_reexec(), "no script to run again")

    def test_dashboard_re_executes_itself_after_an_update(self):
        import functools
        script = self.tmp / "orkestra"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        calls = []

        def fake_exec(path, argv):
            calls.append((path, argv, os.environ.get(MON.REEXEC_ENV)))
            raise SystemExit(0)                          # stands in for the process image being replaced

        updater = threading.Timer(0.4, self.touch, args=("a.py", "x = 7\n"))
        updater.start()
        self.addCleanup(updater.cancel)
        env = {"ORKESTRA_HOME": str(self.tmp / "home"), "ORKESTRA_WORKSPACE": str(self.tmp)}
        quick = functools.partial(MON._watch_code, interval=0.05)
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", [str(script), "dashboard", "--open"]), \
                mock.patch.object(MON.os, "execv", fake_exec), mock.patch.object(MON, "_watch_code", quick), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            os.environ.pop("CLAUDECODE", None)           # an agent shell refuses to start a server (restored on exit)
            with self.assertRaises(SystemExit):
                MON.dashboard(None, port, False)
            flag_in_env = os.environ.get(MON.REEXEC_ENV)
        self.assertEqual(len(calls), 1)
        path, argv, flag = calls[0]
        self.assertEqual(path, sys.executable)
        self.assertEqual(argv, [sys.executable, str(script), "dashboard", "--open"])
        self.assertEqual(flag, "1", "the new process must not open a second browser tab")
        self.assertEqual(flag_in_env, "1")
        self.assertIn(i18n.t("monitor.dashboard_reload"), out.getvalue())
        for code in ("en", "tr"):
            with lang(code):
                self.assertTrue(i18n.t("monitor.dashboard_reload").startswith("orkestra dashboard: "), code)

    def test_a_re_executed_dashboard_binds_patiently_and_does_not_open_a_browser(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(1)                               # the previous incarnation still holds the port
            threading.Timer(0.6, sock.close).start()
            env = {"ORKESTRA_HOME": str(self.tmp / "home"), "ORKESTRA_WORKSPACE": str(self.tmp), MON.REEXEC_ENV: "1",
                   "ORKESTRA_DASHBOARD_NO_RELOAD": "1"}
            opened = []
            with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"webbrowser": mock.Mock(open=opened.append)}), \
                    mock.patch("http.server.ThreadingHTTPServer.serve_forever", side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(io.StringIO()):
                os.environ.pop("CLAUDECODE", None)
                self.assertEqual(MON.dashboard(None, port, True), 0)
        self.assertEqual(opened, [])


def time_far():
    return time.monotonic() + 3600


if __name__ == "__main__":
    unittest.main()
