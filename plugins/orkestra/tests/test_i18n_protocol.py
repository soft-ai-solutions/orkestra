"""Protocol tokens in both languages: every reader and recognizer takes English AND Turkish, every writer follows the
selected language, and a run can be started in one language and continued in the other.

The suite is pinned to Turkish (helpers.py); every test here that depends on the language says which one it means.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import engines as EN
from orkestra_core import evidence as E
from orkestra_core import guards, i18n, intent, memory, paths
from orkestra_core import planparse as PP
from orkestra_core import runs as R
from orkestra_core import tempo as T
from orkestra_core.cli import phase_goal
from orkestra_core.gates import format_results
from orkestra_core.messages import protocol as P
from orkestra_core.state import load_state

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")


@contextlib.contextmanager
def lang(code):
    with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code}):
        yield


def load_dispatch():
    spec = importlib.util.spec_from_file_location("ork_dispatch_protocol", str(helpers.DISPATCH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------- fixtures
# Shapes copied from the legacy suite (test_flow / test_units) and from templates/<lang>/.
TR_SPEC = """---
feature: x
status: draft
---
# Subtract

## Amaç ve değer
Kullanıcı iki sayıyı çıkarabilsin.

## Gereksinimler
- **FR-001**: `sub(a, b)` a-b döndürür MUST.

## Başarı kriterleri
- **SC-001**: `sub(5, 3) == 2` testi geçer.

## Sınırlar
- Her zaman: mevcut add davranışı korunur.
- Asla: bağımlılık ekleme.

## Netleştirmeler
### Oturum 2026-10-04
- Soru gerekmedi — istek tek anlamlı.
"""

EN_SPEC = """---
feature: x
status: draft
---
# Subtract

## Purpose and value
The user can subtract two numbers.

## Requirements
- **FR-001**: `sub(a, b)` returns a-b MUST.

## Success criteria
- **SC-001**: `sub(5, 3) == 2` passes.

## Boundaries
- Always: the existing add behavior is kept.
- Never: add dependencies.

## Clarifications
### Session 2026-10-04
- No questions needed. Reason: the request has a single meaning.
"""

TR_PLAN = """---
feature: x
risk: low
approved_dependencies: []
---
# Plan: Subtract

## Genel kısıtlar
- add() değişmez.

## Görevler

### T001 — sub fonksiyonu
- Dosyalar: `calc.py` (değişir), `tests/test_sub.py` (yeni)
- Bağımlı: —
- Kabul: Given 5 ve 3 When sub Then 2 (FR-001)
- Test: `python3 -m unittest tests.test_sub -q`
- TDD: required

## Test stratejisi ve izlenebilirlik
| FR-001 | test_sub | unit |
| SC-001 | test_sub | unit |
"""

EN_PLAN = """---
feature: x
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


def document(section_ids, picks):
    """One `## heading` + `body-<id>` block per section id; `picks[i]` is 0 (English) or 1 (Turkish) for the i-th."""
    return "".join(f"## {P.SECTIONS[sid][picks[i % len(picks)]]}\nbody-{sid}\n" for i, sid in enumerate(section_ids))


# ====================================================================================== comparison key

class TestFold(unittest.TestCase):
    def test_case_diacritics_and_punctuation_do_not_matter(self):
        same = ["Sınıflandırma", "SINIFLANDIRMA", "siniflandirma", "Siniflandirma", "  sınıflandırma  ", "SINIFLANDIRMA:"]
        self.assertEqual({P.fold(x) for x in same}, {"siniflandirma"})
        self.assertEqual(P.fold("Onayla — uygulamaya başla"), P.fold("onayla - UYGULAMAYA basla"))
        self.assertEqual(P.fold("Onayla — uygulamaya başla"), P.fold("Onayla uygulamaya başla"))
        self.assertEqual(P.fold("İSTİSNALAR"), P.fold("istisnalar"))
        self.assertEqual(P.fold("Apply (orkestra changes only)"), "apply orkestra changes only")
        self.assertEqual(P.fold(None), "")
        self.assertEqual(P.fold(""), "")

    def test_different_words_stay_different(self):
        self.assertNotEqual(P.fold("Clarification"), P.fold("Clarifications"))
        self.assertNotEqual(P.fold("Kapsam içi"), P.fold("Kapsam dışı"))

    def test_ascii_fold_keeps_punctuation(self):
        self.assertEqual(P.ascii_fold("(Oluşturulacak)"), "(olusturulacak)")


class TestProtocolTables(unittest.TestCase):
    def test_every_heading_is_unique_after_folding(self):
        seen = {}
        for sid, pair in P.SECTIONS.items():
            for h in pair:
                key = P.fold(h)
                self.assertNotIn(key, seen, f"{sid}/{h!r} collides with {seen.get(key)}")
                seen[key] = sid

    def test_english_tokens_have_no_turkish_letters(self):
        for sid, (en, _tr) in P.SECTIONS.items():
            self.assertFalse(TR_LETTERS.search(en), sid)
        for key, (en, _tr) in P.REQUEST_FIELDS.items():
            self.assertFalse(TR_LETTERS.search(en), key)
        for key, (en, _tr) in P.TASK_FIELDS.items():
            self.assertFalse(TR_LETTERS.search(en), key)
        for kind, spec in P.PROMPTS.items():
            for pair in (spec["header"], *spec["options"], spec.get("await", ("", ""))):
                self.assertFalse(TR_LETTERS.search(pair[0]), (kind, pair))

    def test_prompt_contract_wording(self):
        """The fixed wording of the AskUserQuestion flows (skills, docs and recognizers share it)."""
        self.assertEqual(P.prompt_header("approve", "en"), "Plan approval")
        self.assertEqual(P.prompt_header("approve", "tr"), "Plan onayı")
        self.assertEqual(P.prompt_options("approve", "en"),
                         ("Approve and start implementing", "I want changes", "Cancel"))
        self.assertEqual(P.prompt_options("approve", "tr"),
                         ("Onayla — uygulamaya başla", "Değişiklik istiyorum", "İptal et"))
        self.assertEqual(P.prompt_await("approve", "en"), "plan approval")
        self.assertEqual(P.prompt_await("approve", "tr"), "plan onayı")
        self.assertEqual(P.prompt_header("rollback", "en"), "Rollback")
        self.assertEqual(P.prompt_header("rollback", "tr"), "Geri alma")
        self.assertEqual(P.prompt_options("rollback", "en"),
                         ("Apply (orkestra changes only)", "Apply including other diffs (--all)", "Never mind"))
        self.assertEqual(P.prompt_options("rollback", "tr"),
                         ("Uygula (yalnız orkestra değişiklikleri)", "Diğer farklar dahil uygula (--all)", "Vazgeç"))
        self.assertEqual(P.SECTIONS["classification"], ("Classification", "Sınıflandırma"))

    def test_hint_phrase(self):
        self.assertEqual(P.prompt_spec("approve", "en"),
                         "header `Plan approval`, options `Approve and start implementing` · `I want changes` · `Cancel`")
        self.assertIn("`Plan onayı`", P.prompt_spec("approve", "tr"))


