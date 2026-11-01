"""Risk classifier for ONE recorded agent action (the dashboard shows risky and dangerous actions).

`classify(tool, what, project, home)` is pure and deterministic: it returns None or
`{"level": "high"|"medium", "code": <code>, "label": <localized short label>}`. It never raises.

Bash commands are read with the shell-aware parser (`shellparse.segments`), so a keyword inside quoted text or as an
argument (`grep -r "rm -rf" .`, `echo "git push --force"`) is not a hit. A cheap compiled prefilter runs first and the
parser is only called when a keyword could match. The decision (level, code) is memoized; the label is looked up per
call so a language switch needs no cache clearing.

This is a heuristic for a human-facing overview, not a guard: it never blocks anything.
"""
from __future__ import annotations

import functools
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from . import guards, i18n, shellparse
from .shellparse import Segment, git_call, positional, short_flags

LEVELS = ("high", "medium")

# Stable order: ties between equal levels go to the code that comes first here.
_HIGH_BASH = ("rm_outside", "git_force_push", "git_push_delete", "git_discard", "git_history", "sql_destructive",
              "db_reset", "pipe_shell", "sudo", "disk_wipe", "infra_destroy", "deploy", "secrets_read")
_MEDIUM_BASH = ("rm_recursive", "git_push", "git_commit", "db_migrate", "network_send", "remote_shell",
                "kill_process", "install_package", "system_change", "chmod_wide")
LEVEL_OF: Dict[str, str] = {}
CODES: Tuple[str, ...] = (
    "rm_outside", "rm_recursive", "git_force_push", "git_push_delete", "git_push", "git_discard", "git_history",
    "git_commit", "sql_destructive", "db_reset", "db_migrate", "pipe_shell", "sudo", "disk_wipe", "infra_destroy",
    "deploy", "secrets_read", "network_send", "remote_shell", "kill_process", "install_package", "system_change",
    "chmod_wide", "secret_edit", "git_internal", "ci_edit", "outside_write", "mcp_destructive", "mcp_external",
    "mcp_sql", "mcp_write",
)
for _c in CODES:
    LEVEL_OF[_c] = "high" if _c in _HIGH_BASH or _c in ("secret_edit", "git_internal", "mcp_destructive",
                                                       "mcp_external") else "medium"
del _c
_RANK = {c: i for i, c in enumerate(CODES)}

MAX_LEN = 4000                      # activity rows are cut near 480 chars; anything longer is garbage or an attack

# ---------------------------------------------------------------- prefilter

_PREFILTER = re.compile(
    r"\b(?:rm|git|sudo|doas|su|dd|mkfs\S*|diskutil|fdisk|shred|terraform|tofu|pulumi|kubectl|helm|aws|gcloud|az|docker|"
    r"docker-compose|wrangler|vercel|vc|netlify|firebase|fly|flyctl|heroku|serverless|sls|cdk|eb|gh|npm|yarn|pnpm|bun|"
    r"bunx|npx|pnpx|twine|cargo|gem|fastlane|xcrun|altool|eas|psql|mysql|mariadb|sqlite3|duckdb|sqlcmd|clickhouse\S*|"
    r"cockroach|supabase|prisma|turso|mongosh|mongo|dropdb|redis-cli|rails|rake|alembic|knex|sequelize\S*|drizzle-kit|"
    r"curl|wget|bash|sh|zsh|python3?|node|perl|ruby|cat|less|more|head|tail|bat|batcat|grep|egrep|fgrep|rg|strings|"
    r"base64|xxd|od|hexdump|nl|tac|sed|awk|cut|sort|jq|yq|openssl|gpg|zip|tar|cp|mv|scp|rsync|sftp|ssh|nc|ncat|netcat|"
    r"printenv|env|security|kill|pkill|killall|launchctl|pip3?|uv|pipx|brew|apt|apt-get|yum|dnf|go|crontab|systemctl|"
    r"defaults|sysctl|networksetup|scutil|nvram|chmod|chown|tee|install|ln)\b|/dev/|/etc/|<\(|\$\(|`",
    re.I)

_TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")

# ---------------------------------------------------------------- path helpers


