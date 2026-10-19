"""The orkestra command line as data: ONE parser for the CLI and for the guards.

The guards used to read `orkestra …` command lines with their own small scanner. Every difference between that
scanner and argparse was a way to run one subcommand while the guard saw another (`phase --no show next`,
`orkestra -- task start T001`, `evidence a <cmd> --`). Now both call `parse()` and `executed_command()` below, so
what the guard analyses is by construction what the CLI executes. No heavy imports here (hooks load this module).
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Any, List, Optional, Tuple

from . import __version__, i18n

RUNS_A_COMMAND = ("tdd", "evidence")
SUB_ACTION = ("task", "gate", "phase", "memory", "report", "agents", "cursor", "copilot", "pi", "mcp")  # key = "<cmd>:<action>"
SUB_CHECK = ("plan", "trace", "deps", "review", "integrity")                   # key = "<cmd>:check"
GLOBAL_VALUE_OPTS = ("-p", "--project", "--run", "--lang")      # global options that take a value: hoisted to the front
GLOBAL_EQ_PREFIXES = ("--project=", "--run=", "--lang=")
LANG_CHOICES = ("en", "tr", "auto")
START_HERE = ("run", "watch", "next", "status", "doctor", "setup")     # the first-run group of `orkestra --help`


class CliParseError(Exception):
    pass


class CliHelp(Exception):
    pass


class QuietParser(argparse.ArgumentParser):
    """argparse that never prints and never exits (for the guards: a hook must not write help text to stdout)."""

    def error(self, message: str) -> Any:               # type: ignore[override]
        raise CliParseError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> Any:   # type: ignore[override]
        if status == 0:
            raise CliHelp()
        raise CliParseError(message or "")

    def print_help(self, file: Any = None) -> None:
        pass

    def print_usage(self, file: Any = None) -> None:
        pass

    def _print_message(self, message: str, file: Any = None) -> None:
        pass


def split_remainder(a: argparse.Namespace, tokens: List[str]) -> List[str]:
    """Pull -p/--project/--run that agents place before `--` out of a REMAINDER list.

    Without a `--` the remainder is the command verbatim (it may legitimately contain `-p`).
    """
    if "--" not in tokens:
        return tokens
    out: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            out.extend(tokens[i + 1:])
            break
        if tok in ("-p", "--project") and i + 1 < len(tokens):
            a.project = tokens[i + 1]
            i += 2
            continue
        if tok == "--run" and i + 1 < len(tokens):
            a.run = tokens[i + 1]
            i += 2
            continue
        if tok == "--why" and i + 1 < len(tokens):
            a.why = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--project="):
            a.project = tok.split("=", 1)[1]
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def lang_arg(raw: Any) -> str:
    """`type` of `--lang` and of `orkestra lang <value>`: en | tr | auto. Spellings like `TR`, `tr_TR.UTF-8` and
    `Turkish` are accepted (i18n.normalize); anything else is a usage error."""
    text = str(raw).strip()
    if text.lower() == "auto":
        return "auto"
    code = i18n.normalize(text)
    if code is None:
        raise argparse.ArgumentTypeError(f"unsupported language {text!r} (use en, tr or auto)")
    return code


def lang_arg_localized(raw: Any) -> str:
    """`lang_arg` for the CLI's own parser: the refusal reads in the user's language. (The guards' parser keeps the
    plain one: a hook must not load a catalog.)"""
    try:
        return lang_arg(raw)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(i18n.t("help.err.bad_lang", value=repr(str(raw).strip())))


def lang_or_show(raw: Any) -> str:
    """`type` of the `orkestra lang` argument: `show` or what `lang_arg` accepts."""
    if str(raw).strip().lower() == "show":
        return "show"
    try:
        return lang_arg(raw)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(f"unsupported value {str(raw).strip()!r} (use show, en, tr or auto)")


def lang_or_show_localized(raw: Any) -> str:
    """`lang_or_show` for the CLI's own parser (see `lang_arg_localized`)."""
    try:
        return lang_or_show(raw)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(i18n.t("help.err.bad_lang_value", value=repr(str(raw).strip())))


