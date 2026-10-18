# Orkestra

English · [Türkçe](README.tr.md)

**You describe a change. Orkestra walks an AI coding agent through the same local steps every time, and blocks
unsafe actions unless you ask. It runs on Claude Code, Cursor, and GitHub Copilot.**

Version 1.6.0 · [changelog](CHANGELOG.md) · [MIT License](LICENSE) · Python 3.9+ standard library only ·
macOS, Linux and Windows

[What it is](#what-it-is) · [Quickstart](#quickstart) · [Engines and platforms](#engines-and-platforms) ·
[How a run flows](#how-a-run-flows) · [Safety guards](#safety-guards) · [Watching runs](#watching-runs) ·
[Project memory](#project-memory) · [Bridge](#bridge-telegram-n8n-and-openclaw) · [Language](#language) ·
[Configuration](#configuration) · [FAQ](#faq-and-troubleshooting) · [Limits](#limits-you-should-know) ·
[Tests and CI](#tests-and-ci) · [Docs](#documentation)

## What it is

**Purpose.** Orkestra is a local pipeline for a development request. It learns the project, asks only the questions
that would change the result, writes a spec and a plan, implements with tests, checks that existing behavior still
passes, and writes a delivery report.

**How you use it.** Clone this repository, run setup once, then start a run from your agent chat or from a terminal
inside the project. The exact commands are in [Quickstart](#quickstart). After that, `orkestra watch` shows the run
and `orkestra next` names the single next step.

**Legal.** The code is [MIT License](LICENSE). Copyright 2026 Orkestra contributors. Orkestra is not a product of
Anthropic, Anysphere, or GitHub, and Claude Code, Cursor, and GitHub Copilot are their owners' products. It is not a
sandbox and not a hosted service: the agent runs as you, on your files, and you remain responsible for what it does.
The license gives no warranty. Report a vulnerability in private through
[GitHub Security Advisories](https://github.com/soft-ai-solutions/orkestra/security/advisories/new), as
[SECURITY.md](SECURITY.md) describes. Third-party code is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Orkestra takes a development request through a repeatable path: discovery, a reviewed plan, TDD implementation,
no-regression gates, independent review and a delivery report. Autopilot is the default: it resolves ordinary
ambiguity from the request and the repository, records its assumptions, validates the plan and keeps going. A real
plan-approval question is asked only when you explicitly choose manual mode. It never commits, pushes, deploys or
uploads to a store unless you ask for that in the message.

The point is where the rules live. The state machine, the playbooks and the allow/deny decisions are code and host
hooks, not only a prompt the model can ignore. That reduces variance across models and sessions; it does not make an
agent infallible (see [Limits](#limits-you-should-know)).

```
/orkestra:run add CSV export to the order list          (Claude Code and GitHub Copilot)
/orkestra-run add CSV export to the order list          (Cursor)
```

**Orkestra is**

- a command line tool, `orkestra`, that keeps the state of a run: ten phases, tasks, red/green test evidence, gates;
- hooks for Claude Code, Cursor and Copilot that check every tool call against that state and can deny it before it runs;
- skills and eight subagents (scout, researcher, architect, plan-critic, implementer, reviewer, adversary, verifier)
  that do the work of each phase;
- a per-project memory under `.orkestra/`, so the next run starts informed;
- optionally, a loopback HTTP bridge so Telegram, n8n or OpenClaw can start headless runs.

**Orkestra is not**

- a sandbox: the agent still runs as you, on your files (see [Limits](#limits-you-should-know));
- a hosted service: one machine, one user, no telemetry;
- a model or an agent: it needs Claude Code, the Cursor CLI or the Copilot CLI/VS Code agent to do the work;
- a replacement for your tests: where a project has none, the regression net is missing and the report says so;
- an automatic committer or deployer: git writes, deploys and store uploads happen only when you ask.

What makes the result depend less on how strong the model is:

- **The state machine is code.** `orkestra phase next` checks the current spec and plan hash, the critic verdict,
  red/green evidence and the gates before a phase can change. Autopilot records a policy approval only for a current,
  reviewed plan; manual mode needs your real reply.
- **Guards are hooks.** Source edits before the plan gate, `git commit` or `push` you did not ask for, reading `.env*`
  and key files, destructive commands and a subagent writing outside its role are denied before the call runs.
- **The next step is computed.** `orkestra next` prints the one action to take now, and only the playbook of the
  current phase is loaded, which keeps the context small.
- **Everything is inspectable and reversible.** An activity log, a live view (`orkestra watch`), a watchdog for stalled
  runs, and a snapshot before the first edit (`/orkestra:rollback`).

## Quickstart

You need Python 3.9 or newer, git, and at least one of Claude Code, the Cursor CLI (`agent`) or the Copilot CLI
(`copilot`). Nothing to `pip install`.

```bash
git clone https://github.com/soft-ai-solutions/orkestra.git
cd orkestra
python3 plugins/orkestra/bin/orkestra setup      # detects Claude Code, Cursor and Copilot and asks before enabling each (--yes: no questions)
python3 plugins/orkestra/bin/orkestra doctor     # exit 0 when the required checks pass and at least one engine is ready
```

Then start a run, from the chat of your agent or from a terminal inside the project (until the `orkestra` shim is on
your PATH, write `python3 plugins/orkestra/bin/orkestra` instead):

```bash
orkestra run "add CSV export to the order list"
```

In a Claude Code or Copilot chat type `/orkestra:run <request>`, in Cursor `/orkestra-run <request>`. Follow it with
`orkestra watch`.

The full installer does the same and more: it runs the plugin and bridge test suites on your machine first (a few
minutes, and it stops if anything is red), registers the checkout as a Claude Code marketplace
(`orkestra@orkestra-local`; Claude Code runs the copy it caches from `plugins/orkestra`), installs the Cursor and Copilot hooks you
accept and, with `install.sh` only, adds optional MCP servers and creates the bridge token.

```bash
bash ./install.sh                  # interactive (macOS and Linux)
bash ./install.sh --yes --skip-tests   # accept the defaults, skip the test suites (you own the risk)
bash ./install.sh --dry-run        # print the plan and change nothing
bash ./install.sh --uninstall      # undo the registrations (add --purge to delete ~/.orkestra and its bridge token too)
```

On Windows use `.\install.ps1` or `python install.py`, not `install.sh`. It takes the same flags except
`--refresh-vendor` (`install.sh` only), accepts `--no-mcp` and ignores it, and has no MCP or bridge-token step
(create the token with `python bridge/orkestra_bridge.py init`):

```powershell
.\install.ps1
python install.py --dry-run
```

`setup` and the installers put an `orkestra` shim on your PATH, but only into a folder that already exists:
`~/.local/bin` (or the folder in `ORKESTRA_SHIM_DIR`). If `orkestra` is not found afterwards, see the
[FAQ](#faq-and-troubleshooting). Inside a Claude Code session the command is available anyway.

First-time details, including Windows and Copilot host settings: [ONBOARDING.md](ONBOARDING.md).

## Engines and platforms

A run is driven by the engine that starts it. The terminal default is Claude Code; Cursor and Copilot runs carry their
engine in the run state, and the engine that started a run is the one that resumes it.

| | Claude Code | Cursor | GitHub Copilot | Pi |
| --- | --- | --- | --- | --- |
| In the chat | `/orkestra:run <request>` | `/orkestra-run <request>` | `/orkestra:run <request>` | `/skill:orkestra-run <request>` |
| From a terminal | `orkestra run -p <project> "<request>"` | `orkestra run --engine cursor -p <project> "<request>"` | `orkestra run --engine copilot -p <project> "<request>"` | `orkestra run --engine pi -p <project> "<request>"` |
| From Telegram | `/run <project> <request>` | `/run cursor <project> <request>` | `/run copilot: <project> <request>` | `/run pi: <project> <request>` |
| Bridge API | `{"project": …, "request": …}` | add `"engine": "cursor"` | add `"engine": "copilot"` | add `"engine": "pi"` |
| Status, continue | `/orkestra:status`, `/orkestra:resume` | `/orkestra-status`, `/orkestra-resume` | `/orkestra:status`, `/orkestra:resume` | `/skill:orkestra-status`, `/skill:orkestra-resume` |
| Undo a run's file changes | `/orkestra:rollback` | `/orkestra-rollback` | `/orkestra:rollback` | `/skill:orkestra-rollback` |
| Commit and push (only you call it) | `/orkestra:ship` | `/orkestra-ship` | `/orkestra:ship` | `/skill:orkestra-ship` |

- **Claude Code**: `orkestra setup` installs a copy of the plugin into `~/.claude/plugins/cache/`; after a `git pull` bump the version and run `orkestra setup` again, otherwise the cached copy keeps running.
- **Cursor**: `orkestra cursor install` links skills and subagents into `~/.cursor` and appends its entries to
  `~/.cursor/hooks.json` without touching yours (after a timestamped backup); `orkestra cursor uninstall` removes
  exactly those. Only chats that carry an Orkestra run are guarded; set `engine.cursor.global_guards` to apply the
  every-session guards to all Cursor chats.
- **Pi**: `orkestra pi install` writes the Orkestra skills under `~/.pi/agent/skills` and the tool extension under `~/.pi/agent/extensions`. `orkestra pi status` and `orkestra pi uninstall` match the Cursor install surface. The extension sends each tool call through the same decision Claude and Cursor use, and a bound run continues to the next step when the turn would otherwise stop. In chat the command is `/skill:orkestra-run`. From a terminal, `orkestra run --engine pi` refuses until the skills and the extension are installed, then starts Pi with that short command instead of pasting the whole playbook.
- **GitHub Copilot** (CLI and VS Code): `orkestra copilot install` writes the hook entries; you still enable the
  Orkestra agent plugin and its lifecycle hooks in the host yourself. An unhooked Copilot session must not start a
  guarded run. Copilot CLI launches default to `gpt-6-luna` with maximum reasoning effort; a supported model or effort
  named in the request overrides that. VS Code agents inherit the model you picked in the chat.

| Platform | Support |
| --- | --- |
| macOS, Linux | Fully supported. Desktop notifications: `osascript` on macOS, `notify-send` on Linux. |
| Windows | Native, WSL not required, less exercised than the other two (see below). |
| Python | 3.9 to 3.14, standard library only. |

**Windows.** Use `.\install.ps1` or `python install.py`. The LaunchAgent (`launchd`) step is macOS-only and skipped.
The PATH shim is `orkestra.cmd`, written to `%LOCALAPPDATA%\Microsoft\WindowsApps` when that folder exists, otherwise
to `%USERPROFILE%\.local\bin` (create it and add it to PATH); `ORKESTRA_SHIM_DIR` overrides both. Developer Mode is
optional: with it Orkestra can use symlinks, without it it copies files and writes the `.cmd` shim. The Claude Code
hooks start through `sh hooks/run_hook.sh`, which tries `python3`, `python` and `py -3` and skips the Microsoft Store
stub, so `sh` (Git Bash) must be on the PATH of the host that runs hooks. The watchdog only notifies there (no
auto-resume), and there is no desktop toast; use the webhook or the bridge. None of this was run on a real Windows
machine by the maintainers; CI runs both test suites on `windows-latest`.

## How a run flows

```
intake → discover → clarify → plan → approve → implement → verify → review → deliver → done
```

Every transition is `orkestra phase next`, and the CLI refuses it until the conditions of the phase hold.

| Phase | What happens | Moves on when (checked by code) |
| --- | --- | --- |
| intake | The request is restated, typed and sized (XS, S, M, L). | `request.md` exists. |
| discover | Scouts map the code, a researcher checks current docs, baseline gates are recorded. | `context.md` is filled. The baseline gates (run in the background) must be recorded before the move to implement. |
| clarify | Only questions that change the outcome are asked (at most 4 per call). `spec.md` is written. | `spec.md` has a Clarifications section and FR/SC ids. |
| plan | The architect writes `plan.md` with atomic TDD tasks; the plan-critic tries to break it. | `orkestra plan check` is clean and the critic says PASS. |
| approve | Autopilot records a policy approval of the reviewed plan; manual mode asks you. | An approval is recorded and the spec and plan have not changed since. |
| implement | Each task: failing test (red), smallest code, passing test (green), task review. | All tasks are closed, each TDD task with green evidence. |
| verify | `orkestra gate verify` re-runs every gate; the verifier ties each requirement to evidence. | No regression, no file changed after the last gate run, `verification.md` says PASS. |
| review | Reviewer and adversary look independently; every finding is fixed, deferred or rejected. | `reviews/triage.md` exists with no open item and the gates are green again. |
| deliver | `report.md` is written (the CLI builds its evidence block) and project memory is updated. | The report exists, its evidence block is untouched, `FEATURES.md` has the entry. |

The parts you may see: the clarifying questions, and the plan approval in manual mode. A task whose review says PASS
is not reopened for minor notes; a third FAIL on the same task is put to you. At every step `orkestra next` names the
single next action, so an interrupted run resumes from the sub-step it was in (`/orkestra:resume`).

## Safety guards

Guards are hook decisions on tool calls (Bash, Read, Edit, Write and the MCP editing tools). The first group applies in
every session where the hooks are active, the second only in a session bound to a run.

| Guard | Applies | What it denies |
| --- | --- | --- |
| `git` | every session | `add`, `commit`, `push`, `merge`, `reset`, `stash`, branch changes and `gh` writes unless the current message asks for them (`commit this`, `commitle`). Never: hook bypass, force-push to main, `git config` or remote changes. The permission is for that message only. |
| `secrets` | every session | Reading, writing, moving or deleting `.env*`, `*.pem`, `*.p8`, `*.mobileprovision`, private keys, service-account JSON, `~/.aws/credentials`, `key.properties` and token-bearing `.npmrc`, through Read, Grep, Edit, Write and Bash redirects. |
| `store` | every session | `eas build` (without `--local`), `eas submit`, `eas update`, `expo publish`. |
| `home` | every session | Writes to `~/.orkestra` (guard settings, grants, bindings). |
| `rollback` | every session | `orkestra rollback --yes` unless you asked for the rollback in that message. |
| `complex`, `busy` | every session | A command too large to inspect safely in one hook call (6 s of CPU), or a call the hook could not check in 9 s on a busy machine (retry the same call once). Both refuse: a hook that timed out would let the call run unchecked. |
| `phase` | run | Source edits before the plan is approved; `spec.md` is frozen after approval. |
| `isolation` | run | Writes outside the run's project (temp folders are exempt). |
| `deps` | run | Installing a dependency the approved plan does not list. |
| `deploy` | run | Deploy, publish, production migrations, `ssh`, remote `rsync`. |
| `destructive` | run | Recursive deletion (`rm -r`) with targets it cannot name, or anywhere but a dedicated temporary subfolder. |
| `roles`, `internals` | run | A read-only agent writing anything but its report; hand-written or scripted writes to machine state; an answer embedded in an approval question; pause/abort/resume/unblock/block/await of a run another chat drives (the user's own message lifts it). Every run-guard denial ends with an `unblock:` line. |
| `wait` | run | Sleeping or polling for a subagent result (the report arrives as a new message). |
| `dialogs` | run | Commands that open a system dialog on your screen (keychain prompts, `osascript` dialogs). |
| `Stop` | run | Ending the turn while a run is active is answered with the next step (at most twice per state of progress and six times per 30 minutes); allowed when you are waited for, when truly blocked, or while a subagent runs. |

The nine named guards (`git`, `secrets`, `store`, `isolation`, `deploy`, `deps`, `phase`, `wait`, `dialogs`) can be
switched off one by one under `guards` in `~/.orkestra/config.json`. Evidence rules sit on top: a test cannot change
between red and green, deleted tests and added `skip`/`only`/`xfail`/`@ts-ignore` are scanned, `verification.md` cannot
cite a file or test that does not exist, and the CLI writes the evidence block of the report from machine data.
Text that arrives as a subagent report is not you: it never grants git, approves a plan or answers a question.

## Watching runs

| What | Command | Notes |
| --- | --- | --- |
| Live terminal view | `orkestra watch` | Refreshes every 2 s; `--once`, `--all` (finished runs too), `-p <project>`, `--json`. |
| Browser view | `orkestra dashboard --open` | Read-only, `127.0.0.1:47616`. |
| The one next step | `orkestra next -p <project>` | At most 3 lines; the same state always gives the same answer. |
| Where the time went | `orkestra tempo -p <project>` | Phase and task durations, review rejects, measured dead wait. |
| Scan from outside | `orkestra watchdog` | Notifies once per new event; `--dry-run`, `--launchd`, and `--status` for the fast watcher (see below). |
| Background subagents | `orkestra agents -p <project>` | `agents clear` drops records of agents that are gone. |

Every run shows its phase, the time in it, the current state (working, running a command, silent, **waiting for you**,
blocked, session may have closed), task and evidence progress, gate results and the latest tool calls. `watch` and the
watchdog share one classifier with five stall classes, each a plain predicate and none a guess about the model: waiting for
you (legitimate), dead wait (a stopped subagent's report is 60 s overdue and the whole run was silent), hung command
(open for 15 min, 30 min for gates, while no agent wrote any row since it started), silent session (10 min without a
record) and loop (the same task in a third review round, or the same guard denial or command three times). Thresholds and
`auto_resume` (`off`, `headless` as default, `all`; at most 3 times) live under `watchdog` in `~/.orkestra/config.json`:
by default only a headless run from the bridge is resumed on its own, an interactive chat is only notified.

## Reliability and speed

A run should not freeze, loop or burn tokens while it waits. Everything below is a plain predicate on files, clocks and
counters: no model is asked and no tokens are spent. A thinking model is not a stall, so "the model has been quiet" is
never a reason to act.

**Who may steer a run**
- A chat is bound to a run only when the main chat itself ran `orkestra start` or `orkestra resume`, the marker it
  printed names this machine's `ORKESTRA_HOME`, and the run exists and is open. A subagent, a `cat` of a log or a sandbox
  copy of the CLI that prints the same line binds nothing.
- A live binding is kept (one ledger line, one warning) instead of being taken over; only an explicit signal unbinds
  (`pause`, `abort`, `phase next`), never a neutral message.
- `pause`, `abort`, `resume`, `unblock`, `block` and `await` of a run another chat drives are refused unless your own
  message asks for it; a driver counts as alive for `watchdog.lease_seconds` (120 s). Every run-guard refusal ends with an
  `unblock:` line that names the way out.

**Caps: no unbounded loops**
- The same refusal escalates: the 2nd adds a note, the 3rd says "do not retry" and names the alternative, the 8th within
  10 minutes blocks the run. The decision never changes; the 20th attempt is refused like the 1st.
- A task's third failed review round goes to you; in autopilot it blocks the task and `--force` cannot lift it. When the
  same agent was launched `tempo.max_dispatches` (3) times for the same task or phase without new evidence,
  `orkestra next` answers `escalate` instead of launching it again.
- A bridge turn has a wall-clock budget (`bridge.turn_timeout_seconds`, 2 h) and an idle limit
  (`bridge.turn_idle_seconds`, 30 min); `headless.max_budget_usd` is an optional spend ceiling and off by default.

**Nothing waits for ever**
- A lock is waited for 2 s in a hook (the hook then carries on and logs it) and 10 s in the CLI, with an error that names
  the holder (pid, command, age); `ORKESTRA_LOCK_TIMEOUT` overrides both.
- A hook has one deadline: 6 s of CPU or 9 s of wall clock (the host kills it at 15 s). A PreToolUse call that hits it is
  denied, every other event ends quietly. Git calls are time-boxed too.
- In a headless run, `orkestra wait` waits inside the process for the subagents (default cap 540 s, below the Bash tool
  limit) and prints the next step; it costs no model turn. An interactive session never blocks there: it ends its turn and
  the report arrives as a message.

**Gates**
- Each gate runs in its own process group with a streaming log. A timeout (`timeout`, 900 s by default) or an idle stall
  (`idle_timeout`, off unless set) stops the whole group, so no orphan keeps a run waiting.
- `gate verify` does not re-run a gate whose baseline run timed out or took 120 s or more (the regression rule cannot
  blame the run for it; the table says so), carries a gate with `paths` forward when none of its files changed, and
  runs typecheck, lint and test gates side by side when `gates_parallel` is above 1. `--resume` continues an interrupted
  verify for the same change; `--all` (or `gates_rerun_failed_baseline`) runs everything in full.

**The fast watcher**
- One watcher per machine checks every open run every 5 s (1 s when a stall is about to be declared) and spends no
  tokens. It starts from the next session start on macOS and Linux (`watchdog.autostart`; `ORKESTRA_NO_DAEMON=1` turns it
  off), the bridge hosts it while it runs, and it exits after `watchdog.daemon_idle_minutes` (30) with no open run.
- It sees: the same refusal over and over, pause/resume flipping, a paused run whose agents still work, an overdue
  subagent report, an autopilot run whose turn ended with work left, a permission prompt nobody answered, a hung command,
  a model/API error, a dead session and a hooks failure streak. Latency is the predicate's own settle time plus one tick:
  seconds for a refusal loop or a flip storm, 20 to 60 s for an idle turn, a permission prompt or an overdue report, and
  minutes only where the limit itself is minutes (a command).
- It notifies once per incident (then after 30 min, 2 h, 8 h and a day, then never). It repairs only records that are
  provably stale: a binding to a closed run, an open command a later row outlived, subagents that are gone. It resumes
  a headless run at most 3 times (after 10 s, 60 s and 300 s) and never while that session's process is alive.
- It cannot: start by itself on Windows, answer a permission prompt or a question for you, kill a process it did not start, or tell
  slow work from a stuck model that is still running a command. After 3 failed ticks it stops repairing and only
  notifies. Its trail is `~/.orkestra/logs/watchdog.log`.

```bash
orkestra watchdog --status                  # is the fast watcher running, which runs does it flag
orkestra watchdog --daemon                  # start it now
orkestra wait --max 300 -p <project>        # headless: wait without tokens
orkestra gate verify --resume -p <project>  # continue an interrupted verify
orkestra brief --review T3 -p <project>     # the reviewer's packet for one task
```

## Project memory

Every project gets a `.orkestra/` folder, created by `orkestra init` (or by the first run):

```
.orkestra/
  config.json            gate commands (test, lint, typecheck, build)
  memory/                PROJECT.md, FEATURES.md, DECISIONS.md, LEARNINGS.md, specs/
  features/NNN-slug/     request, context, spec, plan, ledger, task reports, reviews, verification, report
  runs/                  machine state, backups, evidence logs (git-ignored)
```

Memory opens in layers: a summary first, details only when needed. Subagents get a task brief, not the whole plan, and
write detail to files, returning a few lines. `/orkestra:onboard` builds or refreshes the verified project memory.

## Bridge: Telegram, n8n and OpenClaw

`bridge/orkestra_bridge.py` is a separate, optional process on `127.0.0.1`. It turns a message from Telegram, n8n or
OpenClaw into a headless run; the same hooks apply and plan approval is recorded only from your reply.

```bash
python3 bridge/orkestra_bridge.py telegram-setup     # connects a Telegram bot (asks for the BotFather token)
python3 bridge/orkestra_bridge.py serve              # on macOS, `launchd` starts it at login instead
python3 bridge/orkestra_bridge.py doctor --live      # checks config, claude, plugin, bridge and Telegram
```

In Telegram: `/run <project> <request>` starts a run, plain text answers its questions, `approve` approves a plan,
`/status` shows jobs and the live state, `/cancel` stops the latest job, `/help` lists the commands. The Turkish names
`/yap`, `/durum`, `/iptal`, `/yardim` work too. The bridge long-polls Telegram, so no public URL is needed, and only the
chat captured at setup may write. With the bridge running, `http://127.0.0.1:47615/` serves a local chat workbench:
pick a project and an engine (Claude Code, Cursor or Copilot), start a request, answer questions, stop a run and watch a
live flow map of phases, subagents, tasks, gates and hook activity. It asks for the bridge token and keeps it in page
memory only.

- **n8n**: `bridge/n8n/` holds two workflows; `python3 bridge/n8n_import.py` imports them using `N8N_URL` and
  `N8N_API_KEY` (or `N8N_STATE`). `POST /webhook/orkestra-job` starts a job and needs the `X-Orkestra-Key` header.
- **OpenClaw**: do not give OpenClaw's Telegram bot token to the bridge (a token has one consumer). Call
  `bridge/orkestra_client.py` or the HTTP API (`POST /jobs`, `GET /runs`) instead; see
  [integrations/openclaw/CONTRACT.md](integrations/openclaw/CONTRACT.md).
- The machine that runs the bridge must be awake and online for Telegram-started runs.

## Language

Orkestra speaks English and Turkish; English is the default. Everything meant for a human (`watch` and the dashboard,
activity and notification lines, run artifacts and document templates, the installer, bridge replies, the local web UI,
`--help`) follows one language, resolved in this order (first hit wins):

1. `--lang en|tr|auto` on the command line (before or after the subcommand);
2. the `ORKESTRA_LANG` environment variable (`en`, `tr` or `auto`);
3. the `lang` key of `~/.orkestra/config.json` (`en`, `tr` or `auto`; absent means `auto`);
4. the OS locale (`LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`; on Windows, when none of them is set, the user locale);
5. English.

```bash
orkestra lang show      # the language in use and where it comes from
orkestra lang tr        # save Turkish in ~/.orkestra/config.json  (en | tr | auto)
orkestra --lang tr watch --once
ORKESTRA_LANG=en orkestra doctor
```

What stays the same in both languages:

- Text the model reads (skills, agents, playbooks, `orkestra next` hints, guard messages) is English.
- What you type is understood in both: `commit this` / `commitle`, `approve` / `onay`. The plan-approval and rollback
  questions use fixed wording in both languages (`Plan approval` / `Plan onayı`, `Rollback` / `Geri alma`), and every
  recognizer also accepts the older Turkish text.
- The section headings of `request.md`, `spec.md`, `plan.md` and `report.md` exist in both languages and every parser
  accepts both spellings, so a run started in one language can be continued in the other and old Turkish runs keep
  working.
- Machine-readable output keeps its keys and values (`--json`, the bridge API, state files). `orkestra watch --json`
  adds `phase_label` and `status_label` in the active language; `phase_tr` and `status_tr` stay Turkish for the
  OpenClaw plugin. The same goes for the `language:` and `source:` lines of `orkestra lang show` and for the
  `[OK]` / `[--]` / `[!!]` row names of `doctor` and the Cursor and Copilot `status` (scripts read them; the details and
  summary lines next to them are translated).
- The run ledger (`ledger.md`) is an audit log of what the CLI did, always written in English.

The bridge answers Telegram in the same language (`GET /health` reports it as `lang`); the local web UI has a language
button and honours `?lang=tr`. Contributors: how to add or change text is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Configuration

Global settings live in `~/.orkestra/config.json` (the folder moves with `ORKESTRA_HOME`); a project's gate commands
live in `<project>/.orkestra/config.json`. The keys you are most likely to touch:

| Key | Default | Meaning |
| --- | --- | --- |
| `workspace_root` | `~/Projects` | Where projects are looked up by name (also `ORKESTRA_WORKSPACE`). |
| `lang` | `auto` | Language of human-facing text: `auto`, `en` or `tr`. |
| `guards.<name>` | `true` | Switch one of the nine guards off. `guards.test_syntax` is opt-in and off. |
| `engine.default` | `claude` | The engine for runs started without an explicit choice (`claude`, `cursor`, `copilot`). |
| `watchdog.auto_resume` | `headless` | `off`, `headless` or `all`; `max_auto_resumes` is 3. |
| `tempo.task_review_required` | `true` | On M and L runs `task done` needs a PASS review of that round. |
| `tempo.max_dispatches` | `3` | The same agent launched this often for the same task without new evidence makes `orkestra next` answer `escalate`. |
| `watchdog.autostart` | `true` | SessionStart may start the fast watcher; `daemon_idle_minutes` (30) is how long it lingers without an open run. |
| `gates_parallel` | `2` | Above 1, typecheck, lint and test gates run side by side (at most half the CPUs). |
| `cleanup_artifacts` | `true` | When a run reaches `done`, remove regenerable dumps under the project (`test-results`, `playwright-report`, `android/**/build`, …). Manual: `orkestra clean`. |

## FAQ and troubleshooting

Start with `orkestra doctor`. It checks Python, git, the hooks (it feeds a forbidden command to the hook and expects a
deny), the engines and the language, and exits 1 only when a required check fails. Lines marked `[--]` are optional
or informational.

**`orkestra: command not found`.** The PATH shim is written only into a folder that exists. Create it
(`mkdir -p ~/.local/bin`), put it on PATH, then run `python3 plugins/orkestra/bin/orkestra setup` again (the short
command does not exist until the shim does), or set `ORKESTRA_SHIM_DIR`. Meanwhile
`python3 plugins/orkestra/bin/orkestra <command>` always works.

**`doctor` says `cursor hooks`/`copilot hooks` are not installed.** Optional: only the engines you use matter. Run
`orkestra cursor install` or `orkestra copilot install`; `orkestra cursor status` and `orkestra copilot status` show
the result. Open a new chat afterwards.

**A session is not guarded.** Guards run only where the hooks are active. A Copilot session without the plugin enabled,
a Cursor chat with no Orkestra run (unless `engine.cursor.global_guards` is on), Codex, Gemini CLI and web chats are
outside Orkestra. A request typed without `/orkestra:run` gets only the every-session guards.

**A guard blocked something I want.** For git, say it in the message (`commit this`) or use `/orkestra:ship`. Secrets,
store and `~/.orkestra` are denied on purpose. Any of the nine guards can be turned off in the config.

**A run looks stuck.** `orkestra watch` shows its class (waiting for you, dead wait, hung command, silent, loop).
`orkestra next -p <project>` names the step; `/orkestra:resume` continues it. `orkestra abort -p <project> --reason "..."`
ends it without touching files.

**I want the changes of a run undone.** `/orkestra:rollback` shows a dry run first and asks; it restores the files the
run changed (not your own edits made meanwhile, unless `--all`), backs up what it overwrites and does not touch git.

**Everything is in the wrong language.** `orkestra lang show` prints the language and its source; see
[Language](#language).

**Does anything leave my machine?** Orkestra sends nothing on its own and has no telemetry. Claude Code, Cursor and
Copilot send prompts and project context to their providers under those products' terms. The only other traffic is what
you switch on: the optional bridge talks to Telegram (`api.telegram.org`) when a bot is configured and posts to your
`callback_url`, the watchdog posts to `notify.webhook_url` when you set one, and `install.sh --refresh-vendor` downloads
the pinned reference files from GitHub.

**The installer stopped on a red test.** That is the point of the pre-flight; please open an issue with the output of
`orkestra doctor` and the failing test. `--skip-tests` skips it at your own risk.

**Reporting a bug.** Use the issue forms and paste `orkestra --version`, your OS, Python version, engine and the output
of `orkestra doctor`. Security problems go to [SECURITY.md](SECURITY.md), not to a public issue.

## Limits you should know

- Guards are filters on tool calls, not a sandbox: the agent runs as you, on your files. They stop the mistakes and
  shortcuts models actually take; they are not a security boundary against a determined adversary. In a real trial one
  model ran the hook script itself to approve its own plan. Such commands and file contents are refused now, and the
  Cursor hook also recognises a call started by an agent (the Claude Code hook has no such check), but a script that
  cleans its environment and hides its names can still go around a file-based check.
- Claude Code and Cursor hooks are fail-open: if a hook crashes or the host kills it at 15 s, the tool call is allowed
  (a broken guard must not lock you out of your editor). A hook's own 9 s deadline comes first and denies a PreToolUse
  call instead. Copilot `PreToolUse` is fail-closed: a hook crash or an invalid payload denies
  the tool call. That asymmetry is intentional.
- Shell guards read commands without running them. Obfuscation (string concatenation, a command hidden in a variable)
  and unknown git aliases can slip through; the ledger, `activity.jsonl` and `hooks.log` let you see it afterwards.
- The proof is only as strong as the gates: a project without tests has no regression net, and the report states that
  as a risk. Nothing here promises zero defects; the aim is that a gate, a hook or a reviewer catches a model's mistake
  before it reaches the codebase.
- The Cursor engine depends on the hook behaviour of the Cursor version it was measured on (CLI 2026.10.01, IDE 3.23).
  The CLI was exercised end to end, the IDE window less. Token usage is not reported for Cursor runs.
- Windows is supported natively but less exercised than macOS and Linux (CI runs the suites on `windows-latest`; there
  is no maintainer machine). The watchdog only notifies there, there is no fast watcher, and desktop notifications exist for macOS and Linux.
- One machine, one user. No hosted service, no telemetry: nothing leaves your machine except what the model provider's
  own client sends and the optional features you configure yourself (Telegram, a callback or webhook URL; see the FAQ).

## Tests and CI

```bash
cd plugins/orkestra/tests && python3 -m unittest discover -s . -q     # plugin suite
cd bridge/tests && python3 -m unittest discover -s . -q               # bridge suite
cd integrations/openclaw/plugin && npm test                           # OpenClaw logic-only (required CI)
cd integrations/openclaw/plugin && npm run test:e2e                   # e2e (local/optional; needs OPENCLAW_PKG or Homebrew openclaw)
```

CI (`.github/workflows/tests.yml`) is configured to run both Python suites on Ubuntu, macOS and Windows with Python 3.9,
3.11, 3.13 and 3.14, plus static checks (compile, a Python 3.9 syntax check, the i18n lint, shell syntax), an installer
dry run on all three systems and the required OpenClaw logic-only job on Node.js 22 (Linux and macOS, no Homebrew
`openclaw`, no `continue-on-error`). POSIX-only fixtures are skipped on Windows, not counted as passed. The test suite
is pinned to Turkish output (`ORKESTRA_LANG=tr`) for its older assertions; new tests cover both languages.

## Documentation

- [ORKESTRA.md](ORKESTRA.md): the one-page reference ([Türkçe](ORKESTRA.tr.md)).
- [docs/architecture.html](docs/architecture.html): diagrams of the architecture and the flow
  ([Türkçe](docs/orkestra-mimari.html)); [docs/README.md](docs/README.md) indexes the docs.
- [ONBOARDING.md](ONBOARDING.md): first-time setup on every system ([Türkçe](ONBOARDING.tr.md)).
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) ·
  [AGENTS.md](AGENTS.md) for agents editing this repository · [CHANGELOG.md](CHANGELOG.md)

## Credits and licences

Orkestra is released under the [MIT License](LICENSE).

Its playbooks and agent prompts were written with these projects as references; selected files from them are kept,
unmodified and never loaded at runtime, under `plugins/orkestra/vendor/`, each with its own licence:
[superpowers](https://github.com/obra/superpowers) and
[ECC](https://github.com/affaan-m/ECC). Copyright notices, pinned commits and
licence files: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Orkestra is an independent project. It is not affiliated with or endorsed by Anthropic, Anysphere (Cursor), GitHub or
any of the projects above. "Claude", "Cursor" and "GitHub Copilot" are trademarks of their owners.
