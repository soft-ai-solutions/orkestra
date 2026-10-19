"""`orkestra demo`: a local proof of what orkestra does, with no model and nothing outside a temp folder.

It builds a throw-away git project under a temp dir (its own ORKESTRA_HOME, HOME and tool homes, so the user's runs,
config and agent settings are never read or written), then drives the REAL code paths in child processes:

  1. the plugin's hook entry point (`hooks/dispatch.py`, the file the host runs) is sent a `git push --force` and a
     source edit made before the plan is approved; both are denied with the hook's own words;
  2. the real CLI runs a tiny run: init, start, classification, `phase next`, `gate baseline`, `evidence`, `gate run`;
  3. `orkestra watch --once` prints the dashboard of that run.

Nothing here is faked: every line shown under a command is that command's output. The folder is removed at the end
(`--keep` leaves it). Python standard library only; human text lives in messages/cli.py.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict

from . import i18n

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
BIN = PLUGIN_ROOT / "bin" / "orkestra"
DISPATCH = PLUGIN_ROOT / "hooks" / "dispatch.py"
STEP_TIMEOUT = 60.0
MAX_SHOWN_LINES = 14            # a command's output is shown cut to this many lines (the dashboard needs about 12)
MAX_LINE = 200

# What the host's shell would carry when an agent runs this: none of it may reach the demo's children.
_DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "CURSOR_TRACE_ID",
             "CLAUDE_PROJECT_DIR", "ORKESTRA_PROJECT", "ORKESTRA_HEADLESS", "ORKESTRA_APPROVAL_MODE",
             "ORKESTRA_CURSOR_BIN", "ORKESTRA_CLAUDE_BIN", "ORKESTRA_SHIM_DIR")

_APP = "def greet(name):\n    return 'hello ' + name\n"
_TEST = ("import os\nimport sys\nimport unittest\n\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
         "from app import greet\n\n\nclass GreetTest(unittest.TestCase):\n    def test_greet(self):\n"
         "        self.assertEqual(greet('ada'), 'hello ada')\n\n\nif __name__ == '__main__':\n    unittest.main()\n")
_PYPROJECT = "[project]\nname = \"greeter\"\ndescription = \"A tiny greeting library used by the orkestra demo\"\n"


def isolated_env(root: Path, lang: str) -> Dict[str, str]:
    """The environment of every child: all state under `root`, none of the user's homes."""
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    home = root / "home"
    env.update({
        "ORKESTRA_HOME": str(home / "orkestra"),
        "ORKESTRA_WORKSPACE": str(root),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "COPILOT_HOME": str(home / "copilot"),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "ORKESTRA_CURSOR_HOME": str(home / "cursor"),
        "ORKESTRA_PI_HOME": str(home / "pi"),
        "ORKESTRA_NO_DAEMON": "1",
        "ORKESTRA_PARALLEL_CLI": "0",
        "ORKESTRA_LANG": lang,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


class Demo:
    def __init__(self, root: Path, lang: str, out: Callable[[str], None]) -> None:
        self.root = root
        self.proj = root / "greeter"
        self.env = isolated_env(root, lang)
        self.out = out
        self.session = "demo-session"

    # ---------------------------------------------------------------- child processes
    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN), *args], cwd=str(self.proj), env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=STEP_TIMEOUT)

    def hook(self, event: str, payload: dict) -> dict:
        """One hook call exactly as the host makes it: the event JSON on stdin of hooks/dispatch.py."""
        body = dict(payload, hook_event_name=event, cwd=str(self.proj), session_id=self.session)
        cp = subprocess.run([sys.executable, str(DISPATCH), event], input=json.dumps(body), cwd=str(self.proj),
                            env=self.env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=STEP_TIMEOUT)
        try:
            return json.loads(cp.stdout) if cp.stdout.strip() else {}
        except ValueError:
            return {}

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.proj), *args], env=self.env, capture_output=True, check=True,
                       timeout=STEP_TIMEOUT)

    # ---------------------------------------------------------------- output
    def say(self, key: str, **kw: object) -> None:
        self.out(i18n.t(key, **kw))

    def show(self, text: str) -> None:
        lines = [ln.rstrip() for ln in text.strip("\n").splitlines()]
        for ln in lines[:MAX_SHOWN_LINES]:
            self.out("    " + (ln if len(ln) <= MAX_LINE else ln[:MAX_LINE - 3] + "..."))
        if len(lines) > MAX_SHOWN_LINES:
            self.out("    " + i18n.t("cli.demo_more", n=len(lines) - MAX_SHOWN_LINES))

    def command(self, shown: str, cp: subprocess.CompletedProcess) -> None:
        self.out("  $ " + shown)
        self.show((cp.stdout or "") + (cp.stderr or ""))
        if cp.returncode != 0:
            raise DemoError(i18n.t("cli.demo_step_failed", command=shown, code=cp.returncode))

    def verdict(self, shown: str, answer: dict, guard: str) -> None:
        out = answer.get("hookSpecificOutput") or {}
        denied = out.get("permissionDecision") == "deny"
        self.out("  $ " + shown)
        if not denied:
            raise DemoError(i18n.t("cli.demo_not_denied", command=shown))
        reason = str(out.get("permissionDecisionReason") or "")
        first = reason.splitlines()[0] if reason else guard
        self.out("    " + i18n.t("cli.demo_denied", reason=first if len(first) <= MAX_LINE else first[:MAX_LINE - 3] + "..."))

    # ---------------------------------------------------------------- the story
    def build_project(self) -> None:
        (self.proj / "src").mkdir(parents=True)
        (self.proj / "tests").mkdir()
        (self.proj / "src" / "app.py").write_text(_APP, encoding="utf-8")
        (self.proj / "tests" / "test_app.py").write_text(_TEST, encoding="utf-8")
        (self.proj / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (self.root / "home").mkdir(exist_ok=True)
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=demo@example.invalid", "-c", "user.name=orkestra demo", "commit", "-q", "-m", "demo")

    def story(self) -> None:
        from . import runs as R
        self.say("cli.demo_part_run")
        self.command("orkestra init", self.cli("init"))
        # one gate that needs nothing but this Python (the detected ones may need tools this machine lacks)
        cfg = self.proj / ".orkestra" / "config.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["gates"] = [{"name": "unit", "kind": "test", "timeout": 120, "cwd": ".",
                          "cmd": f"{shlex.quote(sys.executable)} -m unittest discover -s tests -q"}]
        cfg.write_text(json.dumps(data), encoding="utf-8")
        request = "greet people in German as well"
        started = self.cli("start", request)
        self.command(f'orkestra start "{request}"', started)
        # the host binds the chat to the run from the marker line the CLI printed (PostToolUse), here the same hook does
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "orkestra start"},
                                  "tool_response": {"stdout": started.stdout, "stderr": ""}})
        guess = R.classification_prefill(request, "M")
        block = (f"\n## {i18n.heading('classification')}\n- Type: {guess['type']}\n- Size: {guess['size']}\n"
                 f"- Risk: {guess['risk']}\n")
        request_md = self.proj / ".orkestra" / "current" / "request.md"
        with request_md.open("a", encoding="utf-8") as fh:
            fh.write(block)
        self.out("  " + i18n.t("cli.demo_classified", type=guess["type"], size=guess["size"], risk=guess["risk"]))

        self.say("cli.demo_part_guards")
        force = "git push " + "--" + "force origin main"
        self.verdict(force, self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": force}}), "git")
        edit = {"file_path": str(self.proj / "src" / "app.py"), "old_string": "hello", "new_string": "hallo"}
        self.verdict("Edit src/app.py (" + i18n.t("cli.demo_before_approval") + ")",
                     self.hook("PreToolUse", {"tool_name": "Edit", "tool_input": edit}), "phase")

        self.say("cli.demo_part_evidence")
        self.command("orkestra phase next", self.cli("phase", "next"))
        (self.proj / ".orkestra" / "current" / "context.md").write_text(
            "# Context\nsrc/app.py holds greet(); tests/test_app.py covers it with unittest.\n" * 4, encoding="utf-8")
        self.command("orkestra gate baseline", self.cli("gate", "baseline"))
        self.command("orkestra evidence tests -- python3 -m unittest discover -s tests -q",
                     self.cli("evidence", "tests", "--", sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"))
        self.command("orkestra gate run", self.cli("gate", "run"))

        self.say("cli.demo_part_watch")
        self.command("orkestra watch --once", self.cli("watch", "--once"))


class DemoError(Exception):
    pass


def run(out: Callable[[str], None], keep: bool = False) -> int:
    """Run the demo, print through `out`, return the exit code (0: every step behaved as promised)."""
    lang = i18n.lang()
    root = Path(tempfile.mkdtemp(prefix="orkestra-demo-")).resolve()
    rc = 0
    try:
        out(i18n.t("cli.demo_intro", path=root))
        demo = Demo(root, lang, out)
        demo.build_project()
        demo.story()
        out("")
        out(i18n.t("cli.demo_done"))
    except (DemoError, subprocess.SubprocessError, OSError) as exc:
        out(i18n.t("cli.demo_failed", error=str(exc)))
        rc = 1
    finally:
        if keep:
            out(i18n.t("cli.demo_kept", path=root))
        else:
            shutil.rmtree(root, ignore_errors=True)
    out(i18n.t("cli.demo_next"))
    return rc
