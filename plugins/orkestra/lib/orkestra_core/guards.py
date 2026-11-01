"""Guard policy engine (pure functions, unit-tested). Hooks call `pre_tool_decision`.

Global guards (every session where the plugin is enabled, toggle in ~/.orkestra/config.json):
  git      git/gh writes only when the user's CURRENT prompt asks for them
  secrets  no reading/writing of secret files (.env, *.p8, SA keys, private keys ...)
  store    no EAS/expo.dev cloud build/submit/update (user rule 06)

Run guards (only for a session bound to an active orkestra run):
  internals  .orkestra/runs/** is CLI-only; spec.md is frozen after approval
  roles      read-only agents write only inside the feature folder; CLI subcommands by role
  phase      no source edits before the plan is approved
  isolation  no writes outside the run's project (temp dirs excepted)
  deps       no new dependency unless listed in plan `approved_dependencies`
  deploy     no deploy / publish / prod-migration / remote commands
  wait       the main session does not sleep/poll for subagent reports (they arrive as messages)
  dialogs    no command that opens a macOS system dialog on the user's screen
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import shellparse as sp
from .gates import normalize_pkg
from .lazy import LazyModule
from .paths import find_project_root, is_headless, orkestra_home
from .util import is_relative_to, resolve_path

# The CLI's own parser (argparse: ~15 ms to import and build). A hook reaches it only for the few `orkestra ...` lines
# whose decision depends on the parse (see _mentions): `orkestra status` costs nothing.
CP = LazyModule("cliparse", __package__)

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}

PRE_APPROVAL_PHASES = {"intake", "discover", "clarify", "plan", "approve"}
CODE_PHASES = {"implement", "verify", "review", "deliver"}

AGENT_PREFIX = "orkestra:"
READONLY_AGENTS = {"scout", "researcher", "architect", "plan-critic", "reviewer", "adversary", "verifier"}
IMPLEMENTER = "implementer"

# orkestra CLI subcommands each role may run (main session may run everything)
CLI_ALLOWED = {
    "implementer": {"context", "status", "brief", "tdd", "task:start", "task:note", "gate:run", "gate:list", "gate:last",
                    "evidence", "integrity:check", "next", "tempo",
                    "changes", "diff", "plan:check", "deps:check", "doctor", "help", "version", "template",
                    "playbook", "memory:show"},
    "readonly": {"context", "status", "brief", "changes", "diff", "gate:run", "gate:list", "trace:check",
                 "deps:check", "plan:check", "review:check", "doctor", "help", "version", "template", "playbook",
                 "memory:show", "gate:last", "evidence", "integrity:check", "report:evidence", "next", "tempo",
                 "task:list", "integrity", "phase:show", "phase:check"},
}

SECRET_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p8", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.ppk",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", "_netrc", ".pgpass", "credentials.json",
    "*service-account*.json", "*service_account*.json", "*serviceaccount*.json", "gen-lang-client-*.json",
    "*.secret", "*.secrets", "*.secret.json", "secrets.json", "secrets.yml", "secrets.yaml", ".dev.vars", "*.tfvars",
    "*.mobileprovision", "*.env", ".envrc", "key.properties", "keystore.properties", ".git-credentials", ".pypirc",
    "*/.aws/credentials", "*/.aws/config", "*/gh/hosts.yml", "*/.docker/config.json", "*.kdbx", "*.secret.json",
]
SECRET_ALLOW_GLOBS = [".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults",
                      ".env.*.example", ".env.*.sample", "*.env.example", "*.env.sample", "example.env", "sample.env",
                      "*.pub", "*.example.json", "*.tfvars.example"]
CONTENT_DUMPERS = {
    "cat", "less", "more", "head", "tail", "bat", "batcat", "strings", "xxd", "od", "hexdump", "base64",
    "nl", "tac", "sed", "gsed", "awk", "gawk", "grep", "egrep", "fgrep", "rg", "ag", "cut", "sort", "uniq",
    "jq", "yq", "python", "python3", "node", "ruby", "perl", "source", ".", "cp", "scp", "rsync", "curl",
    "wget", "openssl", "pbcopy", "xclip", "diff", "cmp", "vim", "vi", "nano", "code", "open", "dotenv",
    "printenv", "plutil", "security", "zip", "tar", "gpg", "iconv", "column", "paste", "fold", "tr",
}

TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/", "/dev/")


class Decision:
    """A guard's answer. A plain class with dataclass semantics (init, eq, repr): `dataclasses` costs a hook 5 ms."""
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, action: str = "allow", reason: str = "", guard: str = ""):
        self.action = action           # allow | deny
        self.reason = reason
        self.guard = guard

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.action, self.reason, self.guard) == (other.action, other.reason, other.guard)

    def __repr__(self) -> str:
        return f"Decision(action={self.action!r}, reason={self.reason!r}, guard={self.guard!r})"

    @property
    def denied(self) -> bool:
        return self.action == "deny"


ALLOW = Decision()


def deny(guard: str, reason: str) -> Decision:
    return Decision("deny", reason, guard)


# ====================================================================== helpers

_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _norm_path_markers(p: str) -> str:
    """Windows separators as `/`. On POSIX a backslash is an ordinary file-name character (`/tmp\\x` is not inside
    /tmp), so only drive and UNC paths (and every path on Windows) are rewritten."""
    p = p or ""
    return p.replace("\\", "/") if os.name == "nt" or _WINDOWS_PATH.match(p) else p


def _windows_temp_roots() -> list:
    roots = []
    for key in ("TEMP", "TMP", "LOCALAPPDATA"):
        val = os.environ.get(key, "")
        if not val:
            continue
        root = _norm_path_markers(val).rstrip("/")
        if key == "LOCALAPPDATA":
            root = root + "/Temp"
        if root:
            roots.append(root.casefold() + "/")
    return roots


def _is_temp(p: str) -> bool:
    n = _norm_path_markers(p)
    tmpdir = _norm_path_markers(os.environ.get("TMPDIR", "")).rstrip("/")
    if n.startswith(TEMP_PREFIXES) or n == "/dev/null":
        return True
    if tmpdir and n.casefold().startswith(tmpdir.casefold() + "/"):
        return True
    cf = n.casefold()
    return any(cf.startswith(r) for r in _windows_temp_roots())


def _is_temp_subdir(base: str) -> bool:
    """Directory strictly below a temp root (scratch dirs), not the root itself."""
    b = _norm_path_markers(base).rstrip("/").casefold() + "/"
    roots = [r for r in TEMP_PREFIXES if r != "/dev/"]
    tmpdir = _norm_path_markers(os.environ.get("TMPDIR", ""))
    if tmpdir:
        roots.append(tmpdir.rstrip("/").casefold() + "/")
    roots.extend(_windows_temp_roots())
    return any(b.startswith(r) and len(b) > len(r) for r in roots)


def is_secret_path(path: str, extra: Iterable[str] = (), allow: Iterable[str] = ()) -> bool:
    if not path:
        return False
    path = path.lower()                 # macOS volumes are case-insensitive: `.ENV` opens `.env`
    base = os.path.basename(path.rstrip("/"))
    if not base:
        return False
    for g in list(SECRET_ALLOW_GLOBS) + list(allow):
        if fnmatch.fnmatch(base, g.lower()) or fnmatch.fnmatch(path, g.lower()):
            return False
    for g in list(SECRET_GLOBS) + list(extra):
        if fnmatch.fnmatch(base, g.lower()) or fnmatch.fnmatch(path, g.lower()):
            return True
    return False


def looks_like_token_file(path: Path) -> bool:
    """.npmrc / .yarnrc.yml with auth tokens."""
    try:
        if path.name not in (".npmrc", ".yarnrc", ".yarnrc.yml") or not path.is_file() or path.stat().st_size > 65536:
            return False
        txt = path.read_text(encoding="utf-8", errors="ignore")
        return "_auth" in txt or "npmAuthToken" in txt or "password" in txt.lower()
    except Exception:
        return False


def looks_like_sa_key(path: Path) -> bool:
    try:
        if path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size > 262144:
            return False
        head = path.read_text(encoding="utf-8", errors="ignore")
        return '"private_key"' in head and ("BEGIN PRIVATE KEY" in head or '"client_email"' in head)
    except Exception:
        return False


def _agent_role(event: Dict[str, Any]) -> Optional[str]:
    at = event.get("agent_type") or ""
    if not at:
        return None
    if at.startswith(AGENT_PREFIX):
        name = at[len(AGENT_PREFIX):]
        if name == IMPLEMENTER:
            return "implementer"
        if name in READONLY_AGENTS:
            return "readonly"
        return "other"
    return "foreign"  # some other plugin's / built-in subagent (Explore, Plan, general-purpose)


