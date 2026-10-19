"""Engines: which AI coding agent drives a run. `claude` (Claude Code, the default) or `cursor` (Cursor Agent —
the IDE chat and the `agent` CLI).

The state machine, the evidence layer and the guards are engine-neutral. An engine differs in exactly four things, and
they all live here so nothing else has to know:
  * how a session is recognised from the environment of a command the agent runs,
  * how a headless turn is started and continued (command line, where the prompt goes),
  * what the first prompt of a run looks like,
  * what its tools and subagents are called in the instructions the model reads.

Facts about Cursor below were measured on 2026-10-05 against `agent` 2026.10.01 (payloads in tests/fixtures/cursor).
Light module: stdlib only, imported by hooks.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import i18n
from .lazy import LazyModule
from .messages import protocol as _protocol

# Imported on first use: hooks reach this module for short_sid and the session-key prefixes only (each import costs 3 to 6 ms).
shutil = LazyModule("shutil")
subprocess = LazyModule("subprocess")
tempfile = LazyModule("tempfile")
uuid = LazyModule("uuid")

ENGINES = ("claude", "cursor", "copilot", "pi")
DEFAULT_ENGINE = "claude"
AGENT_NAMES = ("scout", "researcher", "architect", "plan-critic", "implementer", "reviewer", "adversary", "verifier")
SKILL_NAMES = ("run", "resume", "status", "onboard", "rollback", "ship")


CURSOR_SID_PREFIX = "cursor-"    # key of a Cursor conversation in ~/.orkestra/sessions (see session_key)
COPILOT_SID_PREFIX = "copilot-"
PI_SID_PREFIX = "pi-"
_PI_SESSION_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class EngineError(Exception):
    pass


def session_key(engine: str, raw_id: str) -> str:
    """The id a session is bound under. Claude Code session ids are used as they are. Cursor conversation ids get a
    prefix: Cursor also runs the hooks of imported Claude Code plugins with its own payloads, and an older orkestra
    copy loaded that way must never find (and act on) a Cursor session's binding or git grant."""
    if not raw_id:
        return ""
    prefix = {"cursor": CURSOR_SID_PREFIX, "copilot": COPILOT_SID_PREFIX, "pi": PI_SID_PREFIX}.get(engine)
    if prefix and not raw_id.startswith(prefix):
        return prefix + raw_id
    return raw_id


def raw_session_id(session_id: str) -> str:
    """What the engine's CLI calls the session (`--resume <id>`)."""
    s = session_id or ""
    for prefix in (CURSOR_SID_PREFIX, COPILOT_SID_PREFIX, PI_SID_PREFIX):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def session_engine(session_id: str, binding: Optional[Mapping[str, Any]] = None) -> str:
    if (session_id or "").startswith(CURSOR_SID_PREFIX):
        return "cursor"
    if (session_id or "").startswith(COPILOT_SID_PREFIX):
        return "copilot"
    if (session_id or "").startswith(PI_SID_PREFIX):
        return "pi"
    try:
        return normalize((binding or {}).get("engine"))
    except EngineError:
        return DEFAULT_ENGINE


def normalize(name: Optional[str], default: Optional[str] = None) -> str:
    n = (name or "").strip().lower()
    if not n:
        return default or DEFAULT_ENGINE
    if n not in ENGINES:
        raise EngineError(f"unknown engine '{name}' (choose: {', '.join(ENGINES)})")
    return n


def default_engine(cfg: Optional[Mapping[str, Any]] = None) -> str:
    """~/.orkestra/config.json → engine.default (claude unless the user changed it)."""
    try:
        return normalize(((cfg or {}).get("engine") or {}).get("default"))
    except EngineError:
        return DEFAULT_ENGINE