def _expand(path: str, home: str) -> str:
    if path == "~" or path.startswith("~/"):
        return (home.rstrip("/") + path[1:]) if home else path
    return path


def _under(path: str, base: str) -> bool:
    if not base:
        return False
    b = base.rstrip("/")
    return path == b or path.startswith(b + "/")


def _is_temp(path: str) -> bool:
    if path.startswith(_TEMP_PREFIXES):
        return True
    tmp = os.environ.get("TMPDIR", "").rstrip("/")
    return bool(tmp) and path.startswith(tmp + "/")


# A path can only match a secret glob if it contains that glob's longest literal run: a cheap test that keeps the
# fnmatch loop (~40 globs, twice per path) away from the thousands of ordinary arguments a dashboard poll classifies.
_SECRET_HINTS = tuple(sorted({max(re.split(r"[*?]", g.lower()), key=len) for g in guards.SECRET_GLOBS} - {""}))


@functools.lru_cache(maxsize=16384)
def _is_secret(path: str, home: str) -> bool:
    if not path:
        return False
    p = _expand(path, home)
    low_p = p.lower()
    if any(h in low_p for h in _SECRET_HINTS) and guards.is_secret_path(p):
        return True
    low = p.lower()
    if low.endswith(".pub"):
        return False
    return "/.ssh/" in low or low.startswith(".ssh/") or "/.aws/" in low or low.startswith(".aws/")


def _is_secret_ref(arg: str, home: str) -> bool:
    """An argument that is, or carries after `=` or `@`, a secret path (`--file=.env`, `-F f=@.env`)."""
    if _is_secret(arg, home):
        return True
    for sep in ("=", "@"):
        if sep in arg and _is_secret(arg.rsplit(sep, 1)[-1], home):
            return True
    return False


# ---------------------------------------------------------------- bash: small helpers

_WRAPPERS = {"time", "env", "nohup", "command", "builtin", "exec", "nice", "timeout", "gtimeout", "if", "then", "do",
             "while", "until", "else", "elif", "!", "{"}