def _tool_paths(event: Dict[str, Any]) -> List[str]:
    ti = event.get("tool_input") or {}
    out = []
    for key in ("file_path", "notebook_path", "path"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    return out


# ---------------------------------------------------------------- bash write detection

FORMATTERS = {
    "prettier": lambda a: "--write" in a or "-w" in a,
    "eslint": lambda a: "--fix" in a,
    "ruff": lambda a: "--fix" in a or (a[:1] == ["format"] and "--check" not in a and "--diff" not in a),
    "black": lambda a: "--check" not in a and "--diff" not in a,
    "isort": lambda a: "--check" not in a and "--check-only" not in a and "--diff" not in a,
    "gofmt": lambda a: "-w" in a,
    "goimports": lambda a: "-w" in a,
    "rustfmt": lambda a: "--check" not in a,
    "swiftformat": lambda a: "--lint" not in a,
    "clang-format": lambda a: "-i" in a,
    "rubocop": lambda a: "-a" in a or "-A" in a or "--autocorrect" in a,
    "ktlint": lambda a: "-F" in a or "--format" in a,
    "biome": lambda a: "--write" in a or "--apply" in a or (a[:1] == ["format"] and "--check" not in a),
    "dprint": lambda a: a[:1] == ["fmt"],
}


def with_cwd(segs: List[sp.Segment], cwd: str) -> List[Tuple[sp.Segment, str]]:
    """Pair each segment with the working directory it runs in (follows `cd`/`pushd` within the command)."""
    out: List[Tuple[sp.Segment, str]] = []
    cur = cwd
    for seg in segs:
        out.append((seg, cur))
        if seg.program in ("cd", "pushd"):
            args = [x for x in seg.args if not x.startswith("-")]
            if not args:
                cur = str(Path.home()) if seg.program == "cd" else cur
            elif args[0] != "-":
                cur = str(resolve_path(args[0], cur))
    return out


def _recursive_rm_targets(program: str, args: List[str]) -> Optional[List[str]]:
    name = os.path.basename(program or "").casefold()
    shells = {"bash", "sh", "zsh", "dash"}
    wrappers = {"command", "env", "exec", "find", "nice", "nohup", "sudo", "stdbuf", "time", "xargs"}
    if name in shells:
        for index, arg in enumerate(args[:-1]):
            if arg == "-c":
                found = False
                targets = []
                for nested in sp.segments(args[index + 1]):
                    result = _recursive_rm_targets(nested.program, nested.args)
                    if result is not None:
                        found = True
                        targets.extend(result)
                return targets if found else None
        return None
    if name in wrappers:
        for index, arg in enumerate(args):
            nested_name = os.path.basename(arg).casefold()
            if nested_name == "rm" or nested_name in shells:
                return _recursive_rm_targets(nested_name, args[index + 1:])
        return None
    if name != "rm":
        return None
    recursive = False
    options = True
    for arg in args:
        if options and arg == "--":
            options = False
            continue
        if not options or arg == "-" or not arg.startswith("-"):
            continue
        if arg.startswith("--"):
            recursive = recursive or arg.split("=", 1)[0] == "--recursive"
        else:
            recursive = recursive or any(flag in arg[1:] for flag in "rR")
    return sp.positional(args) if recursive else None


def bash_write_targets(seg: sp.Segment, cwd: str) -> List[str]:
    """Best-effort list of absolute paths a segment writes. "<cwd>/*" means 'unknown files under cwd'."""
    targets = raw_write_targets(seg)
    out = []
    for t in targets:
        if t == "*":
            out.append(str(Path(cwd).resolve(strict=False)) + "/*")
        elif t and not t.startswith("-"):
            out.append(str(resolve_path(t, cwd)))
    return out


def raw_write_targets(seg: sp.Segment) -> List[str]:
    """The path words a segment writes, exactly as typed (unresolved; "*" = unknown files under cwd)."""
    targets: List[str] = []
    for op, target in seg.redirects:
        if op in (">", ">>", ">|", "&>", "&>>", "<>", ">&") and target and target != "/dev/null" \
                and not target.startswith("/dev/"):          # `>& file` writes it too (`>&2` never gets here)
            targets.append(target)
    prog, a = seg.program, seg.args
    pos = sp.positional(a)
    if prog == "tee":
        targets += pos
    elif prog in ("sed", "gsed") and any(x == "-i" or x.startswith("-i") or x.startswith("--in-place") for x in a):
        uses_e = any(x in ("-e", "--expression", "-f", "--file") for x in a)
        a2 = list(a)
        for i, x in enumerate(a2[:-1]):          # BSD/macOS `sed -i '' …`: the empty backup suffix is not a file
            if x == "-i" and a2[i + 1] == "":
                del a2[i + 1]
                break
        files = sp.positional(a2, ("-e", "--expression", "-f", "--file"))
        targets += files if uses_e else files[1:]
    elif prog == "perl" and any(re.match(r"^-\w*i", x) for x in a):
        files = sp.positional(a, ("-e", "-E"))
        targets += files
    elif prog in ("rm", "rmdir", "unlink", "shred", "touch", "truncate", "mkdir"):
        targets += pos
    elif prog == "mv":
        targets += pos
    elif prog in ("cp", "rsync", "install", "ln", "ditto"):
        if len(pos) >= 2:
            targets.append(pos[-1])
    elif prog == "dd":
        targets += [x[3:] for x in a if x.startswith("of=")]
    elif prog in ("patch",) or (prog == "git" and a[:1] == ["apply"]):
        targets.append("*")
    elif prog in FORMATTERS and FORMATTERS[prog](a):
        files = [p for p in pos if not p.startswith(("format", "check", "fmt"))]
        targets += files or ["*"]
    elif prog == "cargo" and a[:1] == ["fmt"] and "--check" not in a:
        targets.append("*")
    elif prog in ("dart", "flutter") and a[:1] == ["format"] and "--output=none" not in a and "--set-exit-if-changed" not in a:
        targets.append("*")
    elif prog == "go" and a[:1] == ["fmt"]:
        targets.append("*")
    return targets


# A write target the hook cannot resolve: the shell expands it after the check. Temp variables are exempt.
_VAR_PATH_RE = re.compile(r"\$|`")
_TEMP_VAR_RE = re.compile(r"^\$(?:\{(?:TMPDIR|TMP|TEMP)(?::?-[^}$`]*)?\}|(?:TMPDIR|TMP|TEMP)(?![A-Za-z0-9_]))(?:/|$)")
_ASSIGN_RE = re.compile(r"(?:^|[;&|\n(]|\b(?:export|declare|local|readonly|typeset)\s+(?:-\w+\s+)?)\s*"
                        r"([A-Za-z_][A-Za-z0-9_]*)=((?:\"[^\"$`\\\n]*\"|'[^'\n]*'|[^\s\"'$`;&|()<>\\]*))")


_TEMP_DEFAULT_RE = re.compile(r"^/(?:private/)?(?:var/)?tmp(?:/|$)|^/private/var/folders/")
_ANY_ASSIGN_RE = re.compile(r"(?:^|[;&|\n(]|\b(?:export|declare|local|readonly|typeset)\s+(?:-\w+\s+)?)\s*([A-Za-z_][A-Za-z0-9_]*)=")
_REBIND_RE = re.compile(r"\b(?:unset|read|for|select|getopts|mapfile|readarray|printf\s+-v|declare|typeset|local)\b[^;&|\n]*")


def _rebinds(command: str, name: str) -> bool:
    """Does the command assign `name` by anything but a plain `NAME=literal` (unset, read, for, printf -v ...)?"""
    return any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", m.group(0))
               for m in _REBIND_RE.finditer(command or ""))


def is_temp_var_path(target: str, command: str = "") -> bool:
    """`$TMPDIR/x`, `${TMPDIR:-/tmp}/x`: a temp-directory variable followed by a literal remainder. Not when the
    remainder climbs out with `..`, when a `:-` default is not itself a temp directory, or when `command` assigns the
    variable (it is then whatever the command made it)."""
    m = _TEMP_VAR_RE.match(target)
    if not m or _VAR_PATH_RE.search(target[m.end():]) or ".." in target[m.end():].split("/"):
        return False
    default = re.search(r":?-([^}]*)\}", m.group(0))
    if default and not _TEMP_DEFAULT_RE.match(default.group(1)):
        return False
    name = re.search(r"TMPDIR|TMP|TEMP", m.group(0)).group(0)
    return not (command and (re.search(r"(?<![A-Za-z0-9_])" + name + r"=", command) or _rebinds(command, name)))


def literal_assignments(command: str) -> Dict[str, Tuple[int, str]]:
    """{NAME: (position, literal value)} for `NAME=literal` assignments of a command line (values without $ or backtick).
    A name is left out when the shell could read something else than this value: it is assigned more than once (in any
    form), only for one command (`X=v cmd`), inside a subshell, or rebound by unset / read / for."""
    command = command or ""
    counts: Dict[str, int] = {}
    for m in _ANY_ASSIGN_RE.finditer(command):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    found: Dict[str, Tuple[int, str]] = {}
    for m in _ASSIGN_RE.finditer(command):
        name, val = m.group(1), m.group(2)
        if counts.get(name, 0) != 1 or _rebinds(command, name):
            continue
        if m.group(0).lstrip()[:1] == "(" or re.match(r"[ \t]+[^\s;&|()<>#]", command[m.end():]):
            continue                                    # a subshell, or `NAME=v command` (the value lives for one command)
        nxt = command[m.end():m.end() + 1]
        if nxt and nxt in "$`(":
            continue                                    # `NAME=$(...)` is not the empty string
        if val[:1] in "\"'" and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]
        found[name] = (m.start(1), val)
    return found


def _python_touches_orkestra_home(raw: str, home: Path) -> bool:
    """A script that writes `~/.orkestra`, or that reads grants and session files there.

    Reading the project's own `.orkestra/config.json` is ordinary. `open(..., 'w')` of any
    `.orkestra/config.json` still counts, including a path that is not the live home."""
    if not re.search(r"\.orkestra/(config\.json|grants|sessions|active-runs)", raw or ""):
        return False
    home_s = str(home)
    private = ("~/.orkestra" in raw or (home_s and home_s in raw)
               or re.search(r"\.orkestra/(grants|sessions|active-runs)", raw))
    writing = bool(re.search(r"['\"][wax][a-z+]*['\"]|\.write\s*\(|\bos\.remove\b|\bunlink\b|>>|(?:^|[^<])>", raw))
    return bool(private or writing)


def _mktemp_dir(name: str, command: str) -> Optional[str]:
    """`NAME=$(mktemp -d /tmp/...)` is that temp directory. A template outside /tmp is not."""
    match = re.search(
        r"(?:^|[;&|\n])\s*" + re.escape(name) +
        r"=\$\(\s*mktemp\s+-d(?:\s+((?:/(?:private/)?(?:var/)?tmp)/\S+))?\s*\)",
        command or "")
    if not match:
        return None
    path = match.group(1) or "/tmp"
    if ".." in path.split("/"):
        return None
    return path.rstrip("/")


def _for_loop_stays_in_project(target: str, command: str) -> bool:
    """`for f in rel/a rel/b; do cp src "$f"; done`: every word is a relative path, so `$f` stays in the project."""
    match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?/?", target or "")
    if not match:
        return False
    loop = re.search(r"\bfor\s+" + re.escape(match.group(1)) + r"\s+in\s+([^;\n]+)", command or "")
    if not loop:
        return False
    words = loop.group(1).split()
    if not words:
        return False
    for word in words:
        if word.startswith(("/", "~", "$", "`", "-")) or ".." in word.split("/"):
            return False
    return True


def _prefix_temp_target(target: str, command: str) -> Optional[str]:
    """`STATE=/tmp/dir rm -rf "$STATE"`: the value applies only to that one command, not to a later `$STATE`."""
    match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", target or "")
    if not match:
        return None
    name = match.group(1)
    assigned = re.search(
        r"(?:^|[;&|\n])\s*" + re.escape(name) +
        r"=((?:/(?:private/)?(?:var/)?tmp)[^\s\"'$`;&|()<>\\]*)[ \t]+",
        command or "")
    if not assigned:
        return None
    start = assigned.end()
    end_at = re.search(r"[;&|\n]", command[start:])
    end = start + end_at.start() if end_at else len(command)
    uses = [command.find(mark + name, start) for mark in ("$", "${")]
    uses = [pos for pos in uses if pos >= 0]
    if not uses or min(uses) >= end:
        return None
    return assigned.group(1)


