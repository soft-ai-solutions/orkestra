"""Localization of the hook layer, the activity feed and the watchdog (English + Turkish), and the portability of the
hooks (Windows has no SIGALRM, `ps`, `pgrep`; pipes carry a legacy code page).

The suite is pinned to Turkish (helpers.py); everything here selects the language explicitly. Turkish expectations are
the pre-localisation wording, word for word: a legacy run keeps reading the same lines. English output must hold no
Turkish letters except where a user's own text is echoed."""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import ntpath
import os
import re
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import activity as A
from orkestra_core import i18n
from orkestra_core import watchdog as W
from test_engines import CursorBase
from test_flow import FlowBase

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
EM_DASH = "\u2014"
AREAS = ("dispatch", "activity", "watchdog")
PLUGIN = helpers.PLUGIN
LIB = PLUGIN / "lib" / "orkestra_core"
OWNED_SOURCES = [PLUGIN / "hooks" / "dispatch.py", PLUGIN / "hooks" / "cursor_hook.py",
                 PLUGIN / "hooks" / "copilot_hook.py", LIB / "activity.py", LIB / "watchdog.py",
                 LIB / "hookmain.py", LIB / "hookboot.py"]       # hookmain holds the code dispatch.py used to hold
NOW = 1_000_000.0


def lang(code: str):
    """Hooks, the CLI and this process all read ORKESTRA_LANG from the environment."""
    return mock.patch.dict(os.environ, {"ORKESTRA_LANG": code})


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(ago: float, kind: str, tool: str = "", what: str = "", agent: str = "main") -> dict:
    return {"ts": NOW - ago, "kind": kind, "tool": tool, "what": what, "agent": agent}


# ======================================================================= the catalogs

class TestCatalogs(unittest.TestCase):
    def table(self, code: str) -> dict:
        return {k: v for k, v in i18n.catalog(code).items() if k.split(".")[0] in AREAS}

    def test_both_languages_hold_the_same_keys_and_fields(self):
        en, tr = self.table("en"), self.table("tr")
        self.assertTrue(en)
        self.assertEqual(sorted(set(en) ^ set(tr)), [])
        for area in AREAS:
            self.assertTrue([k for k in en if k.startswith(area + ".")], f"no {area}.* messages")
        for key in en:
            self.assertEqual(i18n.fields(en[key]), i18n.fields(tr[key]), key)

    def test_every_message_renders_in_both_languages_without_stray_braces(self):
        for code in ("en", "tr"):
            for key, text in self.table(code).items():
                kw = {f: f"<{f}>" for f in i18n.fields(text)}
                with lang(code):
                    out = i18n.t(key, **kw)
                self.assertTrue(out.strip(), (code, key))
                self.assertNotRegex(out, r"[{}]", (code, key, out))
                for f in kw:
                    self.assertIn(f"<{f}>", out, (code, key))

    def test_english_has_no_turkish_letters_and_no_em_dash(self):
        for key, text in self.table("en").items():
            self.assertNotRegex(text, TR_LETTERS, key)
            self.assertNotIn(EM_DASH, text, key)

    def test_turkish_is_a_real_translation(self):
        en, tr = self.table("en"), self.table("tr")
        same = [k for k in en if en[k] == tr[k] and len(re.sub(r"\{\w+\}|[^A-Za-z]", "", en[k])) > 6]
        self.assertEqual(same, [])

    def test_the_sources_use_exactly_the_catalog_keys(self):
        """A key used in code but missing from the catalog renders as the key itself; a catalog key nobody uses is rot."""
        used = set()
        for path in OWNED_SOURCES:
            text = path.read_text(encoding="utf-8")
            used |= {m for m in re.findall(r"""["']((?:%s)\.[a-z][a-z0-9_]*)["']""" % "|".join(AREAS), text)
                     if m.rsplit(".", 1)[-1] not in ("py", "json", "jsonl", "log")}       # file names, not messages
        catalog = set(self.table("en"))
        self.assertEqual(sorted(used - catalog), [], "keys used in code but not in a catalog")
        everything = "\n".join(p.read_text(encoding="utf-8") for root in (LIB, PLUGIN / "hooks")
                               for p in root.rglob("*.py") if "messages" not in p.parts)
        dead = sorted(k for k in catalog if f'"{k}"' not in everything and f"'{k}'" not in everything)
        self.assertEqual(dead, [], "catalog keys no code refers to")

    def test_every_call_passes_exactly_the_fields_of_its_message(self):
        """i18n.t() never raises: a missing field would leave `{who}` in the text a user reads."""
        en = self.table("en")
        checked = 0
        for path in OWNED_SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                f = node.func
                is_t = (isinstance(f, ast.Name) and f.id in ("t", "_t")) or (isinstance(f, ast.Attribute) and f.attr == "t")
                if not is_t:
                    continue
                first = node.args[0]
                keys = [first.value] if isinstance(first, ast.Constant) else \
                    [x.value for x in (first.body, first.orelse) if isinstance(x, ast.Constant)] if isinstance(first, ast.IfExp) else []
                for key in keys:
                    if not isinstance(key, str) or key not in en:
                        continue
                    passed = sorted(k.arg for k in node.keywords if k.arg)
                    self.assertEqual(passed, i18n.fields(en[key]), f"{path.name}:{node.lineno} {key}")
                    checked += 1
        self.assertGreater(checked, 45)

    def test_owned_sources_hold_no_turkish_letters(self):
        """Independent of test_i18n.PENDING_MIGRATION: the hook layer, activity and watchdog are migrated."""
        for path in OWNED_SOURCES:
            self.assertNotRegex(path.read_text(encoding="utf-8"), TR_LETTERS, path.name)


# ======================================================================= activity.py

