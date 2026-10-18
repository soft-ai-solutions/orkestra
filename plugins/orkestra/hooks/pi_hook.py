#!/usr/bin/env python3
"""Translate a Pi extension event into the same PreToolUse / PostToolUse / Stop decision the other engines use.

Pi calls this through extensions/orkestra-guard.js. A chat with no bound run is left alone. A bound run uses
dispatch.py, and a denied tool call is returned as Pi's `{block: true}`.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))

import dispatch
from orkestra_core import engines as EN
from orkestra_core import paths
from orkestra_core import state as ST

TOOL_NAMES = {
    "bash": "Bash",
    "powershell": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "grep": "Grep",
    "find": "Glob",
    "ls": "Glob",
}
_BAD = "Orkestra hook received invalid JSON."


def _global_guards() -> bool:
    cfg = paths.global_config()
    try:
        return bool(((cfg.get("engine") or {}).get("pi") or {}).get("global_guards"))
    except AttributeError:
        return False


def _claude_event(kind: str, event: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(event.get("sessionId") or "")
    tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
    tool_input = dict(tool_input)
    if "path" in tool_input and "file_path" not in tool_input:
        tool_input["file_path"] = tool_input["path"]
    name = TOOL_NAMES.get(str(event.get("toolName") or "").casefold(), str(event.get("toolName") or ""))
    return {
        "hook_event_name": kind,
        "session_id": EN.session_key("pi", raw) if raw else "",
        "_engine": "pi",
        "cwd": event.get("cwd") or "",
        "tool_name": name,
        "tool_input": tool_input,
        "tool_use_id": str(event.get("toolCallId") or ""),
    }


def _run(handler_name: str, event: Dict[str, Any]) -> Dict[str, Any]:
    handler = dispatch.HANDLERS.get(handler_name)
    if handler is None:
        return {}
    dispatch._ANSWER.update(closed=False, emitted=False)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        dispatch.run_with_hook_deadline(lambda: handler(event))
    raw = captured.getvalue().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _denied(output: Dict[str, Any]) -> str:
    specific = output.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
        return str(specific.get("permissionDecisionReason") or "Orkestra policy denied this tool call.")
    if output.get("decision") == "block":
        return str(output.get("reason") or "")
    return ""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or "")
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    return ""


def decide(event: Dict[str, Any]) -> Dict[str, Any]:
    """One Pi event. `block` stops a tool. `continue` asks Pi for one more model turn."""
    kind = str(event.get("event") or "tool_call")
    if kind == "before_settle":
        mapped = _claude_event("Stop", event)
        if not mapped["session_id"] or not ST.session_binding(mapped["session_id"]):
            return {}
        try:
            reason = _denied(_run("Stop", mapped))
        except Exception:
            return {}
        if reason:
            return {"continue": True, "message": reason}
        return {}
    mapped = _claude_event("PreToolUse" if kind == "tool_call" else "PostToolUse", event)
    if kind == "tool_result":
        mapped["tool_response"] = _text(event.get("output"))
        # A bash result can carry the bind marker before any session exists. Do not drop it.
        if mapped["tool_name"] != "Bash" and not ST.session_binding(mapped["session_id"]) and not _global_guards():
            return {}
        try:
            _run("PostToolUse", mapped)
        except Exception:
            return {}
        return {}
    bound = bool(mapped["session_id"] and ST.session_binding(mapped["session_id"]))
    if not bound and not _global_guards():
        return {}
    handler_name = "PreToolUse" if kind == "tool_call" else "PostToolUse"
    try:
        reason = _denied(_run(handler_name, mapped))
    except dispatch._Deadline as exc:
        if kind != "tool_call":
            return {}
        denial = dispatch.deadline_denial(exc)
        return {"block": True, "reason": denial.reason}
    except Exception:
        if kind != "tool_call":
            return {}
        return {"block": True, "reason": "Orkestra hook failed. The tool call was blocked."}
    if kind == "tool_call" and reason:
        return {"block": True, "reason": reason}
    return {}


def decide_raw(raw: bytes) -> Dict[str, Any]:
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"block": True, "reason": _BAD}
    if not isinstance(event, dict):
        return {"block": True, "reason": _BAD}
    return decide(event)


def main() -> int:
    dispatch.utf8_stdio()
    try:
        out = decide_raw(sys.stdin.buffer.read())
    except Exception:
        out = {"block": True, "reason": "Orkestra hook failed. The tool call was blocked."}
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