# ====================================================================================== heading readers

class TestHeadingReaders(unittest.TestCase):
    IDS = [sid for sid in P.SECTIONS if sid not in ("request", "raw_request")]

    def test_english_turkish_and_mixed_documents_read_identically(self):
        docs = {"en": document(self.IDS, [0]), "tr": document(self.IDS, [1]), "mixed": document(self.IDS, [0, 1, 1, 0])}
        results = {}
        for name, doc in docs.items():
            results[name] = [(PP.has_heading(doc, sid), PP.heading_body(doc, sid)) for sid in self.IDS]
        self.assertEqual(results["en"], results["tr"])
        self.assertEqual(results["en"], results["mixed"])
        for sid, (found, body) in zip(self.IDS, results["en"]):
            self.assertTrue(found, sid)
            self.assertEqual(body, f"body-{sid}\n", sid)

    def test_an_id_matches_only_its_own_heading(self):
        for sid in self.IDS:
            for code in (0, 1):
                doc = f"## {P.SECTIONS[sid][code]}\nx\n"
                others = [o for o in self.IDS if o != sid and PP.has_heading(doc, o)]
                self.assertEqual(others, [], f"{P.SECTIONS[sid][code]!r} also read as {others}")

    def test_case_diacritic_and_suffix_variants(self):
        for h in ("NETLESTIRMELER", "netleştirmeler", "Netlestirmeler", "NETLEŞTİRMELER", "CLARIFICATIONS",
                  "clarifications", "Clarifications (session 1)", "Netleştirmeler: oturum 1"):
            self.assertTrue(PP.has_heading(f"## {h}\n", "clarifications"), h)
            self.assertTrue(PP.has_section(f"## {h}\n", "Netleştirmeler", "Clarifications"), h)
        for h in ("ISTISNALAR", "İSTİSNALAR", "Exceptions", "EXCEPTIONS"):
            self.assertEqual(PP.heading_body(f"## {h}\n- `a.py`\n", "exceptions"), "- `a.py`\n", h)

    def test_levels_and_near_misses(self):
        self.assertTrue(PP.has_heading("### Clarifications\n", "clarifications"))     # level 3 counts for has_*
        self.assertFalse(PP.has_heading("# Clarifications\n", "clarifications"))       # level 1 does not
        self.assertFalse(PP.has_heading("#### Clarifications\n", "clarifications"))
        self.assertFalse(PP.has_heading("## Clarification\n", "clarifications"))       # singular
        self.assertFalse(PP.has_heading("Clarifications\n", "clarifications"))         # not a heading
        self.assertFalse(PP.has_heading("##Clarifications\n", "clarifications"))
        self.assertFalse(PP.has_heading("## Classifications of things\n", "classification"))
        self.assertFalse(PP.has_heading("", "classification"))
        self.assertIsNone(PP.heading_body("### Exceptions\n- `a`\n", "exceptions"))     # bodies are level 2 only
        self.assertIsNone(PP.heading_body("nothing here", "exceptions"))

    def test_body_stops_at_the_next_level_two_heading_and_keeps_subsections(self):
        text = "## Tasks\n### T001\nx\n### T002\ny\n## Exceptions\n- `a.py`\n## Plan changelog\nz\n"
        self.assertEqual(PP.heading_body(text, "tasks"), "### T001\nx\n### T002\ny\n")
        self.assertEqual(PP.heading_body(text, "exceptions"), "- `a.py`\n")
        self.assertEqual(PP.heading_body(text, "plan_changelog"), "z\n")
        self.assertEqual(PP.heading_body("## Exceptions", "exceptions"), "")           # heading on the last line

    def test_crlf_documents(self):
        text = "## Netleştirmeler\r\nsoru\r\n## Sınırlar\r\n- a\r\n"
        self.assertTrue(PP.has_heading(text, "clarifications"))
        self.assertEqual(PP.heading_body(text, "boundaries"), "- a\r\n")

    def test_reading_stays_linear_on_big_and_hostile_documents(self):
        big = "".join(f"## Heading {i}\ntext\n### Sub {i}\n" for i in range(4000)) + "## Netleştirmeler\nx\n"
        hostile = "## a" + " " * 200_000 + "b\n" + "##" + " " * 200_000 + "\n" + "## " + "x" * 200_000 + "\n"
        for text in (big, hostile):
            helpers.assert_cpu_under(self, 1.5, lambda: (PP.has_heading(text, "clarifications"),
                                                         PP.heading_body(text, "clarifications"),
                                                         PP.has_section(text, "Netleştirmeler", "Clarifications")),
                                     "heading search")
        self.assertTrue(PP.has_heading(big, "clarifications"))
        self.assertEqual(PP.heading_body(big, "clarifications"), "x\n")

    def test_legacy_name_based_api_still_works(self):
        self.assertTrue(PP.has_section(TR_SPEC, "Netleştirmeler", "Clarifications"))
        self.assertTrue(PP.has_section(EN_SPEC, "Netleştirmeler", "Clarifications"))
        self.assertFalse(PP.has_section(TR_SPEC.replace("## Netleştirmeler", "## Notlar"), "Netleştirmeler", "Clarifications"))
        self.assertIn("add() değişmez", PP.section(TR_PLAN, "Genel kısıtlar", "Global constraints"))
        self.assertIn("add() does not change", PP.section(EN_PLAN, "Genel kısıtlar", "Global constraints"))
        self.assertIsNone(PP.section(TR_PLAN, "Yok böyle"))

    def test_a_turkish_scope_heading_is_not_read_as_the_longer_ones(self):
        self.assertFalse(PP.has_heading("## Kapsam içi\n", "scope"))
        self.assertTrue(PP.has_heading("## Kapsam içi\n", "in_scope"))
        self.assertTrue(PP.has_heading("## Kapsam\n", "scope"))


