# OpenClaw → Orkestra contract (written by orkestra-core, stable)

Türkçe: [CONTRACT.tr.md](./CONTRACT.tr.md) (same contract, Turkish text; keep the two in step).

OpenClaw calls Orkestra through the **bridge**. OpenClaw's own config and skill structure belong to the
openclaw-agent; this document defines the Orkestra side only.

## Prerequisites (same machine)

- `bash ./install.sh` (or `$ORKESTRA_ROOT/install.sh`; installs the plugin and writes the token to `~/.orkestra/bridge.secret.json`)
- The bridge is running: `python3 ./bridge/orkestra_bridge.py serve` (on macOS also `... launchd`)
- `python3 ./bridge/orkestra_bridge.py doctor` reports ready

## One consumer per Telegram bot

If OpenClaw already polls your Telegram bot, **the bridge does not poll Telegram** (leave `telegram.bot_token` empty).
The user's messages reach OpenClaw; OpenClaw forwards the Orkestra commands to the bridge with the client below.

## Recommended command language (what the user sees)

Every command has an English and a Turkish name; both always work, in both languages.

| User writes                                                   | OpenClaw runs                                                                    |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `/run <project> <request>` (Turkish: `/yap`)                  | Start a new job with the bridge's default engine                                 |
| `/run claude: <project> <request>`                            | Pick Claude Code                                                                 |
| `/run cursor: <project> <request>`                            | Pick the Cursor CLI                                                              |
| `/run copilot: <project> <request>`                           | Pick the Copilot CLI                                                             |
| an answer to a pending question or approval (e.g. `approve`)  | `python3 ./bridge/orkestra_client.py reply --chat-id <chat> "<text>"`            |
| `/runs` (Turkish: `/durum`)                                   | `python3 ./bridge/orkestra_client.py status --chat-id <chat>`                    |
| `/cancel` (Turkish: `/iptal`)                                 | `python3 ./bridge/orkestra_client.py cancel --chat-id <chat>`                    |
| `/live` (suggestion; `/runs` includes it; Turkish: `/canli`)  | `python3 ./bridge/orkestra_client.py runs` → plain text; `--json` for structure   |

The bundled OpenClaw plugin (`integrations/openclaw/plugin`) registers all six names. The English name of `/durum` is
`/runs` because OpenClaw reserves `/status` for itself; a name the host refuses is skipped with a warning and never
stops the plugin.

Alternative (one endpoint): forward every message as it is with `POST /telegram {"chat_id", "text"}` and send the
`message` of the reply to the user. The bridge tells commands and answers apart itself: `/run` or `/yap`, `/status` or
`/durum`, `/cancel` or `/iptal`, `/help` or `/yardim`; anything else is the answer to the job waiting for the user.

## Language

Everything a person reads (Telegram replies, notifications, the web UI, CLI output) is English or Turkish, English by
default. One setting decides for the bridge, the CLI and the plugin, first hit wins:

1. environment variable `ORKESTRA_LANG` (`en` | `tr` | `auto`)
2. `"lang"` in `~/.orkestra/config.json` (`en` | `tr` | `auto`; a running bridge and plugin pick up a change)
3. the OS locale (`LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`; a `tr...` locale selects Turkish)
4. English

What does **not** change with the language: every JSON key and enum of the HTTP API (`status`, `phase`, ...), the HTTP
`error` codes (`unauthorized`, `not found`, ...), the command vocabulary above, and the text the model reads.

Protocol tokens are accepted in both languages, so a run started in one language can be continued in the other:

- The notification header is `[project] <status label> · <phase> · job <id>` in English and `... · iş <id>` in
  Turkish; the start confirmation is `Started: [project] job <id>` / `Başladım: [project] iş <id>`. A client that
  routes a Telegram reply to its job must accept both job words (`job` | `iş`) followed by the 12-hex job id; the
  plugin does (`parseJobRef`).
- A chosen approve option is forwarded as the approval word of its language (`approve` / `onayla`); Orkestra accepts
  both. Options with conditions stay verbatim.

## Notifications (Orkestra → user)

When a job asks a question, waits for approval, finishes or is blocked, the bridge produces a notification:

