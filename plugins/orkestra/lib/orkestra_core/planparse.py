"""Parsers for spec.md / plan.md / verification.md / triage.md.

The formats are defined by templates/<lang>/ (English and Turkish). EVERY reader accepts both spellings of every
heading and task label (`messages/protocol.py`), compared without regard to case, diacritics or punctuation, so a run
started in one language continues in the other and runs written by older versions keep parsing.
Parsers are deliberately tolerant about whitespace and strict about structure.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import hints
from . import i18n
from . import paths
from .messages import protocol as P
from .workkind import nonsoftware_plan_problems

ID_RE = re.compile(r"\b((?:FR|SC)-\d{3})\b")
TASK_HEAD_RE = re.compile(r"^###\s+(T\d{3})\s*(?:[—–:\-]+\s*)?(.*?)\s*$", re.M)

# folded task-field spelling → field key (all languages; see messages/protocol.TASK_FIELD_ALIASES)
FIELD_ALIASES = {key: tuple(P.fold(a) for a in names) for key, names in P.TASK_FIELD_ALIASES.items()}
_NEW_MARKER_RE = re.compile(r"\(\s*(?:" + "|".join(re.escape(P.ascii_fold(m)) for m in P.NEW_FILE_MARKERS) + r")")
_DONE_WORDS = frozenset(P.fold(w) for w in P.TRIAGE_DONE_WORDS)


def parse_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """Tiny YAML subset: `key: value`, `key: [a, b]`, and `key:` followed by `- item` lines."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    data: Dict[str, object] = {}
    current_list_key: Optional[str] = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        m_item = re.match(r"^\s+-\s+(.*)$", raw) or re.match(r"^-\s+(.*)$", raw)
        if m_item and current_list_key:
            lst = data.setdefault(current_list_key, [])
            if isinstance(lst, list):
                lst.append(_scalar(m_item.group(1)))
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        current_list_key = None
        if val == "":
            data[key] = []
            current_list_key = key
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [_scalar(x) for x in _split_list(inner)] if inner else []
        else:
            data[key] = _scalar(val)
    body = "\n".join(lines[end + 1:])
    return data, body


def _split_list(inner: str) -> List[str]:
    out, buf, quote = [], "", None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _scalar(v: str) -> object:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    return v


# ---------------------------------------------------------------- spec

def spec_ids(spec_text: str) -> List[str]:
    seen: List[str] = []
    for m in ID_RE.finditer(spec_text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


_H23_RE = re.compile(r"^#{2,3}[ \t]+(.+)$", re.M)          # titles are compared folded, so trailing blanks need no regex
_H2_RE = re.compile(r"^##[ \t]+(.*)$", re.M)


def _title_starts_with(title: str, name: str, whole_word: bool) -> bool:
    t, n = P.fold(title), P.fold(name)
    if not n or not t.startswith(n):
        return False
    return not whole_word or len(t) == len(n) or t[len(n)] == " "


@functools.lru_cache(maxsize=None)
def _longer_siblings(section_id: str) -> Tuple[str, ...]:
    """Folded headings of OTHER sections that extend a heading of this one (`Kapsam` / `Kapsam ici`): a title that
    starts with one of them belongs to that other section."""
    mine = [P.fold(h) for h in P.SECTIONS[section_id]]
    return tuple(P.fold(h) for sid, pair in P.SECTIONS.items() if sid != section_id for h in pair
                 if any(P.fold(h).startswith(m + " ") for m in mine))


def _is_heading_of(title: str, section_id: str, whole_word: bool) -> bool:
    t = P.fold(title)
    if any(t.startswith(x) for x in _longer_siblings(section_id)):
        return False
    return any(_title_starts_with(title, alias, whole_word) for alias in i18n.heading_aliases(section_id))


def has_section(text: str, *names: str) -> bool:
    """A level-2/3 heading that starts with one of `names` (case, diacritics and punctuation do not matter)."""
    titles = [m.group(1) for m in _H23_RE.finditer(text)]
    return any(_title_starts_with(t, name, True) for name in names for t in titles)


def has_heading(text: str, section_id: str) -> bool:
    """`has_section` for a protocol section id (messages/protocol.SECTIONS): the heading of EITHER language counts."""
    return any(_is_heading_of(m.group(1), section_id, True) for m in _H23_RE.finditer(text))


def heading_body(text: str, section_id: str) -> Optional[str]:
    """`section` for a protocol section id: the body under the heading of either language."""
    return _section_where(text, [lambda title: _is_heading_of(title, section_id, False)])


# ---------------------------------------------------------------- plan tasks

class Task:
    """One plan task. A plain class with dataclass semantics (init, eq, repr): `dataclasses` costs a hook 5 ms."""
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, id: str, title: str, body: str, fields: Optional[Dict[str, str]] = None):
        self.id = id
        self.title = title
        self.body = body
        self.fields = {} if fields is None else fields

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.id, self.title, self.body, self.fields) == (other.id, other.title, other.body, other.fields)

    def __repr__(self) -> str:
        return f"Task(id={self.id!r}, title={self.title!r}, body={self.body!r}, fields={self.fields!r})"

    @property
    def files(self) -> List[str]:
        raw = self.fields.get("files", "")
        found = re.findall(r"`([^`]+)`", raw)
        if found:
            return [f.strip() for f in found if f.strip()]
        return [x.strip() for x in re.split(r"[,;]", raw) if x.strip()]

    @property
    def file_entries(self) -> List[Tuple[str, bool]]:
        """[(path, is_new)] — a file is new when followed by (new)/(create…) or the Turkish (yeni)/(olustur...)."""
        raw = self.fields.get("files", "")
        out: List[Tuple[str, bool]] = []
        for m in re.finditer(r"`([^`]+)`([^`,;]*)", raw):
            path, tail = m.group(1).strip(), P.ascii_fold(m.group(2))
            out.append((path, bool(_NEW_MARKER_RE.search(tail))))
        return out

    @property
    def tdd(self) -> str:
        raw = self.fields.get("tdd", "required").strip().lower()
        return "n/a" if raw.startswith(("n/a", "na", "yok", "gerekmez", "none")) else "required"

    @property
    def depends(self) -> List[str]:
        return re.findall(r"T\d{3}", self.fields.get("depends", ""))