# ====================================================================================== plan / triage readers

class TestTaskReaders(unittest.TestCase):
    def tasks(self, plan):
        return [(t.id, t.files, t.depends, t.tdd, t.file_entries, sorted(t.fields)) for t in PP.parse_tasks(plan)]

    def test_english_turkish_and_mixed_labels_parse_identically(self):
        tr = "### T001 — a\n- Dosyalar: `a.ts` (değişir), `a.test.ts` (yeni)\n- Bağımlı: —\n- Kabul: x (FR-001)\n- Test: `t`\n- TDD: required\n"
        en = "### T001: a\n- Files: `a.ts` (modified), `a.test.ts` (new)\n- Depends: none\n- Acceptance: x (FR-001)\n- Test: `t`\n- TDD: required\n"
        mixed = "### T001: a\n- Files: `a.ts` (değişir), `a.test.ts` (new)\n- Bağımlı: none\n- Acceptance: x (FR-001)\n- Test: `t`\n- TDD: required\n"
        self.assertEqual(self.tasks(tr), self.tasks(en))
        self.assertEqual(self.tasks(tr), self.tasks(mixed))
        entries = PP.parse_tasks(en)[0].file_entries
        self.assertEqual(entries, [("a.ts", False), ("a.test.ts", True)])

    def test_label_spellings_case_and_diacritics(self):
        body = ("### T001 — a\n- DOSYALAR: `a`\n- **BAĞIMLI**: —\n- kabul KRİTERİ: z\n- Tests: `t`\n- tdd: n/a (docs)\n"
                "### T002 — b\n- Bagimli: T001\n- Depends on: T001\n- Acceptance criteria: z\n- File: `b`\n- Doğrulama: `t`\n")
        t1, t2 = PP.parse_tasks(body)
        self.assertEqual(sorted(t1.fields), ["acceptance", "depends", "files", "tdd", "test"])
        self.assertEqual(t1.tdd, "n/a")
        self.assertEqual(sorted(t2.fields), ["acceptance", "depends", "files", "test"])
        self.assertEqual(t2.depends, ["T001"])

    def test_new_file_markers_in_both_languages(self):
        raw = "- Files: `a` (new), `b` (yeni), `c` (create), `d` (oluşturulacak), `e` (modified), `f` (değişir), `g`"
        got = dict(PP.parse_tasks("### T001 — x\n" + raw + "\n")[0].file_entries)
        self.assertEqual({k for k, v in got.items() if v}, {"a", "b", "c", "d"})
        self.assertEqual({k for k, v in got.items() if not v}, {"e", "f", "g"})

    def test_whole_plans_check_the_same(self):
        spec_ids = "FR-001 SC-001"
        self.assertEqual(PP.check_plan(spec_ids, TR_PLAN), [])
        self.assertEqual(PP.check_plan(spec_ids, EN_PLAN), [])
        mixed = EN_PLAN.replace("- Files:", "- Dosyalar:").replace("(modified)", "(değişir)").replace("(new)", "(yeni)")
        self.assertEqual(PP.check_plan(spec_ids, mixed), [])

    def test_missing_field_messages_use_the_run_language(self):
        bad = "---\napproved_dependencies: []\n---\n### T001: x\n- Files: `a`\n"
        with lang("en"):
            probs = PP.check_plan("FR-001", bad)
        self.assertTrue(any("missing `- Acceptance:` line" in p for p in probs), probs)
        self.assertTrue(any("`- Test:`" in p for p in probs), probs)
        with lang("tr"):
            probs = PP.check_plan("FR-001", bad)
        self.assertTrue(any("missing `- Kabul:` line" in p for p in probs), probs)

    def test_new_marker_hint_follows_the_language(self):
        plan = EN_PLAN.replace("(new)", "")
        with tempfile_dir() as proj:
            with lang("en"):
                probs = PP.check_plan("FR-001 SC-001", plan, proj)
            self.assertTrue(any("`(new)`" in p for p in probs), probs)
            with lang("tr"):
                probs = PP.check_plan("FR-001 SC-001", plan, proj)
            self.assertTrue(any("`(yeni)`" in p for p in probs), probs)

    def test_requirement_coverage_message_names_the_acceptance_label(self):
        with lang("en"):
            self.assertTrue(any("`Acceptance:` line" in p for p in PP.check_plan("FR-001 FR-002", EN_PLAN)))
        with lang("tr"):
            self.assertTrue(any("`Kabul:` line" in p for p in PP.check_plan("FR-001 FR-002", EN_PLAN)))

    def test_triage_status_words(self):
        def open_items(status):
            return PP.open_triage_items(f"| id | src | finding | verdict | status | note |\n| R-001 | a | b | fix | {status} | c |\n")
        for done in ("done", "DONE", "fixed", "tamam", "TAMAM", "yapıldı", "YAPILDI", "yapildi"):
            self.assertEqual(open_items(done), [], done)
        for pending in ("open", "açık", "in progress", "-"):
            self.assertEqual(open_items(pending), ["R-001"], pending)
        en = "| R-001 | reviewer | `a.ts:1` null | fix | done | closed |\n| R-002 | adversary | x | reject | - | false positive |\n"
        tr = "| R-001 | reviewer | `a.ts:1` null | fix | tamam | kapandı |\n| R-002 | adversary | x | reject | - | yanlış pozitif |\n"
        self.assertEqual(PP.open_triage_items(en), PP.open_triage_items(tr))


@contextlib.contextmanager
def tempfile_dir():
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="orkestra-proto-")).resolve()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ====================================================================================== recognizers

