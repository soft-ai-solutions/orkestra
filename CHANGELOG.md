# Changelog

## Unreleased

Orkestra now serves people in English and Turkish (English is the default), runs the same on macOS, Linux and Windows
with Python 3.9 to 3.14, and its repository documentation is available in both languages. Entries up to 1.6.0 below
were written in Turkish and are kept as they were.

### Copilot: model, effort, context, allow-all
- **Copilot CLI profile:** `engine.copilot` gains `context` (`long_context`) and `allow_all` (`true`) next to `model`
  (`gpt-6-luna`) and `reasoning_effort` (`max`). allow-all passes `--allow-all-tools --allow-all-paths --allow-all-urls`
  in the headless and interactive command; `--excluded-tools` and the hooks stay. `--context` is omitted for a CLI that
  does not list it.
- **One resolver:** `engines.copilot_resolve` decides flag > request words > stored run choice > config > default for
  `orkestra run`, the watchdog resume (reads `copilot_profile` from the run state) and the bridge. New flags
  `orkestra run --model --effort --context --allow-all/--no-allow-all`; efor levels `none` to `max`; invalid values are
  rejected with a catalog message; the model id is free text or `auto`.
- **Visibility:** `orkestra copilot status` and `orkestra doctor` show the effective values and their source.
- **VS Code:** `orkestra copilot vscode` (plan by default; `--apply --workspace|--user`, `--remove`, `--permissions`,
  `--model/--effort` agent file). Writes `chat.defaultModel` and `chat.defaultConfiguration` (both exist in VS Code
  1.141); effort and context size have no setting. `chat.pluginLocations` is machine-scoped: not written with
  `--workspace` (use `--user`). `--effort` without `--model` is refused; `--remove` drops a key only while it holds
  Orkestra's value. Request words can only lower allow-all and only explicit directives change context. Parallel
  workers use the run's profile. Unverified: agent `reasoning-effort` in Local mode; `gpt-6-luna` is a configured
  default, whether the signed-in account has it is not verifiable offline (first use shows the CLI's own error;
  `--model` / `chat.defaultModel` override).

### Engine audit fixes
- **Copilot:** the install and the docs now say what works: `orkestra run --engine copilot` starts the CLI with
  `--plugin-dir`, and a manual CLI session uses `copilot --plugin-dir ~/.copilot/plugins/orkestra`
  (`copilot plugin install` does not take a local path). Dry-run and install lines name the `hooks.json` they touch.
- **Claude Code:** `orkestra setup` has an update path for an already installed plugin copy.
- **Cursor:** the generated hooks carry a `loop_limit`.
- **Intent:** a regression in intent matching was fixed and pinned by a test.
- **Pi and the installer:** `install.py` detects and plans Pi next to Claude Code, Cursor and Copilot; the no-engine
  messages name Pi; `orkestra doctor` prints `orkestra cursor install` / `orkestra copilot install` next to a missing
  Cursor or Copilot row.

### Control plane and locks (wave A)
- **Binding:** a `[orkestra:bind]` marker now carries ` home=<ORKESTRA_HOME>` and binds a chat only when the main chat
  ran `orkestra start|resume|pause|abort|phase next` itself; subagents and text echoed by `cat`/`grep` never bind. A
  live binding is kept (one ledger line and one warning), bogus bindings heal themselves, and a neutral message no
  longer unbinds (only explicit "forget this run" signals do). Same-status calls are no-ops; a new `status` activity
  row shows who paused or resumed. Every run-guard denial ends with an `unblock:` line.
- **Locks:** `file_lock` never waits forever (2 s in a hook, 10 s in the CLI, override with `ORKESTRA_LOCK_TIMEOUT`);
  the holder is named in the error. Slow git work runs outside the state lock, git calls are bounded and use
  `GIT_OPTIONAL_LOCKS=0`, the non-git scan keeps `run/snapshot/stat-cache.json`, and a second watchdog pass skips
  instead of queueing.
- **Guards:** a `#` comment no longer hides a later command from the shell parser (`# note` + newline + `git push`
  was allowed).

### Stall, loop and token audit (wave B)
- **Hooks:** one hook-wide deadline (6 s CPU, 9 s wall) that nothing can swallow; `git_context` only runs for
  `git checkout|switch|push`; identical refusals escalate (note, then "do not retry", then the run is blocked after 8
  in 10 minutes) while the decision itself never changes; the Stop nudge is keyed by progress and capped; four new
  events (`PostToolUseFailure`, `Notification`, `StopFailure`, `SessionEnd`) close open rows. New hook entries only
  take effect in newly opened sessions.
- **Review and dispatch caps:** `--force` cannot lift the review-round cap in autopilot; three identical launches for a
  task without new evidence make `orkestra next` return an `escalate` action (`tempo.max_dispatches`).
- **Stall classes:** the classifier in `watch`, the dashboard and the watchdog is keyed on deterministic predicates
  only: a hung command needs an open command past its limit while no agent wrote any row since it started (the fast
  watcher also samples the CPU of its process twice, 30 s apart); a dead wait needs a stopped subagent whose report is 60 s overdue while the whole run was
  silent; a subagent with no sign of life for `watchdog.agent_silent_seconds` is lost.
- **Gates:** each gate runs in its own process group with a streaming log; a timeout (`timeout`, default 900 s) or an
  idle stall (`idle_timeout`, off unless set) stops the whole group (SIGTERM, SIGKILL after 3 s), so no orphan keeps the
  run waiting. `gate verify` does not re-run a gate whose baseline run timed out or took 120 s or more (it is shown as
  "failed at baseline", the regression rule cannot blame the run for it), re-runs a fast baseline failure with a
  shortened timeout, carries a gate with `paths` globs forward when none of its files changed, and takes `--resume`
  (skip the gates finished for the same change stamp after an interrupted verify) and `--all` (everything in full; also
  `ORKESTRA_GATES_FULL=1` or `gates_rerun_failed_baseline`). `gates_parallel` N runs typecheck, lint and test gates
  concurrently (at most half the CPUs); the default 1 keeps the config order.
- **Waiting without tokens:** `orkestra wait [--max N]` waits inside the process (poll 2 s, default cap 540 s, below the
  Bash tool limit) until a subagent report or hand-back arrives, a review or task file changes, the run leaves
  `active` or no subagent runs, then prints the next step. It blocks only in a headless run; an interactive session gets
  the next step at once and ends its turn instead.
- **Review packet:** `orkestra brief --review T3` prints what a reviewer needs for one task (acceptance, red and green
  tails, report, diff of its files); `task add --files a,b` records the files at plan time.

### Fast stall detection and cheaper hooks (wave C)
- **Fast watcher** (`lib/orkestra_core/fastwatch.py`): `orkestra watchdog --loop|--daemon|--status`. One watcher per machine (flock on `watchdog.pid`,
  a stale pid file after `kill -9` is taken over) checks every 5 s with deterministic predicates only (deny loop,
  pause/resume flips, paused but working, report overdue, idle active, permission wait, hung command, API-error
  retry, hooks degraded, dead session). It starts lazily from SessionStart (`watchdog.autostart`,
  `watchdog.daemon_idle_minutes`, `ORKESTRA_NO_DAEMON=1` turns it off), spends no tokens, heals only stale bindings,
  stale open commands and dead agents, and stops healing after 3 failed ticks. Notices decay and stop after 5.
  Audit trail: `logs/watchdog.log`. A thinking model is not a stall: no predicate looks at "the model has been quiet".
  What it cannot do: it does not run on Windows (no detached watcher there; the one-shot `orkestra watchdog` only
  notifies), it never signals a process it did not start, it cannot answer a permission prompt or a question for you, and
  its only model-spending action is the existing capped `claude -p --resume` of a headless run (3 per run, after 10 s,
  60 s and 300 s). Detection latency is the predicate's own settle time plus at most one tick (5 s, 1 s once a
  candidate is due): a deny loop or a pause/resume storm is seen within seconds, an idle autopilot turn after 20 s, a
  permission prompt nobody answered after 30 s, an overdue subagent report after 60 s of silence, a hung command only
  after its limit (15 min, 30 min for gates). The watcher is not a faster model; it is a faster clock.
