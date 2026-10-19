"""Evidence layer: deterministic checks that turn model claims into verifiable facts.

  * is_test_path / count_tests      — what counts as a test (language-agnostic heuristics)
  * baseline_content                — the pre-run version of any file (backups / git HEAD / absent)
  * integrity_problems              — removed tests, added skip/only markers, added lint/type suppressions
  * verification_problems           — verification.md may only cite files/tests that exist, every
                                      requirement row needs a verifiable reference
  * evidence_block                  — the report's evidence section, generated from machine state only, written in the
                                      current language and accepted in every supported one
"""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import i18n
from . import paths
from . import planparse as PP
from . import snapshot as S
from .util import read_json, read_text, sha256_file

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?|integration_test|androidTest|testing)(/|$)"
    r"|[._-](test|spec|tests)\.[A-Za-z0-9]+$"
    r"|(^|/)test_[^/]+\.py$|_test\.(go|py|dart|exs?|rb)$"
    r"|Tests?\.(cs|swift|kt|java|scala)$|(^|/)[^/]+Tests?/[^/]+\.(cs|swift)$", re.I)

TEST_MARKERS = [
    re.compile(r"^\s*(async\s+)?def\s+test\w*\s*\(", re.M),                 # python
    re.compile(r"(?<![\w.])(it|test)(\.each\([^)]*\))?\s*\(\s*[\"'`]", re.M),  # jest/vitest/mocha
    re.compile(r"^\s*@(Test|ParameterizedTest|RepeatedTest)\b", re.M),         # junit/kotlin
    re.compile(r"^\s*func\s+Test[A-Z_]\w*\s*\(", re.M),                       # go
    re.compile(r"\[(Fact|Theory|Test|TestMethod|TestCase)\b", re.M),           # .NET
    re.compile(r"^\s*#\[(tokio::)?test\]", re.M),                             # rust
    re.compile(r"(?<![\w.])(testWidgets|test)\s*\(\s*['\"]", re.M),           # dart
    re.compile(r"^\s*func\s+test\w*\s*\(\s*\)", re.M),                        # XCTest
    re.compile(r"^\s*(it|specify|scenario)\s+['\"]", re.M),                   # rspec
]

SKIP_MARKERS = re.compile(
    r"\.(skip|only)\s*\(|(?<![\w.])x(it|describe|test)\s*\(|(?<![\w.])f(it|describe)\s*\(|@pytest\.mark\.(skip|xfail)"
    r"|@unittest\.(skip|expectedFailure)|pytest\.skip\s*\(|@Disabled\b|@Ignore\b|\bt\.Skip(Now|f)?\s*\("
    r"|#\[ignore\]|\bskip\s*:\s*true\b|\.todo\s*\(|Assert\.Inconclusive|\[Ignore\b|XCTSkip", re.I)

SUPPRESSIONS = re.compile(
    r"@ts-ignore|@ts-nocheck|@ts-expect-error|eslint-disable|#\s*type:\s*ignore|#\s*noqa|//\s*nolint|@SuppressWarnings"
    r"|#pragma\s+warning\s+disable|swiftlint:disable|//\s*ignore:|#\s*pylint:\s*disable|rubocop:disable"
    r"|@Suppress\(|biome-ignore|deno-lint-ignore|prettier-ignore", re.I)

TEXT_EXT = re.compile(r"\.(py|pyi|js|jsx|mjs|cjs|ts|tsx|vue|svelte|go|rs|dart|kt|kts|java|scala|cs|swift|rb|php|ex|exs|"
                      r"c|cc|cpp|h|hpp|m|mm|lua|sh|bash|zsh|sql|ya?ml|json|toml|gradle|xml)$", re.I)


def is_test_path(rel: str) -> bool:
    return bool(TEST_PATH_RE.search(rel.replace("\\", "/")))


def count_tests(text: str) -> int:
    return sum(len(rx.findall(text)) for rx in TEST_MARKERS)


# ---------------------------------------------------------------- baseline content