class TestApprovalAnswers(unittest.TestCase):
    def test_every_option_of_the_contract_in_both_languages(self):
        for code in ("en", "tr"):
            approve, change, cancel = P.prompt_options("approve", code)
            self.assertEqual(intent.approval_answer(approve), "approve", approve)
            self.assertEqual(intent.approval_answer(change), "reject", change)
            self.assertEqual(intent.approval_answer(cancel), "reject", cancel)

    def test_case_diacritics_and_dash_variants_of_the_approve_label(self):
        for text in ("Approve and start implementing", "approve and start implementing", "APPROVE AND START IMPLEMENTING",
                     "Approve and start implementing.", "  Approve and start implementing  ",
                     "Onayla — uygulamaya başla", "Onayla - uygulamaya başla", "Onayla – uygulamaya başla",
                     "Onayla uygulamaya başla", "onayla uygulamaya basla", "ONAYLA — UYGULAMAYA BAŞLA",
                     "Onayla — uygulamaya başla (hemen)"):
            self.assertEqual(intent.approval_answer(text), "approve", text)

    def test_legacy_short_forms_still_approve(self):
        for text in ("Onayla", "onayla", "approve", "Approve", "onay", "evet", "lgtm", "yes"):
            self.assertEqual(intent.approval_answer(text), "approve", text)

    def test_near_misses_and_change_requests_reject(self):
        for text in ("Approve and start", "Approve and start implementing, but drop T002",
                     "Approve and start implementing later", "Approve and do not start implementing",
                     "Onayla ama T001'i böl", "Onayla — uygulamaya başlama", "Onayla — uygulamaya başla ama T002'yi çıkar",
                     "I want changes", "Cancel", "Değişiklik istiyorum", "İptal et", "Never mind", "no", "later",
                     "Do not approve", "Hayır", "Apply (orkestra changes only)"):
            self.assertEqual(intent.approval_answer(text), "reject", text)
        self.assertIsNone(intent.approval_answer(""))
        self.assertIsNone(intent.approval_answer("   "))
        self.assertIsNone(intent.approval_answer(None))

    def test_typed_english_approval_sentences(self):
        for text in ("approve", "approved", "I approve", "I approve this plan, go ahead", "approve the plan", "approve it",
                     "lgtm", "go ahead", "Plan approved, thanks"):
            self.assertEqual(intent.approval(text), "approve", text)
        for text in ("I want changes", "do not approve it", "I can't approve this yet", "approve it, but drop T002",
                     "should I approve?", "never approve", "let me approve the plan", "if you approve it we are done",
                     "approve the plan?", "ask me to approve the plan"):
            self.assertNotEqual(intent.approval(text), "approve", text)
        self.assertEqual(intent.approval("onaylıyorum"), "approve")                  # Turkish forms unchanged


class TestRollbackAnswers(unittest.TestCase):
    def test_every_option_of_the_contract_in_both_languages(self):
        for code in ("en", "tr"):
            apply_, all_, never = P.prompt_options("rollback", code)
            self.assertEqual(intent.rollback_answer(apply_), "apply", apply_)
            self.assertEqual(intent.rollback_answer(all_), "apply_all", all_)
            self.assertEqual(intent.rollback_answer(never), "cancel", never)

    def test_variants(self):
        for text, want in (("apply (orkestra changes only)", "apply"), ("APPLY ORKESTRA CHANGES ONLY", "apply"),
                           ("Uygula (yalniz orkestra degisiklikleri)", "apply"),
                           ("Diger farklar dahil uygula --all", "apply_all"), ("Apply including other diffs --all", "apply_all"),
                           ("Vazgec", "cancel"), ("never mind", "cancel"),
                           ("/orkestra-rollback apply", "apply"), ("/orkestra:rollback apply", "apply"),
                           ("/orkestra-rollback uygula", "apply"), ("/orkestra-rollback uygula --all", "apply_all"),
                           ("/orkestra-rollback apply --all", "apply_all")):
            self.assertEqual(intent.rollback_answer(text), want, text)

    def test_near_misses_are_none_never_a_guess(self):
        for text in ("", "apply", "uygula", "Apply (orkestra changes only) and push", "Apply all", "Apply everything",
                     "Never mind, apply", "/orkestra-rollback", "/orkestra-rollback apply and push", "yes", "Onayla",
                     "Approve and start implementing", None):
            self.assertIsNone(intent.rollback_answer(text), text)

    def test_typing_an_option_label_is_not_a_discard_grant(self):
        """In a Cursor chat only the slash command grants `orkestra rollback --yes`, never a pasted option label."""
        for code in ("en", "tr"):
            for label in P.prompt_options("rollback", code):
                self.assertNotIn("discard", intent.git_grants(label), label)
        self.assertIn("discard", intent.git_grants("/orkestra-rollback apply"))
        self.assertIn("discard", intent.git_grants("/orkestra-rollback uygula --all"))


class TestDispatchRecognizers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.D = load_dispatch()

    def question(self, code, extra=False):
        header = P.prompt_header("approve", code)
        q = [{"question": P.PROMPTS["approve"]["question"][0 if code == "en" else 1], "header": header}]
        if extra:
            q.append({"question": "Which extra scope?" if code == "en" else "Hangi ek kapsam?", "header": "Extra scope",
                      "multiSelect": True})
        return q

    def test_the_approval_question_is_recognised_in_both_languages(self):
        for code in ("en", "tr"):
            self.assertTrue(self.D.is_approval_question({"tool_input": {"questions": self.question(code)}}), code)
            header_only = [{"question": "?", "header": P.prompt_header("approve", code)}]
            self.assertTrue(self.D.is_approval_question({"tool_input": {"questions": header_only}}), code)
            self.assertTrue(self.D._is_approval_text(P.prompt_await("approve", code)), code)
        for header in ("Rollback", "Geri alma", "Extra scope", "Ek kapsam", "Color", "Which branch?"):
            ev = {"tool_input": {"questions": [{"question": "Pick one", "header": header}]}}
            self.assertFalse(self.D.is_approval_question(ev), header)
        self.assertFalse(self.D.is_approval_question({}))

    def test_answers_decide_in_both_languages(self):
        for code in ("en", "tr"):
            qs = self.question(code)
            qtext = qs[0]["question"]
            approve, change, cancel = P.prompt_options("approve", code)
            for answer, want in ((approve, "approve"), (change, "reject"), (cancel, "reject")):
                ev = {"tool_input": {"questions": qs}, "tool_response": {"questions": qs, "answers": {qtext: answer}}}
                decision, appr, others = self.D.approval_from_answers(ev)
                self.assertEqual((decision, appr, others), (want, [answer], []), (code, answer))

    def test_an_extra_question_never_decides_and_is_handed_back(self):
        for code in ("en", "tr"):
            qs = self.question(code, extra=True)
            approve, change, _ = P.prompt_options("approve", code)
            ev = {"tool_input": {"questions": qs}, "tool_response": {"answers": {
                qs[0]["question"]: approve, qs[1]["question"]: "Cancel" if code == "en" else "İptal et"}}}
            decision, appr, others = self.D.approval_from_answers(ev)
            self.assertEqual(decision, "approve")
            self.assertEqual(others, [(qs[1]["question"], "Cancel" if code == "en" else "İptal et")])
            ev["tool_response"]["answers"][qs[0]["question"]] = change
            self.assertEqual(self.D.approval_from_answers(ev)[0], "reject")

    def test_list_and_text_shaped_answers(self):
        qs = self.question("en")
        qtext = qs[0]["question"]
        ev = {"tool_input": {"questions": qs}, "tool_response": {"answers": [{"question": qtext, "answer": "Approve and start implementing"}]}}
        self.assertEqual(self.D.approval_from_answers(ev)[0], "approve")
        ev = {"tool_input": {"questions": qs},
              "tool_response": f'User has answered your questions: "{qtext}"="Approve and start implementing"'}
        self.assertEqual(self.D.approval_from_answers(ev)[0], "approve")
        ev["tool_response"] = f'User has answered your questions: "{qtext}"="I want changes"'
        self.assertEqual(self.D.approval_from_answers(ev)[0], "reject")