- **Bridge:** a turn has a wall-clock budget (`bridge.turn_timeout_seconds`) and an activity cap
  (`bridge.turn_idle_seconds`); the child runs in its own session and its whole group is stopped on timeout or
  cancel; notification sends retry with backoff and give up. `headless.max_budget_usd` is opt-in.
- **Hook cold start:** the hook code lives in `lib/orkestra_core/hookmain.py` behind the `hooks/dispatch.py` stub;
  events that provably do nothing exit early; modules a hook does not need are imported lazily. `test_hook_latency`
  requires a guarded and a quiet hook to cost at least 35 percent less CPU than the previous entry script (measured
  side by side in the same run) and to stay under an absolute budget scaled by machine load; it also pins the import
  graph. On POSIX `hooks.json` execs python3 directly (`run_hook.sh` stays for Windows and hosts without python3).
  Without a writable plugin folder the bytecode goes to `~/.orkestra/pycache`; `PYTHONDONTWRITEBYTECODE=1` costs about
  4x CPU per hook. Guard decisions are pinned by `tests/data/guard_decisions.json`, a corpus of more than 5000 recorded
  PreToolUse events (`tests/guard_corpus.py`), replayed on every run of the suite: a lazier or faster hook can never
  change what a guard allows or denies.
- **Config keys (all in `~/.orkestra/config.json`, defaults in `paths.DEFAULT_GLOBAL_CONFIG`):** `tempo.max_dispatches`
  (3), `watchdog.agent_silent_seconds` (150), `watchdog.autostart` (true), `watchdog.daemon_idle_minutes` (30),
  `gates_parallel` (1), `gates_rerun_failed_baseline` (false), `headless.max_budget_usd` (0 = off),
  `bridge.turn_timeout_seconds` (7200), `bridge.turn_idle_seconds` (1800). Environment: `ORKESTRA_LOCK_TIMEOUT`,
  `ORKESTRA_NO_DAEMON=1`, `ORKESTRA_GATES_FULL=1`. A value of the wrong type in `config.json` falls back to the default.
- **Plugin cache:** Claude Code runs the cached plugin copy; bump the version and run `orkestra setup` to get the new
  hooks.

### Two languages
- **English and Turkish for everything a human reads:** `watch` and the dashboard, activity and notification lines, run
  artifacts and document templates, installer output, bridge replies, the local web UI and `--help`. The language is
  resolved as: `--lang`, `ORKESTRA_LANG`, `lang` in `~/.orkestra/config.json`, the OS locale (on Windows the user
  locale), English.
- **New commands and flags:** `orkestra lang [show|en|tr|auto]` (shows the language and where it comes from, or saves
  it); a global `--lang en|tr|auto` that works before or after the subcommand; `setup --lang` (also saves the choice);
  a `language` row in `doctor`; `--version`; `--reason TEXT` on `await`, `block`, `unblock`, `pause` and `abort` (the
  reason can still be given as plain words).
- **Architecture:** `lib/orkestra_core/i18n.py` and the catalogs in `lib/orkestra_core/messages/*.py`. `test_i18n`
  fails on a key present in one language only, on different `{fields}`, on Turkish letters in English text, on an
  untranslated copy and on Turkish letters in code outside the catalogs and the vocabulary files.
- **Protocol tokens are accepted in both languages:** the section headings of `request.md`, `spec.md`, `plan.md` and
  `report.md`, the plan task fields (`Files` / `Dosyalar`, `(new)` / `(yeni)`), the AskUserQuestion headers and options
  (`Plan approval` / `Plan onayı`, `Rollback` / `Geri alma`) and the machine-evidence block of the report. A run started
  in one language continues in the other, and old Turkish runs and old Turkish wording (with or without the em dash,
  any case, with or without diacritics) keep working. English approve and reject phrases are recognized.
- **Templates, skills and docs:** `templates/en/` and `templates/tr/`; skills and agents are language-neutral and answer
  in the user's language; `ORKESTRA.md` is the English reference with `ORKESTRA.tr.md` beside it;
  `integrations/openclaw/CONTRACT.md` (English) and `CONTRACT.tr.md`.
- **Hooks, activity feed, watchdog, `watch` and the dashboard** are bilingual. `watch --json` adds `phase_label` and
  `status_label` in the active language; `phase_tr` and `status_tr` stay Turkish for the OpenClaw plugin.
- **Bridge and OpenClaw:** replies in both languages; chat commands `/run`, `/status`, `/cancel`, `/help` (the old
  `/yap`, `/durum`, `/iptal`, `/yardim` still work); `GET /health` reports `lang`; the web UI has a language button and
  `?lang=`; the n8n workflow names are English (old names are recognized on import); the OpenClaw plugin registers
  `/run`, `/runs`, `/cancel` next to the old commands. The note appended to requests from external triggers is now
  English. To keep the Turkish bridge replies you had before: `orkestra lang tr`.
- **Localized `--help`:** a description, five examples and the exit codes (0 ok, 1 a check failed or the next step is
  blocked, 2 usage error or refused request), and one line per subcommand.

### Installers and Windows
- **`install.py`, `install.sh`, `install.ps1`:** `--dry-run` prints the plan and changes nothing, `--uninstall` removes
  what the installer set up (the Claude Code plugin and marketplace, the Cursor and Copilot hooks, the PATH shim, the
  macOS bridge LaunchAgent), `--purge` also deletes the Orkestra home after a confirmation or `--yes`. Unknown flags
  are an error, so a typo cannot start a real install. Python 3.9 and git are checked with a hint per OS.
  `install.py` and `install.sh` pick their language like `orkestra` does; `install.ps1` words only its own "Python not
  found" message (from `ORKESTRA_LANG`, the locale variables and the Windows UI language) and hands the rest to `install.py`.
- **Windows hook and CLI launchers:** `hooks/hooks.json` now starts `sh hooks/run_hook.sh`, which tries `python3`,
  `python` and `py -3` and skips the Microsoft Store stub. `bin/orkestra` is both a valid shell script and a valid Python
  file, `bin/orkestra.cmd` is the cmd and PowerShell launcher, and `.gitattributes` keep line endings right (LF for the
  shell launchers, CRLF for the `.cmd`). The Cursor and Copilot hook commands no longer bake the Microsoft Store `python3` stub on Windows; they use the interpreter
  that installed them.
- **PATH shim on Windows:** `orkestra.cmd` goes to `%LOCALAPPDATA%\Microsoft\WindowsApps` when that folder exists,
  otherwise to `~/.local/bin`; `ORKESTRA_SHIM_DIR` still wins; a stale shim of ours is renewed, a file of yours is left
  alone; `uninstall` removes it. The CRLF `.cmd` shim is written byte-exact (it was wrong on Python 3.9 and doubled CR on
  3.10 and later).
