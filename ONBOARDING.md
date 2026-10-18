# Onboarding: Orkestra on your machine

English · [Türkçe](ONBOARDING.tr.md)

Product overview: [README.md](README.md) · one-page reference: [ORKESTRA.md](ORKESTRA.md) · rules for agents that edit
this repository: [AGENTS.md](AGENTS.md).

This page takes a new machine from nothing to a first guarded run on macOS, Linux or Windows.

## 1. Before you start

You need:

- **Python 3.9 or newer**, standard library only. Check it with `python3 --version` (Windows: `py -3 --version`).
- **git**.
- **At least one engine**: Claude Code (`claude`), the Cursor CLI (`agent`), or GitHub Copilot (the `copilot` CLI or the
  VS Code agent). Orkestra drives them; it does not replace them.
- **Windows only**: a POSIX `sh` on PATH for the Claude Code hooks. Git for Windows provides it.
  macOS and Linux have it already.

## 2. Get the code

```bash
git clone https://github.com/soft-ai-solutions/orkestra.git
cd orkestra
```

`orkestra setup` copies the Claude Code plugin from `plugins/orkestra` into `~/.claude/plugins/cache/`; Claude Code runs that copy. Keep the checkout where it is: Cursor and Copilot read it directly.

## 3. Install

macOS and Linux:

```bash
bash ./install.sh --dry-run         # optional: print the plan, change nothing
bash ./install.sh                   # interactive; --yes accepts the defaults, --no-mcp skips optional MCP servers
bash ./install.sh --skip-tests      # skip the installer's own test run (faster; you own the risk)
```

Windows (PowerShell or cmd), native, WSL is not required:

```powershell
.\install.ps1 --dry-run
.\install.ps1
python install.py --yes --skip-tests
```

`install.ps1` forwards every argument to `install.py`, which does the engine setup, the PATH shim and the health check
on every system. It has no optional MCP step and no bridge-token step (`--no-mcp` is accepted and ignored; create the
token with `python bridge/orkestra_bridge.py init`) and no `--refresh-vendor`. If PowerShell refuses to run the script
(execution policy), or `python` opens the Microsoft Store, run `py -3 install.py` instead.
The LaunchAgent (`launchd`) step is macOS-only and is skipped on Linux and Windows.

Without `--skip-tests` the installer first runs the plugin and bridge test suites on your machine (a few minutes; CPU
load changes the time) and stops if anything is red. Then it registers the checkout as the Claude Code marketplace
`orkestra-local`, installs `orkestra@orkestra-local`, runs `orkestra doctor`, offers the Cursor and Copilot setup, adds
the PATH shim and, with `install.sh`, offers the optional MCP servers and the bridge token. Without the Claude Code CLI
it continues with Cursor and Copilot only, and warns when no engine is ready. A failed Claude Code step or a failed
required health check ends the install with exit code 1.

## 4. Put `orkestra` on PATH

`orkestra setup` and the installers write a shim named `orkestra`, but only into a folder that already exists:

| System | Shim | Folder |
| --- | --- | --- |
| macOS, Linux | `orkestra` (symlink) | `~/.local/bin` |
| Windows | `orkestra.cmd` | `%LOCALAPPDATA%\Microsoft\WindowsApps` when it exists (on PATH by default), otherwise `%USERPROFILE%\.local\bin` |

`ORKESTRA_SHIM_DIR` overrides the folder everywhere. If the folder does not exist yet, create it, add it to your PATH
and run setup again. The short command does not exist until the shim does, so start it by path:

```bash
mkdir -p ~/.local/bin               # then add it to PATH in your shell profile
python3 plugins/orkestra/bin/orkestra setup
```

On Windows add `%USERPROFILE%\.local\bin` to the user PATH (Settings, then Environment Variables). Developer Mode is
optional: with it Orkestra can use symlinks for skills and agents, without it it copies files and writes the `.cmd`
shim. Until the shim is on PATH, call the CLI by path: `python3 plugins/orkestra/bin/orkestra <command>`
(Windows: `plugins\orkestra\bin\orkestra.cmd <command>`). Inside a Claude Code session `orkestra` works anyway.

## 5. Enable your engines

```bash
orkestra setup                # detects Claude Code, Cursor and Copilot; asks before enabling each
orkestra setup --yes          # enables every detected engine without questions
```

Per engine, the same thing `setup` does:

```bash
orkestra cursor install       # skills and subagents in ~/.cursor, hook entries merged into ~/.cursor/hooks.json
orkestra cursor status
orkestra copilot install      # Copilot hooks under $COPILOT_HOME (or ~/.copilot)
orkestra copilot status
orkestra pi install           # skills under ~/.pi/agent/skills
orkestra pi status
```

`cursor install --dry-run` shows what it would change; `--copy` copies instead of linking. `cursor uninstall` removes
exactly the entries it added, and a timestamped backup of `hooks.json` is taken first.