def expand_literal_vars(target: str, command: str) -> Optional[str]:
    """`target` with every $VAR / ${VAR} replaced by a literal assigned EARLIER in `command`; None when some variable
    (or a backtick / `$(`) cannot be resolved that way."""
    if "`" in target or "$(" in target:
        return None
    assigns = literal_assignments(command)
    bad = False

    def sub(m: "re.Match") -> str:
        nonlocal bad
        name = m.group(1) or m.group(2)
        hit = assigns.get(name)
        use = (command or "").find(m.group(0))
        if hit is None:
            temp = _mktemp_dir(name, command or "")
            where = (command or "").find(name + "=$(")
            if temp is None or where < 0 or (use >= 0 and where >= use):
                bad = True
                return m.group(0)
            return temp
        if use >= 0 and hit[0] >= use:
            bad = True
            return m.group(0)
        return hit[1]

    out = re.sub(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", sub, target)
    return None if bad or _VAR_PATH_RE.search(out) else out


# ====================================================================== global guards

def secrets_guard(event: Dict[str, Any], cfg: Dict[str, Any], segs: List[sp.Segment]) -> Decision:
    sconf = cfg.get("secrets") or {}
    extra, allow = sconf.get("extra_patterns") or [], sconf.get("allow") or []
    tool = event.get("tool_name")
    cwd = event.get("cwd") or os.getcwd()
    msg = ("Secret file access is blocked by orkestra (secrets guard). Do not read, write, move or delete credentials. "
           "Use the matching *.example file, ask the user for the variable NAME only, or ask the user to edit the file "
           "themselves.")

    def sniff(path: Path) -> bool:
        return (looks_like_sa_key(path) or looks_like_token_file(path)) and not any(fnmatch.fnmatch(str(path), g) for g in allow)

    if tool in WRITE_TOOLS or tool in READ_TOOLS or tool == "Grep":
        for p in _tool_paths(event):
            rp = str(resolve_path(p, cwd))
            if is_secret_path(rp, extra, allow):
                return deny("secrets", f"{msg} Path: {p}")
            if tool in READ_TOOLS and sniff(Path(rp)):
                return deny("secrets", f"{msg} Path contains credentials: {p}")
        if tool == "Grep":
            g = ((event.get("tool_input") or {}).get("glob") or "").strip()
            samples = (".env", ".env.local", ".env.production", "prod.env", "key.pem", "AuthKey_X.p8", "id_rsa")
            last = g.split("/")[-1]
            # `dir/**`, `*`, `*.*` match everything: they do not TARGET secret files (the search still skips them)
            if g and g.strip("*/{}") and any(fnmatch.fnmatch(x, last) for x in samples) \
                    and ".example" not in g and last != "*.*" and set(last) != {"*"}:
                return deny("secrets", f"{msg} Grep glob targets secret files: {g}")
    if tool == "Bash":
        for seg, scwd in with_cwd(segs, cwd):
            for t in bash_write_targets(seg, scwd):
                if not t.endswith("/*") and is_secret_path(t, extra, allow):
                    return deny("secrets", f"{msg} Command would write/move/delete: {t}")
            if seg.program not in CONTENT_DUMPERS:
                continue
            for tok in seg.args + [t for op, t in seg.redirects if op == "<"]:
                if tok.startswith("-") and "=" not in tok:
                    continue
                val = tok.split("=", 1)[1] if tok.startswith("-") else tok
                val = val.lstrip("@")
                if is_secret_path(val, extra, allow):
                    return deny("secrets", f"{msg} Command touches: {val}")
                if (val.endswith(".json") or os.path.basename(val).startswith((".npmrc", ".yarnrc"))) and \
                        sniff(resolve_path(val, scwd)):
                    return deny("secrets", f"{msg} Command touches a credentials file: {val}")
    return ALLOW


def home_guard(event: Dict[str, Any], segs: List[sp.Segment]) -> Decision:
    """~/.orkestra (guard config, grants, bindings) is never written by the model."""
    home = orkestra_home().resolve(strict=False)
    cwd = event.get("cwd") or os.getcwd()
    tool = event.get("tool_name")
    msg = ("~/.orkestra holds orkestra's guard configuration, permission grants and run bindings. The model must not "
           "change it — ask the user to edit it if a guard needs adjusting.")
    if tool in WRITE_TOOLS:
        for p in _tool_paths(event):
            if is_relative_to(resolve_path(p, cwd), home):
                return deny("home", msg)
    if tool == "Bash":
        for seg, scwd in with_cwd(segs, cwd):
            if seg.program == "orkestra":
                continue
            for t in bash_write_targets(seg, scwd):
                if is_relative_to(Path(t.rstrip("/*")), home):
                    return deny("home", msg)
            if seg.program in ("python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh") and \
                    _python_touches_orkestra_home(seg.raw, home):
                return deny("home", msg)
    return ALLOW


GIT_READ_SUBS = {
    "", "status", "diff", "log", "show", "blame", "grep", "ls-files", "ls-tree", "ls-remote", "rev-parse",
    "rev-list", "describe", "fetch", "cat-file", "shortlog", "whatchanged", "check-ignore", "check-attr",
    "merge-base", "name-rev", "count-objects", "fsck", "var", "help", "version", "--version", "archive",
    "format-patch", "diff-tree", "diff-files", "diff-index", "for-each-ref", "show-ref", "verify-commit",
    "verify-tag", "cherry", "range-diff", "show-branch", "annotate", "difftool", "clone", "init",
    "check-ref-format", "hash-object", "maintenance", "lfs",
}
GIT_ALWAYS_DENY = {"filter-branch", "filter-repo", "update-ref", "replace", "gc", "prune", "update-index",
                   "fast-import", "commit-tree", "write-tree", "read-tree", "symbolic-ref:write", "config:write",
                   "remote:write", "reflog:write", "notes:write"}


PROTECTED_BRANCH_RE = re.compile(r"(^\+?|:)(refs/heads/)?(main|master)$")


def _git_subkind(call: sp.GitCall, ctx: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[str]]:
    """Return (kind, needed_grant). kind: read | needs | forbidden.  needed_grant may be "a|b" (either)."""
    ctx = ctx or {}
    s, a = call.sub, call.args
    pos = sp.positional(a)
    shorts = sp.short_flags(a)
    hooks_off = any("core.hookspath" in g.lower() for g in call.globals) or any("core.hookspath" in x.lower() for x in a)

    if s in ("commit",):
        if "--no-verify" in a or "n" in shorts or hooks_off:
            return "forbidden", None
        if "--amend" in a:
            return "needs", "amend"
        return "needs", "commit"
    if s == "add":
        return ("read", None) if ("n" in shorts or "--dry-run" in a) else ("needs", "commit")
    if s == "push":
        if "--no-verify" in a or hooks_off or "--mirror" in a or "--delete" in a or "d" in shorts \
                or any(p.startswith(":") for p in pos) or "--prune" in a:
            return "forbidden", None
        forced = any(x.startswith("--force") for x in a) or "f" in shorts or any(p.startswith("+") for p in pos)
        if forced:
            refspecs = pos[1:] if len(pos) > 1 else []
            if any(PROTECTED_BRANCH_RE.search(p) for p in refspecs):
                return "forbidden", None
            if not refspecs and ctx.get("current_branch") in ("main", "master", None):
                return "forbidden", None
            return "needs", "force"
        if "--dry-run" in a or "n" in shorts:
            return "read", None
        return "needs", "push"
    if s in ("merge", "rebase", "cherry-pick", "revert", "am", "pull"):
        if any(x in ("--abort", "--quit", "--show-current-patch") for x in a):
            return "read", None
        return "needs", "sync"
    if s == "branch":
        mutating_flags = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force",
                          "-u", "--set-upstream-to", "--unset-upstream", "--edit-description", "-t", "--track"}
        listing = {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current",
                   "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--column", "--no-column",
                   "--color", "--no-color", "-q", "--quiet"}
        if any(x in ("-D",) for x in a):
            return "needs", "discard"
        if any(x in mutating_flags or x.startswith(("--set-upstream-to=", "--track=")) for x in a):
            return "needs", "branch"
        listing_mode = any(x in ("-l", "--list") for x in a)
        value_flags = ("--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format")
        p2 = sp.positional(a, value_flags)
        if p2 and not listing_mode:
            return "needs", "branch"
        _ = listing
        return "read", None
    if s in ("checkout", "switch"):
        if "--" in a or "." in pos:
            return "needs", "discard"
        if any(x in ("-b", "-B", "-c", "-C", "--orphan", "--detach", "-f", "--force") for x in a):
            return "needs", "branch"
        target = "-" if "-" in a else (pos[0] if len(pos) == 1 else None)
        existing = target is not None and (target == "-" or target in (ctx.get("branches") or []))
        if existing and ctx.get("clean") and not ctx.get("bound"):
            return "read", None  # switching to an existing branch with a clean tree, outside a run
        return "needs", "branch|discard"
    if s == "restore":
        if all(x in ("--staged", "-S") or not x.startswith("-") for x in a) and ("--staged" in a or "-S" in a) \
                and not ("--worktree" in a or "-W" in a):
            return "needs", "commit"
        return "needs", "discard"
    if s == "reset":
        if any(x in ("--hard", "--keep", "--merge") for x in a):
            return "needs", "hard_reset"
        if "--" in a or not pos:
            return "needs", "commit"
        return "needs", "discard"
    if s == "clean":
        return ("read", None) if ("n" in shorts or "--dry-run" in a) else ("needs", "hard_reset")
    if s == "stash":
        if a and a[0] in ("list", "show"):
            return "read", None
        if a and a[0] in ("drop", "clear"):
            return "needs", "discard"
        return "needs", "stash|discard"
    if s == "tag":
        listing = (not pos and not any(x in ("-d", "--delete", "-a", "-s", "-f", "-m", "-F", "-u") for x in a)) \
            or any(x in ("-l", "--list") for x in a)
        if listing:
            return "read", None
        return "needs", "tag"
    if s in ("worktree", "submodule", "sparse-checkout", "bisect"):
        if a and a[0] in ("list", "status", "summary"):
            return "read", None
        if s == "submodule" and a[:1] == ["update"] and "--remote" not in a:
            return "read", None
        if s == "bisect" and a[:1] in (["reset"], ["log"], ["view"], ["visualize"]):
            return "read", None
        if not a and s in ("submodule",):
            return "read", None
        return "needs", "branch"
    if s in ("rm", "mv"):
        if "--cached" in a and s == "rm":
            return "needs", "commit"
        return "needs", "commit"
    if s == "apply":
        return ("needs", "commit") if ("--cached" in a or "--index" in a) else ("read", None)
    if s == "config":
        read_flags = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope",
                      "--get-urlmatch", "--name-only", "-z", "--null", "--global", "--local", "--system",
                      "--worktree", "--includes", "--no-includes", "--type", "--bool", "--int", "--path"}
        if all(x in read_flags or not x.startswith("-") for x in a) and len(sp.positional(a, ("--type", "--file", "-f"))) <= 1:
            return "read", None
        return "forbidden", None
    if s == "remote":
        if not a or a[0] in ("-v", "--verbose", "show", "get-url"):
            return "read", None
        return "forbidden", None
    if s == "reflog":
        if a and a[0] in ("expire", "delete"):
            return "forbidden", None
        return "read", None
    if s == "notes":
        return ("read", None) if (not a or a[0] in ("list", "show")) else ("needs", "commit")
    if s == "symbolic-ref":
        return ("read", None) if len(pos) <= 1 and "-d" not in a and "--delete" not in a else ("forbidden", None)
    if s in ("filter-branch", "filter-repo", "update-ref", "replace", "update-index", "fast-import",
             "commit-tree", "write-tree", "read-tree"):
        return "forbidden", None
    if s in ("gc", "prune"):
        return ("forbidden", None) if any("prune" in x for x in a) or s == "prune" else ("read", None)
    if s == "lfs":
        return ("needs", "push") if (a[:1] == ["push"]) else ("read", None)
    if s == "flow":
        return "needs", "branch"
    if s in GIT_READ_SUBS:
        return "read", None
    return "read", None  # unknown subcommand / alias: allowed (documented limitation)


GH_READ = {
    "pr": {"view", "list", "status", "diff", "checks"},
    "issue": {"view", "list", "status"},
    "run": {"view", "list", "watch", "download"},
    "repo": {"view", "list", "clone"},
    "release": {"view", "list", "download"},
    "workflow": {"view", "list"},
    "gist": {"view", "list", "clone"},
    "label": {"list"},
    "search": None, "browse": None, "auth": None, "status": None, "help": None, "version": None,
    "cache": {"list"}, "ruleset": {"list", "view", "check"}, "extension": {"list"}, "codespace": {"list"},
    "secret": {"list"}, "variable": {"list", "get"}, "project": {"list", "view"}, "org": {"list"},
}


def _gh_subkind(seg: sp.Segment) -> Tuple[str, Optional[str]]:
    a = seg.args
    while a and a[0].startswith("-"):
        flag = a[0]
        a = a[2:] if flag in ("-R", "--repo", "--hostname") and len(a) > 1 else a[1:]
    if not a:
        return "read", None
    group = a[0]
    sub = a[1] if len(a) > 1 else ""
    if group == "api":
        method = None
        for i, x in enumerate(a):
            if x in ("-X", "--method") and i + 1 < len(a):
                method = a[i + 1].upper()
            elif x.startswith("--method="):
                method = x.split("=", 1)[1].upper()
            elif x.startswith("-X") and len(x) > 2:
                method = x[2:].upper()
        has_fields = any(x in ("-f", "-F", "--field", "--raw-field", "--input") or x.startswith(("--field=", "--raw-field="))
                         for x in a)
        if (method in (None, "GET")) and not has_fields:
            return "read", None
        if method == "GET" and has_fields:
            return "read", None
        if a[1:2] == ["graphql"] and method in (None, "POST") and not re.search(r"\bmutation\b", " ".join(a), re.I):
            return "read", None
        return "needs", "gh_write"
    allowed = GH_READ.get(group, set())
    if allowed is None:
        return "read", None
    if group in ("secret", "variable") and sub not in (allowed or set()):
        return "forbidden", None
    if sub in (allowed or set()) or (not sub and group in GH_READ):
        return "read", None
    if group == "pr":
        if sub == "checkout":
            return "needs", "branch"
        if sub == "merge":
            return "needs", "sync"
        return "needs", "pr"
    return "needs", "gh_write"


GRANT_HINT = {
    "commit": "commit (e.g. 'commit this' / 'commitle')", "push": "push (e.g. 'push it' / 'pushla')",
    "force": "force push",
    "amend": "commit --amend", "branch": "branch create/switch (e.g. 'create a branch' / 'yeni branch aç')",
    "pr": "PR (e.g. 'open a PR' / 'PR aç')", "gh_write": "a GitHub write", "discard": "discarding changes",
    "sync": "merge/rebase/pull", "tag": "a tag", "stash": "stash (e.g. 'stash my changes' / 'stashle')",
    "hard_reset": "a hard reset / git clean (say 'reset --hard' or 'git clean' explicitly)",
}