def _field_key(label: str) -> Optional[str]:
    label = P.fold(label.strip().strip("*"))
    for key, aliases in FIELD_ALIASES.items():
        if label in aliases:
            return key
    return None


def parse_tasks(plan_text: str) -> List[Task]:
    tasks: List[Task] = []
    heads = list(TASK_HEAD_RE.finditer(plan_text))
    for idx, m in enumerate(heads):
        start = m.end()
        end = heads[idx + 1].start() if idx + 1 < len(heads) else len(plan_text)
        body = plan_text[start:end]
        # stop at the next level-2 heading
        h2 = re.search(r"^##\s+", body, re.M)
        if h2:
            body = body[:h2.start()]
        fields: Dict[str, str] = {}
        last_key: Optional[str] = None
        for line in body.splitlines():
            fm = re.match(r"^\s*[-*]\s+\**([^:*]+?)\**\s*:\s*(.*)$", line)
            if fm:
                key = _field_key(fm.group(1))
                if key:
                    fields[key] = fm.group(2).strip()
                    last_key = key
                    continue
            if last_key and re.match(r"^\s{2,}\S", line):
                fields[last_key] += " " + line.strip()
        tasks.append(Task(id=m.group(1), title=m.group(2).strip() or m.group(1), body=body.strip(), fields=fields))
    return tasks


def task_section(plan_text: str, task_id: str) -> Optional[str]:
    for t in parse_tasks(plan_text):
        if t.id == task_id:
            return f"### {t.id} — {t.title}\n{t.body}\n"
    return None


def _section_where(text: str, matchers: List[Callable[[str], bool]]) -> Optional[str]:
    heads = list(_H2_RE.finditer(text))
    for match in matchers:                                  # matchers are tried in order, headings in document order
        for m in heads:
            if match(m.group(1)):
                end = text.find("\n", m.end())
                rest = text[end + 1:] if end >= 0 else ""
                nxt = re.search(r"^##\s+", rest, re.M)
                return rest[: nxt.start()] if nxt else rest
    return None


def section(text: str, *names: str) -> Optional[str]:
    """Return the body of the first level-2 section whose title starts with one of names (names are tried in order)."""
    return _section_where(text, [lambda title, n=n: _title_starts_with(title, n, False) for n in names])


# ---------------------------------------------------------------- checks

def _lang_pair(en: str, tr: str) -> str:
    return tr if i18n.lang() == "tr" else en


