"""help: `--help` texts of the CLI and the messages of `orkestra lang` (English + Turkish). Keys are `help.<name>`; both
dicts must hold the same keys and {fields}.

Layout of the keys (cliparse.build_parser reads the first group, cli.cmd_lang the rest):
    help.description / help.epilog / help.usage / help.commands / help.options / help.positionals
    help.opt.<name>    the global options (-h, -p, --run, --lang, --version)
    help.cmd.<name>    ONE line per subcommand, shown in the command list and at the top of `orkestra <name> --help`
    help.lang_*        output of `orkestra lang` and the `setup --lang` confirmation
Rules: no `%` in a help text (argparse expands it), no em dash, and every line of the epilog that starts with
`  orkestra ` must be a command that parses (test_cli_ux checks it).
"""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    "help.description": "Orkestra: a deterministic SDLC engine for Claude Code, Cursor and Copilot.\n"
                        "It takes software work through discovery, a reviewed plan, TDD implementation,\n"
                        "no-regression gates and adversarial review, and keeps the evidence.",
    "help.epilog": (
        "examples:\n"
        "  # check that this machine is ready (python, git, hooks, engines, language)\n"
        "  orkestra doctor\n"
        "\n"
        "  # start a run from your terminal\n"
        "  orkestra run \"add a dark mode toggle to settings\"\n"
        "\n"
        "  # live view of every run: phase, what it does now, who it waits for\n"
        "  orkestra watch\n"
        "\n"
        "  # another project, in Turkish, for this command only\n"
        "  orkestra -p myapp --lang tr status\n"
        "\n"
        "  # use Turkish from now on (en | tr | auto)\n"
        "  orkestra lang tr\n"
        "\n"
        "exit codes:\n"
        "  0  ok\n"
        "  1  a check failed or the next step is blocked (doctor, gate, phase check)\n"
        "  2  usage error, or the request was refused (bad arguments, no open run)\n"
        "\n"
        "details of one command: orkestra <command> --help"
    ),
    "help.usage": "usage:",
    "help.commands": "commands",
    "help.options": "options",
    "help.positionals": "arguments",

    "help.opt.help": "show this help and exit",
    "help.opt.project": "project name (under the workspace) or path",
    "help.opt.run": "run id (default: the project's open run)",
    "help.opt.lang": "language of the output: en, tr or auto (for this command; setup also saves it)",
    "help.opt.version": "print the version and exit",

    # options of single commands (help.opt.<what>), read by cliparse.build_parser
    "help.opt.start_size": "run size; auto (default) infers XS/S/M/L from the request; prefer XS/S for small safe work",
    "help.opt.start_engine": "which agent drives the run (default: the one asking, else claude)",
    "help.opt.approval_mode": "default autopilot; use manual when the user explicitly wants to approve the plan",
    "help.opt.reason_opt": "the reason, instead of the words above (give one of the two, not both)",
    "help.opt.tdd_why": "reason when deviating (different command, test changed after red)",
    "help.opt.gate_resume": "gate verify: skip gates already run for the current change stamp (after an interrupted verify)",
    "help.opt.gate_all": "gate verify: run every gate in full (no baseline skip, no carry-forward, no resume)",
    "help.opt.task_force": "reason: reopen a PASS, the 3rd round after the user's ruling (manual mode only), or start despite "
                           "shared files (goes to the ledger)",
    "help.opt.task_files": "task add: comma-separated files the task touches (the overlap check of `task start` reads them)",
    "help.opt.wait_max": "longest wait in seconds (default 540, stay below the Bash tool timeout)",
    "help.opt.brief_review": "the review packet of a task for the reviewer: acceptance, red/green tails, report, diff of its files",
    "help.opt.wd_dry_run": "classify and print only: no notification, no resume",
    "help.opt.wd_quiet": "print nothing when every run is healthy (for launchd/cron)",
    "help.opt.wd_launchd": "write a LaunchAgent that runs the scan every minute",
    "help.opt.wd_loop": "run the fast watcher in the foreground (singleton; stall predicates every few seconds, zero tokens)",
    "help.opt.wd_daemon": "start the fast watcher detached now (no-op when one runs; POSIX only)",
    "help.opt.wd_status": "show whether the fast watcher runs and which runs it flags (add --json for a machine view)",
    "help.opt.dash_replace": "stop an older dashboard that holds the port (only a process that is an orkestra dashboard) and start this one",
    "help.opt.rollback_all": "also revert differences not touched by orkestra tools",
    "help.opt.playbook_name": "default: the phase the open run is in",
    "help.opt.watch_all": "also show finished runs",
    "help.opt.dash_open": "open the browser",
    "help.opt.run_request": "the request, in your own words",
    "help.opt.run_engine": "default: claude (config: engine.default)",
    "help.opt.run_print": "only print the command that would be started",
    "help.opt.run_model": "Copilot only: model for this run, any id the Copilot CLI accepts or auto (default: engine.copilot.model, gpt-6-luna)",
    "help.opt.run_effort": "Copilot only: reasoning effort none|minimal|low|medium|high|xhigh|max (default: max)",
    "help.opt.run_context": "Copilot only: default|long_context (default: long_context)",
    "help.opt.run_allow_all": "Copilot only: allow every tool, path and URL without asking (default: on)",
    "help.opt.run_no_allow_all": "Copilot only: still passes --allow-all-tools (every tool runs unasked), but drops --allow-all-paths and --allow-all-urls",
    "help.opt.vscode_apply": "write the VS Code settings (needs --workspace PATH or --user); without it only the plan is shown",
    "help.opt.vscode_remove": "take back what --apply wrote (our keys only, and only while they still hold the value we wrote)",
    "help.opt.vscode_workspace": "project folder whose .vscode/settings.json is changed",
    "help.opt.vscode_user": "change the user-level VS Code settings.json instead of a workspace",
    "help.opt.vscode_permissions": "also set chat.permissions.default (autoApprove = Allow all, autopilot); off unless given. chat.defaultModel and chat.defaultConfiguration are always written with --apply",
    "help.opt.vscode_model": "also write .github/agents/orkestra.agent.md pinned to this model id (workspace only); also sets chat.defaultModel",
    "help.opt.vscode_effort": "reasoning-effort for that agent file: low|medium|high|xhigh|max (needs --model; VS Code has no effort setting)",
    "help.opt.copy": "copy files instead of linking them",
    "help.opt.dry_run": "show what would change, write nothing",
    "help.opt.fail_closed": "cursor install: block the tool call when the guard hook crashes or times out (default: Cursor lets it through)",
    "help.opt.setup_yes": "install every detected engine without asking",
    "help.opt.doctor_repair": "restore damaged run state from state.json.prev, or move the run aside (never deletes)",
    "help.opt.lang_value": "show (default): print the language in use and where it comes from; en | tr | auto: save it",

    # usage errors of the parser itself (argparse words them in English): same sentences, so English stays as it was
    "help.error_word": "error",
    "help.group_start": "start here:",
    "help.group_advanced": "advanced:",
    "help.err.unknown_command": "Unknown command {value}. See: orkestra --help",
    "help.err.unknown_command_hint": "Unknown command {value}. Did you mean {suggestion}?",
    "help.opt.start_text": "what you want built, in your words (the run title is derived from it)",
    "help.opt.start_title": "short run title (default: the start of the request)",
    "help.err.invalid_choice": "argument {arg}: invalid choice: {value} (choose from {choices})",
    "help.err.required": "the following arguments are required: {names}",
    "help.err.unrecognized": "unrecognized arguments: {rest}",
    "help.err.expected_one": "argument {arg}: expected one argument",
    "help.err.invalid_value": "argument {arg}: invalid {kind} value: {value}",
    "help.err.not_allowed": "argument {arg}: not allowed with argument {other}",
    "help.err.argument": "argument {arg}: {msg}",
    "help.err.bad_lang": "unsupported language {value} (use en, tr or auto)",
    "help.err.bad_lang_value": "unsupported value {value} (use show, en, tr or auto)",

    "help.cmd.context": "compact digest: project memory + active run + next action",
    "help.cmd.init": "create the .orkestra/ skeleton in a project and detect its gates",
    "help.cmd.start": "start a run",
    "help.cmd.resume": "continue the open run: where it stands and the next step",
    "help.cmd.status": "phase, tasks, approval and next step of a run",
    "help.cmd.phase": "show, check, advance (next) or go back in the phase machine",
    "help.cmd.await": "mark the run as waiting for the user's answer",
    "help.cmd.block": "mark the run as blocked, with a reason",
    "help.cmd.unblock": "clear a block and make the run active again",
    "help.cmd.pause": "pause the run: nothing continues until it is resumed",
    "help.cmd.abort": "abort the run (project files stay untouched)",
    "help.cmd.gate": "detect, confirm, baseline and run the project's verification gates",
    "help.cmd.tdd": "record a red or green test run for a task",
    "help.cmd.evidence": "run a command and record its output as evidence",
    "help.cmd.integrity": "check for removed tests, skip/only markers and new suppressions",
    "help.cmd.report": "the machine evidence block of the delivery report",
    "help.cmd.task": "list tasks, start them, mark them done, blocked, deferred or reopened",
    "help.cmd.next": "the single next step, derived from state + evidence + reviews (use it to resume)",
    "help.cmd.wait": "headless: wait inside this process (no tokens) until subagents finish, then print the next step",
    "help.cmd.tempo": "where the time went: phases, tasks, review rounds, dead wait",
    "help.cmd.agents": "background subagents registered for the run (clear: drop records of agents that are gone)",
    "help.cmd.watchdog": "one scan of every open run: stalled? waiting for you? (notifies; resumes only if allowed)",
    "help.cmd.brief": "the brief for one task: its plan section, spec lines and constraints (--review: the reviewer's packet)",
    "help.cmd.plan": "check plan.md against the spec",
    "help.cmd.trace": "check that every requirement is traced to evidence in verification.md",
    "help.cmd.deps": "check new dependencies against what the plan approved",
    "help.cmd.review": "check that every review finding is triaged (reviews/triage.md)",
    "help.cmd.changes": "files this run changed (attributed to orkestra tools)",
    "help.cmd.diff": "git diff of the files this run changed",
    "help.cmd.rollback": "revert the files this run changed (dry run unless --yes)",
    "help.cmd.clean": "remove regenerable build/test dumps in the project (auto after a run finishes)",
    "help.opt.clean_gradle": "also remove ~/.gradle/caches/*/transforms* (machine-wide; regenerable)",
    "help.cmd.memory": "show or extend the project memory: features, decisions, learnings",
    "help.cmd.playbook": "print the playbook of a phase (default: the phase the open run is in)",
    "help.cmd.template": "print a document template (spec, plan, report, ...)",
    "help.cmd.watch": "live terminal view of every run: phase, what it is doing now, who it waits for",
    "help.cmd.dashboard": "read-only live web page on 127.0.0.1",
    "help.cmd.run": "start a run from the terminal with the engine of your choice (opens its interactive UI)",
    "help.cmd.cursor": "Cursor side: build | install | uninstall | status (skills, subagents and hooks in ~/.cursor)",
    "help.cmd.copilot": "Copilot side: install | uninstall | status | vscode (hooks in ~/.copilot/plugins/orkestra; model, effort, context)",
    "help.cmd.pi": "Pi side: install | uninstall | status (skills and the tool_call extension under ~/.pi/agent)",
    "help.cmd.mcp": "MCP servers: install | status for Claude, Cursor and Copilot (default: context7 and playwright)",
    "help.opt.mcp_engine": "which engine: claude, cursor, copilot or all (default: all)",
    "help.opt.mcp_only": "only this server (repeatable). Default: context7 and playwright",
    "help.cmd.setup": "detect Claude/Cursor/Copilot and install Orkestra for each (interactive, or --yes)",
    "help.cmd.doctor": "check this machine: python, git, hooks, engines, language",
    "help.cmd.version": "print the Orkestra version",
    "help.cmd.lang": "show or set the language of human-facing output: show | en | tr | auto",

    "help.lang_hint": "change it: orkestra lang en | tr | auto (or --lang en|tr for a single command)",
    "help.lang_saved": "language saved: {lang} ({path})",
    "help.lang_saved_auto": "language saved: auto, follows the OS locale and falls back to English ({path})",
    "help.lang_override": "note: {origin} sets the language to {lang} here, so the saved value does not apply "
                          "in this environment",
    "help.lang_backup": "the old config file was not valid JSON: a copy is kept at {path}",
    "help.lang_unreadable": "cannot read {path}: {error}",
    "help.lang_unwritable": "cannot write {path}: {error}",
}