# argparse words its usage errors in English. Each pattern captures the parts of one of them; the catalog sentence of
# the same name (help.err.<key>) puts them back together in the user's language, and in English it is argparse's own
# sentence, so nothing changes there. A message no pattern knows is printed as argparse made it.
_ARGPARSE_ERRORS = (
    ("invalid_choice", re.compile(r"^argument (?P<arg>.+?): invalid choice: (?P<value>.+?) \(choose from (?P<choices>.*)\)$", re.S)),
    ("required", re.compile(r"^the following arguments are required: (?P<names>.+)$", re.S)),
    ("unrecognized", re.compile(r"^unrecognized arguments: (?P<rest>.+)$", re.S)),
    ("expected_one", re.compile(r"^argument (?P<arg>.+?): expected one argument$", re.S)),
    ("invalid_value", re.compile(r"^argument (?P<arg>.+?): invalid (?P<kind>\w+) value: (?P<value>.+)$", re.S)),
    ("not_allowed", re.compile(r"^argument (?P<arg>.+?): not allowed with argument (?P<other>.+)$", re.S)),
    # the refusal of a `type=` function (`--lang de`, `lang xx`): argparse prefixes it with its own English words
    ("argument", re.compile(r"^argument (?P<arg>.+?): (?P<msg>.+)$", re.S)),
)


def _unknown_command(value: str, choices: str) -> Optional[str]:
    """`Unknown command X. Did you mean Y?` for a mistyped subcommand (None: argparse's own sentence stays)."""
    import difflib
    name = value.strip("'\"")
    names = re.findall(r"'([^']+)'", choices)
    close = difflib.get_close_matches(name, names, n=1)
    if close:
        return i18n.t("help.err.unknown_command_hint", value=name, suggestion=close[0])
    return i18n.t("help.err.unknown_command", value=name)


class LocalizedParser(argparse.ArgumentParser):
    """argparse with the `error:` line in the user's language (usage and help already follow it). Same exit code (2)."""

    def error(self, message: str) -> Any:               # type: ignore[override]
        self.print_usage(sys.stderr)
        text = message
        for key, pattern in _ARGPARSE_ERRORS:
            found = pattern.match(message)
            if found:
                text = i18n.t("help.err." + key, **found.groupdict())
                if key == "invalid_choice" and found.group("arg").startswith("<command>"):
                    text = _unknown_command(found.group("value"), found.group("choices")) or text
                break
        self.exit(2, f"{self.prog}: {i18n.t('help.error_word')}: {text}\n")


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the line breaks of the description/epilog (the examples) and says `usage:` in the user's language."""

    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: Optional[str] = None) -> None:
        if prefix is None:
            prefix = i18n.t("help.usage") + " "
        super().add_usage(usage, actions, groups, prefix)

    def _format_action(self, action: Any) -> str:
        """The command list of `orkestra --help` in two groups: START_HERE first, then the rest (Advanced)."""
        if not isinstance(action, argparse._SubParsersAction) or not getattr(action, "_orkestra_grouped", False):
            return super()._format_action(action)
        subs = list(action._get_subactions())
        first = [x for name in START_HERE for x in subs if x.dest == name]
        rest = [x for x in subs if x.dest not in START_HERE]
        out = ""
        for key, items in (("help.group_start", first), ("help.group_advanced", rest)):
            if not items:
                continue
            out += " " * self._current_indent + i18n.t(key) + "\n"
            self._indent()
            out += "".join(super(_Formatter, self)._format_action(x) for x in items)
            self._dedent()
        return out


