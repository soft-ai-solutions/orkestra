"""Deterministic checklist hints: which on-demand playbooks match a set of changed files.

`orkestra changes` prints one `playbooks:` line so reviewers load exactly the lists that fit the diff instead of
guessing (each list costs ~400–750 tokens; none is loaded for docs/config-only diffs).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

from .util import read_text

CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
SECURITY_RE = re.compile(
    r"(^|/)(auth|session|login|signup|oauth|password|token|webhook|middleware|permission|acl|payment|checkout|billing)"
    r"|(^|/)(api|routes?|actions?|loaders?|handlers?|controllers?)(/|\.)|\.env\.example$"
    r"|(^|/)(package\.json|requirements[\w.-]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json)$", re.I)
DATA_RE = re.compile(r"schema\.prisma$|(^|/)migrations?/|\.sql$|(^|/)(models?|entities|repositor(?:y|ies)|db|database)/|drizzle",
                     re.I)
UI_RE = re.compile(r"(^|/)(components?|screens?|pages?|views?|app)/.*\.(tsx|jsx|vue|svelte)$"
                   r"|(^|/)(stores?|slices?|reducers?|hooks)(/|\.)", re.I)
STACK_EXT = (("stack-python", (".py",)), ("stack-go", (".go",)), ("stack-jvm", (".java", ".kt", ".kts", ".scala")))
ORDER = ("security", "data", "stack-mobile", "stack-web", "stack-python", "stack-go", "stack-jvm", "ui-flow")


def is_mobile(project: Path) -> bool:
    pkg = read_text(Path(project) / "package.json")
    return bool(re.search(r'"(expo|react-native)"\s*:', pkg))


def playbooks_for(files: Iterable[str], project: Path) -> List[str]:
    norm = []
    for f in files:
        if not f:
            continue
        n = str(f).replace("\\", "/")
        if "/.orkestra/" in ("/" + n):
            continue
        norm.append(n)
    files = norm
    hit = set()
    for f in files:
        if SECURITY_RE.search(f):
            hit.add("security")
        if DATA_RE.search(f):
            hit.add("data")
        if UI_RE.search(f):
            hit.add("ui-flow")
    if any(f.lower().endswith(CODE_EXT) for f in files):
        hit.add("stack-mobile" if is_mobile(project) else "stack-web")
    for name, exts in STACK_EXT:
        if any(f.lower().endswith(exts) for f in files):
            hit.add(name)
    return [name for name in ORDER if name in hit]
