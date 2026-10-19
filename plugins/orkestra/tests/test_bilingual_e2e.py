"""Orkestra in two languages, end to end: the same work in English and in Turkish, a run that changes language at every
phase, runs written before the localization, and every way a language can be chosen (or mis-chosen).

Everything goes through the real CLI and the real hook dispatcher (subprocesses), the way an agent uses them. The suite
is pinned to Turkish (helpers.py); these tests choose their language explicitly with ORKESTRA_LANG.

Policy under test (see i18n.py): what a HUMAN reads (watch, report evidence, templates, request.md, memory files) follows
the chosen language and English is the default; what the MODEL reads (next hints, guard messages) stays English; the
user's own words and every protocol token (section headings, AskUserQuestion labels) are accepted in BOTH languages.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import evidence as E
from orkestra_core import i18n
from orkestra_core import runs as R
from test_first_run import FreshMachine
from test_flow import FlowBase

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")      # çğıöşüÇĞİÖŞÜ
STEPS = ("intake", "discover", "clarify", "plan", "approve", "implement", "verify", "review", "deliver")


# ====================================================================== what a model writes, per language

EN_SPEC = """---
feature: sub
status: draft
---
# Subtract

## Purpose and value
Users can subtract two numbers.

## Requirements
- **FR-001**: `sub(a, b)` returns a-b MUST.

## Success criteria
- **SC-001**: `sub(5, 3) == 2` passes as a test.

## Boundaries
- Always: the existing add behaviour is kept.
- Never: add dependencies.

## Clarifications
### Session 2026-10-12
- No question needed: the request has one meaning.
"""

EN_PLAN = """---
feature: sub
risk: low
approved_dependencies: []
---
# Plan: Subtract

## Global constraints
- add() does not change.

## Tasks

### T001: sub function
- Files: `calc.py` (modified), `tests/test_sub.py` (new)
- Depends: none
- Acceptance: Given 5 and 3 When sub Then 2 (FR-001)
- Test: `python3 -m unittest tests.test_sub -q`
- TDD: required

## Test strategy and traceability
| FR-001 | test_sub | unit |
| SC-001 | test_sub | unit |
"""

# The Turkish pack is the shape of the templates before the localization (git history: templates/spec.md, plan.md ...).
TR_SPEC = """---
feature: 001-sub
status: draft
size: M
created: 2026-10-12
---
# Çıkarma

> NE ve NEDEN. Nasıl'ı plan.md anlatır. Onaydan sonra bu dosya DONDURULUR (hook ile zorlanır).

## Amaç ve değer
Kullanıcı iki sayıyı çıkarabilsin.

## Kapsam
### Kapsam içi
- sub fonksiyonu
### Kapsam dışı
- ondalık yuvarlama

## Kullanıcı hikayeleri
- **US1 (P1)** — geliştirici olarak iki sayıyı çıkarmak istiyorum, böylece farkı alırım.
  Bağımsız test: sub(5, 3) == 2

## Gereksinimler
- **FR-001**: `sub(a, b)` a-b döndürür MUST.

## Başarı kriterleri
- **SC-001**: `sub(5, 3) == 2` testi geçer.

## Kenar durum matrisi
| # | Durum | Girdi | Beklenen | Gereksinim |
|---|---|---|---|---|
| E1 | negatif sonuç | sub(3, 5) | -2 | FR-001 |

## Sınırlar
- Her zaman: mevcut add davranışı korunur.
- Asla: bağımlılık ekleme.

## Önerilen ek kapsam (zenginleştirme)
- [ret] ondalık destek — kapsam dışı

## Netleştirmeler
### Oturum 2026-10-12
- Soru gerekmedi — istek tek anlamlı.

## Varsayımlar ve açık riskler
- Tam sayı girdi — ondalık gelirse davranış tanımsız
"""

TR_PLAN = """---
feature: 001-sub
risk: low
baseline_head: abc123
approved_dependencies: []
---
# Plan — Çıkarma

> NASIL. Görevler kullanıcı onayından sonra `orkestra` tarafından state'e yüklenir.

## Yaklaşım
- Seçilen: tek fonksiyon

## Kod haritası
| Dosya | Değişiklik | Neden / izlenecek mevcut pattern |
|---|---|---|
| `calc.py` | değişir | add() gibi |