class TestActivityWords(unittest.TestCase):
    def test_durations_in_both_languages(self):
        cases = [(None, "?", "?"), (0, "0s", "0 sn"), (59, "59s", "59 sn"), (60, "1m", "1 dk"), (89, "1m", "1 dk"),
                 (3599, "59m", "59 dk"), (3600, "1h 0m", "1 sa 0 dk"), (3725, "1h 2m", "1 sa 2 dk")]
        for secs, en, tr in cases:
            with lang("en"):
                self.assertEqual(A._dur(secs), en, secs)
            with lang("tr"):
                self.assertEqual(A._dur(secs), tr, secs)

    def test_incident_spans_keep_seconds_up_to_ninety(self):
        """The watchdog headline always counted seconds below 90 (tempo.dur_tr); the live line below 60."""
        with lang("tr"):
            self.assertEqual([A.human_duration(s) for s in (None, -5, 75, 89, 90, 4000)],
                             ["?", "0 sn", "75 sn", "89 sn", "1 dk", "1 sa 6 dk"])
        with lang("en"):
            self.assertEqual([A.human_duration(s) for s in (None, -5, 75, 89, 90, 4000)],
                             ["?", "0s", "75s", "89s", "1m", "1h 6m"])
            self.assertEqual(A.human_duration(12.7), "12s")

    def test_ago(self):
        with lang("en"):
            self.assertEqual((A._ago(125), A._ago(None)), ("2m ago", "?"))
        with lang("tr"):
            self.assertEqual((A._ago(125), A._ago(None)), ("2 dk önce", "?"))

    def test_main_agent_and_subagent_names(self):
        with lang("en"):
            self.assertEqual([A.short_agent(a) for a in ("main", "", "orkestra:reviewer", "x")],
                             ["main", "main", "reviewer", "x"])
        with lang("tr"):
            self.assertEqual([A.short_agent(a) for a in ("main", "", "orkestra:reviewer")], ["ana", "ana", "reviewer"])

    def test_question_summary_and_headers(self):
        ev = {"tool_name": "AskUserQuestion",
              "tool_input": {"questions": [{"header": "Scope"}, {"question": "Which one?"}, "junk"]}}
        self.assertEqual(A.ask_heads(ev), "Scope, Which one?")
        self.assertEqual(A.ask_heads({"tool_input": {}}), "")
        with lang("en"):
            self.assertEqual(A.summarize(ev, Path("/p")), "question: Scope, Which one?")
            self.assertEqual(A.pending_question([row(1, "tool", "AskUserQuestion", "")]), "question")
        with lang("tr"):
            self.assertEqual(A.summarize(ev, Path("/p")), "soru: Scope, Which one?")
            self.assertEqual(A.pending_question([row(1, "tool", "AskUserQuestion", "")]), "soru")

    def test_summaries_of_other_tools_do_not_depend_on_the_language(self):
        evs = [{"tool_name": "Bash", "tool_input": {"command": "make  test"}},
               {"tool_name": "Agent", "tool_input": {"subagent_type": "orkestra:reviewer", "description": "Review T001"}},
               {"tool_name": "Read", "tool_input": {"file_path": "/p/a.py"}}]
        for ev in evs:
            with lang("en"):
                en = A.summarize(ev, Path("/p"))
            with lang("tr"):
                self.assertEqual(A.summarize(ev, Path("/p")), en)