- **`doctor`:** a `python3 on PATH (plugin hooks)` row (informational).

### Portability (Windows, narrow-codec consoles, Python 3.9 to 3.14)
- **Locking and files:** on Windows every process now locks the same byte, so state writes exclude each other (a process
  that found the lock file already present used to lock a different byte); a file system without `flock` continues
  unlocked instead of crashing every write; `write_text_atomic` writes LF everywhere and retries `os.replace` on Windows
  while a reader holds the file; the generated Cursor tree is written with LF.
- **Hooks:** a thread-based deadline replaces `SIGALRM` where it does not exist; hook stdin and stdout are UTF-8 (a
  strict console codec used to make the hook fail open and lose the deny reason); Windows path separators and case in the
  Cursor hook; no `ps` walk when there is no `ps`.
- **Watchdog:** it no longer calls `os.kill(pid, 0)` on Windows (that terminates the process); auto-resume is disabled
  there and it only notifies. Linux desktop notifications through `notify-send` (macOS unchanged; Windows: webhook or
  bridge).
- **Terminals and files:** `watch` and `dashboard` no longer die on a console that cannot encode a character
  (`watch --json` falls back to `\u` escapes); a `config.json` with a byte order mark is read; `git`, `ps` and `pgrep`
  output is decoded as UTF-8; the bridge `launchd` command no longer writes a plist off macOS; the bridge cancel works on
  Windows (it returned 400 because of `SIGINT`); `find_project_root` compares the workspace root and the home folder case-insensitively on macOS and Windows.
- **First run on a bare machine:** a wrong-typed setting in `config.json` (`doctor` names it, other commands print one
  line and exit 2) and an unusable `ORKESTRA_HOME` (a file where the folder should be, no permission) are reported in one
  line instead of a traceback. The new first-run end-to-end suite found twelve such defects; each is fixed with a test.

### CI
- The unit job runs both Python suites on Ubuntu, macOS and Windows with Python 3.9, 3.11, 3.13 and 3.14 (fail-fast
  off); a static job (compile, a Python 3.9 syntax check, the i18n lint, `bash -n` and `sh -n` of the shell scripts); an
  installer job (`--dry-run`, `--uninstall --dry-run`, a Turkish run, `install.ps1` on Windows); the OpenClaw logic job as
  before; a concurrency group cancels superseded runs. The workflow has not been observed running on GitHub yet.

### Documentation and community files
- `README.md` (English) and `README.tr.md` (Turkish) were rewritten as mirrors of each other: what Orkestra is and is
  not, a quickstart, engines and platforms (including Windows), the phases, the guard table checked against `guards.py`,
  monitoring, project memory, the bridge, the language rules, configuration, a FAQ and troubleshooting, and the limits.
  Corrected on the way: `POST /runs` was named as the way to start a bridge job (it is `POST /jobs`; `GET /runs` is the
  live view), the Windows hook note (hooks start through `run_hook.sh`, not a bare `python3`), the CI matrix, the claim
  that `setup` always creates the PATH shim (only into an existing folder), and stale test counts.
- `ONBOARDING.md` is a full first-time setup (Windows included) and has a Turkish twin, `ONBOARDING.tr.md`.
- `docs/architecture.html` is the English architecture page; `docs/orkestra-mimari.html` and the root
  `orkestra-mimari.html` (Turkish) were brought in line with the code (three engines including Copilot, bilingual
  commands, current layout, no stale test counts) and link to each other; `docs/README.md` indexes the docs.
- The reliability work is documented in `README.md` / `README.tr.md` ("Reliability and speed"), the reference sections
  of `ORKESTRA.md` / `ORKESTRA.tr.md` (commands, guards, monitoring, caps, config keys), `CONTRIBUTING.md` (the hook
  latency budget and how to extend the guard decision corpus) and the two stall tables of the architecture pages;
  `test_docs_drift` now also checks `wait`, `watchdog --loop|--daemon|--status`, `gate verify --resume|--all` and
  `brief --review` against the real parser.
- `CONTRIBUTING.md` gained "Adding or changing text", "Adding a language" and a pre-PR checklist; `AGENTS.md` gained the
  localization rule; `SECURITY.md` states the bridge secret permissions per OS and accepts reports in English or Turkish.
- GitHub: issue forms for bug reports and feature requests (they ask for the Orkestra version, OS, Python version,
  engine and the output of `orkestra doctor`), `config.yml` with blank issues disabled and a link to private security
  reporting, a pull request template with the gate checklist, and a Dependabot configuration for GitHub Actions only.
- `tests/test_docs_drift.py` checks that every `orkestra <subcommand>` and `--flag` named in the docs exists in the CLI
  parser, that relative links and anchors resolve, that the English and Turkish documents mirror each other, that the
  versions agree and that the quickstart commands run with `--help`.

### Tests
- New suites: `test_i18n`, `test_i18n_protocol`, `test_i18n_templates`, `test_i18n_hooks`, `test_i18n_monitor`,
  `test_i18n_install`, `test_i18n_integration`, `test_cli_ux`, `test_first_run`, `test_bilingual_e2e`,
  `test_portability`, `test_installer` and `test_docs_drift` (plugin), `test_bridge_i18n` (bridge) and the language and
  message tests of the OpenClaw plugin. The older plugin and bridge suites are pinned to Turkish output
  (`ORKESTRA_LANG=tr`); new tests cover both languages.

### Behavior changes
- `awaiting.question` now keeps only the question headers (the doubled `soru: soru: X` prefix is gone).
- Durations under 90 seconds in the monitor read `45s` / `45 sn` instead of `0 dk`; the terminal view and the dashboard
  share one formatter.
- `request.md` starts with `# Request: <title>` (English) or `# İstek: <title>` (Turkish); `engines.run_prompt` labels the
  request `Request:` in an English session.
- A `YAPILDI` finding row is no longer open; the older Turkish approve label followed by a change request such as
  `... ama T002'yi çıkar` no longer approves.

### Fixes
- Uppercase Turkish labels and headings such as `BAĞIMLI` and `İSTİSNALAR` were not recognized.
- The `(yeni)` marker in the plan template hint sat inside backticks, so a model copying the line produced a bogus file
  entry.
- An unlabeled extra answer in an approval reply reached the model context as the Turkish `ek soru`.
- **Typed approvals (English):** `approve it, but change the colour`, `approved, except the migration`, `go ahead but use
  blue instead` or `I'll approve it tomorrow` are not approvals any more (the first three are recorded as change
  requests); `Don't worry, I approve` and `I won't change anything, I approve` are. The same holds for text typed after
  the option label (`Approve and start implementing, drop T002`). New English yes words: `proceed`, `continue`, `start`,
  `ok`, `okay`, `yes please`, `looks good`, `accept`, `do it`; manual mode is requested by `wait for my approval`,
  `I want to review the plan first`, `require my approval`, `use manual approval`. Typographic apostrophes (`don’t`) read
  like the plain one, also for git permissions.
- **Windows commands:** a gate, TDD or evidence command runs through `cmd.exe` as typed (`npm test`, `cd x && y`, quotes),
  several arguments of `tdd` and `evidence` run as a list (a failing quoted command was recorded as a pass), and a
  program that cannot start is a failed gate or exit 127, not a crash.
