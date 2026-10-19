# Contributing

Thank you for looking into Orkestra. By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
A few rules keep it safe to change.

1. **Do not edit hook, guard or agent files while a run is open.** Cursor and Copilot read the checkout, and Claude
   Code picks a change up at the next `orkestra setup`, so a half-edited `hooks/`, `lib/orkestra_core/` or `agents/` file
   takes effect in running sessions.
   Work on a copy or a branch; `orkestra watch` shows open runs.
2. **Every change comes with a test** that fails without it. The suite must stay green on Python 3.9 and on the
   newest Python you have:
   ```bash
   cd plugins/orkestra/tests && python3 -m unittest discover -s . -q
   cd bridge/tests && python3 -m unittest discover -s . -q
   cd integrations/openclaw/plugin && npm test          # logic-only (CI)
   cd integrations/openclaw/plugin && npm run test:e2e  # optional e2e; needs OPENCLAW_PKG
   ```
   The Python suites use only the standard library; the OpenClaw logic suite uses Node's built-in test runner and has no npm dependencies. OpenClaw e2e is optional and skips when `OPENCLAW_PKG` (default Homebrew path) is missing.
3. **Limits on time are CPU budgets, not wall-clock** (`helpers.assert_cpu_under`); tests must pass on a busy machine.
4. **Generated files.** `plugins/orkestra/cursor/` is generated from the Claude sources:
   `python3 plugins/orkestra/bin/orkestra cursor build`. A test fails when it is stale.
5. **Text the model reads is English**; say what to do and give the exact command. If the CLI can decide
   something deterministically, put it in the CLI rather than in a playbook.
   Text a human reads (dashboards, `watch`, notifications, run artifacts, the installer, bridge replies, `--help`) is
   English and Turkish: add the message to the catalogs in `plugins/orkestra/lib/orkestra_core/messages/` and call
   `i18n.t(...)`; `test_i18n` fails on a key that exists in one language only, on Turkish letters in English text
   and on Turkish letters in code outside the catalogs. New tests cover both languages (`ORKESTRA_LANG=en|tr`).
6. **Third-party text.** Do not paste text from other projects into playbooks or prompts. References go under
   `plugins/orkestra/vendor/<project>/` with the upstream licence file, a row in `vendor/PINNED.md` and in
   `THIRD_PARTY_NOTICES.md`. Only permissive licences (MIT, BSD, Apache-2.0).
7. **A Telegram bot token has one consumer.** If another program already polls your bot, do not give the same
   token to the bridge (Telegram answers 409 Conflict); use the HTTP API of the bridge from that program instead
   (`integrations/openclaw/CONTRACT.md` is an example).
8. Do not commit `dist/`, `internal/`, secrets, or anything under `~/.orkestra`.

## Changing hooks and guards

A hook runs on every tool call of every session, so two things are pinned by tests: how long it takes and what it decides.

**Latency budget.** `tests/test_hook_latency.py` measures the real `hooks/dispatch.py` against a frozen copy of the
previous entry script (`tests/data/dispatch_pre_c2.py.txt`), interleaved in the same run. A guarded `PreToolUse` and a
quiet event must each cost at least 35 percent less CPU than that script and stay under an absolute CPU budget that is
scaled by a reference loop (so a loaded machine widens it, a fast one does not). The same file pins the import graph:
an event that does nothing loads `json`, `os`, `paths` and `state` only; a guarded one never loads `runs`, `tempo`,
`evidence`, `snapshot`, `engines`, `argparse`, `subprocess` and friends. So: do not add a top-level import to
`hookmain.py`, `hookboot.py` or `guards.py`; import inside the function that needs it, or use `lazy.LazyModule`. Never
measure with wall-clock time in a test; use CPU time, an injected clock or a generous bound.

```bash
cd plugins/orkestra/tests && python3 -m unittest test_hook_latency -q
```

**Guard decision corpus.** `tests/data/guard_decisions.json` holds more than 5000 recorded `PreToolUse` events (shell lines, file
tools, MCP, agents; bound and unbound sessions; several roles and grants; Windows-style paths) with the decision each
got: allowed, or denied by which guard. `test_hook_latency` replays all of them, so a faster hook can never change what
a guard allows or denies. Strictness is the contract: a decision may become more permissive only to fix a documented
false positive, and then a test must show that the true positive is still denied.