def _local_baseline(run_dir: Path, base: Dict[str, Any], edits: Dict[str, Any], rel: str) -> Tuple[bool, Optional[str]]:
    """(known, text) from the baseline backups and the pre-edit copies; known=False means "ask git"."""
    bfile = (base.get("files") or {}).get(rel)
    if bfile is not None:
        if bfile.get("backup"):
            return True, read_text(run_dir / "snapshot" / "baseline-files" / rel) or ""
        if bfile.get("sha256") is None:
            return True, None
    e = edits.get(rel)
    if e is not None:
        if e.get("backup"):
            return True, read_text(run_dir / "snapshot" / "edits" / rel) or ""
        if not e.get("existed"):
            return True, None
    return False, None


def baseline_contents(project: Path, run_dir: Path, rels: Iterable[str]) -> Dict[str, Optional[str]]:
    """Text of each `rel` before the run (None = did not exist / unknown). The files git has to answer for come from
    ONE `git cat-file --batch`, not one process per file; the baseline and the edits manifest are read once."""
    base = S.load_baseline(run_dir)
    edits = read_json(run_dir / "snapshot" / "edits.json", default={}) or {}
    out: Dict[str, Optional[str]] = {}
    ask_git: List[str] = []
    for rel in dict.fromkeys(rels):
        known, text = _local_baseline(run_dir, base, edits, rel)
        if known:
            out[rel] = text
        else:
            out[rel] = None
            ask_git.append(rel)
    if ask_git and base.get("git"):
        for rel, blob in S.git_blobs(project, ask_git).items():
            out[rel] = None if blob is None else blob.decode("utf-8", "replace")
    return out


def baseline_content(project: Path, run_dir: Path, rel: str) -> Optional[str]:
    """Text of `rel` before the run (None = did not exist / unknown)."""
    return baseline_contents(project, run_dir, [rel])[rel]


def _added_lines(before: Optional[str], after: Optional[str]) -> List[str]:
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    out = []
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


# ---------------------------------------------------------------- test-change detection (TDD)

def test_files_state(project: Path, run_dir: Path,
                     changed: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Optional[str]]:
    """{rel: sha256} of test files the run changed so far (plus files whose test count grew). `changed` is a scan the
    caller already made. A file with no test in it cannot have gained one, so only files that DO contain tests ask for
    their baseline, and all of them in one batch."""
    rows: List[Tuple[str, Optional[int]]] = []          # (rel, tests in the file now; None: a test file by its path)
    for item in changed if changed is not None else S.changed_files(project, run_dir):
        rel = item["path"]
        p = project / rel
        if is_test_path(rel):
            rows.append((rel, None))
        elif p.is_file() and TEXT_EXT.search(rel):
            now = count_tests(read_text(p))
            if now > 0:
                rows.append((rel, now))
    bases = baseline_contents(project, run_dir, [rel for rel, now in rows if now is not None])
    state: Dict[str, Optional[str]] = {}
    for rel, now in rows:
        p = project / rel
        if now is None:
            state[rel] = sha256_file(p) if p.is_file() else None
        elif now > count_tests(bases.get(rel) or ""):
            state[rel] = sha256_file(p)   # inline tests (e.g. Rust #[cfg(test)])
    return state


# ---------------------------------------------------------------- integrity

def approved_exceptions(project: Path, run_id: str) -> Set[str]:
    """Paths listed under the Exceptions section (either language) of the APPROVED plan: user-approved only."""
    plan = read_text(paths.run_dir(project, run_id) / "approved" / "plan.md")
    sec = PP.heading_body(plan, "exceptions") or ""
    return {p.strip() for p in re.findall(r"`([^`]+)`", sec)}