- **Installers:** `--purge` refuses a folder that is not an Orkestra home (the drive root, your user folder, the current
  directory, this checkout, a folder of other files), expands `~` like `orkestra` does and exits 1 when the deletion
  fails; a failed setup or health check with an engine present exits 1; `install.sh` no longer dies without `HOME`,
  survives an apostrophe in the checkout path, takes only `y`/`yes` (Turkish also `e`/`evet`) as a yes and no longer
  reports a healthy install as "not ready".
- **`orkestra template` and `playbook`** cannot read a file outside their own folders.
- **Bridge:** the agent is fed and read as UTF-8 whatever the locale (a Turkish request used to leave a job "running" on
  a Windows code page); `?lang=en|tr` makes the server write its own texts (errors, live status, engine reasons) in that
  language and the local UI sends it; the page's hero line and `LOCAL` tag are translated.
- **Language:** every option of `--help` and the `error:` line of a usage error follow the language; `orkestra run`
  without a request prints its usage in it; `orkestra diff` reads git as UTF-8; the no-Python message of
  `hooks/run_hook.sh` picks its language like `orkestra` does (config.json and all locale variables included); the
  OpenClaw plugin reads a `config.json` with a BOM.
- **Smaller:** the backup of a damaged `config.json` is mode 600; uninstalling one checkout keeps another checkout's
  `orkestra.cmd`; `doctor` no longer tells Windows users to create a `python3.exe`; dry-run plans read "would update
  hooks.json: ...".
- **Typed intent, second pass:** `without breaking ...`, `no need for ...` and `not the manual ...` cancel a request to
  keep the plan decision only when they are about asking or approving in the same sentence. So
  `Refactor the parser without breaking the API. Ask me for approval first.` stays manual, while
  `releases need my approval`, `I want to see the design in dark mode` and `enable manual approval of refunds` are
  feature wording, not the request. `approve it` and `approve the plan` approve only as the opening words of a message.
  A condition (`provided`, `assuming`, `as long as`, `pending`, `subject to`, `once`, `only`), a limit (`T001 only`,
  `the first two tasks`, `without T003`) or a retraction (`actually, wait`) anywhere in the message, and the Turkish
  negated, modal, future and conditional forms (`onaylamadım`, `onaylayabilir miyim`, `onaylayacağım ama yarın`,
  `onaylıyorum fakat başlama`), are no approval.
- **Guards:** a command nested deeper than the parser follows (`bash -c`, `eval`, `$(...)` more than 4 levels) is refused
  instead of being parsed into nothing and allowed; on POSIX a backslash in `/tmp\x` is part of a file name, not a path
  separator, so it is no longer read as a temp path; `cursor build --dry-run` writes nothing.
- **Installers:** a failing `claude plugin ...` command, or a failed required health check, ends `install.sh`,
  `install.py` and `orkestra setup` with exit code 1 instead of "ready"; a marketplace registered from another checkout
  is registered again for this one; `install.sh` says when no PATH shim could be made; an unparsable `hooks.json` stops
  the uninstall before anything is removed (exit 1); `--purge` works through a symlinked home; a dry run leaves no
  `__pycache__`; the setup question takes `y`/`yes` (`e`/`evet` in Turkish) as a whole word, so `exit` and `yok` are no.
- **Language:** installer `--help`, the output of `orkestra cursor|copilot install|uninstall|status`, the details and
  summary of `orkestra doctor`, `argument ...:` usage errors, the watchdog LaunchAgent message and the bridge's last
  English sentences follow the language; both LaunchAgents carry `LANG` because launchd starts jobs without the user's
  locale. Row names, `[OK]`/`[--]`/`[!!]` and the `language:`/`source:` lines stay as they are for scripts; `ledger.md`
  stays English.
- **OpenClaw:** an approval option that carries a condition or a change (`Approve and skip T002`) reaches orkestra
  verbatim instead of as a bare `approve`.

### Also in this release
- Six stale Cursor skills were regenerated from the current Claude sources.
- The OpenClaw plugin's Node tests run in the Linux and macOS CI workflow.
- The bridge has a loopback-only local chat UI (the token stays in page memory) and project and engine discovery.
- The local UI shows a responsive phase map built from real run data, the active agent network, tasks, quality gates and
  the latest hook activity.
- The README install address and phase list were aligned with the code, and the CI Python matrix was widened to 3.14
  (keeping 3.9 as the minimum).
- The public-repository test verifies the root-anchored `dist/` and `internal/` ignore rules by behavior.

## 1.6.0 — 2026-10-06
Depoyu herkese açmaya hazırlık (dört açıdan denetim, her bulgu ayrıca çürütülmeye çalışıldı) ve zayıf bir modelin
yanlış yapabildiği dört yerin CLI'a alınması.
- **Lisans ve kaynaklar:** `THIRD_PARTY_NOTICES.md` (kökte ve eklenti klasöründe): dokuz kaynak projenin adresi,
  sabitlenen commit'i, lisansı, telif satırı ve klasördeki lisans dosyası. humanlayer (Apache-2.0) için lisansın tam
  metni eklendi (`LICENSE-APACHE-2.0.txt`; upstream'in kendi LICENSE'ı yalnız başlıktı, NOTICE dosyası yok).
  karpathy-skills için upstream'de LICENSE yok: MIT beyanını taşıyan `plugin.json` kanıt olarak eklendi, açıklamalı
  LICENSE yazıldı. Eklenti klasörüne Orkestra'nın kendi LICENSE'ı kondu. Bir test her vendor klasörünün lisansını ve
  bildirimdeki satırını denetler.
- **Kişisel veri:** manifestlerden e-posta çıkarıldı; özel proje adları ve run başlıkları genel adlarla değiştirildi;
  iç çalışma notları (`COORDINATION.md`, `integrations/ecc-karpathy/`, `integrations/openclaw/NOTES.md`) `internal/`
  altına taşındı ve `.gitignore`'a eklendi. Bir test e-posta, ev dizini yolu, sohbet kimliği ve bot token'ı arar.
