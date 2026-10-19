"""watchdog: human-facing messages (English + Turkish). Keys are `watchdog.<name>`; both dicts must hold the same keys and {fields}.

The incident headline (`message`) is what the macOS / Linux notification, the webhook, the bridge (Telegram) and the
`orkestra watchdog` output show. `{text}` is the classifier's own sentence (tempo.classify). The resume decisions
(`resume_decision()["why"]`) are diagnostics and stay English in both languages."""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    # incident headline by class (tempo.classify cls 1..4; anything else is plain information)
    "watchdog.head_user": "WAITING FOR YOU",
    "watchdog.head_dead_wait": "DEAD WAIT",
    "watchdog.head_hung": "HUNG COMMAND",
    "watchdog.head_silent": "SILENT SESSION",
    "watchdog.head_info": "info",
    "watchdog.message": "[{project}] {head}{since}: {text} · phase {phase}",
    # what the watchdog did about it (appended to the headline)
    "watchdog.auto_resumed": " → auto-resumed (attempt {attempt})",
    "watchdog.auto_resume_limit": " → auto-resume limit reached, you need to continue it (/orkestra:resume)",
    # `orkestra watchdog` output
    "watchdog.no_open_runs": "No open orkestra runs.",
    "watchdog.damaged_run": "[{project}] DAMAGED run {run_id}: state.json is unreadable. Run `orkestra doctor --repair` to restore it from state.json.prev.",
    "watchdog.damaged_run_noprev": "[{project}] DAMAGED run {run_id}: state.json is unreadable and there is no state.json.prev. Run `orkestra doctor --repair` to move it aside.",
    "watchdog.note_test_broad": "NOTE {task}: the Test line runs the whole suite ({test}); name a path or filter (-k, -run, --testNamePattern).",
    "watchdog.note_acceptance_vague": "NOTE {task}: the Acceptance line has no Given/When/Then; state the check as a concrete case.",
    "watchdog.loop_line": "   loop: {loops}",
    "watchdog.resume_line": "   auto-resume: {why}",
    # fast watcher (fastwatch.py): one notification per incident, in the incident's own words
    "watchdog.fast_deny_loop": "[{project}] DENY LOOP: the same refusal {count} times ({what}). The agent keeps retrying a blocked action; change the approach or block the run.",
    "watchdog.fast_control_flips": "[{project}] PAUSE/RESUME STORM: the run changed status {count} times in 10 minutes. It is recorded as an incident; find the session that keeps flipping it.",
    "watchdog.fast_paused_working": "[{project}] PAUSED BUT WORKING: the run is paused, yet an agent ran a tool {dur} ago.",
    "watchdog.fast_report_overdue": "[{project}] REPORT OVERDUE: a subagent stopped {dur} ago and its report never reached the main session.",
    "watchdog.fast_idle_active": "[{project}] NOBODY DRIVING: the run is active, the session ended its turn {dur} ago and work is left.",
    "watchdog.fast_permission_wait": "[{project}] PERMISSION WAIT: the host is waiting for your approval ({what}). Orkestra never approves for you.",
    "watchdog.fast_hung_command": "[{project}] HUNG COMMAND? `{what}` has run {dur} with no sign of life from any agent{proof}.",
    "watchdog.fast_stop_failure": "[{project}] TURN FAILED ({what}): a model or API error ended the turn {dur} ago.",
    "watchdog.fast_dead_session": "[{project}] SESSION GONE: the host process of the session that drove this run no longer exists.",
    "watchdog.fast_soft_silence": "[{project}] quiet for {dur}",
    "watchdog.fast_hooks_degraded": "[{project}] HOOKS DEGRADED: {count} hook errors in a minute ({what}). Guards may be failing open.",
    "watchdog.fast_breaker": "[{project}] FAST WATCHER: {count} failed ticks in a row ({what}). The circuit breaker is open: it only notifies now and heals nothing.",
    "watchdog.fast_proof_cpu": " (no CPU used between two samples)",
    "watchdog.fast_proof_none": " (process state not verified)",
    # `orkestra watchdog --status` / --daemon / doctor
    "watchdog.fast_status_running": "Fast watcher: running (pid {pid}) · tick {interval} s · {runs} open runs · last tick {ago} ago",
    "watchdog.fast_status_breaker": "Fast watcher: circuit breaker OPEN (notifies only, no automatic healing)",
    "watchdog.fast_status_stopped": "Fast watcher: not running (autostart {autostart})",
    "watchdog.fast_flag_line": "   {run}: {flags}",
    "watchdog.fast_daemon_started": "Fast watcher started (pid {pid}).",
    "watchdog.fast_daemon_running": "Fast watcher already running (pid {pid}).",
    "watchdog.fast_daemon_disabled": "Fast watcher not started: disabled by ORKESTRA_NO_DAEMON=1, or this platform has no flock (Windows).",
    "watchdog.fast_doctor_name": "fast watcher",
    "watchdog.fast_doctor_on": "running (pid {pid})",
    "watchdog.fast_doctor_off": "not running; SessionStart starts it (autostart {autostart})",
    # bridge turns (bridge/orkestra_bridge.py)
    "watchdog.turn_timeout": "The turn was interrupted after {seconds} s (wall-clock budget bridge.turn_timeout_seconds).",
    "watchdog.turn_idle": "The turn was interrupted: no run activity for {seconds} s (bridge.turn_idle_seconds).",
    "watchdog.driver_alive": "The previous turn of this job is still running (pid {pid}). Wait for it to end or cancel the job; a second agent on one session would corrupt it.",
}