def _retitle(parser: argparse.ArgumentParser) -> None:
    """Translate the two default argument groups of a subcommand parser (argparse hard-codes English titles).
    Uses argparse internals, so a Python that renames them just keeps the English titles."""
    for attr, key in (("_positionals", "help.positionals"), ("_optionals", "help.options")):
        group = getattr(parser, attr, None)
        if group is not None:
            group.title = i18n.t(key)


def build_parser(parser_class: Any = LocalizedParser, *, localize: bool = True) -> argparse.ArgumentParser:
    """The one parser. `localize=False` (the guards) skips every catalog lookup: a hook never prints help."""
    def h(key: str) -> Optional[str]:
        return i18n.t(key) if localize else None

    # allow_abbrev=False everywhere: `--no` must never mean `--note` (the guards read the command line literally)
    p = parser_class(prog="orkestra", description=h("help.description"), epilog=h("help.epilog"),
                     formatter_class=_Formatter, add_help=False, allow_abbrev=False)
    g = p.add_argument_group(h("help.options")) if localize else p
    g.add_argument("-h", "--help", action="help", help=h("help.opt.help"))
    g.add_argument("--version", action="version", version=f"orkestra {__version__}", help=h("help.opt.version"))
    g.add_argument("-p", "--project", help=h("help.opt.project"))
    g.add_argument("--run", help=h("help.opt.run"))
    g.add_argument("--lang", type=lang_arg_localized if localize else lang_arg, metavar="{" + ",".join(LANG_CHOICES) + "}",
                   help=h("help.opt.lang"))
    sub_kw = {"dest": "cmd", "metavar": "<command>"}
    if localize:
        sub_kw["title"] = h("help.commands")
    sub = p.add_subparsers(**sub_kw)
    sub._orkestra_grouped = localize                    # type: ignore[attr-defined]

    def sp_(name: str, **kw: Any) -> argparse.ArgumentParser:
        kw.setdefault("allow_abbrev", False)
        kw.setdefault("add_help", False)
        kw.setdefault("formatter_class", _Formatter)
        line = h("help.cmd." + name)
        if line:                                        # the one-liner of `orkestra --help` and `orkestra <name> --help`
            kw.setdefault("help", line)
            kw.setdefault("description", line)
        child = sub.add_parser(name, **kw)
        child.add_argument("-h", "--help", action="help", help=h("help.opt.help"))
        if localize:
            _retitle(child)
        return child

    sp_("context")
    sp_("init")
    s = sp_("start")
    s.add_argument("text", nargs="?", help=h("help.opt.start_text"))
    s.add_argument("--title", help=h("help.opt.start_title"))
    s.add_argument("--request")
    s.add_argument("--request-file")
    s.add_argument("--size", default="auto", choices=["auto", "XS", "S", "M", "L"],
                   help=h("help.opt.start_size"))
    s.add_argument("--headless", action="store_true")
    s.add_argument("--engine", choices=["claude", "cursor", "copilot", "pi"], help=h("help.opt.start_engine"))
    s.add_argument("--approval-mode", choices=["autopilot", "manual"], default=None, help=h("help.opt.approval_mode"))
    sp_("resume")
    s = sp_("status")
    s.add_argument("--json", action="store_true")
    s = sp_("phase")
    s.add_argument("action", choices=["next", "back", "show", "check"])
    s.add_argument("target", nargs="?")
    s.add_argument("--note")
    s.add_argument("--reason")
    for name in ("await", "block", "unblock", "pause", "abort"):
        s = sp_(name)
        s.add_argument("reason", nargs="*")
        s.add_argument("--reason", dest="reason_opt", metavar="TEXT",
                       help=h("help.opt.reason_opt"))
    s = sp_("gate")
    s.add_argument("action", choices=["detect", "list", "confirm", "baseline", "run", "verify", "last"])
    s.add_argument("names", nargs="*")
    s.add_argument("--write", action="store_true")
    s.add_argument("--resume", action="store_true", help=h("help.opt.gate_resume"))
    s.add_argument("--all", action="store_true", help=h("help.opt.gate_all"))
    s = sp_("tdd")
    s.add_argument("kind", choices=["red", "green"])
    s.add_argument("task")
    s.add_argument("--why", default="", help=h("help.opt.tdd_why"))
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sp_("evidence")
    s.add_argument("label")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sp_("integrity")
    s.add_argument("check", choices=["check"])
    s = sp_("report")
    s.add_argument("action", choices=["evidence"])
    s.add_argument("--write", action="store_true")
    s = sp_("task")
    s.add_argument("action", choices=["list", "start", "done", "block", "defer", "reopen", "note", "add"])
    s.add_argument("task", nargs="?")
    s.add_argument("text", nargs="*")
    s.add_argument("--agent")
    s.add_argument("--no-tdd", action="store_true")
    s.add_argument("--force", default="", help=h("help.opt.task_force"))
    s.add_argument("--files", default="", help=h("help.opt.task_files"))
    s = sp_("next")
    s.add_argument("--json", action="store_true")
    s = sp_("wait")
    s.add_argument("--max", type=float, default=540.0, help=h("help.opt.wait_max"))
    s = sp_("tempo")
    s.add_argument("--json", action="store_true")
    s = sp_("agents")
    s.add_argument("action", nargs="?", default="list", choices=["list", "clear"])
    s.add_argument("--json", action="store_true")
    s = sp_("watchdog")
    s.add_argument("--json", action="store_true")
    s.add_argument("--dry-run", action="store_true", help=h("help.opt.wd_dry_run"))
    s.add_argument("--quiet", action="store_true", help=h("help.opt.wd_quiet"))
    s.add_argument("--launchd", action="store_true", help=h("help.opt.wd_launchd"))
    s.add_argument("--loop", action="store_true", help=h("help.opt.wd_loop"))
    s.add_argument("--daemon", action="store_true", help=h("help.opt.wd_daemon"))
    s.add_argument("--status", action="store_true", help=h("help.opt.wd_status"))
    s.add_argument("--idle-exit", type=float, default=0.0, help=argparse.SUPPRESS)   # --loop: leave after N idle seconds
    s = sp_("brief")
    s.add_argument("task")
    s.add_argument("--review", action="store_true", help=h("help.opt.brief_review"))
    for name in ("plan", "trace", "deps", "review"):
        s = sp_(name)
        s.add_argument("check", choices=["check"])
    s = sp_("changes")
    s.add_argument("--json", action="store_true")
    s = sp_("diff")
    s.add_argument("--stat", action="store_true")
    s = sp_("rollback")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--all", action="store_true", help=h("help.opt.rollback_all"))
    s = sp_("clean")
    s.add_argument("--dry-run", action="store_true", help=h("help.opt.dry_run"))
    s.add_argument("--gradle-transforms", action="store_true", help=h("help.opt.clean_gradle"))
    s = sp_("memory")
    s.add_argument("action", choices=["show", "feature-add", "decision", "learn", "observe", "check"])
    s.add_argument("text", nargs="*")
    s.add_argument("--title")
    s.add_argument("--summary")
    s.add_argument("--capabilities")
    s = sp_("playbook")
    s.add_argument("name", nargs="?", help=h("help.opt.playbook_name"))
    s = sp_("template")
    s.add_argument("name")
    s = sp_("watch")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--all", action="store_true", help=h("help.opt.watch_all"))
    s.add_argument("--json", action="store_true")
    s = sp_("dashboard")
    s.add_argument("--port", type=int, default=47616)
    s.add_argument("--open", action="store_true", help=h("help.opt.dash_open"))
    s.add_argument("--replace", action="store_true", help=h("help.opt.dash_replace"))
    s = sp_("run")
    s.add_argument("request", nargs="+", help=h("help.opt.run_request"))
    s.add_argument("--engine", choices=["claude", "cursor", "copilot", "pi"], help=h("help.opt.run_engine"))
    s.add_argument("--approval-mode", choices=["autopilot", "manual"], default=None, help=h("help.opt.approval_mode"))
    s.add_argument("--print", action="store_true", help=h("help.opt.run_print"))
    s.add_argument("--model", default=None, help=h("help.opt.run_model"))
    s.add_argument("--effort", default=None, help=h("help.opt.run_effort"))
    s.add_argument("--context", default=None, help=h("help.opt.run_context"))
    s.add_argument("--allow-all", dest="allow_all", action="store_true", default=None, help=h("help.opt.run_allow_all"))
    s.add_argument("--no-allow-all", dest="allow_all", action="store_false", help=h("help.opt.run_no_allow_all"))
    s = sp_("cursor")
    s.add_argument("action", choices=["build", "install", "uninstall", "status"])
    s.add_argument("--copy", action="store_true", help=h("help.opt.copy"))
    s.add_argument("--dry-run", action="store_true", help=h("help.opt.dry_run"))
    s.add_argument("--fail-closed", action="store_true", help=h("help.opt.fail_closed"))
    s = sp_("copilot")
    s.add_argument("action", choices=["install", "uninstall", "status", "vscode"])
    s.add_argument("--dry-run", action="store_true", help=h("help.opt.dry_run"))
    s.add_argument("--apply", action="store_true", help=h("help.opt.vscode_apply"))
    s.add_argument("--remove", action="store_true", help=h("help.opt.vscode_remove"))
    s.add_argument("--workspace", default=None, metavar="PATH", help=h("help.opt.vscode_workspace"))
    s.add_argument("--user", action="store_true", help=h("help.opt.vscode_user"))
    s.add_argument("--permissions", choices=["autoApprove", "autopilot"], default=None, help=h("help.opt.vscode_permissions"))
    s.add_argument("--model", default=None, help=h("help.opt.vscode_model"))
    s.add_argument("--effort", default=None, help=h("help.opt.vscode_effort"))
    s = sp_("pi")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--dry-run", action="store_true", help=h("help.opt.dry_run"))
    s = sp_("mcp")
    s.add_argument("action", choices=["install", "status"])
    s.add_argument("--engine", choices=["claude", "cursor", "copilot", "all"], default="all",
                   help=h("help.opt.mcp_engine"))
    s.add_argument("--only", action="append", choices=["context7", "playwright", "serena"],
                   help=h("help.opt.mcp_only"))
    s = sp_("setup")
    s.add_argument("--yes", "-y", action="store_true", help=h("help.opt.setup_yes"))
    s = sp_("demo")
    s.add_argument("--keep", action="store_true", help=h("help.opt.demo_keep"))
    s = sp_("doctor")
    s.add_argument("--repair", action="store_true", help=h("help.opt.doctor_repair"))
    sp_("version")
    s = sp_("lang")
    s.add_argument("value", nargs="?", default="show", type=lang_or_show_localized if localize else lang_or_show,
                   metavar="{show," + ",".join(LANG_CHOICES) + "}",
                   help=h("help.opt.lang_value"))
    return p


