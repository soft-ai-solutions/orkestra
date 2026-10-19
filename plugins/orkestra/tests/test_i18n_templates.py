"""Bilingual run documents: templates/en + templates/tr, and the language-neutral model-facing markdown.

What this locks (stdlib only, no network, no writes outside temp dirs):
  * every template exists in English and Turkish, with the same placeholders, the same skeleton (heading levels and
    protocol section ids), the same table shapes, ids and comments, so a run can switch language and keep working;
  * template headings are exactly the `messages/protocol.SECTIONS` spelling of their language, and every section id is
    used by exactly one template (`classification` lives in request.md, which the model writes);
  * the planparse parsers read both languages (task fields, sections, verdict, triage);
  * `orkestra template` serves the right language, with English as the fallback;
  * skills and agents stay English instructions: Turkish appears only as named protocol tokens or trigger phrases, the
    fixed AskUserQuestion wording contract is present (English first, Turkish alternative second);
  * ORKESTRA.md (English) and ORKESTRA.tr.md carry the same structure and link to each other;
  * markdown links in the files these documents live in resolve.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import helpers
from orkestra_core import i18n, planparse
from orkestra_core.messages import protocol

PLUGIN = helpers.PLUGIN
ROOT = PLUGIN.parent.parent
TEMPLATES = PLUGIN / "templates"
SKILLS = PLUGIN / "skills"
AGENTS = PLUGIN / "agents"
LANGS = ("en", "tr")
TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
EM_DASH = "—"
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
IDS = re.compile(r"\b(?:FR-\d{3}|SC-\d{3}|T\d{3}|R-\d{3}|E\d|US\d)\b")

# Templates whose headings are protocol sections (each heading must be a SECTIONS spelling of the template's language).
PROTOCOL_TEMPLATES = ("spec.md", "plan.md", "report.md")
# Every template the CLI and the playbooks rely on.
EXPECTED_TEMPLATES = {"context.md", "plan.md", "report.md", "spec.md", "task-report.md", "triage.md", "verification.md",
                      "memory/DECISIONS.md", "memory/FEATURES.md", "memory/LEARNINGS.md", "memory/PROJECT.md"}
# request.md has no template: runs.py writes its title and `Raw request`, the model appends `Classification`.
REQUEST_MD_SECTIONS = {"request", "raw_request", "classification"}
# Free-form sub-headings that are not protocol sections: a task heading and the clarification session heading.
FREE_HEADING = {"en": re.compile(r"^(T\d{3}\b.*|Session \{\{date\}\})$"), "tr": re.compile(r"^(T\d{3}\b.*|Oturum \{\{date\}\})$")}

FENCE = re.compile(r"^\s*(```|~~~)")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def template_files(lang: str) -> Dict[str, Path]:
    root = TEMPLATES / lang
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*")) if p.is_file()}


def outline(text: str) -> dict:
    """The structure of a markdown document, ignoring all prose: what has to be identical across languages."""
    lines = text.splitlines()
    front: List[str] = []
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                front = [ln.split(":", 1)[0] for ln in lines[1:i] if ":" in ln and not ln.startswith((" ", "-"))]
                start = i + 1
                break
    headings: List[Tuple[int, str]] = []
    tables: List[Tuple[int, int]] = []
    fences = 0
    in_fence = False
    block: List[str] = []
    for ln in lines[start:] + [""]:
        if FENCE.match(ln):
            fences += 1
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.lstrip().startswith("|"):
            block.append(ln)
            continue
        if block:
            cols = {len(b.strip().strip("|").split("|")) for b in block}
            tables.append((cols.pop() if len(cols) == 1 else -1, len(block)))
            block = []
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", ln)
        if m:
            headings.append((len(m.group(1)), m.group(2)))
    return {"front": front, "headings": headings, "tables": tables, "fences": fences,
            "comments": len(re.findall(r"<!--", text)), "ids": Counter(IDS.findall(text)),
            "placeholders": Counter(PLACEHOLDER.findall(text))}


def section_id(text: str, lang: str):
    col = LANGS.index(lang)
    for sid, pair in protocol.SECTIONS.items():
        if pair[col] == text:
            return sid
    return None


def heading_keys(text: str, lang: str, protocol_doc: bool) -> List[Tuple[int, str]]:
    """[(level, key)]: `title` for the H1, a section id for protocol headings, `free` for task/session headings,
    otherwise the heading text itself (model-facing templates keep identical English headings in both languages)."""
    keys: List[Tuple[int, str]] = []
    for level, head in outline(text)["headings"]:
        if level == 1:
            keys.append((1, "title"))
        elif FREE_HEADING[lang].match(head):
            keys.append((level, "free"))
        else:
            sid = section_id(head, lang)
            keys.append((level, sid if sid else ("?" + head if protocol_doc else head)))
    return keys


def strip_code(text: str) -> str:
    out: List[str] = []
    in_fence = False
    for ln in text.splitlines():
        if FENCE.match(ln):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(re.sub(r"`[^`\n]*`", "", ln))
    return "\n".join(out)


LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def broken_links(path: Path) -> List[str]:
    """Relative file links of a markdown file that point at nothing (anchors, URLs and code samples are skipped)."""
    bad = []
    for target in LINK.findall(strip_code(read(path))):
        if target.startswith("#") or SCHEME.match(target) or any(ch in target for ch in "<>{}"):
            continue
        rel = target.split("#", 1)[0]
        if rel and not (path.parent / rel).exists():
            bad.append(target)
    return bad


def front_matter(text: str) -> Dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    fm: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


class TestTemplateFiles(unittest.TestCase):
    def test_both_languages_hold_the_same_templates(self):
        en, tr = template_files("en"), template_files("tr")
        self.assertEqual(sorted(set(en) ^ set(tr)), [], "template present in one language only")
        self.assertEqual(set(en), EXPECTED_TEMPLATES)

    def test_no_legacy_root_layout(self):
        stray = [p.name for p in TEMPLATES.iterdir() if p.is_file()]
        self.assertEqual(stray, [], "templates live in templates/<lang>/ only (i18n.template_dir)")
        self.assertEqual(sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir()), sorted(LANGS))

    def test_files_are_portable_utf8(self):
        for lang in LANGS:
            for rel, path in template_files(lang).items():
                raw = path.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{lang}/{rel}: BOM")
                self.assertTrue(raw.endswith(b"\n"), f"{lang}/{rel}: no final newline")
                self.assertTrue(raw.strip(), f"{lang}/{rel}: empty")
                # a Windows checkout may hold CRLF (git autocrlf); text mode folds it, a lone CR would survive
                self.assertNotIn("\r", raw.decode("utf-8").replace("\r\n", "\n"), f"{lang}/{rel}: stray carriage return")

    def test_english_templates_are_english_and_free_of_the_em_dash(self):
        for rel, path in template_files("en").items():
            text = read(path)
            self.assertEqual(TR_LETTERS.findall(text), [], f"en/{rel}: Turkish letters")
            self.assertNotIn(EM_DASH, text, f"en/{rel}: em dash in new user-visible text")

    def test_human_facing_templates_are_translated_not_copied(self):
        en, tr = template_files("en"), template_files("tr")
        for rel in PROTOCOL_TEMPLATES:
            self.assertTrue(TR_LETTERS.search(read(tr[rel])), f"tr/{rel} is not Turkish")
            self.assertNotEqual(read(en[rel]), read(tr[rel]), rel)
            en_lines = set(read(en[rel]).splitlines())
            copied = [ln for ln in read(tr[rel]).splitlines() if ln in en_lines and not ln.startswith("|")
                      and len(re.sub(r"`[^`]*`|\{\{\w+\}\}|[^A-Za-z]", "", ln)) > 20]
            self.assertEqual(copied, [], f"{rel}: lines identical in en and tr (untranslated?)")

    def test_placeholders_ids_and_skeleton_match_across_languages(self):
        en, tr = template_files("en"), template_files("tr")
        for rel in sorted(EXPECTED_TEMPLATES):
            a, b = outline(read(en[rel])), outline(read(tr[rel]))
            self.assertEqual(a["placeholders"], b["placeholders"], f"{rel}: {{{{placeholders}}}} differ")
            self.assertEqual(a["front"], b["front"], f"{rel}: front matter keys differ")
            self.assertEqual(a["tables"], b["tables"], f"{rel}: table shapes differ")
            self.assertEqual(a["fences"], b["fences"], f"{rel}: code fences differ")
            self.assertEqual(a["comments"], b["comments"], f"{rel}: HTML comments differ")
            self.assertEqual(a["ids"], b["ids"], f"{rel}: FR-/SC-/T/R/E/US ids differ")
            self.assertEqual(heading_keys(read(en[rel]), "en", rel in PROTOCOL_TEMPLATES),
                             heading_keys(read(tr[rel]), "tr", rel in PROTOCOL_TEMPLATES), f"{rel}: heading skeleton differs")

    def test_known_placeholders_only(self):
        known = {"run_id", "title", "project", "date", "size", "head", "task"}
        for lang in LANGS:
            for rel, path in template_files(lang).items():
                self.assertLessEqual(set(PLACEHOLDER.findall(read(path))), known, f"{lang}/{rel}")
                self.assertEqual(len(re.findall(r"\{\{|\}\}", read(path))), 2 * len(PLACEHOLDER.findall(read(path))),
                                 f"{lang}/{rel}: stray braces")


class TestProtocolHeadings(unittest.TestCase):
    def test_protocol_template_headings_are_the_language_spelling_of_sections(self):
        for lang in LANGS:
            files = template_files(lang)
            for rel in PROTOCOL_TEMPLATES:
                for key in heading_keys(read(files[rel]), lang, True):
                    self.assertFalse(key[1].startswith("?"), f"{lang}/{rel}: heading {key[1][1:]!r} is not a SECTIONS spelling")

    def test_a_heading_of_the_other_language_is_not_accepted(self):
        """tr/ must not drift to the English spelling and en/ must not keep the Turkish one."""
        en_only = {pair[0] for pair in protocol.SECTIONS.values()} - {pair[1] for pair in protocol.SECTIONS.values()}
        for rel in PROTOCOL_TEMPLATES:
            tr_heads = {h for _, h in outline(read(template_files("tr")[rel]))["headings"]}
            en_heads = {h for _, h in outline(read(template_files("en")[rel]))["headings"]}
            self.assertEqual(tr_heads & en_only, set(), f"tr/{rel} uses an English section heading")
            self.assertEqual(en_heads & {pair[1] for pair in protocol.SECTIONS.values()}, set(),
                             f"en/{rel} uses a Turkish section heading")

    def test_every_section_is_in_exactly_one_template_in_the_same_order(self):
        for lang in LANGS:
            files = template_files(lang)
            seen: List[str] = []
            for rel in PROTOCOL_TEMPLATES:
                ids = [k for _, k in heading_keys(read(files[rel]), lang, True) if k not in ("title", "free")]
                self.assertEqual(len(ids), len(set(ids)), f"{lang}/{rel}: duplicate section")
                seen += ids
            self.assertEqual(sorted(seen), sorted(set(protocol.SECTIONS) - REQUEST_MD_SECTIONS),
                             f"{lang}: a section is missing from the templates (request.md sections are written by runs.py)")
        en_order = [k for rel in PROTOCOL_TEMPLATES for _, k in heading_keys(read(template_files("en")[rel]), "en", True)]
        tr_order = [k for rel in PROTOCOL_TEMPLATES for _, k in heading_keys(read(template_files("tr")[rel]), "tr", True)]
        self.assertEqual(en_order, tr_order)

    def test_parsers_find_the_sections_in_both_languages(self):
        for lang in LANGS:
            files = template_files(lang)
            spec, plan = read(files["spec.md"]), read(files["plan.md"])
            self.assertTrue(planparse.has_section(spec, *i18n.heading_aliases("clarifications")), lang)
            self.assertIsNotNone(planparse.section(plan, *i18n.heading_aliases("global_constraints")), lang)
            self.assertIsNotNone(planparse.section(plan, *i18n.heading_aliases("exceptions")), lang)
            self.assertIsNotNone(planparse.section(spec, *i18n.heading_aliases("boundaries")), lang)
            for sid in ("purpose", "requirements", "success_criteria", "edge_matrix", "assumptions"):
                self.assertTrue(planparse.has_section(spec, *i18n.heading_aliases(sid)), (lang, sid))

    def test_plan_tasks_parse_the_same_in_both_languages(self):
        shapes = {}
        for lang in LANGS:
            tasks = planparse.parse_tasks(read(template_files(lang)["plan.md"]))
            self.assertEqual([t.id for t in tasks], ["T001", "T002"], lang)
            for t in tasks:
                self.assertEqual(sorted(t.fields), ["acceptance", "depends", "files", "tdd", "test"], f"{lang} {t.id}")
                self.assertEqual(t.tdd, "required", f"{lang} {t.id}")
            self.assertEqual(tasks[1].depends, ["T001"], lang)
            self.assertEqual(sorted(tasks[0].files), ["src/a.test.ts", "src/a.ts"], lang)
            self.assertEqual([new for _, new in tasks[0].file_entries][-1], True, f"{lang}: the new-file marker is not read")
            shapes[lang] = [(t.id, sorted(t.fields)) for t in tasks]
        self.assertEqual(shapes["en"], shapes["tr"])

    def test_requirement_ids_and_frontmatter_read_the_same(self):
        for lang in LANGS:
            files = template_files(lang)
            self.assertEqual(planparse.spec_ids(read(files["spec.md"])), ["FR-001", "FR-002", "SC-001"], lang)
            fm, _ = planparse.parse_frontmatter(read(files["plan.md"]))
            self.assertEqual(sorted(fm), ["approved_dependencies", "baseline_head", "feature", "risk"], lang)
            self.assertEqual(fm["approved_dependencies"], [], lang)

    def test_verdict_placeholder_and_triage_example_do_not_count(self):
        for lang in LANGS:
            files = template_files(lang)
            verification = read(files["verification.md"])
            self.assertIsNone(planparse.verdict(verification), f"{lang}: the VERDICT placeholder must not be a verdict")
            self.assertEqual(planparse.verdict(verification.replace("<PASS|FAIL>", "PASS")), "PASS", lang)
            self.assertEqual(planparse.open_triage_items(read(files["triage.md"])), [], f"{lang}: example rows must be closed")

    def test_features_table_header_lets_the_cli_append_rows(self):
        for lang in LANGS:
            text = read(template_files(lang)["memory/FEATURES.md"])
            self.assertIn("| id |", text, lang)           # memory.feature_add keeps the template's header only then


class TestCliServesTheRightLanguage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-tpl-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def template(self, name: str, lang: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CURSOR"))}
        env.update({"ORKESTRA_HOME": str(self.tmp / "home"), "ORKESTRA_WORKSPACE": str(self.tmp / "ws"),
                    "ORKESTRA_LANG": lang, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        cp = subprocess.run([sys.executable, str(helpers.BIN), "template", name], cwd=str(self.tmp), env=env,
                            capture_output=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_template_command_prints_the_file_of_the_active_language(self):
        for lang in LANGS:
            for rel, path in template_files(lang).items():
                name = rel[:-3]
                self.assertEqual(self.template(name, lang).strip(), read(path).strip(), f"{lang}: orkestra template {name}")

    def test_english_output_has_no_turkish_letters(self):
        for rel in sorted(EXPECTED_TEMPLATES):
            out = self.template(rel[:-3], "en")
            self.assertEqual(TR_LETTERS.findall(out), [], rel)

    def test_unsupported_language_falls_back_to_english(self):
        self.assertEqual(self.template("spec", "de").strip(), read(TEMPLATES / "en" / "spec.md").strip())

    def test_the_documented_language_switches_exist(self):
        """ORKESTRA.md names `orkestra lang show` and `--lang en|tr`: both must work on the real CLI."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CURSOR", "ORKESTRA_LANG"))}
        env.update({"ORKESTRA_HOME": str(self.tmp / "home"), "ORKESTRA_WORKSPACE": str(self.tmp / "ws"),
                    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "LC_ALL": "C", "LANG": "C", "LANGUAGE": ""})
        shown = subprocess.run([sys.executable, str(helpers.BIN), "lang", "show"], cwd=str(self.tmp), env=env,
                               capture_output=True, encoding="utf-8")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertRegex(shown.stdout.splitlines()[0], r"\ben\b")             # English when nothing selects a language
        once = subprocess.run([sys.executable, str(helpers.BIN), "--lang", "tr", "template", "spec"], cwd=str(self.tmp),
                              env=env, capture_output=True, encoding="utf-8")
        self.assertEqual(once.returncode, 0, once.stderr)
        self.assertEqual(once.stdout.strip(), read(TEMPLATES / "tr" / "spec.md").strip())

    def test_template_dir_resolves_each_language_folder(self):
        for lang in LANGS:
            self.assertEqual(i18n.template_dir(TEMPLATES, lang), TEMPLATES / lang)