## Genel kısıtlar
- Mevcut public API/davranış bozulmaz (baseline gate'leri yeşil kalır).

## Görevler

### T001 — sub fonksiyonu
- Dosyalar: `calc.py` (değişir), `tests/test_sub.py` (yeni)
- Bağımlı: —
- Kabul: Given 5 ve 3 When sub Then 2 (FR-001)
- Test: `python3 -m unittest tests.test_sub -q`
- TDD: required

## Test stratejisi ve izlenebilirlik
| Gereksinim | Test | Tür |
|---|---|---|
| FR-001 | test_sub | unit |
| SC-001 | test_sub | unit |

## Doğrulama kapıları
- Baseline'da geçen tüm gate'ler geçmeye devam eder.

## Riskler ve geri alma
- Risk: yok

## İstisnalar
Normalde YOK.

## Yeni bağımlılıklar
Yok.

## Plan değişiklik günlüğü
<!-- append-only -->
"""

PACKS = {
    "en": {
        "title": "Subtract numbers",
        "request": "add a sub function that subtracts two numbers",
        "classification": "\n## Classification\n- type: feature\n- risk: low\n",
        "spec": EN_SPEC, "plan": EN_PLAN,
        "ask": ("Plan approval", "Do you approve the plan and the spec?"),
        "approve": "Approve and start implementing",
        "changes": "I want changes",
        "triage": "| R-001 | reviewer | `calc.py:2` null check | fix | done | closed by T001 |\n",
        "report": "# Delivery report: Subtract\n\n- Status: COMPLETED\n\n## What was done and its value\n" + "sub() added. " * 40,
        "context": "# Context\n" + "calc.py defines add(); tests live in tests/. " * 8,
    },
    "tr": {
        "title": "Çıkarma işlemi",
        "request": "iki sayıyı çıkaran bir sub fonksiyonu ekle",
        "classification": "\n## Sınıflandırma\n- tip: özellik\n- risk: düşük\n",
        "spec": TR_SPEC, "plan": TR_PLAN,
        "ask": ("Plan onayı", "Plan ve spec'i onaylıyor musun?"),
        "approve": "Onayla — uygulamaya başla",
        "changes": "Değişiklik istiyorum",
        "triage": "| R-001 | reviewer | `calc.py:2` null kontrolü | fix | tamam | T001 ile kapandı |\n",
        "report": "# Teslim raporu — Çıkarma\n\n- Durum: TAMAMLANDI\n\n## Ne yapıldı ve ürüne değeri\n"
                  + "sub() eklendi. " * 40,
        "context": "# Context\n" + "calc.py add() tanımlar; testler tests/ altında. " * 8,
    },
}
VERIFICATION = ("| FR-001 | `tests/test_sub.py::test_sub` | PASS |\n"
                "| SC-001 | gate unit | PASS |\n\nVERDICT: PASS\n")


class Journey(FlowBase):
    """FlowBase with a language per step: a step runs under ORKESTRA_LANG=<its language> and writes the artifacts a model
    would write in that language. Every output (CLI and hooks) is recorded with the language it was produced in."""

    START_LANG = "en"

    def setUp(self):
        self.seen = []                                           # (language, label, text)
        patcher = mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.START_LANG})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()                                          # creates the project and runs `init` in START_LANG

    # ------------------------------------------------------------------ recording
    def cli(self, *args, **kw):
        cp = super().cli(*args, **kw)
        self.seen.append((os.environ["ORKESTRA_LANG"], " ".join(args), cp.stdout + cp.stderr))
        return cp

    def hook(self, event, payload):
        out = super().hook(event, payload)
        self.seen.append((os.environ["ORKESTRA_LANG"], "hook " + event, json.dumps(out, ensure_ascii=False)))
        return out

    @contextlib.contextmanager
    def speaking(self, code):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code}):
            yield

    def ask(self, pack, answer):
        header, question = pack["ask"]
        q = [{"question": question, "header": header}]
        return self.hook("PostToolUse", {"session_id": self.sid, "tool_name": "AskUserQuestion", "cwd": str(self.proj),
                                         "tool_input": {"questions": q},
                                         "tool_response": {"questions": q, "answers": {question: answer}}})

    def ledger(self):
        return (self.fdir / "ledger.md").read_text(encoding="utf-8")

    def write(self, name, text):
        (self.fdir / name).write_text(text, encoding="utf-8")

    def views(self):
        """Run the human-facing views once, in the current language, and return their outputs by name."""
        return {name: self.cli(*args, check=False).stdout for name, args in (
            ("watch", ("watch", "--once")), ("tempo", ("tempo", "-p", "demo")), ("spec template", ("template", "spec")),
            ("memory", ("memory", "show", "-p", "demo")))}

    # ------------------------------------------------------------------ the whole pipeline
    def journey(self, langs):
        """`orkestra start` to `done`, step i in language langs[i]."""
        assert len(langs) == len(STEPS)
        packs = [PACKS[code] for code in langs]
        self.step_views = {}

        def step(i):
            self.current = i
            return self.speaking(langs[i])

        with step(0):
            self.start(size="M", title=packs[0]["title"], request=packs[0]["request"])
            self.assertIn("classify the request", self.cli("next", "-p", "demo").stdout)
            with open(self.fdir / "request.md", "a", encoding="utf-8") as fh:
                fh.write(packs[0]["classification"])
            self.assertNotIn("classify the request", self.cli("next", "-p", "demo").stdout)
            self.step_views[0] = self.views()
            self.phase_next()                                                           # -> discover
        with step(1):
            self.write("context.md", packs[1]["context"])
            self.cli("gate", "baseline", "-p", "demo")
            self.step_views[1] = self.views()
            self.phase_next()                                                           # -> clarify
        with step(2):
            self.write("spec.md", packs[2]["spec"])
            self.step_views[2] = self.views()
            self.phase_next()                                                           # -> plan
        with step(3):
            self.write("plan.md", packs[3]["plan"])
            self.write("reviews/plan-critic.md", "# critic\nVERDICT: PASS\n")
            self.assertIn("OK", self.cli("plan", "check", "-p", "demo").stdout)
            self.step_views[3] = self.views()
            self.phase_next()                                                           # -> approve
        with step(4):
            cp = self.phase_next(expect_ok=False)
            self.assertIn("no user approval", cp.stderr)
            self.ask(packs[4], packs[4]["changes"])
            self.assertIn("user reject via AskUserQuestion", self.ledger())
            self.phase_next(expect_ok=False)
            self.ask(packs[4], packs[4]["approve"])
            self.assertIn("user approve via AskUserQuestion", self.ledger())
            self.step_views[4] = self.views()
            self.phase_next()                                                           # -> implement
        with step(5):
            self.cli("task", "start", "T001", "-p", "demo")
            self.tdd_cycle("T001", "sub", "a - b", 5, 3, 2)
            self.write("reviews/T001.review.md", "VERDICT: PASS\n")
            self.cli("task", "done", "T001", "-p", "demo", "reviewed: PASS")
            self.step_views[5] = self.views()
            self.phase_next()                                                           # -> verify
        with step(6):
            self.assertIn("NO REGRESSIONS", self.cli("gate", "verify", "-p", "demo").stdout)
            self.write("verification.md", VERIFICATION)
            self.assertIn("OK", self.cli("trace", "check", "-p", "demo").stdout)
            self.step_views[6] = self.views()
            self.phase_next()                                                           # -> review
        with step(7):
            self.write("reviews/triage.md", packs[7]["triage"])
            self.assertIn("OK", self.cli("review", "check", "-p", "demo").stdout)
            self.step_views[7] = self.views()
            self.phase_next()                                                           # -> deliver
        with step(8):
            self.write("report.md", packs[8]["report"])
            self.cli("memory", "feature-add", "-p", "demo", "--title", packs[8]["title"], "--summary", "sub() added",
                     "--capabilities", "calc")
            self.cli("memory", "decision", "-p", "demo", "keep add() as it is")
            self.cli("memory", "learn", "-p", "demo", "tests live in tests/")
            self.cli("report", "evidence", "--write", "-p", "demo")
            self.step_views[8] = self.views()
            # the machine block is accepted whichever language reads it
            for code in ("en", "tr"):
                with self.speaking(code):
                    self.assertEqual(self.cli("phase", "check", "-p", "demo", check=False).returncode, 0, code)
            self.assertIn("[orkestra:unbind]", self.phase_next().stdout)                 # -> done

    def finished(self):
        st = json.loads((self.proj / ".orkestra" / "runs" / self.rid / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((st["phase"], st["status"]), ("done", "done"))
        self.assertIn(self.rid, self.memory_file("FEATURES.md"))
        self.assertGreater(len(self.seen), 40, "the recorder saw (almost) nothing: the language checks would be vacuous")

    def memory_file(self, name):
        return (self.proj / ".orkestra" / "memory" / name).read_text(encoding="utf-8")

    def leaks(self, code="en"):
        return [(lang, label, m.group(0)) for lang, label, text in self.seen if lang == code
                for m in TR_LETTERS.finditer(text)]


# ====================================================================== the same flow, twice

def heading_line(watch: str, word: str) -> bool:
    """The `watch` dashboard names its rows in the language of the process: `phase` / `faz`, `ledger` / `günlük`."""
    return bool(re.search(r"^\s*%s\b" % word, watch, re.M))


class TestEnglishFlow(Journey):
    START_LANG = "en"

    def test_a_whole_run_in_english_is_english_and_complete(self):
        self.journey(["en"] * len(STEPS))
        self.finished()
        self.assertEqual(self.leaks("en")[:5], [], "Turkish letters in an English run")
        request = (self.fdir / "request.md").read_text(encoding="utf-8")
        for needle in ("# Request: Subtract numbers", "## Raw request", "- size: M", "- approval mode: manual", "## Classification"):
            self.assertIn(needle, request)
        report = (self.fdir / "report.md").read_text(encoding="utf-8")
        self.assertIn("### Machine evidence", report)
        self.assertIn("Commit/push: not done", report)
        self.assertEqual(report.count("orkestra:evidence:begin"), 1)
        features = self.memory_file("FEATURES.md")
        self.assertIn("| id | title | date | capabilities | summary |", features)
        self.assertIn(self.rid, features)
        self.assertIn("keep add() as it is", self.memory_file("DECISIONS.md"))
        self.assertIn("tests live in tests/", self.memory_file("LEARNINGS.md"))
        self.assertIn("## Summary", self.memory_file("PROJECT.md"))
        for i, views in self.step_views.items():
            self.assertTrue(heading_line(views["watch"], "phase"), "step %s: %s" % (STEPS[i], views["watch"]))
            self.assertFalse(heading_line(views["watch"], "faz"))
            self.assertIn("## Purpose and value", views["spec template"])
        hints = [text for _, label, text in self.seen if label.startswith("next ")]
        self.assertTrue(any("classify the request: append `## Classification`" in t for t in hints), hints[:2])


class TestTurkishFlow(Journey):
    START_LANG = "tr"

    def test_a_whole_run_in_turkish_is_turkish_and_complete(self):
        self.journey(["tr"] * len(STEPS))
        self.finished()
        request = (self.fdir / "request.md").read_text(encoding="utf-8")
        for needle in ("# İstek: Çıkarma işlemi", "## Ham istek", "- boyut: M", "- onay modu: manual", "## Sınıflandırma"):
            self.assertIn(needle, request)
        self.assertNotIn("Raw request", request)
        report = (self.fdir / "report.md").read_text(encoding="utf-8")
        self.assertIn("### Makine kanıtı", report)
        self.assertIn("Commit/push: yapılmadı", report)
        self.assertNotIn("Machine evidence", report)
        self.assertEqual(report.count("orkestra:evidence:begin"), 1)
        features = self.memory_file("FEATURES.md")
        self.assertIn("| id | başlık | tarih | yetenekler | özet |", features)
        self.assertIn(self.rid, features)
        self.assertIn("## Summary", self.memory_file("PROJECT.md"))
        # the human-facing views speak Turkish at every step; the model-facing hint stays English
        for i, views in self.step_views.items():
            self.assertTrue(heading_line(views["watch"], "faz"), "step %s: %s" % (STEPS[i], views["watch"]))
            self.assertFalse(heading_line(views["watch"], "phase"))
            self.assertIn("## " + i18n.heading("purpose", "tr"), views["spec template"])
        hints = [text for _, label, text in self.seen if label.startswith("next ")]
        self.assertTrue(any("classify the request: append `## Sınıflandırma`" in t for t in hints), hints[:2])


# ====================================================================== a run that changes language at every step

class MixedJourney(Journey):
    """The language flips at every phase: each artifact is written in the language of its own step, and every later step
    (in the other language) has to read it."""

    def mixed(self, first):
        other = "tr" if first == "en" else "en"
        langs = [first if i % 2 == 0 else other for i in range(len(STEPS))]
        self.journey(langs)
        self.finished()
        request = (self.fdir / "request.md").read_text(encoding="utf-8")
        # request.md keeps the language of the step that started the run; its classification the language of step 0
        self.assertIn("## Ham istek" if first == "tr" else "## Raw request", request)
        self.assertIn("## Sınıflandırma" if first == "tr" else "## Classification", request)
        self.assertNotIn("## Raw request" if first == "tr" else "## Ham istek", request)
        spec = (self.fdir / "spec.md").read_text(encoding="utf-8")
        plan = (self.fdir / "plan.md").read_text(encoding="utf-8")
        self.assertEqual("## Amaç ve değer" in spec, langs[2] == "tr")
        self.assertEqual("## Görevler" in plan, langs[3] == "tr")
        self.assertEqual(("## Purpose and value" in spec), langs[2] == "en")
        report = (self.fdir / "report.md").read_text(encoding="utf-8")
        self.assertEqual("### Makine kanıtı" in report, langs[8] == "tr")
        self.assertEqual("### Machine evidence" in report, langs[8] == "en")
        self.assertEqual(report.count("orkestra:evidence:begin"), 1)
        # the views follow the language of THEIR step, whatever language the artifacts they show are in
        for i, views in self.step_views.items():
            row = "faz" if langs[i] == "tr" else "phase"
            self.assertTrue(heading_line(views["watch"], row), "step %s (%s): %s" % (STEPS[i], langs[i], views["watch"]))
            self.assertIn("## " + i18n.heading("purpose", langs[i]), views["spec template"])
        # the run was approved in the wording of step 4 and finished in the language of step 8
        self.assertIn("user approve via AskUserQuestion: " + PACKS[langs[4]]["approve"], self.ledger())
        return langs


class TestEnglishThenTurkishThenEnglish(MixedJourney):
    START_LANG = "en"

    def test_the_language_flips_at_every_phase_starting_in_english(self):
        langs = self.mixed("en")
        # a run that began in English printed no Turkish while a step was in English (nothing of the user's is Turkish)
        english_steps = [(lang, label, text) for lang, label, text in self.seen
                         if lang == "en" and label.split()[0] in ("watch", "status", "next", "context", "lang")]
        self.assertTrue(english_steps)
        self.assertEqual([(label, m.group(0)) for _, label, text in english_steps for m in TR_LETTERS.finditer(text)][:3], [])
        self.assertEqual(langs[0], "en")


class TestTurkishThenEnglishThenTurkish(MixedJourney):
    START_LANG = "tr"

    def test_the_language_flips_at_every_phase_starting_in_turkish(self):
        self.assertEqual(self.mixed("tr")[0], "tr")


# ====================================================================== the way a user says yes, in either language

class TestApprovalWordingInEverySession(FlowBase):
    """The plan-approval question may be asked in either language, in a session of either language, with the legacy
    Turkish labels (with or without the dash, any case, with or without diacritics)."""

    APPROVE = ("Approve and start implementing", "Onayla — uygulamaya başla", "Onayla - uygulamaya basla")
    SHOUTED = ("APPROVE AND START IMPLEMENTING", "ONAYLA — UYGULAMAYA BAŞLA", "onayla uygulamaya basla")
    NOT_APPROVE = ("I want changes", "Cancel", "Değişiklik istiyorum", "degisiklik istiyorum", "İptal et", "iptal et",
                   "Cancel it")
    QUESTIONS = {"en": ("Plan approval", "Do you approve the plan and the spec?"),
                 "tr": ("Plan onayı", "Plan ve spec'i onaylıyor musun?")}

    def ask(self, header, question, answer):
        q = [{"question": question, "header": header}]
        return self.hook("PostToolUse", {"session_id": self.sid, "tool_name": "AskUserQuestion", "cwd": str(self.proj),
                                         "tool_input": {"questions": q},
                                         "tool_response": {"questions": q, "answers": {question: answer}}})

    def approval(self):
        path = self.proj / ".orkestra" / "runs" / self.rid / "approval.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def at_approval(self, session):
        self.start(approval_mode="manual")
        self.advance_to_approve()

    def test_every_label_in_a_session_of_either_language(self):
        for session in ("en", "tr"):
            asked_in = "tr" if session == "en" else "en"                    # the question text may be in the OTHER language
            first = True
            for label in self.APPROVE + self.SHOUTED[:1 if session == "en" else 3]:
                with self.subTest(session=session, approve=label), mock.patch.dict(os.environ, {"ORKESTRA_LANG": session}):
                    self.at_approval(session)
                    header, question = self.QUESTIONS[asked_in if not first else session]
                    if first:                                                # every refusal once per session
                        for refusal in self.NOT_APPROVE:
                            self.ask(header, question, refusal)
                            self.assertEqual(self.approval().get("decision"), "reject", refusal)
                            self.phase_next(expect_ok=False)
                    self.ask(header, question, label)
                    self.assertEqual(self.approval().get("decision"), "approve", label)
                    self.phase_next()
                    self.assertEqual(json.loads(self.cli("status", "--json", "-p", "demo").stdout)["phase"], "implement")
                    self.cli("abort", "-p", "demo", "next label")
                    first = False

    def test_free_text_approval_in_a_session_of_either_language(self):
        no = ("I approve but remove T001", "wait, change the plan", "onaylıyorum ama T001'i çıkar", "hayır, değiştir",
              "do I approve this?")
        for session, phrases in (("en", ("I approve", "onaylıyorum")), ("tr", ("go ahead", "onay verdim devam et"))):
            first = True
            for phrase in phrases:
                with self.subTest(session=session, yes=phrase), mock.patch.dict(os.environ, {"ORKESTRA_LANG": session}):
                    self.at_approval(session)
                    if first:
                        for refusal in no:
                            self.assertNotIn("APPROVED", json.dumps(self.approve(refusal)), refusal)
                            self.phase_next(expect_ok=False)
                    self.assertIn("APPROVED", json.dumps(self.approve(phrase)), phrase)
                    self.phase_next()
                    self.cli("abort", "-p", "demo", "next phrase")
                    first = False


# ====================================================================== runs from before the localization

LEGACY_TRIAGE = ("| id | kaynak | bulgu (dosya:satır) | verdict | status | not |\n|---|---|---|---|---|---|\n"
                 "| R-001 | reviewer | `calc.py:2` null check eksik | fix | done | T001 ile kapandı |\n"
                 "| R-002 | adversary | yanlış pozitif | reject | — | |\n"
                 "| R-003 | reviewer | küçük | fix | tamam | |\n"
                 "| R-004 | reviewer | küçük | fix | yapıldı | |\n")
LEGACY_VERIFICATION = ("## Traceability\n| ID | Kanıt (doğrulanabilir referans ZORUNLU) | Sonuç |\n|---|---|---|\n" + VERIFICATION)
LEGACY_FEATURES = ("# Features\n\nOne line per delivered orkestra run (newest last). Details live in "
                   "`.orkestra/features/<id>/`.\n\n| id | başlık | tarih | yetenekler | özet |\n|---|---|---|---|---|\n"
                   "| 000-eski | Eski özellik | 2026-01-01 | auth | eski özet |\n")


class LegacyRun(FlowBase):
    """A run whose artifacts were written by the Turkish-only version, continued by a user who now works in English (or
    sets a language orkestra does not know, which means English)."""

    LANG = "en"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ORKESTRA_LANG": self.LANG})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        memory = self.proj / ".orkestra" / "memory"                 # the project memory as the old version created it
        (memory / "FEATURES.md").write_text(LEGACY_FEATURES, encoding="utf-8")
        (memory / "DECISIONS.md").write_text("# Decisions\n\n- 2026-01-01 [000-eski] karar — neden\n", encoding="utf-8")

    def write_text(self, name, text):
        (self.fdir / name).write_text(text, encoding="utf-8")


class TestLegacyTurkishRunContinuedInEnglish(LegacyRun):
    def test_every_reader_accepts_the_old_artifacts(self):
        self.start(size="M", approval_mode="manual", title="Eski çıkarma", request="sub fonksiyonu ekle")
        # request.md exactly as the old `start` wrote it
        (self.fdir / "request.md").write_text(
            "# İstek — Eski çıkarma\n\n- run: `%s`\n- boyut: M\n- mod: interactive\n- motor: claude\n"
            "- onay modu: manual\n- tarih: 2026-10-12T10:00:00+03:00\n\n## Ham istek\n\nsub fonksiyonu ekle\n" % self.rid,
            encoding="utf-8")
        self.assertIn("classify the request", self.cli("next", "-p", "demo").stdout)
        with open(self.fdir / "request.md", "a", encoding="utf-8") as fh:
            fh.write("\n## Sınıflandırma\n- tip: özellik\n")
        self.assertNotIn("classify the request", self.cli("next", "-p", "demo").stdout)
        # the host hook that corrects the origin engine finds the old `motor` label
        self.assertTrue(R.record_engine_origin(self.proj, self.rid, "cursor"))
        self.assertIn("- motor: cursor", (self.fdir / "request.md").read_text(encoding="utf-8"))

        self.phase_next()
        self.write_text("context.md", PACKS["tr"]["context"])
        self.cli("gate", "baseline", "-p", "demo")
        self.phase_next()
        self.write_text("spec.md", TR_SPEC)
        self.phase_next()
        self.write_text("plan.md", TR_PLAN)
        self.write_text("reviews/plan-critic.md", "# critic\nVERDICT: PASS\n")
        self.assertIn("OK", self.cli("plan", "check", "-p", "demo").stdout)
        self.phase_next()
        self.assertIn("APPROVED", json.dumps(self.approve("onaylıyorum")))   # approved in Turkish, continued in English
        self.phase_next()
        self.cli("task", "start", "T001", "-p", "demo")
        brief = self.cli("brief", "T001", "-p", "demo").stdout
        self.assertIn("## Global constraints", brief)
        self.assertIn("Mevcut public API", brief, "the old Turkish constraints section is found")
        self.assertIn("## Boundaries (spec)", brief)
        self.assertIn("Her zaman: mevcut add", brief, "the old Turkish boundaries section is found")
        self.tdd_cycle("T001", "sub", "a - b", 5, 3, 2)
        self.write_text("reviews/T001.review.md", "VERDICT: PASS\n")
        self.cli("task", "done", "T001", "-p", "demo", "reviewed: PASS")
        self.phase_next()
        self.cli("gate", "verify", "-p", "demo")
        self.write_text("verification.md", LEGACY_VERIFICATION)
        self.assertIn("OK", self.cli("trace", "check", "-p", "demo").stdout)
        self.phase_next()
        # the old triage table: Turkish done words close a `fix` row in English mode too
        self.write_text("reviews/triage.md", LEGACY_TRIAGE)
        self.assertIn("OK", self.cli("review", "check", "-p", "demo").stdout)
        self.phase_next()
        # memory: the old table header is kept (one table), the new row joins the old one, the digest reads both
        self.cli("memory", "feature-add", "-p", "demo", "--title", "Subtract", "--summary", "sub() added", "--capabilities", "calc")
        features = self.memory_file("FEATURES.md")
        self.assertEqual(features.count("| id |"), 1)
        self.assertIn("000-eski", features)
        self.assertIn(self.rid, features)
        self.assertIn("features 2", self.cli("context", "-p", "demo").stdout)
        self.cli("memory", "decision", "-p", "demo", "keep add()")
        self.assertIn("karar", self.memory_file("DECISIONS.md"))
        # the old report, with the machine block exactly as the old version rendered it (Turkish)
        self.write_text("report.md", PACKS["tr"]["report"] + "\n\n" + E.evidence_block(self.proj, self.rid, "tr") + "\n")
        self.assertEqual(self.cli("phase", "check", "-p", "demo", check=False).returncode, 0,
                         "a Turkish evidence block still counts as current in an English session")
        # writing it again replaces the block (once), now in English
        self.cli("report", "evidence", "--write", "-p", "demo")
        rewritten = (self.fdir / "report.md").read_text(encoding="utf-8")
        self.assertEqual(rewritten.count("orkestra:evidence:begin"), 1)
        self.assertIn("### Machine evidence", rewritten)
        self.assertNotIn("Makine kanıtı", rewritten)
        self.assertEqual(self.cli("phase", "check", "-p", "demo", check=False).returncode, 0)
        self.phase_next()                                                   # -> done
        self.assertEqual(json.loads(self.cli("status", "--json", "--run", self.rid, "-p", "demo").stdout)["phase"], "done")

    def memory_file(self, name):
        return (self.proj / ".orkestra" / "memory" / name).read_text(encoding="utf-8")


class TestLegacyRunWithAnUnsupportedLanguageSetting(TestLegacyTurkishRunContinuedInEnglish):
    """ORKESTRA_LANG=de means English, never a crash and never "whatever the OS locale says"."""
    LANG = "de"


# ====================================================================== choosing the language

NEUTRAL = {"ORKESTRA_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "", "LANGUAGE": "", "NO_COLOR": "1"}


class TestChoosingTheLanguage(helpers.Sandbox):
    """What the real CLI prints for every way of asking, valid or not."""

    def setUp(self):
        super().setUp()
        self.proj = self.make_project("demo")

    def run_cli(self, *args, **env):
        merged = dict(NEUTRAL)
        merged.update(env)
        return self.cli(*args, check=False, env=merged)

    def shown(self, **env):
        cp = self.run_cli("lang", "show", **env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        pairs = (line.split(": ", 1) for line in cp.stdout.splitlines() if ": " in line)
        return {k: v for k, v in pairs if k in ("language", "source")}

    def test_the_language_of_every_spelling(self):
        table = [
            ({}, "en"), ({"ORKESTRA_LANG": "en"}, "en"), ({"ORKESTRA_LANG": "EN"}, "en"),
            ({"ORKESTRA_LANG": "tr"}, "tr"), ({"ORKESTRA_LANG": "TR"}, "tr"), ({"ORKESTRA_LANG": "tr_TR.UTF-8"}, "tr"),
            ({"ORKESTRA_LANG": "tr-TR"}, "tr"), ({"ORKESTRA_LANG": "Turkish"}, "tr"), ({"ORKESTRA_LANG": "english"}, "en"),
            # not supported, or not a language at all: English, whatever the OS says
            ({"ORKESTRA_LANG": "de"}, "en"), ({"ORKESTRA_LANG": "de", "LANG": "tr_TR.UTF-8"}, "en"),
            ({"ORKESTRA_LANG": "fr_FR.UTF-8", "LC_ALL": "tr_TR.UTF-8"}, "en"), ({"ORKESTRA_LANG": "klingon"}, "en"),
            ({"ORKESTRA_LANG": "../../etc/passwd"}, "en"), ({"ORKESTRA_LANG": "tr; echo hi"}, "en"),
            ({"ORKESTRA_LANG": "çğü"}, "en"), ({"ORKESTRA_LANG": "-"}, "en"), ({"ORKESTRA_LANG": "0"}, "en"),
            # auto, empty and blank mean "ask the OS"
            ({"ORKESTRA_LANG": "auto", "LANG": "tr_TR.UTF-8"}, "tr"), ({"ORKESTRA_LANG": "AUTO", "LANG": "de_DE.UTF-8"}, "en"),
            ({"ORKESTRA_LANG": "   ", "LANG": "tr_TR.UTF-8"}, "tr"), ({"LANG": "tr_TR.UTF-8"}, "tr"),
            ({"LC_ALL": "tr_TR.UTF-8", "LANG": "en_US.UTF-8"}, "tr"), ({"LC_ALL": "C", "LANG": "tr_TR.UTF-8"}, "tr"),
            ({"LANGUAGE": "de:tr:en"}, "tr"), ({"LANG": "de_DE.UTF-8"}, "en"), ({"LANG": "C"}, "en"),
            ({"LANG": "POSIX"}, "en"), ({"LC_MESSAGES": "tr_TR.UTF-8"}, "tr"),
        ]
        for env, want in table:
            with self.subTest(env=env):
                self.assertEqual(self.shown(**env)["language"], want)

    def test_the_output_follows_the_language_and_unknown_means_english(self):
        envs = (({"ORKESTRA_LANG": "en"}, False), ({"ORKESTRA_LANG": "tr"}, True), ({"ORKESTRA_LANG": "de"}, False),
                ({"ORKESTRA_LANG": "garbage!"}, False), ({"ORKESTRA_LANG": ""}, False), ({"LANG": "tr_TR.UTF-8"}, True),
                ({"ORKESTRA_LANG": "de", "LANG": "tr_TR.UTF-8"}, False))
        for env, turkish in envs:
            for args in (("--help",), ("template", "spec"), ("template", "report"), ("watch", "--once")):
                with self.subTest(env=env, args=args):
                    cp = self.run_cli(*args, **env)
                    self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                    self.assertEqual(bool(TR_LETTERS.search(cp.stdout)), turkish, "%s %s\n%s" % (env, args, cp.stdout[:300]))

    def test_artifacts_are_created_in_the_selected_language(self):
        """`init` writes the memory files and `start` the request in the language that is selected at that moment."""
        want = {"en": ("| id | title |", "# Request:", "## Raw request", "Machine evidence"),
                "tr": ("| id | başlık |", "# İstek:", "## Ham istek", "Makine kanıtı"),
                "de": ("| id | title |", "# Request:", "## Raw request", "Machine evidence")}
        for code, (features_header, request_title, raw_heading, evidence_title) in want.items():
            with self.subTest(language=code):
                proj = self.make_project("demo-" + code)
                self.run_cli("init", "-p", proj.name, ORKESTRA_LANG=code)
                memory = proj / ".orkestra" / "memory"
                self.assertIn(features_header, (memory / "FEATURES.md").read_text(encoding="utf-8"))
                for name in ("PROJECT.md", "FEATURES.md", "DECISIONS.md", "LEARNINGS.md"):
                    text = (memory / name).read_text(encoding="utf-8")
                    if name == "PROJECT.md":    # init replaces the template with the text observed from the checkout
                        self.assertTrue(text.startswith("# Project: " + proj.name), "%s in %s" % (name, code))
                        self.assertIn("## Summary", text)
                        self.assertIn("unknown", text)
                        continue
                    template = (helpers.PLUGIN / "templates" / ("tr" if code == "tr" else "en") / "memory" / name)
                    self.assertEqual(text, template.read_text(encoding="utf-8").replace("{{project}}", proj.name),
                                     "%s in %s" % (name, code))
                out = self.run_cli("start", "-p", proj.name, "--title", "Demo", "--request", "ping", ORKESTRA_LANG=code).stdout
                rid = re.search(r"run=(\S+)", out).group(1)
                request = (proj / ".orkestra" / "features" / rid / "request.md").read_text(encoding="utf-8")
                self.assertIn(request_title, request)
                self.assertIn(raw_heading, request)
                self.assertEqual(bool(TR_LETTERS.search(request)), code == "tr")
                self.assertIn(evidence_title, self.run_cli("report", "evidence", "-p", proj.name, ORKESTRA_LANG=code).stdout)

    def test_unsupported_languages_leave_no_turkish_anywhere(self):
        for garbage in ("de", "xx", "garbage!", ""):
            with self.subTest(language=garbage):
                proj = self.make_project("g-" + (re.sub(r"\W", "", garbage) or "empty"))
                seen = []
                for args in (("init",), ("start", "--title", "Demo", "--request", "ping"), ("status",), ("next",),
                             ("context",), ("tempo",), ("report", "evidence"), ("template", "plan"), ("memory", "show"),
                             ("lang", "show")):
                    cp = self.run_cli("-p", proj.name, *args, ORKESTRA_LANG=garbage)
                    seen.append((args, cp.stdout + cp.stderr))
                    self.assertNotIn("Traceback", cp.stderr, args)
                cp = self.run_cli("watch", "--once", "-p", proj.name, ORKESTRA_LANG=garbage)
                seen.append((("watch",), cp.stdout + cp.stderr))
                cp = self.run_cli("--help", ORKESTRA_LANG=garbage)
                seen.append((("--help",), cp.stdout))
                leaks = [(args, m.group(0)) for args, text in seen for m in TR_LETTERS.finditer(text)]
                self.assertEqual(leaks[:3], [])

    def test_the_flag_the_config_and_the_environment_in_order(self):
        self.home.mkdir(parents=True, exist_ok=True)
        config = self.home / "config.json"
        config.write_text(json.dumps({"lang": "tr"}), encoding="utf-8")
        self.assertEqual(self.shown(), {"language": "tr", "source": "config (%s)" % config})
        # the environment beats the file, the flag beats the environment
        self.assertEqual(self.shown(ORKESTRA_LANG="en")["language"], "en")
        self.assertIn("language: en", self.run_cli("--lang", "en", "lang", "show", ORKESTRA_LANG="tr").stdout)
        self.assertIn("language: tr", self.run_cli("lang", "show", "--lang", "tr").stdout)
        # an unsupported flag is a usage error, not a silent English
        bad = self.run_cli("--lang", "de", "lang", "show")
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        self.assertIn("de", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        # a config value nobody supports falls through to the OS, then to English
        config.write_text(json.dumps({"lang": "de"}), encoding="utf-8")
        self.assertEqual(self.shown(LANG="tr_TR.UTF-8")["language"], "tr")
        self.assertEqual(self.shown(LANG="de_DE.UTF-8")["language"], "en")
        # `orkestra lang <x>` saves, and keeps what else the file holds
        config.write_text(json.dumps({"workspace_root": str(self.ws), "lang": "auto"}), encoding="utf-8")
        saved = self.run_cli("lang", "tr")
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual((data["lang"], data["workspace_root"]), ("tr", str(self.ws)))
        self.assertEqual(self.shown()["language"], "tr")
        self.run_cli("lang", "auto")
        self.assertEqual(self.shown()["language"], "en")



# ====================================================================== the installer-style command

class TestSetupSpeaksTheChosenLanguage(FreshMachine):
    """`orkestra setup` asks its questions and reports in the chosen language (it printed Turkish prompts to everybody)."""

    def without_agents(self) -> str:
        """PATH without the claude stub: nothing for setup to detect."""
        path = self.mkdir("empty-bin")
        self.checkpoint()
        return str(path)

    def test_nothing_to_install_says_so_in_each_language(self):
        nothing = self.without_agents()
        english = self.orc("setup", "--yes", expect=1, PATH=nothing)
        self.assertIn("no engine found", english.out)
        self.assertNotRegex(english.out, TR_LETTERS.pattern)
        self.assertNotIn("motorsuz", english.out)
        turkish = self.orc("setup", "--yes", expect=1, PATH=nothing, ORKESTRA_LANG="tr")
        self.assertIn("motorsuz", turkish.out)
        self.assertRegex(turkish.out, TR_LETTERS.pattern)
        self.assert_nothing_outside()

    def test_the_questions_and_the_answers_follow_the_language(self):
        self.mkdir("cursor-home")                                   # both homes exist, claude is the stub: three engines
        self.mkdir("copilot-home")
        cases = (("en", "n\nn\nn\n", ("Install the Claude marketplace and plugin? [Y/n]",
                                         "Install the Cursor skill, subagents and hooks? [Y/n]",
                                         "Install the Copilot hooks? [Y/n]", "setup: Claude skipped", "setup: Cursor skipped",
                                         "setup: Copilot skipped")),
                 ("tr", "h\nh\nh\n", ("Claude marketplace/plugin kurulsun mu? [E/h]",
                                        "Cursor skill, alt-ajan ve hooks kurulsun mu? [E/h]", "Copilot hooks kurulsun mu? [E/h]",
                                        "setup: Claude atlandı", "setup: Cursor atlandı", "setup: Copilot atlandı")))
        for lang, typed, expected in cases:
            with self.subTest(language=lang):
                res = self.orc("setup", expect=0, typed=typed, ORKESTRA_LANG=lang)
                for needle in expected:
                    self.assertIn(needle, res.out)
                if lang == "en":
                    self.assertNotRegex(res.out, TR_LETTERS.pattern)
                    self.assertNotIn("kurulsun", res.out)
        self.assertFalse((self.tmp / "cursor-home" / "skills").exists(), "every question was answered no")


if __name__ == "__main__":
    unittest.main()