class TestLiveness(unittest.TestCase):
    """One line per state, word for word in Turkish (the pre-localisation text) and natural English."""

    def text(self, code, st, rows=(), mtime=None, pending=None):
        with lang(code):
            return A.liveness(st, list(rows), NOW, mtime, pending)

    def check(self, st, rows, en, tr, level, mtime=None, pending=None):
        for code, want in (("en", en), ("tr", tr)):
            got = self.text(code, st, rows, mtime, pending)
            self.assertEqual(got["text"], want, code)
            self.assertEqual(got["level"], level, code)          # the level is machine-readable: same in both
            if code == "en":
                self.assertNotRegex(got["text"], TR_LETTERS)
        return got

    def test_finished_runs(self):
        self.check({"status": "done"}, [], "done ✓", "bitti ✓", "done")
        self.check({"status": "aborted"}, [], "aborted", "iptal edildi", "done")

    def test_waiting_for_the_user(self):
        st = {"status": "awaiting_user", "awaiting": {"question": "Which color?"}}
        self.check(st, [], "WAITING FOR YOU · question: Which color?", "SENİ BEKLİYOR — soru: Which color?", "wait")
        self.check({"status": "blocked", "blocked": {"reason": "CI is red"}}, [], "BLOCKED · CI is red",
                   "BLOKE — CI is red", "blocked")
        q = [row(5, "tool", "AskUserQuestion", "question: Scope")]
        self.check({"status": "active", "phase": "implement"}, q,
                   "WAITING FOR YOU · a question is open in the Claude window (question: Scope)",
                   "SENİ BEKLİYOR — Claude penceresinde soru açık (question: Scope)", "wait")

    def test_a_turn_that_ended_and_a_conversation_phase(self):
        st = {"status": "active", "phase": "implement"}
        self.check(st, [row(30, "idle", what="x")],
                   "WAITING FOR YOU · Claude finished its turn (30s ago); reply in that window",
                   "SENİ BEKLİYOR — Claude turu bitirdi (30 sn önce); o pencereye cevap yaz", "wait")
        talk = {"status": "active", "phase": "clarify"}
        self.check(talk, [], "probably WAITING FOR YOU · phase clarify, no activity for 10m; "
                             "a question or approval may be open in the Claude window of that project",
                   "muhtemelen SENİ BEKLİYOR — clarify fazında 10 dk hareket yok; "
                   "o projenin Claude penceresinde soru/onay açık olabilir", "wait", mtime=NOW - 600)
        self.check(talk, [], "probably WAITING FOR YOU · phase clarify; "
                             "a question or approval may be open in the Claude window of that project",
                   "muhtemelen SENİ BEKLİYOR — clarify fazında; o projenin Claude penceresinde soru/onay açık olabilir",
                   "wait")

    def test_paused_and_stale_runs_name_the_resume_command_of_their_engine(self):
        for engine, cmd, where_en, where_tr in (("claude", "/orkestra:resume", "the Claude window", "Claude penceresinde"),
                                                ("cursor", "/orkestra-resume", "the Cursor chat", "Cursor sohbetinde"),
                                                (None, "/orkestra:resume", "the Claude window", "Claude penceresinde")):
            st = {"status": "paused", "engine": engine} if engine else {"status": "paused"}
            self.check(st, [], f"paused · run {cmd} in that project to continue",
                       f"duraklatıldı — devam için o projede {cmd}", "paused")
            st = dict(st, status="active", phase="implement")
            self.check(st, [row(5000, "tool", "Read", "a.py")],
                       f"no activity for 1h 23m: the session may have closed; run {cmd} in that project",
                       f"1 sa 23 dk hareket yok — oturum kapanmış olabilir; o projede {cmd}", "stale")
            self.check(st, [row(5, "tool", "AskUserQuestion", "question: Q")],
                       f"WAITING FOR YOU · a question is open in {where_en} (question: Q)",
                       f"SENİ BEKLİYOR — {where_tr} soru açık (question: Q)", "wait")
            self.check(dict(st, phase="approve"), [], "probably WAITING FOR YOU · phase approve, no activity for 2m; "
                       f"a question or approval may be open in {where_en} of that project",
                       f"muhtemelen SENİ BEKLİYOR — approve fazında 2 dk hareket yok; "
                       f"o projenin {where_tr} soru/onay açık olabilir", "wait", mtime=NOW - 120)

    def test_working_quiet_and_commands(self):
        st = {"status": "active", "phase": "implement"}
        self.check(st, [row(5, "tool", "Read", "a.py")], "working · main", "çalışıyor — ana", "run")
        self.check(st, [row(5, "tool", "Read", "a.py", agent="orkestra:implementer")], "working · implementer",
                   "çalışıyor — implementer", "run")
        self.check(st, [], "no activity recorded yet", "henüz aktivite kaydı yok", "quiet")
        self.check(st, [row(500, "tool", "Read", "a.py")],
                   "quiet · 8m: the model is thinking or a long step is running",
                   "sessiz · 8 dk — model düşünüyor ya da uzun bir adım sürüyor", "quiet")
        self.check(st, [row(100, "tool", "Bash", "make test")], "command running · 1m: make test",
                   "komut çalışıyor · 1 dk: make test", "run")
        self.check(st, [row(2000, "tool", "Bash", "make test")], "command running · 33m: make test",
                   "komut çalışıyor · 33 dk: make test", "stale")
        agent_then_bash = [row(200, "tool", "Agent", "→ orkestra:reviewer: x"), row(100, "tool", "Bash", "make test")]
        self.check(st, agent_then_bash, "command running · 1m: make test · running subagent: reviewer",
                   "komut çalışıyor · 1 dk: make test · çalışan alt-ajan: reviewer", "run")

    def test_background_subagents(self):
        st = {"status": "active", "phase": "implement"}
        pend = [{"type": "orkestra:reviewer", "tasks": ["T001", "T002"], "at": NOW - 125}]
        self.check(st, [row(5, "tool", "Read", "a.py")], "subagent running: reviewer T001/T002 2m",
                   "alt-ajan çalışıyor: reviewer T001/T002 2 dk", "run", pending=pend)
        self.check(st, [row(5, "wait_agents", what="x")],
                   "subagent running: reviewer T001/T002 2m · main session is waiting for the report",
                   "alt-ajan çalışıyor: reviewer T001/T002 2 dk · ana oturum raporu bekliyor", "run", pending=pend)

    def test_run_state_newer_than_the_last_hook_event(self):
        st = {"status": "active", "phase": "implement"}
        self.check(st, [row(500, "tool", "Read", "a.py")], "implement phase open · run state updated 3s ago; "
                   "last hook activity 8m ago", "implement fazı açık — run durumu 3 sn önce güncellendi; "
                   "son hook aktivitesi 8 dk önce", "run", mtime=NOW - 3)
        self.check(st, [], "implement phase open · run state updated 10m ago; no hook activity yet",
                   "implement fazı açık — run durumu 10 dk önce güncellendi; henüz hook aktivitesi yok", "quiet",
                   mtime=NOW - 600)
        self.check({"status": "active"}, [], "active phase open · run state updated 3s ago; no hook activity yet",
                   "aktif fazı açık — run durumu 3 sn önce güncellendi; henüz hook aktivitesi yok", "run", mtime=NOW - 3)

    def test_long_texts_are_cut_the_same_way_in_both_languages(self):
        st = {"status": "awaiting_user", "awaiting": {"question": "q" * 500}}
        for code in ("en", "tr"):
            self.assertEqual(len(self.text(code, st)["text"]), 220)

    def test_messages_keep_working_when_a_catalog_entry_is_missing(self):
        """A missing translation falls back to English, then to the key: a status line must never raise."""
        cats = {"en": {"activity.live_done": "done"}, "tr": {}}
        with mock.patch.object(i18n, "_catalogs", cats), lang("tr"):
            self.assertEqual(A.liveness({"status": "done"}, [], NOW)["text"], "done")
            self.assertEqual(A.liveness({"status": "aborted"}, [], NOW)["text"], "activity.live_aborted")


# ======================================================================= watchdog.py

class WatchdogBase(helpers.Sandbox):
    def view(self, cls=4, key="silent", since=1200, text="session silent", mode="interactive", sid="sess-1", **kw):
        v = {"project": "/w/demo", "project_name": "demo", "run_id": "001-x", "title": "T", "phase": "implement",
             "status": "active", "mode": mode, "next": "orkestra next -p demo",
             "stall": {"cls": cls, "key": key, "since": since, "text": text},
             "sessions": [{"id": sid, "age": 900}]}
        v.update(kw)
        return v

    def config(self, **wd):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"watchdog": wd}), encoding="utf-8")

    def run_main(self, views, code, **kw):
        buf = io.StringIO()
        with lang(code), mock.patch.object(W, "scan", return_value=views), contextlib.redirect_stdout(buf):
            W.main(now=NOW, **kw)
        return buf.getvalue()


class TestWatchdogHeadlines(WatchdogBase):
    def test_every_class_in_both_languages(self):
        heads = {0: ("info", "bilgi"), 1: ("WAITING FOR YOU", "SENİ BEKLİYOR"), 2: ("DEAD WAIT", "ÖLÜ BEKLEME"),
                 3: ("HUNG COMMAND", "ASILI KOMUT"), 4: ("SILENT SESSION", "SESSİZ OTURUM"), 9: ("info", "bilgi")}
        for cls, (en, tr) in heads.items():
            v = self.view(cls=cls, since=1200)
            with lang("en"):
                self.assertEqual(W._message(v), f"[demo] {en} · 20m: session silent · phase implement")
                self.assertNotRegex(W._message(v), TR_LETTERS)
            with lang("tr"):
                self.assertEqual(W._message(v), f"[demo] {tr} · 20 dk — session silent · faz implement")

    def test_no_age_no_suffix(self):
        for code, want in (("en", "[demo] SILENT SESSION: s · phase implement"),
                           ("tr", "[demo] SESSİZ OTURUM — s · faz implement")):
            with lang(code):
                self.assertEqual(W._message(self.view(since=None, text="s")), want)
                self.assertEqual(W._message(self.view(since=0, text="s")), want)

    def test_the_resume_decision_names_the_silence_in_the_users_language(self):
        cfg = {"watchdog": {"auto_resume": "headless", "max_auto_resumes": 3, "lease_seconds": 120}}
        with lang("en"):
            self.assertIn("silent 20m;", W.resume_decision(self.view(mode="headless"), cfg, {}, NOW)["why"])
        with lang("tr"):
            self.assertIn("silent 20 dk;", W.resume_decision(self.view(mode="headless"), cfg, {}, NOW)["why"])


