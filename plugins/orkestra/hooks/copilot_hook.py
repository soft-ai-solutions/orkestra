#!/usr/bin/env python3
"""Translate Copilot Agent Plugins hook events to Orkestra's existing dispatcher."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

import dispatch  # noqa: E402
from orkestra_core import i18n, paths, state as ST  # noqa: E402
from orkestra_core.lazy import LazyModule  # noqa: E402
from orkestra_core.util import file_lock, write_json_atomic  # noqa: E402

# imported when first used (engines pulls in subprocess, shutil, tempfile and uuid; this script starts for every event)
EN = LazyModule("engines", "orkestra_core")
hashlib = LazyModule("hashlib")

EVENTS = {
    "SessionStart": "SessionStart",
    "UserPromptSubmit": "UserPromptSubmit",
    "subagentStart": "SubagentStart",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "PostToolUseFailure": "PostToolUse",
    "Stop": "Stop",
    "SubagentStop": "SubagentStop",
}
TOOL_NAMES = {
    "bash": "Bash",
    "run_in_terminal": "Bash",
    "runinterminal": "Bash",
    "execute/runinterminal": "Bash",
    "powershell": "Bash",
    "view": "Read",
    "create": "Write",
    "edit": "Edit",
    "str_replace_editor": "Edit",
    "apply_patch": "Edit",
    "grep": "Grep",
    "rg": "Grep",
    "glob": "Glob",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "update_todo": "TodoWrite",
    "task": "Agent",
    "runsubagent": "Agent",
    "run_subagent": "Agent",
    "ask_user": "AskUserQuestion",
}
AGENT_ROLES = ("scout", "researcher", "architect", "plan-critic", "implementer", "reviewer", "adversary", "verifier")
RESUME_COMMAND = re.compile(r"^\s*/orkestra(?::|-)resume\b", re.I)
RUN_MARKER = re.compile(r"(?m)^\[orkestra:(?:bind|unbind)\] project=(.+?) run=(\S+)(?: home=.+?)?\s*$")
ONE_CHAT = ("[orkestra] Stay in this chat. Do not open another chat or subagent. "
            "Do this role here with the shell and the orkestra CLI.")


def _copilot_session_id(value: Any) -> str:
    return EN.session_key("copilot", str(value or ""))


def _canonical_agent(value: Any) -> str:
    text = str(value or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    for role in AGENT_ROLES:
        if slug in (role, "orkestra-" + role):
            return "orkestra:" + role
    return "orkestra:unknown"


def _prompt_file(session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    home = Path(os.environ.get("ORKESTRA_HOME") or "~/.orkestra").expanduser()
    return home / "copilot" / (key + ".json")


def _load_prompt_id(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    try:
        data = json.loads(_prompt_file(session_id).read_text(encoding="utf-8"))
        return str(data.get("prompt_id") or "") or None
    except (OSError, ValueError, AttributeError):
        return None


def _save_prompt_id(session_id: str, prompt_id: str) -> None:
    if not session_id:
        return
    path = _prompt_file(session_id)
    try:
        with file_lock(path):
            write_json_atomic(path, {"prompt_id": prompt_id})
    except OSError as exc:
        dispatch.log("Copilot prompt binding failed: " + str(exc))


def _role_path(session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    home = Path(os.environ.get("ORKESTRA_HOME") or "~/.orkestra").expanduser()
    return home / "copilot" / "roles" / (key + ".json")


def _change_role(session_id: str, role: str, delta: int) -> None:
    if not session_id:
        return
    path = _role_path(session_id)
    try:
        with file_lock(path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            active = data.get("active") if isinstance(data, dict) else {}
            active = active if isinstance(active, dict) else {}
            count = max(0, int(active.get(role, 0)) + delta)
            if count:
                active[role] = count
            else:
                active.pop(role, None)
            if active:
                write_json_atomic(path, {"active": active})
            else:
                path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as exc:
        dispatch.log("Copilot agent role binding failed: " + str(exc))


def _active_role(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    try:
        data = json.loads(_role_path(session_id).read_text(encoding="utf-8"))
        active = data.get("active") if isinstance(data, dict) else None
        roles = [str(role) for role, count in (active or {}).items() if int(count) > 0]
        if len(roles) == 1:
            return roles[0]
        if roles:
            return "orkestra:unknown"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _start_subagent(event: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _copilot_session_id(event.get("session_id") or event.get("sessionId"))
    cwd = str(event.get("cwd") or os.getcwd())
    project = paths.find_project_root(cwd)
    if not session_id or not project:
        return {}
    if not ST.project_active_run(project):
        return {}
    # subagentStart can only add context (a deny is ignored); the PreToolUse deny on `task` is the real gate.
    return {"additionalContext": ONE_CHAT}


def _finish_subagent(event: Dict[str, Any]) -> None:
    session_id = _copilot_session_id(event.get("session_id") or event.get("sessionId"))
    role = _canonical_agent(event.get("agent_type") or event.get("agent_name") or event.get("agent_display_name"))
    _change_role(session_id, role, -1)
    if session_id:
        try:
            (ST.children_dir() / (ST._safe(session_id) + ".json")).unlink(missing_ok=True)
        except OSError as exc:
            dispatch.log("Copilot child binding cleanup failed: " + str(exc))


def _resume_target(event: Dict[str, Any]) -> Optional[Path]:
    prompt = str(event.get("prompt") or "")
    match = RESUME_COMMAND.match(prompt)
    if not match:
        return None
    args = prompt[match.end():].strip().split()
    if args and args[0] == "-p":
        args = args[1:]
    hint = args[0].rstrip(",;:") if args else ""
    if hint:
        candidate = Path(hint).expanduser()
        if not candidate.is_absolute():
            candidate = paths.workspace_root() / candidate
    else:
        candidate = paths.find_project_root(event.get("cwd") or os.getcwd())
        if candidate is None:
            return None
    project = Path(candidate).resolve(strict=False)
    workspace = paths.workspace_root().resolve(strict=False)
    try:
        project.relative_to(workspace)
    except ValueError:
        return None
    return project if project.is_dir() and paths.find_project_root(project) == project else None


def _bind_explicit_resume(event: Dict[str, Any]) -> Optional[str]:
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return "[orkestra] Copilot session id is missing; the run was not bound. Stop before continuing."
    project = _resume_target(event)
    if project is None:
        return "[orkestra] Could not resolve the project from this explicit resume request inside the workspace. " \
               "Pass its project name, then retry; no run state was changed."
    run_id = ST.project_active_run(project)
    if not run_id:
        return None
    existing = ST.session_binding(session_id)
    if existing:
        if Path(existing["project"]) == project and existing["run_id"] == run_id:
            return None
        return "[orkestra] This Copilot session is already bound to another Orkestra run. Stop before continuing."
    lease = float((paths.global_config().get("watchdog") or {}).get("lease_seconds") or 120)
    others = [row for row in ST.run_sessions(project, run_id)
              if row["id"] != ST._safe(session_id) and row["age"] < lease]
    if others:
        return "[orkestra] Another live session is already driving this run. Do not resume here until it is closed."
    if not ST.bind_session(session_id, project, run_id, engine="copilot"):      # validated: a closed run is no run
        return f"[orkestra] Run {run_id} in {project} cannot be bound ({ST.bind_problem(project, run_id)}); no run state was changed."
    dispatch.activity.record(project, run_id, event, "bind", i18n.t("dispatch.copilot_bound"))
    return f"[orkestra] Copilot session bound to {run_id} in {project}. Continue only after the CLI resume command succeeds."


def _result_payload(event: Dict[str, Any], event_name: str) -> Any:
    if event_name == "PostToolUseFailure":
        return {"stderr": str(event.get("error") or "tool failed"), "exit_code": 1}
    result = event.get("tool_result", event.get("tool_response"))
    if not isinstance(result, dict):
        return result
    if "answers" in result or "stdout" in result or "stderr" in result:
        return result
    text = result.get("text_result_for_llm", result.get("textResultForLlm"))
    if not isinstance(text, str):
        return result
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def normalize_event(event_name: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Map the documented VS Code-compatible Copilot payload to dispatch.py's Claude shape."""
    normalized = dict(event)
    normalized["hook_event_name"] = EVENTS.get(event_name, event_name)
    normalized["session_id"] = _copilot_session_id(event.get("session_id") or event.get("sessionId"))
    normalized["_engine"] = "copilot"
    normalized["cwd"] = event.get("cwd") or os.getcwd()
    child = ST.child_binding(normalized["session_id"])
    role = _active_role(normalized["session_id"])
    if child:
        normalized["session_id"] = child["parent"]
        normalized["agent_type"] = role or child["agent_type"]
    elif role:
        normalized["agent_type"] = role
    if event_name == "UserPromptSubmit":
        prompt_id = event.get("prompt_id") or event.get("promptId")
        if not prompt_id:
            stamp = str(event.get("timestamp") or time.time_ns())
            digest = hashlib.sha256((stamp + "\0" + str(event.get("prompt") or "")).encode("utf-8")).hexdigest()[:20]
            prompt_id = "copilot-" + digest
        normalized["prompt_id"] = str(prompt_id)
        _save_prompt_id(normalized["session_id"], normalized["prompt_id"])
    else:
        normalized["prompt_id"] = event.get("prompt_id") or event.get("promptId") or _load_prompt_id(normalized["session_id"])

    if event_name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        tool_name = str(event.get("tool_name") or event.get("toolName") or "")
        normalized["tool_name"] = TOOL_NAMES.get(tool_name.casefold(), tool_name)
        tool_input = event.get("tool_input", event.get("toolArgs"))
        if isinstance(tool_input, str):             # `toolArgs` arrives as a JSON string: the command must stay readable
            try:                                    # (a bind marker counts only in the output of `orkestra start|resume`)
                tool_input = json.loads(tool_input)
            except ValueError:
                tool_input = {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        else:
            tool_input = dict(tool_input)
        if normalized["tool_name"] in ("Agent", "Task"):
            agent = tool_input.get("subagent_type") or tool_input.get("agent") or tool_input.get("agent_name")
            if agent:
                tool_input["subagent_type"] = _canonical_agent(agent)
            if "prompt" not in tool_input:
                tool_input["prompt"] = tool_input.get("task") or tool_input.get("request") or tool_input.get("message") or ""
            if "description" not in tool_input:
                tool_input["description"] = tool_input.get("title") or tool_input.get("summary") or ""
        normalized["tool_input"] = tool_input
        normalized["tool_response"] = _result_payload(event, event_name)
        if event_name == "PostToolUse" and isinstance(normalized["tool_response"], str) \
                and RUN_MARKER.search(normalized["tool_response"]):
            normalized["tool_name"] = "Bash"

    if event_name == "SubagentStop":
        normalized["agent_id"] = event.get("agent_id") or event.get("agentId") or ""
        normalized["agent_type"] = _canonical_agent(event.get("agent_type") or event.get("agent_name") or event.get("agent_display_name"))
        normalized["agent_name"] = event.get("agent_name") or event.get("agentName") or ""
        normalized["last_assistant_message"] = event.get("last_assistant_message") or event.get("response") or ""

    return normalized


def _copilot_output(event_name: str, output: Dict[str, Any]) -> Dict[str, Any]:
    specific = output.get("hookSpecificOutput")
    if isinstance(specific, dict):
        context = specific.get("additionalContext")
        decision = specific.get("permissionDecision")
        reason = specific.get("permissionDecisionReason")
    else:
        context = None
        decision = None
        reason = None
    # Each result carries the flat Copilot CLI shape and the nested `hookSpecificOutput` shape VS Code reads.
    if event_name == "PreToolUse" and decision:
        return deny_output(reason or "Orkestra policy denied this tool call.", decision)
    if context and event_name in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
        return context_output(event_name, context)
    if event_name in ("Stop", "SubagentStop"):
        flat = {key: output[key] for key in ("decision", "reason") if key in output}
        if flat:
            flat["hookSpecificOutput"] = dict(flat, hookEventName=event_name)
        return flat
    return {}


def deny_output(reason: str, decision: str = "deny") -> Dict[str, Any]:
    return {"permissionDecision": decision, "permissionDecisionReason": reason,
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def context_output(event_name: str, context: str) -> Dict[str, Any]:
    name = "PostToolUse" if event_name == "PostToolUseFailure" else event_name
    return {"additionalContext": context, "hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}


def _mention_one_chat(event_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """SessionStart reminds the model to stay in this chat. Copilot drops UserPromptSubmit output, so no reminder there."""
    if event_name != "SessionStart":
        return payload
    prev = str(payload.get("additionalContext") or "")
    if ONE_CHAT in prev:
        return dict(payload)
    return context_output(event_name, (prev + "\n" + ONE_CHAT).strip() if prev else ONE_CHAT)


def handle_event(event_name: str, event: Dict[str, Any]) -> Dict[str, Any]:
    if event_name in ("SubagentStart", "subagentStart"):
        return _start_subagent(event)
    handler_name = EVENTS.get(event_name)
    handler = dispatch.HANDLERS.get(handler_name or "")
    if handler is None:
        return {}
    finish_subagent = event_name == "SubagentStop"
    output: Dict[str, Any] = {}
    try:
        normalized = normalize_event(event_name, event)
        is_explicit_resume = event_name == "UserPromptSubmit" and RESUME_COMMAND.match(
            str(normalized.get("prompt") or "")
        )
        resume_context = _bind_explicit_resume(normalized) if is_explicit_resume else None
        if event_name == "PreToolUse" and normalized.get("tool_name") in ("Agent", "Task"):
            root = paths.find_project_root(str(normalized.get("cwd") or os.getcwd()))
            if root and ST.project_active_run(root):
                return deny_output(ONE_CHAT)
        if event_name == "PreToolUse" and normalized.get("agent_type") and not ST.session_binding(normalized["session_id"]):
            return deny_output("[orkestra:roles] Copilot subagent has no active run binding; tool call blocked.")
        captured = io.StringIO()
        dispatch._ANSWER.update(closed=False, emitted=False)   # this path skips dispatch.main(), which re-opens the answer
        with contextlib.redirect_stdout(captured):
            dispatch.run_with_hook_deadline(lambda: handler(normalized))
        raw = captured.getvalue().strip()
        if not raw:
            base = context_output(event_name, resume_context) if resume_context else {}
            return _mention_one_chat(event_name, base)
        raw_output = json.loads(raw)
        if not isinstance(raw_output, dict):
            raise ValueError("Orkestra hook returned a non-object")
        output = raw_output
        translated = _copilot_output(event_name, output)
        if resume_context:
            translated.update(context_output(event_name, "\n\n".join(
                value for value in (resume_context, translated.get("additionalContext")) if value
            )))
        if event_name == "SubagentStop" and translated.get("decision") == "block":
            finish_subagent = False
        return _mention_one_chat(event_name, translated)
    except dispatch._Deadline as exc:
        dispatch.log("DEADLINE (" + exc.kind + ") in Copilot " + event_name)
        if event_name == "PreToolUse":     # a hook that times out lets the call through unchecked: refuse it
            d = dispatch.deadline_denial(exc)
            return deny_output("[orkestra:" + d.guard + "] " + d.reason)
        return {}
    except Exception as exc:
        import traceback                    # only an error needs it (importing it costs 9 ms)
        dispatch.log("ERROR in Copilot " + event_name + ": " + str(exc) + "\n" + traceback.format_exc())
        if event_name == "PreToolUse":
            return deny_output("[orkestra:complex] Orkestra hook failure; tool call blocked.")
        return {}
    finally:
        if finish_subagent:
            _finish_subagent(event)


def main() -> int:
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    dispatch.utf8_stdio()
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            raise ValueError("Copilot hook payload must be a JSON object")
        event_name = event_name or str(event.get("hook_event_name") or "")
        output = handle_event(event_name, event)
        if output:
            sys.stdout.write(json.dumps(output, ensure_ascii=False))
    except Exception as exc:
        dispatch.log("ERROR parsing Copilot hook " + event_name + ": " + str(exc))
        if event_name == "PreToolUse":
            sys.stdout.write(json.dumps(deny_output("[orkestra:complex] Invalid hook payload; tool call blocked.")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