- **Yabancı biri için:** İngilizce `README.md` (Türkçe belge `README.tr.md`), `CONTRIBUTING.md`, `SECURITY.md`,
  GitHub Actions iş akışı (henüz GitHub'da koşmadı), sürüm geri alma arşiv yerine etiketle. Varsayılan çalışma klasörü
  tek yerde; bridge istemcisi ve OpenClaw eklentisi kendi bulundukları kopyayı kullanır.
- **`task done` (M/L):** bu turun PASS review'u yoksa ya da review FAIL diyorsa CLI reddeder (playbook zaten böyle
  diyordu; zayıf bir orkestratör reviewer'ı hiç çağırmadan görevi kapatabiliyordu). XS/S ve sonradan eklenen görevler
  muaf. Kapatmak için: `~/.orkestra/config.json` → `tempo.task_review_required: false`.
- **`orkestra playbook`** adsız çağrılınca run'ın fazını kullanır; yanlış fazın playbook'u istenirse başına not koyar.
- **Başsız run:** her playbook'un başına "turu bitirme, `sleep 30; orkestra next` ile bekle" satırı gelir
  (playbook'lar "turu bitir" diyordu; başsız süreçte bu alt-ajanları öldürür).
- **`orkestra brief`** düzeltme turunda review dosyasının yolunu verir.
- Testler: 344 plugin + 16 bridge.

## 1.5.2 — 2026-10-06
Kurulumu (`install.sh`) elle çalıştırınca durduran iki şey; ikisi de yük ve terminalle ilgiliydi, akışla değil.
- Testlerdeki beş "doğrusal kalmalı" süre sınırı duvar saatinden CPU süresine geçti (`helpers.assert_cpu_under`;
  makinenin o anki yavaşlığı sabit bir döngüyle ölçülür, bütçe en çok 6 kat genişler). Yük altında biri 1,5 sn
  yerine 1,82 sn ölçüp `install.sh`'ı durdurmuştu; kod değil ölçüm yöntemi yanlıştı.
- Bridge `doctor`: `claude plugin list` klavyeyi (stdin) devralıyordu; terminalden elle çalıştırılan `install.sh`'ta
  60 sn bekleyip hatayla ölüyordu (borudan çalışınca görünmüyordu). stdin kapalı başlatılır, cevap gelmezse satır
  olarak yazılır. Aynı düzeltme `orkestra doctor`'daki `claude mcp list` ve `agent create-chat` çağrılarında.

## 1.5.1 — 2026-10-05
Cursor hook'u hafifletildi. Olay: Cursor IDE'de `/orkestra-run …` gönderildi, makine aşırı yüklüydü (yük ortalaması
230–385; aynı anda üç Playwright koşusu), kullanıcının kendi `safety-gate.py` hook'u 8 sn'lik süresini aşıp
`failClosed` olduğu için gönderimi engelledi. Orkestra engellemedi, ama o anda o da ağırdı: ilk istem için 5 `ps`
çağrısı + bütün modüller (ölçüm: o yük altında 7–19 sn; Cursor 15 sn'de kesti) ve aynı anda başlayan diğer hook'lara
yük bindiriyordu.
- **Hafif yol:** hiçbir run'ın tanımadığı bir sohbetin istemi, stop'u ve alt-ajan başlangıcı artık orkestra
  modülleri yüklenmeden ve süreç listesi okunmadan cevaplanır (yalnız küçük `intent` modülü). Orada onaylanacak,
  dürtülecek ya da kaydedilecek bir şey yoktur; geriye kalan defter işi yalnız bir şeyi GERİ ALIR (eski git izni,
  alınmamış not), bu yüzden köken denetimi gerekmez. İzin VEREN istem ("commit et"), sürücü duyurusu ve bir run'ın
  tanıdığı sohbetteki her şey eskisi gibi tam yoldan geçer. Ölçüm (yüksüz): ilk istem 138 → 57 ms, stop 136 → 35 ms,
  `ps` çağrısı 5 → 0.
- `subprocess`, `traceback`, `uuid` yalnız kullanıldıkları yerde yüklenir (bu betik her Cursor olayında başlar).
- Hook süreleri: 15 → 30 sn (sessionStart 20 → 30; subagentStart/sessionEnd 10 → 15). Süresi dolan hook "cevap yok"
  sayılır, yani çalışmamış bir guard'dır. Uygulamak için `orkestra cursor install` bir kez daha çalıştırılır.
- Denenip bırakılan: köken denetimini tek `ps -A` çağrısına indirmek — aynı yük altında tek çağrı 6–10 sn, süreç
  başına `ps -p` 0,1–0,8 sn sürdü; olduğu gibi kaldı.
- Testler: 336 plugin + 16 bridge (yeni: `TestLightPrompts`, 6 test; 13 mutasyonun 13'ü yakalandı).
- Orkestra'nın dışında kalan: `safety-gate.py` (8 sn, `failClosed`) aynı yük altında tek başına ~6 sn sürüyor.

## 1.5.0 — 2026-10-05
İki motor: bir run'ı Claude Code (varsayılan) ya da Cursor sürer; motor istek başına seçilir. Durum makinesi, kapılar,
kanıt ve guard'lar tektir — Cursor için ayrı bir kopya yok, yalnız bir çevirmen var.
- **Seçim:** terminal `orkestra run [--engine claude|cursor] -p <proje> "<istek>"` (o motorun kendi arayüzünü projede
  açar); Telegram `/yap cursor <proje> <istek>` (`cursor:` / `claude` da olur); bridge API'sinde `"engine": "cursor"`;
  Cursor sohbetinde `/orkestra-run <istek>`. Bir şey yazılmazsa Claude. Varsayılanı değiştirmek:
  `~/.orkestra/config.json` → `engine.default`. Run'ın motoru `state.json`, `request.md`, `status`, `watch` ve panelde.
- **`orkestra cursor install | uninstall | status | build`:** skill'leri (6) ve alt-ajanları (8) Claude kaynaklarından
  üretir (`plugins/orkestra/cursor/`, test bayatlamayı yakalar), `~/.cursor`'a bağlar, hook girdilerini
  `~/.cursor/hooks.json`'a **senin girdilerine dokunmadan** ekler (önce zaman damgalı yedek; bozuk ya da tanımadığı
  biçimde dosyaya hiç dokunmaz; symlink ise hedefine yazar) ve `orkestra` komutunu PATH'e koyar. Yinelenebilir.
- **`hooks/cursor_hook.py`:** Cursor olaylarını (sessionStart, beforeSubmitPrompt, preToolUse, postToolUse,
  postToolUseFailure, stop, subagentStart, sessionEnd) `dispatch.py`'nin aynı işleyicilerine çevirir, cevabı Cursor
  biçimine döndürür. Cursor'un gerçek davranışı ölçülerek yazıldı (`agent` 2026.10.01; örnek yükler
  `tests/fixtures/cursor/`):
  - Oturum `cursor-<sohbet kimliği>` olarak bağlanır. Her alt-ajan ayrı bir sohbettir ve olaylarında ajan türü yoktur:
    ana oturumun `Task` çağrısı kaydedilir, alt sohbet ilk mesajındaki Task metniyle eşlenir. İlk çağrıları transkript
    oluşmadan gelir; o sırada bekleyen başlatmanın rolüyle (birden çok tür bekliyorsa en kısıtlısıyla) değerlendirilir,
    kalıcı kayıt yalnız kesin eşleşmede yazılır. Run'ın projesi Cursor'un açıldığı klasör olmak zorunda değil.
  - Soru ve plan onayı: Cursor'da AskUserQuestion yok; başsız protokol geçerli (`orkestra await`, sen `onay` yazarsın,
    hook kaydeder). Baskı kipinde (`agent -p`) istem ve durma olayı gelmediği için bridge ve watchdog istemi hook
    katmanına kendileri bildirir (`engines.driver_prompt`); ajan kabuğundan gelen böyle bir bildirim reddedilir.
  - "O mesajda izin" (commit/push) ve tur başına en çok iki devam uyarısı kullanıcı istemine bağlıdır; Cursor aynı
    istem içinde üretim kimliğini değiştirdiği için tur ayrı izlenir. Oturum bitince izin de biter.
  - Cursor'un kabuğu çağrılar arasında dizinini korur ve olaylarda dizin gelmez: `cd` komut komut izlenir
    (`shellcwd.py`); izlenemeyen durumda (`cd "$X"`, `cd -`, pushd, alias/fonksiyon tanımı, `a || cd x` …) sonraki
    komut `cd <mutlak yol> && …` ile başlamak zorundadır. `make && cd build` gibi koşullu bir `cd`, komut sıfırla
    dönene kadar "bilinmiyor" sayılır. Göreli yol artık kabuğun gerçekten bulunduğu yere göre değerlendirilir; bir
    run'ın durum dosyalarıyla aynı adı taşıyan göreli yollar (`state.json`, `approval.json`, `evidence/…`) Cursor'da
    tam yolla yazılmak zorundadır.
  - Alt-ajanlar ön planda çalışır; Cursor bitişte olay göndermediği için dönüş, ana oturumun sonraki hamlesinden anlaşılır.
  - Bir orkestra run'ıyla ilgisi olmayan Cursor sohbetlerine dokunulmaz (hiçbir orkestra modülü yüklenmeden cevaplanır).
    Bir run sürmüş sohbet, run kapandıktan sonra da her-oturum guard'larına (git, sır, store) tabidir. Hepsini tüm
    Cursor sohbetlerine açmak: `engine.cursor.global_guards: true`.
- **Bridge / watchdog:** iş başına motor; Cursor işi kendi sohbetinde sürer (`agent create-chat`, `--resume`), duran iş
  aynı sohbette devam ettirilir. Hook'lar kurulu değilse Cursor turu başlatılmaz (guard'sız ajan çalışmaz) — her turdan
  önce bakılır. `doctor` Cursor satırı ekler. Claude komut satırı ve istemleri 1.4.0 ile aynıdır.