- `bridge.secret.json → callback_url`: given OpenClaw's incoming-webhook address, the bridge POSTs this JSON:
  `{"id","project","status","phase","run_id","chat_id","message"}`. `message` is the text to send to the user as it is
  (in the bridge's language). `status`: `awaiting_user` (waiting for an answer) · `done` · `blocked` · `cancelled` · `error`.
- If a webhook is not possible, OpenClaw polls `orkestra_client.py status --chat-id <chat>` periodically and forwards the
  `last_message` of jobs whose `updated` changed.

## HTTP API (127.0.0.1:47615, `Authorization: Bearer <bridge.secret.json → token>`)

- `POST /jobs {project, request, chat_id?, source?, engine?}` → 202 job · 400 when the project already has an open job
  (`engine`: `claude` | `cursor` | `copilot`; the default comes from the bridge/orkestra configuration)
- Any request may add `?lang=en|tr`: the texts the bridge writes for that answer (errors, live status, engine reasons)
  then use that language instead of the bridge's own. JSON keys, status values and HTTP codes never change.
- `POST /jobs/<id>/reply {text}` · `POST /reply {chat_id, text}` → 202
- `GET /jobs[?chat_id=]` · `GET /jobs/<id>` · `GET /health` → `{"ok": true, "lang": "en" | "tr"}` (`lang` was added with the language support; additive)
- `GET /runs` → `{"runs": [...]}` (1.2.0+, additive): for every open run `project_name, run_id, phase, status,
  live{level,text}, tasks_done, tasks_total, activity[{time,agent,kind,tool,what}]`. `live.level`:
  `run|wait|quiet|stale|blocked|paused|done`; `wait` = waiting for an answer from the user.
- `POST /telegram {chat_id, text}` → `{message}` (the command language above; only `allowed_chat_ids` when it is not empty)
- `GET /projects` → `{"projects": [{"name"}], "restricted": bool}`: the folders a job may be started in (the allowlist
  applied). The client prints it with `orkestra_client.py projects`.

`orkestra_client.py` tries a connection again (after 0.3, 1 and 2 s) only when the request never reached the bridge
(connection refused while it restarts, no free local port on a busy machine); a request that reached it is never sent
twice. It ignores `HTTP(S)_PROXY` for the local bridge and always prints JSON, also for an answer that is not JSON.

## Plain-language requests (the bundled plugin)

Besides the commands, the plugin gives the OpenClaw agent two tools: `orkestra_run {project, request, engine?}` and
`orkestra_status {}`. When the owner writes "fix the notification sound in namaz vakitleri" the agent starts the run
with `orkestra_run`, which takes the same path as `/run`: the name is matched to a folder (a spelling that folds to
exactly one folder is corrected, otherwise candidates are listed), a project with an open run is refused (fail closed),
and the job's questions come back as notifications answered with Reply. The agent never answers a run's question.
The tools are declared in `openclaw.plugin.json` (`contracts.tools`); a limited tool profile (for example `coding`)
offers them to the model only when they are in `tools.alsoAllow`.

## Owner commands (passphrase)

A message from the owner's chat that contains the owner's passphrase is handled by the plugin itself and never reaches
the agent or its transcript. The config holds only `ownerCommand: {salt, sha256, words}`: the hex SHA-256 of
`salt + "\n" + the phrase folded to lower-case ASCII letters and digits, spaces removed`
(`logic.js` `ownerPhraseDigest`), so capitals, Turkish letters, spaces and punctuation do not matter. The
plugin then:

1. starts the bridge when it is down (`orkestra_client.py ensure`: `launchctl kickstart` of `com.orkestra.bridge`
   without `-k`, then `bootstrap`, else a detached `serve`);
2. reads the project from the message (folder names with spaces or Turkish suffixes); when none or several match it
   asks once, with `[owner <code>]` in the question, and a Reply with the letter or the folder name starts the job
   (valid 30 minutes, no passphrase needed again);
3. starts the job with `owner: true` (`POST /jobs`, accepted only from `source` `openclaw` or `cli`). The bridge runs it
   on the claude engine with `owner_model` / `owner_fallback_model` / `owner_effort` (default `fable`, `opus`,
   `xhigh`), autopilot, up to `owner_max_auto_resumes` (8) automatic continuations, and writes an owner grant for the
   job's session (`~/.orkestra/grants/owner/<session>.json`, renewed every turn, removed when the job ends or is
   cancelled, 12 h at most).

With the owner grant the hooks allow commit, push, branch, PR, tag, merge/rebase/pull, stash, GitHub writes and deploy
or release commands. What cannot be undone stays refused even when the request asks for it: force push, amend, hard
reset or `git clean`, discarding changes, recursive deletes outside a temporary folder, and remote actions that delete
or drop (`destroy`, `delete`, `rm`, `reset`, `down`, `drop`, `truncate`, rsync `--delete`, schema pushes and the like),
as well as secret files and EAS cloud builds. The open-run rule of `/run` applies too.

`/ide` or a short message such as "restart the ide" (Turkish wording works too) runs `orkestra_client.py revive`: the bridge, the dashboard
(127.0.0.1:47616) and the apps in `ideApps` (default `["Cursor"]`) are checked and started when they are down, and the
answer lists each one.

## Guarantees

- Headless runs pass through the same guards as interactive ones: no commit, push or deploy, secrets are never read;
  autopilot is the default, and a manual plan approval is recorded only from a real user message.
- Copilot bridge jobs start through the `copilot` CLI with the active Orkestra hook plugin. The default profile is
  `gpt-6-luna` / `max`; when the request names exactly one supported model or effort it is used, an invalid or ambiguous
  choice falls back to the default.
- OpenClaw must not send this on the user's behalf.
- One open run per project; a second request returns 400.
- `allowed_projects` (bridge.secret.json) limits which projects can be started remotely.
