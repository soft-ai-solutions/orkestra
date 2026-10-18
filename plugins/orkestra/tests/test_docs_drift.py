"""Docs must not drift from the code or from each other.

What is checked, and against what:
  * every `orkestra <subcommand> [action]` and every `--flag` the docs name exists in the real CLI parser
    (`cliparse.build_parser`), and the installers' flags exist in `install.py --help` / `install.sh`;
  * relative links, images and in-page anchors of the repo-level docs resolve (GitHub's anchor rules);
  * `README.md` <-> `README.tr.md` and `ONBOARDING.md` <-> `ONBOARDING.tr.md` mirror each other: same headings in the same
    order, same tables (rows and columns), same code blocks (commands identical, comments and quoted request text free),
    same links, same set of CLI commands, a language switch link at the top;
  * facts a reader relies on come from the code: the phase list, the guard names, the config defaults, the version;
  * the English and Turkish architecture pages (`docs/architecture.html`, `docs/orkestra-mimari.html`) keep the same
    structure, the root copy of the Turkish page equals the one in `docs/`, and their headline numbers come from the code;
  * the GitHub community files exist and say what they must;
  * the quickstart commands run with `--help` (they exist and parse), in a sandbox.

Stdlib only; reads files, runs `--help` in a temporary HOME / ORKESTRA_HOME, writes nothing outside it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import helpers
from orkestra_core import __version__, cliparse, paths, state

ROOT = helpers.PLUGIN.parent.parent
BIN = helpers.BIN

README_EN = ROOT / "README.md"
README_TR = ROOT / "README.tr.md"
ONBOARDING_EN = ROOT / "ONBOARDING.md"
ONBOARDING_TR = ROOT / "ONBOARDING.tr.md"

# Docs whose `orkestra <subcommand> --flag` mentions are checked against the parser.
COMMAND_DOCS = [README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR, ROOT / "ORKESTRA.md", ROOT / "ORKESTRA.tr.md",
                ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", ROOT / "AGENTS.md", ROOT / "docs" / "README.md"]
# Repo-level Markdown whose relative links and anchors must resolve.
LINK_DOCS = COMMAND_DOCS + [ROOT / "CODE_OF_CONDUCT.md", ROOT / "CHANGELOG.md", ROOT / "THIRD_PARTY_NOTICES.md",
                            ROOT / "integrations" / "openclaw" / "CONTRACT.md",
                            ROOT / "integrations" / "openclaw" / "CONTRACT.tr.md"]
HTML_ROOT_TR = ROOT / "orkestra-mimari.html"
HTML_DOCS_TR = ROOT / "docs" / "orkestra-mimari.html"
HTML_DOCS_EN = ROOT / "docs" / "architecture.html"

ROOT_OPTIONS = {"-h", "--help", "--version", "-p", "--project", "--run", "--lang"}
GLOBAL_WITH_VALUE = {"-p", "--project", "--run", "--lang"}
LANG_ACTIONS = {"show", "en", "tr", "auto"}
TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------- the real CLI parser

class ParserFacts:
    """Subcommand names, their option strings (nested actions included) and the choices of their action argument."""

    def __init__(self) -> None:
        parser = cliparse.build_parser(localize=False)
        self.root_options: Set[str] = {o for a in parser._actions for o in a.option_strings}
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        assert subs, "the CLI parser has no subcommands"
        self.subparsers: Dict[str, argparse.ArgumentParser] = dict(subs[0].choices)
        self.options: Dict[str, Set[str]] = {}
        self.actions: Dict[str, Optional[Set[str]]] = {}
        for name, sp in self.subparsers.items():
            self.options[name] = self._options_of(sp)
            self.actions[name] = self._choices_of(sp)
        self.actions["lang"] = set(LANG_ACTIONS)

    def _options_of(self, sp: argparse.ArgumentParser) -> Set[str]:
        out: Set[str] = set()
        for a in sp._actions:
            out.update(a.option_strings)
            if isinstance(a, argparse._SubParsersAction):
                for child in a.choices.values():
                    out |= self._options_of(child)
        return out

    @staticmethod
    def _choices_of(sp: argparse.ArgumentParser) -> Optional[Set[str]]:
        for a in sp._actions:
            if not a.option_strings and a.choices:
                return {str(c) for c in a.choices}
        return None


FACTS = ParserFacts()


# --------------------------------------------------------------------------------------------- markdown helpers

FENCE = re.compile(r"^\s*```(\S*)\s*$")


def split_markdown(text: str) -> Tuple[List[Tuple[str, List[str]]], List[str]]:
    """(fenced code blocks as (info string, lines), the remaining prose lines)."""
    blocks: List[Tuple[str, List[str]]] = []
    prose: List[str] = []
    current: Optional[Tuple[str, List[str]]] = None
    for line in text.splitlines():
        m = FENCE.match(line)
        if m:
            if current is None:
                current = (m.group(1), [])
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current[1].append(line)
        else:
            prose.append(line)
    assert current is None, "unterminated code fence"
    return blocks, prose


def headings(text: str) -> List[Tuple[int, str]]:
    _, prose = split_markdown(text)
    out = []
    for line in prose:
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            out.append((len(m.group(1)), m.group(2)))
    return out


def github_slug(heading: str) -> str:
    """GitHub's heading anchor: strip markup, lower-case, drop punctuation, spaces to hyphens."""
    h = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    h = h.replace("`", "").replace("*", "").strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def anchors_of(path: Path) -> Set[str]:
    seen: Dict[str, int] = {}
    out: Set[str] = set()
    for _, title in headings(read(path)):
        base = github_slug(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else f"{base}-{n}")
    return out


def inline_spans(prose: Iterable[str]) -> List[str]:
    spans = []
    for line in prose:
        spans += [s.replace("\\|", "|") for s in re.findall(r"`([^`\n]+)`", line)]
    return spans


def tables(text: str) -> List[List[int]]:
    """Each table as the list of column counts of its rows (the separator row included)."""
    _, prose = split_markdown(text)
    out: List[List[int]] = []
    current: List[int] = []
    for line in prose + [""]:
        if line.lstrip().startswith("|"):
            current.append(len(re.split(r"(?<!\\)\|", line.strip().strip("|"))))
        elif current:
            out.append(current)
            current = []
    return out


