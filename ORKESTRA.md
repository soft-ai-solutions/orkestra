# Orkestra: summary (version 1.6.0, October 19, 2026)

> Türkçe: [ORKESTRA.tr.md](ORKESTRA.tr.md) · This file is the canonical English reference; the Turkish one carries the same content.

This file summarizes all of Orkestra in one place. Usage details are in `README.md`, release history in `CHANGELOG.md`,
the visual walkthrough in `docs/architecture.html`, contribution rules in `CONTRIBUTING.md`.

## What it is

Orkestra is a plugin for Claude Code, Cursor and GitHub Copilot (and Pi). It takes a development request through ten phases: intake, discovery, clarification,
plan, **your approval**, implementation, verification, review, delivery, done. The rules are not left to the model's
goodwill: a command line tool (`orkestra`) keeps the phase order and the evidence, and host hooks check the
prohibitions on every tool call.

Source code cannot be written until you approve the plan. Every task is closed with evidence of a test that failed
first and then passed. Every check that passed at the start of a run (tests, lint, types, build) must still pass at the
end. Commit, push, deploy and store uploads are not part of the flow; they happen only when you explicitly ask for
them in that very message.

## Language

Orkestra serves people in English and Turkish; English is the default. The language of human-facing text (dashboards,
`watch`, activity lines, notifications, run artifacts, installer and bridge replies, `--help`) is resolved in this
order, first hit wins:

1. the `ORKESTRA_LANG` environment variable (`en`, `tr` or `auto`);
2. the `lang` key of `~/.orkestra/config.json` (`en`, `tr` or `auto`; the default is `auto`);
3. the OS locale (`LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`; a `tr…` locale selects Turkish; on Windows, when none of them is set, the user locale);
4. English.

`orkestra lang show` prints the language in use and where it comes from; `orkestra lang en|tr|auto` saves the choice
(the `lang` key above); `--lang en|tr` switches a single command.

What stays the same in both languages:

- Text the model reads (skills, agents, playbooks, `orkestra next` hints, guard messages) is English.
- What you type is understood in both languages: `commit this` / `commitle`, `approve` / `onay`, and so on.
- The section headings of `request.md`, `spec.md`, `plan.md` and `report.md` exist in both languages
  (`lib/orkestra_core/messages/protocol.py`). Every parser accepts both spellings, so a run started in one language
  can be continued in the other, and old Turkish runs keep working.
- Document templates live in `plugins/orkestra/templates/en/` and `plugins/orkestra/templates/tr/`;
  `orkestra template <name>` prints the one for the active language.

## Architecture

There are four parts, all under `plugins/orkestra/` (Claude Code runs the copy `orkestra setup` caches; Cursor and Copilot read the checkout):

| Part | Location | Job |
|---|---|---|
| Skills and playbooks | `skills/` | Texts that tell the main session (the orchestrator) what to do in each phase. One playbook per phase; optional checklists load only when the changed files match. |
| Agents | `agents/` | Eight subagents: scout, researcher, architect, plan-critic, implementer, reviewer, adversary, verifier. Only the implementer may write source code. |
| CLI | `bin/orkestra` → `lib/orkestra_core/` | State machine (`runs.py`, `state.py`), gates (`gates.py`), TDD and evidence (`evidence.py`), rollback backups (`snapshot.py`), next step and timing (`tempo.py`), monitoring (`monitor.py`, `usage.py`), external scan (`watchdog.py`). |
| Hooks | `hooks/hooks.json` → `hooks/dispatch.py` | The single entry point for ten events: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop, and four that keep the activity feed honest (failed tool calls, permission prompts, API errors, session end): PostToolUseFailure, Notification, StopFailure, SessionEnd. `dispatch.py` is a stub; the code is `lib/orkestra_core/hookmain.py`, and events that provably do nothing exit before it loads. Guard decisions live in `guards.py`; `shellparse.py` reads the shell command and the CLI's own parser (`cliparse.py`) reads the `orkestra …` line. An error inside a hook never locks the session (it allows the call and logs to `~/.orkestra/logs/hooks.log`). One deadline covers a whole hook: 6 s of CPU or 9 s of wall clock; a PreToolUse call that reaches it is refused (guard `complex`, or `busy` when the machine was only slow), any other event ends quietly. New hook entries only take effect in newly opened sessions. |