def detect_env(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Which agent runs the current process? (a command started from the agent's shell tool)

    Cursor exports CURSOR_AGENT=1 and CURSOR_CONVERSATION_ID to the shell of its agent (IDE chat, CLI, subagents);
    Claude Code exports CLAUDECODE=1. None = an ordinary terminal."""
    env = os.environ if env is None else env
    explicit = str(env.get("ORKESTRA_ENGINE") or "").strip().lower()
    if explicit in ENGINES:
        return explicit
    cursor = env.get("CURSOR_AGENT") == "1" or bool(env.get("CURSOR_CONVERSATION_ID"))
    claude = env.get("CLAUDECODE") == "1"
    if cursor and claude:           # one agent started from the other's shell: Claude Code sets its marker itself,
        return "claude"             # the Cursor ones were inherited
    return "cursor" if cursor else ("claude" if claude else None)


def env_session_id(env: Optional[Mapping[str, str]] = None) -> str:
    """The session id the hooks of this agent will carry — known from the environment only for Cursor."""
    env = os.environ if env is None else env
    if detect_env(env) == "cursor":
        return str(env.get("CURSOR_CONVERSATION_ID") or "")
    return ""


def binary(engine: str, cfg: Optional[Mapping[str, Any]] = None, hint: Optional[str] = None) -> Optional[str]:
    """Absolute path of the engine's CLI, or None. Order: env override → hint (bridge config) → config → PATH."""
    engine = normalize(engine)
    ecfg = ((cfg or {}).get("engine") or {}).get(engine) or {}
    names = {"claude": ("claude",), "cursor": ("agent", "cursor-agent"), "copilot": ("copilot",), "pi": ("pi",)}[engine]
    cands = [os.environ.get(f"ORKESTRA_{engine.upper()}_BIN"), hint, ecfg.get("bin"), *names]
    if engine == "cursor":      # a launchd / cron PATH rarely has the installer's directory
        cands += ["~/.local/bin/agent", "~/.local/bin/cursor-agent", "/usr/local/bin/cursor-agent",
                  "/opt/homebrew/bin/cursor-agent"]
    for c in cands:
        if not c:
            continue
        c = os.path.expanduser(str(c))
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
            continue
        found = shutil.which(c)
        if found:
            return found
    return None


def new_session(engine: str, bin_path: str, cwd: str) -> str:
    """A session id for the first headless turn. Claude takes any uuid (--session-id); Cursor hands one out."""
    if normalize(engine) in ("claude", "copilot"):
        return str(uuid.uuid4())
    try:
        cp = subprocess.run([bin_path, "create-chat"], cwd=cwd, capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineError(f"cursor: `{bin_path} create-chat` failed: {exc}")
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", cp.stdout or "")
    if cp.returncode != 0 or not m:
        raise EngineError(f"cursor: `create-chat` gave no chat id (exit {cp.returncode}): {(cp.stderr or cp.stdout)[-300:]}")
    return m.group(0)


def _pi_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def pi_assistant_text(stream: str) -> str:
    """Last non-empty assistant text in a Pi JSONL stream.

    Records split on LF only. U+2028, U+2029 and U+0085 stay inside JSON strings.
    A later assistant row with no text does not erase the previous answer.
    """
    last = ""
    text = (stream or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw in text.split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "message_end":
            continue
        message = rec.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        got = _pi_content_text(message.get("content"))
        if got:
            last = got
    return last


def pi_turn_message(stdout: str) -> str:
    """What a Pi turn should show. Tool `result` objects are not the answer."""
    return pi_assistant_text(stdout) or (stdout or "")[-2000:]


def headless_command(engine: str, bin_path: str, session_id: str, prompt: str, new: bool,
                     opts: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """{"argv": [...], "stdin": text | None} for one non-interactive turn on an existing or new session.

    claude: prompt on stdin, `--session-id` (new) / `--resume`, permission mode and turn cap from the options.
    cursor: prompt is the last ARGUMENT; the chat id always goes to `--resume` (it was made by `create-chat`);
            `--force` lets tools run without a prompt (our preToolUse hook still gates every call), `--trust` skips
            the workspace-trust question that would block a headless process."""
    engine, o = normalize(engine), dict(opts or {})
    if engine == "claude":
        argv = [bin_path, "-p", "--session-id" if new else "--resume", session_id,
                "--permission-mode", str(o.get("permission_mode") or "auto"),
                "--output-format", "json", "--max-turns", str(o.get("max_turns") or 400)]
        if o.get("max_budget_usd") and float(o["max_budget_usd"]) > 0:     # opt-in spend ceiling (headless.max_budget_usd)
            argv += ["--max-budget-usd", str(o["max_budget_usd"])]
        return {"argv": argv + [str(x) for x in (o.get("extra_args") or [])], "stdin": prompt}
    if engine == "pi":
        # Pi JSON mode: one JSONL stream on stdout, session pinned by --session-id (creates the id when absent).
        # --extension loads the same file ~/.pi/agent/extensions also discovers, so the tool_call guard runs.
        text = (prompt or "").replace("\x00", "")
        if text.lstrip(" \t").startswith("-"):
            text = "\n" + text
        sid = _PI_SESSION_SAFE.sub("-", raw_session_id(session_id)).strip("-._")
        sid = sid or "session"
        from . import pikit as PK
        extension = str(o.get("extension") or PK.extension_path())
        argv = [bin_path, "--mode", "json", "--session-id", sid]
        if Path(extension).is_file():
            argv += ["--extension", extension]
        return {"argv": argv + [str(x) for x in (o.get("extra_args") or [])] + [text], "stdin": None}
    if engine == "copilot":
        # `-p` takes the prompt as its value: pass it as --prompt=<text> (also safe for a prompt starting with "-").
        argv = [bin_path, "--output-format", "json", *_copilot_permission_args(o),
                "--excluded-tools", COPILOT_EXCLUDED_TOOLS]
        if o.get("approval_mode", "autopilot") != "manual":
            argv += ["--autopilot", "--no-ask-user"]
        argv += _copilot_profile_args(bin_path, o)
        if o.get("max_autopilot_continues"):
            argv += ["--max-autopilot-continues", str(o["max_autopilot_continues"])]
        plugin_dir = str(o.get("plugin_dir") or copilot_plugin_dir())
        if plugin_dir:
            argv += ["--plugin-dir", plugin_dir]
        argv += [("--session-id=" if new else "--resume=") + raw_session_id(session_id)]
        return {"argv": argv + [str(x) for x in (o.get("extra_args") or [])] + ["--prompt=" + (prompt or "").replace("\x00", "")],
                "stdin": None}
    argv = [bin_path, "-p", "--output-format", "json", "--force", "--trust", "--resume", session_id]
    if o.get("model"):
        argv += ["--model", str(o["model"])]
    prompt = (prompt or "").replace("\x00", "")          # an argument cannot carry NUL…
    if prompt.lstrip(" \t").startswith("-"):
        prompt = "\n" + prompt                           # …and must not be read as an option ("- T2'yi çıkar")
    return {"argv": argv + [str(x) for x in (o.get("extra_args") or [])] + [prompt], "stdin": None}


def interactive_command(engine: str, bin_path: str, prompt: str, opts: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Open the engine's own interactive terminal UI with the first prompt already submitted."""
    engine, o = normalize(engine), dict(opts or {})
    if engine == "pi":
        text = (prompt or "").replace("\x00", "")
        extra = [str(x) for x in (o.get("interactive_args") or [])]
        from . import pikit as PK
        flags = ["--extension", str(o.get("extension") or PK.extension_path())]
        sid = str(o.get("session_id") or "")
        if sid:
            flags = ["--session-id", raw_session_id(sid)] + flags
        if text.lstrip(" \t").startswith("-"):
            return [bin_path, *extra, *flags, "--", text]
        return [bin_path, *extra, *flags, text]
    if engine == "copilot":
        argv = [bin_path, "-i", prompt, *_copilot_permission_args(o), "--excluded-tools", COPILOT_EXCLUDED_TOOLS]
        if o.get("approval_mode", "autopilot") != "manual":
            argv += ["--autopilot", "--no-ask-user"]
        argv += _copilot_profile_args(bin_path, o)
        if o.get("max_autopilot_continues"):       # the same cap the headless builder passes
            argv += ["--max-autopilot-continues", str(o["max_autopilot_continues"])]
        plugin_dir = str(o.get("plugin_dir") or copilot_plugin_dir())
        if plugin_dir:
            argv += ["--plugin-dir", plugin_dir]
        return argv + [str(x) for x in (o.get("interactive_args") or [])]
    argv = [bin_path]
    if engine == "cursor" and o.get("model"):
        argv += ["--model", str(o["model"])]
    return argv + [str(x) for x in (o.get("interactive_args") or [])] + [prompt]


def run_prompt(engine: str, request: str) -> str:
    if normalize(engine) == "claude":
        return f"/orkestra:run {request}"
    if normalize(engine) == "pi":
        # The installed skill holds the playbook. A short command starts faster than pasting it.
        return f"/skill:orkestra-run {request}".strip()
    # no reliance on skill discovery: the CLI serves the same instructions the skill carries
    body = ("Orkestra run. First run the shell command `orkestra playbook run` and follow what it prints exactly "
            f"(it is the orkestra-run skill).\n\n{i18n.heading('request')}: {request}")
    if normalize(engine) == "copilot":
        return COPILOT_RUN_RULE + "\n\n" + body
    return body


def resume_prompt(engine: str, project_name: str = "") -> str:
    if normalize(engine) == "claude":
        return f"/orkestra:resume {project_name}".rstrip()
    if normalize(engine) == "pi":
        return f"/skill:orkestra-resume {project_name}".strip()
    return ("Continue the open Orkestra run. First run the shell command `orkestra resume"
            + (f" -p {project_name}" if project_name else "") + "` and do what its `next` line says "
            "(`orkestra playbook resume` has the procedure).")


# ---------------------------------------------------------------- instructions: one text, two vocabularies

# Tools that open another Copilot chat or subagent. Official names: task, list_agents, read_agent, write_agent.
COPILOT_EXCLUDED_TOOLS = "task,list_agents,read_agent,write_agent"
COPILOT_RUN_RULE = ("Stay in this one Copilot session. Do not open another chat, fleet, or subagent. "
                    "Do every role yourself with the shell and the orkestra CLI.")

NOTES_MARK = "[orkestra · Cursor engine]"
COPILOT_MARK = "[orkestra · Copilot engine]"
COPILOT_NOTES = COPILOT_MARK + """ The instructions below were written for Claude Code. In Copilot:
- Do not open another chat, session, fleet, or subagent. The task tool is off. Do every role in this one session.
- Use the shell and the `orkestra` CLI. Where the text says to dispatch a subagent, do that role here instead.
- Do not end the turn while the run is active. End it only for `orkestra await` or `orkestra block`.
- AskUserQuestion does not exist. Write the question in this session, run `orkestra await "<one-line summary>"`, and end the turn.
- One session runs the task's own tests. The full suite runs at `orkestra gate baseline` and `orkestra gate verify`, not after every edit."""
CURSOR_NOTES = NOTES_MARK + """ The instructions below were written for Claude Code. In Cursor:
- A subagent `orkestra:<name>` is the Cursor subagent `orkestra-<name>`: launch it with the Task tool
  (subagent_type "orkestra-<name>"), ALWAYS in the foreground (run_in_background: false). Its report comes back in
  the same turn. The "model" column of the agent table does not apply (subagents use the chat's model).
- Do NOT end your turn while the run is active: there is nothing to wait for. End it only after `orkestra await`
  (a question / the plan approval) or `orkestra block`.
- AskUserQuestion does not exist. Wherever the text says to ask with it: write the question(s) in the chat, run
  `orkestra await "<one-line summary>"`, then end your turn (`orkestra playbook headless` has the wording — the user
  reads your message right here in the chat). The user's next message is the answer; a hook marks the run active
  again. Plan approval is any clear approve intent the hook recognizes ("approve", "I approve", "go ahead", "lgtm",
  "onay", "onaylıyorum", "onay verdim", "bu planı onaylıyorum yap …") — not only the bare word "approve". Never
  approve yourself; after their message run `orkestra phase next`, which tells you whether the approval was recorded. The approval is
  never skipped ("don't ask questions" in the request does not waive it) and never recorded by you in any way.
- There is no SendMessage: to send findings back to a subagent, launch the same subagent type again with the
  findings and the path of its previous report.
- The shell keeps its working directory between calls and the guards cannot see it. Stay in the project root; if
  another directory is needed use absolute paths or `cd <absolute path> && …` in the same command.
- Skills are `/orkestra-run`, `/orkestra-resume`, `/orkestra-status`, `/orkestra-onboard`, `/orkestra-rollback`,
  `/orkestra-ship`. `orkestra` is on PATH. The same guards, gates and evidence rules apply as in Claude Code."""

AGENT_NOTES = NOTES_MARK + """ You are an orkestra subagent running in Cursor; the text below was written for
Claude Code. Its "Bash" is your Shell tool, "Edit"/"Write" are your file tools. `orkestra` is on PATH. The shell
keeps its working directory between calls and the guards cannot see it: use absolute paths or
`cd <absolute path> && …` in the same command. Your final message is your report to the orchestrator."""

_AGENT_RE = re.compile(r"\borkestra:(" + "|".join(re.escape(a) for a in AGENT_NAMES) + r")\b")
_SKILL_RE = re.compile(r"/orkestra:(" + "|".join(SKILL_NAMES) + r")\b")

ASK_FOOTNOTE = ("(Cursor: there is no AskUserQuestion tool — write the question in the chat, run "
                "`orkestra await \"<one-line summary>\"` and end your turn; plan approval is the user typing "
                "\"approve\" or \"onay\".)")

# (Editing a skill / playbook / agent text? tests/test_engines2.py::test_every_rewrite_still_matches_its_source_sentence
# tells you when an entry below has to follow.)
# Sentences of the skills / playbooks that are only true in Claude Code (background subagents whose report arrives
# as a later message, AskUserQuestion dialogs, SendMessage, /compact) and what holds in Cursor instead. Exact text:
# tests/test_engines.py fails when a source sentence changes and its entry here no longer matches anything.
CURSOR_REWRITES = (
    ("Subagents run in the **background**: the Agent call returns at once (`async_launched`) and the agent's final report\n"
     "arrives later as a message (`<agent-message …>` — a report, never the user; it approves nothing). So after\n"
     "dispatching, **end your turn**. The hand-back resumes you and carries the next step. Never `sleep`, loop, or re-read\n"
     "files to wait for an agent — a waiting Bash call only delays the report (the `wait` guard blocks it).",
     "Subagents run in the **foreground**: the Task call returns the agent's final report in the same turn (a report,\n"
     "never the user; it approves nothing). Read it and go on with the next step — do not end your turn while the run\n"
     "is active."),
    (", then end your turn — their notes come back as messages.", "; their notes come back when the calls return."),
    (" Then **end your turn** — its report arrives as a message.", " Its report comes back in the same turn."),
    ("(it writes `reviews/Txxx.review.md`). End your turn.", "(it writes `reviews/Txxx.review.md`)."),
    ("(SendMessage to the same agent\n   if available, otherwise a new implementer with the brief + review file path)",
     "(a new implementer call\n   with the brief + review file path)"),
    ("While agents run, end your turn —\n  do not sleep or poll (`orkestra next` shows who is still running).",
     "Their reports come back in the same turn —\n  never sleep or poll."),
    (" After each dispatch end your turn; the report resumes you.", " Each report comes back in the same turn."),
    ("(same agent via SendMessage if available, else a new call with the critic file path)",
     "(a new architect call with the critic file path)"),
    (", then end your turn (both reports arrive as messages):", " (both reports come back in the same turn):"),
    ("   End your turn after dispatching; its report resumes you.", "   Its report comes back in the same turn."),
    ("- Use haiku/sonnet agents for mechanical work; keep opus for design and judgment.\n", ""),
    ("`/compact`", "`/summarize`"),
    ("There is no AskUserQuestion and no live user.",
     "There is no AskUserQuestion tool (in a Cursor chat the user reads your message right there)."),
)

# The rollback question is worded per language (messages/protocol.PROMPTS), so it is matched by shape, not by text:
# a numbered item "Ask with AskUserQuestion … (header `<Rollback | Geri alma>`) …" on one line.
_ROLLBACK_ASK_RE = re.compile(
    r"^([ \t]*\d+\.[ \t]+)Ask with AskUserQuestion[^\n]*`(?:%s)`[^\n]*$"
    % "|".join(re.escape(h) for h in _protocol.PROMPTS["rollback"]["header"]), re.M)       # type: ignore[attr-defined]
_ROLLBACK_CURSOR = ("Ask in the chat which one they want and END YOUR TURN. Applying needs the user's own message: they send\n"
                    "   `/orkestra-rollback apply` or `/orkestra-rollback uygula` (only orkestra's changes), or the same with\n"
                    "   `--all` (also the other differences); anything else = no.")


def _rename(text: str) -> str:
    out = text
    for old, new in CURSOR_REWRITES:
        if old in out:
            out = out.replace(old, new)
    out = _ROLLBACK_ASK_RE.sub(lambda m: m.group(1) + _ROLLBACK_CURSOR, out)
    return _SKILL_RE.sub(r"/orkestra-\1", _AGENT_RE.sub(r"orkestra-\1", out)).replace("orkestra:<name>", "orkestra-<name>")


def adapt_text(text: str, engine: Optional[str], footnote: bool = True) -> str:
    """Rename what has another name in the engine; everything else is left exactly as written. A message that
    tells the model to use AskUserQuestion gets one line saying what that means in Cursor."""
    if (engine or DEFAULT_ENGINE) != "cursor" or not text:
        return text
    out = _rename(text)
    if footnote and "AskUserQuestion" in out and NOTES_MARK not in out and ASK_FOOTNOTE not in out:
        out = out.rstrip("\n") + "\n" + ASK_FOOTNOTE + ("\n" if text.endswith("\n") else "")
    return out


def with_notes(text: str, engine: Optional[str], agent: bool = False) -> str:
    """The vocabulary header plus the renamed text (whole documents: playbooks, skills; `agent`: a subagent's file)."""
    if engine == "copilot":
        return COPILOT_NOTES + "\n\n" + (text or "")
    if (engine or DEFAULT_ENGINE) != "cursor":
        return text
    return (AGENT_NOTES if agent else CURSOR_NOTES) + "\n\n" + _rename(text)


def short_sid(session_id: str) -> str:
    """Eight characters that tell sessions apart in logs (a Cursor key starts with the same prefix for all)."""
    s = session_id or ""
    if s.startswith(CURSOR_SID_PREFIX):
        return "c:" + s[len(CURSOR_SID_PREFIX):][:8]
    if s.startswith(COPILOT_SID_PREFIX):
        return "cp:" + s[len(COPILOT_SID_PREFIX):][:8]
    return s[:8]


# ---------------------------------------------------------------- headless drivers (bridge, watchdog)

CURSOR_HOOK = Path(__file__).resolve().parents[2] / "hooks" / "cursor_hook.py"
PLUGIN_BIN = Path(__file__).resolve().parents[2] / "bin"
AGENT_MARKERS = ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID")     # what an agent's own shell carries


def child_env(engine: str, env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Environment for an engine process a driver starts."""
    out = dict(os.environ if env is None else env)
    out["ORKESTRA_HEADLESS"] = "1"
    out["ORKESTRA_ENGINE"] = normalize(engine)
    for k in AGENT_MARKERS:                 # a driver started from inside some agent's shell: the child is its own agent
        out.pop(k, None)
    if normalize(engine) in ("cursor", "copilot"):
        # always first: another `orkestra` earlier on PATH (a stale shim) must not shadow this checkout's CLI
        parts = [p for p in str(out.get("PATH") or "").split(os.pathsep) if p and p != str(PLUGIN_BIN)]
        out["PATH"] = os.pathsep.join([str(PLUGIN_BIN)] + parts)
    return out


def copilot_plugin_dir(cfg: Optional[Mapping[str, Any]] = None) -> str:
    """The installed Orkestra Copilot plugin, if present."""
    configured = (((cfg or {}).get("engine") or {}).get("copilot") or {}).get("plugin_dir")
    if configured:
        return os.path.expanduser(str(configured))
    home = Path(os.environ.get("COPILOT_HOME") or "~/.copilot").expanduser()
    candidate = home / "plugins" / "orkestra"
    return str(candidate) if candidate.is_dir() else ""


def copilot_hooks_active(plugin_dir: str) -> bool:
    """True only when the plugin registers Orkestra's prompt and tool guards."""
    if not plugin_dir:
        return False
    root = Path(plugin_dir).expanduser()
    if not (root / "plugin.json").is_file():       # Copilot ignores a plugin directory without its manifest
        return False
    candidates = (root / "com.github.copilot" / "hooks" / "hooks.json", root / "hooks" / "hooks.json")
    for path in candidates:
        try:
            hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if all(any("copilot_hook.py" in json.dumps(entry) for entry in hooks.get(event, []))
               for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse")):
            return True
    return False


# Copilot CLI model / effort / context / allow-all: what the user may name, and the one resolver every path uses.
# Facts (Copilot CLI 1.0.98, `copilot --help`): --reasoning-effort none|minimal|low|medium|high|xhigh|max,
# --context default|long_context, --allow-all = --allow-all-tools --allow-all-paths --allow-all-urls.
# Whether a given model honours max / long_context is not exposed by the CLI: the CLI's own error is surfaced.
COPILOT_DEFAULTS = {"model": "gpt-6-luna", "reasoning_effort": "max", "context": "long_context", "allow_all": True}
COPILOT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
COPILOT_CONTEXTS = ("default", "long_context")
_COPILOT_MODELS = (
    "gpt-6-luna", "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-5.4", "gpt-5.2", "gpt-5", "gpt-4.1", "gpt-4o",
    "o4-mini", "o3", "o3-mini",
)
_COPILOT_EFFORTS = COPILOT_EFFORTS
_MODEL_FLAG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")      # free text, but never garbage for argv
_MODEL_IN_REQUEST = re.compile(
    r"(?i)(?:\b(?:use|with|model|modeli)\b[\s:=-]*)?(?:gpt[-\s]?6(?:\.1)?[-\s]?(?:luna|astra|sol)|gpt[-\s]?5(?:\.\d)?|"
    r"gpt[-\s]?4(?:\.1|o)?|\bo4-mini\b|\bo3(?:-mini)?\b)")
_EFFORT_IN_REQUEST = re.compile(
    r"(?i)(?:reasoning[\s-]*effort|effort|reasoning)[\s:=-]*(max|xhigh|high|medium|low|minimal|none)\b"
    r"|\b(max|xhigh|high|medium|low|minimal)\s+reasoning(?:\s+effort)?\b")
# Context / allow-all words count only as an explicit directive ("use long context", "no allow all"): a task such
# as "change the default context menu" or "allow all origins" is about the project, not about the run's profile.
_CONTEXT_LONG = re.compile(
    r"(?i)\b(?:use|using|with|set|enable)\s+(?:the\s+)?long[\s_-]*context\b|\blong[\s_-]*context\s+(?:mode|please|on)\b"
    r"|\buzun\s+(?:context|ba[gğ]lam)\s+(?:kullan|ile)\b")
_CONTEXT_DEFAULT = re.compile(
    r"(?i)\b(?:use|using|with|set)\s+(?:the\s+)?(?:default|normal|standard)\s+context\b(?!\s+\w)"
    r"|\b(?:default|normal|standard)\s+context\s+(?:mode|please)\b"
    r"|\b(?:varsay[ıi]lan|normal)\s+(?:context|ba[gğ]lam)\s+(?:kullan|ile)\b")
_ALLOW_ALL_NO = re.compile(r"(?i)\b(?:no|not|without|don'?t)[\s-]+allow[\s-]*all\b|\bhepsine\s+izin\s+verme\b")
_ALLOW_ALL_YES = re.compile(r"(?i)\b(?:use|with|enable)\s+allow[\s-]*all\b(?!\s+\w)|\bhepsine\s+izin\s+ver\b")


def _norm_copilot_model(token: str) -> str:
    t = re.sub(r"[\s_]+", "-", (token or "").strip().lower())
    t = t.replace("gpt6", "gpt-6").replace("gpt5", "gpt-5").replace("gpt4", "gpt-4")
    t = re.sub(r"^(gpt-6(?:\.1)?)(luna|astra|sol)$", r"\1-\2", t)
    return t


def pick_copilot_model(request: str) -> Optional[str]:
    """The one supported model named in the request, or None (nothing named, or several different ones)."""
    found: List[str] = []
    for m in _MODEL_IN_REQUEST.finditer(request or ""):
        name = _norm_copilot_model(re.sub(r"^(?:use|with|model|modeli)[\s:=-]*", "", _norm_copilot_model(m.group(0))))
        if name in _COPILOT_MODELS and name not in found:
            found.append(name)
    return found[0] if len(found) == 1 else None


def pick_copilot_effort(request: str) -> Optional[str]:
    found: List[str] = []
    for m in _EFFORT_IN_REQUEST.finditer(request or ""):
        word = (m.group(1) or m.group(2) or "").lower()
        if word in COPILOT_EFFORTS and word not in found:
            found.append(word)
    return found[0] if len(found) == 1 else None


def pick_copilot_context(request: str) -> Optional[str]:
    long_, default = bool(_CONTEXT_LONG.search(request or "")), bool(_CONTEXT_DEFAULT.search(request or ""))
    return "long_context" if long_ and not default else ("default" if default and not long_ else None)


def pick_copilot_allow_all(request: str) -> Optional[bool]:
    text = request or ""
    no = bool(_ALLOW_ALL_NO.search(text))
    yes = bool(_ALLOW_ALL_YES.search(_ALLOW_ALL_NO.sub(" ", text)))
    return False if no and not yes else (True if yes and not no else None)


def copilot_model_for_request(request: str, default: str = "gpt-6-luna") -> str:
    """Pick one Copilot model from the request text, or keep the configured default.

    A single explicit supported name wins (`use gpt-6-astra`). Several different names, or none, → default.
    """
    return pick_copilot_model(request) or default or "gpt-6-luna"


def copilot_effort_for_request(request: str, default: str = "max") -> str:
    """Pick one reasoning-effort word from the request, or keep the configured default."""
    return pick_copilot_effort(request) or default or "max"


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    return True if word in ("1", "true", "yes", "on", "y") else (False if word in ("0", "false", "no", "off", "n") else None)


def _copilot_check(key: str, value: Any, origin: str) -> Any:
    """Validate ONE profile value at the boundary (EngineError with a catalog message); returns the normal form."""
    if key == "allow_all":
        flag = _as_bool(value)
        if flag is None:
            raise EngineError(i18n.t("cli.copilot_bad_bool", key=key, value=value, origin=origin))
        return flag
    text = str(value).strip()
    if key == "model":
        if text.lower() == "auto":
            return "auto"
        if not _MODEL_FLAG_OK.match(text):
            raise EngineError(i18n.t("cli.copilot_bad_model", value=text[:60], origin=origin))
        return text
    allowed = COPILOT_EFFORTS if key == "reasoning_effort" else COPILOT_CONTEXTS
    if text.lower() not in allowed:
        raise EngineError(i18n.t("cli.copilot_bad_value", key=key, value=text[:60], allowed=", ".join(allowed),
                                 origin=origin))
    return text.lower()


def copilot_resolve(cfg: Optional[Mapping[str, Any]] = None, request: str = "", flags: Optional[Mapping[str, Any]] = None,
                    selected: Optional[Mapping[str, Any]] = None, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The ONE place a Copilot run's model / effort / context / allow-all is decided (run, resume, watchdog, bridge).

    Precedence, highest first: explicit flag, words in the request (allow-all: only "no allow all" counts), the choice already stored for this run/job
    (`selected`), engine.copilot config (`extra` = per-key overrides on top of it, e.g. the bridge's), built-in default.
    Returns the engine.copilot options with the four keys resolved plus `sources` ({key: where it came from}).
    """
    engine_cfg = (cfg or {}).get("engine")
    section = engine_cfg.get("copilot") if isinstance(engine_cfg, Mapping) else None     # a hand-edited config may hold junk
    opts = dict(section) if isinstance(section, Mapping) else {}
    opts.update({k: v for k, v in (extra or {}).items() if v not in (None, "")})
    req = {"model": pick_copilot_model(request), "reasoning_effort": pick_copilot_effort(request),
           "context": pick_copilot_context(request),
           "allow_all": False if pick_copilot_allow_all(request) is False else None}     # words may only LOWER privileges
    sources: Dict[str, str] = {}
    for key, default in COPILOT_DEFAULTS.items():
        layers = (("flag", (flags or {}).get(key)), ("request", req[key]), ("run", (selected or {}).get(key)),
                  ("config", opts.get(key)), ("default", default))
        for origin, value in layers:
            if value is None or value == "":
                continue
            opts[key] = _copilot_check(key, value, origin)
            sources[key] = origin
            break
    opts["sources"] = sources
    return opts


def copilot_profile_of(opts: Mapping[str, Any]) -> Dict[str, Any]:
    """The four resolved keys, as stored in the run state so a resume reuses what the run started with."""
    return {k: opts.get(k) for k in COPILOT_DEFAULTS}


def _copilot_permission_args(o: Mapping[str, Any]) -> List[str]:
    """allow_all (default on) = what `--allow-all` documents: every tool, path and URL without a prompt, so a
    headless turn never stalls on a question. --excluded-tools, deny rules and Orkestra's own hooks still apply."""
    if _as_bool(o.get("allow_all", True)) is False:
        return ["--allow-all-tools"]
    return ["--allow-all-tools", "--allow-all-paths", "--allow-all-urls"]


_cli_help: Dict[str, Optional[str]] = {}


def copilot_cli_has(bin_path: str, flag: str) -> Optional[bool]:
    """Does `copilot --help` list this flag? None = could not be checked (assume yes). Cached per binary."""
    if bin_path not in _cli_help:
        try:        # the CLI unpacks itself on first start: give it a throwaway home so nothing lands in the user's
            with tempfile.TemporaryDirectory(prefix="orkestra-cphelp-") as scratch:
                env = dict(os.environ, HOME=scratch, USERPROFILE=scratch, COPILOT_HOME=os.path.join(scratch, "c"),
                           XDG_CONFIG_HOME=scratch, XDG_CACHE_HOME=scratch, XDG_STATE_HOME=scratch,
                           APPDATA=scratch, LOCALAPPDATA=scratch)
                cp = subprocess.run([bin_path, "--help"], capture_output=True, encoding="utf-8", errors="replace",
                                    timeout=10, stdin=subprocess.DEVNULL, env=env)
            _cli_help[bin_path] = (cp.stdout or "") + (cp.stderr or "") if cp.returncode == 0 else None
        except Exception:
            _cli_help[bin_path] = None
    text = _cli_help[bin_path]
    return None if not text else flag in text


def _copilot_profile_args(bin_path: str, o: Mapping[str, Any]) -> List[str]:
    argv: List[str] = []
    if o.get("model"):
        argv += ["--model", str(o["model"])]
    if o.get("reasoning_effort"):
        argv += ["--reasoning-effort", str(o["reasoning_effort"])]
    if o.get("context") and copilot_cli_has(bin_path, "--context") is not False:    # an older CLI: omit silently
        argv += ["--context", str(o["context"])]
    return argv


def driver_prompt(engine: str, session_id: str, prompt: str, cwd: str) -> Optional[str]:
    """Tell the hook layer about a prompt a headless driver is about to send. Returns context for the model ("")
    — or None when the hook refused to hear it (the driver itself runs inside an agent's shell: nothing was
    recorded, and the caller must not go on as if the user's words had counted).

    Claude Code fires UserPromptSubmit in print mode. Cursor's print mode (`agent -p`) fires NO prompt hook
    (measured), so approval ("onay"), "the user replied" and the per-message git permission would never be seen:
    the driver calls the Cursor hook itself, with the user's own words.

    The hook runs in a short-lived reparented child (subprocess → double-fork), not by forking this process: the
    bridge HTTP server may call us from a worker thread. Agent env markers are kept so a model that calls this
    with CURSOR_AGENT set is still refused inside the hook.
    """
    if normalize(engine) != "cursor":
        return ""
    payload = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": raw_session_id(session_id),
               "prompt": prompt, "workspace_roots": [str(cwd)], "generation_id": "", "_driver": True}
    out_path = Path(tempfile.mkdtemp(prefix="orkestra-drv-")) / "out.json"
    try:
        if os.name == "nt":
            script = (
                "import os, sys, subprocess\n"
                f"out = {str(out_path)!r}\n"
                f"hook = {str(CURSOR_HOOK)!r}\n"
                f"payload = {json.dumps(json.dumps(payload))}\n"
                "flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)\n"
                "cp = subprocess.run([sys.executable, hook, 'beforeSubmitPrompt'], input=payload,\n"
                "                    capture_output=True, text=True, env=dict(os.environ), timeout=30,\n"
                "                    creationflags=flags)\n"
                "body = cp.stdout if cp.stdout.strip() else '{}'\n"
                "tmp = out + '.tmp'\n"
                "with open(tmp, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(body); fh.flush(); os.fsync(fh.fileno())\n"
                "os.replace(tmp, out)\n"
            )
        else:
            script = (
                "import os, sys, subprocess\n"
                f"out = {str(out_path)!r}\n"
                f"hook = {str(CURSOR_HOOK)!r}\n"
                f"payload = {json.dumps(json.dumps(payload))}\n"
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    if os.fork() == 0:\n"
                "        cp = subprocess.run([sys.executable, hook, 'beforeSubmitPrompt'], input=payload,\n"
                "                            capture_output=True, text=True, env=dict(os.environ), timeout=30)\n"
                "        body = cp.stdout if cp.stdout.strip() else '{}'\n"
                "        tmp = out + '.tmp'\n"
                "        with open(tmp, 'w', encoding='utf-8') as fh:\n"
                "            fh.write(body); fh.flush(); os.fsync(fh.fileno())\n"
                "        os.replace(tmp, out)\n"
                "        os._exit(0)\n"
                "    os._exit(0)\n"
                "os.wait()\n"
            )
        subprocess.run([sys.executable, "-c", script], check=True, timeout=45)
        deadline = time.time() + 45
        raw = ""
        while time.time() < deadline:
            if out_path.exists():
                raw = out_path.read_text(encoding="utf-8")
                if raw.strip():
                    break
            time.sleep(0.02)
        data = json.loads(raw or "{}")
        if isinstance(data, dict) and data.get("refused"):
            return None
        return str(data.get("context") or "") if isinstance(data, dict) else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            out_path.unlink(missing_ok=True)
            out_path.parent.rmdir()
        except OSError:
            pass



def agent_type_from_cursor(subagent_type: str) -> str:
    """Cursor subagent name → the agent type the guards know (`orkestra-reviewer` → `orkestra:reviewer`)."""
    s = (subagent_type or "").strip()
    if s.startswith("orkestra-") and s[len("orkestra-"):] in AGENT_NAMES:
        return "orkestra:" + s[len("orkestra-"):]
    return "cursor:" + (s or "agent")


def parse_result(stdout: str) -> Dict[str, Any]:
    """The final JSON object of a headless turn (both engines: {type: result, result, session_id, is_error …})."""
    text = (stdout or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    for line in reversed(text.splitlines()):        # stream-json or noise before the result line
        line = line.strip()
        if line.startswith("{") and '"result"' in line:
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except ValueError:
                continue
    return {}