def git_guard(event: Dict[str, Any], segs: List[sp.Segment], grants: Set[str]) -> Decision:
    ctx = dict(event.get("_git_ctx") or {})
    ctx.setdefault("bound", bool(event.get("_bound")))
    for seg in segs:
        call = sp.git_call(seg)
        if call is not None:
            kind, need = _git_subkind(call, ctx)
            label = f"git {call.sub}".strip()
        elif seg.program == "gh":
            kind, need = _gh_subkind(seg)
            label = "gh " + " ".join(seg.args[:2])
        else:
            continue
        if kind == "read":
            continue
        if kind == "forbidden":
            return deny("git", f"`{label}` is forbidden by orkestra (history/config-destroying, hook bypass, "
                               f"force-push to main, or secret change). Ask the user to run it themselves if really needed.")
        if need and not any(n in grants for n in need.split("|")):
            hint = " or ".join(GRANT_HINT.get(n, n) for n in need.split("|"))
            return deny("git", f"`{label}` needs explicit user permission in the CURRENT message "
                               f"({hint}). Policy: no branch/commit/push unless the user asks. "
                               f"Leave the changes in the working tree and tell the user they are ready to commit.")
    return ALLOW


def store_guard(segs: List[sp.Segment]) -> Decision:
    for seg in segs:
        prog, a = seg.program, seg.args
        if any(x in ("--eas-cloud", "--auto-submit") for x in a):
            return deny("store", "EAS/expo.dev cloud build/submit is forbidden (rule 06-local-store-release-only). "
                                 "Use the project's local build + direct App Store Connect / Play scripts.")
        if prog in ("eas", "eas-cli"):
            sub = a[0] if a else ""
            if sub in ("submit", "update", "deploy") or sub.startswith(("workflow", "submit:", "update:")) or sub == "build:internal" \
                    or (sub == "build" and "--local" not in a):
                return deny("store", f"`eas {sub}` (cloud) is forbidden (rule 06-local-store-release-only). "
                                     f"Use `eas build --local` or the project's local build/submit scripts.")
        if prog == "expo" and a[:1] == ["publish"]:
            return deny("store", "`expo publish` (expo.dev) is forbidden (rule 06-local-store-release-only).")
    return ALLOW


# ====================================================================== run guards

DEPLOY_PATTERNS: List[Tuple[str, Any]] = [
    ("npm", lambda a: a[:1] == ["publish"] or (a[:1] in (["run"], ["run-script"]) and len(a) > 1 and re.match(r"^(deploy|release|publish|submit|ship)", a[1]))),
    ("pnpm", lambda a: a[:1] in (["publish"], ["deploy"]) or (a[:1] == ["run"] and len(a) > 1 and re.match(r"^(deploy|release|publish|submit|ship)", a[1])) or (a[:1] and re.match(r"^(deploy|release|publish|submit|ship)", a[0]))),
    ("yarn", lambda a: a[:1] in (["publish"], ["npm"]) or (a[:1] and re.match(r"^(deploy|release|publish|submit|ship)", a[0]))),
    ("bun", lambda a: a[:1] == ["publish"] or (a[:1] == ["run"] and len(a) > 1 and re.match(r"^(deploy|release|publish|submit|ship)", a[1]))),
    ("vercel", lambda a: not a or a[0] in ("deploy", "promote", "alias", "rollback", "redeploy", "--prod", "-p", "remove", "rm") or "--prod" in a),
    ("netlify", lambda a: a[:1] in (["deploy"], ["sites:delete"])),
    ("fly", lambda a: a[:1] and a[0] in ("deploy", "launch", "scale", "secrets", "destroy", "apps")),
    ("flyctl", lambda a: a[:1] and a[0] in ("deploy", "launch", "scale", "secrets", "destroy", "apps")),
    ("wrangler", lambda a: a[:1] and a[0] in ("deploy", "publish", "secret", "delete", "rollback")),
    ("firebase", lambda a: a[:1] and a[0] in ("deploy", "hosting:disable", "functions:delete")),
    ("gcloud", lambda a: "deploy" in a or "delete" in a),
    ("aws", lambda a: any(x in a for x in ("deploy", "sync", "update-function-code", "update-service", "put-object", "delete-stack", "rm", "cp", "mv"))),
    ("sam", lambda a: a[:1] and a[0] in ("deploy", "delete")),
    ("serverless", lambda a: a[:1] and a[0] in ("deploy", "remove")),
    ("sls", lambda a: a[:1] and a[0] in ("deploy", "remove")),
    ("cdk", lambda a: a[:1] and a[0] in ("deploy", "destroy")),
    ("terraform", lambda a: a[:1] and a[0] in ("apply", "destroy", "import", "taint")),
    ("tofu", lambda a: a[:1] and a[0] in ("apply", "destroy", "import")),
    ("pulumi", lambda a: a[:1] and a[0] in ("up", "destroy", "import")),
    ("kubectl", lambda a: a[:1] and a[0] in ("apply", "delete", "create", "replace", "patch", "rollout", "scale", "set", "edit", "drain", "cordon")),
    ("helm", lambda a: a[:1] and a[0] in ("install", "upgrade", "uninstall", "rollback", "delete")),
    ("docker", lambda a: a[:1] == ["push"] or a[:2] == ["image", "push"] or "-H" in a or "--host" in a),
    ("heroku", lambda a: bool(a) and not a[0].startswith(("logs", "apps:info", "config:get"))),
    ("railway", lambda a: a[:1] and a[0] in ("up", "deploy", "down", "delete")),
    ("shopify", lambda a: (a[:2] in (["app", "deploy"], ["app", "release"])) or (a[:2] in (["theme", "push"], ["theme", "publish"]))),
    ("fastlane", lambda a: True),
    ("xcrun", lambda a: a[:1] == ["altool"] or (a[:1] == ["notarytool"] and "submit" in a)),
    ("prisma", lambda a: a[:2] in (["migrate", "deploy"], ["migrate", "reset"], ["db", "push"], ["db", "execute"])),
    ("supabase", lambda a: a[:2] in (["db", "push"], ["db", "reset"]) or a[:1] == ["link"] or (a[:1] == ["functions"] and "deploy" in a)),
    ("drizzle-kit", lambda a: a[:1] in (["push"], ["drop"])),
    ("ssh", lambda a: True),
    ("scp", lambda a: any(re.match(r"^[^/\s]+@?[^/\s]*:", x) for x in sp.positional(a))),
    ("rsync", lambda a: any(re.match(r"^[^/\s]+@?[^/\s]*:", x) for x in sp.positional(a))),
    ("sftp", lambda a: True),
    ("lftp", lambda a: True),
    ("make", lambda a: bool(a) and bool(re.match(r"^(deploy|release|publish|submit|ship)", a[0]))),
]


# Words that make a remote action impossible to undo (deleted resources, dropped data, a schema push that may drop
# columns, history rewritten on a server). Matched as whole words of the arguments, ssh's remote command included.
REMOTE_DESTRUCTIVE_WORDS = frozenset((
    "destroy", "delete", "remove", "rm", "rmdir", "uninstall", "purge", "prune", "drop", "dropdb", "truncate", "reset",
    "down", "taint", "wipe", "shred", "mkfs", "dd", "force",
))
SCHEMA_PUSH = {"prisma": (["db", "push"], ["db", "execute"]), "supabase": (["db", "push"],), "drizzle-kit": (["push"],)}


def remote_destructive(prog: str, args: List[str]) -> bool:
    if any(args[:len(p)] == p for p in SCHEMA_PUSH.get(prog, ())):
        return True
    words = {w for a in args for w in re.split(r"[^a-z0-9]+", a.lower()) if w}
    return bool(words & REMOTE_DESTRUCTIVE_WORDS)


def deploy_guard(segs: List[sp.Segment], grants: Iterable[str] = ()) -> Decision:
    """`deploy` in the grants (an explicit user request, or an owner command) lets a publish run. A destructive one never does."""
    may_deploy = "deploy" in set(grants)
    for seg in segs:
        for prog, pred in DEPLOY_PATTERNS:
            if seg.program == prog:
                try:
                    hit = bool(pred(seg.args))
                except Exception:
                    hit = False
                if hit and may_deploy:
                    if not remote_destructive(prog, seg.args):
                        continue
                    return deny("deploy", f"`{prog} {' '.join(seg.args[:3])}` would delete or overwrite something on a "
                                          f"server, a cloud account or a database that cannot be restored. Owner "
                                          f"commands never run these. Tell the owner the exact command instead.")
                if hit:
                    return deny("deploy", f"`{prog} {' '.join(seg.args[:3])}` is a deploy/publish/remote/prod-DB action. "
                                          f"Orkestra never runs these during a run. Finish the run, then tell the user "
                                          f"the exact command to run (project isolation rule 05 applies).")
    return ALLOW


def _install_packages(seg: sp.Segment) -> Optional[Tuple[str, List[str]]]:
    p, a = seg.program, seg.args
    vf = ("--registry", "--prefix", "-C", "--dir", "--filter", "-w", "--workspace", "--save-prefix", "--tag",
          "-i", "--index-url", "--extra-index-url", "-c", "--constraint", "--target", "-t", "--python", "-p",
          "--group", "-G", "--optional", "--extra", "--source", "--version", "-v", "--framework", "-f")
    if p in ("npm", "cnpm") and a[:1] and a[0] in ("i", "install", "add", "isntall", "in"):
        return "npm", sp.positional(a[1:], vf)
    if p == "pnpm" and a[:1] and a[0] in ("add", "install", "i"):
        return "pnpm", sp.positional(a[1:], vf)
    if p == "yarn" and a[:1] and a[0] in ("add",):
        return "yarn", sp.positional(a[1:], vf)
    if p == "bun" and a[:1] and a[0] in ("add", "install", "i"):
        return "bun", sp.positional(a[1:], vf)
    if p == "expo" and a[:1] == ["install"]:
        return "expo", sp.positional(a[1:], vf)
    if p in ("pip", "pip3") and a[:1] == ["install"]:
        return "pip", _pip_pkgs(a[1:])
    if p in ("python", "python3") and a[:3] == ["-m", "pip", "install"]:
        return "pip", _pip_pkgs(a[3:])
    if p == "uv" and a[:1] == ["add"]:
        return "uv", sp.positional(a[1:], vf)
    if p == "uv" and a[:2] == ["pip", "install"]:
        return "uv", _pip_pkgs(a[2:])
    if p == "poetry" and a[:1] == ["add"]:
        return "poetry", sp.positional(a[1:], vf)
    if p == "pipenv" and a[:1] == ["install"]:
        return "pipenv", _pip_pkgs(a[1:])
    if p == "cargo" and a[:1] == ["add"]:
        return "cargo", sp.positional(a[1:], vf)
    if p == "go" and a[:1] == ["get"]:
        return "go", sp.positional(a[1:], vf)
    if p == "gem" and a[:1] == ["install"]:
        return "gem", sp.positional(a[1:], vf)
    if p == "bundle" and a[:1] == ["add"]:
        return "bundle", sp.positional(a[1:], vf)
    if p == "composer" and a[:1] in (["require"], ["req"]):
        return "composer", sp.positional(a[1:], vf)
    if p == "dotnet" and a[:1] == ["add"] and "package" in a:
        idx = a.index("package")
        return "dotnet", sp.positional(a[idx + 1:], vf)[:1]
    if p in ("flutter", "dart") and a[:2] == ["pub", "add"]:
        return "pub", sp.positional(a[2:], vf)
    if p == "brew" and a[:1] in (["install"], ["reinstall"]):
        return "brew", sp.positional(a[1:], vf)
    if p == "pod" and a[:1] == ["install"]:
        return None
    return None


def _pip_pkgs(a: List[str]) -> List[str]:
    out, skip = [], False
    for x in a:
        if skip:
            skip = False
            continue
        if x in ("-r", "--requirement", "-e", "--editable", "-c", "--constraint", "-i", "--index-url",
                 "--extra-index-url", "-t", "--target", "--python", "-f", "--find-links"):
            skip = True
            continue
        if x.startswith("-"):
            continue
        if x in (".", "..") or x.startswith(("./", "../", "/")) or x.endswith((".whl", ".tar.gz", ".zip")):
            continue
        out.append(x)
    return out