class TestSkillsAndAgents(unittest.TestCase):
    # Turkish that skills and agents may contain, on purpose: trigger phrases, the Turkish half of the fixed
    # AskUserQuestion wording, protocol section names the model must write when the conversation is Turkish, and
    # one input example. Anything else with a Turkish letter is Turkish instruction text that slipped back in.
    ALLOWED_TURKISH = (
        "kaldığın yerden", "projeyi tanı", "nerede kaldık",
        "Plan ve spec'i onaylıyor musun?", "Plan onayı", "plan onayı", "Onayla — uygulamaya başla", "Değişiklik istiyorum",
        "İptal et", "bu planı onaylıyorum yap …", "onaylıyorum", "Onaylamak için \"onay\" yaz; değişiklik için ne değişsin yaz.",
        "Geri alma", "Uygula (yalnız orkestra değişiklikleri)", "Diğer farklar dahil uygula (--all)", "Vazgeç",
        "Sınıflandırma", "Tür", "Etkilenen yüzeyler",
        "Netleştirmeler", "Varsayımlar ve açık riskler", "(Önerilen)", "Kapsam dışı", "Plan değişiklik günlüğü",
        "İstisnalar", "Yapılmayanlar / ertelenenler", "Commit önerisi", "hiçbir şey commit'lenmedi", "Bağımlı",
        "İ/ı", "Yaptığım varsayımlar (otopilot)", "Yeniden üretim",
    )
    UNCONDITIONAL_TURKISH = (
        re.compile(r"(?i)\b(?:in|to|answer|report)\b[^\n.;:(]{0,25}\bTurkish\b"
                   r"(?!\s*(?:when|conversation|plan|spec|report|template|characters))"),
        re.compile(r"\(Turkish\s*[,)]|\bTurkish, short\b"),
    )

    @staticmethod
    def documents() -> List[Path]:
        return sorted(SKILLS.rglob("*.md")) + sorted(AGENTS.glob("*.md"))

    def test_front_matter_parses_and_names_match(self):
        for path in sorted(SKILLS.glob("*/SKILL.md")) + sorted(AGENTS.glob("*.md")):
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{path.name}: BOM hides the front matter")
            self.assertNotIn("\r", raw.decode("utf-8").replace("\r\n", "\n"), f"{path.name}: stray carriage return")
            text = read(path)                       # text mode folds CRLF, as the Cursor generator reads it
            self.assertTrue(text.startswith("---\n"), path)
            fm = front_matter(text)
            self.assertTrue(fm.get("name"), f"{path}: no name")
            self.assertTrue(fm.get("description"), f"{path}: no description")
            expected = path.parent.name if path.name == "SKILL.md" else path.stem
            self.assertEqual(fm["name"], expected, path)
            if "argument-hint" in fm:
                self.assertEqual(TR_LETTERS.findall(fm["argument-hint"]), [], path)
                self.assertNotRegex(fm["argument-hint"], r"\b(?:proje|istek|mesaj)\b", f"{path}: argument-hint is not English")

    def test_cursor_generator_reads_every_skill_and_agent(self):
        from orkestra_core import cursorkit
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            text = cursorkit.skill_text(path.parent.name)
            self.assertIn(f"name: orkestra-{path.parent.name}", text)
            self.assertRegex(text, r'description: ".{20,}"')
        for path in sorted(AGENTS.glob("*.md")):
            text = cursorkit.agent_text(path.stem)
            self.assertIn(f"name: orkestra-{path.stem}", text)
            self.assertRegex(text, r'description: ".{20,}"')

    def test_trigger_phrases_for_both_languages_are_kept(self):
        desc = front_matter(read(SKILLS / "resume" / "SKILL.md"))["description"]
        for phrase in ("resume", "continue", "devam", "kaldığın yerden"):
            self.assertIn(phrase, desc)

    def test_turkish_is_confined_to_the_named_vocabulary(self):
        text_by_file = {p: read(p) for p in self.documents()}
        for p, text in text_by_file.items():
            rest = text
            for phrase in sorted(self.ALLOWED_TURKISH, key=len, reverse=True):
                rest = rest.replace(phrase, " ")
            left = [ln.strip() for ln in rest.splitlines() if TR_LETTERS.search(ln)]
            self.assertEqual(left, [], f"{p.relative_to(PLUGIN)}: Turkish text outside the allowed vocabulary")
        everything = "\n".join(text_by_file.values())
        unused = [ph for ph in self.ALLOWED_TURKISH if ph not in everything]
        self.assertEqual(unused, [], "stale entries in ALLOWED_TURKISH: remove them")

    def test_no_instruction_demands_turkish_unconditionally(self):
        legacy = ("Talk to the user in **Turkish**, briefly", "Answer in Turkish in", "Report in Turkish in",
                  "push result in Turkish.", "Show the two groups in Turkish:", "(Turkish, ≤ 25 lines)",
                  "one sentence (Turkish)", "for this run (Turkish)", "Turkish, short headers", "plan.md in Turkish (user")
        for sample in legacy:
            self.assertTrue(any(rx.search(sample) for rx in self.UNCONDITIONAL_TURKISH), f"detector misses: {sample}")
        for p in self.documents():
            for n, line in enumerate(read(p).splitlines(), 1):
                for rx in self.UNCONDITIONAL_TURKISH:
                    self.assertIsNone(rx.search(line), f"{p.relative_to(PLUGIN)}:{n}: Turkish is demanded unconditionally")

    def test_orchestrator_follows_the_users_language(self):
        run = read(SKILLS / "run" / "SKILL.md")
        self.assertRegex(run, r"language they write in")
        self.assertIn("English by default", run)
        for name in ("status", "onboard", "ship", "rollback"):
            self.assertIn("English by default", read(SKILLS / name / "SKILL.md"), name)

    def test_fixed_wording_contract_english_first_turkish_second(self):
        """The AskUserQuestion wording in the playbooks is the one in `protocol.PROMPTS` (which the recognisers use)."""
        docs = {"approve": read(SKILLS / "run" / "references" / "phase-approve.md"),
                "rollback": read(SKILLS / "rollback" / "SKILL.md")}
        for kind, doc in docs.items():
            spec = protocol.PROMPTS[kind]
            pairs = [spec["header"], *spec["options"]] + ([spec["question"]] if "question" in spec else [])
            for en_token, tr_token in pairs:
                self.assertIn(f"`{en_token}`", doc, f"{kind}: {en_token}")
                self.assertIn(f"`{tr_token}`", doc, f"{kind}: {tr_token}")
                self.assertLess(doc.index(f"`{en_token}`"), doc.index(f"`{tr_token}`"), f"{kind}: {en_token}: English first")
        headless = read(SKILLS / "run" / "references" / "headless.md")
        en_wait, tr_wait = (f'orkestra await "{w}"' for w in protocol.PROMPTS["approve"]["await"])
        self.assertIn(en_wait, headless)
        self.assertIn(tr_wait, headless)
        self.assertLess(headless.index(en_wait), headless.index(tr_wait))
        # the hook recognises the approval question by "approv" / "onay" in its header and question text
        asked = [ln for ln in docs["approve"].splitlines() if ln.strip().startswith(("- header:", "- question:"))]
        self.assertEqual(len(asked), 2)
        for line in asked:
            self.assertIn("approv", line.lower(), line)
            self.assertIn("onay", line.lower(), line)

    def test_documents_name_the_section_spellings_of_the_catalog(self):
        refs = {
            SKILLS / "run" / "references" / "phase-intake.md": ("classification",),
            SKILLS / "run" / "references" / "phase-clarify.md": ("clarifications", "assumptions"),
            SKILLS / "run" / "references" / "phase-implement.md": ("plan_changelog",),
            SKILLS / "run" / "references" / "phase-verify.md": ("exceptions",),
            SKILLS / "run" / "references" / "phase-review.md": ("not_done",),
            SKILLS / "ship" / "SKILL.md": ("commit_suggestion",),
            AGENTS / "architect.md": ("clarifications", "out_of_scope", "risks"),
        }
        for path, ids in refs.items():
            text = read(path)
            for sid in ids:
                en, tr = protocol.SECTIONS[sid]
                self.assertIn(en, text, f"{path.name}: English heading of {sid} ({en!r})")
                self.assertIn(tr, text, f"{path.name}: Turkish heading of {sid} ({tr!r})")
                self.assertLess(text.index(en), text.index(tr), f"{path.name}: {sid}: English first")
        intake = read(SKILLS / "run" / "references" / "phase-intake.md")
        self.assertIn("## " + protocol.SECTIONS["classification"][0], intake)
        self.assertIn("## " + protocol.SECTIONS["classification"][1], intake)

    def test_architect_uses_the_task_labels_of_the_templates(self):
        arch = read(AGENTS / "architect.md")

        def task_labels(lang: str) -> List[str]:
            body = read(template_files(lang)["plan.md"]).split("## " + protocol.SECTIONS["tasks"][LANGS.index(lang)], 1)[1]
            seen: List[str] = []
            for label in re.findall(r"^- (\w+):", body.split("\n## ", 1)[0], re.M):
                if label not in seen:
                    seen.append(label)
            return seen

        for index, lang in enumerate(LANGS):
            self.assertEqual(task_labels(lang), [pair[index] for pair in protocol.TASK_FIELDS.values()], lang)
        for en_label, tr_label in protocol.TASK_FIELDS.values():
            self.assertIn(f"`- {en_label}:`", arch)
            self.assertIn(tr_label, arch)
        for key in protocol.TASK_FIELDS:
            self.assertIn(key, planparse.FIELD_ALIASES)

    def test_templates_named_by_the_documents_exist_in_both_languages(self):
        named = set()
        for p in self.documents():
            named |= set(re.findall(r"orkestra template ([a-z][\w/-]*)", read(p)))
        self.assertTrue({"spec", "plan", "report", "context", "triage", "task-report", "verification"} <= named, named)
        for lang in LANGS:
            have = {rel[:-3] for rel in template_files(lang)}
            self.assertEqual(sorted(named - have), [], f"{lang}: a document names a template that does not exist")