def hoist_globals(argv: List[str]) -> List[str]:
    """Move -p/--project/--run/--lang to the front so they work anywhere (agents put them everywhere).

    Stops at `--`, and for `tdd <kind> <task>` leaves the test command untouched.
    """
    front: List[str] = []
    rest: List[str] = []
    i, positional, sub = 0, 0, None
    while i < len(argv):
        tok = argv[i]
        if tok == "--" or (sub == "tdd" and positional >= 3) or (sub == "evidence" and positional >= 2):
            rest.extend(argv[i:])
            break
        if tok in GLOBAL_VALUE_OPTS and i + 1 < len(argv):
            front += [tok, argv[i + 1]]
            i += 2
            continue
        if tok == "--force" and i + 1 < len(argv):      # keep its value from being read as a positional
            rest += [tok, argv[i + 1]]
            i += 2
            continue
        if tok.startswith(GLOBAL_EQ_PREFIXES):
            front.append(tok)
            i += 1
            continue
        if not tok.startswith("-"):
            if sub is None:
                sub = tok
            positional += 1
        rest.append(tok)
        i += 1
    return front + rest


_QUIET: List[Any] = []


def parse(parser: argparse.ArgumentParser, argv: List[str]) -> argparse.Namespace:
    """hoist_globals + parse_args, for the CLI and for the guards alike.

    A `--` before the subcommand is refused here: argparse versions disagree on it (3.9 runs `-- task start T001`
    as `task start`, 3.10+ rejects it), and the guard must never depend on which python runs the CLI."""
    args = hoist_globals(list(argv))
    k = 0
    while k < len(args):
        if args[k] in GLOBAL_VALUE_OPTS:
            k += 2
        elif args[k].startswith(GLOBAL_EQ_PREFIXES):
            k += 1
        else:
            break
    if k < len(args) and args[k] == "--":
        parser.error("`--` before the subcommand is not accepted")
    return parser.parse_args(args)