To add a case:

1. On a clean tree run `python3 guard_corpus.py --check` (in `plugins/orkestra/tests`); it must print `0 differences`.
2. Add the command to `BASH_MULTI` in `guard_corpus.py`, at the **end** of the list (the wrappers are chosen by position,
   so an insertion in the middle would re-key unrelated rows), or a path to `FILE_PATHS` or a tool call to `MCP` /
   `OTHER_TOOLS`. Paths use the placeholders `{P}` (bound project), `{O}` (outside), `{H}` (ORKESTRA_HOME), `{U}` (home).
3. Run `python3 guard_corpus.py --write`. It records what the code decides **today** for every event, which is why step 1
   comes first and why `--write` is never a way to make a failing replay pass.
4. Read `git diff --stat tests/data/guard_decisions.json`: only added rows are expected. If an existing row changed, a
   guard decision changed; that needs a stated reason in the pull request and the same reason in the test that proves it.
5. Run `test_hook_latency` and `test_guards` / `test_guards2`.

**Stalls and loops.** Anything that decides "stuck" must be a plain predicate on rows, registry files, counters and
clocks (see `lib/orkestra_core/fastwatch.py`: pure functions of a `RunCtx`, tested on synthetic timelines), never "the
model has been quiet": a thinking model is not a stall. Every loop in the engine needs a cap and a test of the cap
(`tempo.max_dispatches`, the deny escalation, the review rounds, the Stop-nudge ceiling), and every wait needs a deadline
(`util.file_lock` timeouts, the hook deadline, `orkestra wait --max`). Timing tests inject the clock; stress tests stay
bounded (at most 16 processes, at most 60 s).

**Running the CLI by hand next to a live run.** The CLI prints `[orkestra:bind]` / `[orkestra:unbind]` marker lines
and the hooks act on them. A marker names its `ORKESTRA_HOME` and a hook ignores one for another home, so test by hand
only with a throw-away `ORKESTRA_HOME` and `ORKESTRA_WORKSPACE`, and redirect the output to a file
(`orkestra ... > out.txt 2>&1; grep -v '^\[orkestra:' out.txt`) instead of letting a marker line reach a live session.

## Adding or changing text

Orkestra speaks English (the default) and Turkish. Who reads the text decides where it lives:

| Who reads it | Where it lives | Language |
| --- | --- | --- |
| The model (skills, agents, playbooks, `orkestra next` hints, guard messages) | In place, in the source file | English only |
| A human (dashboards, `watch`, activity and notification lines, run artifacts, installer output, bridge replies, `--help`) | The catalogs in `plugins/orkestra/lib/orkestra_core/messages/<area>.py` | English and Turkish |
| Machine consumers (JSON keys, status values, HTTP error codes, state files) | In place | Never localized |

Steps for a new or changed human-facing message:

1. Add the key `"<area>.<name>"` to **both** the `EN` and the `TR` dict of the area module. Both texts use the same
   `{named}` fields; the English text has no Turkish letters; the Turkish text is a real translation, not a copy.
   Write natural text in each language and do not use the em dash (U+2014) in new text: use `:`, `.` or ` · `.
2. Read it with `i18n.t("<area>.<name>", field=value)`. Pass values as fields instead of gluing translated pieces
   together. `i18n.pick(en, tr)` is for the rare one-off only.
3. Protocol tokens are not display text: section headings of `request.md`, `spec.md`, `plan.md` and `report.md`, and the
   AskUserQuestion headers and option labels for plan approval and rollback live in `messages/protocol.py` (`SECTIONS`,
   `PROMPTS`) and in `templates/en/` and `templates/tr/`. Every parser accepts **all** spellings (`protocol.fold()` ignores
   case, diacritics and punctuation), so a run started in one language continues in the other and old Turkish runs keep
   working. Never remove a spelling; add the new one beside it, and keep `templates/en/` and `templates/tr/` identical in
   placeholders and table skeleton.