# ====================================================================================== evidence block

GOLDEN_FACTS = {
    "approval": {"decision": "approve", "source": "autopilot", "at": "2026-01-01T00:00:00+00:00", "hash": "abcdef0123456789"},
    "gates": [("unit", "PASS", "PASS"), ("lint", "FAIL", "FAIL"), ("new", "—", "PASS")],
    "comparison": {"ok": False, "pre_existing_failures": ["lint"]},
    "tasks": [("T001", "done", "—", 0, True, ""), ("T002", "done", 1, 0, False, "d1; fn"),
              ("T003", "deferred", "—", "—", True, "why")],
    "integrity": [],
    "exceptions": ["a.py", "b.py"],
    "changed": [{"path": "a.py", "change": "modified"}, {"path": "t/x.py", "change": "added"}],
    "triage": {"fix": 1, "defer": 2, "reject": 3},
}

# What the Turkish-only versions wrote for the same facts (byte for byte: reports of older runs must stay current).
GOLDEN_TR = """<!-- orkestra:evidence:begin (generated by `orkestra report evidence` — do not edit) -->
### Makine kanıtı

- Onay: **approve** · kaynak autopilot · 2026-01-01T00:00:00+00:00 · plan özeti `abcdef012345`

| Gate | Baseline | Son verify |
|---|---|---|
| unit | PASS | PASS |
| lint | FAIL | FAIL |
| new | — | PASS |

Regresyon: VAR / doğrulanmadı · baştan beri kırık: lint

| Görev | Red (exit) | Green (exit) | Not |
|---|---|---|---|
| T001 done | — | 0 | TDD n/a |
| T002 done | 1 | 0 | d1; fn |
| T003 deferred | — | — | TDD n/a · why |

Test bütünlüğü: temiz
Onaylı istisnalar: a.py, b.py
Değişen dosyalar (2): a.py (modified), t/x.py (added)
Review triage: 1 düzeltildi · 2 ertelendi · 3 reddedildi
Commit/push: yapılmadı (değişiklikler çalışma ağacında)
<!-- orkestra:evidence:end -->"""

GOLDEN_EN = """<!-- orkestra:evidence:begin (generated by `orkestra report evidence` — do not edit) -->
### Machine evidence

- Approval: **approve** · source autopilot · 2026-01-01T00:00:00+00:00 · plan digest `abcdef012345`

| Gate | Baseline | Last verify |
|---|---|---|
| unit | PASS | PASS |
| lint | FAIL | FAIL |
| new | — | PASS |

Regression: YES / not verified · broken from the start: lint

| Task | Red (exit) | Green (exit) | Note |
|---|---|---|---|
| T001 done | — | 0 | TDD n/a |
| T002 done | 1 | 0 | d1; fn |
| T003 deferred | — | — | TDD n/a · why |

Test integrity: clean
Approved exceptions: a.py, b.py
Changed files (2): a.py (modified), t/x.py (added)
Review triage: 1 fixed · 2 deferred · 3 rejected
Commit/push: not done (changes are in the working tree)
<!-- orkestra:evidence:end -->"""


class TestEvidenceRendering(unittest.TestCase):
    def test_turkish_block_is_byte_identical_to_what_older_versions_wrote(self):
        self.assertEqual(E._render(GOLDEN_FACTS, "tr"), GOLDEN_TR)

    def test_english_block(self):
        self.assertEqual(E._render(GOLDEN_FACTS, "en"), GOLDEN_EN)
        self.assertFalse(TR_LETTERS.search(GOLDEN_EN))

    def test_variants_without_approval_with_problems_and_a_clean_regression(self):
        facts = dict(GOLDEN_FACTS, approval=None, comparison={"ok": True}, exceptions=[],
                     integrity=["a.py: test count dropped 3 → 2", "Fix the cause instead."])
        en, tr = E._render(facts, "en"), E._render(facts, "tr")
        self.assertIn("- Approval: no record", en)
        self.assertIn("- Onay: kayıt yok", tr)
        self.assertIn("\nRegression: none\n", en)
        self.assertIn("\nRegresyon: yok\n", tr)
        self.assertIn("Test integrity: PROBLEM: a.py: test count dropped 3 → 2\n", en)
        self.assertIn("Test bütünlüğü: SORUN: a.py: test count dropped 3 → 2\n", tr)
        self.assertNotIn("Fix the cause", en)
        self.assertNotIn("exceptions", en.lower())
        self.assertNotIn("istisnalar", tr.lower())

    def test_the_markers_are_language_neutral(self):
        for code in ("en", "tr"):
            block = E._render(GOLDEN_FACTS, code)
            self.assertTrue(block.startswith(E.BEGIN) and block.endswith(E.END), code)

    def test_evidence_catalog_is_complete_in_both_languages(self):
        keys = {k for k in i18n.catalog("en") if k.startswith("evidence.")}
        self.assertTrue(keys)
        self.assertEqual(keys, {k for k in i18n.catalog("tr") if k.startswith("evidence.")})


# ====================================================================================== sandboxed runs