def requested_lang(argv: List[str]) -> Optional[str]:
    """The language `--lang` asks for (en | tr | auto; the last one wins), or None. Reads the global options the way
    `parse()` does, so the CLI can set ORKESTRA_LANG BEFORE it builds the (localized) parser; a bad value is left
    for the parser to reject."""
    args = hoist_globals(list(argv))
    value: Optional[str] = None
    k = 0
    while k < len(args):
        tok = args[k]
        if tok in GLOBAL_VALUE_OPTS:
            if tok == "--lang" and k + 1 < len(args):
                value = args[k + 1]
            k += 2
        elif tok.startswith(GLOBAL_EQ_PREFIXES):
            if tok.startswith("--lang="):
                value = tok.split("=", 1)[1]
            k += 1
        else:
            break
    if value is None:
        return None
    try:
        return lang_arg(value)
    except argparse.ArgumentTypeError:
        return None


def global_values(argv: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """(`-p/--project`, `--run`) exactly as `parse()` reads them (hoisted to the front, the last one wins), or None."""
    args = hoist_globals(list(argv))
    project: Optional[str] = None
    run: Optional[str] = None
    k = 0
    while k < len(args):
        tok = args[k]
        if tok in GLOBAL_VALUE_OPTS:
            if k + 1 < len(args):
                if tok in ("-p", "--project"):
                    project = args[k + 1]
                elif tok == "--run":
                    run = args[k + 1]
            k += 2
        elif tok.startswith(GLOBAL_EQ_PREFIXES):
            if tok.startswith("--project="):
                project = tok.split("=", 1)[1]
            elif tok.startswith("--run="):
                run = tok.split("=", 1)[1]
            k += 1
        else:
            break
    return project, run


def inspect(argv: List[str]) -> Tuple[str, List[str], str]:
    """(subcommand key, the command the call would execute, parse error) for the arguments after `orkestra`.

    key: "help" | "task:start" | "phase:next" | "plan:check" | "tdd" | … exactly as the CLI would dispatch it.
    The command list is non-empty only for `tdd` / `evidence`, and it is the list `runs.tdd_record` /
    `runs.run_evidence` receive. A non-empty error means the CLI would stop with a usage error (nothing runs)."""
    if not _QUIET:
        _QUIET.append(build_parser(QuietParser, localize=False))
    try:
        ns = parse(_QUIET[0], argv)
    except CliHelp:
        return "help", [], ""
    except CliParseError as exc:
        return "", [], str(exc) or "usage error"
    cmd = getattr(ns, "cmd", None)
    if not cmd:
        return "help", [], ""
    if cmd in RUNS_A_COMMAND:
        return cmd, split_remainder(ns, list(getattr(ns, "command", None) or [])), ""
    if cmd in SUB_ACTION:
        return f"{cmd}:{getattr(ns, 'action', '')}", [], ""
    if cmd in SUB_CHECK:
        return f"{cmd}:check", [], ""
    return cmd, [], ""