_RUNNERS = {"npm", "pnpm", "yarn", "bun", "bunx", "npx", "pnpx", "corepack"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _tool(seg: Segment, *names: str) -> Optional[List[str]]:
    """Arguments after tool `names` when the segment runs it directly or through a package runner."""
    if seg.program in names:
        return seg.args
    if seg.program in _RUNNERS:
        for i, a in enumerate(seg.args):
            if a in names and (i == 0 or seg.args[i - 1] in ("exec", "dlx", "x", "run")):
                return seg.args[i + 1:]
    return None


def _has_sudo(seg: Segment) -> bool:
    for tok in seg.raw.split()[:10]:
        b = os.path.basename(tok)
        if b in ("sudo", "doas"):
            return True
        if b in _WRAPPERS or _ASSIGN.match(tok) or tok.startswith("-") or tok.isdigit():
            continue
        return False
    return False


def _rm_codes(seg: Segment, project: str, home: str) -> List[str]:
    a = seg.args
    sf = short_flags(a)
    if not ("r" in sf or "R" in sf or "--recursive" in a):
        return []
    proj = project.rstrip("/")
    targets, scratch = positional(a), 0           # scratch: targets inside a temp dir (never the temp dir itself)
    for t in targets:
        if t in ("/", "~", "*", ".*") or "$" in t or "`" in t or "__SUBST__" in t:
            return ["rm_outside"]
        if t.startswith("~"):
            full = os.path.normpath(_expand(t, home)) if home and t.startswith("~/") else ""
            if not full or full == proj or not _under(full, proj):
                return ["rm_outside"]
            continue
        if t == ".." or t.startswith("../") or "/../" in t or t.endswith("/.."):
            return ["rm_outside"]
        if t.startswith("/"):
            full = os.path.normpath(t)
            if _under(full, proj):
                continue
            if _is_temp(full + "/") and full not in ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders"):
                scratch += 1
                continue
            return ["rm_outside"]
    # clearing a scratch folder under /tmp is housekeeping, not a risk; anything else in the list keeps it medium
    return [] if targets and scratch == len(targets) else ["rm_recursive"]


def _git_codes(seg: Segment) -> List[str]:
    gc = git_call(seg)
    if gc is None:
        return []
    sub, a = gc.sub, gc.args
    sf, pos = short_flags(a), positional(a)
    out: List[str] = []
    if sub == "push":
        if "--dry-run" in a or "n" in sf:
            return []                       # a dry run changes nothing
        force = ("f" in sf or any(x in ("--force", "--force-if-includes") or x.startswith("--force-with-lease")
                                  for x in a) or any(p.startswith("+") and len(p) > 1 for p in pos))
        delete = "--delete" in a or "d" in sf or any(p.startswith(":") and len(p) > 1 for p in pos)
        if force:
            out.append("git_force_push")
        if delete:
            out.append("git_push_delete")
        if not force and not delete:
            out.append("git_push")
    elif sub == "reset":
        if "--hard" in a:
            out.append("git_discard")
    elif sub == "clean":
        if ("f" in sf or "--force" in a) and "n" not in sf and "--dry-run" not in a:
            out.append("git_discard")
    elif sub == "checkout":
        after = a[a.index("--") + 1:] if "--" in a else []
        if "." in after or len(after) >= 2 or pos == ["."]:
            out.append("git_discard")
    elif sub == "restore":
        staged = "--staged" in a or "S" in sf
        worktree = "--worktree" in a or "W" in sf
        paths = positional(a, ("-s", "--source"))
        if not (staged and not worktree) and ("." in paths or "*" in paths or len(paths) >= 2):
            out.append("git_discard")
    elif sub == "stash":
        if pos[:1] in (["drop"], ["clear"]):
            out.append("git_discard")
    elif sub == "branch":
        if "D" in sf or ("--delete" in a and ("--force" in a or "f" in sf)) or ("d" in sf and "f" in sf):
            out.append("git_discard")
    elif sub in ("filter-branch", "filter-repo", "update-ref", "rebase"):
        out.append("git_history")
    elif sub == "reflog":
        if "expire" in a or "delete" in a:
            out.append("git_history")
    elif sub == "gc":
        if any(x.startswith("--prune") for x in a):
            out.append("git_history")
    elif sub == "commit":
        out.append("git_history" if "--amend" in a else "git_commit")
    elif sub == "merge":
        out.append("git_commit")
    elif sub == "tag":
        if "d" in sf or "--delete" in a:
            out.append("git_commit")
    return out


# ---- SQL

_DB_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "duckdb", "sqlcmd", "clickhouse", "clickhouse-client",
               "cockroach", "supabase", "prisma", "turso", "mongosh", "mongo"}
_SQL_DESTRUCTIVE = re.compile(
    r"\bdrop\s+(?:table|database|schema|view|index|materialized\s+view)\b|\btruncate\s+(?:table\s+)?[\w\"`\[]"
    r"|\balter\s+table\b[^;]*\bdrop\b|\bdrop\s+column\b", re.I)
_DELETE_FROM = re.compile(r"\bdelete\s+from\b", re.I)
_WHERE = re.compile(r"\bwhere\b", re.I)
_MONGO_DESTRUCTIVE = re.compile(r"dropDatabase\s*\(|\.drop\s*\(", re.I)


def _sql_destructive(text: str) -> bool:
    if _SQL_DESTRUCTIVE.search(text) or _MONGO_DESTRUCTIVE.search(text):
        return True
    for stmt in text.split(";"):
        if _DELETE_FROM.search(stmt) and not _WHERE.search(stmt):
            return True
    return False


def _is_db_client(seg: Segment) -> bool:
    if seg.program in _DB_CLIENTS:
        return True
    a = _tool(seg, "wrangler")
    return a is not None and "d1" in a and "execute" in a


# ---- network

_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/@?#]*@)?(\[[^\]]+\]|[^/:?#]+)")
_BARE_HOST = re.compile(r"^(?:\[[0-9a-fA-F:]+\]|localhost|[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,}|\d+\.\d+\.\d+\.\d+)(?::\d+)?(?:/.*)?$")
_LOCAL_HOST = re.compile(r"^(?:localhost|127(?:\.\d+){3}|\[?::1\]?|0\.0\.0\.0|\[::\]|.*\.localhost)$", re.I)