class RunBase(helpers.Sandbox):
    """A real project + CLI subprocesses, with the language chosen per call."""

    def setUp(self):
        super().setUp()
        self.proj = self.make_project("demo")
        self.cli("init", "-p", "demo")
        cfgp = self.proj / ".orkestra" / "config.json"             # no gates: nothing real is run by `gate baseline`
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
        cfg["gates"] = []
        cfgp.write_text(json.dumps(cfg), encoding="utf-8")

    def ork(self, code, *args, check=True):
        return self.cli(*args, env={"ORKESTRA_LANG": code}, check=check)

    def start(self, code, request="sub fonksiyonu ekle", title="Subtract", size="S"):
        out = self.ork(code, "start", "-p", "demo", "--title", title, "--request", request, "--size", size).stdout
        m = re.search(r"\[orkestra:bind\] project=(.+?) run=(\S+)", out)
        self.assertIsNotNone(m, out)
        self.rid = m.group(2)
        self.fdir = self.proj / ".orkestra" / "features" / self.rid
        return out

    def state(self):
        return load_state(self.proj, self.rid)

    def next_text(self, code):
        with lang(code):
            return T.next_action(self.proj, self.state(), deep=True)["text"]


class TestWriters(RunBase):
    def test_start_writes_request_md_in_the_selected_language(self):
        self.start("en", request="add a subtract function")
        en = (self.fdir / "request.md").read_text(encoding="utf-8")
        self.assertTrue(en.startswith("# Request: Subtract\n"), en)
        for label in ("- run: `", "- size: S", "- mode: ", "- engine: ", "- approval mode: autopilot", "- date: ", "## Raw request"):
            self.assertIn(label, en)
        self.assertIn("add a subtract function", en)
        self.assertFalse(TR_LETTERS.search(en), en)
        self.ork("en", "abort", "-p", "demo", "test")
        self.start("tr", request="sub fonksiyonu ekle")
        tr = (self.fdir / "request.md").read_text(encoding="utf-8")
        self.assertTrue(tr.startswith("# İstek: Subtract\n"), tr)
        for label in ("- boyut: S", "- mod: ", "- motor: ", "- onay modu: autopilot", "- tarih: ", "## Ham istek"):
            self.assertIn(label, tr)

    def test_memory_files_come_from_the_selected_language_templates(self):
        for code in ("en", "tr"):
            proj = self.make_project(f"mem-{code}")
            self.ork(code, "init", "-p", f"mem-{code}")
            tpl = helpers.PLUGIN / "templates" / code / "memory"
            observed = (proj / ".orkestra" / "memory" / "PROJECT.md").read_text(encoding="utf-8")
            self.assertTrue(observed.startswith("# Project: " + proj.name), f"{code}/PROJECT.md")
            self.assertIn("## Summary", observed)
            self.assertIn("unknown", observed)
            for name in ("FEATURES.md", "DECISIONS.md", "LEARNINGS.md"):
                want = (tpl / name).read_text(encoding="utf-8").replace("{{project}}", proj.name)
                got = (proj / ".orkestra" / "memory" / name).read_text(encoding="utf-8")
                self.assertEqual(got, want, f"{code}/{name}")

    def test_the_evidence_block_is_written_in_the_selected_language(self):
        self.start("en")
        self.ork("en", "report", "evidence", "--write", "-p", "demo")
        en = (self.fdir / "report.md").read_text(encoding="utf-8")
        self.assertIn("### Machine evidence", en)
        self.assertIn("Commit/push: not done", en)
        self.assertFalse(TR_LETTERS.search(en), en)
        self.ork("tr", "report", "evidence", "--write", "-p", "demo")
        tr = (self.fdir / "report.md").read_text(encoding="utf-8")
        self.assertIn("### Makine kanıtı", tr)
        self.assertNotIn("Machine evidence", tr)                     # replaced, not appended
        self.assertEqual(tr.count(E.BEGIN), 1)

    def test_features_table_is_created_in_the_selected_language(self):
        for code, columns in (("en", "| id | title | date | capabilities | summary |"),
                              ("tr", "| id | başlık | tarih | yetenekler | özet |")):
            proj = self.make_project(f"feat-{code}", git=False)
            with lang(code):
                memory.feature_add(proj, "001-x", "A title", "summary", ["cap"])
            text = (paths.memory_dir(proj) / "FEATURES.md").read_text(encoding="utf-8")
            self.assertIn(columns, text, code)
            self.assertIn("| 001-x | A title |", text)
            self.assertTrue(text.startswith("# Features"))

    def test_gate_table_header(self):
        res = [{"name": "unit", "ok": True, "timed_out": False, "duration_s": 1.0, "cmd": "x", "exit": 0, "log": "l", "tail": ""}]
        with lang("en"):
            out = format_results(res)
        self.assertTrue(out.startswith("| gate | baseline | now | time | command |"), out)
        with lang("tr"):
            out = format_results(res)
        self.assertTrue(out.startswith("| gate | baseline | now | süre | komut |"), out)

    def test_engine_origin_is_corrected_in_either_language(self):
        for code, label in (("en", "engine"), ("tr", "motor")):
            self.start(code)
            self.assertIn(f"- {label}: claude", (self.fdir / "request.md").read_text(encoding="utf-8"))
            with lang(code):
                self.assertTrue(R.record_engine_origin(self.proj, self.rid, "cursor"))
            text = (self.fdir / "request.md").read_text(encoding="utf-8")
            self.assertIn(f"- {label}: cursor", text)
            self.assertNotIn("claude", text.split("##")[0])
            self.ork(code, "abort", "-p", "demo", "again")

    def test_request_scaffold_is_pure_and_explicit_about_the_language(self):
        a = R.request_scaffold("T", "001-t", "S", "interactive", "claude", "autopilot", " body ", "en")
        b = R.request_scaffold("T", "001-t", "S", "interactive", "claude", "autopilot", " body ", "tr")
        self.assertIn("## Raw request\n\nbody\n", a)
        self.assertIn("## Ham istek\n\nbody\n", b)