4. User-intent vocabulary (`intent.py`: `commitle`, `onayla`, `commit this`, `approve`) only grows.
5. Write the test for both languages. The plugin suite is pinned to Turkish (`tests/helpers.py` sets
   `ORKESTRA_LANG=tr`) so older assertions stay valid, so a new test sets the language itself:
   `mock.patch.dict(os.environ, {"ORKESTRA_LANG": "en"})`, or `env={"ORKESTRA_LANG": "en"}` for a subprocess. For
   English output assert that no Turkish letter appears (`[çğıöşüÇĞİÖŞÜ]`), except where a user's own text is echoed.
6. Run the narrow gates, then the full suites:
   ```bash
   cd plugins/orkestra/tests && python3 -m unittest test_i18n test_docs_drift -q
   ```

What `test_i18n` enforces for you: a key that exists in one language only fails; `{fields}` that differ fail; Turkish
letters in English text fail; an untranslated copy in `TR` fails; Turkish letters anywhere in the Python sources outside
the catalogs and the vocabulary files (`i18n.py`, `intent.py`, `util.py`, `guards.py`, `engines.py`) fail.

### Documentation

`README.md` and `README.tr.md` mirror each other: same sections, same commands, same order. Change both in one commit
(`ONBOARDING.md` and `ONBOARDING.tr.md` the same way; `ORKESTRA.md` and `ORKESTRA.tr.md` likewise). Name only commands
and flags that exist: `tests/test_docs_drift.py` parses the docs against the real CLI parser, checks that relative links
and anchors resolve, that the two languages mirror each other and that version strings match. The architecture page is
`docs/architecture.html` (English) and `docs/orkestra-mimari.html` (Turkish); there is no second copy at the repo root.

## Adding a language

English and Turkish are the supported languages today. The message catalogs are built per language, but a few places
still hold the two languages as a pair (`SECTIONS` and `PROMPTS` in `messages/protocol.py`, `i18n.pick(en, tr)`,
`i18n.heading()`, the installers' two-argument `L` helper). A third language therefore starts with a small
generalization of those places. Please open an issue first so that the language code and a reviewer who reads that
language are agreed. The places to touch, in order:

1. `lib/orkestra_core/i18n.py`: add the code to `SUPPORTED` and its names to `_NAMES`; make `pick()`, `heading()` and
   `template_dir()` work for more than the `tr`-or-else rule.
2. `lib/orkestra_core/messages/*.py`: next to `EN` and `TR`, add a dict named by the upper-case code (for example `DE`) in
   **every** area listed in `i18n.AREAS`, with the same keys and fields (`i18n._load()` reads `getattr(mod, CODE)`).
3. `messages/protocol.py`: extend the `SECTIONS` pairs and the `PROMPTS` wording contract with the new spelling. Parsers
   already accept every spelling; the tests then confirm it.
4. `plugins/orkestra/templates/<code>/`: copy `templates/en/` and translate it. Placeholders, table shape and comments
   must match `en/` (`test_i18n_templates`).
5. Entry points outside the catalogs: `cliparse.LANG_CHOICES` and `messages/help.py`; `install.sh` (`lang_of`, `L`),
   `install.py` (`_MESSAGES`), `install.ps1`, the error text in `hooks/run_hook.sh`; `bridge/ui/i18n.js`;
   `integrations/openclaw/plugin/lang.js` and `messages.js`.
6. `lib/orkestra_core/intent.py`: the phrases for "commit", "push", "approve" and "reject" in the new language.
7. Documents: `README.<code>.md`, `ONBOARDING.<code>.md`, `ORKESTRA.<code>.md`, `integrations/openclaw/CONTRACT.<code>.md`,
   an architecture page, and the language switch links at the top of each.
8. Tests: widen every loop that names `en` and `tr` (`test_i18n*`, `test_cli_ux`, `test_bilingual_e2e`, the bridge and
   OpenClaw suites), and add an end-to-end run in the new language next to `test_bilingual_e2e`.

## Before you open a pull request

The pull request template has the checklist. In short: both Python suites green on Python 3.9 and your newest Python,
the OpenClaw logic suite if you touched it, `python3 plugins/orkestra/bin/orkestra cursor build` run when a Claude
source (agents, skills, hooks) changed, catalog parity (`test_i18n`) and `test_docs_drift` green, and no secrets,
tokens, personal paths or private sibling repositories in the diff.

By contributing you agree that your contribution is licensed under the MIT License of this repository.