TR: Dict[str, str] = {
    "help.description": "Orkestra: Claude Code, Cursor ve Copilot için deterministik bir SDLC motoru.\n"
                        "Yazılım işini keşif, gözden geçirilmiş plan, TDD ile uygulama, regresyon\n"
                        "kapıları ve karşıt inceleme boyunca taşır ve kanıtı saklar.",
    "help.epilog": (
        "örnekler:\n"
        "  # bu makinenin hazır olup olmadığını kontrol et (python, git, hook'lar, motorlar, dil)\n"
        "  orkestra doctor\n"
        "\n"
        "  # terminalden bir run başlat\n"
        "  orkestra run \"ayarlara karanlık mod anahtarı ekle\"\n"
        "\n"
        "  # tüm run'ların canlı görünümü: aşama, şu an ne yapıyor, kimi bekliyor\n"
        "  orkestra watch\n"
        "\n"
        "  # başka bir proje, yalnız bu komut için Türkçe\n"
        "  orkestra -p myapp --lang tr status\n"
        "\n"
        "  # bundan sonra Türkçe kullan (en | tr | auto)\n"
        "  orkestra lang tr\n"
        "\n"
        "çıkış kodları:\n"
        "  0  tamam\n"
        "  1  bir kontrol başarısız ya da sıradaki adım engelli (doctor, gate, phase check)\n"
        "  2  kullanım hatası veya istek reddedildi (hatalı argüman, açık run yok)\n"
        "\n"
        "bir komutun ayrıntısı: orkestra <komut> --help"
    ),
    "help.usage": "kullanım:",
    "help.commands": "komutlar",
    "help.options": "seçenekler",
    "help.positionals": "argümanlar",

    "help.opt.help": "bu yardımı göster ve çık",
    "help.opt.project": "proje adı (çalışma alanı altında) veya yolu",
    "help.opt.run": "run kimliği (varsayılan: projenin açık run'ı)",
    "help.opt.lang": "çıktının dili: en, tr veya auto (bu komut için; setup ayrıca kaydeder)",
    "help.opt.version": "sürümü yazdır ve çık",

    "help.opt.start_size": "run boyutu; auto (varsayılan) boyutu istekten XS/S/M/L olarak çıkarır; küçük ve güvenli işte XS/S "
                           "tercih edin",
    "help.opt.start_engine": "run'ı hangi ajanın yürüteceği (varsayılan: isteği yapan, yoksa claude)",
    "help.opt.approval_mode": "varsayılan autopilot; kullanıcı planı açıkça kendisi onaylamak istiyorsa manual kullanın",
    "help.opt.reason_opt": "yukarıdaki kelimeler yerine gerekçe (ikisinden birini verin, ikisini birden değil)",
    "help.opt.tdd_why": "sapma gerekçesi (farklı komut, kırmızıdan sonra değişen test)",
    "help.opt.gate_resume": "gate verify: mevcut değişiklik damgası için zaten çalışmış gate'leri atla (yarıda kalan verify sonrası)",
    "help.opt.gate_all": "gate verify: her gate'i tam çalıştır (baseline atlaması, taşıma ve devam yok)",
    "help.opt.task_force": "gerekçe: PASS almış bir görevi, kullanıcı kararından sonra 3. turu (yalnız manual mod) yeniden "
                           "açmak veya ortak dosyalara rağmen başlatmak için (günlüğe yazılır)",
    "help.opt.task_files": "task add: görevin dokunduğu dosyalar, virgülle ayrılmış (`task start` çakışma denetimi bunları okur)",
    "help.opt.wait_max": "en uzun bekleme (saniye; varsayılan 540, Bash aracının zaman aşımının altında kalın)",
    "help.opt.brief_review": "reviewer için görev inceleme paketi: kabul ölçütleri, red/green kuyrukları, rapor, dosyalarının diff'i",
    "help.opt.wd_dry_run": "yalnız sınıflandır ve yazdır: bildirim yok, devam ettirme yok",
    "help.opt.wd_quiet": "her run sağlıklıysa hiçbir şey yazdırma (launchd/cron için)",
    "help.opt.wd_launchd": "taramayı dakikada bir çalıştıran bir LaunchAgent yaz",
    "help.opt.wd_loop": "hızlı bekçiyi ön planda çalıştır (tekil; birkaç saniyede bir takılma koşulları, sıfır token)",
    "help.opt.wd_daemon": "hızlı bekçiyi şimdi arka planda başlat (zaten çalışıyorsa bir şey yapmaz; yalnız POSIX)",
    "help.opt.wd_status": "hızlı bekçinin çalışıp çalışmadığını ve hangi run'ları işaretlediğini göster (makine görünümü için --json ekle)",
    "help.opt.dash_replace": "portu tutan eski bir dashboard'u durdur (yalnızca orkestra dashboard olan süreç) ve bunu başlat",
    "help.opt.rollback_all": "orkestra araçlarının dokunmadığı farkları da geri al",
    "help.opt.playbook_name": "varsayılan: açık run'ın içinde bulunduğu aşama",
    "help.opt.watch_all": "bitmiş run'ları da göster",
    "help.opt.dash_open": "tarayıcıyı aç",
    "help.opt.run_request": "isteğiniz, kendi sözlerinizle",
    "help.opt.run_engine": "varsayılan: claude (yapılandırma: engine.default)",
    "help.opt.run_print": "başlatılacak komutu yalnız yazdır",
    "help.opt.run_model": "yalnız Copilot: bu run için model, Copilot CLI'nin kabul ettiği herhangi bir kimlik ya da auto (varsayılan: engine.copilot.model, gpt-6-luna)",
    "help.opt.run_effort": "yalnız Copilot: akıl yürütme eforu none|minimal|low|medium|high|xhigh|max (varsayılan: max)",
    "help.opt.run_context": "yalnız Copilot: default|long_context (varsayılan: long_context)",
    "help.opt.run_allow_all": "yalnız Copilot: her araca, yola ve URL'ye sormadan izin ver (varsayılan: açık)",
    "help.opt.run_no_allow_all": "yalnız Copilot: --allow-all-tools yine geçer (her araç sormadan çalışır), ama --allow-all-paths ve --allow-all-urls düşer",
    "help.opt.vscode_apply": "VS Code ayarlarını yaz (--workspace PATH ya da --user gerekir); yoksa yalnız plan gösterilir",
    "help.opt.vscode_remove": "--apply ile yazılanları geri al (yalnız bizim anahtarlar, ve yalnız hâlâ yazdığımız değeri taşıyorsa)",
    "help.opt.vscode_workspace": "ayarları .vscode/settings.json içinde değiştirilecek proje klasörü",
    "help.opt.vscode_user": "çalışma alanı yerine kullanıcı düzeyindeki VS Code settings.json dosyasını değiştir",
    "help.opt.vscode_permissions": "ayrıca chat.permissions.default yaz (autoApprove = Hepsine izin, autopilot); verilmezse kapalı. chat.defaultModel ve chat.defaultConfiguration --apply ile her zaman yazılır",
    "help.opt.vscode_model": "ayrıca bu model kimliğine sabitlenmiş .github/agents/orkestra.agent.md yaz (yalnız çalışma alanı); chat.defaultModel de buna ayarlanır",
    "help.opt.vscode_effort": "o ajan dosyası için reasoning-effort: low|medium|high|xhigh|max (--model gerekir; VS Code'da efor ayarı yok)",
    "help.opt.copy": "dosyaları bağlamak yerine kopyala",
    "help.opt.dry_run": "neyin değişeceğini göster, hiçbir şey yazma",
    "help.opt.fail_closed": "cursor install: koruma kancası çökerse veya zaman aşımına uğrarsa araç çağrısını engelle (varsayılan: Cursor geçirir)",
    "help.opt.setup_yes": "bulunan her motoru sormadan kur",
    "help.opt.doctor_repair": "bozuk run durumunu state.json.prev ile geri yükle ya da run'ı kenara taşı (asla silmez)",
    "help.opt.lang_value": "show (varsayılan): kullanılan dili ve nereden geldiğini yazdır; en | tr | auto: kaydet",

    "help.error_word": "hata",
    "help.group_start": "buradan başla:",
    "help.group_advanced": "gelişmiş:",
    "help.err.unknown_command": "Bilinmeyen komut {value}. Bak: orkestra --help",
    "help.err.unknown_command_hint": "Bilinmeyen komut {value}. Şunu mu demek istedin: {suggestion}?",
    "help.opt.start_text": "ne istediğin, kendi cümlenle (run başlığı buradan türetilir)",
    "help.opt.start_title": "kısa run başlığı (varsayılan: isteğin başı)",
    "help.err.invalid_choice": "{arg} argümanı için geçersiz seçenek: {value} (şunlardan biri olmalı: {choices})",
    "help.err.required": "şu argümanlar gerekli: {names}",
    "help.err.unrecognized": "tanınmayan argümanlar: {rest}",
    "help.err.expected_one": "{arg} argümanı bir değer bekliyor",
    "help.err.invalid_value": "{arg} argümanı için geçersiz {kind} değeri: {value}",
    "help.err.not_allowed": "{arg} argümanı {other} ile birlikte kullanılamaz",
    "help.err.argument": "{arg} argümanı: {msg}",
    "help.err.bad_lang": "desteklenmeyen dil {value} (en, tr veya auto kullanın)",
    "help.err.bad_lang_value": "desteklenmeyen değer {value} (show, en, tr veya auto kullanın)",

    "help.cmd.context": "kısa özet: proje belleği + etkin run + sıradaki adım",
    "help.cmd.init": "projede .orkestra/ iskeletini oluştur ve kapıları (gate) algıla",
    "help.cmd.start": "bir run başlat",
    "help.cmd.resume": "açık run'a devam et: nerede olduğu ve sıradaki adım",
    "help.cmd.status": "bir run'ın aşaması, görevleri, onayı ve sıradaki adımı",
    "help.cmd.phase": "aşama makinesinde göster, denetle, ilerle (next) veya geri dön",
    "help.cmd.await": "run'ı kullanıcının cevabını bekliyor olarak işaretle",
    "help.cmd.block": "run'ı bir gerekçeyle engellenmiş olarak işaretle",
    "help.cmd.unblock": "engeli kaldır ve run'ı yeniden etkin yap",
    "help.cmd.pause": "run'ı duraklat: devam ettirilene kadar hiçbir şey sürmez",
    "help.cmd.abort": "run'ı iptal et (proje dosyalarına dokunulmaz)",
    "help.cmd.gate": "projenin doğrulama kapılarını algıla, onayla, referans al ve çalıştır",
    "help.cmd.tdd": "bir görev için kırmızı veya yeşil test koşusunu kaydet",
    "help.cmd.evidence": "bir komutu çalıştır ve çıktısını kanıt olarak kaydet",
    "help.cmd.integrity": "silinmiş test, skip/only işareti ve yeni bastırma var mı denetle",
    "help.cmd.report": "teslim raporunun makine kanıt bloğu",
    "help.cmd.task": "görevleri listele, başlat; bitti, engelli, ertelendi veya yeniden açıldı olarak işaretle",
    "help.cmd.next": "tek bir sıradaki adım; durum + kanıt + incelemelerden türetilir (devam için kullan)",
    "help.cmd.wait": "headless: bu süreç içinde bekle (token harcamaz), alt-ajanlar bitince sıradaki adımı yaz",
    "help.cmd.tempo": "zaman nereye gitti: aşamalar, görevler, inceleme turları, ölü bekleme",
    "help.cmd.agents": "run'a kayıtlı arka plan alt-ajanları (clear: artık olmayanların kayıtlarını sil)",
    "help.cmd.watchdog": "açık her run için tek tarama: takıldı mı? seni mi bekliyor? (bildirir; yalnız izin varsa devam ettirir)",
    "help.cmd.brief": "tek bir görevin özeti: plandaki bölümü, spec satırları ve kısıtlar (--review: reviewer paketi)",
    "help.cmd.plan": "plan.md dosyasını spec'e göre denetle",
    "help.cmd.trace": "her gereksinimin verification.md içinde kanıta bağlandığını denetle",
    "help.cmd.deps": "yeni bağımlılıkları planın onayladıklarıyla karşılaştır",
    "help.cmd.review": "her inceleme bulgusunun triyaj edildiğini denetle (reviews/triage.md)",
    "help.cmd.changes": "bu run'ın değiştirdiği dosyalar (orkestra araçlarına atfedilen)",
    "help.cmd.diff": "bu run'ın değiştirdiği dosyaların git farkı",
    "help.cmd.rollback": "bu run'ın değiştirdiği dosyaları geri al (--yes yoksa kuru çalıştırma)",
    "help.cmd.clean": "projedeki yeniden üretilebilir derleme/test artıklarını sil (run bitince otomatik)",
    "help.opt.clean_gradle": "~/.gradle/caches/*/transforms* ağaçlarını da sil (makine geneli; yeniden üretilir)",
    "help.cmd.memory": "proje belleğini göster veya genişlet: özellikler, kararlar, öğrenilenler",
    "help.cmd.playbook": "bir aşamanın playbook'unu yazdır (varsayılan: açık run'ın aşaması)",
    "help.cmd.template": "bir belge şablonunu yazdır (spec, plan, report, ...)",
    "help.cmd.watch": "tüm run'ların canlı terminal görünümü: aşama, şu an ne yapıyor, kimi bekliyor",
    "help.cmd.dashboard": "127.0.0.1 üzerinde salt okunur canlı web sayfası",
    "help.cmd.run": "terminalden seçtiğin motorla bir run başlat (motorun etkileşimli arayüzünü açar)",
    "help.cmd.cursor": "Cursor tarafı: build | install | uninstall | status (~/.cursor altında skill, alt-ajan ve hook'lar)",
    "help.cmd.copilot": "Copilot tarafı: install | uninstall | status | vscode (~/.copilot/plugins/orkestra altında hook'lar; model, efor, context)",
    "help.cmd.pi": "Pi tarafı: install | uninstall | status (~/.pi/agent altında skill ve tool_call eklentisi)",
    "help.cmd.mcp": "MCP sunucuları: Claude, Cursor ve Copilot için install | status (varsayılan: context7 ve playwright)",
    "help.opt.mcp_engine": "hangi motor: claude, cursor, copilot ya da all (varsayılan: all)",
    "help.opt.mcp_only": "yalnız bu sunucu (tekrarlanabilir). Varsayılan: context7 ve playwright",
    "help.cmd.setup": "Claude/Cursor/Copilot algıla ve her biri için Orkestra'yı kur (etkileşimli ya da --yes)",
    "help.cmd.doctor": "bu makineyi denetle: python, git, hook'lar, motorlar, dil",
    "help.cmd.version": "Orkestra sürümünü yazdır",
    "help.cmd.lang": "insanın okuduğu çıktının dilini göster veya ayarla: show | en | tr | auto",

    "help.lang_hint": "değiştirmek için: orkestra lang en | tr | auto (tek komut için --lang en|tr)",
    "help.lang_saved": "dil kaydedildi: {lang} ({path})",
    "help.lang_saved_auto": "dil kaydedildi: auto, işletim sistemi dilini izler; bilinmiyorsa İngilizce kullanılır ({path})",
    "help.lang_override": "not: {origin} dili burada {lang} olarak belirliyor, bu yüzden kaydedilen değer "
                          "bu ortamda geçerli olmaz",
    "help.lang_backup": "eski yapılandırma dosyası geçerli JSON değildi: kopyası {path} yolunda saklandı",
    "help.lang_unreadable": "{path} okunamıyor: {error}",
    "help.lang_unwritable": "{path} yazılamıyor: {error}",
}