class TestCrossLanguageContinuation(RunBase):
    """A run started in one language is continued in the other: nothing it already wrote is rejected."""

    def drive(self, start_code, advance_code, spec, plan, classification, flip_report=True):
        self.start(start_code)
        self.assertIn("classify the request", self.next_text(advance_code))
        # intake: the classification the model appended (in the language it was working in)
        with open(self.fdir / "request.md", "a", encoding="utf-8") as fh:
            fh.write(f"\n## {classification}\n- type: feature\n")
        self.assertIn("phase next", self.next_text(advance_code))
        self.ork(advance_code, "phase", "next", "-p", "demo")                      # intake → discover
        (self.fdir / "context.md").write_text("# Context\n" + ("calc.py defines add(); tests in tests/. " * 10), encoding="utf-8")
        self.ork(advance_code, "gate", "baseline", "-p", "demo")
        self.ork(advance_code, "phase", "next", "-p", "demo")                      # → clarify
        (self.fdir / "spec.md").write_text(spec, encoding="utf-8")
        self.ork(advance_code, "phase", "next", "-p", "demo")                      # → plan  (needs the clarifications heading)
        (self.fdir / "plan.md").write_text(plan, encoding="utf-8")
        self.ork(advance_code, "phase", "next", "-p", "demo")                      # → approve (plan check)
        self.assertEqual(self.state()["phase"], "approve")
        self.ork(advance_code, "phase", "next", "-p", "demo")                      # autopilot approval → implement
        st = self.state()
        self.assertEqual(st["phase"], "implement")
        self.assertEqual(list(st["tasks"]), ["T001"])
        self.assertEqual(st["tasks"]["T001"]["files"], ["calc.py", "tests/test_sub.py"])
        self.assertEqual(st["tasks"]["T001"]["tdd"], "required")
        return st

    def test_started_in_turkish_advanced_in_english_with_english_artifacts(self):
        self.drive("tr", "en", EN_SPEC, EN_PLAN, "Classification")

    def test_started_in_english_advanced_in_turkish_with_turkish_artifacts(self):
        self.drive("en", "tr", TR_SPEC, TR_PLAN, "Sınıflandırma")

    def test_started_in_turkish_with_turkish_artifacts_advanced_in_english(self):
        self.drive("tr", "en", TR_SPEC, TR_PLAN, "Sınıflandırma")

    def test_started_and_advanced_in_english(self):
        self.drive("en", "en", EN_SPEC, EN_PLAN, "Classification")

    def test_mixed_artifacts(self):
        spec = EN_SPEC.replace("## Clarifications", "## Netleştirmeler")
        plan = TR_PLAN.replace("- Dosyalar:", "- Files:").replace("(yeni)", "(new)")
        self.drive("en", "tr", spec, plan, "Sınıflandırma")

    def test_intake_hint_follows_the_language_and_either_heading_satisfies_it(self):
        self.start("en")
        self.assertIn("## Classification", self.next_text("en"))
        self.assertIn("## Sınıflandırma", self.next_text("tr"))
        req = self.fdir / "request.md"
        base = req.read_text(encoding="utf-8").split("\n## ")[0] + "\n## Raw request\n\nx\n"
        for heading in ("Classification", "Sınıflandırma", "SINIFLANDIRMA", "classification"):
            req.write_text(base + f"\n## {heading}\n- type: feature\n", encoding="utf-8")
            for code in ("en", "tr"):
                self.assertIn("phase next", self.next_text(code), (heading, code))
        req.write_text(base + "\nClassification: feature\n", encoding="utf-8")        # not a heading
        self.assertIn("classify the request", self.next_text("en"))

    def test_clarify_hints_follow_the_language_and_accept_either_heading(self):
        self.start("en")
        self.ork("en", "phase", "next", "-p", "demo")
        (self.fdir / "context.md").write_text("# Context\n" + ("x " * 150), encoding="utf-8")
        self.ork("en", "gate", "baseline", "-p", "demo")
        self.ork("en", "phase", "next", "-p", "demo")
        self.assertEqual(self.state()["phase"], "clarify")
        self.assertIn("## Clarifications", self.next_text("en"))
        self.assertIn("## Netleştirmeler", self.next_text("tr"))
        incomplete = EN_SPEC.replace("## Clarifications", "## Notes")
        (self.fdir / "spec.md").write_text(incomplete, encoding="utf-8")
        self.assertIn("## Clarifications", self.next_text("en"))
        self.assertIn("## Netleştirmeler", self.next_text("tr"))
        cp = self.ork("en", "phase", "next", "-p", "demo", check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("## Clarifications", cp.stderr)
        cp = self.ork("tr", "phase", "next", "-p", "demo", check=False)
        self.assertIn("## Netleştirmeler", cp.stderr)
        (self.fdir / "spec.md").write_text(TR_SPEC, encoding="utf-8")
        self.assertIn("phase next", self.next_text("en"))
        (self.fdir / "spec.md").write_text(EN_SPEC, encoding="utf-8")
        self.assertIn("phase next", self.next_text("tr"))

    def test_the_approval_hint_carries_the_contract_wording_of_each_language(self):
        out = self.ork("en", "start", "-p", "demo", "--title", "T", "--request", "x", "--size", "S",
                       "--approval-mode", "manual").stdout
        self.rid = re.search(r"run=(\S+)", out).group(1)
        self.fdir = self.proj / ".orkestra" / "features" / self.rid
        st = self.state()
        st["phase"] = "approve"                                    # the hint depends on the phase and the missing approval
        with lang("en"):
            en = T.next_action(self.proj, st, deep=False)["text"]
        with lang("tr"):
            tr = T.next_action(self.proj, st, deep=False)["text"]
        self.assertIn("header `Plan approval`", en)
        self.assertIn("`Approve and start implementing` · `I want changes` · `Cancel`", en)
        self.assertIn("header `Plan onayı`", tr)
        self.assertIn("`Onayla — uygulamaya başla` · `Değişiklik istiyorum` · `İptal et`", tr)
        self.assertFalse(TR_LETTERS.search(en), en)

    def test_report_evidence_block_is_current_in_either_language(self):
        self.start("tr")
        self.ork("tr", "report", "evidence", "--write", "-p", "demo")
        self.assertIn("### Makine kanıtı", (self.fdir / "report.md").read_text(encoding="utf-8"))
        for code in ("tr", "en"):
            with lang(code):
                self.assertEqual(E.report_has_current_block(self.proj, self.rid), (True, ""), code)
        self.ork("en", "report", "evidence", "--write", "-p", "demo")
        self.assertIn("### Machine evidence", (self.fdir / "report.md").read_text(encoding="utf-8"))
        for code in ("tr", "en"):
            with lang(code):
                self.assertEqual(E.report_has_current_block(self.proj, self.rid), (True, ""), code)

    def test_an_edited_or_outdated_block_is_still_refused(self):
        self.start("en")
        self.ork("en", "report", "evidence", "--write", "-p", "demo")
        path = self.fdir / "report.md"
        good = path.read_text(encoding="utf-8")
        for code in ("en", "tr"):
            with lang(code):
                path.write_text(good.replace("Commit/push: not done", "Commit/push: done"), encoding="utf-8")
                ok, why = E.report_has_current_block(self.proj, self.rid)
                self.assertFalse(ok)
                self.assertIn("outdated or was edited", why)
                path.write_text("# no block\n", encoding="utf-8")
                self.assertIn("lacks the machine evidence block", E.report_has_current_block(self.proj, self.rid)[1])
                path.write_text(good, encoding="utf-8")
        with lang("en"):
            self.assertTrue(E.report_has_current_block(self.proj, self.rid)[0])
        # a fact changes after the block was written (an approval appears): outdated in BOTH languages
        (paths.run_dir(self.proj, self.rid) / "approval.json").write_text(
            json.dumps({"decision": "approve", "source": "test", "at": "2026-01-01T00:00:00+00:00", "hash": "0" * 64}),
            encoding="utf-8")
        for code in ("en", "tr"):
            with lang(code):
                self.assertFalse(E.report_has_current_block(self.proj, self.rid)[0], code)

    def test_approved_exceptions_are_read_under_either_heading(self):
        self.start("en")
        approved = paths.run_dir(self.proj, self.rid) / "approved"
        approved.mkdir(parents=True, exist_ok=True)
        for heading in ("Exceptions", "İstisnalar", "ISTISNALAR"):
            (approved / "plan.md").write_text(f"## Tasks\nx\n\n## {heading}\n- `tests/a_test.py` — reason\n\n## New dependencies\n`no.py`\n",
                                              encoding="utf-8")
            with lang("en"):
                self.assertEqual(E.approved_exceptions(self.proj, self.rid), {"tests/a_test.py"}, heading)
            with lang("tr"):
                self.assertEqual(E.approved_exceptions(self.proj, self.rid), {"tests/a_test.py"}, heading)
        (approved / "plan.md").write_text("## Tasks\nx\n", encoding="utf-8")
        self.assertEqual(E.approved_exceptions(self.proj, self.rid), set())


# ====================================================================================== hints

class TestHints(unittest.TestCase):
    def test_phase_goals_name_the_heading_and_prompt_of_the_current_language(self):
        with lang("en"):
            clar, appr = phase_goal("clarify"), phase_goal("approve")
        self.assertIn("## Clarifications", clar)
        self.assertIn("header `Plan approval`", appr)
        self.assertIn("`Approve and start implementing`", appr)
        self.assertFalse(TR_LETTERS.search(clar + appr))
        with lang("tr"):
            clar, appr = phase_goal("clarify"), phase_goal("approve")
        self.assertIn("## Netleştirmeler", clar)
        self.assertIn("header `Plan onayı`", appr)
        self.assertIn("`Onayla — uygulamaya başla`", appr)
        for phase in ("intake", "discover", "plan", "implement", "verify", "review", "deliver", "done"):
            self.assertTrue(phase_goal(phase), phase)
        self.assertEqual(phase_goal("nope"), "")

    def test_cursor_run_prompt_label_follows_the_language(self):
        with lang("en"):
            self.assertTrue(EN.run_prompt("cursor", "add x").endswith("Request: add x"))
            self.assertEqual(EN.run_prompt("claude", "add x"), "/orkestra:run add x")
        with lang("tr"):
            self.assertTrue(EN.run_prompt("cursor", "x ekle").endswith("İstek: x ekle"))

    def test_cursor_approval_examples_are_all_recognised_as_approvals(self):
        for phrase in ("approve", "I approve", "go ahead", "lgtm", "onay", "onaylıyorum", "onay verdim",
                       "bu planı onaylıyorum yap"):
            self.assertIn(f'"{phrase}', EN.CURSOR_NOTES, phrase)
            self.assertEqual(intent.approval(phrase), "approve", phrase)
        self.assertIn('"approve" or "onay"', EN.ASK_FOOTNOTE)

    def test_forgery_message_and_grant_hints_carry_english_examples(self):
        self.assertIn("plan approval", guards.FORGERY_MSG)
        self.assertIn("plan onayı", guards.FORGERY_MSG)
        examples = {"commit": "commit this", "push": "push it", "branch": "create a branch", "pr": "open a PR",
                    "stash": "stash my changes"}
        for cat, phrase in examples.items():
            self.assertIn(phrase, guards.GRANT_HINT[cat], cat)
            self.assertIn(cat, intent.git_grants(phrase), phrase)             # the example really grants what it names
        for cat, phrase in {"commit": "commitle", "push": "pushla", "branch": "yeni branch aç", "pr": "PR aç",
                            "stash": "stashle"}.items():
            self.assertIn(phrase, guards.GRANT_HINT[cat], cat)
            self.assertIn(cat, intent.git_grants(phrase), phrase)
        self.assertIn("discard", intent.git_grants("discard the changes"))

    def test_rollback_skill_is_rewritten_for_cursor_whatever_the_language_of_its_question(self):
        legacy = ("3. Ask with AskUserQuestion (header `Geri alma`): `Uygula (yalnız orkestra değişiklikleri)` · "
                  "`Diğer farklar dahil uygula (--all)` · `Vazgeç`.\n")
        current = ("3. Ask with AskUserQuestion (header `Rollback`): `Apply (orkestra changes only)` · "
                   "`Apply including other diffs (--all)` · `Never mind`. When the conversation is Turkish use the header "
                   "`Geri alma` and the options `Uygula (yalnız orkestra değişiklikleri)`.\n")
        for src in (legacy, current):
            out = EN._rename("1. first\n2. second\n" + src + "4. apply it\n")
            self.assertNotIn("AskUserQuestion", out)
            self.assertIn("`/orkestra-rollback apply`", out)
            self.assertIn("`/orkestra-rollback uygula`", out)
            self.assertIn("END YOUR TURN", out)
            self.assertTrue(out.endswith("4. apply it\n"))
        untouched = "3. Ask with AskUserQuestion (header `Color`): red\n"
        self.assertEqual(EN._rename(untouched), untouched)
        skill = (helpers.PLUGIN / "skills" / "rollback" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("Ask with AskUserQuestion", EN._rename(skill))
        self.assertIn("/orkestra-rollback uygula", EN._rename(skill))


# ====================================================================================== hygiene

class TestOwnedModulesAreLanguageNeutral(unittest.TestCase):
    def test_no_turkish_letters_outside_the_catalogs(self):
        lib = helpers.PLUGIN / "lib" / "orkestra_core"
        for name in ("planparse.py", "evidence.py", "runs.py", "memory.py", "gates.py"):
            text = (lib / name).read_text(encoding="utf-8")
            self.assertEqual(TR_LETTERS.findall(text), [], name)


if __name__ == "__main__":
    unittest.main()