def _field_label(key: str) -> str:
    """The task-line label the current language's plan template uses (`Files` / `Dosyalar`)."""
    return _lang_pair(*P.TASK_FIELDS[key])


def check_plan(spec_text: str, plan_text: str, project: Optional[Path] = None, *,
               work_kind: Optional[str] = None, run_id: Optional[str] = None,
               repro_first: bool = True) -> List[str]:
    """Structural lint (+ file existence when `project` is given). Returns problems (empty == OK).

    writing and general add deliverable-path and content-check TDD problems. Any other
    work_kind, including a missing one, leaves the software list unchanged. `repro_first=False` skips the
    bugfix/perf repro-first rule (a run already past plan approval must not be blocked by it).
    """
    problems: List[str] = []
    if not spec_text.strip():
        problems.append("spec.md is missing or empty")
    if not plan_text.strip():
        problems.append("plan.md is missing or empty")
        return problems
    fm, _ = parse_frontmatter(plan_text)
    if "approved_dependencies" not in fm:
        problems.append("plan.md frontmatter must declare `approved_dependencies:` (use [] when none)")
    tasks = parse_tasks(plan_text)
    if not tasks:
        problems.append("plan.md has no tasks (expected headings like `### T001 — Title`)")
    ids = [t.id for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate task ids: {', '.join(dupes)}")
    for t in tasks:
        for req in ("files", "acceptance", "test"):
            if not t.fields.get(req, "").strip():
                problems.append(f"{t.id}: missing `- {_field_label(req)}:` line")
        if t.tdd == "n/a" and "(" not in t.fields.get("tdd", "") and len(t.fields.get("tdd", "")) < 8:
            problems.append(f"{t.id}: `TDD: n/a` needs a reason, e.g. `TDD: n/a (docs only)`")
        for dep in t.depends:
            if dep not in ids:
                problems.append(f"{t.id}: depends on unknown task {dep}")
        if project is not None:
            for path, is_new in t.file_entries:
                if any(ch in path for ch in "*?<>{}") or path.startswith(("http", "$")):
                    continue
                if not is_new and not (project / path).exists():
                    problems.append(f"{t.id}: `{path}` does not exist in the project — fix the path, or mark it "
                                    f"`({_lang_pair(*P.NEW_FILE_LABEL)})` if the task creates it")
    # requirement coverage: every FR must be referenced by at least one task
    fr_ids = [i for i in spec_ids(spec_text) if i.startswith("FR-")]
    task_text = "\n".join(t.body for t in tasks)
    for fr in fr_ids:
        if fr not in task_text:
            problems.append(f"{fr} is not covered by any task (reference it in a task's "
                            f"`{_field_label('acceptance')}:` line)")
    sc_ids = [i for i in spec_ids(spec_text) if i.startswith("SC-")]
    for sc in sc_ids:
        if sc not in plan_text:
            problems.append(f"{sc} is not referenced anywhere in plan.md (map it in the test strategy)")
    if work_kind in ("writing", "general"):
        problems.extend(nonsoftware_plan_problems(tasks, run_id if isinstance(run_id, str) else ""))
    elif repro_first and tasks and project is not None and isinstance(run_id, str) and run_id:
        problems.extend(bugfix_plan_problems(tasks, spec_text, request_type(project, run_id)))
    return problems


_TYPE_LINE_RE = re.compile(r"^\s*[-*]?\s*\**\s*(type|tur)\s*\**\s*:\s*\**\s*([a-z]+)", re.I | re.M)
REPRO_FIRST_TYPES = ("bugfix", "perf")


def request_type(project: Path, run_id: str) -> str:
    """The `Type:` (Turkish `Tur:`) token of the request's classification block, lower case; "" when absent."""
    try:
        text = (paths.feature_dir(project, run_id) / "request.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    body = heading_body(text, "classification")
    m = _TYPE_LINE_RE.search(P.ascii_fold(body)) if body else None
    return m.group(2).lower() if m else ""


def bugfix_plan_problems(tasks: List[Task], spec_text: str, kind: str) -> List[str]:
    """A bugfix or perf request needs a repro-first task, and T001 must tie to a requirement (the symptom FR)."""
    if kind not in REPRO_FIRST_TYPES:
        return []
    out: List[str] = []
    if not any(t.tdd == "required" for t in tasks):
        out.append(i18n.t("runs.plan_bugfix_tdd", kind=kind))
    first = next((t for t in tasks if t.id == "T001"), tasks[0])
    fr_ids = [i for i in spec_ids(spec_text) if i.startswith("FR-")]
    if fr_ids and not any(re.search(rf"(?<![\w-]){re.escape(fr)}(?![\w-])", first.body) for fr in fr_ids):
        out.append(i18n.t("runs.plan_bugfix_symptom", task=first.id, kind=kind, fr=fr_ids[0]))
    return out


SIZE_ESCALATION_TASKS = 3
SIZE_ESCALATION_FILES = 8


def size_escalation_reason(tasks: List[Task]) -> str:
    """Why a plan outgrew XS/S ("" when it did not): risky paths, more than 3 tasks, or more than 8 distinct files."""
    files = sorted({f.replace("\\", "/") for t in tasks for f in t.files if f and "/.orkestra/" not in "/" + f})
    risky = [f for f in files if hints.SECURITY_RE.search(f) or hints.DATA_RE.search(f)]
    if risky:
        return i18n.t("runs.size_escalated_why_risky", files=", ".join(risky[:3]))
    if len(tasks) > SIZE_ESCALATION_TASKS:
        return i18n.t("runs.size_escalated_why_tasks", count=len(tasks), limit=SIZE_ESCALATION_TASKS)
    if len(files) > SIZE_ESCALATION_FILES:
        return i18n.t("runs.size_escalated_why_files", count=len(files), limit=SIZE_ESCALATION_FILES)
    return ""


_BROAD_TEST_RE = re.compile(r"^(?:npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|pytest|python3?\s+-m\s+pytest|"
                            r"go\s+test(?:\s+\./\.\.\.)?|cargo\s+test|mvn\s+test|\./mvnw\s+test|npx\s+jest|"
                            r"npx\s+vitest(?:\s+run)?|vitest(?:\s+run)?)$", re.I)


def plan_notes(tasks: List[Task]) -> List[str]:
    """Non-blocking NOTE lines: a Test that is a bare suite command, an Acceptance without Given/When/Then."""
    notes: List[str] = []
    for t in tasks:
        cmd = t.fields.get("test", "").strip().strip("`").strip()
        if cmd and _BROAD_TEST_RE.match(cmd):
            notes.append(i18n.t("watchdog.note_test_broad", task=t.id, test=cmd))
        words = set(P.fold(t.fields.get("acceptance", "")).split())
        if t.fields.get("acceptance", "").strip() and not words & set(P.GWT_WORDS):
            notes.append(i18n.t("watchdog.note_acceptance_vague", task=t.id))
    return notes


def check_trace(spec_text: str, verification_text: str) -> List[str]:
    """Every FR/SC id must appear in verification.md on a row marked PASS."""
    problems: List[str] = []
    ids = spec_ids(spec_text)
    if not ids:
        return ["spec.md defines no FR-/SC- ids"]
    rows: Dict[str, List[str]] = {}
    for line in verification_text.splitlines():
        for m in ID_RE.finditer(line):
            rows.setdefault(m.group(1), []).append(line)
    for i in ids:
        lines = rows.get(i)
        if not lines:
            problems.append(f"{i}: not present in verification.md")
            continue
        if not any(re.search(r"\bPASS\b", ln) for ln in lines):
            problems.append(f"{i}: no PASS row in verification.md")
        if any(re.search(r"\bFAIL\b", ln) for ln in lines):
            problems.append(f"{i}: has a FAIL row in verification.md")
    return problems


def verdict(text: str) -> Optional[str]:
    """The LAST `VERDICT:` line wins (a template placeholder or an earlier round must not count)."""
    found = re.findall(r"^\s*\**VERDICT\**\s*:\s*\**\s*([A-Z_]+)\b", text, re.M)
    return found[-1] if found else None


def open_triage_items(triage_text: str) -> List[str]:
    """Rows `| R-001 | ... | fix | open |` style. A row with verdict fix and status != done is open."""
    open_items: List[str] = []
    for line in triage_text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if not cells or not re.match(r"^r-\d{3}$", cells[0]):
            continue
        if "fix" in cells and not any(P.fold(c) in _DONE_WORDS for c in cells):
            open_items.append(cells[0].upper())
        if any(c in ("pending", "untriaged", "?") for c in cells):
            open_items.append(cells[0].upper() + " (untriaged)")
    return open_items