def _hosts(args: List[str]) -> List[str]:
    urls = [m.group(1) for m in (_URL.match(x) for x in args) if m]
    if urls:
        return urls
    out = []
    for x in args:
        if x.startswith("-") or not _BARE_HOST.match(x):
            continue
        h = re.match(r"^(\[[^\]]+\]|[^/:]+)", x)
        if h:
            out.append(h.group(1))
    return out


_CURL_DATA_SHORT = re.compile(r"^-[A-Za-z]*[dFT]$")
_CURL_METHOD = re.compile(r"^-X(?:POST|PUT|PATCH|DELETE)$", re.I)
_WGET_DATA = ("--post-data", "--post-file", "--body-data", "--body-file", "--method=POST", "--method=PUT",
              "--method=PATCH", "--method=DELETE")


def _sends_data(seg: Segment) -> bool:
    a = seg.args
    if seg.program == "curl":
        for i, x in enumerate(a):
            if x.startswith(("--data", "--form", "--upload-file", "--json")) or _CURL_DATA_SHORT.match(x) \
                    or _CURL_METHOD.match(x):
                return True
            if x in ("-X", "--request") and i + 1 < len(a) and a[i + 1].upper() in ("POST", "PUT", "PATCH", "DELETE"):
                return True
            if x.upper().startswith("--REQUEST=") and x.split("=", 1)[1].upper() in ("POST", "PUT", "PATCH", "DELETE"):
                return True
        return False
    return any(x.startswith(_WGET_DATA) for x in a)


_REMOTE_OPERAND = re.compile(r"^(?:[\w.\-]+@)?[\w.\-]+:(?!//)")
_SSH_WITH_VALUE = ("-i", "-p", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-e", "-I", "-m", "-O",
                   "-Q", "-S", "-W", "-w")

# ---- readers / system

_READERS = {"cat", "less", "more", "head", "tail", "bat", "batcat", "grep", "egrep", "fgrep", "rg", "strings", "base64",
            "xxd", "od", "hexdump", "nl", "tac", "awk", "gawk", "sed", "gsed", "cut", "sort", "jq", "yq", "openssl",
            "gpg", "zip", "tar", "cp", "scp", "rsync", "curl", "wget", "diff", "pbcopy", "source", "."}
_MODE_WIDE = re.compile(r"^(?:0?777|[ugoa]*[+=]rwx)$")
_SIGKILL = {"-9", "-KILL", "-SIGKILL"}
_PIPE_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "python", "python3", "node", "perl", "ruby"}
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_BARE_ENV = re.compile(r"(?:^|[;&|(])\s*(?:sudo\s+)?env\s*(?:$|[;&|)>])")
_PIPE_SHELL = re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:\S*/)?(?:sh|bash|zsh|dash|ksh)\b")
_PIPE_INTERP = re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:\S*/)?(?:python3?|node|perl|ruby)\b")
_SUBST_FETCH = re.compile(r"(?:\b(?:bash|sh|zsh|dash|ksh)\s+<\(|\$\(|`)\s*(?:curl|wget)\b")
# Product hosts this workspace publishes. A HEAD or body check of these is not "run the download".
_OWN_SITE = re.compile(
    r"(?:^|\.)(?:puzzleo\.lol|namazvakitleri\.co|zaynews\.com|estimationline\.com|retroep\.com|"
    r"edevrun\.com|kolaytacir\.com|kocabayticaret\.com|firatkocabay\.workers\.dev)$", re.I)
_INLINE_EVAL = ("-c", "-e", "--eval")
_DISK_REDIRECT = re.compile(r"^/dev/(?:r?disk|sd|nvme|hd|vd)")


