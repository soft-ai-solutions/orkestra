"""Pi side of Orkestra: install the skills and the tool extension into ~/.pi/agent.

The extension subscribes to Pi's tool_call event and asks hooks/pi_hook.py for the same decision the other
engines use. Never called from hooks. Writes outside a project, so the user runs `orkestra pi install`.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import List, Tuple

from . import engines as EN
from . import i18n

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS = PLUGIN_ROOT / "skills"
MARKER = ".orkestra-managed"
NOTE = (
    "Pi note: `orkestra pi install` also writes the tool_call extension. "
    "A bound run uses the same tool decision as Claude and Cursor. "
    "Drive a run with `orkestra playbook run` and the commands it prints.\n\n"
)
EXTENSION_MARK = "// orkestra-managed\n"


def pi_home() -> Path:
    return Path(os.environ.get("ORKESTRA_PI_HOME") or "~/.pi/agent").expanduser()


def skills_dir() -> Path:
    return pi_home() / "skills"


def extension_path() -> Path:
    return pi_home() / "extensions" / "orkestra-guard.js"


def extension_text() -> str:
    hook = json.dumps(str(PLUGIN_ROOT / "hooks" / "pi_hook.py"))
    body = r'''
import { spawnSync } from "node:child_process";

const HOOK = __HOOK__;

function ask(payload) {
  const run = spawnSync("python3", [HOOK], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 8000,
  });
  if (run.error || run.status) {
    if (payload.event === "tool_call") {
      return { block: true, reason: "Orkestra hook did not answer." };
    }
    return {};
  }
  try {
    const out = JSON.parse(run.stdout || "{}");
    return out && typeof out === "object" ? out : {};
  } catch (err) {
    if (payload.event === "tool_call") {
      return { block: true, reason: "Orkestra hook returned invalid JSON." };
    }
    return {};
  }
}

function textOf(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(textOf).join("\n");
  if (value && typeof value === "object" && typeof value.text === "string") return value.text;
  return "";
}

function sessionId(ctx) {
  if (process.env.ORKESTRA_PI_SESSION) return process.env.ORKESTRA_PI_SESSION;
  const manager = ctx && ctx.sessionManager;
  if (manager && typeof manager.getSessionId === "function") {
    try { return String(manager.getSessionId() || ""); } catch (err) { return ""; }
  }
  return "";
}

export default function (pi) {
  pi.on("tool_call", async (event, ctx) => {
    const out = ask({
      event: "tool_call",
      toolName: event.toolName,
      toolCallId: event.toolCallId,
      input: event.input,
      cwd: ctx && ctx.cwd,
      sessionId: sessionId(ctx),
    });
    if (out.block) return { block: true, reason: String(out.reason || "denied") };
  });
  pi.on("tool_result", async (event, ctx) => {
    ask({
      event: "tool_result",
      toolName: event.toolName,
      toolCallId: event.toolCallId,
      input: event.input,
      output: textOf(event.content),
      cwd: ctx && ctx.cwd,
      sessionId: sessionId(ctx),
    });
  });
  pi.on("agent_before_settle", async (_event, ctx) => {
    const out = ask({ event: "before_settle", cwd: ctx && ctx.cwd, sessionId: sessionId(ctx) });
    if (out.continue && out.message) {
      return {
        continue: true,
        entries: [{ type: "custom_message", customType: "orkestra", content: String(out.message), display: true }],
      };
    }
  });
}
'''
    return EXTENSION_MARK + body.replace("__HOOK__", hook)


def detected() -> bool:
    return shutil.which("pi") is not None or pi_home().is_dir()


def _split_frontmatter(text: str) -> Tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[: end + 5], text[end + 5:].lstrip("\n")
    return "", text


def skill_text(name: str) -> str:
    """Pi's /skill command uses the frontmatter name, so it must be orkestra-<name>."""
    raw = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    front, body = _split_frontmatter(raw)
    skill_name = f"orkestra-{name}"
    if not front:
        return f"---\nname: {skill_name}\n---\n\n" + NOTE + raw
    lines = []
    named = False
    for line in front.splitlines():
        if line.startswith("name:"):
            lines.append(f"name: {skill_name}")
            named = True
        else:
            lines.append(line)
    if not named:
        lines.insert(1, f"name: {skill_name}")
    return "\n".join(lines) + "\n" + NOTE + body


def _ours(directory: Path) -> bool:
    return (directory / MARKER).is_file()


def guards_installed() -> bool:
    path = extension_path()
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == extension_text()


def skills_installed() -> bool:
    root = skills_dir()
    for name in EN.SKILL_NAMES:
        directory = root / f"orkestra-{name}"
        path = directory / "SKILL.md"
        if not _ours(directory) or not path.is_file():
            return False
        if path.read_text(encoding="utf-8") != skill_text(name):
            return False
    return True


def install(dry: bool = False) -> List[str]:
    """Idempotent. Returns what was (or, with dry=True, would be) written."""
    actions: List[str] = []
    root = skills_dir()
    for name in EN.SKILL_NAMES:
        directory = root / f"orkestra-{name}"
        path = directory / "SKILL.md"
        text = skill_text(name)
        if directory.exists() and not _ours(directory):
            actions.append(i18n.t("cli.pi_skill_kept", path=directory))
            continue
        if _ours(directory) and path.is_file() and path.read_text(encoding="utf-8") == text:
            continue
        actions.append(i18n.t("cli.pi_skill_write", path=path))
        if dry:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        path.write_text(text, encoding="utf-8")
        (directory / MARKER).write_text("orkestra\n", encoding="utf-8")
    path = extension_path()
    text = extension_text()
    if path.exists() and not path.read_text(encoding="utf-8").startswith(EXTENSION_MARK):
        actions.append(i18n.t("cli.pi_skill_kept", path=path))
    elif path.is_file() and path.read_text(encoding="utf-8") == text:
        pass
    else:
        actions.append(i18n.t("cli.pi_skill_write", path=path))
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                path.unlink()
            path.write_text(text, encoding="utf-8")
    return actions


def uninstall(dry: bool = False) -> List[str]:
    """Remove only skill directories this installer marked."""
    actions: List[str] = []
    root = skills_dir()
    for name in EN.SKILL_NAMES:
        directory = root / f"orkestra-{name}"
        if not _ours(directory):
            continue
        actions.append(i18n.t("cli.pi_skill_remove", path=directory))
        if dry:
            continue
        shutil.rmtree(directory)
    path = extension_path()
    if path.is_file() and path.read_text(encoding="utf-8").startswith(EXTENSION_MARK):
        actions.append(i18n.t("cli.pi_skill_remove", path=path))
        if not dry:
            path.unlink()
    return actions


def status() -> List[Tuple[object, str, str]]:
    """(True, None) rows. None does not fail the status exit. Skills missing is False."""
    binary = shutil.which("pi")
    return [
        (skills_installed(), "pi skills",
         str(skills_dir()) if skills_installed() else i18n.t("cli.pi_skills_missing")),
        (guards_installed(), "pi extension",
         str(extension_path()) if guards_installed() else i18n.t("cli.pi_extension_missing")),
        (True if binary else None, "pi", binary or i18n.t("cli.pi_binary_missing")),
    ]