`bridge/` is a separate process: it turns a request coming from Telegram, n8n or OpenClaw into a headless session
(Claude Code, Cursor or GitHub Copilot). The same hooks run there too.

### Engines (Cursor arrived with 1.5.0)

A run is driven by Claude Code (the default), Cursor or GitHub Copilot; the choice is made per request (`orkestra run --engine cursor`,
`/run cursor <project> <request>` in the bridge (`/yap` in Turkish), `"engine": "cursor"` in the bridge API,
`/orkestra-run` in a Cursor chat). The state machine, gates, evidence and guards are shared. Everything
engine-specific lives in three files:

| File | Job |
|---|---|
| `lib/orkestra_core/engines.py` | Engine names, the session key (`cursor-<chat>`), command lines, the first prompt, text translation (`orkestra:reviewer` → `orkestra-reviewer`, the Cursor counterpart of sentences that are only true in Claude). |
| `hooks/cursor_hook.py` | Translates Cursor events into `dispatch.py` handlers: subchat → main session mapping, prompt/turn tracking, shell directory tracking (`shellcwd.py`), recognizing that an agent started it. |
| `lib/orkestra_core/cursorkit.py` | Generates the Cursor skill/subagent files (`plugins/orkestra/cursor/`), installs them into `~/.cursor`, merges `hooks.json`. |

Behaviors measured in Cursor that shaped the design: every subagent is a separate chat and its events carry no type;
in print mode (`agent -p`) no prompt or stop event arrives; the shell directory is kept across calls but not reported
in the event; a command that exits non-zero ends with `postToolUseFailure`; there is no event for a subagent finishing.
Details and sample payloads: `CHANGELOG.md` (1.5.0), `tests/fixtures/cursor/`.

## Commands

In chat: `/orkestra:run <request>`, `/orkestra:status`, `/orkestra:resume`, `/orkestra:onboard`,
`/orkestra:rollback`, `/orkestra:ship` (commit + push; only you can invoke it). In Cursor the same ones use a dash:
`/orkestra-run` … `/orkestra-ship`.

In a terminal (`python3 $ORKESTRA_ROOT/plugins/orkestra/bin/orkestra …` or `./plugins/orkestra/bin/orkestra`; once the installer has put `orkestra` on your PATH, just `orkestra …`):

