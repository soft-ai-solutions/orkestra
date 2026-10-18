"""bridge: messages the HTTP/Telegram bridge shows to people (English + Turkish). Keys are `bridge.<name>`; both dicts
must hold the same keys and {fields}.

Scope: Telegram/webhook replies, notifications, the error replies of the HTTP API, the CLI output of
`bridge/orkestra_bridge.py`, `orkestra_client.py` and `n8n_import.py`. NOT here: API JSON keys and enums (stable), the
model-facing note appended to a request that came from an external trigger (English only), the local web UI (its own
table in `bridge/ui/i18n.js`, same policy) and the OpenClaw plugin (`integrations/openclaw/plugin/messages.js`, which
must say the same thing for the labels and header it shares with this file: a test compares them).

Two spellings are protocol, not display text: the notification header `[project] <label> · <phase> · job <id>` /
`iş <id>` (`bridge.job_ref`) is parsed by the OpenClaw plugin to route a Telegram reply to its job; it accepts both.
"""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    # --- notification header pieces (the plugin parses `bridge.job_ref`) -----------------------------------------
    "bridge.job_ref": "job {id}",
    "bridge.label.awaiting_user": "⏸ Waiting for your reply",
    "bridge.label.done": "✅ Done",
    "bridge.label.blocked": "⛔ Blocked",
    "bridge.label.error": "❌ Error",
    "bridge.label.cancelled": "🛑 Cancelled",
    "bridge.label.paused": "⏸ Paused",

    # --- chat command language (Telegram / POST /telegram) --------------------------------------------------------
    "bridge.usage": (
        "Orkestra commands:\n"
        "/run <project> <request> · start a new job (default engine)\n"
        "/run claude:|cursor:|copilot:|pi: <project> <request> · pick the engine explicitly\n"
        "/status · jobs + live status (which job is where)\n"
        "/cancel · stop the latest job\n"
        "Any other message is sent as the answer to the job waiting for you (for example \"approve\").\n"
        "The Turkish names work too: /yap, /durum, /iptal."),
    "bridge.cmd.started": ("Started: [{project}] job {id}{engine}. I will write here when a question or an approval "
                           "is needed."),
    "bridge.cmd.start_failed": "Could not start: {error}",
    "bridge.cmd.jobs_header": "Jobs in this chat:",
    "bridge.cmd.no_jobs": "No job has been started from this chat.",
    "bridge.cmd.live_header": "Live runs:",
    "bridge.cmd.no_open_job": "No open job.",
    "bridge.cmd.cancelled": ("Cancelled: {id} (files stay as they are; to undo, run /orkestra:rollback on the "
                             "desktop)."),
    "bridge.cmd.still_running": "The job is still running; I will write when it finishes.",
    "bridge.cmd.nothing_waiting": "No job is waiting for you.",
    "bridge.cmd.resumed": "Got it, continuing: [{project}] job {id}.",

    # --- live view (`/status`, `orkestra_client.py runs`) ---------------------------------------------------------
    "bridge.live.none": "No open Orkestra runs.",
    "bridge.live.run": "• {project} · {run} · phase {phase}{tasks}",
    "bridge.live.tasks": " · task {done}/{total}",

    # --- errors that reach a person (HTTP 400 `error`, chat replies) ----------------------------------------------
    "bridge.err.invalid_project": "invalid project name: {name}",
    "bridge.err.project_not_found": "project not found: {name} (workspace: {workspace})",
    "bridge.err.project_not_allowed": "{name} is not in the bridge allowed_projects list",
    "bridge.err.unknown_engine": "unknown engine: {name} (choices: {choices})",
    "bridge.err.workspace_unavailable": "the bridge workspace is not available",
    "bridge.err.allowed_projects_type": "bridge allowed_projects must be a list of project names",
    "bridge.err.configured_project_unavailable": "configured project is unavailable: {name}",
    "bridge.err.no_waiting_job": "no job is waiting for a reply from this chat",
    "bridge.job.empty_request": "the request is empty",
    "bridge.job.already_open": "{project} already has an open job ({id}, {status}). Finish it first or send /cancel.",
    "bridge.job.still_running": "the job is still running; I will write to you when it finishes",
    "bridge.job.closed": "the job is closed ({status}); use /run to start a new one",

    # --- engines readiness (also the `reason` the web UI shows) ---------------------------------------------------
    "bridge.engine.claude_missing": "Claude Code CLI not found",
    "bridge.engine.cursor_cli_missing": ("Cursor CLI (agent) not found: https://cursor.com/cli (or set cursor_bin in "
                                         "bridge.secret.json)"),
    "bridge.engine.cursor_hooks_missing": ("Orkestra hooks are not installed in Cursor. Run once in a terminal: "
                                           "orkestra cursor install"),
    "bridge.engine.cursor_check_failed": "Could not check the Cursor setup: {error}",
    "bridge.engine.copilot_cli_missing": "Copilot CLI not found (check copilot_bin or PATH)",
    "bridge.engine.copilot_hooks_missing": ("Orkestra Copilot hooks are not installed; the UserPromptSubmit, "
                                            "PreToolUse and PostToolUse hooks are required"),

    # --- a turn of a job (stored in the job's last_message, sent as the notification body) -------------------------
    "bridge.turn.cursor_not_started": "Cursor turn not started: {why}",
    "bridge.turn.cursor_agent_shell": (
        "Cursor turn not started: the bridge seems to have been started from inside an agent shell (CURSOR_AGENT "
        "environment variable); the hook layer refused your message. Start the bridge from a normal terminal or "
        "with launchd."),
    "bridge.turn.copilot_not_started": "Copilot turn not started: {why}",
    "bridge.turn.binary_missing": "{engine} not found: {binary}",
    "bridge.turn.start_failed": "{engine} could not be started: {error}",
    "bridge.turn.exit_code": "{engine} exited with code {code}: {stderr}",
    "bridge.turn.paused_hint": "(The run came to a pause. Send any message to continue.)",
    "bridge.turn.auto_resume_failed": "Automatic continuation could not start ({error}). Send a message to continue.",
    "bridge.turn.auto_resume_cap": ("(Automatic continuation limit reached: the run was continued {count} times and "
                                    "stopped again. Send a message to continue.)"),

    # --- serve / init / launchd ----------------------------------------------------------------------------------
    "bridge.serve.no_token": "no token configured: run: python3 orkestra_bridge.py init",
    "bridge.serve.non_loopback": "refusing to bind a non-loopback host (expose it through an authenticated tunnel instead)",
    "bridge.serve.restarted": "(The bridge restarted; send a message to continue the job.)",
    "bridge.serve.banner": ("orkestra bridge on http://{host}:{port} · local UI: http://127.0.0.1:{port}/ "
                            "(log: {log})"),
    "bridge.init.summary": ("config:   {config}{perm}\n"
                            "token:    {token}\n"
                            "claude:   {claude}\n"
                            "Telegram: python3 {script} telegram-setup"),
    "bridge.launchd.wrote": "wrote {plist}\nload it with:  launchctl load -w {plist}",
    "bridge.launchd.not_macos": ("launchd exists on macOS only. Elsewhere, start the bridge from your service manager "
                                 "(a systemd user unit, Windows Task Scheduler, ...) with:\n  {command}"),

    # --- telegram-setup ------------------------------------------------------------------------------------------
    "bridge.tg.openclaw_warning": (
        "WARNING: OpenClaw has a Telegram configuration. A bot token can be polled by ONE program only.\n"
        "  → Do NOT give OpenClaw's bot to Orkestra here. Either create a SEPARATE bot for Orkestra in BotFather,\n"
        "  → or keep Telegram in OpenClaw and call Orkestra from OpenClaw: integrations/openclaw/CONTRACT.md"),
    "bridge.tg.continue_prompt": "Continue with a separate bot? [y/N] ",
    "bridge.tg.step_token": ("1) In Telegram open @BotFather → /newbot → choose a name and a username → copy the "
                             "token it gives you."),
    "bridge.tg.token_prompt": "Bot token (hidden): ",
    "bridge.tg.token_invalid": "Could not verify the token: {error}",
    "bridge.tg.webhook_in_use": (
        "This bot has a webhook ({host}): another program (for example OpenClaw) is using it.\n"
        "Give Orkestra a SEPARATE bot to avoid a conflict. (Use --force to override; not recommended.)"),
    "bridge.tg.webhook_unnamed": "configured",
    "bridge.tg.step_start": "2) In Telegram open @{username} and send /start. I will wait for 3 minutes...",
    "bridge.tg.found": "   Found: {name} (chat {chat})",
    "bridge.tg.no_message": "No message arrived. Make sure you sent /start to the bot, then run the command again.",
    "bridge.tg.connected": "Orkestra connected ✅",
    "bridge.tg.saved": ("3) Saved: {config} (only you can write: chat {chat}, user {user}).\n"
                        "4) Start the bridge:  python3 {script} serve\n"
                        "   (macOS, to start it at every login:  python3 {script} launchd)"),

    # --- doctor (the [OK]/[!!]/[--]/[i ] tags are not translated) --------------------------------------------------
    "bridge.doctor.config": "config",
    "bridge.doctor.token": "bridge token",
    "bridge.doctor.claude_cli": "claude CLI",
    "bridge.doctor.claude_not_found": "not found; run init again",
    "bridge.doctor.plugin_list_hung": "`{claude} plugin list` did not respond ({error})",
    "bridge.doctor.plugin_installed": "Orkestra plugin installed",
    "bridge.doctor.run_install": "run install.sh",
    "bridge.doctor.headless": "headless claude ({mode} mode)",
    "bridge.doctor.headless_hint": " → try setting permission_mode to 'acceptEdits' in bridge.secret.json",
    "bridge.doctor.cursor_engine": "Cursor engine",
    "bridge.doctor.cursor_absent": "no Cursor engine (optional: https://cursor.com/cli, then `orkestra cursor install`)",
    "bridge.doctor.default_engine": "default engine",
    "bridge.doctor.default_engine_is": "default engine: {engine}",
    "bridge.doctor.bridge_up": "bridge running",
    "bridge.doctor.bridge_down": "bridge not running",
    "bridge.doctor.tg_bot": "Telegram bot @{username}",
    "bridge.doctor.tg_webhook": "a webhook is set: another program is using this bot!",
    "bridge.doctor.tg_chat": "Telegram allowed chat",
    "bridge.doctor.tg_label": "Telegram",
    "bridge.doctor.tg_not_configured": "Telegram is not configured (python3 orkestra_bridge.py telegram-setup)",
    "bridge.doctor.openclaw_note": ("OpenClaw uses Telegram: give Orkestra a separate bot, or use the OpenClaw to "
                                    "bridge contract"),
    "bridge.doctor.summary_ok": "doctor: ready",
    "bridge.doctor.summary_bad": "doctor: gaps found (see above)",

    # --- --help of the CLIs --------------------------------------------------------------------------------------
    "bridge.cli.help": (
        "Orkestra bridge: lets n8n, Telegram or any webhook start and steer headless Orkestra runs.\n"
        "\n"
        "  python3 orkestra_bridge.py init              create ~/.orkestra/bridge.secret.json (token, port, claude path)\n"
        "  python3 orkestra_bridge.py telegram-setup    connect a Telegram bot in one step\n"
        "  python3 orkestra_bridge.py serve             HTTP API on 127.0.0.1 (+ Telegram long polling when configured)\n"
        "  python3 orkestra_bridge.py launchd           macOS: start automatically at login\n"
        "  python3 orkestra_bridge.py doctor [--live]   check config, claude, plugin, bridge, Telegram\n"
        "\n"
        "Local UI: http://127.0.0.1:{port}/ (sign in with the bridge token).\n"
        "Language of every message: ORKESTRA_LANG, then \"lang\" in ~/.orkestra/config.json, then the OS locale "
        "(English or Turkish)."),
    "bridge.client.help": (
        "Tiny client for the Orkestra bridge (OpenClaw tools, shell scripts, cron, Shortcuts over SSH ...)\n"
        "\n"
        "  orkestra_client.py start <project> <request...> [--chat-id ID] [--source openclaw] [--engine claude|cursor|copilot|pi]\n"
        "  orkestra_client.py reply <job-id> <text...>\n"
        "  orkestra_client.py reply --chat-id ID <text...>    the job of that chat waiting for an answer\n"
        "  orkestra_client.py status [<job-id>] [--chat-id ID]\n"
        "  orkestra_client.py wait <job-id> [--timeout 1800]   block until the job stops running, print its message\n"
        "  orkestra_client.py cancel --chat-id ID              stop the latest open job of that chat\n"
        "  orkestra_client.py runs [--json]                    live view: every open run, its phase, what it does now\n"
        "\n"
        "Reads the bearer token from ~/.orkestra/bridge.secret.json (same machine). Prints JSON (the message for "
        "`wait`).\n"
        "Exit codes: 0 ok · 1 bridge/HTTP error · 2 usage."),
    "bridge.client.not_configured": "bridge not configured: run: python3 {script} init",
    "bridge.client.unreachable": "bridge not reachable ({reason}); start it: python3 {script} serve",

    # --- n8n_import.py -------------------------------------------------------------------------------------------
    "bridge.n8n.need_key": ("Set N8N_API_KEY, or N8N_STATE to a JSON file with an apiKey field (there is no default "
                            "path into a sibling repo)."),
    "bridge.n8n.missing_file": "missing file: {path}",
    "bridge.n8n.bad_state": "N8N_STATE must be JSON with an apiKey field ({path}): {error}",
    "bridge.n8n.updated": "updated: {name} ({id})",
    "bridge.n8n.created": "created: {name} ({id})",
    "bridge.n8n.active": ("  active → POST {url}/webhook/orkestra-job  header X-Orkestra-Key: <n8n_webhook_key in "
                          "{config}>"),
    "bridge.n8n.activate_failed": "  could not activate ({code}); activate it in the n8n UI",
    "bridge.n8n.callback_url": "callback URL for the bridge config (callback_url): {url}/webhook/orkestra-callback",
    "bridge.n8n.unreachable": "n8n not reachable at {url}: {error}",
}

