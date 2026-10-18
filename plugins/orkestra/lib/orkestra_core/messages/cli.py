"""cli: human-facing messages (English + Turkish). Keys are `cli.<name>`; both dicts must hold the same keys and {fields}.

Refusals the CLI gives when the machine or a hand-edited file is the problem (a folder that cannot be written, a damaged
config or state file, an unreadable request file). Each one names the file and says what to do next."""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    "cli.no_project": ("not inside a project. cd into it or pass --project <name|path> (workspace: {workspace}). A folder "
                       "with no .git and no project file (package.json, pyproject.toml, Makefile, ...) is not recognised "
                       "by itself: run `orkestra init -p .` inside it once."),
    "cli.io_error": "{detail}. Check that the location exists and that you can write to it.",
    "cli.internal_error": ("unexpected error ({error}). If a config or state file was edited by hand, `orkestra doctor` "
                           "shows what is wrong. Set ORKESTRA_DEBUG=1 for the full traceback."),
    "cli.home_unusable": ("the orkestra home folder {path} cannot be used ({error}). Set ORKESTRA_HOME to a folder you "
                          "can write to (default: ~/.orkestra)."),
    "cli.home_is_file": "it is a file, not a folder",
    # a run's state.json that exists but cannot be used
    "cli.state_damaged": ("run '{run_id}': its state file {path} is damaged ({reason}). Restore it from a backup, or "
                          "delete the run folders {rdir} and {fdir} to drop the run."),
    "cli.damage_json": "not valid JSON",
    "cli.damage_object": "not a JSON object",
    "cli.damage_phase": "unknown phase {phase}",
    "cli.damage_keys": "missing {keys}",
    # ~/.orkestra/config.json (doctor)
    "cli.config_not_json": ("{path} is not valid JSON, so its settings are ignored until it is fixed (delete the file "
                            "to go back to the defaults)."),
    "cli.config_not_object": ("{path} must hold a JSON object, not a list or a single value; its settings are ignored "
                              "until it is fixed."),
    "cli.config_bad_values": ("{path} holds settings of the wrong type: {details}. Fix them in the file (leave a setting "
                              "out to use its default)."),
    "cli.config_wrong_type": "\"{key}\" must be a JSON {expected} (found: {found})",
    "cli.kind_object": "object",
    "cli.kind_array": "array",
    "cli.kind_string": "string",
    "cli.kind_number": "number",
    "cli.kind_boolean": "boolean",
    "cli.kind_null": "null",
    # <project>/.orkestra/config.json
    "cli.project_config_bad": ("{path} cannot be used: {problem}. Fix the file (or delete it so the gates are detected "
                               "again), then repeat the command."),
    "cli.project_config_json": "it is not valid JSON",
    "cli.project_config_object": "it must hold a JSON object",
    "cli.project_config_gates": "\"gates\" must be a list of objects that each have a \"name\" and a \"cmd\"",
    # start --request-file
    "cli.request_file_missing": "request file not found: {path}",
    "cli.request_file_unreadable": "cannot read the request file {path}: {error}",
    "cli.request_file_encoding": ("the request file {path} is not UTF-8 text. Save it as UTF-8 (or UTF-16 with a byte "
                                  "order mark) and try again."),
    "cli.request_file_empty": "the request file {path} is empty (the request must say what to do)",
    # orkestra setup
    "cli.setup_no_engine": ("setup: no engine found (no Claude CLI, no Cursor home, no Copilot home); nothing was "
                            "installed."),
    "cli.setup_ask_claude": "Install the Claude marketplace and plugin?",
    "cli.setup_ask_cursor": "Install the Cursor skill, subagents and hooks?",
    "cli.setup_ask_pi": "Install the Orkestra skills for Pi?",
    "cli.pi_skill_write": "write Pi skill {path}",
    "cli.pi_skill_kept": "left {path} in place: it is not an Orkestra skill",
    "cli.pi_skill_remove": "remove Pi skill {path}",
    "cli.pi_installed": "Pi skills and the tool extension are installed.",
    "cli.pi_extension_missing": "not installed (`orkestra pi install`)",
    "cli.pi_partial": "Pi install is not complete: an existing skill directory was left in place.",
    "cli.pi_dry_run": "Dry run: no Pi files were changed.",
    "cli.pi_removed": "Pi skills removed.",
    "cli.pi_nothing": "Pi skills are already current.",
    "cli.pi_nothing_installed": "No Orkestra Pi skills were installed.",
    "cli.pi_skills_missing": "not installed (`orkestra pi install`)",
    "cli.pi_binary_missing": "pi CLI not found on PATH (https://pi.dev)",
    "cli.pi_skills_required": ("Orkestra skills or the Pi tool extension are not installed. Run `orkestra pi install` once, then try again. "
                               "A bound Pi session then uses the same tool decision as Claude and Cursor."),
    "cli.setup_ask_copilot": "Install the Copilot hooks?",
    "cli.setup_line": "setup: {engine} {state}",
    "cli.setup_state_done": "done",
    "cli.setup_state_current": "already current",
    "cli.setup_state_installed": "installed",
    "cli.setup_state_skipped": "skipped",
    "cli.setup_state_failed": "FAILED",
    "cli.setup_claude_manual": ("setup: Claude Code did not accept every step (see the messages above). Run them yourself, "
                                "then repeat `orkestra setup`:\n  claude plugin marketplace add {root}\n  claude plugin "
                                "install orkestra@orkestra-local -s user"),
    "cli.setup_marketplace_moved": ("setup: the marketplace {name} is registered from another location; registering it "
                                    "again for {root}"),
    "cli.launchd_macos_only": ("--launchd writes a macOS LaunchAgent and works on macOS only. Elsewhere run "
                               "`orkestra watchdog --quiet` every minute from cron, a systemd timer or Task Scheduler "
                               "(the bridge scans by itself while it runs)."),
    "cli.ask_yes_default": "Y/n",
    "cli.ask_no_default": "y/N",
    "cli.run_usage": 'usage: orkestra run [--engine claude|cursor|copilot|pi] [-p <project>] "<request>"',
    "cli.run_opened": "run opened for tracking: {run} in {project} (size {size})",
    "cli.run_dashboard_hint": "follow live: `orkestra dashboard --open` or the bridge Flows panel",
    "cli.watchdog_launchd_wrote": ("wrote {plist}\nload it with:  launchctl load -w {plist}\n(not needed when the bridge "
                                   "runs: it scans every minute by itself)"),
    # orkestra cursor build
    "cli.cursor_assets_written": "cursor assets: {count} file(s) written: {files}",
    "cli.cursor_assets_would": "cursor assets: {count} file(s) would be written (dry run, nothing written): {files}",
    "cli.cursor_assets_current": "cursor assets: already current",
    # orkestra cursor | copilot  (the rows' labels are stable identifiers; their details and the summary lines are prose)
    "cli.kit_would": "would {action}",
    "cli.cursor_nothing_to_change": "cursor: nothing to change",
    "cli.cursor_dry_run": "cursor: dry run, nothing written",
    "cli.cursor_installed": "cursor: installed. New Cursor chats (IDE and `agent`) load it; open ones need a new chat.",
    "cli.cursor_nothing_installed": "cursor: nothing installed",
    "cli.cursor_uninstall_dry": "cursor: dry run, nothing removed",
    "cli.cursor_removed": "cursor: removed",
    "cli.copilot_nothing_to_change": "copilot: nothing to change",
    "cli.copilot_dry_run": "copilot: dry run, nothing written",
    "cli.copilot_installed": "copilot: installed. Restart Copilot / VS Code chats so they load the hooks.",
    "cli.copilot_nothing_installed": "copilot: nothing installed",
    "cli.copilot_uninstall_dry": "copilot: dry run, nothing removed",
    "cli.copilot_removed": "copilot: removed",
    # orkestra doctor  (the labels of the rows stay as they are; the detail and the summary are prose)
    "cli.doctor_not_found": "not found",
    "cli.doctor_not_found_optional": "not found (optional)",
    "cli.doctor_claude_missing": "not found (optional: Cursor/Copilot hooks also count)",
    "cli.doctor_workspace_missing": ("{path} not found (optional: projects resolve by .git/.orkestra; set "
                                     "ORKESTRA_WORKSPACE or workspace_root to change it)"),
    "cli.doctor_python3_fallback": "not found; hooks/run_hook.sh falls back to {fallback}",
    "cli.doctor_python3_none": ("not found, and neither `python` nor `py` is on PATH: the plugin hooks cannot start, so "
                                "the guards do not run"),
    "cli.doctor_copy_same": "{path} (version {version}) matches this checkout",
    "cli.doctor_copy_differs": ("{path} (version {version}) differs from this checkout: Claude Code still runs that older "
                                "copy. Update it with `orkestra setup` or `claude plugin update orkestra@orkestra-local` "
                                "(when it says up to date, raise the plugin version first)"),
    "cli.doctor_active": "active",
    "cli.doctor_not_installed": "not installed",
    "cli.doctor_engines_ready": "{count} ready",
    "cli.doctor_engines_none": "none: install Claude, `orkestra cursor install`, or Copilot hooks",
    "cli.doctor_configured": "configured",
    "cli.doctor_not_configured": "not configured (optional)",
    "cli.doctor_mcp_failed": "could not run",
    "cli.doctor_all_passed": "doctor: all required checks passed",
    "cli.doctor_some_failed": "doctor: some required checks FAILED",
    # --- Cursor / Copilot kits: what an install or uninstall did (or, in a dry run, would do) ----------------------
    "cli.kit_backup": "backup {target} → {backup}",
    "cli.kit_link": "link {dst} → {src}",
    "cli.kit_copy": "copy {dst} → {src}",
    "cli.kit_copy_nolink": "copy {dst} → {src}  (symlink unavailable)",
    "cli.kit_remove": "remove {path}",
    "cli.kit_hooks_added": "update hooks.json: orkestra entries for {events}",
    "cli.kit_hooks_removed": "update hooks.json: orkestra entries removed",
    "cli.kit_write_cmd_shim": "write {path} → {target}  (Windows PATH shim)",
    "cli.kit_link_shim": "link {path} → {target}  (Cursor's shell needs `orkestra` on PATH)",
    "cli.kit_write_shim_wrapper": "write {path} → {target}  (symlink unavailable; shell wrapper)",
    "cli.kit_bad_json": "{path} is not valid JSON: fix it first (nothing was changed)",
    "cli.kit_not_object": "{where} is not a JSON object: fix it first (nothing was changed)",
    "cli.kit_hooks_not_object": "{where}: \"hooks\" is not an object: fix it first (nothing was changed)",
    "cli.kit_event_not_list": "{where}: hooks.{event} is not a list: fix it first (nothing was changed)",
    "cli.kit_stale": "plugins/orkestra/cursor is stale: run `orkestra cursor build` first",
    # --- status rows: the label stays as it is (a stable name), the detail is prose ---------------------------------
    "cli.kit_not_found": "not found",
    "cli.kit_cursor_cli_missing": "not found: https://cursor.com/cli",
    "cli.kit_run_build": "run `orkestra cursor build`",
    "cli.kit_n_missing": "{count} missing",
    "cli.kit_n_events": "{have}/{total} events",
    "cli.kit_not_on_path": "not found (Cursor's shell cannot call the CLI)",
    "cli.kit_older_copy": ("{path}: its hooks answer in Cursor chats with old rules; refresh it with "
                               "`claude plugin update orkestra@orkestra-local` (see README: Cursor)"),
    "cli.kit_active": "active",
    "cli.kit_not_installed": "not installed",
    # `orkestra pause|resume|unblock|block|await|abort` on a run that already is in that state
    "cli.status_unchanged": "{run}: already {status}, nothing changed",
    # orkestra clean
    "cli.clean_disabled": "artifact cleanup is disabled (cleanup_artifacts=false in config)",
    "cli.clean_header": "{mode}: {count} path(s), {freed}",
    "cli.clean_mode_dry": "DRY RUN (nothing removed)",
    "cli.clean_mode_done": "REMOVED",
    "cli.clean_none": "nothing to clean",
    "cli.clean_bytes": "{size} GiB",
    "cli.clean_line": "- {path} ({size})",
    "cli.clean_error": "error: {path}: {error}",
    "cli.mcp_row": "{host}: {name} is {state}",
    "cli.mcp_partial": "installed on {hosts}. Other hosts failed:",
    "cli.mcp_refused": "orkestra mcp changes the MCP configuration: run it from your terminal.",
}