def deps_guard(segs: List[sp.Segment], approved: Iterable[str]) -> Decision:
    approved_n = {normalize_pkg(x) for x in approved}
    for seg in segs:
        hit = _install_packages(seg)
        if not hit:
            continue
        manager, pkgs = hit
        new = [normalize_pkg(p) for p in pkgs if normalize_pkg(p) and normalize_pkg(p) not in approved_n]
        if new:
            return deny("deps", f"New dependency not approved: {', '.join(new)} ({manager}). Only packages listed in "
                                f"plan.md `approved_dependencies` (approved by the user) may be installed. If it is truly "
                                f"needed, stop and ask the user; after approval re-plan or record it via `orkestra` plan change.")
    return ALLOW


# ---------------------------------------------------------------- wait guard (main session of a run)
# Claude Code runs subagents in the background and delivers their report as a new message. A main session that
# sleeps or polls for that report only delays it (measured on 2026-10-04: 8–29 % of wall time was this dead wait).

WAIT_SCAN_CHARS = 6000                   # hooks must stay fast on any input: scan a bounded prefix, linear patterns only
_LOOP_HEAD_RE = re.compile(r"\b(?:while|until)\b|\bfor\s+\w+\s+in\b")
_LOOP_DO_RE = re.compile(r"\bdo\b")
_LOOP_SLEEP_RE = re.compile(r"\bsleep\s+[\d.$]")
_LOOP_DONE_RE = re.compile(r"\bdone\b")
PY_WAIT_RE = re.compile(r"time\.sleep\(|while\s+time\.time\(\)")
PY_SLEEP_ARG_RE = re.compile(r"time\.sleep\(\s*(\d+(?:\.\d+)?)")
RUN_ARTIFACT_RE = re.compile(
    r"\.orkestra/|\borkestra\s+(?:-\S+\s+\S+\s+)*(?:status|task|watch|context|next|tempo)\b|\.report\.md|"
    r"\.review\.md|\bverification\.md|\btriage\.md")
TASK_OUTPUT_RE = re.compile(r"/tasks/[\w.-]+\.output")     # Claude Code's own file for a background agent or command


def _has_wait_loop(text: str) -> bool:
    """`while|until|for … do … sleep N … done`, in that order. Four linear searches (the old single regex with lazy
    quantifiers was super-linear: 17 KB of `while do sleep 1` took seconds)."""
    pos = 0
    for rx in (_LOOP_HEAD_RE, _LOOP_DO_RE, _LOOP_SLEEP_RE, _LOOP_DONE_RE):
        m = rx.search(text, pos)
        if not m:
            return False
        pos = m.end()
    return True
PASSIVE_PROGRAMS = {"sleep", "echo", "true", ":", "wait", "date", "printf", "cd"}


def _sleep_seconds(seg: sp.Segment) -> float:
    if seg.program != "sleep":
        return 0.0
    total = 0.0
    for x in seg.args:
        m = re.match(r"^(\d+(?:\.\d+)?)([smhd]?)$", x)
        if m:
            total += float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return total


def wait_guard(event: Dict[str, Any], binding: Dict[str, Any], segs: List[sp.Segment]) -> Decision:
    if event.get("tool_name") != "Bash" or event.get("agent_type"):
        return ALLOW                                # subagents may wait for their own background commands
    st = binding.get("state") or {}
    if st.get("status") != "active" or is_headless(st):
        return ALLOW                                # `claude -p` ends with its turn: there the old behaviour stays
    raw = ((event.get("tool_input") or {}).get("command") or "")[:WAIT_SCAN_CHARS]
    stripped = sp._strip_heredocs(raw)
    cwd = (event.get("cwd") or "") + "/"
    sleeps = [_sleep_seconds(s) for s in segs]
    python = bool(re.search(r"\bpython3?\b", stripped))
    loop = _has_wait_loop(stripped) or (python and bool(PY_WAIT_RE.search(raw)))
    touches = bool(RUN_ARTIFACT_RE.search(stripped)) or "/.orkestra/" in _norm_path_markers(cwd)
    waits = loop or max(sleeps or [0.0]) >= 10
    polls_run = touches and waits
    polls_output = waits and bool(TASK_OUTPUT_RE.search(stripped))   # may be the session's own background command
    # a plain wait is only "waiting for a subagent" while one is registered as running (a 30 s pause for a dev
    # server is fine otherwise) — the registry is read only when the command is a wait candidate at all
    py_sleep = max([float(x) for x in PY_SLEEP_ARG_RE.findall(raw)] or [0.0]) if python else 0.0
    plain_wait = (bool(segs) and all(s.program in PASSIVE_PROGRAMS for s in segs) and sum(sleeps) >= 30) \
        or sum(sleeps) >= 60 or py_sleep >= 30
    if not polls_run and not polls_output and not plain_wait:
        return ALLOW
    project = binding.get("project")
    pending = []
    try:
        from . import tempo as _T                 # late import: only wait candidates pay for it
        pending = _T.live_agents(Path(str(project)), str(binding.get("run_id") or st.get("run_id") or ""))[0]
    except Exception:
        pending = []
    if pending:
        return deny("wait", "Do not sleep or poll for a subagent. Subagents run in the background and each final report "
                            "is delivered to you as a new message — nothing you run here makes it arrive sooner, and a "
                            "waiting Bash call delays it. End your turn now (the Stop hook allows it while a subagent is "
                            "running).")
    if polls_run:
        return deny("wait", "Do not poll the run's files in a sleep loop: no subagent is registered as running, so "
                            f"nothing will change by waiting. Ask for the next step instead: `orkestra next -p {project}`.")
    return ALLOW


# ---------------------------------------------------------------- dialog guard (during a run)
# Commands that put a macOS system dialog on the user's screen (seen 2026-10-04: a test probed a locked keychain with
# `security show-keychain-info` and the user got password prompts for hours).

def dialog_guard(segs: List[sp.Segment]) -> Decision:
    for seg in segs:
        p, a = seg.program, seg.args
        hit = ""
        if p == "security" and a:
            sub = a[0]
            has_pw = any(x in ("-p", "-P", "-k") or x.startswith(("-p", "-k")) for x in a[1:])
            if sub in ("show-keychain-info", "authorize", "execute-with-privileges"):
                hit = f"security {sub}"
            elif sub in ("unlock-keychain", "create-keychain", "set-key-partition-list", "set-keychain-password") and not has_pw:
                hit = f"security {sub} (no password argument → GUI prompt)"
            elif sub in ("find-generic-password", "find-internet-password") and any(x in ("-w", "-g") or
                                                                                   re.match(r"^-[a-zA-Z]*[wg]", x) for x in a[1:]):
                hit = f"security {sub} -w/-g (reads a secret → access prompt)"
        elif p == "osascript" and re.search(r"display\s+(dialog|alert)|choose\s+(file|folder|from\s+list)", seg.raw, re.I):
            hit = "osascript dialog"
        elif p == "xcode-select" and "--install" in a:
            hit = "xcode-select --install"
        if hit:
            return deny("dialogs", f"`{hit}` opens a macOS system dialog on the user's screen. Orkestra never does that "
                                   f"during a run, and tests must not either: stub the binary (a fake `security` first on "
                                   f"PATH) or pass the password non-interactively to a throw-away keychain. If the real "
                                   f"call is unavoidable, ask the user first — they can run it themselves.")
    return ALLOW


# `orkestra …` command lines are read with the CLI's OWN parser (cliparse.inspect): what the guard analyses is what
# the CLI will execute. A private scanner here was a second opinion about the same string, and every difference was a
# bypass (`phase --no show next`, `orkestra -- task start T001`, `evidence a <cmd> --`).
MAX_CLI_NESTING = 4
_CLI_CACHE: Dict[Tuple[str, ...], Tuple[str, List[str], str]] = {}


def _cli_info(seg: sp.Segment) -> Tuple[str, List[str], str]:
    """(subcommand key, command a tdd/evidence call executes, parse error) — ("", [], error) when the CLI would stop
    with a usage error, in which case nothing runs."""
    key = tuple(seg.args)
    hit = _CLI_CACHE.get(key)
    if hit is None:
        if len(_CLI_CACHE) > 64:
            _CLI_CACHE.clear()
        try:
            hit = CP.inspect(list(seg.args))
        except Exception as exc:                    # never let a parser surprise open a hole: treat as unparseable
            hit = ("", [], f"cannot parse: {exc}")
        _CLI_CACHE[key] = hit
    return hit


# Subcommand names whose parse decides something. argparse picks a subcommand by an exact argument (no abbreviation of
# subcommand names), so a call that contains none of these words cannot parse to one of them. tests pin the first one
# to cliparse.RUNS_A_COMMAND and the second to run-control commands.
_RUNS_A_COMMAND = ("tdd", "evidence")


def _mentions(args: Iterable[str], names: Iterable[str]) -> bool:
    """Does one of `args` equal one of `names`? (the cheap test before a parse that would load argparse)"""
    wanted = set(names)
    return any(a in wanted for a in args)


def _cli_subcommand(seg: sp.Segment) -> Optional[str]:
    """None for other programs; "" when the command line does not parse (the CLI would refuse it)."""
    if seg.program != "orkestra":
        return None
    return _cli_info(seg)[0]


# ---------------------------------------------------------------- run control: who may pause / abort / resume a run
# A run has ONE driver: the chat bound to it (sessions/<id>.json, heartbeat = the file's mtime). Pause storms and
# stolen runs came from other chats and subagents steering a run they do not drive (`-p` of another project, advice to
# "pause on topic change", agents of one workflow sharing a session id). While the driver is alive (heard from within
# the watchdog lease) only the driver changes the run, unless the USER asks for it in the current message.

# command -> the control words of the user's message (intent.control_grants) that permit it
CONTROL_COMMANDS = {"pause": ("pause",), "abort": ("abort",), "resume": ("resume",), "unblock": ("resume",),
                    "block": ("pause", "abort", "resume"), "await": ("pause", "abort", "resume")}
CONTROL_EXAMPLE = {"pause": "pause the run", "abort": "abort the run", "resume": "continue", "unblock": "continue",
                   "block": "pause the run", "await": "pause the run"}


def _cli_project(arg: Optional[str], cwd: str) -> Optional[Path]:
    """The project folder `orkestra -p <arg>` resolves to, found the way cli._resolve_project finds it (None: the CLI
    would refuse, or there is nothing to find). test_binding_model pins the two together."""
    try:
        return _cli_project_unsafe(arg, cwd)
    except (OSError, RuntimeError, ValueError):      # name too long, symlink loop, NUL, lone surrogate: the CLI refuses it too
        return None


def _cli_project_unsafe(arg: Optional[str], cwd: str) -> Optional[Path]:
    from . import paths as P
    ws = P.workspace_root()
    if not arg:
        return P.find_project_root(Path(cwd))
    cand = Path(os.path.expanduser(arg))
    if not cand.is_absolute():
        local = (Path(cwd) / arg).resolve(strict=False)
        in_ws = (ws / arg).resolve(strict=False)
        if arg in (".", "..") or arg.startswith(("./", "../")):
            cand = local
        elif in_ws.is_dir() and in_ws != ws:
            cand = in_ws
        else:
            cand = local
        if cand.is_dir() and cand != ws:
            cand = P.find_project_root(cand) or cand
    cand = cand.resolve(strict=False)
    return cand if cand.is_dir() and cand != ws else None