- **Kendi kendini onaylama (gerçek bir run'da görüldü, iki motor için kapatıldı):** istekte "soru sormana gerek yok"
  yazan bir denemede Cursor modeli onay kapısında orkestranın testlerini okudu, oradaki yükü bir `python3 <<'PY'`
  betiğine kopyalayıp `hooks/cursor_hook.py beforeSubmitPrompt`'u `{"prompt": "onay"}` ile kendisi çalıştırdı ve
  uygulamaya geçti. Üç katman eklendi: (1) bağlı oturumda orkestranın hook giriş noktalarını ya da `orkestra_core`'u
  **adlandıran** bir komut yalnız okuyabilir (grep, cat); çalıştıran ya da yazan komut ve bu adları içeren dosya içeriği
  (Write/Edit) `internals` ile reddedilir — orkestra deposunun kendisi hariç; (2) Cursor hook'u kendisini ajanın
  başlattığını tanır (ortamında ajan kabuğunun işaretleri ya da üst süreçlerinde Cursor'un ajan-komutu sarmalayıcısı
  varsa hiçbir şey kaydetmez; `beforeSubmitPrompt`, `stop`, `sessionEnd`, `postToolUse` dahil); (3) onay playbook'u,
  başsız playbook ve `phase next` reddi bunun kullanıcının rızasını taklit etmek olduğunu ve "sorma" demenin onayı
  kaldırmadığını açıkça söyler. Kaydedilen komut regresyon testidir. Sınır: ortamını temizleyip üst süreçlerinden
  kopan, adları gizleyen bir betik bunları aşar — guard'lar bir kum havuzu değildir.
- **Diğer guard değişiklikleri (iki motor):** Grep'te `klasör/**` deseni artık "sır dosyası hedefliyor" sayılmıyor
  (`*` ve `*.*` gibi; `.env*`, `**/.*` hâlâ ret). `/orkestra-ship` ve `/orkestra-rollback` (Cursor adları) aynı
  izinleri verir. Bozuk (UTF-8 olmayan) durum dosyası artık "yok" sayılır, hook'u düşürmez.
- **Metinler:** playbook ve skill'ler tek kaynaktan; Cursor'a verilirken adlar çevrilir, yalnız Claude Code'da doğru
  olan cümleler (arka plan ajanı, "turu bitir", SendMessage, /compact) Cursor karşılığıyla değişir
  (`engines.CURSOR_REWRITES`; kaynak cümle değişirse test kırılır).
- **Doğrulama:** gerçek Cursor CLI ile beş XS run (dördü bridge üzerinden başsız, biri terminal arayüzünde): onay
  öncesi kaynak yazımı ve izinsiz commit reddi, yazılan `onay`ın kaydı, implementer / verifier / reviewer alt-ajan
  rolleri, devam uyarısı, kabuk dizini izleme. Dördüncü run yukarıdaki kendi kendini onaylamayı ortaya çıkardı;
  düzeltmeden sonra aynı istekle beşinci run onayı bekledi. Bağımsız inceleme iki turda yapıldı: ilk tur 1
  engelleyici + 7 önemli, ikinci tur (düzeltmeler ve yeni katmanlar üzerinde) 1 önemli (başarısız olan bir `&&`
  zincirinden sonra dizinin yanlış bilinmesi) + küçük bulgular verdi; hepsi düzeltildi ve teste bağlandı
  (38 mutasyonun 38'i testlerce yakalandı). Testler: 330 plugin + 16 bridge
  (Python 3.9, 3.10, 3.14).
- **Bilinen sınırlar:** Cursor IDE penceresi doğrudan sürülmedi (CLI ile aynı hook sözleşmesini kullanır; IDE'ye özgü
  davranış gözlenmedi). Cursor'da implementer için "yeşil kanıt olmadan bitirme" uyarısı yok (alt-ajan bitiş olayı
  gelmiyor; `task done` kapısı aynen geçerli). Cursor run'larında token sayımı yok. Cursor, Claude Code'un eklenti
  kaydındaki orkestra kopyasını da yükler; o kopya eskiyse `orkestra cursor status` söyler.

## 1.4.0 — 2026-10-05
Hız, tempo denetimi ve kesintisiz devam.
Ölçüm (4–5 Ekim, 5 run): duvar saatinin %12–34'ü "alt-ajan bitti, ana oturum bekleme döngüsünde" geçmiş; 116 yeniden
açmanın 35'i yalnız minor içindi; bir onay "Onayla + ek kapsam" seçimi yüzünden ret yazılmıştı.
- **Ölü bekleme (A1):** Claude Code alt-ajanları arka planda çalıştırıyor ve raporu yeni mesajla getiriyor. `Stop` hook'u
  artık alt-ajan çalışırken turun bitmesine izin verir (eskiden engelliyordu; model de `sleep`/python döngüsüyle
  bekliyordu). Yeni `wait` guard'ı ana oturumun bekleme/yoklama komutlarını reddeder. Alt-ajan kaydı:
  `.orkestra/runs/<run>/agents.json` (hook'lar yazar; `SendMessage` ile devam ettirilen ajan da izlenir).
  Başsız (`claude -p`) çalışmalarda eski davranış korunur.
- **`orkestra next` (U1):** sıradaki tek adım; görev alt-adımı (started · red · green_stale · review_pending ·
  review_fail_rN · review_pass) state + kanıt + inceleme dosyasından türetilir. `context`, `resume`, `status`,
  SessionStart notu, Stop mesajı ve alt-ajan raporu geldiğinde eklenen bağlam bunu kullanır.
  `task start` / `task done` idempotent: yeniden çağrı hiçbir şeyi iki kez yazmaz; bitmiş görev `task start` ile açılamaz.
- **Tempo (U3):** `task done` / `phase next` çıktısında tek satır (`tempo: …`), bayrak varsa ne yapılacağıyla birlikte.
  `orkestra tempo`: faz ve görev süreleri, ret sayısı, ölçülen ölü bekleme payı.
- **Duraklama sınıfları (U2):** 1 kullanıcı · 2 ölü bekleme · 3 asılı komut · 4 sessiz oturum · 5 döngü. `watch` ve panel
  gösterir. Eşikler: `~/.orkestra/config.json` → `watchdog`.
- **`orkestra watchdog` (U4):** oturum dışı tarama + bir kez bildirim; sessiz başsız run'ı aynı oturumda devam ettirir
  (`auto_resume`: off | headless | all; en çok 3, artan aralık; canlı süreç / bridge işi varken asla). Bridge: duran işi
  aynı oturumda `/orkestra:resume` ile en çok 3 kez kendisi sürdürür ve taramayı dakikada bir çalıştırır.
  İkinci oturum aynı run'a bağlanırsa uyarı (ilk oturum son 2 dk içinde çalıştıysa) ya da devralma kaydı düşer.
- **İnceleme turları (A2/A4):** `task reopen`, son inceleme `PASS` ise reddeder (minor'lar `task note` ile final
  incelemeye taşınır); üçüncü tur kullanıcının kararını ister; `--force "<gerekçe>"` ledger'a yazılır.
- **Onay (A3):** yalnız onay sorusunun cevabı değerlendirilir; aynı çağrıdaki diğer cevaplar bağlama aktarılır.
  `<agent-message` / `<task-notification` / `[Subagent hand-back]` istemleri kullanıcı mesajı sayılmaz (onay, ret, git
  izni, "kullanıcı cevap verdi" yok); aktivitede `handback` olarak görünür.
- **Doğruluk (B):** soru açıkken run `awaiting_user` olur ve arka plan ajanı satırları soruyu gizlemez; dosyaları çakışan
  iki görev birlikte başlatılamaz; red ile green arasında ÇALIŞAN (ya da o arada biten) başka görevin test dosyası
  red→green kontrolünde sapma sayılmaz (`foreign_tests_changed` + ledger + rapor notu; çoktan bitmiş bir görevin
  testini değiştirmek hâlâ `--why` ister); biten run'ın token değeri state'e yazılır ve panelde kalır.
- **Guard düzeltmeleri (C1):** `2>&1` artık argüman sanılmıyor (`orkestra --help 2>&1` → "orkestra 2", `npm i x 2>&1` →
  paket "2" hataları); macOS `sed -i ''` betiği dosya sanılmıyor; `/tmp/x/*` geçici sayılıyor (kökün kendisi
  `/tmp/*` değil); `.orkestra/runs` altındaki kanıt ve aktivite kayıtlarını OKUYAN betikler serbest (makine durumu —
  state/approval/agents/gates — için 1.3.0'ın sıkı testi sürüyor). Salt-okur ajan ret mesajı deneme için `/tmp`
  yolunu söyler.
- **Sistem penceresi (C2):** yeni `dialogs` guard'ı + implementer / architect / plan-critic / reviewer / verifier kuralı.
- **Diğer:** `orkestra start` iki açık run varken uyarır (A7); `activity.jsonl` dolunca eski satırlar
  `activity.1.jsonl`'a taşınır (A8); 20'den fazla görevli planda bölme önerisi notu (D1); `orkestra changes` çıktısında
  deterministik `playbooks:` satırı (D5); bağlı olmayan oturuma "bu oturum run'ı sürmüyor" notu (D3).
- **Bağımsız inceleme, iki tur (yayın öncesi; 1. tur: 1 engelleyici + 6 büyük + 9 küçük, 2. tur: 2 büyük + 8 küçük
  bulgu; hepsi testli düzeltildi):**
  - Alt-ajan kaydı run'ı park edemez: kayıt, başlatan oturumu taşır; başka oturum run'ı devralınca sayılmaz; oturum
    yeniden başlayınca (SessionStart, `compact` hariç) ve rapor mesajı gelince düşer; `silent_seconds` boyunca hiçbir
    alt-ajan izi yoksa "kayıp" sayılır (`next` normal adımı söyler, not düşer). `orkestra resume` izi olmayan kayıtları
    düşürür; `orkestra agents [clear]` eklendi. `SendMessage` kaydı çağrı başarıyla döndükten sonra (PostToolUse) yazılır;
    arka plan başlatması yalnız yapısal olarak (`status: async_launched`) tanınır.
  - Başsız çalışmaya artık "turu bitir" denmez ("turun içinde bekle").
  - Güvenlik gerilemeleri kapatıldı: fd numarası atılınca yönlendirme hedefi kaybolmuyor (`exec 3>.env`, `true;2>dosya`,
    `;>dosya` artık yakalanıyor); betiklerle makine durumuna yazma biçimleri (`open(os.path.join(…),'w')`, `Path.open('w')`,
    `os.open`, `mode='w'`, perl/ruby/node) yeniden reddediliyor; CLI seçenek kısaltmaları kapalı (`allow_abbrev=False`) ve
    alt-ajanlar için bilinmeyen seçenek reddediliyor (`phase --no show next` ile `phase next` çalıştırılamıyor).
  - Önceden beri açık olanlar da kapandı: `orkestra evidence <etiket> <komut>` (`--` olmadan) içindeki komut artık
    guard'lardan geçiyor; `rollback --yes` konumdan bağımsız yakalanıyor; macOS'ta büyük/küçük harf farkıyla
    (`.ENV`, `.ORKESTRA/runs/…`) guard atlatılamıyor; AskUserQuestion girdisinin hiçbir yerine `answers` gömülemiyor.
  - `orkestra next`: yeniden açılan TDD'siz görev eski rapor yüzünden bitmiş sayılmıyor; son notu `BLOCKED` olan görev
    için karar isteniyor; yalnız `PASS` / `PASS_WITH_NOTES` geçer sayılıyor; `phase back implement` sonrası açık görev
    yoksa düzeltme görevi eklenmesi isteniyor (ping-pong yok).
  - Stop hook'u alt-ajan durdu ama raporu henüz gelmediyse dürtmüyor (çifte iş); hook'un yazdığı `awaiting_user` soru
    kapanınca kalkıyor; iki bind işaretinde tek JSON; rapor mesajı git iznini her durumda sonlandırıyor; yeniden onayda
    görev kayıtları (`review_seen`, `first_started`) korunuyor.
  - `wait` guard'ı: uygulama kodundaki `reviews/` yolu tetiklemiyor; salt bekleme yalnız kayıtlı alt-ajan varken
    reddediliyor; düzenli ifade doğrusal (17 KB'lık girdi 3,7 sn → < 0,1 sn).
  - `watchdog`: kendi başlattığı sürecin pid'i ve bağlı tüm oturum kimlikleri kontrol ediliyor; komut içinde ölen başsız
    oturum da sürdürülebiliyor. Bridge: tur sonu ile iptal yarışında iptal kazanıyor.
  - **Tek ayrıştırıcı (2. tur):** guard'lar `orkestra …` satırını artık CLI'nin kendi ayrıştırıcısıyla okur (yeni modül
    `orkestra_core/cliparse.py`); guard'ın gördüğü ile CLI'nin çalıştırdığı aynı şeydir. `evidence a <komut> --`,
    iç içe `orkestra evidence … orkestra task start …`, baştaki `--` gibi farklar kapandı; iç içe çağrılar sonuna kadar
    açılır (4 kat sınırı). `--` alt komuttan önce her Python sürümünde reddedilir.
  - "Başsız" tek yerde tanımlı (`paths.is_headless`: `mode == headless` ya da `ORKESTRA_HEADLESS=1`); bridge/watchdog'un
    sürdürdüğü etkileşimli run'da da Stop hook'u, bekleme guard'ı ve `next` aynı şeyi söyler.
  - Alt-ajan satırları ajan kimliğini (`aid`) taşır: aynı türden bir ajanın durması diğerinin çalışan komutunu
    kapatmaz. Kayıt, başka oturum yeni diye değil, başlatan oturum bir kira süresi (120 sn) sessizse düşer; aynı
    pencereden `resume` sessiz ajanı düşürmez; yolda olan her rapor ayrı sayılır (görev bildirimi rapor yerine geçmez).
  - Ayrıştırıcı: `>& dosya` yazma hedefidir; çalışma dizini `.orkestra/runs` olan ve heredoc ile beslenen betikler de
    denetlenir. Tek komutta 200'den fazla farklı yönlendirme hedefi ya da 2000'den fazla komut reddedilir; PreToolUse
    guard'ı 8 sn'de bitmezse hook zaman aşımına (= denetimsiz çalışma) bırakmak yerine açıkça reddeder (`complex`);
    büyük/küçük harfli özel `TMPDIR` geçici sayılır.
  - **Varsayılan KAPALI seçenek `guards.test_syntax`:** `[[ a > b ]]`, `(( n > 100 ))`, `$(( 5 > 3 ))` biçimlerini
    yönlendirme değil karşılaştırma olarak okur (salt-okur ajanlar ve onay öncesi fazlar bu biçimlerde boşuna
    engelleniyor — 1.3.0'dan beri böyle). Tanıyıcı dar tutuldu (yalnız komut konumundaki tırnaksız `[[ … ]]`; POSIX
    `sh`/`dash` içinde hiç; aritmetik gövdesi doğrusal denetleyiciyle) ve bash'e karşı doğrulandı; zsh'e karşı
    doğrulanmadığı ve bağımsız incelemenin son turu tamamlanamadığı için kapalı gönderiliyor. Kapalıyken davranış
    1.3.0'daki katı okumadır (yanlış ret olabilir, kaçan yazma olmaz).
- Testler: eklenti 112 → 228, bridge 6 → 9.

## 1.3.0 — 2026-10-05
ECC + karpathy-skills bağlaması (W1–W8; açık run yokken uygulandı):
- implementer: cerrahi diff, en sade kod, sessiz varsayım yok (karpathy).
- reviewer: sessiz hata avı, görevle izlenemeyen satır = bulgu; isteğe bağlı kontrol listeleri
  (`orkestra playbook security | stack-web | stack-mobile | data`) yalnız diff eşleşince yüklenir.
- adversary: derin listeler (`security`, `ui-flow`, `data`). architect: varsayımı yüzeye çıkar, önce mevcut kod /
  kurulu bağımlılık. plan-critic: FR'ye izlenemeyen görev = bulgu. verifier: `ui-flow`, `stack-mobile`.
- `run` skill: `/compact` yalnız faz sınırında önerilir. `vendor/PINNED.md`: ecc + karpathy satırları;
  `scripts/refresh-vendor.sh` artık `refresh-vendor-ecc.sh`'yi de çağırır.
- intake: hedef proje orkestra'nın kendisiyse katkı kuralları kuralı hatırlatılır.
- katkı kuralları: üç agent (orkestra-core, openclaw-agent, ecc-karpathy), sahiplik tablosu,
  "açık run varken plugin dosyası değiştirilmez" ve değişiklik öncesi yedek/md5 kuralı.
- Aşağıdaki 1.2.1 sonrası değişiklikler de bu sürüme dahildir:
- `watch`/`dashboard`: çalışmalar sabit sırada (açık olanlar önce, sonra proje adına göre) — son hareket edene göre
  yer değiştirmiyor.
- `watch`/`dashboard`/`watch --json`: çalışma başına token kullanımı (`usage`: girdi, çıktı, cache yazma, cache okuma,
  bağlam). Kaynak: bağlı oturumun ve alt-ajanlarının Claude Code kayıtları (`~/.claude/projects/…`); salt-okunur, yalnız
  sayılar okunur, çalışmanın başlangıcından itibaren sayılır. Yeni modül `orkestra_core/usage.py`, testler
  `tests/test_usage.py`. Hook'lar ve CLI akışı değişmedi (monitor yalnız `watch`/`dashboard` içinde yüklenir).
- Yeni, kimsenin çağırmadığı dosyalar: `vendor/ecc`, `vendor/karpathy`, 5 isteğe bağlı playbook, `scripts/refresh-vendor-ecc.sh`.

## 1.2.1 — 2026-10-04
- `watch`/`dashboard`: clarify/approve fazında hareket yoksa "muhtemelen SENİ BEKLİYOR" gösterilir (soru/onay
  penceresi açık kalmış olabilir).

## 1.2.0 — 2026-10-04
Canlı takip:
- Hook'lar her çalışmanın aktivite akışını yazar: `.orkestra/runs/<run>/activity.jsonl` (araç çağrıları, alt-ajan
  başlangıç/sonuçları, guard engellemeleri, sorular/cevaplar, tur sonu). Boyut sınırlı (512 KB), sır görünümlü değerler
  maskelenir, iş akışını asla bloklamaz.
- `orkestra watch [--once|--all|--json] [-p proje]`: tüm açık çalışmalar tek ekranda; "şu an ne yapıyor / seni mi
  bekliyor / oturum kapandı mı" tespiti.
- `orkestra dashboard [--port 47616] [--open]`: aynı görünüm tarayıcıda; yalnız 127.0.0.1, salt-okunur,
  DNS-rebinding korumalı.
- Bridge: Telegram `/durum` artık canlı durumu da gösterir; `GET /runs`; `orkestra_client.py runs` (eklemeli,
  kırıcı değişiklik yok).

## 1.1.2 — 2026-10-04
- Bridge varsayılan portu 8787 → 47615 (8787 ve 18787 game-app'in dev/e2e sunucularında). Eski 8787 ayarı
  otomatik taşınır.

## 1.1.1 — 2026-10-04
- Serena MCP düzenleme araçları (`safe_delete_symbol` dahil) orkestra guard'larına tabi; Serena ile yapılan
  düzenlemeler de yedeklenir ve rollback kapsamına girer.

## 1.1.0 — 2026-10-04
Kanıt katmanı (halüsinasyona ve "test oyunlarına" karşı deterministik kurallar):
- `plan check`: plandaki dosyalar gerçekten var olmalı; yeni dosyalar `(yeni)` ile işaretlenir.
- `tdd`: green, red ile aynı komutla koşulur; red sonrası test değişirse reddedilir (`--why` ile gerekçe raporda görünür);
  TDD görevi test dosyası değişmeden kapanmaz.
- `integrity check`: silinen test, eklenen skip/only/xfail, eklenen lint/type bastırma → verify geçmez
  (istisna yalnız onaylı planın `## İstisnalar` bölümünde).
- `verification.md`: var olmayan dosya/test adı gösterilemez; her gereksinimde doğrulanabilir referans;
  elle kontrol `orkestra evidence` logu ister.
- `report evidence --write`: raporun kanıt bölümü makine verisinden üretilir; elle değiştirilirse kapanış reddedilir.
- `orkestra tdd/evidence ... -- <komut>` içindeki komutlar da guard'lardan geçer.
- `.orkestra/current` kısa yolu (glob gerekmez).
Telegram / OpenClaw:
- `orkestra_bridge.py telegram-setup` (tek komut), `doctor [--live]`, mutlak claude yolu (launchd).
- `bridge/orkestra_client.py` (start/reply/status/wait/cancel) ve `integrations/openclaw/CONTRACT.md`.
- katkı kuralları: çok agent'lı çalışma kuralları (Telegram tek tüketici, sahiplik).

## 1.0.0 — 2026-10-04
İlk sürüm: faz makinesi, guard'lar, agent ekibi, gates, rollback, hafıza, bridge.