class TestWatchdogOutput(WatchdogBase):
    def test_nothing_open(self):
        self.assertEqual(self.run_main([], "en"), "No open orkestra runs.\n")
        self.assertEqual(self.run_main([], "tr"), "Açık orkestra çalışması yok.\n")
        self.assertEqual(self.run_main([], "en", quiet=True), "")

    def test_incident_lines(self):
        v = self.view(stall={"cls": 4, "key": "silent", "since": 1200, "text": "session silent",
                             "loops": ["same command 3x", "same guard 3x", "ignored"]})
        en = self.run_main([v], "en", dry_run=True).splitlines()
        self.assertEqual(en[0], "[demo] SILENT SESSION · 20m: session silent · phase implement")
        self.assertEqual(en[1], "   loop: same command 3x | same guard 3x")
        self.assertEqual(en[2], "   orkestra next -p demo")
        self.assertTrue(en[3].startswith("   auto-resume: auto_resume=headless"), en[3])
        tr = self.run_main([dict(v)], "tr", dry_run=True).splitlines()
        self.assertEqual(tr[0], "[demo] SESSİZ OTURUM · 20 dk — session silent · faz implement")
        self.assertEqual(tr[1], "   döngü: same command 3x | same guard 3x")
        self.assertTrue(tr[3].startswith("   otomatik devam: auto_resume=headless"), tr[3])

    def test_the_json_output_keeps_machine_fields_and_localizes_only_the_message(self):
        v = self.view()
        out = {}
        for code in ("en", "tr"):
            out[code] = json.loads(self.run_main([dict(v)], code, dry_run=True, as_json=True))[0]
        self.assertEqual(out["en"]["stall"], out["tr"]["stall"])
        self.assertEqual((out["en"]["auto_resume"], out["en"]["notify"]), (out["tr"]["auto_resume"], out["tr"]["notify"]))
        self.assertNotEqual(out["en"]["message"], out["tr"]["message"])

    def test_auto_resume_limit_and_the_engines_own_resume_command(self):
        self.config(auto_resume="all", max_auto_resumes=3, macos_notify=False)
        book = {"resumes": {"/w/demo|001-x": {"count": 3, "last": 0}}}
        (self.home / "watchdog.json").write_text(json.dumps(book), encoding="utf-8")
        for sid, cmd in (("sess-1", "/orkestra:resume"), ("cursor-abc", "/orkestra-resume")):
            en = self.run_main([self.view(sid=sid)], "en", dry_run=True).splitlines()[0]
            self.assertTrue(en.endswith(f" → auto-resume limit reached, you need to continue it ({cmd})"), en)
            tr = self.run_main([self.view(sid=sid)], "tr", dry_run=True).splitlines()[0]
            self.assertTrue(tr.endswith(f" → otomatik devam sınırı aşıldı, senin devam ettirmen gerekiyor ({cmd})"), tr)

    def test_auto_resume_limit_blocks_the_run_so_it_stops_showing_as_running(self):
        # 2026-11: a run whose session vanished stayed "active" for 4.5 days after the third failed resume
        self.config(auto_resume="all", max_auto_resumes=3, macos_notify=False)
        book = {"resumes": {"/w/demo|001-x": {"count": 3, "last": 0}}}
        for code, want in (("en", "Auto-resume gave up after 3 attempts. Continue it with /orkestra:resume"),
                           ("tr", "Otomatik devam 3 denemeden sonra durdu. /orkestra:resume ile devam ettir")):
            (self.home / "watchdog.json").write_text(json.dumps(book), encoding="utf-8")
            calls = []
            with mock.patch.object(W, "_set_run_status", side_effect=lambda *a: calls.append(a)):
                self.run_main([self.view()], code)
                self.run_main([self.view()], code, dry_run=True)          # a dry run never writes
                self.run_main([self.view(status="paused")], code)          # only an active run is blocked
            self.assertEqual(calls, [(Path("/w/demo"), "001-x", "blocked", want)], code)
            if code == "en":
                self.assertNotRegex(want, TR_LETTERS)

    def test_a_resumed_run_says_which_attempt(self):
        self.config(auto_resume="all", macos_notify=False)
        for code, want in (("en", " → auto-resumed (attempt 1)"), ("tr", " → otomatik devam ettirildi (deneme 1)")):
            (self.home / "watchdog.json").unlink(missing_ok=True)
            with mock.patch.object(W, "_spawn_resume", return_value=4242), \
                    mock.patch.object(W, "_process_alive", return_value=False), \
                    mock.patch.object(W, "_bridge_owns", return_value=False):
                out = self.run_main([self.view(cls=4)], code, as_json=True)
            self.assertTrue(json.loads(out)[0]["message"].endswith(want), (code, out))

    def test_notifications_carry_the_localized_headline(self):
        self.config(macos_notify=True)
        (self.home / "watchdog.json").unlink(missing_ok=True)
        for code, head in (("en", "SILENT SESSION"), ("tr", "SESSİZ OTURUM")):
            (self.home / "watchdog.json").unlink(missing_ok=True)
            sent, hooked = [], []
            with mock.patch.object(W, "_notify", side_effect=lambda t, x: sent.append((t, x))), \
                    mock.patch.object(W, "_webhook", side_effect=lambda c, p: hooked.append(p)):
                self.run_main([self.view(cls=4)], code)
            self.assertEqual(len(sent), 1, code)
            self.assertEqual(sent[0][0], "Orkestra")
            self.assertIn(head, sent[0][1])
            self.assertEqual(hooked[0]["message"], sent[0][1])
            self.assertEqual((hooked[0]["class"], hooked[0]["key"]), (4, "silent"))     # machine fields never localize
            if code == "en":
                self.assertNotRegex(sent[0][1], TR_LETTERS)