def is_orkestra_word(word: str) -> bool:
    """Is this word the orkestra CLI (any path to it; on Windows also `orkestra.cmd` / `.exe` / `.ps1`)?"""
    name = (word or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(?:cmd|exe|bat|ps1)$", "", name, flags=re.I).lower() == "orkestra"


def _orkestra_argv(seg: sp.Segment) -> Optional[List[str]]:
    """The arguments of an `orkestra ...` call: also `python3 <path>/orkestra ...`."""
    if is_orkestra_word(seg.program):
        return list(seg.args)
    if INTERPRETER_RE.match(seg.program or ""):
        args = _skip_interpreter_flags(list(seg.args))
        if args and is_orkestra_word(args[0]):
            return args[1:]
    return None


# Interpreter options that take their value as the next word (`python3 -X utf8 <script>`); -c / -m / -e name code, not a script.
_INTERPRETER_VALUE_FLAGS = {"-X", "-W", "-r", "--require", "--import", "--loader", "--experimental-loader"}


def _skip_interpreter_flags(args: List[str]) -> List[str]:
    """`python3 -u -B <script> ...` -> `<script> ...`: the options before the script do not change what runs. Stops at
    the first word that is not an option, and returns [] for code given inline (-c / -m / -e / -)."""
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        flag = args[k]
        if flag in ("-c", "-m", "-e", "--eval", "--print", "-p"):
            return []
        k += 2 if flag in _INTERPRETER_VALUE_FLAGS else 1
    return args[k:]


def control_targets(segs: List[sp.Segment], cwd: str) -> List[Tuple[str, Path, str]]:
    """[(command, project, run id)] for every pause / abort / resume / unblock / block / await in the command line."""
    from . import state as ST
    out: List[Tuple[str, Path, str]] = []
    for seg, scwd in with_cwd(segs, cwd):
        argv = _orkestra_argv(seg)
        if argv is None:
            continue
        if not _mentions(argv, CONTROL_COMMANDS):      # cannot parse to a control command: skip the parser
            continue
        try:
            key = CP.inspect(argv)[0]
        except Exception:
            continue
        if key not in CONTROL_COMMANDS:
            continue
        proj_arg, run_arg = CP.global_values(argv)
        project = _cli_project(proj_arg, scwd)
        if project is None:
            continue
        run_id = run_arg or ST.project_active_run(project)
        if run_id:
            out.append((key, project, run_id))
    return out


def ownership_guard(event: Dict[str, Any], segs: List[sp.Segment], cfg: Dict[str, Any], grants: Iterable[str]) -> Decision:
    """Deny `orkestra pause|abort|resume|unblock|block|await` for a run another chat drives (see above)."""
    cmd = (event.get("tool_input") or {}).get("command") or ""
    if event.get("tool_name") != "Bash" or "orkestra" not in cmd:
        return ALLOW
    from . import state as ST
    granted = set(grants)
    lease = float(((cfg.get("watchdog") or {}).get("lease_seconds")) or 120)
    sid = ST._safe(str(event.get("session_id") or ""))
    subagent = bool(event.get("agent_type") or event.get("agent_id"))
    for key, project, run_id in control_targets(segs, event.get("cwd") or os.getcwd()):
        st = ST.read_json(ST.state_path(project, run_id))
        if not isinstance(st, dict) or st.get("status") in ST.TERMINAL_STATUSES:
            continue                                    # nothing to protect; the CLI reports what is wrong
        alive = [s for s in ST.run_sessions(project, run_id) if s["age"] < lease]
        if not alive or (not subagent and sid in {s["id"] for s in alive}):
            continue                                    # nobody drives it, or the caller is the driver
        if granted & set(CONTROL_COMMANDS[key]):
            continue                                    # the user asked for it in this very message
        from .engines import short_sid
        driver = alive[0]
        who = ("This call comes from a subagent, not from the chat that drives the run" if subagent
               else "This chat is not the driver")
        return deny("roles",
                    f"`orkestra {key}` would change run {run_id} ({project}), which session {short_sid(driver['id'])} is "
                    f"driving (heard from {driver['age']} s ago; a driver counts as alive for {int(lease)} s). {who}. Two "
                    f"chats steering one run is what caused pause/resume storms: only the user decides. Do not retry.\n"
                    f"unblock: tell the user; once their message asks for it (for example \"{CONTROL_EXAMPLE[key]}\") this "
                    f"call is allowed, or they run `orkestra {key} -p {project}` themselves.")
    return ALLOW


def unblock_line(event: Dict[str, Any], binding: Dict[str, Any]) -> str:
    """The last line of every run-guard denial: the way out, so that nobody is left without one. The chat that drives
    the run can pause or abort it; any other agent says so to the user, who decides."""
    project, run_id = binding.get("project"), binding.get("run_id")
    if event.get("agent_type") or event.get("agent_id"):
        return (f"unblock: this chat is not the driver of run {run_id} (project {project}); do not retry. Tell the user: "
                f"only they can decide, e.g. `orkestra pause -p {project}` or `orkestra abort \"<reason>\" -p {project}`.")
    return (f"unblock: if this run should not govern this chat, run `orkestra pause -p {project}` "
            f"(or `orkestra abort \"<reason>\" -p {project}` to end it).")


def _is_cursor_agent_scratch(path: str) -> bool:
    """Cursor's per-workspace agent transcripts (`~/.cursor/projects`). Not a product repo."""
    n = _norm_path_markers(path).rstrip("/").casefold()
    if n.endswith("/*"):
        n = n[:-2]
    root = str((Path.home() / ".cursor" / "projects").resolve(strict=False)).casefold()
    return n == root or n.startswith(root + "/")


def _classify(path: str, project: Path, feature_dir: Path) -> str:
    # compared case-insensitively: on a macOS volume `.ORKESTRA/runs/x/approval.json` IS the machine state
    path = str(path).casefold()
    project, feature_dir = Path(str(project).casefold()), Path(str(feature_dir).casefold())
    if _is_cursor_agent_scratch(path):
        return "temp"
    if path.endswith("/*"):
        base = Path(path[:-2])
        if is_relative_to(base, project / ".orkestra"):
            return "orkestra"
        if is_relative_to(base, project) or is_relative_to(project, base):
            return "source"
        return "temp" if _is_temp_subdir(str(base)) else "outside"
    p = Path(path)
    if is_relative_to(p, project / ".orkestra" / "runs"):
        return "runs"
    if is_relative_to(p, feature_dir):
        return "feature"
    if is_relative_to(p, project / ".orkestra"):
        return "orkestra"
    if is_relative_to(p, project):
        return "source"
    if _is_temp(path):
        return "temp"
    return "outside"


# Scripts that mention .orkestra/runs: reading the evidence / activity logs is normal work (reviewer, verifier);
# everything else there is machine state (state.json, approval.json, agents.json, gates-*.json, approved/, snapshot/)
# and keeps the strict 1.3.0 test — a forged approval.json must not depend on how cleverly `open(…, 'w')` is written.
RUN_MENTION_RE = re.compile(r"\.orkestra/runs[^\s'\"`)(,;]*", re.I)
RUN_READABLE_RE = re.compile(r"/evidence(?:/|$)|/activity[\w.]*\.jsonl$|/watchdog-resume-\d+\.log$", re.I)
# state, approval and the agent list are written only by the CLI. A script may read gates, snapshots and logs.
RUN_PRIVATE_RE = re.compile(r"/(?:state|approval|agents)\.json$|/ledger(?:\.|$)|\.lock$", re.I)
SCRIPT_STRICT_RE = re.compile(r"open\(|write|unlink|rename|replace|remove|truncate|shutil|copy|>")
SCRIPT_WRITE_RE = re.compile(
    r",\s*['\"](?:[wax]b?\+?|r\+b?|rb\+)['\"]|\bmode\s*=\s*['\"](?:[wax]|r\+)|\.open\(\s*['\"][wax]|os\.open\(|O_WRONLY|O_RDWR|"
    r"O_TRUNC|O_APPEND|O_CREAT|\.write(?:_text|_bytes)?\(|writeFile|appendFile|createWriteStream|copyFile|cpSync|"
    r"binwrite|\bunlink\b|\brename\b|os\.replace|os\.remove|shutil\.|rmtree|rmSync|\btruncate\b|json\.dump\(|"
    r"print\([^\n]*\bfile\s*=|['\"]\s*>{1,2}\s*['\"]|>\s*\S*\.orkestra/runs")


SCRIPT_PROGRAMS = ("python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh", "deno", "bun", "php")
MACHINE_STATE_RE = re.compile(r"state\.json|approval|agents\.json|gates-|approved|snapshot|ledger|\.lock|\.\./", re.I)


# The hook entry points decide what counts as "the user said so" (plan approval, per-message git permission, which
# session drives a run). They are started by the agent host — and by the headless drivers, for Cursor's print mode —
# never by a command the model runs: a model that pipes `{"prompt": "onay"}` into one, or calls the library behind
# them, would approve its own plan. Defence in depth, not a sandbox: only orkestra's own files and library count
# (another project's `dispatch.py` or `record_approval()` is none of our business).
INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|node|ruby|perl|bash|sh|zsh|dash|deno|bun|php)$")
HOOK_FILES = ("dispatch.py", "cursor_hook.py")
HOOK_CODE_RE = re.compile(r"\borkestra_core\b|hooks/(?:dispatch|cursor_hook)\.py|\bimport\s+cursor_hook\b")


# 2026-10-05, a real run: told "no need to ask questions", a model looked up how approval is recorded, copied the
# payload from the test suite into a `python3 <<'PY'` script, ran hooks/cursor_hook.py beforeSubmitPrompt with
# {"prompt": "onay"} and went on to implement. So, in a bound session, anything that NAMES orkestra's hook entry
# points or its library may only be read (grep, cat …): no command that executes or writes with those names in it,
# and no file whose content has them. (A project that is the orkestra checkout itself is exempt — it could not be
# developed otherwise.) Only orkestra's own names count; another project's dispatch.py is none of our business.
# Only what points at THIS plugin counts: its directory, an import of its library, its Cursor entry point, the
# variable Claude Code hands plugins. A project's own `hooks/dispatch.py`, or prose that merely says "orkestra_core",
# is ordinary work.
_PLUGIN_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INTERNALS_RE = re.compile(
    r"plugins/orkestra/(?:hooks|lib|bin)\b|\b(?:from|import)\s+orkestra_core\b|\borkestra_core\s*[./]\s*\w|"
    r"\bcursor_hook\.py\b|\bCLAUDE_PLUGIN_ROOT\b|orkestra[\w./-]{0,160}/hooks/dispatch\.py|"
    r"hooks[^\n\w]{2,12}dispatch\.py|plugins/cache/orkestra|"
    + re.escape(_PLUGIN_DIR) + r"(?![\w-])")
READONLY_PROGRAMS = {"grep", "rg", "egrep", "fgrep", "cat", "head", "tail", "less", "more", "ls", "wc", "stat", "file",
                     "diff", "nl", "sort", "uniq", "cut", "bat", "cd", "true", ":", "echo", "printf", "orkestra", "pwd",
                     "tree", "du", "which", "realpath", "basename", "dirname", "jq",
                     # inspection that names a path and changes nothing (`find` only without -exec/-delete/-fprint ...)
                     "readlink", "md5", "md5sum", "shasum", "sha1sum", "sha256sum", "cksum", "od", "hexdump", "strings",
                     "column", "tr", "rev", "fold", "paste", "comm", "expand", "df"}
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
# programs that run what they are given (a script on stdin, an inline program): in a command that names the internals
# they count as acting whatever their own arguments say (`cat <hook> | awk '{system($0)}'`)
RUNNER_PROGRAMS = {"eval", "source", ".", "exec", "osascript", "lua", "tclsh", "expect", "awk", "gawk", "mawk", "nawk"}
GIT_LOOKS = {"log", "show", "diff", "grep", "blame", "status", "ls-files", "cat-file", "shortlog", "describe"}
FORGERY_MSG = ("This names orkestra's own hook entry points or library. They are run by the agent host and the "
               "`orkestra` CLI only: a command or file of yours must not call, import or drive them — recording an "
               "approval or a permission that way is forging the user's consent (also when the user said not to ask "
               "questions). Read-only inspection is fine. Normal project commands in the plan (tests/builds) remain "
               "allowed; do not retry this action through another interpreter. For run state use `orkestra status --json`, "
               "`orkestra next --json` or `orkestra gate last`. If you need the user's approval: show the summary, run "
               "`orkestra await \"plan approval\"` (Turkish conversation: \"plan onayı\") and end your turn.")