def _deploy(seg: Segment) -> bool:
    p = seg.program
    a = seg.args
    pos = positional(a)
    t = _tool(seg, "wrangler")
    if t is not None:
        tp = positional(t)
        return any(w in ("deploy", "publish") for w in tp[:2]) or tp[:2] in (["secret", "put"], ["secret", "bulk"])
    t = _tool(seg, "vercel", "vc")
    if t is not None:
        return (positional(t)[:1] == ["deploy"]) or "--prod" in t or "--production" in t
    t = _tool(seg, "netlify")
    if t is not None:
        return positional(t)[:1] == ["deploy"] and any(x.startswith("--prod") for x in t)
    for names, subs in ((("firebase",), ("deploy",)), (("fly", "flyctl"), ("deploy",)),
                        (("serverless", "sls"), ("deploy",)), (("cdk",), ("deploy",)), (("eb",), ("deploy",)),
                        (("eas",), ("build", "submit", "update")), (("helm",), ("install", "upgrade")),
                        (("kubectl",), ("apply", "rollout")), (("terraform", "tofu"), ("apply",)),
                        (("pulumi",), ("up",))):
        t = _tool(seg, *names)
        if t is not None:
            tp = positional(t)
            return bool(tp) and tp[0] in subs
    t = _tool(seg, "heroku")
    if t is not None:
        tp = positional(t)
        return bool(tp) and (tp[0].startswith("releases") or tp[0] in ("ps:scale", "config:set", "config:unset"))
    if p == "gcloud":
        return "deploy" in pos
    if p == "docker":
        return pos[:1] == ["push"] or pos[:2] == ["image", "push"]
    if p == "gh":
        return pos[:2] == ["release", "create"]
    if p in ("npm", "yarn", "pnpm", "bun"):
        return "publish" in pos[:2]
    if p == "twine":
        return pos[:1] == ["upload"]
    if p == "cargo":
        return pos[:1] == ["publish"]
    if p == "gem":
        return pos[:1] == ["push"]
    if p in ("fastlane", "altool"):
        return True
    if p == "xcrun":
        return pos[:1] == ["altool"] or (pos[:1] == ["notarytool"] and "submit" in pos)
    return False


def _infra_destroy(seg: Segment) -> bool:
    p, a = seg.program, seg.args
    pos = positional(a)
    if p in ("terraform", "tofu", "pulumi"):
        return pos[:1] == ["destroy"]
    if p == "kubectl":
        return pos[:1] == ["delete"]
    if p == "helm":
        return pos[:1] in (["uninstall"], ["delete"])
    if p == "aws":
        return any(w.startswith(("delete", "terminate")) for w in pos[:3]) or \
            (pos[1:2] == ["rm"] and "--recursive" in a) or pos[1:2] == ["rb"]
    if p in ("gcloud", "az"):
        return "delete" in pos
    if p in ("docker", "docker-compose"):
        sf = short_flags(a)
        if pos[:1] == ["compose"]:
            pos = pos[1:]
        if pos[:2] == ["system", "prune"] or pos[:2] in (["volume", "rm"], ["volume", "prune"]):
            return True
        if pos[:1] == ["rm"] and ("f" in sf or "--force" in a):
            return True
        if pos[:1] == ["down"] and ("v" in sf or "--volumes" in a):
            return True
    return False


def _own_or_local(host: str) -> bool:
    raw = (host or "").strip().strip("[]")
    if _LOCAL_HOST.match(host or "") or _LOCAL_HOST.match(raw):
        return True
    return bool(_OWN_SITE.search(raw.lower()))


def _own_site_inline_check(segs: List[Segment]) -> bool:
    """True when every curl/wget host is one of our sites or loopback, and every interpreter is an inline
    check (`python3 -c`, `node -e`). `curl | sh` and `curl | python3` (the body is the program) stay dangerous."""
    fetches = [s for s in segs if s.program in ("curl", "wget")]
    interps = [s for s in segs if s.program in ("python", "python3", "node", "perl", "ruby")]
    if not fetches or not interps:
        return False
    hosts = [h for s in fetches for h in _hosts(s.args)]
    if not hosts or any(not _own_or_local(h) for h in hosts):
        return False
    return all(any(a == flag or a.startswith(flag) for flag in _INLINE_EVAL for a in s.args) for s in interps)