TR: Dict[str, str] = {
    "cli.no_project": ("bir projenin içinde değilsiniz. Proje klasörüne girin (cd) ya da --project <ad|yol> verin "
                       "(çalışma alanı: {workspace}). .git ve proje dosyası (package.json, pyproject.toml, Makefile, ...) "
                       "olmayan bir klasör kendiliğinden tanınmaz: içindeyken bir kez `orkestra init -p .` çalıştırın."),
    "cli.io_error": "{detail}. Konumun var olduğunu ve oraya yazabildiğinizi kontrol edin.",
    "cli.internal_error": ("beklenmeyen hata ({error}). Bir yapılandırma ya da durum dosyası elle düzenlendiyse neyin "
                           "bozuk olduğunu `orkestra doctor` gösterir. Tam hata izi için ORKESTRA_DEBUG=1 verin."),
    "cli.home_unusable": ("orkestra ana klasörü {path} kullanılamıyor ({error}). ORKESTRA_HOME'u yazabileceğiniz bir "
                          "klasöre ayarlayın (varsayılan: ~/.orkestra)."),
    "cli.home_is_file": "klasör değil, bir dosya",
    "cli.state_damaged": ("'{run_id}' run'ı: durum dosyası {path} bozuk ({reason}). Yedekten geri yükleyin ya da "
                          "run'ı bırakmak için {rdir} ve {fdir} klasörlerini silin."),
    "cli.damage_json": "geçerli JSON değil",
    "cli.damage_object": "JSON nesnesi değil",
    "cli.damage_phase": "bilinmeyen faz {phase}",
    "cli.damage_keys": "eksik: {keys}",
    "cli.config_not_json": ("{path} geçerli JSON değil; düzeltilene kadar ayarları yok sayılır (varsayılanlara dönmek "
                            "için dosyayı silin)."),
    "cli.config_not_object": ("{path} bir JSON nesnesi içermeli (liste ya da tek değer olmaz); düzeltilene kadar "
                              "ayarları yok sayılır."),
    "cli.config_bad_values": ("{path} içinde türü yanlış ayarlar var: {details}. Dosyada düzeltin (varsayılanı "
                              "kullanmak için ayarı silin)."),
    "cli.config_wrong_type": "\"{key}\" için JSON türü {expected} olmalı (bulunan: {found})",
    "cli.kind_object": "nesne",
    "cli.kind_array": "dizi",
    "cli.kind_string": "metin",
    "cli.kind_number": "sayı",
    "cli.kind_boolean": "mantıksal değer",
    "cli.kind_null": "null",
    "cli.project_config_bad": ("{path} kullanılamıyor: {problem}. Dosyayı düzeltin (ya da gate'ler yeniden bulunsun "
                               "diye silin), sonra komutu yineleyin."),
    "cli.project_config_json": "geçerli JSON değil",
    "cli.project_config_object": "bir JSON nesnesi içermeli",
    "cli.project_config_gates": "\"gates\" bir liste olmalı ve her öğede \"name\" ile \"cmd\" bulunmalı",
    "cli.request_file_missing": "istek dosyası bulunamadı: {path}",
    "cli.request_file_unreadable": "istek dosyası {path} okunamıyor: {error}",
    "cli.request_file_encoding": ("istek dosyası {path} UTF-8 metin değil. UTF-8 (ya da BOM'lu UTF-16) olarak kaydedip "
                                  "yeniden deneyin."),
    "cli.request_file_empty": "istek dosyası {path} boş (istek ne yapılacağını söylemeli)",
    "cli.setup_no_engine": ("setup: motorsuz. Claude CLI, Cursor dizini ya da Copilot dizini bulunamadı; hiçbir şey "
                            "kurulmadı."),
    "cli.setup_ask_claude": "Claude marketplace/plugin kurulsun mu?",
    "cli.setup_ask_cursor": "Cursor skill, alt-ajan ve hooks kurulsun mu?",
    "cli.setup_ask_pi": "Pi için Orkestra skill'leri kurulsun mu?",
    "cli.pi_skill_write": "Pi skill yaz: {path}",
    "cli.pi_skill_kept": "{path} duruyor: Orkestra skill'i değil",
    "cli.pi_skill_remove": "Pi skill kaldır: {path}",
    "cli.pi_installed": "Pi skill'leri ve araç eklentisi kuruldu.",
    "cli.pi_extension_missing": "kurulu değil (`orkestra pi install`)",
    "cli.pi_partial": "Pi kurulumu tamam değil: var olan bir skill dizini yerinde bırakıldı.",
    "cli.pi_dry_run": "Deneme: Pi dosyası değişmedi.",
    "cli.pi_removed": "Pi skill'leri kaldırıldı.",
    "cli.pi_nothing": "Pi skill'leri zaten güncel.",
    "cli.pi_nothing_installed": "Kurulu Orkestra Pi skill'i yoktu.",
    "cli.pi_skills_missing": "kurulu değil (`orkestra pi install`)",
    "cli.pi_binary_missing": "pi CLI PATH'te yok (https://pi.dev)",
    "cli.pi_skills_required": ("Pi için Orkestra skill'leri veya araç eklentisi kurulu değil. Bir kez `orkestra pi install` "
                               "çalıştırın, sonra yeniden deneyin. Bağlı Pi oturumu Claude ve Cursor ile aynı araç kararını kullanır."),
    "cli.setup_ask_copilot": "Copilot hooks kurulsun mu?",
    "cli.setup_line": "setup: {engine} {state}",
    "cli.setup_state_done": "tamam",
    "cli.setup_state_current": "zaten güncel",
    "cli.setup_state_installed": "kuruldu",
    "cli.setup_state_skipped": "atlandı",
    "cli.setup_state_failed": "BAŞARISIZ",
    "cli.setup_claude_manual": ("setup: Claude Code her adımı kabul etmedi (yukarıdaki mesajlara bak). Adımları kendiniz "
                                "çalıştırın, sonra `orkestra setup` komutunu yineleyin:\n  claude plugin marketplace add "
                                "{root}\n  claude plugin install orkestra@orkestra-local -s user"),
    "cli.setup_marketplace_moved": ("setup: {name} marketplace'i başka bir konumdan kayıtlı; {root} için yeniden "
                                    "kaydediliyor"),
    "cli.launchd_macos_only": ("--launchd yalnız macOS'ta çalışan bir LaunchAgent yazar. Diğer sistemlerde "
                               "`orkestra watchdog --quiet` komutunu cron, systemd timer ya da Görev Zamanlayıcı ile "
                               "dakikada bir çalıştırın (bridge çalışırken kendisi tarar)."),
    "cli.ask_yes_default": "E/h",
    "cli.ask_no_default": "e/H",
    "cli.run_usage": 'kullanım: orkestra run [--engine claude|cursor|copilot|pi] [-p <proje>] "<istek>"',
    "cli.run_opened": "takip için run açıldı: {run} · {project} (boyut {size})",
    "cli.run_dashboard_hint": "canlı takip: `orkestra dashboard --open` veya bridge Flows paneli",
    "cli.watchdog_launchd_wrote": ("yazıldı: {plist}\nşununla yükle:  launchctl load -w {plist}\n(bridge çalışırken gerekmez: "
                                   "o kendisi dakikada bir tarar)"),
    "cli.cursor_assets_written": "cursor dosyaları: {count} dosya yazıldı: {files}",
    "cli.cursor_assets_would": "cursor dosyaları: {count} dosya yazılırdı (deneme modu, hiçbir şey yazılmadı): {files}",
    "cli.cursor_assets_current": "cursor dosyaları: zaten güncel",
    "cli.kit_would": "şunu yapardı: {action}",
    "cli.cursor_nothing_to_change": "cursor: değiştirilecek bir şey yok",
    "cli.cursor_dry_run": "cursor: deneme modu, hiçbir şey yazılmadı",
    "cli.cursor_installed": "cursor: kuruldu. Yeni Cursor sohbetleri (IDE ve `agent`) onu yükler; açık olanlar için yeni sohbet gerekir.",
    "cli.cursor_nothing_installed": "cursor: kurulu bir şey yok",
    "cli.cursor_uninstall_dry": "cursor: deneme modu, hiçbir şey kaldırılmadı",
    "cli.cursor_removed": "cursor: kaldırıldı",
    "cli.copilot_nothing_to_change": "copilot: değiştirilecek bir şey yok",
    "cli.copilot_dry_run": "copilot: deneme modu, hiçbir şey yazılmadı",
    "cli.copilot_installed": "copilot: kuruldu. Hook'ların yüklenmesi için Copilot / VS Code sohbetlerini yeniden başlatın.",
    "cli.copilot_nothing_installed": "copilot: kurulu bir şey yok",
    "cli.copilot_uninstall_dry": "copilot: deneme modu, hiçbir şey kaldırılmadı",
    "cli.copilot_removed": "copilot: kaldırıldı",
    "cli.doctor_not_found": "bulunamadı",
    "cli.doctor_not_found_optional": "bulunamadı (isteğe bağlı)",
    "cli.doctor_claude_missing": "bulunamadı (isteğe bağlı: Cursor/Copilot hook'ları da sayılır)",
    "cli.doctor_workspace_missing": ("{path} bulunamadı (isteğe bağlı: projeler .git/.orkestra ile bulunur; değiştirmek için "
                                     "ORKESTRA_WORKSPACE ya da workspace_root ayarlayın)"),
    "cli.doctor_python3_fallback": "bulunamadı; hooks/run_hook.sh {fallback} komutuna döner",
    "cli.doctor_python3_none": ("bulunamadı ve PATH içinde `python` da `py` de yok: eklenti hook'ları başlayamaz, bu yüzden "
                                "guard'lar çalışmaz"),
    "cli.doctor_copy_same": "{path} (sürüm {version}) bu checkout ile aynı",
    "cli.doctor_copy_differs": ("{path} (sürüm {version}) bu checkout'tan farklı: Claude Code hâlâ o eski kopyayı çalıştırıyor. "
                                "`orkestra setup` ya da `claude plugin update orkestra@orkestra-local` ile güncelleyin "
                                "(güncel diyorsa önce eklenti sürümünü yükseltin)"),
    "cli.doctor_active": "etkin",
    "cli.doctor_not_installed": "kurulu değil",
    "cli.doctor_engines_ready": "{count} hazır",
    "cli.doctor_engines_none": "hiç yok: Claude kurun, `orkestra cursor install` ya da Copilot hook'larını çalıştırın",
    "cli.doctor_configured": "yapılandırılmış",
    "cli.doctor_not_configured": "yapılandırılmamış (isteğe bağlı)",
    "cli.doctor_mcp_failed": "çalıştırılamadı",
    "cli.doctor_all_passed": "doctor: gerekli tüm kontroller geçti",
    "cli.doctor_some_failed": "doctor: bazı gerekli kontroller BAŞARISIZ",
    # --- Cursor / Copilot kits ---------------------------------------------------------------------------------
    "cli.kit_backup": "yedekle {target} → {backup}",
    "cli.kit_link": "bağla {dst} → {src}",
    "cli.kit_copy": "kopyala {dst} → {src}",
    "cli.kit_copy_nolink": "kopyala {dst} → {src}  (symlink kullanılamıyor)",
    "cli.kit_remove": "kaldır {path}",
    "cli.kit_hooks_added": "hooks.json güncelle: {events} için orkestra girdileri",
    "cli.kit_hooks_removed": "hooks.json güncelle: orkestra girdileri kaldırıldı",
    "cli.kit_write_cmd_shim": "yaz {path} → {target}  (Windows PATH kısayolu)",
    "cli.kit_link_shim": "bağla {path} → {target}  (Cursor'ın kabuğu için `orkestra` PATH içinde olmalı)",
    "cli.kit_write_shim_wrapper": "yaz {path} → {target}  (symlink kullanılamıyor; kabuk sarmalayıcısı)",
    "cli.kit_bad_json": "{path} geçerli JSON değil: önce düzeltin (hiçbir şey değiştirilmedi)",
    "cli.kit_not_object": "{where} bir JSON nesnesi değil: önce düzeltin (hiçbir şey değiştirilmedi)",
    "cli.kit_hooks_not_object": "{where}: \"hooks\" bir nesne değil: önce düzeltin (hiçbir şey değiştirilmedi)",
    "cli.kit_event_not_list": "{where}: hooks.{event} bir liste değil: önce düzeltin (hiçbir şey değiştirilmedi)",
    "cli.kit_stale": "plugins/orkestra/cursor eski: önce `orkestra cursor build` çalıştırın",
    "cli.kit_not_found": "bulunamadı",
    "cli.kit_cursor_cli_missing": "bulunamadı: https://cursor.com/cli",
    "cli.kit_run_build": "`orkestra cursor build` çalıştırın",
    "cli.kit_n_missing": "{count} eksik",
    "cli.kit_n_events": "{have}/{total} olay",
    "cli.kit_not_on_path": "bulunamadı (Cursor'ın kabuğu CLI'yi çağıramaz)",
    "cli.kit_older_copy": ("{path}: hook'ları Cursor sohbetlerinde eski kurallarla yanıt veriyor; "
                               "`claude plugin update orkestra@orkestra-local` ile yenileyin (README: Cursor bölümüne bakın)"),
    "cli.kit_active": "etkin",
    "cli.kit_not_installed": "kurulu değil",
    "cli.status_unchanged": "{run}: zaten {status}, bir şey değişmedi",
    "cli.clean_disabled": "artık temizliği kapalı (config'te cleanup_artifacts=false)",
    "cli.clean_header": "{mode}: {count} yol, {freed}",
    "cli.clean_mode_dry": "KURU ÇALIŞTIRMA (hiçbir şey silinmedi)",
    "cli.clean_mode_done": "SİLİNDİ",
    "cli.clean_none": "temizlenecek bir şey yok",
    "cli.clean_bytes": "{size} GiB",
    "cli.clean_line": "- {path} ({size})",
    "cli.clean_error": "hata: {path}: {error}",
    "cli.mcp_row": "{host}: {name} durumu {state}",
    "cli.mcp_partial": "{hosts} üzerine kuruldu. Diğer hedefler başarısız:",
    "cli.mcp_refused": "orkestra mcp MCP yapılandırmasını değiştirir: bunu terminalinizden çalıştırın.",
}