def link_targets(text: str) -> List[str]:
    """Targets of Markdown links and images outside code blocks and code spans."""
    _, prose = split_markdown(text)
    out: List[str] = []
    for line in prose:
        line = re.sub(r"`[^`\n]*`", "", line)
        out += re.findall(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", line)
    return out


# --------------------------------------------------------------------------------------------- CLI mentions

class Mention(NamedTuple):
    sub: str
    action: Optional[str]
    flags: List[str]
    raw: str


ORKESTRA_CALL = re.compile(r"(?:(?<=^)|(?<=[\s`\"'(]))(?:[\w.$~{}-]+/)*orkestra(?=\s)")


def mentions_in(snippet: str) -> List[Mention]:
    """`orkestra <sub> [action] [--flags]` calls inside one code span or one line of a code block."""
    out: List[Mention] = []
    for m in ORKESTRA_CALL.finditer(snippet):
        rest = snippet[m.end():]
        rest = re.split(r"\s#\s|\s\|\s|;|&&|\|\|", rest, maxsplit=1)[0]            # comment, pipe, next command
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()
        i = 0
        flags: List[str] = []
        while i < len(tokens):
            tok = tokens[i]
            if tok in GLOBAL_WITH_VALUE:
                flags.append(tok)
                i += 2
            elif tok.startswith(("--lang=", "--project=", "--run=")):
                flags.append(tok.split("=", 1)[0])
                i += 1
            elif tok in ("--version", "-h", "--help"):
                flags.append(tok)
                i += 1
            else:
                break
        if i >= len(tokens) or not re.fullmatch(r"[a-z][a-z-]*", tokens[i]):
            continue
        sub = tokens[i]
        rest_tokens = tokens[i + 1:]
        action = rest_tokens[0] if rest_tokens and re.fullmatch(r"[a-z][a-z-]*", rest_tokens[0]) else None
        for tok in rest_tokens:
            if tok.startswith("--") and re.fullmatch(r"--[A-Za-z][\w-]*(=.*)?", tok):
                flags.append(tok.split("=", 1)[0])
            elif re.fullmatch(r"-[A-Za-z]", tok):
                flags.append(tok)
        out.append(Mention(sub, action, flags, snippet.strip()))
    return out


def all_mentions(path: Path) -> List[Mention]:
    blocks, prose = split_markdown(read(path))
    out: List[Mention] = []
    for _, lines in blocks:
        for line in lines:
            out += mentions_in(line)
    for span in inline_spans(prose):
        out += mentions_in(span)
    return out


def install_flags() -> Set[str]:
    cp = subprocess.run([sys.executable, str(ROOT / "install.py"), "--help"], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=dict(os.environ, ORKESTRA_LANG="en"))
    flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*|-[a-z])(?![\w-])", cp.stdout))
    sh = read(ROOT / "install.sh")
    flags |= set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)\)", sh))               # the case labels of install.sh
    return flags


# --------------------------------------------------------------------------------------------- tests