def _bash(cmd: str, project: str, home: str) -> Set[str]:
    found: Set[str] = set()
    segs = shellparse.segments(cmd)
    programs = {s.program for s in segs}
    has_fetch = bool(programs & {"curl", "wget"})
    stripped = _QUOTED.sub('""', cmd)

    if has_fetch and (_PIPE_SHELL.search(stripped)
                      or (_SUBST_FETCH.search(cmd) and programs & (_PIPE_SHELLS | {"__SUBST__"}))
                      or (_PIPE_INTERP.search(stripped) and not _own_site_inline_check(segs))):
        found.add("pipe_shell")
    if _BARE_ENV.search(stripped):
        found.add("secrets_read")

    db_segs = [s for s in segs if _is_db_client(s)]
    if db_segs:
        text = " ".join(" ".join(s.args) for s in db_segs)
        if "|" in cmd or "<" in cmd:
            text += " " + " ".join(" ".join(s.args) for s in segs)
        if _sql_destructive(text):
            found.add("sql_destructive")

    for seg in segs:
        p, a = seg.program, seg.args
        pos = positional(a)
        sf = short_flags(a)
        if _has_sudo(seg) or (p == "su" and "c" in sf):
            found.add("sudo")
        if p == "rm":
            found.update(_rm_codes(seg, project, home))
        elif p == "git":
            found.update(_git_codes(seg))
        elif p == "dd":
            if any(x.startswith("of=") and x != "of=/dev/null" for x in a):
                found.add("disk_wipe")
        elif p.startswith("mkfs") or p in ("fdisk", "shred"):
            found.add("disk_wipe")
        elif p == "diskutil":
            if a and (a[0].lower().startswith("erase") or a[0] in ("partitionDisk", "zeroDisk", "secureErase",
                                                                    "reformat")):
                found.add("disk_wipe")
        elif p == "chmod":
            if any(_MODE_WIDE.match(x) for x in a) or "R" in sf or "--recursive" in a:
                found.add("chmod_wide")
        elif p == "chown":
            if "R" in sf or "--recursive" in a:
                found.add("chmod_wide")
        elif p in ("kill",):
            if any(x in _SIGKILL for x in a) or any(x == "-s" and i + 1 < len(a) and a[i + 1] in ("9", "KILL")
                                                    for i, x in enumerate(a)):
                found.add("kill_process")
        elif p in ("pkill", "killall"):
            found.add("kill_process")
        elif p == "launchctl":
            if pos[:1] in (["kill"], ["bootout"], ["unload"]):
                found.add("kill_process")
            elif pos[:1] in (["load"], ["bootstrap"], ["enable"]):
                found.add("system_change")
        elif p == "systemctl":
            if pos[:1] and pos[0] in ("enable", "disable", "stop", "restart"):
                found.add("system_change")
        elif p == "crontab":
            if "e" in sf or "r" in sf or (pos and "l" not in sf):
                found.add("system_change")
        elif p == "defaults":
            if pos[:1] == ["write"]:
                found.add("system_change")
        elif p == "sysctl":
            if "w" in sf:
                found.add("system_change")
        elif p == "networksetup":
            if a and not a[0].startswith(("-get", "-list", "-show", "-print")):
                found.add("system_change")
        elif p == "scutil":
            if "--set" in a:
                found.add("system_change")
        elif p == "nvram":
            if any("=" in x for x in a) or "d" in sf or "c" in sf:
                found.add("system_change")
        elif p == "tee":
            if any(x.startswith("/etc/") for x in pos):
                found.add("system_change")
        elif p in ("cp", "mv", "install", "ln"):
            if pos and pos[-1].startswith("/etc/"):
                found.add("system_change")
        elif p == "printenv":
            found.add("secrets_read")
        elif p == "security":
            if pos[:1] in (["find-generic-password"], ["find-internet-password"]) and \
                    any(re.match(r"^-[A-Za-z]*w", x) for x in a):
                found.add("secrets_read")
        elif p == "gh":
            if pos[:2] == ["auth", "token"]:
                found.add("secrets_read")
        elif p == "ssh":
            if positional(a, _SSH_WITH_VALUE):
                found.add("remote_shell")
        elif p in ("nc", "ncat", "netcat"):
            found.add("network_send")
        elif p in ("scp", "rsync", "sftp"):
            if p == "sftp":
                if positional(a, _SSH_WITH_VALUE):
                    found.add("network_send")
            elif any(_REMOTE_OPERAND.match(x) or x.startswith("rsync://") for x in pos):
                found.add("network_send")
        elif p in ("curl", "wget"):
            if _sends_data(seg):
                hosts = _hosts(a)
                if not hosts or any(not _LOCAL_HOST.match(h) for h in hosts):
                    found.add("network_send")

        # redirects: disk devices, /etc
        for op, target in seg.redirects:
            if op in (">", ">>", ">|", "&>", "&>>"):
                if _DISK_REDIRECT.match(target):
                    found.add("disk_wipe")
                elif target.startswith("/etc/"):
                    found.add("system_change")

        # secret readers
        if p in _READERS:
            if any(_is_secret_ref(x, home) for x in a) or any(op == "<" and _is_secret(t, home)
                                                              for op, t in seg.redirects):
                found.add("secrets_read")

        # package installs
        found.update(_install(seg))
        # databases
        found.update(_db(seg))
        if _infra_destroy(seg):
            found.add("infra_destroy")
        if _deploy(seg):
            found.add("deploy")
    # npx -y / --yes: shellparse peels npx, so look at the raw words
    for seg in segs:
        toks = seg.raw.split()
        for i, tok in enumerate(toks[:8]):
            if os.path.basename(tok) == "npx":
                if any(x in ("-y", "--yes") for x in toks[i + 1:i + 4]):
                    found.add("install_package")
                break
    return found