def _is_orkestra_repo(project: Path) -> bool:
    return (project / "plugins" / "orkestra" / "hooks" / "dispatch.py").exists() or \
        (project / "hooks" / "dispatch.py").exists() and (project / "lib" / "orkestra_core").is_dir()


def _git_verb(args: List[str]) -> str:
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return ""


def _strip_shell_comments(text: str) -> str:
    """The command without its `# ...` comments (quote-aware: a `#` inside quotes is data, one in the middle of a word
    is no comment, and a heredoc never gets here). The shell never runs a comment, so a path named only in one is
    not a call (`python3 -c 'print(1)'  # looks at <plugin>/lib`)."""
    out: List[str] = []
    i, n, quote = 0, len(text), ""
    escaped_to = -1                 # index right after an escape pair: `x\ #` is one word, not a comment
    parens: List[bool] = []         # per open `(` outside quotes: was it `$(`, `<(` or `>(` (a substitution)?
    subst_close = -2                # index of the last `)` that closed a substitution: a `#` right after it is mid-word
    while i < n:
        ch = text[i]
        if not quote and ch == "(":
            parens.append(i > 0 and text[i - 1] in "$<>")
        elif not quote and ch == ")" and parens and parens.pop():
            subst_close = i
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            escaped_to = i
            continue
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and i != escaped_to and i - 1 != subst_close and (i == 0 or text[i - 1] in " \t\n;&|()"):
            j = text.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# programs that change files whatever their arguments show (`... | xargs sed -i s/a/b/` gets its files from a pipe)
WRITER_PROGRAMS = {"tee", "rm", "rmdir", "unlink", "shred", "touch", "truncate", "mkdir", "mv", "cp", "rsync", "install",
                   "ln", "ditto", "dd", "patch"}


def _writes(seg: sp.Segment) -> bool:
    prog, a = seg.program, seg.args
    if prog in WRITER_PROGRAMS or prog in FORMATTERS:
        return True
    if prog in ("sed", "gsed"):
        return any(x.startswith("--in-place") or re.match(r"^-[a-zA-Z]*i", x) for x in a)
    if prog == "perl":
        return any(re.match(r"^-\w*i", x) for x in a)
    if prog == "git" and a[:1] == ["apply"]:
        return True
    if prog in ("cargo", "go", "dart", "flutter"):
        return bool({"fmt", "format"} & set(a[:1]))
    return bool(bash_write_targets(seg, "/"))


def _looks_readonly(seg: sp.Segment) -> bool:
    prog = seg.program
    if prog in READONLY_PROGRAMS:
        return True
    if prog == "sed":
        return not any(a == "--in-place" or re.match(r"^-[a-zA-Z]*i", a) for a in seg.args)
    if prog == "git":
        return _git_verb(seg.args) in GIT_LOOKS
    if prog == "find":
        return not any(a in FIND_ACTIONS for a in seg.args)
    return False


# machine introspection that changes nothing in its plain forms (`sysctl -n hw.ncpu`, `date`, `uname -a`, `ps -o pid= -p 1`)
INTROSPECTION_PROGRAMS = {"date", "uname", "whoami", "id", "hostname", "sw_vers", "ps", "uptime", "nproc", "arch",
                          "sysctl", "getconf", "groups", "locale", "vm_stat", "lscpu"}


def _introspects(seg: sp.Segment) -> bool:
    """A machine-introspection program in a form that cannot change the system (no `sysctl -w` / `name=value`, no
    `date -s`)."""
    if seg.program not in INTROSPECTION_PROGRAMS:
        return False
    for a in seg.args:
        if seg.program == "sysctl" and (a in ("-w", "-f") or "=" in a or a.startswith("--write")):
            return False
        if seg.program == "date" and (a.startswith("-s") or a.startswith("--set") or re.fullmatch(r"\d{8,12}(\.\d\d)?", a)):
            return False
    return True


def _names_internals_and_acts(full: str, segs: List[sp.Segment]) -> bool:
    """The command text names orkestra's internals and does more than read.

    Reading (ls, grep, cat, find without -exec ...) of the plugin is fine, and so is plain machine introspection
    (`sysctl -n hw.ncpu`, `date`, `uname -a`) next to it. Acting is: a heredoc or a write (redirect, tee, cp, sed -i ...)
    anywhere in such a command, an interpreter or other runner anywhere in it (it could run what the rest names), and
    every other program, whatever its own arguments show: a dynamic program name, a command substitution, `curl -o`,
    `make` or `tar` could all be the one that changes the plugin."""
    if not INTERNALS_RE.search(full[:100000]):
        return False
    if "<<" in full:
        return True
    if not INTERNALS_RE.search(_strip_shell_comments(full)[:100000]):
        return False                                     # only a comment mentions them
    for seg in segs:
        if any(op.strip("&0123456789").startswith(">") and not t.startswith("/dev/") for op, t in seg.redirects):
            return True
        if INTERPRETER_RE.match(seg.program or "") or seg.program in RUNNER_PROGRAMS:
            return True
        if _writes(seg):
            return True
        if not (_looks_readonly(seg) or _introspects(seg)):
            return True
    return False


def _written_text(event: Dict[str, Any]) -> str:
    ti = event.get("tool_input") or {}
    parts = [ti.get(k) for k in ("content", "new_string", "new_source", "patch")]
    parts += [e.get("new_string") for e in (ti.get("edits") or []) if isinstance(e, dict)]
    return "\n".join(str(p) for p in parts if isinstance(p, str))[:200000]


def _is_orkestra_hook(arg: str, cwd: str) -> bool:
    if os.path.basename(arg) not in HOOK_FILES:
        return False
    try:
        p = resolve_path(arg, cwd)
        return p.parent.name == "hooks" and (p.parent.parent / "lib" / "orkestra_core").is_dir()
    except Exception:
        return False


def _hook_code(text: str) -> bool:
    return bool(HOOK_CODE_RE.search(text)) or ("hooks" in text and bool(re.search(r"\bimport\s+dispatch\b", text)))


def _calls_hook_entry(seg: sp.Segment, cwd: str, raw_program: str = "") -> bool:
    if seg.program in HOOK_FILES:                   # run directly: ./hooks/dispatch.py UserPromptSubmit
        first = (seg.raw or "").split()
        cand = next((w for w in first if os.path.basename(w) == seg.program), "")
        if cand and _is_orkestra_hook(cand, cwd):
            return True
    if not INTERPRETER_RE.match(seg.program or ""):
        return False
    args = list(seg.args)
    for i, a in enumerate(args):
        if a in ("-c", "-e", "--eval", "-m") and i + 1 < len(args) and _hook_code(args[i + 1]):
            return True
        if not a.startswith("-") and _is_orkestra_hook(a, cwd):
            return True
    return False


def _script_writes_run_state(raw: str) -> bool:
    """True when a script would change `.orkestra/runs`, or when it opens CLI-owned state.

    Evidence logs, gate files and snapshots may be opened and printed. `state.json`, `approval.json` and
    `agents.json` stay on the strict test, so `open()` and `Path.replace()` of those files are refused.
    A write mode, `.write()`, `json.dump`, unlink, or a path that climbs out with `..` still counts."""
    mentions = RUN_MENTION_RE.findall(raw)
    cleaned = re.sub(r"&?>&?\s*(?:/dev/null\b|\d\b)", " ", raw)
    if not mentions or any(".." in m or RUN_PRIVATE_RE.search(m) for m in mentions):
        return bool(SCRIPT_STRICT_RE.search(cleaned))
    if all(".orkestra/runs" in m.lower() for m in mentions):
        return bool(SCRIPT_WRITE_RE.search(cleaned))
    return bool(SCRIPT_STRICT_RE.search(cleaned))


def _switch_project_hint(event: Dict[str, Any], project: Path, run_id: str, path: str) -> str:
    """Isolation refusal that lands in ANOTHER project: say how this chat moves there, so it never answers "ask the user
    to stop the run in a terminal". The driver may pause its own run (ownership_guard lets it); a subagent reports."""
    try:
        other = find_project_root(path)
    except Exception:
        other = None
    other_dir = Path(str(other)).resolve(strict=False) if other is not None else None
    if other_dir is None or is_relative_to(other_dir, project) or is_relative_to(project, other_dir):
        return ""
    if event.get("agent_type") or event.get("agent_id"):
        return (f"\nThe target belongs to project {other}, not to run {run_id}: end with STATUS: BLOCKED and say so; "
                f"only the orchestrator chat may switch projects.")
    return (f"\nThe target belongs to another project ({other}). This chat is the driver of run {run_id}, so when the "
            f"user's request is about {Path(str(other)).name}, do not ask them to stop it in a terminal: run "
            f"`orkestra pause -p {project}` yourself (reversible with `orkestra resume -p {project}`), then continue with "
            f"`orkestra start -p {other} ...`.")