class TestCommandsExist(unittest.TestCase):
    def test_every_documented_subcommand_action_and_flag_exists(self):
        problems: List[str] = []
        seen = 0
        for doc in COMMAND_DOCS:
            for mn in all_mentions(doc):
                seen += 1
                rel = doc.relative_to(ROOT)
                if mn.sub not in FACTS.subparsers:
                    problems.append(f"{rel}: `orkestra {mn.sub}` is not a subcommand ({mn.raw[:70]})")
                    continue
                choices = FACTS.actions.get(mn.sub)
                if mn.action and choices is not None and mn.action not in choices:
                    problems.append(f"{rel}: `orkestra {mn.sub} {mn.action}`: action not in {sorted(choices)}")
                allowed = FACTS.options[mn.sub] | FACTS.root_options
                for flag in mn.flags:
                    if flag not in allowed:
                        problems.append(f"{rel}: `orkestra {mn.sub}` has no {flag} ({mn.raw[:70]})")
        self.assertGreater(seen, 40, "the mention scanner found almost nothing: it is broken")
        self.assertEqual(problems, [])

    def test_the_scanner_itself_finds_and_rejects(self):
        found = mentions_in("python3 plugins/orkestra/bin/orkestra cursor install --dry-run   # note")
        self.assertEqual([(m.sub, m.action, m.flags) for m in found], [("cursor", "install", ["--dry-run"])])
        found = mentions_in('ORKESTRA_LANG=en orkestra --lang tr watch --once')
        self.assertEqual([(m.sub, m.flags) for m in found], [("watch", ["--lang", "--once"])])
        self.assertEqual(mentions_in("~/.orkestra/config.json and .orkestra/runs and orkestra_core"), [])
        self.assertEqual(mentions_in("`orkestra` status/resume tools"), [])
        bogus = mentions_in("orkestra frobnicate --nope")[0]
        self.assertNotIn(bogus.sub, FACTS.subparsers)
        self.assertNotIn("--nope", FACTS.options["watch"] | FACTS.root_options)

    def test_installer_flags_in_the_docs_exist(self):
        known = install_flags()
        for must in ("--yes", "--skip-tests", "--dry-run", "--uninstall", "--purge", "--no-mcp"):
            self.assertIn(must, known, f"{must} vanished from the installers: the docs still name it")
        problems = []
        for doc in (README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR):
            blocks, prose = split_markdown(read(doc))
            lines = [ln for _, b in blocks for ln in b] + inline_spans(prose)
            for line in lines:
                if not re.search(r"install\.(sh|py|ps1)", line):
                    continue
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", line.split("#")[0]):
                    if flag not in known:
                        problems.append(f"{doc.name}: {flag} in `{line.strip()[:70]}`")
        self.assertEqual(problems, [])

    def test_bridge_commands_in_the_docs_exist(self):
        cp = subprocess.run([sys.executable, str(ROOT / "bridge" / "orkestra_bridge.py"), "--help"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=dict(os.environ, ORKESTRA_LANG="en"))
        known = set(re.findall(r"orkestra_bridge\.py\s+([a-z][a-z-]*)", cp.stdout))
        self.assertTrue({"serve", "telegram-setup", "doctor"} <= known, known)
        problems = []
        for doc in (README_EN, README_TR):
            blocks, prose = split_markdown(read(doc))
            for line in [ln for _, b in blocks for ln in b] + inline_spans(prose):
                for sub in re.findall(r"orkestra_bridge\.py\s+([a-z][a-z-]*)", line):
                    if sub not in known:
                        problems.append(f"{doc.name}: orkestra_bridge.py {sub}")
        self.assertEqual(problems, [])


class TestBridgeApiInTheDocs(unittest.TestCase):
    """HTTP routes the READMEs name (`POST /jobs`, `GET /runs`, the n8n webhook) exist in the bridge."""

    def test_documented_routes_exist(self):
        src = read(ROOT / "bridge" / "orkestra_bridge.py")
        api = src.split("HTTP API", 1)[1].split("Local UI", 1)[0]
        real = {(m, "/" + p.split("/")[1]) for m, p in re.findall(r"\b(GET|POST)\s+(/[\w/<>\[\]?=-]+)", api)}
        self.assertIn(("POST", "/jobs"), real)
        webhook = " ".join(read(p) for p in (ROOT / "bridge" / "n8n").glob("*.json"))
        # every document that names a route, not only the READMEs: ORKESTRA.md once sent readers to a `POST /runs` that
        # does not exist (jobs are started with `POST /jobs`; `GET /runs` is the read-only live view)
        for doc in (README_EN, README_TR, ROOT / "ORKESTRA.md", ROOT / "ORKESTRA.tr.md",
                    ROOT / "integrations" / "openclaw" / "CONTRACT.md", ROOT / "integrations" / "openclaw" / "CONTRACT.tr.md"):
            blocks, prose = split_markdown(read(doc))
            text = "\n".join([ln for _, b in blocks for ln in b] + inline_spans(prose))
            for method, route in re.findall(r"\b(GET|POST)\s+(/[\w/-]+)", text):
                if route.startswith("/webhook/"):
                    self.assertIn('"path": "%s"' % route.split("/")[2], webhook, f"{doc.name}: {method} {route}")
                    continue
                self.assertIn((method, "/" + route.split("/")[1]), real, f"{doc.name}: {method} {route} is not a bridge route")

    def test_chat_commands_in_the_docs_exist_in_both_languages(self):
        src = read(ROOT / "bridge" / "orkestra_bridge.py")
        for name in ("run", "yap", "status", "durum", "cancel", "iptal", "help", "yardim"):
            self.assertRegex(src, r"\b" + name + r"\b", name)
        for doc in (README_EN, README_TR):
            text = read(doc)
            for name in ("/run", "/status", "/cancel", "/help", "/yap", "/durum", "/iptal", "/yardim"):
                self.assertIn(name, text, f"{doc.name} does not mention {name}")


class TestLinksResolve(unittest.TestCase):
    def test_relative_links_images_and_anchors_resolve(self):
        problems: List[str] = []
        for doc in LINK_DOCS:
            self.assertTrue(doc.is_file(), f"{doc.relative_to(ROOT)} is listed but missing")
            for target in link_targets(read(doc)):
                if re.match(r"^(https?:|mailto:)", target):
                    continue
                file_part, _, frag = target.partition("#")
                dest = doc if not file_part else (doc.parent / file_part).resolve()
                if not dest.exists():
                    problems.append(f"{doc.relative_to(ROOT)}: {target} -> missing file")
                    continue
                if frag and dest.suffix == ".md" and frag not in anchors_of(dest):
                    problems.append(f"{doc.relative_to(ROOT)}: {target} -> no such heading in {dest.name}")
        self.assertEqual(problems, [])

    def test_github_slug_rules(self):
        self.assertEqual(github_slug("Bridge: Telegram, n8n and OpenClaw"), "bridge-telegram-n8n-and-openclaw")
        self.assertEqual(github_slug("Run'ı izleme `x`"), "runı-izleme-x")
        self.assertEqual(github_slug("FAQ and troubleshooting"), "faq-and-troubleshooting")

    def test_html_pages_link_to_each_other_and_in_page_anchors_resolve(self):
        for page in (HTML_ROOT_TR, HTML_DOCS_TR, HTML_DOCS_EN):
            html = read(page)
            ids = set(re.findall(r'\sid="([^"]+)"', html))
            for frag in re.findall(r'href="#([^"]+)"', html):
                self.assertIn(frag, ids, f"{page.relative_to(ROOT)}: #{frag} has no target")
            for href in re.findall(r'href="([^"#:]+\.html)"', html):
                self.assertTrue((page.parent / href).is_file(), f"{page.relative_to(ROOT)}: {href} is missing")
        self.assertIn('href="architecture.html" hreflang="en"', read(HTML_DOCS_TR))
        self.assertIn('href="docs/architecture.html" hreflang="en"', read(HTML_ROOT_TR))
        self.assertIn('href="orkestra-mimari.html" hreflang="tr"', read(HTML_DOCS_EN))


# --------------------------------------------------------------------------------------------- mirrors

def normalized_block(info: str, lines: List[str]) -> Tuple[str, List[str]]:
    """A code block reduced to its commands: comments, aligned annotations and quoted request text do not count."""
    out: List[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"\s+#\s.*$", "", line)                       # trailing comment
        line = re.split(r"\s{2,}", line, maxsplit=1)[0]                      # aligned annotation
        if line.startswith("/orkestra"):
            line = line.split()[0]                                  # a slash command carries free request text
        line = re.sub(r'"[^"]*"', '"..."', line)
        line = re.sub(r"<[^>\s]+>", "<x>", line)                 # <project> / <proje>
        out.append(line)
    return info, out


def mirror_key(target: str) -> str:
    """A link target seen from the other language: the Turkish twin of a document maps to its English name."""
    file_part = target.partition("#")[0]
    file_part = file_part.replace(".tr.md", ".md").replace("orkestra-mimari.html", "architecture.html")
    return file_part


class MirrorBase:
    en: Path
    tr: Path

    def test_a_language_switch_link_is_at_the_top_of_both(self):
        self.assertIn(f"]({self.tr.name})", "\n".join(read(self.en).splitlines()[:6]))
        self.assertIn(f"]({self.en.name})", "\n".join(read(self.tr).splitlines()[:6]))

    def test_same_headings_in_the_same_order(self):
        en = [lvl for lvl, _ in headings(read(self.en))]
        tr = [lvl for lvl, _ in headings(read(self.tr))]
        self.assertEqual(en, tr, "heading levels differ: the two documents no longer mirror each other")
        self.assertGreater(len(en), 8)

    def test_same_tables(self):
        self.assertEqual(tables(read(self.en)), tables(read(self.tr)))

    def test_same_code_blocks_and_commands(self):
        en = [normalized_block(i, b) for i, b in split_markdown(read(self.en))[0]]
        tr = [normalized_block(i, b) for i, b in split_markdown(read(self.tr))[0]]
        self.assertEqual(len(en), len(tr), "different number of code blocks")
        for n, (a, b) in enumerate(zip(en, tr), 1):
            self.assertEqual(a, b, f"code block {n} differs between {self.en.name} and {self.tr.name}")

    def test_same_links(self):
        en = sorted(mirror_key(t) for t in link_targets(read(self.en)) if not t.startswith(("http", self.tr.name, self.en.name)))
        tr = sorted(mirror_key(t) for t in link_targets(read(self.tr)) if not t.startswith(("http", self.tr.name, self.en.name)))
        self.assertEqual(sorted(set(en)), sorted(set(tr)))

    def test_same_cli_commands_are_mentioned(self):
        en = {(m.sub, m.action) for m in all_mentions(self.en)}
        tr = {(m.sub, m.action) for m in all_mentions(self.tr)}
        self.assertEqual(sorted(en - tr, key=str), [], f"only in {self.en.name}")
        self.assertEqual(sorted(tr - en, key=str), [], f"only in {self.tr.name}")

    def test_the_english_document_is_english(self):
        prose = "\n".join(split_markdown(read(self.en))[1])
        prose = re.sub(r"`[^`\n]*`", "", prose)
        prose = re.sub(r"\[[^\]]*\]\([^)]*\)", lambda m: "" if "Türk" in m.group(0) else m.group(0), prose)
        self.assertEqual(TR_LETTERS.findall(prose), [], "Turkish letters in English prose (outside code spans)")

    def test_the_turkish_document_is_really_turkish(self):
        self.assertGreater(len(TR_LETTERS.findall(read(self.tr))), 60)


class TestReadmeMirror(MirrorBase, unittest.TestCase):
    en, tr = README_EN, README_TR

    def test_sixteen_sections_and_the_required_limits_heading(self):
        # sixteen since "Reliability and speed" / "Güvenilirlik ve hız" was added after "Watching runs"
        for doc in (README_EN, README_TR):
            self.assertEqual(sum(1 for lvl, _ in headings(read(doc)) if lvl == 2), 16, doc.name)
        self.assertIn("## Limits you should know", read(README_EN))
        for doc, title, before in ((README_EN, "Reliability and speed", "Project memory"),
                                   (README_TR, "Güvenilirlik ve hız", "Proje hafızası")):
            titles = [t for lvl, t in headings(read(doc)) if lvl == 2]
            self.assertIn(title, titles, doc.name)
            self.assertEqual(titles.index(title) + 1, titles.index(before), f"{doc.name}: {title} sits before {before}")


class TestOnboardingMirror(MirrorBase, unittest.TestCase):
    en, tr = ONBOARDING_EN, ONBOARDING_TR


# --------------------------------------------------------------------------------------------- facts from the code

class TestFactsComeFromTheCode(unittest.TestCase):
    def test_version_is_one_number_everywhere(self):
        manifest = json.loads(read(ROOT / ".claude-plugin" / "marketplace.json"))
        plugin = json.loads(read(helpers.PLUGIN / ".claude-plugin" / "plugin.json"))
        self.assertEqual(manifest["metadata"]["version"], __version__)
        self.assertEqual([p["version"] for p in manifest["plugins"]], [__version__])
        self.assertEqual(plugin["version"], __version__)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        banner = re.compile(r"(?:Latest release|Son sürüm|\(version|\(sürüm|Sürüm|Version)\s+(\d+\.\d+\.\d+)")
        for doc in (README_EN, README_TR, ROOT / "ORKESTRA.md", ROOT / "ORKESTRA.tr.md"):
            found = banner.findall(read(doc)[:2500])
            self.assertTrue(found, f"{doc.name} states no version in its header")
            self.assertEqual(set(found), {__version__}, f"{doc.name} names a version other than {__version__}")
        for page in (HTML_ROOT_TR, HTML_DOCS_TR, HTML_DOCS_EN):
            meta = re.search(r'<p class="meta">(.*?)</p>', read(page), re.S)
            self.assertIsNotNone(meta, page.name)
            self.assertEqual(set(banner.findall(meta.group(1))), {__version__}, page.name)
        changelog = read(ROOT / "CHANGELOG.md")
        self.assertIn("## Unreleased", changelog)
        self.assertRegex(changelog, r"(?m)^## " + re.escape(__version__) + r"\b", "CHANGELOG has no entry for this version")

    def test_the_phase_line_in_both_readmes_is_the_state_machine(self):
        want = " → ".join(state.PHASES)
        for doc in (README_EN, README_TR):
            lines = [ln.strip() for _, b in split_markdown(read(doc))[0] for ln in b]
            self.assertIn(want, lines, f"{doc.name}: the phase line differs from state.PHASES")

    def test_guard_table_names_real_guards_and_all_switchable_ones(self):
        cfg_guards = set(paths.DEFAULT_GLOBAL_CONFIG["guards"]) - {"test_syntax"}
        in_code = set(re.findall(r'deny\("([a-z_]+)"', read(helpers.PLUGIN / "lib" / "orkestra_core" / "guards.py")))
        # refusals the hook itself issues (deadline: `busy`, autopilot question: `approval`) may be named, none is required
        from_hook = set(re.findall(r'deny\("([a-z_]+)"', read(helpers.PLUGIN / "lib" / "orkestra_core" / "hookmain.py")))
        named_by_hook = in_code | from_hook | {"Stop"}
        self.assertEqual(len(cfg_guards), 9)
        for doc in (README_EN, README_TR):
            names: Set[str] = set()
            for line in read(doc).splitlines():
                m = re.match(r"^\|\s*((?:`[A-Za-z_]+`(?:,\s*)?)+)\s*\|\s*(?:every session|her oturum|run)\s*\|", line)
                if m:
                    names |= set(re.findall(r"`([A-Za-z_]+)`", m.group(1)))
            self.assertGreaterEqual(len(names), 12, f"{doc.name}: guard table not found")
            self.assertEqual(sorted(names - named_by_hook), [], f"{doc.name} names a guard the code does not have")
            self.assertEqual(sorted(cfg_guards - names), [], f"{doc.name} omits a switchable guard")
            self.assertEqual(sorted(in_code - names), [], f"{doc.name} omits a guard the code has (a user cannot learn why "
                                                          f"a command was denied from a table that does not name it)")

    def test_configuration_table_matches_the_defaults(self):
        cfg = paths.DEFAULT_GLOBAL_CONFIG
        expect = {"workspace_root": cfg["workspace_root"], "lang": cfg["lang"], "guards.<": True,
                  "engine.default": cfg["engine"]["default"], "watchdog.auto_resume": cfg["watchdog"]["auto_resume"],
                  "tempo.task_review_required": cfg["tempo"]["task_review_required"]}
        for doc in (README_EN, README_TR):
            rows = {}
            for line in read(doc).splitlines():
                m = re.match(r"^\|\s*`([^`|]+)`\s*\|\s*`([^`|]+)`\s*\|", line)
                if m:
                    rows[m.group(1)] = m.group(2)
            for key, want in expect.items():
                hit = [k for k in rows if k.startswith(key)]
                self.assertTrue(hit, f"{doc.name}: configuration row {key} missing")
                self.assertEqual(rows[hit[0]], str(want).lower() if isinstance(want, bool) else str(want), f"{doc.name}: {key}")
        # the dotted keys really exist
        self.assertIn("auto_resume", cfg["watchdog"])
        self.assertIn("task_review_required", cfg["tempo"])
        self.assertIn("global_guards", cfg["engine"]["cursor"])

    def test_engine_names_in_the_docs_are_the_cli_choices(self):
        run = FACTS.subparsers["run"]
        choices: Set[str] = set()
        for a in run._actions:
            if "--engine" in a.option_strings:
                choices = set(a.choices)
        self.assertEqual(choices, {"claude", "cursor", "copilot", "pi"})
        for doc in (README_EN, README_TR):
            for engine in ("cursor", "copilot"):
                self.assertIn(f"orkestra run --engine {engine}", read(doc))

    def test_language_rules_in_the_readmes_match_i18n(self):
        from orkestra_core import i18n
        self.assertEqual(i18n.SUPPORTED, ("en", "tr"))
        self.assertEqual(i18n.DEFAULT, "en")
        for doc in (README_EN, README_TR):
            text = read(doc)
            for needle in ("ORKESTRA_LANG", "orkestra lang show", "--lang", "LC_ALL"):
                self.assertIn(needle, text, doc.name)
        self.assertEqual(set(cliparse.LANG_CHOICES), {"en", "tr", "auto"})


class TestArchitecturePages(unittest.TestCase):
    @staticmethod
    def counts(html: str) -> Dict[str, int]:
        return {tag: len(re.findall(pattern, html)) for tag, pattern in {
            "section": r"<section\b", "h2": r"<h2\b", "h3": r"<h3\b", "table": r"<table\b", "tr": r"<tr\b",
            "li": r"<li\b", "svg": r"<svg\b", "svg text": r"<text\b", "svg path": r"<path\b", "svg rect": r"<rect\b",
            "chip": r'class="chip', "stat": r'class="stat"', "phase": r'class="ph\b', "pre": r"<pre\b",
            "note": r'class="note', "card": r'class="card', "details": r"<details\b"}.items()}

    def test_english_and_turkish_pages_have_the_same_structure(self):
        en, tr = self.counts(read(HTML_DOCS_EN)), self.counts(read(HTML_DOCS_TR))
        self.assertEqual(en, tr)
        self.assertGreater(en["tr"], 40)

    def test_each_section_has_the_same_rows_and_bullets(self):
        def per_section(path: Path) -> List[Tuple[int, int, int]]:
            parts = re.split(r"<section\b", read(path))[1:]
            return [(p.count("<tr"), p.count("<li"), p.count('class="chip')) for p in parts]
        self.assertEqual(per_section(HTML_DOCS_EN), per_section(HTML_DOCS_TR))

    def test_the_two_turkish_copies_differ_only_in_the_link_to_the_english_page(self):
        root = read(HTML_ROOT_TR).replace("docs/architecture.html", "architecture.html")
        self.assertEqual(root, read(HTML_DOCS_TR))

    def test_language_attributes_and_titles(self):
        self.assertIn('<html lang="en">', read(HTML_DOCS_EN))
        self.assertIn('<html lang="tr">', read(HTML_DOCS_TR))
        self.assertIn("<title>Orkestra: architecture and flow</title>", read(HTML_DOCS_EN))

    def test_headline_numbers_come_from_the_code(self):
        agents = sorted(p.stem for p in (helpers.PLUGIN / "agents").glob("*.md"))
        hooks = json.loads(read(helpers.PLUGIN / "hooks" / "hooks.json"))["hooks"]
        engines = {"claude", "cursor", "copilot"}
        for page in (HTML_DOCS_EN, HTML_DOCS_TR, HTML_ROOT_TR):
            html = read(page)
            stats = [int(n) for n in re.findall(r'<div class="stat"><b>(\d+)</b>', html)]
            self.assertEqual(stats, [len(state.PHASES), len(agents), len(hooks), len(engines)], page.name)
            rail = re.findall(r'<div class="nm">([a-z]+)</div>', html)
            self.assertEqual(rail[:len(state.PHASES)], state.PHASES, f"{page.name}: the phase rail differs from state.PHASES")
            team = re.findall(r"<tr><td><b>([a-z-]+)</b></td><td>(?:haiku|sonnet|opus)</td>", html)
            self.assertEqual(sorted(team), agents, f"{page.name}: the agent table differs from agents/")

    def test_no_em_dash_in_the_new_english_page_and_no_stale_test_counts(self):
        self.assertNotIn("—", read(HTML_DOCS_EN))
        for page in (HTML_DOCS_EN, HTML_DOCS_TR, HTML_ROOT_TR):
            html = read(page)
            self.assertNotRegex(html, r"\b3[0-9]{2} \+ 16\b", f"{page.name}: a hard-coded test count that drifts")
            self.assertNotRegex(html, r"\b344 (test|tests)\b", page.name)


def squash(text: str) -> str:
    """Whitespace collapsed, so a phrase wrapped over two lines still matches."""
    return re.sub(r"\s+", " ", text)


def section_of(path: Path, title: str) -> str:
    """The body of the `## title` section of a Markdown file (up to the next `## `)."""
    text = read(path)
    start = text.index(f"\n## {title}\n") + 1
    end = text.find("\n## ", start + 4)
    return text[start:end if end != -1 else len(text)]


def source_number(path: Path, name: str) -> float:
    """The number a module-level `NAME = <number>` in a source file is set to (for modules the test must not import)."""
    m = re.search(r"(?m)^" + re.escape(name) + r"\s*=\s*([0-9.]+)", read(path))
    assert m, f"{name} not found in {path.name}"
    return float(m.group(1))


class TestReliabilityDocs(unittest.TestCase):
    """The reliability and speed work (control plane, caps, locks, `wait`, the fast watcher, gate verify) is documented
    in both languages, the commands exist in the parser and the numbers the docs quote are the numbers in the code."""

    LIB = helpers.PLUGIN / "lib" / "orkestra_core"
    ORK = (ROOT / "ORKESTRA.md", ROOT / "ORKESTRA.tr.md")
    NEW_KEYS = ("tempo.max_dispatches", "watchdog.agent_silent_seconds", "watchdog.autostart", "watchdog.daemon_idle_minutes",
                "gates_parallel", "gates_rerun_failed_baseline", "headless.max_budget_usd", "bridge.turn_timeout_seconds",
                "bridge.turn_idle_seconds")

    @staticmethod
    def default(key: str):
        node = paths.DEFAULT_GLOBAL_CONFIG
        for part in key.split("."):
            node = node[part]
        return node

    def test_the_new_commands_and_flags_exist_in_the_parser(self):
        for sub in ("wait", "brief", "watchdog", "gate"):
            self.assertIn(sub, FACTS.subparsers)
        self.assertLessEqual({"--loop", "--daemon", "--status"}, FACTS.options["watchdog"])
        self.assertLessEqual({"--resume", "--all"}, FACTS.options["gate"])
        self.assertIn("verify", FACTS.actions["gate"])
        self.assertIn("--review", FACTS.options["brief"])
        self.assertIn("--max", FACTS.options["wait"])

    def test_both_readmes_show_and_explain_them(self):
        for doc in (README_EN, README_TR):
            subs = {(m.sub, m.action) for m in all_mentions(doc)}
            for pair in (("wait", None), ("brief", None), ("gate", "verify"), ("watchdog", None)):
                self.assertIn(pair, subs, f"{doc.name} never shows `orkestra {' '.join(x for x in pair if x)}`")
            flags = {f for m in all_mentions(doc) for f in m.flags}
            self.assertLessEqual({"--status", "--daemon", "--resume", "--review", "--max"}, flags, doc.name)
            text = read(doc)
            for needle in ("ORKESTRA_LOCK_TIMEOUT", "ORKESTRA_NO_DAEMON", "tempo.max_dispatches", "watchdog.autostart",
                           "gates_parallel", "watchdog.log", "unblock:"):
                self.assertIn(needle, text, f"{doc.name} does not mention {needle}")

    def test_the_reference_documents_describe_them_in_both_languages(self):
        events = list(json.loads(read(helpers.PLUGIN / "hooks" / "hooks.json"))["hooks"])
        for doc in self.ORK:
            text = squash(read(doc))
            for needle in ("watchdog --loop", "wait [--max", "gate verify [--resume", "brief [--review]", "fastwatch.py",
                           "ORKESTRA_LOCK_TIMEOUT", "ORKESTRA_NO_DAEMON", "ORKESTRA_GATES_FULL", "unblock:", "`busy`",
                           "`loop`", "watchdog.log", "hookmain.py"):
                self.assertIn(needle, text, f"{doc.name} does not mention {needle}")
            for event in events:
                self.assertIn(event, text, f"{doc.name} does not list the hook event {event}")
            for key in self.NEW_KEYS:
                self.assertIn(f"`{key}`", text, f"{doc.name} does not document {key}")
            self.assertNotIn("8 saniyede" if doc.name.endswith("tr.md") else "within 8 seconds", text,
                             f"{doc.name} still states the old 8 second hook limit")

    def test_every_documented_default_is_the_default_in_the_code(self):
        seen = 0
        for doc in (README_EN, README_TR) + self.ORK:
            for line in read(doc).splitlines():
                m = re.match(r"^\|\s*`([a-z_]+(?:\.[a-z_]+)*)`\s*\|\s*`([^`|]+)`\s*\|", line)
                if not m:
                    continue
                try:
                    want = self.default(m.group(1))
                except (KeyError, TypeError):
                    continue
                if isinstance(want, (dict, list)):
                    continue
                seen += 1
                self.assertEqual(m.group(2), str(want).lower() if isinstance(want, bool) else str(want),
                                 f"{doc.name}: {m.group(1)}")
        self.assertGreater(seen, 20, "the default-value scanner found almost nothing: it is broken")
        for key in self.NEW_KEYS:
            self.default(key)                               # every key the docs introduce exists in the defaults

    def test_the_numbers_quoted_in_the_docs_are_the_numbers_in_the_code(self):
        from orkestra_core import gates, runs, tempo, util
        hm = self.LIB / "hookmain.py"
        wall, cpu = int(source_number(hm, "GUARD_DEADLINE_SECONDS")), int(source_number(hm, "HOOK_CPU_SECONDS"))
        lock_hook, lock_cli = int(util.HOOK_LOCK_TIMEOUT), int(util.CLI_LOCK_TIMEOUT)
        skip, gate_timeout = int(runs.BASELINE_SKIP_SECONDS), int(gates.DEFAULT_TIMEOUT)
        self.assertEqual((lock_hook, lock_cli, tempo.WAIT_MAX, tempo.MAX_REVIEW_ROUNDS, tempo.MAX_DISPATCHES, wall, cpu),
                         (2, 10, 540, 3, 3, 9, 6), "a limit changed: update the docs and this test together")
        en = {README_EN: squash(section_of(README_EN, "Reliability and speed")),
              ROOT / "ORKESTRA.md": squash(section_of(ROOT / "ORKESTRA.md", "Reliability: caps, locks and gates"))}
        tr = {README_TR: squash(section_of(README_TR, "Güvenilirlik ve hız")),
              ROOT / "ORKESTRA.tr.md": squash(section_of(ROOT / "ORKESTRA.tr.md", "Güvenilirlik: tavanlar, kilitler ve kapılar"))}
        for doc, text in en.items():
            for needle in (f"{lock_hook} s in a hook", f"{lock_cli} s in the CLI", f"{tempo.WAIT_MAX} s", f"{skip} s or more",
                           f"{gate_timeout} s"):
                self.assertIn(needle, text, f"{doc.name}: {needle}")
        for doc, text in tr.items():
            for needle in (f"hook'ta {lock_hook} sn", f"CLI'de {lock_cli} sn", f"{tempo.WAIT_MAX} sn", f"{skip} sn ve üstü",
                           f"{gate_timeout} sn"):
                self.assertIn(needle, text, f"{doc.name}: {needle}")
        self.assertIn(f"{cpu} s of CPU or {wall} s of wall clock", en[README_EN])
        self.assertIn(f"{cpu} sn CPU ya da {wall} sn duvar saati", tr[README_TR])
        # the stall predicates the fast-watcher table and the READMEs quote
        from orkestra_core import fastwatch
        self.assertEqual((fastwatch.DENY_REPEATS, fastwatch.DENY_BLOCK_REPEATS, fastwatch.FLIP_COUNT, fastwatch.TICK,
                          fastwatch.IDLE_SETTLE, fastwatch.PERM_WAIT, tempo.DUE_REPORT_SECONDS),
                         (3, 8, 3, 5.0, 20.0, 30.0, 60), "a watcher threshold changed: update the docs and this test together")
        for doc, row in ((self.ORK[0], "| report overdue |"), (self.ORK[1], "| rapor gecikti |")):
            self.assertIn(row, read(doc))
        dispatch = {m.group(1): int(m.group(2)) for m in re.finditer(
            r"(?m)^(DENY_BLOCK_HITS|STOP_NUDGE_CEILING|MAX_STOP_BLOCKS_PER_PROMPT)\b[^=\n]*=\s*(\d+)", read(hm))}
        self.assertEqual(dispatch, {"DENY_BLOCK_HITS": 8, "STOP_NUDGE_CEILING": 6, "MAX_STOP_BLOCKS_PER_PROMPT": 2})

    def test_the_stall_tables_of_the_architecture_pages_state_the_current_predicates(self):
        from orkestra_core import tempo
        for page in (HTML_DOCS_EN, HTML_DOCS_TR, HTML_ROOT_TR):
            html = read(page)
            self.assertIn(f"{tempo.DUE_REPORT_SECONDS} sn" if page != HTML_DOCS_EN else f"{tempo.DUE_REPORT_SECONDS} s", html)
            self.assertNotIn("90 sn önce bitti", html)
            self.assertNotIn("finished 90 s ago", html)
            self.assertNotIn("8 saniyede bitmezse" if page != HTML_DOCS_EN else "does not finish in 8 seconds", html)

    def test_contributing_explains_the_hook_budget_and_the_decision_corpus(self):
        text = squash(read(ROOT / "CONTRIBUTING.md"))
        self.assertIn("## Changing hooks and guards", text)
        for needle in ("test_hook_latency", "guard_corpus.py --check", "guard_corpus.py --write", "BASH_MULTI", "dispatch_pre_c2",
                       "35 percent", "LazyModule", "[orkestra:bind]", "decision"):
            self.assertIn(needle, text)
        corpus = helpers.PLUGIN / "tests" / "guard_corpus.py"
        src = read(corpus)
        for symbol in ("BASH_MULTI = [", "FILE_PATHS", '"--write"', "def check"):
            self.assertIn(symbol, src, f"CONTRIBUTING sends contributors to {symbol}, which is gone")
        self.assertGreater(len(json.loads(read(helpers.PLUGIN / "tests" / "data" / "guard_decisions.json"))["events"]), 5000,
                           "CONTRIBUTING says the corpus holds more than 5000 events")
        self.assertTrue((helpers.PLUGIN / "tests" / "data" / "dispatch_pre_c2.py.txt").is_file())

    def test_the_unreleased_entry_names_the_reliability_work(self):
        changelog = read(ROOT / "CHANGELOG.md")
        unreleased = changelog[changelog.index("## Unreleased"):changelog.index("\n## ", changelog.index("## Unreleased") + 5)]
        for needle in ("orkestra wait", "watchdog --loop", "--status", "gate verify", "--resume", "brief --review",
                       "tempo.max_dispatches", "watchdog.autostart", "ORKESTRA_LOCK_TIMEOUT", "guard_decisions.json",
                       "[orkestra:bind]", "home=", "fastwatch"):
            self.assertIn(needle, unreleased, f"the Unreleased entry does not mention {needle}")

    def test_the_helpers_of_this_class_find_and_reject(self):
        self.assertEqual(squash("a\n  b   c"), "a b c")
        self.assertIn("Reliability and speed", section_of(README_EN, "Reliability and speed").splitlines()[0])
        self.assertNotIn("\n## ", section_of(README_EN, "Reliability and speed")[4:])
        with self.assertRaises(ValueError):
            section_of(README_EN, "No such section")
        self.assertEqual(source_number(self.LIB / "hookmain.py", "GUARD_DEADLINE_SECONDS"), 9.0)


class TestEachLanguagePointsAtItsOwnPage(unittest.TestCase):
    """`mirror_key` maps the Turkish architecture page to the English name when links are compared, which hides a
    reference to the wrong language in a code span (ORKESTRA.md sent English readers to the Turkish page)."""

    def test_the_one_page_references_name_the_page_of_their_language(self):
        english, turkish = read(ROOT / "ORKESTRA.md"), read(ROOT / "ORKESTRA.tr.md")
        self.assertIn("docs/architecture.html", english)
        self.assertNotIn("orkestra-mimari", english)
        self.assertIn("docs/orkestra-mimari.html", turkish)
        self.assertNotIn("architecture.html", turkish)

    def test_every_architecture_reference_in_the_english_documents_is_the_english_page(self):
        for doc in (README_EN, ONBOARDING_EN, ROOT / "ORKESTRA.md", ROOT / "CONTRIBUTING.md"):
            for line in read(doc).splitlines():
                if "orkestra-mimari" in line:
                    self.assertTrue("Turkish" in line or "Türkçe" in line, f"{doc.name}: {line.strip()[:100]}")


class TestDocsIndex(unittest.TestCase):
    def test_docs_readme_lists_every_page_in_docs(self):
        index = read(ROOT / "docs" / "README.md")
        for page in sorted((ROOT / "docs").glob("*.html")):
            self.assertIn(f"]({page.name})", index, f"docs/README.md does not link {page.name}")
        for target in ("../README.md", "../README.tr.md", "../ONBOARDING.md", "../ONBOARDING.tr.md", "../CONTRIBUTING.md"):
            self.assertIn(f"]({target}", index)


class TestNewTextRules(unittest.TestCase):
    EM_DASH = "—"

    def test_no_em_dash_in_the_new_user_visible_documents(self):
        for path in (README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR, ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md",
                     ROOT / "ORKESTRA.md",
                     ROOT / "docs" / "README.md", ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md",
                     ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
                     ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml",
                     ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"):
            self.assertNotIn(self.EM_DASH, read(path), path.relative_to(ROOT))
        changelog = read(ROOT / "CHANGELOG.md")
        unreleased = changelog[changelog.index("## Unreleased"):changelog.index("\n## ", changelog.index("## Unreleased") + 5)]
        self.assertNotIn(self.EM_DASH, unreleased)

    def test_the_unreleased_changelog_entry_is_english_and_names_the_effort(self):
        changelog = read(ROOT / "CHANGELOG.md")
        unreleased = changelog[changelog.index("## Unreleased"):changelog.index("\n## ", changelog.index("## Unreleased") + 5)]
        self.assertEqual(TR_LETTERS.findall(re.sub(r"`[^`\n]*`", "", unreleased)), [])
        for needle in ("orkestra lang", "--lang", "--reason", "install.ps1", "run_hook.sh", "CI", "test_docs_drift",
                       "Windows", "README.tr.md", "architecture.html"):
            self.assertIn(needle, unreleased, f"the Unreleased entry does not mention {needle}")

    def test_agents_md_has_the_single_localization_rule(self):
        agents = read(ROOT / "AGENTS.md")
        self.assertEqual(len(re.findall(r"\*\*Localization\.\*\*", agents)), 1)
        for needle in ("messages/*.py", "i18n.t", "messages/protocol.py", "ORKESTRA_LANG=tr", "Adding a language"):
            self.assertIn(needle, agents)

    def test_contributing_explains_text_and_languages(self):
        text = read(ROOT / "CONTRIBUTING.md")
        for heading in ("## Adding or changing text", "## Adding a language", "## Before you open a pull request"):
            self.assertIn(heading, text)
        for needle in ("i18n.t(", "messages/protocol.py", "ORKESTRA_LANG", "test_i18n", "test_docs_drift", "cursor build",
                       "templates/<code>/", "i18n.AREAS"):
            self.assertIn(needle, text)


class TestCommunityFiles(unittest.TestCase):
    GH = ROOT / ".github"

    def test_issue_forms_ask_for_the_environment(self):
        bug = read(self.GH / "ISSUE_TEMPLATE" / "bug_report.yml")
        feat = read(self.GH / "ISSUE_TEMPLATE" / "feature_request.yml")
        self.assertIn("body:", bug)
        self.assertIn("body:", feat)
        for needle in ("orkestra --version", "orkestra doctor", "Python version", "Operating system", "Engine", "Windows",
                       "Claude Code", "Cursor", "GitHub Copilot", "required: true"):
            self.assertIn(needle, bug)
        ids = re.findall(r"^\s+id:\s*(\S+)", bug, re.M)
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids in the bug form")
        for needed in ("version", "engine", "os", "python", "doctor", "steps"):
            self.assertIn(needed, ids)
        self.assertIn("security/advisories/new", bug)
        self.assertIn("standard library", feat)
        self.assertIn("dependency", feat.lower())

    def test_config_disables_blank_issues_and_points_at_security(self):
        cfg = read(self.GH / "ISSUE_TEMPLATE" / "config.yml")
        self.assertRegex(cfg, r"(?m)^blank_issues_enabled:\s*false\s*$")
        self.assertIn("security/advisories/new", cfg)
        self.assertIn("SECURITY.md", cfg)
        for url in re.findall(r"url:\s*(\S+)", cfg):
            self.assertTrue(url.startswith("https://github.com/soft-ai-solutions/orkestra/"), url)

    def test_pull_request_template_carries_the_gates(self):
        pr = read(self.GH / "PULL_REQUEST_TEMPLATE.md")
        for needle in ("plugins/orkestra/tests", "bridge/tests", "python3 -m unittest discover", "cursor build",
                       "test_i18n", "test_docs_drift", "secrets", "ORKESTRA_LANG", "Python 3.9", "npm test"):
            self.assertIn(needle, pr)
        self.assertGreaterEqual(pr.count("- [ ]"), 10)

    def test_dependabot_covers_github_actions_only(self):
        text = read(self.GH / "dependabot.yml")
        self.assertEqual(re.findall(r'package-ecosystem:\s*"?([\w-]+)"?', text), ["github-actions"])
        self.assertIn("version: 2", text)
        used = set(re.findall(r"uses:\s*([\w./-]+)@", read(self.GH / "workflows" / "tests.yml")))
        self.assertTrue(used, "no GitHub Actions in the workflow?")

    def test_the_forms_are_the_only_issue_templates(self):
        # The old free-text bug_report.md / feature_request.md were removed (test_public follows): GitHub lists every valid
        # template of the folder, so they showed up next to the forms and skipped the version / OS / doctor questions.
        folder = self.GH / "ISSUE_TEMPLATE"
        forms = {p.name for p in folder.glob("*.yml")}
        self.assertTrue({"bug_report.yml", "feature_request.yml", "config.yml"} <= forms)
        self.assertEqual(sorted(p.name for p in folder.glob("*.md")), [], "a Markdown template next to the forms")


# --------------------------------------------------------------------------------------------- quickstart --help

class TestQuickstartCommandsRun(unittest.TestCase):
    """Every command a reader is told to type runs with `--help` (it exists and parses), in a throwaway home."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="orkestra-docs-"))
        cls.env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID")}
        cls.env.update(HOME=str(cls.tmp), USERPROFILE=str(cls.tmp), ORKESTRA_HOME=str(cls.tmp / "home"),
                       ORKESTRA_WORKSPACE=str(cls.tmp / "ws"), ORKESTRA_LANG="en", PYTHONIOENCODING="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_help(self, argv: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=str(self.tmp), env=self.env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, stdin=subprocess.DEVNULL)

    @staticmethod
    def command_lines(path: Path) -> List[str]:
        out = []
        for info, lines in split_markdown(read(path))[0]:
            if info not in ("bash", "powershell", "sh"):
                continue
            for raw in lines:
                line = re.sub(r"\s+#\s.*$", "", raw.strip())
                if line and not line.startswith("#"):
                    out.append(line)
        return out

    def test_orkestra_commands_in_the_quickstarts_run_with_help(self):
        wanted: Set[Tuple[str, Optional[str]]] = set()
        for doc in (README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR):
            for line in self.command_lines(doc):
                for mn in mentions_in(line):
                    action = mn.action if (FACTS.actions.get(mn.sub) and mn.action in FACTS.actions[mn.sub]) else None
                    if mn.sub == "lang":
                        action = None
                    wanted.add((mn.sub, action))
        self.assertGreaterEqual(len(wanted), 8, wanted)
        for sub, action in sorted(wanted, key=str):
            argv = [sys.executable, str(BIN), sub] + ([action] if action else []) + ["--help"]
            cp = self.run_help(argv)
            self.assertEqual(cp.returncode, 0, f"{' '.join(argv[2:])}: {cp.stderr[:200]}")
            self.assertIn("usage", cp.stdout.lower(), " ".join(argv[2:]))

    def test_installer_commands_in_the_quickstarts_run_with_help(self):
        scripts = {"install.py": [sys.executable, str(ROOT / "install.py"), "--help"]}
        if os.name != "nt" and shutil.which("bash"):
            scripts["install.sh"] = ["bash", str(ROOT / "install.sh"), "--help"]
        used: Set[str] = set()
        for doc in (README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR):
            for line in self.command_lines(doc):
                m = re.search(r"install\.(sh|py|ps1)", line)
                if m:
                    used.add(f"install.{m.group(1)}")
        self.assertEqual(used, {"install.sh", "install.py", "install.ps1"})
        self.assertTrue((ROOT / "install.ps1").is_file())
        for name, argv in scripts.items():
            cp = self.run_help(argv)
            self.assertEqual(cp.returncode, 0, f"{name} --help: {cp.stderr[:200]}")
            self.assertIn("--dry-run", cp.stdout, name)

    def test_the_files_the_quickstarts_run_exist(self):
        for doc in (README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR):
            for line in self.command_lines(doc):
                for rel in re.findall(r"(?:python3?|py -3|bash)\s+\.?/?((?:plugins|bridge)/[\w./-]+)", line):
                    self.assertTrue((ROOT / rel).is_file(), f"{doc.name}: `{line[:70]}` names a missing file")

    def test_quickstart_in_the_readme_is_the_documented_three_commands(self):
        text = read(README_EN)
        section = text.split("## Quickstart", 1)[1].split("\n## ", 1)[0]
        first_block = split_markdown(section)[0][0][1]
        cmds = [ln.split("#")[0].strip() for ln in first_block if ln.strip()]
        self.assertEqual(cmds[0], "git clone https://github.com/soft-ai-solutions/orkestra.git")
        self.assertEqual(cmds[1], "cd orkestra")
        self.assertTrue(cmds[2].endswith("bin/orkestra setup"))
        self.assertTrue(cmds[3].endswith("bin/orkestra doctor"))


class TestInstallerClaims(unittest.TestCase):
    """What the docs say the installers do and how to recover from a missing shim."""

    DOCS = (README_EN, README_TR, ONBOARDING_EN, ONBOARDING_TR)

    def test_a_missing_shim_is_never_fixed_by_the_command_the_shim_provides(self):
        for doc in self.DOCS:
            text = read(doc)
            seen = 0
            for paragraph in re.split(r"\n\s*\n", text):
                if "command not found" in paragraph:
                    seen += 1
                    self.assertIn("bin/orkestra setup", paragraph, f"{doc.name}: the remedy must run setup by path")
            self.assertGreaterEqual(seen, 1, doc.name)
            for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S):
                if "mkdir -p ~/.local/bin" in block:
                    lines = [ln.split("#")[0].strip() for ln in block.splitlines()]
                    self.assertNotIn("orkestra setup", lines, f"{doc.name}: `orkestra` is what is missing")

    def test_install_py_is_not_credited_with_what_only_install_sh_does(self):
        for doc in self.DOCS:
            text = read(doc)
            self.assertIn("--refresh-vendor", text, f"{doc.name} must say that --refresh-vendor is install.sh only")
            self.assertIn("--no-mcp", text, doc.name)
        cp = subprocess.run([sys.executable, str(ROOT / "install.py"), "--refresh-vendor"], capture_output=True, text=True,
                            encoding="utf-8", env={**os.environ, "ORKESTRA_LANG": "en"}, timeout=60)
        self.assertEqual(cp.returncode, 2, "the docs say install.py has no --refresh-vendor: keep both true")


if __name__ == "__main__":
    unittest.main()