class TestDesktopNotifications(unittest.TestCase):
    def test_macos_goes_through_osascript_with_the_text_escaped(self):
        calls = []
        with mock.patch.object(W.sys, "platform", "darwin"), mock.patch.object(W.shutil, "which", return_value="/x/osascript"), \
                mock.patch.object(W.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])):
            W._macos_notify("Orkestra", 'say "hi" \\ there')
            W._linux_notify("Orkestra", "x")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["osascript", "-e"])
        self.assertIn('display notification "say \\"hi\\" \\\\ there" with title "Orkestra"', calls[0][2])

    def test_linux_goes_through_notify_send_and_option_like_text_is_not_an_option(self):
        calls = []
        with mock.patch.object(W.sys, "platform", "linux"), mock.patch.object(W.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(W.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])):
            W._notify("Orkestra", "--urgency=critical SESSION SILENT")
        self.assertEqual(calls, [["/usr/bin/notify-send", "--app-name=Orkestra", "--", "Orkestra",
                                  "--urgency=critical SESSION SILENT"]])

    def test_without_a_notifier_nothing_runs(self):
        for platform, which in (("win32", "C:/x/tool.exe"), ("linux", None), ("darwin", None), ("freebsd13", "/x/tool")):
            with mock.patch.object(W.sys, "platform", platform), mock.patch.object(W.shutil, "which", return_value=which), \
                    mock.patch.object(W.subprocess, "run", side_effect=AssertionError("must not run")):
                W._notify("Orkestra", "x")

    def test_a_failing_notifier_never_raises(self):
        with mock.patch.object(W.sys, "platform", "linux"), mock.patch.object(W.shutil, "which", return_value="/x/notify-send"), \
                mock.patch.object(W.subprocess, "run", side_effect=OSError("boom")):
            W._notify("Orkestra", "x")


class TestWatchdogOnWindows(WatchdogBase):
    """os.kill(pid, 0) terminates the process on Windows, and there is no ps / pgrep: nothing is probed there."""

    def test_pid_and_process_checks_never_probe(self):
        with mock.patch.object(W, "_IS_WINDOWS", True), mock.patch.object(W.os, "kill", side_effect=AssertionError("kill")), \
                mock.patch.object(W.subprocess, "run", side_effect=AssertionError("subprocess")):
            self.assertTrue(W._pid_alive(os.getpid(), ["sess-1"]))
            self.assertTrue(W._process_alive(["sess-1"], os.getpid()))
            self.assertTrue(W._process_alive(["sess-1"]))
            self.assertFalse(W._pid_alive(None))                           # garbage is still garbage

    def test_our_own_child_is_still_answered_through_its_handle(self):
        class Child:
            def __init__(self, code):
                self.code = code

            def poll(self):
                return self.code

        with mock.patch.object(W, "_IS_WINDOWS", True), mock.patch.dict(W._children, {4242: Child(None), 4243: Child(0)}):
            self.assertTrue(W._pid_alive(4242))
            self.assertFalse(W._pid_alive(4243))

    def test_no_auto_resume_on_windows_only_a_notification(self):
        cfg = {"watchdog": {"auto_resume": "all", "max_auto_resumes": 3, "lease_seconds": 120}}
        with mock.patch.object(W, "_IS_WINDOWS", True):
            d = W.resume_decision(self.view(), cfg, {}, NOW)
            self.assertFalse(d["resume"])
            self.assertIn("Windows", d["why"])
            self.assertEqual(W.resume_decision(self.view(), {"watchdog": {"auto_resume": "off"}}, {}, NOW)["why"][:15],
                             "auto_resume=off")                                  # an explicit "off" is still reported first
        self.assertTrue(W.resume_decision(self.view(), cfg, {}, NOW)["resume"])  # POSIX is unchanged


# ======================================================================= the hooks: portability