def integrity_problems(project: Path, run_id: str, changed: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """`changed` is a scan the caller already made (one scan per command, not one per check)."""
    rdir = paths.run_dir(project, run_id)
    allowed = approved_exceptions(project, run_id)
    problems: List[str] = []
    items = changed if changed is not None else S.changed_files(project, rdir)
    rels = [it["path"] for it in items if it["path"] not in allowed and TEXT_EXT.search(it["path"])]
    befores = baseline_contents(project, rdir, rels)            # every baseline from one git call
    for rel in rels:
        p = project / rel
        before = befores.get(rel)
        after = read_text(p) if p.is_file() else None
        if is_test_path(rel) or count_tests(before or "") > 0:
            n_before, n_after = count_tests(before or ""), count_tests(after or "")
            if after is None and n_before:
                problems.append(f"{rel}: test file deleted ({n_before} tests)")
            elif n_after < n_before:
                problems.append(f"{rel}: test count dropped {n_before} → {n_after}")
        added = _added_lines(before, after)
        for line in added:
            if SKIP_MARKERS.search(line) and (is_test_path(rel) or count_tests(after or "")):
                problems.append(f"{rel}: adds a skip/only/xfail marker: {line.strip()[:100]}")
                break
        for line in added:
            if SUPPRESSIONS.search(line):
                problems.append(f"{rel}: adds a lint/type suppression: {line.strip()[:100]}")
                break
    if problems:
        problems.append("Fix the cause instead. If the user truly wants an exception, list the path under "
                        f"`## {i18n.heading('exceptions')}` in plan.md with the reason and get the plan re-approved.")
    return problems


# ---------------------------------------------------------------- verification references

PATH_TOKEN = re.compile(r"(?<![\w/.-])((?:\.{0,2}/)?[\w.@+-]+(?:/[\w.@+-]+)+\.[A-Za-z0-9]{1,8})(?:::([\w.\[\]-]+))?")
TEST_NAME_TOKEN = re.compile(r"`([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)`")


def _project_test_corpus(project: Path, limit_files: int = 4000, limit_bytes: int = 30_000_000) -> str:
    chunks: List[str] = []
    total = 0
    count = 0
    for root, dirs, files in os.walk(project):
        dirs[:] = sorted(d for d in dirs if d not in S.SKIP_DIRS)     # prune: node_modules, Pods ... are never listed
        for name in sorted(files):
            if count >= limit_files or total >= limit_bytes:
                return "\n".join(chunks)
            p = Path(root) / name
            rel = str(p.relative_to(project))
            if not TEXT_EXT.search(rel):
                continue
            if not is_test_path(rel) and "test" not in name.lower():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            chunks.append(txt)
            total += len(txt)
            count += 1
    return "\n".join(chunks)


def verification_problems(project: Path, run_id: str, spec_text: str, ver_text: str,
                          gate_names: Iterable[str]) -> List[str]:
    """Anti-hallucination checks on verification.md (on top of planparse.check_trace)."""
    rdir = paths.run_dir(project, run_id)
    fdir = paths.feature_dir(project, run_id)
    problems: List[str] = []
    gates = {g.lower() for g in gate_names}
    corpus: Optional[str] = None
    ids = PP.spec_ids(spec_text)
    rows: Dict[str, List[str]] = {}
    for line in ver_text.splitlines():
        for i in re.findall(r"\b((?:FR|SC)-\d{3})\b", line):
            rows.setdefault(i, []).append(line)

    def exists(token: str) -> bool:
        t = token.lstrip("./")
        for base in (project, fdir, rdir, rdir / "evidence"):
            if (base / token).exists() or (base / t).exists():
                return True
        return False

    for line in ver_text.splitlines():
        for m in PATH_TOKEN.finditer(line):
            token, test_name = m.group(1), m.group(2)
            if token.startswith(("http", "www.")) or "://" in line[max(0, m.start() - 8):m.start()]:
                continue
            if not exists(token):
                problems.append(f"verification.md cites a file that does not exist: {token}")
            elif test_name:
                target = next((b / token for b in (project, fdir, rdir) if (b / token).exists()), None)
                last = test_name.split(".")[-1].split("[")[0]
                if target is not None and target.is_file() and last not in read_text(target):
                    problems.append(f"verification.md cites test `{test_name}` which is not in {token}")
    for i in ids:
        lines = rows.get(i) or []
        joined = " ".join(lines)
        ok_ref = False
        for m in PATH_TOKEN.finditer(joined):
            if exists(m.group(1)):
                ok_ref = True
        for name in TEST_NAME_TOKEN.findall(joined):
            if "test" in name.lower():
                if corpus is None:
                    corpus = _project_test_corpus(project)
                if name.split(".")[-1] in corpus:
                    ok_ref = True
                else:
                    problems.append(f"{i}: cites test `{name}` that does not exist in the project's tests")
        if any(g and re.search(rf"\b{re.escape(g)}\b", joined.lower()) for g in gates):
            ok_ref = True
        if re.search(r"\b(manual|manuel|elle)\b", joined, re.I) and not re.search(r"evidence/\S+\.log", joined):
            problems.append(f"{i}: a manual check must cite its recorded log (`orkestra evidence {i} -- <cmd>` → evidence/…log)")
        if lines and not ok_ref:
            problems.append(f"{i}: no verifiable reference (existing test file/test name, gate name or evidence log)")
    return sorted(set(problems), key=problems.index)


# ---------------------------------------------------------------- report evidence block

BEGIN = "<!-- orkestra:evidence:begin (generated by `orkestra report evidence` — do not edit) -->"
END = "<!-- orkestra:evidence:end -->"
SC_MARK = "<!-- orkestra:scorecard -->"       # == scorecard.MARK (kept here so a currency check imports nothing)


def _facts(project: Path, run_id: str, scorecard: bool = True) -> Dict[str, Any]:
    """Everything the evidence block says, as data: the block is a rendering of these facts in one language, so the
    expensive part (diffs, changed files) runs once however many languages are compared. The scorecard (tokens grow
    while the run goes on) is only read when a block is written, never when one is checked for being current."""
    from .state import load_state  # local import to avoid a cycle
    st = load_state(project, run_id)
    rdir = paths.run_dir(project, run_id)
    appr = read_json(rdir / "approval.json")
    base = {r["name"]: r for r in ((read_json(rdir / "gates-baseline.json") or {}).get("results") or [])}
    ver = read_json(rdir / "gates-verify.json") or {}
    gates = []
    for r in ver.get("results") or []:
        b = base.get(r["name"])
        gates.append((r["name"], "PASS" if b and b["ok"] else ("FAIL" if b else "—"), "PASS" if r["ok"] else "FAIL"))
    tasks = []
    for tid in sorted(st.get("tasks") or {}):
        t = st["tasks"][tid]
        red, green = t.get("red") or {}, t.get("green") or {}
        note = "; ".join(str(x) for x in (green.get("deviations") or []) + (red.get("deviations") or [])
                         + ([green["foreign_note"]] if green.get("foreign_note") else []))
        tasks.append((tid, t.get("status"), red.get("exit", "—"), green.get("exit", "—"), t.get("tdd") == "n/a", note))
    tri = read_text(paths.feature_dir(project, run_id) / "reviews" / "triage.md")
    changed = S.changed_files(project, rdir)            # one scan serves the integrity check and the changed list
    return {
        "approval": appr if isinstance(appr, dict) else None,
        "gates": gates,
        "comparison": ver.get("comparison") or {},
        "tasks": tasks,
        "integrity": integrity_problems(project, run_id, changed),
        "exceptions": sorted(approved_exceptions(project, run_id)),
        "changed": changed,
        "triage": {v: len(re.findall(rf"\|\s*{v}\s*\|", tri, re.I)) for v in ("fix", "defer", "reject")},
        "scorecard": _scorecard(project, st) if scorecard else None,
    }


def _scorecard(project: Path, st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from . import scorecard as SC           # late import: only report writing needs it
    try:
        return SC.compute(project, st)
    except Exception:                       # a scorecard problem must never block the report
        return None


def _render(facts: Dict[str, Any], code: str) -> str:
    """The evidence block for `facts` in language `code` (en | tr). The Turkish rendering is byte-identical to what
    versions before the localization wrote."""
    cat = i18n.catalog(code)

    def t(key: str, **kw: Any) -> str:
        return cat[key].format(**kw) if kw else cat[key]

    lines: List[str] = [BEGIN, "### " + t("evidence.title"), ""]
    appr = facts["approval"]
    if appr:
        lines.append(t("evidence.approval", decision=appr.get("decision"), source=appr.get("source"), at=appr.get("at"),
                       hash=str(appr.get("hash"))[:12]))
    else:
        lines.append(t("evidence.approval_none"))
    lines += ["", t("evidence.gate_header"), "|---|---|---|"]
    for name, before, after in facts["gates"]:
        lines.append(f"| {name} | {before} | {after} |")
    comp = facts["comparison"]
    state = t("evidence.regression_none") if comp.get("ok") else t("evidence.regression_found")
    lines.append("\n" + t("evidence.regression", state=state)
                 + (t("evidence.pre_existing", names=", ".join(comp["pre_existing_failures"]))
                    if comp.get("pre_existing_failures") else ""))
    lines += ["", t("evidence.task_header"), "|---|---|---|---|"]
    for tid, status, red, green, tdd_na, note in facts["tasks"]:
        lines.append(f"| {tid} {status} | {red} | {green} | "
                     f"{('TDD n/a' if tdd_na else '')}{(' · ' if note and tdd_na else '')}{note} |")
    integ = facts["integrity"]
    lines.append("\n" + t("evidence.integrity", state=t("evidence.integrity_clean") if not integ
                           else t("evidence.integrity_problem", details="; ".join(integ[:-1]))))
    if facts["exceptions"]:
        lines.append(t("evidence.exceptions", paths=", ".join(facts["exceptions"])))
    changed = facts["changed"]
    lines.append(t("evidence.changed", count=len(changed),
                   files=", ".join(f"{c['path']} ({c['change']})" for c in changed[:40])))
    tri = facts["triage"]
    lines.append(t("evidence.triage", fix=tri["fix"], defer=tri["defer"], reject=tri["reject"]))
    if facts.get("scorecard"):
        from . import scorecard as SC
        lines.extend(SC.lines(facts["scorecard"], t, SC.MARK))
    lines.append(t("evidence.no_commit"))
    lines.append(END)
    return "\n".join(lines)


def evidence_block(project: Path, run_id: str, code: Optional[str] = None) -> str:
    """The report's machine-evidence block, in `code` (default: the current language)."""
    return _render(_facts(project, run_id), code or i18n.lang())


def _norm(block: str) -> str:
    return re.sub(r"\s+", " ", block).strip()


def _strip_scorecard(block: str) -> str:
    """The block without its scorecard lines: they are figures that keep moving (tokens), not claims to keep current."""
    return "\n".join(x for x in block.splitlines() if not x.rstrip().endswith(SC_MARK))


def report_has_current_block(project: Path, run_id: str) -> Tuple[bool, str]:
    """The block in report.md must equal the block of the current facts. It may be written in ANY supported language:
    a run started in Turkish and finished in English (or the other way round) is not outdated, and neither is a
    report written by a version that only knew Turkish."""
    rep = read_text(paths.feature_dir(project, run_id) / "report.md")
    m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), rep, re.S)
    if not m:
        return False, "report.md lacks the machine evidence block — run `orkestra report evidence --write`"
    facts = _facts(project, run_id, scorecard=False)
    if _norm(_strip_scorecard(m.group(0))) not in {_norm(_render(facts, code)) for code in i18n.all_langs()}:
        return False, "report.md evidence block is outdated or was edited — run `orkestra report evidence --write`"
    return True, ""


def write_block(project: Path, run_id: str) -> Path:
    from .util import write_text_atomic
    path = paths.feature_dir(project, run_id) / "report.md"
    rep = read_text(path)
    block = evidence_block(project, run_id)
    pattern = re.escape(BEGIN) + r".*?" + re.escape(END)
    if re.search(pattern, rep, re.S):
        rep = re.sub(pattern, lambda _m: block, rep, flags=re.S)
    else:
        rep = (rep.rstrip() + "\n\n" if rep.strip() else "") + block + "\n"
    write_text_atomic(path, rep)
    return path
