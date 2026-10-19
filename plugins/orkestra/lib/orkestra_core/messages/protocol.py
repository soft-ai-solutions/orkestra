"""protocol: the tokens of the run artifacts (request / spec / plan / report) and of the AskUserQuestion flows, in each
language, plus the comparison key every reader uses.

`SECTIONS[id] = (english, turkish)`. Templates are written with the heading of their language, every parser accepts
BOTH spellings (`i18n.heading_aliases`), so a run started in one language can be continued in the other. These are
protocol tokens, not display text: change one only together with `templates/<lang>/` and `planparse`.

Nothing here is looked up with `==`: readers compare with `fold()` (case, diacritics and punctuation insensitive),
so `Sınıflandırma`, `SINIFLANDIRMA` and `siniflandirma` are one heading, and `Onayla — uygulamaya başla` equals
`onayla uygulamaya basla`.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Tuple

SECTIONS: Dict[str, Tuple[str, str]] = {
    # request.md
    "request": ("Request", "İstek"),
    "raw_request": ("Raw request", "Ham istek"),
    "classification": ("Classification", "Sınıflandırma"),
    # spec.md
    "purpose": ("Purpose and value", "Amaç ve değer"),
    "reproduction": ("Reproduction", "Yeniden üretim"),
    "scope": ("Scope", "Kapsam"),
    "in_scope": ("In scope", "Kapsam içi"),
    "out_of_scope": ("Out of scope", "Kapsam dışı"),
    "user_stories": ("User stories", "Kullanıcı hikayeleri"),
    "requirements": ("Requirements", "Gereksinimler"),
    "success_criteria": ("Success criteria", "Başarı kriterleri"),
    "edge_matrix": ("Edge-case matrix", "Kenar durum matrisi"),
    "boundaries": ("Boundaries", "Sınırlar"),
    "extra_scope": ("Suggested extra scope (enrichment)", "Önerilen ek kapsam (zenginleştirme)"),
    "clarifications": ("Clarifications", "Netleştirmeler"),
    "assumptions": ("Assumptions and open risks", "Varsayımlar ve açık riskler"),
    # plan.md
    "approach": ("Approach", "Yaklaşım"),
    "code_map": ("Code map", "Kod haritası"),
    "interfaces": ("Interfaces and contracts", "Arayüzler ve sözleşmeler"),
    "global_constraints": ("Global constraints", "Genel kısıtlar"),
    "tasks": ("Tasks", "Görevler"),
    "test_strategy": ("Test strategy and traceability", "Test stratejisi ve izlenebilirlik"),
    "gates": ("Verification gates", "Doğrulama kapıları"),
    "risks": ("Risks and rollback", "Riskler ve geri alma"),
    "exceptions": ("Exceptions", "İstisnalar"),
    "new_dependencies": ("New dependencies", "Yeni bağımlılıklar"),
    "plan_changelog": ("Plan changelog", "Plan değişiklik günlüğü"),
    # report.md
    "delivered": ("What was done and its value", "Ne yapıldı ve ürüne değeri"),
    "evidence": ("Evidence", "Kanıt"),
    "deviations": ("Deviations from the plan", "Plandan sapmalar"),
    "not_done": ("Not done / deferred", "Yapılmayanlar / ertelenenler"),
    "autopilot_assumptions": ("Assumptions I made (autopilot)", "Yaptığım varsayımlar (otopilot)"),
    "how_to_try": ("How to try it", "Nasıl denersin"),
    "followups": ("Risks and follow-ups", "Riskler ve takip önerileri"),
    "commit_suggestion": ("Commit suggestion", "Commit önerisi"),
}

# The `- label: value` lines under the request.md title: (english, turkish) label per field.
REQUEST_FIELDS: Dict[str, Tuple[str, str]] = {
    "run": ("run", "run"),
    "size": ("size", "boyut"),
    "mode": ("mode", "mod"),
    "engine": ("engine", "motor"),
    "approval_mode": ("approval mode", "onay modu"),
    "date": ("date", "tarih"),
}

# The `- Label: value` lines of a plan task: the label each template writes (english, turkish) ...
TASK_FIELDS: Dict[str, Tuple[str, str]] = {
    "files": ("Files", "Dosyalar"),
    "depends": ("Depends", "Bağımlı"),
    "acceptance": ("Acceptance", "Kabul"),
    "test": ("Test", "Test"),
    "tdd": ("TDD", "TDD"),
}
# ... and every spelling the parser takes for that field (a superset of the labels above; compared with `fold`).
TASK_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "files": ("Files", "File", "Dosyalar", "Dosya"),
    "acceptance": ("Acceptance", "Acceptance criteria", "Kabul", "Kabul kriteri", "Kabul kriterleri"),
    "test": ("Test", "Tests", "Testler", "Doğrulama"),
    "tdd": ("TDD",),
    "depends": ("Depends", "Depends on", "Dependencies", "Dependency", "Bağımlı", "Bağımlılık", "Bağımlılıklar"),
}
# `(new)` after a path in a task's file list: the task creates the file (no existence check). Prefixes, any language.
NEW_FILE_MARKERS: Tuple[str, ...] = ("new", "create", "yeni", "oluştur")
NEW_FILE_LABEL: Tuple[str, str] = ("new", "yeni")             # what the template of each language writes (english, turkish)
# Given / When / Then words of an acceptance line, any language (compared with `ascii_fold`).
GWT_WORDS: Tuple[str, ...] = ("given", "when", "then", "verilen", "eger", "ise", "oldugunda", "zaman")
# triage.md: a `fix` row with one of these cells is closed.
TRIAGE_DONE_WORDS: Tuple[str, ...] = ("done", "fixed", "tamam", "yapıldı")

# AskUserQuestion flows: ONE wording, shared by the code that recognises the answers, the hints and the skills.
# options are (english, turkish) pairs in presentation order; `await` is the one-line summary for `orkestra await`.
PROMPTS: Dict[str, Dict[str, object]] = {
    "approve": {
        "header": ("Plan approval", "Plan onayı"),
        "question": ("Do you approve the plan and the spec?", "Plan ve spec'i onaylıyor musun?"),
        "options": (
            ("Approve and start implementing", "Onayla — uygulamaya başla"),
            ("I want changes", "Değişiklik istiyorum"),
            ("Cancel", "İptal et"),
        ),
        "await": ("plan approval", "plan onayı"),
    },
    "rollback": {
        "header": ("Rollback", "Geri alma"),
        "options": (
            ("Apply (orkestra changes only)", "Uygula (yalnız orkestra değişiklikleri)"),
            ("Apply including other diffs (--all)", "Diğer farklar dahil uygula (--all)"),
            ("Never mind", "Vazgeç"),
        ),
    },
}

_TR_ASCII = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                           "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


def ascii_fold(text: object) -> str:
    """Lower-case with Turkish letters (and any other accent) reduced to their ASCII base; punctuation is kept."""
    t = str(text or "").translate(_TR_ASCII).lower()
    return "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))


def fold(text: object) -> str:
    """The comparison key of a protocol token: `ascii_fold`, every run of punctuation/space becomes ONE space."""
    return re.sub(r"[\W_]+", " ", ascii_fold(text)).strip()


def prompt_header(kind: str, code: str) -> str:
    pair = PROMPTS[kind]["header"]
    return pair[1] if code == "tr" else pair[0]            # type: ignore[index]


def prompt_options(kind: str, code: str) -> Tuple[str, ...]:
    return tuple(o[1] if code == "tr" else o[0] for o in PROMPTS[kind]["options"])      # type: ignore[attr-defined]


def prompt_await(kind: str, code: str) -> str:
    pair = PROMPTS[kind]["await"]
    return pair[1] if code == "tr" else pair[0]            # type: ignore[index]


def prompt_spec(kind: str, code: str) -> str:
    """The AskUserQuestion wording as one phrase for a hint: header and options, quoted."""
    return "header `{}`, options {}".format(prompt_header(kind, code),
                                            " · ".join(f"`{o}`" for o in prompt_options(kind, code)))