TR: Dict[str, str] = {
    # --- notification header pieces ------------------------------------------------------------------------------
    "bridge.job_ref": "iş {id}",
    "bridge.label.awaiting_user": "⏸ Yanıtını bekliyor",
    "bridge.label.done": "✅ Tamamlandı",
    "bridge.label.blocked": "⛔ Engellendi",
    "bridge.label.error": "❌ Hata",
    "bridge.label.cancelled": "🛑 İptal",
    "bridge.label.paused": "⏸ Duraklatıldı",

    # --- chat command language -----------------------------------------------------------------------------------
    "bridge.usage": (
        "Orkestra komutları:\n"
        "/yap <proje> <istek> · yeni iş başlat (varsayılan engine)\n"
        "/yap claude:|cursor:|copilot:|pi: <proje> <istek> · engine'i açıkça seç\n"
        "/durum · işler + canlı durum (hangi iş nerede)\n"
        "/iptal · son işi durdur\n"
        "Diğer her mesaj, seni bekleyen işe yanıt olarak gider (ör. \"onay\").\n"
        "İngilizce karşılıkları da çalışır: /run, /status, /cancel."),
    "bridge.cmd.started": "Başladım: [{project}] iş {id}{engine}. Soru ya da onay gerektiğinde buraya yazacağım.",
    "bridge.cmd.start_failed": "Başlatılamadı: {error}",
    "bridge.cmd.jobs_header": "Bu sohbetin işleri:",
    "bridge.cmd.no_jobs": "Bu sohbetten başlatılmış iş yok.",
    "bridge.cmd.live_header": "Canlı çalışmalar:",
    "bridge.cmd.no_open_job": "Açık iş yok.",
    "bridge.cmd.cancelled": "İptal edildi: {id} (dosyalar yerinde; geri almak için masaüstünde /orkestra:rollback).",
    "bridge.cmd.still_running": "İş hâlâ çalışıyor; bitince yazacağım.",
    "bridge.cmd.nothing_waiting": "Seni bekleyen bir iş yok.",
    "bridge.cmd.resumed": "Aldım, devam ediyorum: [{project}] iş {id}.",

    # --- live view -----------------------------------------------------------------------------------------------
    "bridge.live.none": "Açık orkestra çalışması yok.",
    "bridge.live.run": "• {project} · {run} · faz {phase}{tasks}",
    "bridge.live.tasks": " · görev {done}/{total}",

    # --- errors that reach a person ------------------------------------------------------------------------------
    "bridge.err.invalid_project": "geçersiz proje adı: {name}",
    "bridge.err.project_not_found": "proje bulunamadı: {name} (workspace: {workspace})",
    "bridge.err.project_not_allowed": "{name} bridge allowed_projects listesinde değil",
    "bridge.err.unknown_engine": "bilinmeyen motor: {name} (seçenekler: {choices})",
    "bridge.err.workspace_unavailable": "bridge workspace'i kullanılamıyor",
    "bridge.err.allowed_projects_type": "bridge allowed_projects proje adlarından oluşan bir liste olmalı",
    "bridge.err.configured_project_unavailable": "yapılandırılmış proje kullanılamıyor: {name}",
    "bridge.err.no_waiting_job": "bu sohbetten yanıt bekleyen bir iş yok",
    "bridge.job.empty_request": "istek boş",
    "bridge.job.already_open": "{project} için açık bir iş var ({id}, {status}). Önce onu bitir veya /iptal.",
    "bridge.job.still_running": "iş hâlâ çalışıyor; bitince sana yazacağım",
    "bridge.job.closed": "iş kapandı ({status}); yeni iş için /yap kullan",

    # --- engines readiness ---------------------------------------------------------------------------------------
    "bridge.engine.claude_missing": "Claude Code CLI bulunamadı",
    "bridge.engine.cursor_cli_missing": ("Cursor CLI (agent) bulunamadı: https://cursor.com/cli (ya da "
                                         "bridge.secret.json → cursor_bin)"),
    "bridge.engine.cursor_hooks_missing": ("Cursor'da orkestra hook'ları kurulu değil. Terminalde bir kez çalıştır: "
                                           "orkestra cursor install"),
    "bridge.engine.cursor_check_failed": "Cursor kurulumu denetlenemedi: {error}",
    "bridge.engine.copilot_cli_missing": "Copilot CLI bulunamadı (copilot_bin veya PATH'i denetle)",
    "bridge.engine.copilot_hooks_missing": ("Orkestra Copilot hook'ları kurulu değil; UserPromptSubmit, PreToolUse "
                                            "ve PostToolUse hook'ları gerekir"),

    # --- a turn of a job -----------------------------------------------------------------------------------------
    "bridge.turn.cursor_not_started": "Cursor turu başlatılmadı: {why}",
    "bridge.turn.cursor_agent_shell": (
        "Cursor turu başlatılmadı: bridge bir ajan kabuğunun içinden başlatılmış görünüyor (CURSOR_AGENT ortam "
        "değişkeni); hook katmanı mesajını kabul etmedi. Bridge'i normal bir terminalden ya da launchd ile başlat."),
    "bridge.turn.copilot_not_started": "Copilot turu başlatılmadı: {why}",
    "bridge.turn.binary_missing": "{engine} bulunamadı: {binary}",
    "bridge.turn.start_failed": "{engine} başlatılamadı: {error}",
    "bridge.turn.exit_code": "{engine} çıkış kodu {code}: {stderr}",
    "bridge.turn.paused_hint": "(Çalışma bir duraklamaya geldi. Devam için herhangi bir mesaj yaz.)",
    "bridge.turn.auto_resume_failed": "Otomatik devam başlatılamadı ({error}). Devam için bir mesaj yaz.",
    "bridge.turn.auto_resume_cap": ("(Otomatik devam sınırı aşıldı: {count} kez devam ettirildi, çalışma yine "
                                    "durdu. Devam için bir mesaj yaz.)"),

    # --- serve / init / launchd ----------------------------------------------------------------------------------
    "bridge.serve.no_token": "token yapılandırılmamış, önce çalıştır: python3 orkestra_bridge.py init",
    "bridge.serve.non_loopback": ("loopback olmayan bir adrese bağlanmayı reddediyorum (bunun yerine kimlik "
                                  "doğrulamalı bir tünel kullan)"),
    "bridge.serve.restarted": "(Bridge yeniden başladı; işi sürdürmek için bir mesaj yaz.)",
    "bridge.serve.banner": ("orkestra bridge http://{host}:{port} adresinde · yerel arayüz: "
                            "http://127.0.0.1:{port}/ (log: {log})"),
    "bridge.init.summary": ("yapılandırma: {config}{perm}\n"
                            "token:        {token}\n"
                            "claude:       {claude}\n"
                            "Telegram:     python3 {script} telegram-setup"),
    "bridge.launchd.wrote": "yazıldı: {plist}\nşununla yükle:  launchctl load -w {plist}",
    "bridge.launchd.not_macos": ("launchd yalnız macOS'ta vardır. Başka sistemlerde bridge'i servis yöneticinden "
                                 "(systemd kullanıcı birimi, Windows Görev Zamanlayıcı, ...) şu komutla başlat:\n"
                                 "  {command}"),

    # --- telegram-setup ------------------------------------------------------------------------------------------
    "bridge.tg.openclaw_warning": (
        "UYARI: OpenClaw'da bir Telegram yapılandırması var. Bir bot token'ını yalnız TEK program dinleyebilir.\n"
        "  → OpenClaw'ın kullandığı botu buraya VERME. Ya BotFather'dan orkestra için AYRI bir bot oluştur,\n"
        "  → ya da Telegram'ı OpenClaw'da bırak ve OpenClaw'dan orkestrayı çağır: integrations/openclaw/CONTRACT.md"),
    "bridge.tg.continue_prompt": "Ayrı bir bot ile devam edilsin mi? [e/H] ",
    "bridge.tg.step_token": ("1) Telegram'da @BotFather → /newbot → bir ad ve kullanıcı adı ver → verdiği "
                             "token'ı kopyala."),
    "bridge.tg.token_prompt": "Bot token (görünmez): ",
    "bridge.tg.token_invalid": "Token doğrulanamadı: {error}",
    "bridge.tg.webhook_in_use": (
        "Bu botun bir webhook'u var ({host}): başka bir program (ör. OpenClaw) onu kullanıyor.\n"
        "Çakışmamak için orkestraya AYRI bir bot ver. (Zorlamak için --force; önerilmez.)"),
    "bridge.tg.webhook_unnamed": "ayarlı",
    "bridge.tg.step_start": "2) Telegram'da @{username} botunu aç ve /start yaz. 3 dakika bekliyorum...",
    "bridge.tg.found": "   Bulundu: {name} (chat {chat})",
    "bridge.tg.no_message": "Mesaj gelmedi. Bota /start yazdığından emin ol ve komutu tekrar çalıştır.",
    "bridge.tg.connected": "Orkestra bağlandı ✅",
    "bridge.tg.saved": ("3) Kaydedildi: {config} (yalnız sen yazabilirsin: chat {chat}, kullanıcı {user}).\n"
                        "4) Bridge'i başlat:  python3 {script} serve\n"
                        "   (macOS'ta her açılışta otomatik için:  python3 {script} launchd)"),

    # --- doctor --------------------------------------------------------------------------------------------------
    "bridge.doctor.config": "config",
    "bridge.doctor.token": "bridge token",
    "bridge.doctor.claude_cli": "claude CLI",
    "bridge.doctor.claude_not_found": "bulunamadı; init'i tekrar çalıştır",
    "bridge.doctor.plugin_list_hung": "`{claude} plugin list` cevap vermedi ({error})",
    "bridge.doctor.plugin_installed": "orkestra plugin kurulu",
    "bridge.doctor.run_install": "install.sh çalıştır",
    "bridge.doctor.headless": "headless claude ({mode} modu)",
    "bridge.doctor.headless_hint": " → bridge.secret.json'da permission_mode'u 'acceptEdits' yapmayı dene",
    "bridge.doctor.cursor_engine": "cursor motoru",
    "bridge.doctor.cursor_absent": "cursor motoru yok (isteğe bağlı: https://cursor.com/cli, sonra `orkestra cursor install`)",
    "bridge.doctor.default_engine": "varsayılan motor",
    "bridge.doctor.default_engine_is": "varsayılan motor: {engine}",
    "bridge.doctor.bridge_up": "bridge çalışıyor",
    "bridge.doctor.bridge_down": "bridge çalışmıyor",
    "bridge.doctor.tg_bot": "telegram bot @{username}",
    "bridge.doctor.tg_webhook": "webhook ayarlı: başka program bu botu kullanıyor!",
    "bridge.doctor.tg_chat": "telegram izinli chat",
    "bridge.doctor.tg_label": "telegram",
    "bridge.doctor.tg_not_configured": "telegram yapılandırılmadı (python3 orkestra_bridge.py telegram-setup)",
    "bridge.doctor.openclaw_note": ("OpenClaw Telegram kullanıyor: orkestra'ya ayrı bot ver ya da OpenClaw → bridge "
                                    "sözleşmesini kullan"),
    "bridge.doctor.summary_ok": "doctor: hazır",
    "bridge.doctor.summary_bad": "doctor: eksikler var (yukarı bak)",

    # --- --help of the CLIs --------------------------------------------------------------------------------------
    "bridge.cli.help": (
        "Orkestra bridge: n8n, Telegram ya da herhangi bir webhook'un başsız (headless) Orkestra çalışmalarını "
        "başlatıp yönlendirmesini sağlar.\n"
        "\n"
        "  python3 orkestra_bridge.py init              ~/.orkestra/bridge.secret.json oluşturur (token, port, claude yolu)\n"
        "  python3 orkestra_bridge.py telegram-setup    bir Telegram botunu tek adımda bağlar\n"
        "  python3 orkestra_bridge.py serve             127.0.0.1'de HTTP API (+ ayarlıysa Telegram long polling)\n"
        "  python3 orkestra_bridge.py launchd           macOS: oturum açılınca otomatik başlat\n"
        "  python3 orkestra_bridge.py doctor [--live]   config, claude, plugin, bridge ve Telegram denetimi\n"
        "\n"
        "Yerel arayüz: http://127.0.0.1:{port}/ (bridge token'ı ile giriş yap).\n"
        "Tüm mesajların dili: ORKESTRA_LANG, sonra ~/.orkestra/config.json içindeki \"lang\", sonra işletim "
        "sistemi dili (İngilizce ya da Türkçe)."),
    "bridge.client.help": (
        "Orkestra bridge için küçük istemci (OpenClaw araçları, kabuk betikleri, cron, SSH üzerinden Kısayollar ...)\n"
        "\n"
        "  orkestra_client.py start <proje> <istek...> [--chat-id ID] [--source openclaw] [--engine claude|cursor|copilot|pi]\n"
        "  orkestra_client.py reply <iş-id> <metin...>\n"
        "  orkestra_client.py reply --chat-id ID <metin...>    o sohbette yanıt bekleyen iş\n"
        "  orkestra_client.py status [<iş-id>] [--chat-id ID]\n"
        "  orkestra_client.py wait <iş-id> [--timeout 1800]    iş çalışmayı bırakana kadar bekler, mesajını yazar\n"
        "  orkestra_client.py cancel --chat-id ID              o sohbetin son açık işini durdurur\n"
        "  orkestra_client.py runs [--json]                    canlı görünüm: her açık çalışma, fazı, şu an ne yaptığı\n"
        "\n"
        "Bearer token'ı ~/.orkestra/bridge.secret.json dosyasından okur (aynı makine). JSON yazar (`wait` için "
        "mesajı).\n"
        "Çıkış kodları: 0 tamam · 1 bridge/HTTP hatası · 2 kullanım."),
    "bridge.client.not_configured": "bridge yapılandırılmamış, önce çalıştır: python3 {script} init",
    "bridge.client.unreachable": "bridge'e ulaşılamıyor ({reason}); başlat: python3 {script} serve",

    # --- n8n_import.py -------------------------------------------------------------------------------------------
    "bridge.n8n.need_key": ("N8N_API_KEY ayarla ya da apiKey alanı olan bir JSON dosyasını N8N_STATE ile göster "
                            "(komşu bir depoya varsayılan yol yoktur)."),
    "bridge.n8n.missing_file": "dosya yok: {path}",
    "bridge.n8n.bad_state": "N8N_STATE, apiKey alanı olan bir JSON olmalı ({path}): {error}",
    "bridge.n8n.updated": "güncellendi: {name} ({id})",
    "bridge.n8n.created": "oluşturuldu: {name} ({id})",
    "bridge.n8n.active": ("  etkin → POST {url}/webhook/orkestra-job  header X-Orkestra-Key: <{config} içindeki "
                          "n8n_webhook_key>"),
    "bridge.n8n.activate_failed": "  etkinleştirilemedi ({code}); n8n arayüzünden etkinleştir",
    "bridge.n8n.callback_url": "bridge ayarı için callback adresi (callback_url): {url}/webhook/orkestra-callback",
    "bridge.n8n.unreachable": "n8n'e {url} adresinden ulaşılamıyor: {error}",
}