def _pkgs(args: List[str]) -> List[str]:
    return [x for x in positional(args, ("-r", "--requirement", "-e", "--editable", "-c", "--constraint", "-i",
                                        "--index-url", "--extra-index-url", "-t", "--target", "--registry",
                                        "--prefix", "--tag", "--path", "--version", "--root"))
            if x not in (".", "..")]


def _install(seg: Segment) -> List[str]:
    p, a = seg.program, seg.args
    if p.startswith("python") and a[:2] == ["-m", "pip"]:
        p, a = "pip", a[2:]
    if p == "uv" and a[:1] == ["pip"]:
        p, a = "pip", a[1:]
    pos = _pkgs(a)
    first = positional(a)[:1]
    if p in ("npm", "pnpm", "yarn", "bun"):
        if first and first[0] in ("install", "i", "add") and len(pos) > 1:
            return ["install_package"]
    elif p in ("pip", "pip3", "pipx", "brew", "apt", "apt-get", "yum", "dnf", "cargo", "gem", "go"):
        if first and first[0] == "install" and len(pos) > 1:
            return ["install_package"]
        if p == "brew" and first and first[0] == "reinstall" and len(pos) > 1:
            return ["install_package"]
    elif p == "uv":
        if first == ["add"] and len(pos) > 1:
            return ["install_package"]
    return []


def _db(seg: Segment) -> List[str]:
    p, a = seg.program, seg.args
    out: List[str] = []
    if p == "dropdb":
        return ["db_reset"]
    if p == "redis-cli":
        if any(x.lower() in ("flushall", "flushdb") for x in a):
            return ["db_reset"]
    if p in ("rails", "rake", "bundle"):
        words = [x for x in a if not x.startswith("-")]
        if any(w.startswith(("db:drop", "db:reset")) for w in words):
            return ["db_reset"]
        if any(w.startswith("db:migrate") for w in words):
            return ["db_migrate"]
    t = _tool(seg, "prisma")
    if t is not None:
        tp = positional(t)
        if tp[:2] == ["migrate", "reset"] or (tp[:2] == ["db", "push"] and
                                              any(x in ("--force-reset", "--accept-data-loss") for x in t)):
            return ["db_reset"]
        if tp[:2] in (["migrate", "deploy"], ["migrate", "dev"], ["db", "push"]):
            return ["db_migrate"]
    t = _tool(seg, "supabase")
    if t is not None:
        tp = positional(t)
        if tp[:2] == ["db", "reset"]:
            return ["db_reset"]
        if tp[:2] == ["db", "push"]:
            return ["db_migrate"]
    t = _tool(seg, "alembic")
    if t is not None and positional(t)[:1] in (["upgrade"], ["downgrade"]):
        return ["db_migrate"]
    t = _tool(seg, "knex")
    if t is not None and any(x.startswith("migrate") for x in positional(t)[:1]):
        return ["db_migrate"]
    t = _tool(seg, "sequelize", "sequelize-cli")
    if t is not None and any(x.startswith("db:migrate") for x in positional(t)[:1]):
        return ["db_migrate"]
    t = _tool(seg, "drizzle-kit")
    if t is not None and positional(t)[:1] in (["push"], ["migrate"]):
        return ["db_migrate"]
    return out