def run_guard(event: Dict[str, Any], cfg: Dict[str, Any], binding: Dict[str, Any], segs: List[sp.Segment],
              grants: Iterable[str] = ()) -> Decision:
    st = binding.get("state") or {}
    if st.get("status") not in ("active", "awaiting_user", "blocked", "paused"):
        return ALLOW
    guards = cfg.get("guards") or {}
    project = Path(binding["project"]).resolve(strict=False)
    run_id = binding["run_id"]
    fdir = project / ".orkestra" / "features" / run_id
    phase = st.get("phase", "intake")
    role = _agent_role(event)
    tool = event.get("tool_name")
    cwd = event.get("cwd") or str(project)
    spec_path = fdir / "spec.md"
    approved = bool(st.get("approved"))

    def check_target(path: str, via: str) -> Decision:
        kind = _classify(path, project, fdir)
        if kind == "runs":
            return deny("internals", f"{path} is orkestra machine state; only the `orkestra` CLI and hooks may write it.")
        if kind == "temp":
            return ALLOW
        if approved and str(path).casefold() == str(spec_path).casefold():
            return deny("internals", "spec.md is frozen after user approval. Record deviations in plan.md "
                                     "'Plan changelog' (Turkish plan: 'Plan değişiklik günlüğü') and, if scope changes, go back with "
                                     "`orkestra phase back plan` to get re-approval.")
        if role in ("readonly", "other"):
            if kind != "feature":
                return deny("roles", f"Agent {event.get('agent_type')} is read-only: it may only write report files under "
                                     f"{fdir}. Attempted ({via}): {path} — for a mutation probe or scratch copy, `cp` the "
                                     f"files to /tmp/<dir> and work there; never change or back up files inside the project.")
            return ALLOW
        if role == "implementer" and kind == "orkestra" and not str(path).casefold().startswith(str(fdir).casefold()):
            return deny("roles", "The implementer must not modify orkestra memory/config. Report instead.")
        if kind == "outside":
            if guards.get("isolation", True):
                return deny("isolation", f"Write outside the run's project is blocked (project isolation). "
                                         f"Project: {project} · attempted ({via}): {path}"
                                         f"{_switch_project_hint(event, project, run_id, path)}")
            return ALLOW
        if kind == "source" and phase in PRE_APPROVAL_PHASES and guards.get("phase", True):
            return deny("phase", f"Source changes are blocked in phase '{phase}': the plan is not approved yet. "
                                 f"Only .orkestra/ artifacts may be written now. Attempted ({via}): {path}")
        return ALLOW

    own_repo = _is_orkestra_repo(project)
    if tool in WRITE_TOOLS:
        for p in _tool_paths(event):
            d = check_target(str(resolve_path(p, cwd)), tool)
            if d.denied:
                return d
        if not own_repo and INTERNALS_RE.search(_written_text(event)):
            return deny("internals", FORGERY_MSG)
        return ALLOW

    if tool != "Bash":
        return ALLOW

    if not own_repo and _names_internals_and_acts((event.get("tool_input") or {}).get("command") or "", segs):
        return deny("internals", FORGERY_MSG)
    full_cmd = (event.get("tool_input") or {}).get("command") or ""
    for seg, scwd in with_cwd(segs, cwd):
        recursive_targets = _recursive_rm_targets(seg.program, seg.args)
        if recursive_targets is not None:
            if not recursive_targets:
                return deny("destructive", "Recursive deletion with unknown targets is blocked; remove only exact planned files.")
            for target in recursive_targets:
                # A variable assigned to one literal temp directory in this same command is that directory.
                # Anything else keeps the unexpanded target, so an unknown variable stays a deny.
                prefixed = _prefix_temp_target(target, full_cmd)
                expanded = prefixed if prefixed is not None else expand_literal_vars(target, full_cmd)
                if expanded is not None and _classify(str(resolve_path(expanded, scwd)), project, fdir) == "temp":
                    continue
                resolved = str(resolve_path(target, scwd))
                d = check_target(resolved, "bash:rm")
                if d.denied:
                    return d
                if _classify(resolved, project, fdir) != "temp":
                    return deny("destructive", "Recursive deletion is blocked outside a dedicated temporary subdirectory. "
                                                   "Remove exact planned files instead.")
        if role is None and seg.program == "orkestra":
            continue                                # the orchestrator may run every subcommand: no parse needed
        sub = _cli_subcommand(seg)
        if sub is not None:
            if sub == "":
                if role is not None:                # the CLI would print a usage error; say why instead
                    return deny("roles", f"this `orkestra` command line does not parse ({_cli_info(seg)[2][:160]}). Write "
                                         f"subcommands and options in full, exactly as documented (no abbreviations).")
                continue
            if role == "implementer" and sub not in CLI_ALLOWED["implementer"]:
                return deny("roles", f"`orkestra {sub.replace(':', ' ')}` is reserved for the orchestrator. "
                                     f"Implementer may use: brief, tdd red/green, task start/note, gate run, changes.")
            if role in ("readonly", "other") and sub not in CLI_ALLOWED["readonly"] \
                    and not (event.get("agent_type") == "orkestra:verifier" and sub == "gate:verify"):
                return deny("roles", f"`orkestra {sub.replace(':', ' ')}` is not allowed for read-only agents.")
            continue
        for raw_t in raw_write_targets(seg):
            if not (_VAR_PATH_RE.search(raw_t) and guards.get("isolation", True)) or is_temp_var_path(raw_t, full_cmd):
                continue
            if _for_loop_stays_in_project(raw_t, full_cmd):
                continue
            prefixed = _prefix_temp_target(raw_t, full_cmd)
            if prefixed is not None and _classify(str(resolve_path(prefixed, scwd)), project, fdir) == "temp":
                continue
            expanded = expand_literal_vars(raw_t, full_cmd)
            if expanded is None:
                return deny("isolation", f"`{raw_t}` is a write target with an unexpanded variable or backtick: the "
                                         f"hook checks the path before the shell expands it, so it cannot tell where "
                                         f"this writes. Write the literal path (inside {project}), or assign the "
                                         f"variable to a literal earlier in the same command.")
            d = check_target(str(resolve_path(expanded, scwd)), f"bash:{seg.program}")
            if d.denied:
                return d
        for t in bash_write_targets(seg, scwd):
            d = check_target(t, f"bash:{seg.program}")
            if d.denied:
                return d
        if not own_repo and _calls_hook_entry(seg, scwd):
            return deny("internals", FORGERY_MSG)
        raw = seg.raw
        if seg.program in SCRIPT_PROGRAMS and "/.orkestra/runs" in (_norm_path_markers(scwd).casefold() + "/"):
            # relative paths ARE run state here: strict as soon as a machine-state name appears, else the write test
            clean = re.sub(r"&?>&?\s*(?:/dev/null\b|\d\b)", " ", raw)
            if (SCRIPT_STRICT_RE if MACHINE_STATE_RE.search(raw) else SCRIPT_WRITE_RE).search(clean):
                return deny("internals", "Scripts must not modify .orkestra/runs (CLI-only machine state); this one runs "
                                         "inside it. Work from the project root and read through the `orkestra` CLI.")
        if ".orkestra/runs" in raw.lower() and seg.program in SCRIPT_PROGRAMS:
            if _script_writes_run_state(raw):
                return deny("internals", "Scripts must not modify .orkestra/runs (CLI-only machine state). Reading the "
                                         "evidence and activity logs there is fine; for state use `orkestra status --json`, "
                                         "`orkestra next --json` or `orkestra gate last`.")
    # a script fed through a heredoc (`python3 - <<'EOF' … EOF`): its body is not part of any segment
    full = (event.get("tool_input") or {}).get("command") or ""
    if "<<" in full and ".orkestra/runs" in full.lower() and any(s.program in SCRIPT_PROGRAMS for s in segs) \
            and _script_writes_run_state(full[:20000].replace("<<", " ")):
        return deny("internals", "Scripts must not modify .orkestra/runs (CLI-only machine state). Reading the evidence "
                                 "and activity logs there is fine; for state use `orkestra status --json`, "
                                 "`orkestra next --json` or `orkestra gate last`.")
    if not own_repo and "<<" in full and _hook_code(full[:20000]) and any(INTERPRETER_RE.match(s.program or "") for s in segs):
        return deny("internals", FORGERY_MSG)
    if guards.get("wait", True):
        d = wait_guard(event, binding, segs)
        if d.denied:
            return d
    if guards.get("dialogs", True):
        d = dialog_guard(segs)
        if d.denied:
            return d
    if guards.get("deploy", True):
        d = deploy_guard(segs, grants)
        if d.denied:
            return d
    if guards.get("deps", True):
        approved_deps = (st.get("approved") or {}).get("deps") or []
        d = deps_guard(segs, approved_deps)
        if d.denied:
            return d
    return ALLOW


# ====================================================================== entry

def expand_cli_commands(segs: List[sp.Segment]) -> List[sp.Segment]:
    """`orkestra tdd|evidence ... -- <cmd>` runs <cmd>: analyze it like any other command (no guard bypass)."""
    import shlex as _shlex
    out = list(segs)
    todo = [(seg, 0) for seg in segs if seg.program == "orkestra"]
    while todo:
        seg, depth = todo.pop()
        # exactly the list the CLI hands to the shell (only `tdd` / `evidence` run one: no parse for the other commands)
        rest = _cli_info(seg)[1] if _mentions(seg.args, _RUNS_A_COMMAND) else []
        if not rest:
            continue
        if depth >= MAX_CLI_NESTING:
            out.append(sp.Segment(program=TOO_DEEP))
            continue
        inner = sp.segments(_shlex.join(rest) if len(rest) > 1 else rest[0])
        out.extend(inner)
        todo.extend((s, depth + 1) for s in inner if s.program == "orkestra")   # `evidence a orkestra evidence b …`
    return out


TOO_DEEP = "<orkestra-nested-too-deep>"
MAX_REDIRECTS = 200
MAX_SEGMENTS = 2000


MCP_WRITE_RE = re.compile(r"^mcp__.+__(replace_|insert_|rename_|delete_|safe_delete_|create_|write_|edit_|move_|remove_)")
MCP_SHELL_RE = re.compile(r"^mcp__.+__(execute_shell_command|run_shell|shell_exec|execute_command|run_command)$")


def normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Map MCP editing/shell tools (e.g. Serena's replace_symbol_body) onto Write/Bash semantics.
    Claude Code's PowerShell tool (Windows) carries the same `command` input as Bash and goes through the Bash guards
    (the shell parser is POSIX-shaped: git/rm style commands are caught, PowerShell-only cmdlets are not)."""
    tool = event.get("tool_name") or ""
    if tool == "PowerShell":
        ev = dict(event)
        ev["tool_name"] = "Bash"
        return ev
    if not tool.startswith("mcp__"):
        return event
    ti = event.get("tool_input") or {}
    if MCP_SHELL_RE.match(tool):
        ev = dict(event)
        ev["tool_name"] = "Bash"
        ev["tool_input"] = {"command": str(ti.get("command") or ti.get("cmd") or "")}
        if ti.get("cwd"):
            ev["cwd"] = str(resolve_path(str(ti["cwd"]), event.get("cwd")))
        return ev
    if MCP_WRITE_RE.match(tool):
        path = ti.get("relative_path") or ti.get("file_path") or ti.get("path") or ti.get("name_path_file") or ""
        ev = dict(event)
        ev["tool_name"] = "Write"
        ev["tool_input"] = {"file_path": str(path) if path else str(event.get("cwd") or ".")}
        return ev
    return event


def _has_key(obj: Any, names: Tuple[str, ...], depth: int = 0) -> bool:
    """A non-empty value under one of `names`, at any depth (the UI may echo the input back in its response)."""
    if depth > 6:
        return False
    if isinstance(obj, dict):
        return any((k in names and bool(v)) or _has_key(v, names, depth + 1) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_key(v, names, depth + 1) for v in obj[:50])
    return False


def pre_tool_decision(event: Dict[str, Any], cfg: Dict[str, Any], binding: Optional[Dict[str, Any]],
                      grants: Iterable[str]) -> Decision:
    event = normalize_event(event)
    grants = list(grants)
    tool = event.get("tool_name") or ""
    guards = cfg.get("guards") or {}
    segs: List[sp.Segment] = []
    sp.TEST_SYNTAX = bool(guards.get("test_syntax", False))     # one hook process = one decision (see shellparse)
    if tool == "Bash":
        cmd = (event.get("tool_input") or {}).get("command") or ""
        segs = expand_cli_commands(sp.segments(cmd))
        # every redirect is resolved and checked by several guards: keep the hook far away from its timeout (a hook
        # that times out lets the command run unchecked)
        if len({t for s in segs for _, t in s.redirects}) > MAX_REDIRECTS or len(segs) > MAX_SEGMENTS:
            return deny("complex", f"This command is too large to check in one call (more than {MAX_REDIRECTS} different "
                                   f"redirect targets or {MAX_SEGMENTS} commands). Split it, or put it in a script file "
                                   f"under the project and run that.")
        if any(seg.program == sp.NESTED_TOO_DEEP for seg in segs):
            return deny("complex", f"This command nests shells, `eval` or `$(...)` more than {sp.MAX_DEPTH} levels deep, "
                                   f"which cannot be checked. Run the inner command directly, or put it in a script "
                                   f"file under the project and run that.")
    if guards.get("secrets", True):
        d = secrets_guard(event, cfg, segs)
        if d.denied:
            return d
    d = home_guard(event, segs)
    if d.denied:
        return d
    if tool == "Bash":
        if any(seg.program == TOO_DEEP for seg in segs):
            return deny("internals", "`orkestra tdd|evidence` wrapped inside itself more than "
                                     f"{MAX_CLI_NESTING} times cannot be analysed. Run the inner command directly.")
        for seg in segs:                            # nested calls are in `segs` too (expand_cli_commands)
            if "rollback" in seg.args and "--yes" in seg.args and _cli_subcommand(seg) == "rollback" \
                    and "discard" not in set(grants):
                return deny("rollback", "`orkestra rollback --yes` reverts files and needs the user's explicit request "
                                        "in the current message (/orkestra:rollback, 'discard the changes' or "
                                        "'değişiklikleri geri al'). "
                                        "Show the dry run instead.")
        try:
            d = ownership_guard(event, segs, cfg, grants)
        except Exception:                           # an unusable control call must not skip the guards behind it (the hook fails open)
            d = ALLOW
        if d.denied:
            return d
        if guards.get("git", True):
            ev = dict(event, _bound=bool(binding))
            d = git_guard(ev, segs, set(grants))
            if d.denied:
                return d
        if guards.get("store", True):
            d = store_guard(segs)
            if d.denied:
                return d
    if binding and tool == "AskUserQuestion":
        # The UI fills `answers` AFTER PreToolUse; answers present here would be model-authored (forged approval).
        if _has_key(event.get("tool_input"), ("answers", "answer")):
            return deny("internals", "Do not pre-fill `answers` in AskUserQuestion — only the user answers questions.")
        return ALLOW
    if binding:
        d = run_guard(event, cfg, binding, segs, grants)
        if d.denied:
            return Decision(d.action, d.reason + "\n" + unblock_line(event, binding), d.guard)   # always a way out
    return ALLOW