`pi install --dry-run` shows the skill files it would write under `~/.pi/agent/skills` and the extension under
`~/.pi/agent/extensions`. `pi uninstall` removes only the directories and the extension it marked. The extension
gates tool calls on a bound run. `orkestra run --engine pi` starts Pi with `/skill:orkestra-run` once the skills
and the extension are installed.

### Copilot host enable (manual)

`orkestra copilot install` writes Orkestra hook entries for the Copilot plugin. This repository has **no supported API**
to flip the host's own toggles (the VS Code GitHub Copilot agent plugin, session hooks, the model picker). After install:

1. Confirm `orkestra copilot status` reports hooks active.
2. In the Copilot host (VS Code and/or the Copilot CLI), enable the Orkestra agent plugin and its lifecycle hooks
   yourself, so that `SessionStart`, `UserPromptSubmit`, `PreToolUse` and `PostToolUse` run `copilot_hook.py`.
3. Do not start or resume a guarded Orkestra run from an unhooked Copilot session.

If a future host exposes a stable enable API, document it here; until then treat host enable as a manual step.

## 6. Check the machine

```bash
orkestra doctor
```

It exits 0 when the required checks pass and at least one engine is ready, and 1 otherwise. `[OK]` lines passed, `[--]`
lines are optional or informational (MCP servers, an engine you do not use). The rows to look at first:

- `hook self-test (deny force-push)`: the hook really denies a forbidden command. If this fails, the guards are not
  active, so do not start runs until it is fixed.
- `python3 on PATH (plugin hooks)`: informational. The Claude Code hooks start through `sh hooks/run_hook.sh`, which
  tries `python3`, `python` and `py -3`; on Windows this row may say `not found` while `python` or `py` still works.
- `engine ready`: at least one of Claude Code, Cursor hooks, Copilot hooks.
- `language`: which language Orkestra will use and why (next step).

## 7. Pick a language

English is the default, and Orkestra follows your OS locale unless you say otherwise.

```bash
orkestra lang show            # the language in use and where it comes from
orkestra lang tr              # save Turkish in ~/.orkestra/config.json  (en | tr | auto)
```

For a single command use `--lang tr`; for a shell session `ORKESTRA_LANG=tr`. The order is in the README
([Language](README.md#language)).

## 8. First run

Projects are looked up by name under the workspace folder, `~/Projects` by default. Change it with `"workspace_root"`
in `~/.orkestra/config.json` or the `ORKESTRA_WORKSPACE` environment variable; a path always works too.

```bash
orkestra init -p <project>    # optional: create .orkestra/ and detect the test, lint, typecheck and build gates
orkestra run -p <project> "describe the smallest safe change you would make"
orkestra watch                # in a second terminal: phase, what it does now, who it waits for
```

In an agent chat the same request is `/orkestra:run <request>` (Cursor: `/orkestra-run`). To stop, use the host's stop
control, `orkestra pause -p <project>` or `orkestra abort -p <project> --reason "..."`; project files stay as they are.

## Updating and uninstalling

Update with `git pull` in the checkout, then bump the version and run `orkestra setup`: Claude Code runs the copy cached
under `~/.claude/plugins/cache/`, keyed by the version, so a pull alone changes nothing there (Cursor and Copilot read
the checkout). Agent, skill and `hooks.json` changes apply in a newly opened Claude Code session. Check the Cursor side with
`orkestra cursor status`, and restart the bridge if you use it (it is a long-lived process).

```bash
bash ./install.sh --uninstall           # macOS and Linux; add --purge to delete ~/.orkestra as well
python install.py --uninstall --dry-run # any system: print what would be removed first
```

Uninstall removes the Claude Code plugin and marketplace, the Cursor and Copilot hook entries, the PATH shim and the
macOS bridge LaunchAgent. It leaves the MCP servers `install.sh` added alone (remove them with `claude mcp remove <name>`).
`--purge` deletes the Orkestra home (settings, run history, bridge token) only after you confirm or pass `--yes`. The
`.orkestra/` folders inside your projects are never touched. A `hooks.json` that no longer parses stops the uninstall
with exit code 1 and changes nothing: fix the file and run it again.

## When something is off

| Symptom | What to do |
| --- | --- |
| `orkestra: command not found` | Section 4: create the shim folder, put it on PATH, run `python3 plugins/orkestra/bin/orkestra setup` again. |
| `doctor` fails the hook self-test | Re-run `orkestra setup`; on Windows check that `sh` and a real Python 3.9+ (not the Store stub) are on PATH. |
| Cursor or Copilot guards do not fire | `orkestra cursor status` / `orkestra copilot status`, then open a **new** chat; for Copilot enable the host plugin (section 5). |
| The installer stops on a red test | Open an issue with the failing test and the output of `orkestra doctor`. `--skip-tests` skips it at your own risk. |
| Output is in the wrong language | `orkestra lang show`, then `orkestra lang en` or `orkestra lang tr`. |

More in the README: [FAQ and troubleshooting](README.md#faq-and-troubleshooting).