# ---------------------------------------------------------------- file tools

_CI_PATH = re.compile(r"(?:^|/)(?:\.github/workflows/|\.circleci/|\.gitlab-ci\.ya?ml$|azure-pipelines\.ya?ml$|"
                      r"Jenkinsfile$|bitbucket-pipelines\.ya?ml$)")
_GIT_INTERNAL = re.compile(r"(?:^|/)\.git/")


def _file_codes(path: str, project: str, home: str, writing: bool) -> Set[str]:
    out: Set[str] = set()
    if _is_secret(path, home):
        out.add("secrets_read" if not writing else "secret_edit")
    if not writing:
        return out
    p = _expand(path, home)
    if _GIT_INTERNAL.search(p) or p.endswith("/.git"):
        out.add("git_internal")
    if _CI_PATH.search(p):
        out.add("ci_edit")
    if project:                               # without a known project there is no "outside"
        full = os.path.normpath(p if p.startswith(("/", "~")) else os.path.join(project, p))
        if not _under(full, project) and not _is_temp(full + "/"):
            out.add("outside_write")
    return out


# ---------------------------------------------------------------- mcp

_READ_PREFIX = ("get_", "list_", "read_", "search_", "describe_", "fetch_", "find_", "show_", "view_", "count_")
_MCP_DESTRUCTIVE = ("delete", "drop", "remove", "destroy", "purge", "truncate", "wipe", "reset")
_MCP_EXTERNAL = ("deploy", "publish", "release", "send", "post_message", "email", "charge", "payment", "transfer",
                 "merge_pull_request")
_MCP_WRITE = ("create", "update", "write", "edit", "set_", "push", "upload", "insert", "upsert")


def _mcp_codes(tool: str, what: str) -> Set[str]:
    parts = tool.split("__")
    name = "__".join(parts[2:] if len(parts) > 2 else parts[1:]).lower()
    toks = name.split("_")
    sqlish = "sql" in toks or "migration" in toks or name in ("query", "run_query", "execute_query")
    out: Set[str] = set()
    if (sqlish or "query" in toks) and _sql_destructive(what):
        out.add("sql_destructive")
    if name.startswith(_READ_PREFIX):
        return out
    if any(w in name for w in _MCP_DESTRUCTIVE):
        out.add("mcp_destructive")
    if any(w in name for w in _MCP_EXTERNAL):
        out.add("mcp_external")
    if sqlish:
        out.add("mcp_sql")
    if any(w in name for w in _MCP_WRITE):
        out.add("mcp_write")
    return out


# ---------------------------------------------------------------- entry points

_FILE_WRITE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _pick(codes: Set[str]) -> Optional[Tuple[str, str]]:
    if not codes:
        return None
    best = min(codes, key=lambda c: (0 if LEVEL_OF[c] == "high" else 1, _RANK[c]))
    return LEVEL_OF[best], best


@functools.lru_cache(maxsize=32768)
def _decide(tool: str, what: str, project: str, home: str) -> Optional[Tuple[str, str]]:
    try:
        what = what[:MAX_LEN]
        if tool == "Bash":
            if not _PREFILTER.search(what):
                return None
            return _pick(_bash(what, project, home))
        if tool in _FILE_WRITE:
            return _pick(_file_codes(what.strip(), project, home, True))
        if tool == "Read":
            return _pick(_file_codes(what.strip(), project, home, False))
        if tool.startswith("mcp__"):
            return _pick(_mcp_codes(tool, what))
    except Exception:
        return None
    return None


def classify(tool: str, what: str, project: str = "", home: str = "") -> Optional[Dict[str, str]]:
    """None, or {"level": "high"|"medium", "code": <code>, "label": <short label in the current language>}."""
    try:
        got = _decide(str(tool or ""), str(what or ""), str(project or ""), str(home or ""))
        if got is None:
            return None
        return {"level": got[0], "code": got[1], "label": i18n.t("risk.r_" + got[1])}
    except Exception:
        return None


classify.cache_clear = _decide.cache_clear          # type: ignore[attr-defined]
classify.cache_info = _decide.cache_info            # type: ignore[attr-defined]