| Command | Job |
|---|---|
| `watch` · `dashboard --open` | Live view (terminal · http://127.0.0.1:47616). The page shows risky agent actions and can pause or abort a run (`--read-only` hides the controls). |
| Bridge chat UI | http://127.0.0.1:47615/ · pick the project and Claude/Cursor/Copilot; follow the phase map, the agent network, tasks and the latest hook activity. |
| `status -p <project>` · `context` | The run's phase, tasks, evidence gaps. |
| `next -p <project>` | The single next step. The same state always gives the same answer. |
| `tempo -p <project> [--run <id>]` | Time breakdown: phases, time per task, review rejects, share of dead wait. |
| `watchdog [--dry-run\|--quiet\|--launchd]` | One scan of every open run from outside the session; notifies. |
| `watchdog --loop\|--daemon\|--status` | The fast watcher: `--loop` runs it in the foreground, `--daemon` starts it detached (POSIX), `--status` shows whether it runs and what it flags (`--json` for a machine view). See Monitoring. |
| `wait [--max <s>] -p <project>` | Headless runs: wait inside the process (no tokens) for subagent reports, then print the next step. Default cap 540 s. An interactive session does not block. |
| `gate verify [--resume\|--all] -p <project>` | The no-regression check. `--resume` continues an interrupted verify; `--all` re-runs every gate in full. |
| `brief [--review] <task>` | The brief for one task; with `--review` the reviewer's packet (acceptance, red and green tails, report, diff of its files). |
| `agents [clear] -p <project>` | Records of background subagents. |
| `doctor` | Environment and hook self-test. |
| `lang [show\|en\|tr\|auto]` | The language of human-facing text and where it comes from; `en`, `tr` and `auto` save the choice. `--lang` on any command switches that one call. |
| `await\|block\|unblock\|pause\|abort -p <project> --reason "<text>"` | The reason may also be given as plain words. `orkestra --version` prints the version. |
| `run [--engine claude\|cursor\|copilot] -p <project> "<request>"` | Opens the chosen engine's own interface in the project and starts the run. |
| `cursor install \| uninstall \| status \| build` | The Cursor side: install skills, subagents and hooks (your own `hooks.json` entries are kept). |
| `copilot install \| uninstall \| status` | The Copilot side: install the hooks (your own `hooks.json` entries are kept). |

The orchestrator and the agents use the remaining subcommands (`phase`, `task`, `tdd`, `gate`, `evidence`,
`plan check`, `memory` …); you never need to call them by hand. Options are written in full; abbreviations (such as
`--no`) are not accepted.

## Files

Every project gets a `.orkestra/` folder:

- `features/NNN-slug/`: the documents a person reads: `request.md`, `context.md`, `spec.md`, `plan.md`, `ledger.md`
  (every phase transition and evidence line), `tasks/`, `reviews/`, `verification.md`, `report.md`. May go into git.
- `runs/NNN-slug/`: machine state: `state.json`, `approval.json`, `agents.json`, `activity.jsonl`, evidence logs,
  file backups. Never goes into git; only the CLI and the hooks write it.
- `memory/`: project memory: `PROJECT.md`, `FEATURES.md`, `specs/`, `DECISIONS.md`, `LEARNINGS.md`.
- `config.json`: the project's gate commands. `current`: a shortcut to the open run.

On the machine, `~/.orkestra/` holds: `config.json` (guard switches, `watchdog` thresholds, `lang`), session-to-run
bindings, logs, the bridge settings. The model cannot write to this folder.

## Guards

In every Claude Code session: **git** (commit/push/branch only when asked in that very message; a subagent report
carries no permission), **secrets** (`.env*` and key files can be neither read nor written), **store** (EAS/Expo
uploads), **home** (`~/.orkestra`), **rollback**, **complex** (a command too large to be inspected).

Only in a session bound to a run: **phase** (no writes to source before approval), **isolation** (no writes outside the
project), **deps** (no unapproved packages), **deploy**, **destructive** (recursive deletion only inside a dedicated
temporary subfolder), **roles** (read-only agents write only their report),
**internals** (no hand-written or scripted writes to machine state; the approval answer cannot be embedded; also
`orkestra pause|abort|resume|unblock|block|await` of a run another chat drives, which only your own message lifts), **wait**
(no sleeping or polling for a subagent), **dialogs** (no command that opens a system window on the screen), **Stop**
(ending the turn while a run is active is answered with the next step, at most twice per state of progress and six times
per 30 minutes; ending it while a subagent is running is allowed).

Every denial of a run guard ends with an `unblock:` line, the way out: the driving chat can `orkestra pause` or
`orkestra abort` the run, any other agent tells you. A refused call is counted per guard, tool, target and actor: the 2nd
identical refusal adds a note, the 3rd becomes "denied Nx: do not retry; <alternative>" (an exact `orkestra ...`
rewrite where one exists), and the 8th within 10 minutes blocks the run and records a `loop` row. The decision itself
never changes. Two more refusals come from the hook itself: `busy` (the call could not be checked in 9 s on a busy
machine; retry the same call once) and `approval` (a question in an autopilot run).

A deliberate exception: `/tmp/<folder>` (and anything under `$TMPDIR`) is exempt from isolation, because agents need a
place for experiments and copies. The temp root itself (`/tmp/*`) is not exempt.

Nine guards can be switched off under `guards` in `~/.orkestra/config.json`. `guards.test_syntax` is an option in the
opposite direction and ships turned off: when enabled, `[[ a > b ]]` and `(( n > 100 ))` are read as comparisons, not as
redirections. It was verified against bash and not against zsh, which is why it is off.

## Monitoring

`watch` and `dashboard` show, for each run, the phase, the time spent in it, task and evidence progress, gate results,
token usage, the latest tool calls and the **stall class**:

1. waiting for you (question, approval, blocked, paused, turn ended) · 2. dead wait (a stopped subagent's report is
60 s overdue, the registry says so, and the whole run was silent since) · 3. hung command (open for `hung_command_seconds`,
900 s, or `gate_command_seconds`, 1800 s, while no agent wrote any row since it started) · 4. silent session (no record
for 10 minutes) · 5. loop (the same rejection or the same command 3 times, a third review round).

Every class is a plain predicate on rows, registry files and clocks; none is "the model has been quiet", because a
thinking model is not a stall. A false stall costs more than a late one.

`watchdog` makes the same classification from outside the session and sends a desktop notification (macOS, or Linux with `notify-send`) once per new event.
When the bridge is running it runs by itself every minute; when it is not, `orkestra watchdog --launchd` writes a
LaunchAgent file. Automatic resume is only for runs started headless (at most 3 times, in the same session, and never
while that session's process is alive). A run you drive from an interactive window is never resumed on its own.

### The fast watcher

`orkestra watchdog --loop|--daemon|--status` (`lib/orkestra_core/fastwatch.py`). One per machine: a locked pid file
(`~/.orkestra/watchdog.pid`; the lock dies with the process, so a leftover after `kill -9` is taken over). It checks every
open run every 5 s (1 s while a stall is about to be declared, 15 s with no open run), re-reads a file only when its
size or mtime changed, and spends no tokens. SessionStart starts it when none runs (POSIX; `watchdog.autostart`,
`ORKESTRA_NO_DAEMON=1` turns it off); the bridge hosts it as a thread; it exits after `watchdog.daemon_idle_minutes` with
no open run and no live session.

| Predicate | True when | Settle time |
|---|---|---|
| deny loop | the same refusal 3 times in 120 s (8 times in 600 s: the run should be blocked) | none |
| control flips | 3 pause/resume status changes in 10 min | none |
| paused but working | a paused run whose agents ran a tool in the last 60 s | 5 s |
| report overdue | a stopped subagent's report is due, no hand-back, no main action, whole run silent | 60 s |
| idle active | an autopilot run's turn ended with work left and the state did not move | 20 s |
| permission wait | the host shows a permission prompt nobody answered | 30 s |
| hung command | an open command past its limit while no agent wrote a row since it started, and its process shows no CPU | 15 or 30 min |
| API error | a model or API error ended the turn and nothing happened since | retry ladder |
| dead session | every bound session's host process is gone | none |
| hooks degraded | 3 hook errors in `hooks.log` within 60 s | none |

What it does: one notification per incident (then after 30 min, 2 h, 8 h and a day, at most 5 in all); and it heals only
what is provably stale: a binding to a missing or closed run, an open command that a later row of the same agent
outlived (an honest synthetic `end` row), subagent records of agents that are gone, one ledger line for a flip storm.
For a headless run it resumes the session with the capped `claude -p --resume` (3 per run, after 10 s, 60 s, 300 s, never
while the session's process or a bridge job is alive). What it cannot do: run on Windows, approve a prompt or answer a
question, kill a process it did not start, or tell a slow command from a stuck one without the CPU sample. After 3 failed
ticks it stops healing and only notifies. Everything it does is in `~/.orkestra/logs/watchdog.log` (rotated at 1 MB).

## Timing and tempo

Measured on five runs with 1.2.1 on October 4-5 (`orkestra tempo`):

| Run | Duration | Tasks | Review rejects | Share of dead wait |
|---|---|---|---|---|
| game-app · remaining work | 12 h 37 min | 28 | 17 | 29% |
| deals-app · deep analysis | 6 h 57 min | 57 | 67 | 19% |
| mobile-app 001 | 5 h 55 min | 27 | 13 | 34% |
| mobile-app 002 | 5 h 58 min | 28 | 19 | 26% |
| mobile-app 003 | 11 h 2 min (5 h 18 min of it waiting for approval) | 62 | 0 | 12% |

There were two big losses. The first was dead wait: while Claude Code ran a subagent in the background and delivered the
report as a new message, the Stop hook prevented the turn from ending and the model waited in a `sleep` loop. Since 1.4.0
the turn may end while an agent runs, and waiting commands are rejected. The second was needless review rounds: 35 of
116 rejects were only for minor notes. Now a task that gets PASS is not reopened, notes move to the final review, and
the third FAIL is put to you.

The effect of these changes on duration has not been measured yet. After the first run on 1.4.0, `orkestra tempo` gives
the same table; the comparison is made from there.

Seamless continuation: there is no separate "where was I" record for an interrupted run. `orkestra next` computes the
answer from the state, evidence and review files; even if the session closes, the context is compacted or you open a
new window, it finds the same sub-step. `/orkestra:resume` uses it.

## Reliability: caps, locks and gates

No agent may block another, nothing may loop without a bound, and waiting costs no tokens.

**Control plane.** A chat is bound to a run only when the main chat ran `orkestra start|resume` itself (an unbind comes
from `pause`, `abort` or `phase next`), the printed marker's `home=` is this hook's `ORKESTRA_HOME`, and the run exists, is
open and has an absolute project path. Subagents, `cat` of a log and sandbox copies of the CLI bind nothing. A live
binding is kept with one ledger line and one warning; a bogus one heals itself; a neutral message never unbinds.
`pause`, `abort`, `resume`, `unblock`, `block` and `await` of a run another chat drives are refused unless the user's
message asks for it (a driver is alive for `watchdog.lease_seconds`). A call that changes nothing (pause of a paused run)
is a no-op and writes no row. A `status` activity row shows who paused or resumed.

**Caps.**

| What | Cap | Then |
|---|---|---|
| identical refusals | 2nd note, 3rd "do not retry", 8th in 10 min | the run is blocked, a `loop` row is written |
| Stop nudges | 2 per state of progress, 6 per 30 min per run | the turn ends; autopilot nudges never send the model to ask a question |
| review rounds of a task | 3 | manual: the user rules; autopilot: the task is blocked and `--force` cannot lift it (`task defer` or `block` are the ways on) |
| identical dispatches | `tempo.max_dispatches` (3) without new evidence | `orkestra next` answers `escalate` |
| subagent registry | no sign of life for `watchdog.agent_silent_seconds` (150 s) | the agent is listed as lost |
| bridge turn | `bridge.turn_timeout_seconds` (7200 s) wall clock, `bridge.turn_idle_seconds` (1800 s) without run activity | the turn is interrupted (SIGINT, its process group 20 s later) and the run resumed; a turn with an open command is never idle |
| headless spend | `headless.max_budget_usd` (0 = off) | passed to `claude -p --max-budget-usd` |

**Locks and deadlines.** A lock is waited for 2 s in a hook (then the hook goes on unlocked and logs one line; a state
transaction raises a one-line error) and 10 s in the CLI; the error names the holder's pid, command and age.
`ORKESTRA_LOCK_TIMEOUT` overrides both. Slow work (git baselines, the test-file scan, usage totals) runs before the lock is
taken, and every git call is time-boxed and runs with `GIT_OPTIONAL_LOCKS=0`. The hook deadline is in Architecture.

**Waiting.** In a headless run `orkestra wait [--max N]` polls every 2 s inside the process and returns on the first of:
no subagent running, a new report due, a hand-back, a review or task file changing, the run leaving `active`, or the cap
(540 s by default, below the 600 s a Bash call may run). Run it again if the verdict still says running.

**Gates.** Each gate runs in its own process group with a streaming log in `evidence/`; a timeout (`timeout`, 900 s by
default) or an idle stall (`idle_timeout`, off unless set) stops the whole group (SIGTERM, SIGKILL after 3 s).
`gate verify` re-uses what is provably unchanged: a gate whose baseline run timed out or took 120 s or more is not re-run
(shown as "failed at baseline"), a gate with `paths` globs and no changed file is carried forward from the last passing
verify (shown as "PASS (carried)"), a fast baseline failure is re-run with a shortened timeout. `--resume` skips the gates
finished for the same change stamp; `--all`, `ORKESTRA_GATES_FULL=1` or `gates_rerun_failed_baseline` run everything in
full. `gates_parallel` above 1 runs typecheck, lint and test gates side by side (at most half the CPUs).

**Configuration keys added by this work** (`~/.orkestra/config.json`, defaults in `paths.DEFAULT_GLOBAL_CONFIG`):

| Key | Default | Meaning |
|---|---|---|
| `tempo.max_dispatches` | `3` | Identical dispatches without new evidence before `next` escalates. |
| `watchdog.agent_silent_seconds` | `150` | A registered subagent with no sign of life this long is lost. |
| `watchdog.autostart` | `true` | SessionStart may start the fast watcher. |
| `watchdog.daemon_idle_minutes` | `30` | The detached watcher exits after this long with no open run and no live session. |
| `gates_parallel` | `2` | Concurrent typecheck, lint and test gates (a project `config.json` value wins). |
| `gates_rerun_failed_baseline` | `false` | `true`: `gate verify` re-runs gates that failed or timed out at baseline. |
| `headless.max_budget_usd` | `0` | Above 0, a spend ceiling for headless turns. |
| `bridge.turn_timeout_seconds` | `7200` | Wall-clock budget of one bridge turn. |
| `bridge.turn_idle_seconds` | `1800` | A bridge turn without run activity this long is interrupted and resumed. |

A value of the wrong type in `config.json` is ignored in favour of the default (`doctor` names it).

## Scope

Orkestra works in Claude Code, Cursor and GitHub Copilot sessions that have the hooks enabled; the full flow only
kicks in once a run is started. First setup: `orkestra setup` (or `cursor install` / `copilot install`).

- A request that comes through `/orkestra:run` (Claude/Copilot) or `/orkestra-run` (Cursor) and through the bridge: the full flow.
- A request typed without starting a run: only the "every session" guards apply. There is no plan, approval or TDD.
- A second session in the same project that is not bound to the run while a run is open: it gets a warning but can change files.
- A Copilot session without hooks, Cursor's non-Orkestra agent, Codex, Gemini CLI, a claude.ai chat and Cowork: out of scope.
- The bridge (Telegram/n8n/OpenClaw/local workbench) starts the Claude Code, Cursor and Copilot engines
  (`/run copilot: …` (`/yap` in Turkish), `POST /jobs` with `"engine": "copilot"`).

## Coordination

Three agents update this folder: orkestra-core (the plugin and the bridge), openclaw-agent (`integrations/openclaw/`),
ecc (`vendor/ecc`, optional playbooks, this document). The rules are in `CONTRIBUTING.md`;
the most important ones: no plugin file is changed while a run is open; a backup and an md5 are taken before a change,
the work is done in a separate copy and the tests run there, then it is transferred to the live folder in one go;
commit, push and branch happen only at the explicit request of the human user / repository owner.

When a new option or subcommand is added to the CLI, the single place is `lib/orkestra_core/cliparse.py`: the guards
read the command line with the parser there.

## Maintenance and rollback

Tests:

```bash
cd ./plugins/orkestra/tests && python3 -m unittest discover -s . -q
cd ./bridge/tests && python3 -m unittest discover -s . -q
cd ./integrations/openclaw/plugin && npm test
python3 ./plugins/orkestra/bin/orkestra doctor
```

Claude Code runs the plugin copy that `orkestra setup` caches under `~/.claude/plugins/cache/` (keyed by the version), so a
code change reaches it only after you bump the version and run `orkestra setup`; Cursor and Copilot read the checkout on
every call. Agent and skill texts and `hooks.json` take effect in a newly opened Claude Code session. The bridge is a
long-lived process; restart it when the bridge code changed. `install.sh` is needed only for the first installation,
adding the MCP, and running the tests on this machine.

Rolling back a version (when no run is open): the changelog names every version; check out the commit that released it.
The repository has no release tags.

```bash
python3 plugins/orkestra/bin/orkestra cursor uninstall   # only when going below 1.5.0 (Cursor support arrived with 1.5.0)
git log --oneline -- CHANGELOG.md                        # find the commit of the version you want
git checkout <commit>                                    # (if release tags exist one day: git fetch --tags && git checkout v1.4.0)
cd plugins/orkestra/tests && python3 -m unittest discover -s . -q
```

If you did not get the repository with git, download the commit you want from GitHub as a ZIP and do the same with it.
Undoing the changes a run made to a project's files is a separate matter: `/orkestra:rollback`.

Refreshing the pinned references: `bash install.sh --refresh-vendor` (sources and commits are in `vendor/PINNED.md`).

## Open items

- The CI matrix is configured for Python 3.9, 3.11, 3.13 and 3.14 on Linux, macOS and Windows; the full local test suite ran on Python 3.14.7.
- The Cursor engine was tried on five real XS runs with the command line tool (`agent` 2026.10.01); the Cursor IDE window
  was not tried separately (same hook contract; no IDE-specific difference was observed because none was looked for).
- Cursor also loads the old orkestra copy in Claude Code's plugin registry (1.0.0, `~/.claude/plugins/cache/…`);
  that copy's hooks answer in Cursor chats with the old rules (for example it rejects a `folder/**` Grep).
  `claude plugin update orkestra@orkestra-local` refreshes the registry; its effect on the copy Claude Code runs
  has not been tried.
- Self-approval was seen in a real run and closed with three layers (CHANGELOG 1.5.0); a script that cleans its
  environment, detaches from its parent processes and hides names can still get around it. On the Claude Code side the
  hook has no "an agent started me" check (Claude Code's process layout was not measured); the command and content checks
  exist on both engines.
- In Cursor runs there is no token counting and no warning about the implementer's TDD evidence at finish time.
- Token cost (76-546 million cache reads per run) is measured but not reduced.
- The scope gaps stand as above.