class TestDeadlineWithoutSigalrm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.D = load("ork_dispatch_i18n", helpers.DISPATCH)

    def no_sigalrm(self):
        """`import signal` inside _with_deadline sees a module without SIGALRM/alarm, as on Windows."""
        return mock.patch.dict(sys.modules, {"signal": types.SimpleNamespace()})

    def test_a_call_that_takes_too_long_is_refused(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with self.no_sigalrm():
            with self.assertRaises(self.D._Deadline):
                self.D._with_deadline(1, lambda: release.wait(30))

    def test_a_quick_call_returns_its_value_and_its_errors(self):
        with self.no_sigalrm():
            self.assertEqual(self.D._with_deadline(30, lambda: "ok"), "ok")
            self.assertIsNone(self.D._with_deadline(30, lambda: None))

            def boom():
                raise KeyError("x")

            with self.assertRaises(KeyError):
                self.D._with_deadline(30, boom)

    def test_the_worker_is_a_daemon_so_the_hook_can_exit(self):
        seen = []
        with self.no_sigalrm():
            self.D._with_deadline(30, lambda: seen.append(threading.current_thread()))
        self.assertTrue(seen[0].daemon)
        self.assertNotEqual(seen[0], threading.main_thread())

    @unittest.skipUnless(hasattr(__import__("signal"), "SIGALRM"), "POSIX only")
    def test_posix_keeps_using_the_alarm_in_the_main_thread(self):
        ran_in = []
        self.assertEqual(self.D._with_deadline(30, lambda: ran_in.append(threading.current_thread()) or "ok"), "ok")
        self.assertIs(ran_in[0], threading.current_thread())               # no worker thread on POSIX
        with self.assertRaises(self.D._Deadline):
            self.D._with_deadline(1, lambda: __import__("time").sleep(5))

    @unittest.skipUnless(hasattr(__import__("signal"), "SIGALRM"), "POSIX only")
    def test_posix_outside_the_main_thread_runs_without_a_limit_as_before(self):
        box = []
        worker = threading.Thread(target=lambda: box.append(self.D._with_deadline(1, lambda: "ok")))
        worker.start()
        worker.join(30)
        self.assertEqual(box, ["ok"])


class HookProcess(FlowBase):
    """Runs dispatch.py as the host does, optionally in an interpreter that lacks SIGALRM or has a legacy console codec."""

    def run_dispatch(self, event, payload, *, no_sigalrm=False, env=None):
        payload = dict(payload, hook_event_name=event)
        e = dict(os.environ)
        e.update(env or {})
        if no_sigalrm:
            code = ("import runpy, signal, sys\ndel signal.SIGALRM\nsys.argv = [sys.argv[1], sys.argv[2]]\n"
                    "runpy.run_path(sys.argv[0], run_name='__main__')")
            argv = [sys.executable, "-c", code, str(helpers.DISPATCH), event]
        else:
            argv = [sys.executable, str(helpers.DISPATCH), event]
        return subprocess.run(argv, input=json.dumps(payload).encode("utf-8"), capture_output=True, env=e, timeout=120)


class TestHooksWithoutSigalrm(HookProcess):
    def test_a_guard_still_denies_and_allows_through_the_thread_timer(self):
        self.start()
        commit = {"session_id": self.sid, "tool_name": "Bash", "cwd": str(self.proj), "prompt_id": "p1",
                  "tool_input": {"command": "git add -A && git commit -m wip"}}
        cp = self.run_dispatch("PreToolUse", commit, no_sigalrm=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        out = json.loads(cp.stdout.decode("utf-8"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[orkestra:", out["hookSpecificOutput"]["permissionDecisionReason"])
        ok = dict(commit, tool_input={"command": "ls"})
        cp = self.run_dispatch("PreToolUse", ok, no_sigalrm=True)
        self.assertEqual((cp.returncode, cp.stdout), (0, b""), cp.stderr)
        self.assertNotIn(b"Traceback", cp.stderr)
        log = self.home / "logs" / "hooks.log"
        self.assertFalse(log.exists() and "ERROR" in log.read_text(encoding="utf-8"))


class TestHooksSpeakUtf8(HookProcess):
    ASCII_PIPES = {"PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}

    def test_a_legacy_console_codec_does_not_lose_the_answer(self):
        """With a strict ascii codec `json.dumps(..., ensure_ascii=False)` raised on the run summary's dash: the hook
        failed open and the session got no context (a deny containing `→` would have been lost the same way)."""
        self.start()
        cp = self.run_dispatch("SessionStart", {"session_id": self.sid, "cwd": str(self.proj), "source": "resume"},
                               env=self.ASCII_PIPES)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        text = cp.stdout.decode("utf-8")
        self.assertIn("Active run", json.loads(text)["hookSpecificOutput"]["additionalContext"])
        self.assertIn(EM_DASH, text)                                            # real UTF-8 on the wire

    def test_a_utf8_payload_is_read_whatever_the_codec(self):
        self.start()
        ev = {"session_id": self.sid, "cwd": str(self.proj), "prompt_id": "p9", "prompt": "devam et: şğüöçı İ",
              "hook_event_name": "UserPromptSubmit"}
        cp = subprocess.run([sys.executable, str(helpers.DISPATCH), "UserPromptSubmit"],
                            input=json.dumps(ev, ensure_ascii=False).encode("utf-8"), capture_output=True,
                            env=dict(os.environ, **self.ASCII_PIPES), timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        rows = (self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl").read_text(encoding="utf-8")
        self.assertIn("devam et: şğüöçı İ", rows)

    def test_streams_that_cannot_be_reconfigured_are_left_alone(self):
        D = load("ork_dispatch_utf8", helpers.DISPATCH)
        fake = io.StringIO()
        with mock.patch.object(D.sys, "stdout", fake), mock.patch.object(D.sys, "stdin", io.StringIO("{}")):
            D.utf8_stdio()                                                       # no reconfigure(): nothing happens
            D.emit({"a": "ş"})
        self.assertEqual(json.loads(fake.getvalue()), {"a": "ş"})


class TestSourcesStayPortable(unittest.TestCase):
    """Text files and pipes name their encoding in the hook layer, activity and watchdog (a Windows default of cp125x
    would otherwise mangle or reject Turkish text)."""

    def calls(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                yield node

    @staticmethod
    def name(node):
        f = node.func
        return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""),
                f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else "")

    def test_text_mode_files_name_their_encoding(self):
        for path in OWNED_SOURCES:
            for node in self.calls(path):
                attr, owner = self.name(node)
                kws = {k.arg for k in node.keywords}
                if attr in ("read_text", "write_text") or (attr == "open" and owner in ("", "builtins")):
                    mode = ""
                    args = node.args[1:2] if attr == "open" else []
                    if args and isinstance(args[0], ast.Constant):
                        mode = str(args[0].value)
                    for k in node.keywords:
                        if k.arg == "mode" and isinstance(k.value, ast.Constant):
                            mode = str(k.value.value)
                    if "b" in mode:
                        continue
                    self.assertIn("encoding", kws, f"{path.name}:{node.lineno} {attr}() without encoding=")

    def test_text_mode_subprocess_pipes_name_their_encoding(self):
        for path in OWNED_SOURCES:
            for node in self.calls(path):
                attr, owner = self.name(node)
                if owner == "subprocess" and attr in ("run", "Popen", "check_output"):
                    kws = {k.arg: k.value for k in node.keywords}
                    text_mode = any(isinstance(kws.get(k), ast.Constant) and kws[k].value is True
                                    for k in ("text", "universal_newlines"))
                    if text_mode:
                        self.assertIn("encoding", kws, f"{path.name}:{node.lineno} text=True without encoding=")

    def test_no_posix_only_calls_run_unguarded_in_the_hooks(self):
        """fork / SIGALRM / killpg / os.kill(pid, 0) are not available (or not harmless) on Windows."""
        for path in (PLUGIN / "hooks" / "dispatch.py", PLUGIN / "hooks" / "cursor_hook.py",
                     PLUGIN / "hooks" / "copilot_hook.py", LIB / "hookmain.py", LIB / "hookboot.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("os.fork", "os.killpg", "os.setsid", "os.kill(", "fcntl", "signal.SIGKILL", "os.getuid"):
                self.assertNotIn(banned, text, f"{path.name}: {banned}")
            for m in re.finditer(r"signal\.(SIGALRM|alarm)", text):
                head = text[:m.start()]
                self.assertIn('hasattr(signal, "SIGALRM")', head, f"{path.name}: SIGALRM used before the guard")


# ======================================================================= cursor_hook.py

class TestCursorHookPortability(helpers.Sandbox):
    @classmethod
    def setUpClass(cls):
        cls.C = load("ork_cursor_hook_i18n", helpers.CURSOR_HOOK)

    def test_related_workspaces_with_posix_paths(self):
        C = self.C
        for roots, want in ((["/w"], True), (["/w/p"], True), (["/w/p/sub"], True), (["/w/pp"], False),
                            (["/other"], False), (["/other", "/w"], True), ([], True)):
            self.assertIs(C._related("/w/p", {"workspace_roots": roots}), want, roots)

    def test_related_workspaces_with_windows_paths(self):
        """Windows separators are backslashes and drive letters compare case-insensitively."""
        C = self.C
        with mock.patch.object(C.os, "path", ntpath), mock.patch.object(C.os, "sep", "\\"):
            for roots, want in ((["C:\\Users\\me\\Projects"], True), (["C:\\Users\\me\\Projects\\demo"], True),
                                (["C:\\Users\\me\\Projects\\demo\\src"], True), (["c:\\users\\ME\\projects"], True),
                                (["C:\\Users\\me\\Projects\\demo2"], False), (["D:\\Projects"], False),
                                (["C:\\"], True)):
                self.assertIs(C._related("C:\\Users\\me\\Projects\\demo", {"workspace_roots": roots}), want, roots)

    def test_a_transcript_path_with_backslashes_is_found(self):
        conv = "11111111-2222-4333-8444-555555555555"
        # on POSIX a file whose NAME holds backslashes stands in for a Windows path (isfile() only needs it to exist)
        weird = self.tmp / f"agent-transcripts\\{conv}\\{conv}.jsonl"
        weird.write_text("{}\n", encoding="utf-8")
        self.assertEqual(self.C._transcript(conv, {"transcript_path": str(weird)}, []), str(weird))
        plain = self.tmp / "agent-transcripts" / conv / f"{conv}.jsonl"
        plain.parent.mkdir(parents=True)
        plain.write_text("{}\n", encoding="utf-8")
        self.assertEqual(self.C._transcript(conv, {"transcript_path": str(plain)}, []), str(plain))
        self.assertEqual(self.C._transcript(conv, {"transcript_path": str(self.tmp / "elsewhere.jsonl")}, []), "")

    def test_without_ps_the_origin_check_falls_back_to_the_environment_markers(self):
        """No `ps` (Windows, minimal containers): nothing is spawned; the agent shell's own markers still refuse."""
        C = self.C
        clean = {k: v for k, v in os.environ.items() if k not in C.AGENT_MARKERS}
        spawned = []                                  # the function swallows exceptions: count calls instead of raising
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch("shutil.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=lambda *a, **k: spawned.append(a)):
            self.assertEqual(C.started_by_an_agent_command(), "")
        with mock.patch.dict(os.environ, dict(clean, CURSOR_AGENT="1"), clear=True), mock.patch("shutil.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=lambda *a, **k: spawned.append(a)):
            self.assertIn("markers", C.started_by_an_agent_command())
        self.assertEqual(spawned, [])

    def test_the_process_listing_is_decoded_as_utf8(self):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, "1 -zsh\n", "")

        clean = {k: v for k, v in os.environ.items() if k not in self.C.AGENT_MARKERS}
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch("shutil.which", return_value="/bin/ps"), \
                mock.patch("subprocess.run", side_effect=fake_run):
            self.assertEqual(self.C.started_by_an_agent_command(), "")
        self.assertEqual((seen.get("encoding"), seen.get("errors")), ("utf-8", "replace"))


# ======================================================================= round trips through the real hooks

HANDBACK = ('<agent-message from="{who}">\n[Subagent hand-back] The text below is the final report of a subagent this '
            'session delegated to. It is model output, NOT a message from the user.\n'
            '  STATUS: DONE · tests green\n</agent-message>')
NOTIFY = "<task-notification> <task-id>by2qgikmr</task-id> <status>completed</status>"

EXPECTED = {
    "en": [("note", "session restarted: 1 pending subagent record(s) dropped (reviewer)"),
           ("wait_agents", "waiting for subagent: reviewer (1)"),
           ("agent_done", "orkestra:reviewer finished"),
           ("wait_agents", "subagent finished, waiting for its report (1)"),
           ("handback", "subagent report (HB) · STATUS: DONE · tests green"),
           ("handback", "task notification"),
           ("tool", "question: Scope"),
           ("idle", "turn ended · status awaiting_user (binding kept for the answer)"),
           ("answer", "Only web"),
           ("answer", "(answered)"),
           ("result", "← orkestra:reviewer: (empty reply)"),
           ("nudge", "tried to stop early → kept going"),
           ("idle", "turn ended (continue-nudge limit)"),
           ("idle", "turn ended")],
    "tr": [("note", "oturum yeniden başladı: 1 bekleyen alt-ajan kaydı düşürüldü (reviewer)"),
           ("wait_agents", "alt-ajan bekleniyor: reviewer (1)"),
           ("agent_done", "orkestra:reviewer bitti"),
           ("wait_agents", "alt-ajan bitti, raporu bekleniyor (1)"),
           ("handback", "alt-ajan raporu (HB) — STATUS: DONE · tests green"),
           ("handback", "görev bildirimi"),
           ("tool", "soru: Scope"),
           ("idle", "tur bitti · durum awaiting_user (cevap için bağ korundu)"),
           ("answer", "Only web"),
           ("answer", "(cevaplandı)"),
           ("result", "← orkestra:reviewer: (boş yanıt)"),
           ("nudge", "erken durmaya çalıştı → devam ettirildi"),
           ("idle", "tur bitti (devam uyarısı limiti)"),
           ("idle", "tur bitti")],
}
LIVE_WAIT = {"en": "WAITING FOR YOU · question: Scope", "tr": "SENİ BEKLİYOR — soru: Scope"}


class TestHookRoundTrips(FlowBase):
    """The real hooks, one run, the language switched between two passes: each pass writes its lines in its own
    language, and a run started in one language simply continues in the other."""

    def rows(self):
        p = self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def state(self):
        return json.loads((self.proj / ".orkestra" / "runs" / self.rid / "state.json").read_text(encoding="utf-8"))

    def ev(self, **kw):
        return dict({"session_id": self.sid, "cwd": str(self.proj)}, **kw)

    def launch(self, agent_id):
        ti = {"subagent_type": "orkestra:reviewer", "description": "Review T001", "prompt": "scope: task T001"}
        self.hook("PreToolUse", self.ev(tool_name="Agent", tool_input=ti))
        self.hook("PostToolUse", self.ev(tool_name="Agent", tool_input=ti, tool_response={
            "status": "async_launched", "agentId": agent_id, "description": "Review T001", "outputFile": "/x/out"}))

    def stop(self, pid, **kw):
        return self.hook("Stop", self.ev(prompt_id=pid, stop_hook_active=False, **kw))

    def pass_in(self, code):
        """Everything the hooks write in one language; returns that pass's rows and the live line."""
        tag = code
        hb, hb2 = f"hb-{code}-1", f"hb-{code}-2"
        before = len(self.rows())
        with lang(code):
            self.launch(f"{tag}-a1")
            self.hook("SessionStart", self.ev(source="startup"))               # a new process: pending agents are gone
            self.launch(hb)
            self.stop(f"p-{tag}-0")                                            # a subagent runs: the turn may end
            self.hook("SubagentStop", self.ev(agent_id=hb, agent_type="orkestra:reviewer", stop_hook_active=True))
            self.stop(f"p-{tag}-0b")                                           # finished, report not delivered yet
            self.hook("UserPromptSubmit", self.ev(prompt_id=f"{tag}-u1", prompt=HANDBACK.format(who=hb)))
            self.hook("UserPromptSubmit", self.ev(prompt_id=f"{tag}-u2", prompt=NOTIFY))
            q = [{"header": "Scope", "question": "Which scope?"}]
            self.hook("PreToolUse", self.ev(tool_name="AskUserQuestion", tool_input={"questions": q}))
            st = self.state()
            live = A.liveness(st, A.read_rows(self.proj, self.rid), state_mtime=None)
            self.stop(f"p-{tag}-q")                                            # waiting for the user: binding is kept
            self.hook("PostToolUse", self.ev(tool_name="AskUserQuestion", tool_input={"questions": q},
                                             tool_response={"answers": {"Which scope?": "Only web"}}))
            self.hook("PostToolUse", self.ev(tool_name="AskUserQuestion", tool_input={"questions": q},
                                             tool_response={"answers": {}}))
            ti = {"subagent_type": "orkestra:reviewer", "description": "Review T001", "prompt": "scope"}
            self.hook("PreToolUse", self.ev(tool_name="Agent", tool_input=ti))
            self.hook("PostToolUse", self.ev(tool_name="Agent", tool_input=ti, tool_response=""))
            for i in range(3):                                                 # two nudges, then the limit
                self.stop(f"p-{tag}-n")
            self.hook("Stop", self.ev(prompt_id=f"p-{tag}-z", stop_hook_active=True))
        return self.rows()[before:], live, st

    def test_each_language_writes_its_own_activity_lines(self):
        self.start()
        for code in ("en", "tr"):
            rows, live, st = self.pass_in(code)
            got = [(r["kind"], r["what"]) for r in rows]
            for kind, what in EXPECTED[code]:
                what = what.replace("HB", f"hb-{code}-1"[:12])
                self.assertIn((kind, what), got, f"{code}: {kind} {what!r}")
            self.assertEqual(got.count(EXPECTED[code][11]), 2, code)           # two nudges per prompt
            if code == "en":
                for r in rows:
                    if r["kind"] not in ("user",):
                        self.assertNotRegex(r["what"], TR_LETTERS, r)
            self.assertEqual(st["awaiting"]["question"], "Scope", code)       # the headers only: no language prefix
            self.assertEqual(live["text"], LIVE_WAIT[code], code)
            self.assertEqual(live["level"], "wait")
            self.assertEqual(self.state()["status"], "active", code)

    def test_a_run_continues_in_the_other_language_and_old_lines_stay_as_written(self):
        self.start()
        n0 = len(self.rows())
        first, _, _ = self.pass_in("tr")
        second, live, _ = self.pass_in("en")
        self.assertIn(("idle", "tur bitti"), [(r["kind"], r["what"]) for r in first])
        self.assertIn(("idle", "turn ended"), [(r["kind"], r["what"]) for r in second])
        self.assertEqual(self.rows()[n0:n0 + len(first)], first)                 # history is never rewritten
        self.assertEqual(live["text"], LIVE_WAIT["en"])

    def test_session_binding_and_off_topic_notes(self):
        out = self.start()
        want = {"en": [("bind", "session bound (sess-1)"),
                       ("note", "session unbound · off-topic prompt (not about demo)")],
                "tr": [("bind", "oturum bağlandı (sess-1)"), ("note", "oturum çözüldü · konu dışı prompt (demo değil)")]}
        for code in ("en", "tr"):
            before = len(self.rows())
            with lang(code):
                self.hook("PostToolUse", self.ev(tool_name="Bash", tool_input={"command": "orkestra start"},
                                                 tool_response={"stdout": out, "stderr": ""}))
                # 2026-10-12: an ordinary message keeps the binding; "never mind" is an explicit signal to let go of the run
                self.hook("UserPromptSubmit", self.ev(prompt_id=f"off-{code}", prompt="never mind this, please bake a chocolate cake"))
            got = [(r["kind"], r["what"]) for r in self.rows()[before:]]
            for item in want[code]:
                self.assertIn(item, got, code)

    def test_the_agent_result_line_is_localized_but_keeps_a_status_line(self):
        D = load("ork_dispatch_result", helpers.DISPATCH)
        ev = {"tool_input": {"subagent_type": "orkestra:reviewer"}, "tool_response": {"content": [{"type": "text",
              "text": "Looks fine.\nVERDICT: PASS no findings"}]}}
        empty = {"tool_input": {}, "tool_response": ""}
        with lang("en"):
            self.assertEqual(D.agent_result_line(ev), "← orkestra:reviewer: VERDICT: PASS no findings")
            self.assertEqual(D.agent_result_line(empty), "← agent: (empty reply)")
        with lang("tr"):
            self.assertEqual(D.agent_result_line(ev), "← orkestra:reviewer: VERDICT: PASS no findings")
            self.assertEqual(D.agent_result_line(empty), "← agent: (boş yanıt)")


class TestCursorRoundTrip(CursorBase):
    PROMPT = "Review task T001 of run X: check spec compliance of calc.py and report in reviews/T001.md"

    def rows(self):
        p = self.proj / ".orkestra" / "runs" / self.rid / "activity.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def age_launches(self, seconds):
        p = self.proj / ".orkestra" / "runs" / self.rid / "agents.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        for x in data.get("cursor_launches") or []:
            x["at"] -= seconds
        p.write_text(json.dumps(data), encoding="utf-8")

    def test_returned_tasks_and_closed_sessions_are_written_in_the_users_language(self):
        self.start()
        want = {"en": ["← orkestra:reviewer: returned", "turn ended · status active (session closed)"],
                "tr": ["← orkestra:reviewer: döndü", "tur bitti · durum active (oturum kapandı)"]}
        for n, code in enumerate(("en", "tr")):
            before = len(self.rows())
            with lang(code):
                ti = {"description": "Review T001", "prompt": f"{self.PROMPT} ({code})", "model": "",
                      "subagent_type": "orkestra-reviewer", "run_in_background": False}
                self.tool("Task", ti, gen=f"g{n}", tool_use_id=f"tu-task-{code}")
                self.age_launches(100)                                          # the Task call is back by now
                self.shell("orkestra next -p demo", gen=f"g{n}", tool_use_id=f"tu-next-{code}")
                self.chook("sessionEnd", gen=f"g{n}")
            what = [r["what"] for r in self.rows()[before:]]
            for line in want[code]:
                self.assertIn(line, what, (code, what))
            if code == "en":
                for line in what:
                    self.assertNotRegex(line, TR_LETTERS)


if __name__ == "__main__":
    unittest.main()