class TestReferenceDocuments(unittest.TestCase):
    EN = ROOT / "ORKESTRA.md"
    TR = ROOT / "ORKESTRA.tr.md"

    @staticmethod
    def file_spans(text: str) -> set:
        spans = set(re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", text, flags=re.S)))
        # each language names the architecture page of its own language: the Turkish page is the twin of the English one
        return {s.replace("orkestra-mimari.html", "architecture.html")
                for s in spans if re.search(r"\.(?:py|md|json|jsonl|sh|html|log)$", s)}

    def test_they_link_to_each_other_at_the_top(self):
        self.assertIn("(ORKESTRA.tr.md)", "\n".join(read(self.EN).splitlines()[:6]))
        self.assertIn("(ORKESTRA.md)", "\n".join(read(self.TR).splitlines()[:6]))

    def test_english_is_english(self):
        lines = [ln for ln in read(self.EN).splitlines() if "ORKESTRA.tr.md" not in ln and TR_LETTERS.search(ln)]
        self.assertEqual(lines, [])
        self.assertNotIn(EM_DASH, read(self.EN))

    def test_turkish_is_turkish(self):
        text = read(self.TR)
        self.assertGreater(len(TR_LETTERS.findall(text)), 500)
        self.assertIn("## Kapsam", text)                    # the section legacy tests and readers look for

    def test_same_structure_in_both(self):
        a, b = outline(read(self.EN)), outline(read(self.TR))
        self.assertEqual([lvl for lvl, _ in a["headings"]], [lvl for lvl, _ in b["headings"]], "heading levels")
        self.assertEqual(a["tables"], b["tables"], "table shapes")
        self.assertEqual(a["fences"], b["fences"])
        self.assertEqual(self.file_spans(read(self.EN)), self.file_spans(read(self.TR)), "files and paths named")
        blocks = [[re.sub(r"[ \t]+#[^\n]*", "", b) for b in re.findall(r"```[^\n]*\n(.*?)```", read(p), re.S)]
                  for p in (self.EN, self.TR)]
        self.assertEqual(blocks[0], blocks[1], "command blocks differ")
        cross = {"ORKESTRA.md", "ORKESTRA.tr.md"}
        links = [sorted(t for t in LINK.findall(read(path)) if t not in cross) for path in (self.EN, self.TR)]
        self.assertEqual(links[0], links[1], "links differ")

    def test_language_section_matches_the_implementation(self):
        text = read(self.EN)
        section = text.split("## Language", 1)[1].split("\n## ", 1)[0]
        for needle in ("ORKESTRA_LANG", "config.json", "`lang`", *i18n._LOCALE_VARS, "English"):
            self.assertIn(needle, section)
        steps = re.findall(r"^\d\. (.*)$", section, re.M)
        self.assertEqual(len(steps), 4, steps)
        for step, needle in zip(steps, ("ORKESTRA_LANG", "config.json", "LC_ALL", "English")):
            self.assertIn(needle, step, "resolution order differs from i18n.lang")
        self.assertIn("lib/orkestra_core/messages/protocol.py", section)
        for needle in ("orkestra lang show", "orkestra lang en|tr|auto", "--lang en|tr"):
            self.assertIn(needle, section)
        self.assertIn("templates/en/", section)
        tr_section = read(self.TR).split("## Dil", 1)[1].split("\n## ", 1)[0]
        tr_steps = re.findall(r"^\d\. (.*)$", tr_section, re.M)
        self.assertEqual(len(tr_steps), 4, tr_steps)
        for step, needle in zip(tr_steps, ("ORKESTRA_LANG", "config.json", "LC_ALL", "İngilizce")):
            self.assertIn(needle, step, "resolution order differs from i18n.lang (tr)")
        for needle in ("ORKESTRA_LANG", "config.json", "`lang`", *i18n._LOCALE_VARS, "messages/protocol.py", "templates/tr/",
                       "orkestra lang show", "orkestra lang en|tr|auto", "--lang en|tr"):
            self.assertIn(needle, tr_section)

    def test_scope_section_keeps_the_facts_legacy_tests_check(self):
        en = read(self.EN).split("## Scope", 1)[1].split("\n## ", 1)[0]
        tr = read(self.TR).split("## Kapsam", 1)[1].split("\n## ", 1)[0]
        for section in (en, tr):
            self.assertRegex(section, r"\bsetup\b")
            self.assertIn("copilot install", section)
            self.assertRegex(section, r"[Bb]ridge.*[Cc]opilot|[Cc]opilot.*[Bb]ridge")
        self.assertNotRegex(en, r"[Cc]opilot[^\n.]{0,80}out of scope")
        self.assertNotRegex(tr, r"[Cc]opilot[^\n.]{0,80}kapsam d[ıi][şs][ıi]")

    def test_named_files_exist(self):
        for path in (self.EN, self.TR):
            for name in re.findall(r"`((?:[\w.-]+/)*[\w.-]+\.(?:md|sh|html))`", read(path)):
                if "/" in name or name.isupper() or name[0].isupper():
                    hit = (ROOT / name).exists() or (PLUGIN / name).exists() or list(ROOT.glob(f"**/{name}"))
                    self.assertTrue(hit or name.startswith(("<", "~")) or "*" in name, f"{path.name} names missing file {name}")


class TestMarkdownLinks(unittest.TestCase):
    def test_the_checker_finds_broken_links_and_skips_the_rest(self):
        with tempfile.TemporaryDirectory(prefix="orkestra-links-") as d:
            root = Path(d)
            (root / "sub").mkdir()
            (root / "sub" / "there.md").write_text("x", encoding="utf-8")
            doc = root / "a.md"
            doc.write_text("[ok](sub/there.md) [ok2](sub/there.md#part) [web](https://example.com/x.md) [mail](mailto:a@b.c)\n"
                           "[anchor](#top) [gone](sub/missing.md) [also gone](missing.md#frag) ![img](pic.png)\n"
                           "`[code](nope.md)`\n```\n[fenced](nope2.md)\n```\n[tpl](<path>/x.md)\n", encoding="utf-8")
            self.assertEqual(broken_links(doc), ["sub/missing.md", "missing.md#frag"])

    def test_links_in_templates_skills_agents_and_reference_documents_resolve(self):
        files = (sorted(TEMPLATES.rglob("*.md")) + sorted(SKILLS.rglob("*.md")) + sorted(AGENTS.glob("*.md"))
                 + [ROOT / "ORKESTRA.md", ROOT / "ORKESTRA.tr.md"])
        self.assertGreater(len(files), 40)
        for path in files:
            self.assertEqual(broken_links(path), [], f"{path.relative_to(ROOT)} links to a missing file")


if __name__ == "__main__":
    unittest.main()
