"""Per-project long-term memory (git-versionable markdown, progressive disclosure).

  memory/PROJECT.md     stack, structure, commands, conventions (verified by onboarding)
  memory/FEATURES.md    one line per delivered feature  → index loaded every run
  memory/DECISIONS.md   ADR-lite, append-only
  memory/LEARNINGS.md   pitfalls worth not repeating (high bar)
  memory/specs/<capability>.md  living specs (current truth per capability)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from . import i18n
from . import paths
from .util import read_json, read_text, today, write_text_atomic


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", (cell or "").replace("|", "/")).strip()


def feature_add(project: Path, run_id: str, title: str, summary: str, capabilities: List[str]) -> None:
    path = paths.memory_dir(project) / "FEATURES.md"
    text = read_text(path)
    if "| id |" not in text:                        # no table yet (the template normally brings it): current language
        text = i18n.t("runs.features_header") + ("\n" + text if text.strip() and not text.startswith("# Features") else "")
    lines = [ln for ln in text.splitlines() if not ln.startswith(f"| {run_id} |")]
    row = f"| {run_id} | {_clean(title)} | {today()} | {_clean(', '.join(capabilities))} | {_clean(summary)[:220]} |"
    lines.append(row)
    write_text_atomic(path, "\n".join(lines).rstrip() + "\n")


def append_entry(project: Path, name: str, run_id: str, text: str) -> None:
    path = paths.memory_dir(project) / name
    cur = read_text(path) or f"# {name[:-3].title()}\n\n"
    write_text_atomic(path, cur.rstrip() + f"\n\n- {today()} [{run_id}] {text.strip()}\n")


_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_GLUE = frozenset({
    "this", "that", "with", "from", "have", "does", "only", "when", "your", "into", "they", "were",
    "been", "each", "than", "then", "also", "just", "like", "over", "such", "same", "more", "some",
    "what", "which", "will", "would", "about", "after", "before", "other", "their", "there", "these",
    "those", "icin", "olan", "gibi", "sonra", "bunu", "kadar", "ancak", "veya", "diye", "bile", "daha",
})


def words(text: str) -> set:
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _GLUE}


def rank_learnings(lines: List[str], query: str, limit: int = 6) -> tuple:
    """Lessons that share words with the request. No overlap: the newest lines, and matched is False."""
    limit = max(0, int(limit))
    query_words = words(query)
    scored = [(len(query_words & words(ln)) if query_words else 0, i, ln) for i, ln in enumerate(lines)]
    matched = [row for row in scored if row[0] > 0]
    if matched:
        matched.sort(key=lambda row: (-row[0], -row[1]))
        return [row[2] for row in matched[:limit]], True
    return [row[2] for row in scored[-limit:]], False


def digest(project: Path, max_features: int = 8, query: str = "", learnings_limit: int = 6) -> Dict[str, str]:
    mdir = paths.memory_dir(project)
    out: Dict[str, str] = {}
    proj = read_text(mdir / "PROJECT.md")
    # first ~25 meaningful lines of PROJECT.md (the summary block)
    lines = [ln for ln in proj.splitlines() if ln.strip()][:25]
    out["project"] = "\n".join(lines)
    feats = [ln for ln in read_text(mdir / "FEATURES.md").splitlines() if re.match(r"^\| \d{3}-", ln)]
    out["features"] = "\n".join(feats[-max_features:])
    out["features_total"] = str(len(feats))
    learn = [ln for ln in read_text(mdir / "LEARNINGS.md").splitlines() if ln.startswith("- ")]
    picked, matched = rank_learnings(learn, query, learnings_limit)
    out["learnings"] = "\n".join(picked)
    out["learnings_total"] = str(len(learn))
    out["learnings_matched"] = "1" if matched else "0"
    specs = sorted(p.stem for p in (mdir / "specs").glob("*.md")) if (mdir / "specs").is_dir() else []
    out["specs"] = ", ".join(specs)
    return out


def _lesson_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _known_lessons(project: Path) -> set:
    text = read_text(paths.memory_dir(project) / "LEARNINGS.md")
    keys = set()
    for ln in text.splitlines():
        if not ln.startswith("- "):
            continue
        body = re.sub(r"^- \d{4}-\d{2}-\d{2} \[[^\]]+\] ", "", ln)
        keys.add(_lesson_key(body))
    return keys


def _triage_fixes(project: Path, run_id: str) -> List[str]:
    text = read_text(paths.feature_dir(project, run_id) / "reviews" / "triage.md")
    out: List[str] = []
    for ln in text.splitlines():
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("id",) or set(cells[0]) <= set("-: "):
            continue
        if cells[3] != "fix" or cells[4] != "done":
            continue
        finding = cells[2]
        if len(finding) < 12 or "..." in finding or "missing null check" in finding:
            continue
        out.append(f"{cells[0]}: {finding}"[:220])
    return out


def remember_finished_run(project: Path, st: dict) -> int:
    """Index a finished run: one feature row if none exists, plus proved mistakes. Returns new lesson count."""
    rid = str(st.get("run_id") or "")
    if not rid:
        return 0
    feats = read_text(paths.memory_dir(project) / "FEATURES.md")
    if f"| {rid} |" not in feats:
        feature_add(project, rid, str(st.get("title") or rid), str(st.get("title") or rid), [])
    lessons: List[str] = []
    for tid, task in (st.get("tasks") or {}).items():
        for note in task.get("notes") or []:
            text = str(note).strip()
            if text.upper().startswith("BLOCKED") or text.startswith("blocked: review round limit"):
                lessons.append(f"{tid}: {text}"[:220])
    lessons.extend(_triage_fixes(project, rid))
    known = _known_lessons(project)
    added = 0
    for text in lessons:
        if added >= 4:
            break
        key = _lesson_key(text)
        if not key or key in known:
            continue
        append_entry(project, "LEARNINGS.md", rid, text)
        known.add(key)
        added += 1
    return added


_SKIP_DIRS = {".git", "node_modules", ".orkestra", "dist", "build", ".next", "venv", ".venv",
              "__pycache__", "target", ".idea", ".gradle"}
_STACK = ("package.json", "pyproject.toml", "go.mod", "Cargo.toml", "app.json", "pubspec.yaml", "pom.xml",
          "build.gradle", "build.gradle.kts", "Package.swift", "composer.json", "Gemfile", "requirements.txt",
          "setup.py", "Makefile")
_ENTRIES = ("src/main.ts", "src/index.ts", "src/index.js", "src/main.py", "app/main.py", "manage.py",
            "main.go", "app.json", "Package.swift")
_HEADINGS = ("## Summary", "## Structure", "## Testing")


def is_template(text: str) -> bool:
    return "<one line:" in text or "{{project}}" in text or "<languages, frameworks" in text


def _head_lines(path: Path, limit: int = 200) -> List[str]:
    if not path.is_file():
        return []
    out: List[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh):
            if i >= limit:
                break
            out.append(ln.rstrip("\n"))
    return out


def _first_prose(path: Path) -> str:
    for ln in _head_lines(path):
        s = ln.strip()
        if not s or s[0] in "#|>-`" or s.startswith("```"):
            continue
        return re.sub(r"\s+", " ", s)[:160]
    return ""


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"`+", "", text)).strip()


def observe(project: Path) -> bool:
    """Fill PROJECT.md from files that exist. A filled file is left as it is. Returns True when it writes."""
    project = Path(project)
    dest = paths.memory_dir(project) / "PROJECT.md"
    current = read_text(dest)
    if current.strip() and not is_template(current):
        return False
    text = _observed(project)
    write_text_atomic(dest, text)
    return True


def _observed(project: Path) -> str:
    project = Path(project)
    lines = [f"# Project: {project.name}", "",
             "Written from files that exist in this checkout. A line that says unknown was not in those files.",
             ""]
    lines.append("## Summary")
    purpose = ""
    purpose_from = ""
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        prose = _first_prose(project / name)
        if prose:
            purpose, purpose_from = prose, name
            break
    if purpose_from:
        lines.append(f"- Purpose: {_plain(purpose)} (`{purpose_from}`)")
    else:
        lines.append("- Purpose: unknown")
    stack = [name for name in _STACK if (project / name).is_file()]
    if stack:
        lines.append("- Stack: " + ", ".join(f"`{name}`" for name in stack))
    else:
        lines.append("- Stack: unknown")
    entries = [name for name in _ENTRIES if (project / name).is_file()]
    if (project / "package.json").is_file():
        data = read_json(project / "package.json", default={}) or {}
        main = data.get("main")
        if isinstance(main, str) and (project / main).is_file() and main not in entries:
            entries.append(main)
    if entries:
        lines.append("- Entry points: " + ", ".join(f"`{name}`" for name in entries))
    else:
        lines.append("- Entry points: unknown")
    data_bits: List[str] = []
    if (project / "prisma" / "schema.prisma").is_file():
        data_bits.append("`prisma/schema.prisma`")
    env_file = ""
    env_names: List[str] = []
    for name in (".env.example", ".env.sample"):
        if (project / name).is_file():
            env_file = name
            for ln in _head_lines(project / name):
                match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", ln.strip())
                if match and match.group(1) not in env_names:
                    env_names.append(match.group(1))
            break
    if env_names:
        data_bits.append("env names " + ", ".join(env_names[:20]) + f" (`{env_file}`)")
    lines.append("- Data: " + ("; ".join(data_bits) if data_bits else "unknown"))
    deploy = ""
    deploy_from = ""
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        for ln in _head_lines(project / name):
            if re.search(r"deploy", ln, re.I) and ln.strip() and not ln.strip().startswith("#"):
                deploy = re.sub(r"\s+", " ", ln.strip())[:160]
                deploy_from = name
                break
        if deploy_from:
            break
    if deploy_from:
        lines.append(f"- Deploy target: {_plain(deploy)} (`{deploy_from}`)")
    else:
        lines.append("- Deploy target: unknown")
    cfg = read_json(project / ".orkestra" / "config.json", default={}) or {}
    gate_names = [str(g.get("name")) for g in (cfg.get("gates") or []) if isinstance(g, dict) and g.get("name")]
    if gate_names and (project / ".orkestra" / "config.json").is_file():
        lines.append("- Gates: " + ", ".join(gate_names) + " (`.orkestra/config.json`)")
    else:
        lines.append("- Gates: unknown")
    rules: List[str] = []
    for ln in _head_lines(project / "AGENTS.md"):
        s = ln.strip()
        if re.match(r"^\d+\.\s+\S", s) or s.lower().startswith("never "):
            rules.append(re.sub(r"\s+", " ", s)[:140])
        if len(rules) == 3:
            break
    if rules and (project / "AGENTS.md").is_file():
        lines.append("- Invariants: " + " | ".join(_plain(rule) for rule in rules) + " (`AGENTS.md`)")
    else:
        lines.append("- Invariants: unknown")
    lines.append("")
    lines.append("## Structure")
    dirs = [p.name for p in sorted(project.iterdir())
            if p.is_dir() and p.name not in _SKIP_DIRS and not p.name.startswith(".")][:15]
    if dirs:
        lines.extend(f"- `{name}`" for name in dirs)
    else:
        lines.append("- unknown")
    lines.append("")
    lines.append("## Conventions")
    lines.append("- unknown")
    lines.append("")
    lines.append("## Testing")
    test_dirs = [name for name in ("tests", "test", "__tests__") if (project / name).is_dir()]
    if test_dirs:
        lines.append("- " + ", ".join(f"`{name}`" for name in test_dirs))
    else:
        lines.append("- unknown")
    lines.append("")
    lines.append("## Integrations")
    if env_names and env_file:
        lines.append("- env names only: " + ", ".join(env_names[:20]) + f" (`{env_file}`)")
    else:
        lines.append("- unknown")
    lines.append("")
    lines.append("## Concerns")
    lines.append("- unknown")
    lines.append("")
    return "\n".join(lines)


def check(project: Path) -> List[str]:
    """Problems in PROJECT.md. Empty means every fact cites a path that exists, or says unknown."""
    project = Path(project)
    dest = paths.memory_dir(project) / "PROJECT.md"
    text = read_text(dest)
    if not text.strip():
        return ["PROJECT.md is missing"]
    problems: List[str] = []
    if is_template(text):
        problems.append("PROJECT.md is still the onboard template")
    if len(text.splitlines()) > 120:
        problems.append("PROJECT.md is longer than 120 lines")
    if re.search(r"(?m)^[A-Z][A-Z0-9_]{3,}=.+$", text):
        problems.append("PROJECT.md contains an env assignment; record names only")
    for heading in _HEADINGS:
        if heading not in text:
            problems.append(f"missing {heading}")
    for i, ln in enumerate(text.splitlines(), 1):
        if not ln.startswith("- "):
            continue
        if re.search(r"\bunknown\b", ln, re.I):
            continue
        cites = re.findall(r"`([^`]+)`", ln)
        if not cites:
            problems.append(f"line {i} states a fact with no cited path")
            continue
        for cite in cites:
            rel = cite.strip().strip("`").rstrip("/")
            parts = Path(rel).parts
            if not rel or rel.startswith("/") or ".." in parts:
                problems.append(f"line {i} cites a path outside the project")
                continue
            if not (project / rel).exists():
                problems.append(f"line {i} cites {cite} but it is not in the project")
    return problems