TR: Dict[str, str] = {
    "watchdog.head_user": "SENİ BEKLİYOR",
    "watchdog.head_dead_wait": "ÖLÜ BEKLEME",
    "watchdog.head_hung": "ASILI KOMUT",
    "watchdog.head_silent": "SESSİZ OTURUM",
    "watchdog.head_info": "bilgi",
    "watchdog.message": "[{project}] {head}{since} — {text} · faz {phase}",
    "watchdog.auto_resumed": " → otomatik devam ettirildi (deneme {attempt})",
    "watchdog.auto_resume_limit": " → otomatik devam sınırı aşıldı, senin devam ettirmen gerekiyor (/orkestra:resume)",
    "watchdog.no_open_runs": "Açık orkestra çalışması yok.",
    "watchdog.damaged_run": "[{project}] BOZUK çalışma {run_id}: state.json okunamıyor. `orkestra doctor --repair` ile state.json.prev dosyasından geri yükle.",
    "watchdog.damaged_run_noprev": "[{project}] BOZUK çalışma {run_id}: state.json okunamıyor ve state.json.prev yok. Kenara almak için `orkestra doctor --repair` çalıştır.",
    "watchdog.note_test_broad": "NOT {task}: Test satırı tüm paketi çalıştırıyor ({test}); bir yol veya süzgeç ver (-k, -run, --testNamePattern).",
    "watchdog.note_acceptance_vague": "NOT {task}: Kabul satırında Verilen/Eğer/İse yok; kontrolü somut bir durum olarak yaz.",
    "watchdog.loop_line": "   döngü: {loops}",
    "watchdog.resume_line": "   otomatik devam: {why}",
    "watchdog.fast_deny_loop": "[{project}] RET DÖNGÜSÜ: aynı ret {count} kez ({what}). Ajan engellenmiş bir eylemi tekrar deniyor; yaklaşımı değiştir ya da run'ı engelle.",
    "watchdog.fast_control_flips": "[{project}] DURAKLAT/DEVAM FIRTINASI: run 10 dakikada {count} kez durum değiştirdi. Olay olarak kaydedildi; onu çeviren oturumu bul.",
    "watchdog.fast_paused_working": "[{project}] DURAKLATILMIŞ AMA ÇALIŞIYOR: run duraklatılmış, yine de bir ajan {dur} önce araç çalıştırdı.",
    "watchdog.fast_report_overdue": "[{project}] RAPOR GECİKTİ: bir alt ajan {dur} önce durdu ve raporu ana oturuma hiç ulaşmadı.",
    "watchdog.fast_idle_active": "[{project}] KİMSE SÜRMÜYOR: run aktif, oturum turunu {dur} önce bitirdi ve iş kaldı.",
    "watchdog.fast_permission_wait": "[{project}] İZİN BEKLİYOR: ana makine onayını bekliyor ({what}). Orkestra senin yerine asla onaylamaz.",
    "watchdog.fast_hung_command": "[{project}] ASILI KOMUT MU? `{what}` {dur} boyunca çalışıyor, hiçbir ajandan yaşam belirtisi yok{proof}.",
    "watchdog.fast_stop_failure": "[{project}] TUR BAŞARISIZ ({what}): bir model ya da API hatası turu {dur} önce bitirdi.",
    "watchdog.fast_dead_session": "[{project}] OTURUM YOK: bu run'ı süren oturumun ana süreci artık yok.",
    "watchdog.fast_soft_silence": "[{project}] {dur} süredir sessiz",
    "watchdog.fast_hooks_degraded": "[{project}] KANCALAR BOZUK: bir dakikada {count} kanca hatası ({what}). Korumalar açık bırakıyor olabilir.",
    "watchdog.fast_breaker": "[{project}] HIZLI BEKÇİ: art arda {count} tur başarısız ({what}). Devre kesici açık: artık yalnız bildirir, hiçbir şeyi onarmaz.",
    "watchdog.fast_proof_cpu": " (iki örnek arasında CPU kullanılmadı)",
    "watchdog.fast_proof_none": " (süreç durumu doğrulanamadı)",
    "watchdog.fast_status_running": "Hızlı bekçi: çalışıyor (pid {pid}) · tur {interval} sn · {runs} açık run · son tur {ago} önce",
    "watchdog.fast_status_breaker": "Hızlı bekçi: devre kesici AÇIK (yalnız bildirir, otomatik onarım yok)",
    "watchdog.fast_status_stopped": "Hızlı bekçi: çalışmıyor (otomatik başlatma {autostart})",
    "watchdog.fast_flag_line": "   {run}: {flags}",
    "watchdog.fast_daemon_started": "Hızlı bekçi başlatıldı (pid {pid}).",
    "watchdog.fast_daemon_running": "Hızlı bekçi zaten çalışıyor (pid {pid}).",
    "watchdog.fast_daemon_disabled": "Hızlı bekçi başlatılmadı: ORKESTRA_NO_DAEMON=1 ile kapatılmış ya da bu platformda flock yok (Windows).",
    "watchdog.fast_doctor_name": "hızlı bekçi",
    "watchdog.fast_doctor_on": "çalışıyor (pid {pid})",
    "watchdog.fast_doctor_off": "çalışmıyor; SessionStart başlatır (otomatik başlatma {autostart})",
    "watchdog.turn_timeout": "Tur {seconds} sn sonra kesildi (duvar saati bütçesi bridge.turn_timeout_seconds).",
    "watchdog.turn_idle": "Tur kesildi: {seconds} sn boyunca run etkinliği yok (bridge.turn_idle_seconds).",
    "watchdog.driver_alive": "Bu işin önceki turu hâlâ çalışıyor (pid {pid}). Bitmesini bekle ya da işi iptal et; tek oturumda ikinci bir ajan oturumu bozar.",
}

