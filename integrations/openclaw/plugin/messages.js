// integrations/openclaw/plugin/messages.js
// Everything the plugin says to a person, in English and Turkish. `t(key, params, lang)` picks the text; the language
// comes from lang.js (ORKESTRA_LANG, config.json, OS locale, English). Same policy as the Python side: text for a
// human is here; protocol tokens stay accepted in both languages (see logic.js: parseJobRef, expandOptionReply).
//
// The status labels and `job_ref` repeat the ones of the bridge (plugins/orkestra/lib/orkestra_core/messages/bridge.py,
// keys `bridge.label.*` and `bridge.job_ref`) on purpose: the plugin builds the same notification header the bridge
// sends. bridge/tests/test_bridge_i18n.py compares the two tables.
import { DEFAULT_LANG, resolveLang } from "./lang.js";

export const MESSAGES = {
  en: {
    "label.running": "▶️ Running",
    "label.queued": "⏳ Queued",
    "label.awaiting_user": "⏸ Waiting for your reply",
    "label.done": "✅ Done",
    "label.blocked": "⛔ Blocked",
    "label.error": "❌ Error",
    "label.cancelled": "🛑 Cancelled",
    "label.paused": "⏸ Paused",
    "job_ref": "job {id}",
    "tasks.suffix": " · task {done}/{total}",
    "where.telegram_run": "Telegram /run",
    "where.telegram_job": "Telegram job",
    "where.desktop": "desktop",
    "notify.reply_hint": "↩️ Use Reply on this message to answer (if it lists options, send just the letter).",
    "busy.header": "⛔ Not started: project [{project}] already has an open Orkestra run: \"{title}\"",
    "busy.footer": "A second run in the same project could take over the open one and break it. Wait for it to finish (/runs) or close that run from its own session.",
    "status.title": "🎼 Orkestra status",
    "status.runs_error": "⚠️ Could not read the run list: {error}",
    "status.no_open_runs": "No open runs.",
    "status.open_runs": "Open runs ({n}):",
    "status.run_head": "• {project}: {title}",
    "status.closed": "Closed in the last 12 hours: {list}",
    "status.jobs_error": "⚠️ Could not read the Telegram jobs: {error}",
    "status.no_jobs": "No open job started from Telegram.",
    "status.open_jobs": "Open jobs started from Telegram ({n}):",
    "status.reply_hint": "↩️ Use Reply on a job's notification message to answer it.",
    "status.desktop_note": "ℹ️ Runs started on the desktop are monitored here; answer their questions from that project's Claude window.",
    "cmd.usage": "Usage: /run [claude:|cursor:|copilot:|pi:] <project> <request>\nExample: /run copilot: shop add CSV export to the orders list",
    "cmd.run_desc": "Start an Orkestra job: /run [engine:] <project> <request>",
    "cmd.runs_desc": "Orkestra status: every open run (desktop included) and Telegram jobs",
    "cmd.cancel_desc": "Stop the latest open Orkestra job",
    "cmd.check_failed": "⚠️ Not started: could not check for open runs ({error}). Waiting, to be safe; try again in a moment.",
    "cmd.start_failed": "⚠️ Could not start: {error}",
    "cmd.unknown_project": "⚠️ Not started: there is no project folder \"{name}\" in the workspace.",
    "cmd.did_you_mean": "Did you mean: {list}?",
    "cmd.started": "▶️ Started: [{project}] job {id}{engine}",
    "cmd.started_note": "I will finish it on autopilot by default; if you asked for manual approval, use Reply on this message only at the approval step.",
    "cmd.cancel_sent": "Cancel request sent.",
    "cmd.cancel_failed": "⚠️ Could not cancel: {error}",
    "route.empty": "⚠️ An empty reply was not forwarded to Orkestra.",
    "route.forwarded": "↪️ [{project}] job {id} → {text}",
    "route.failed": "⚠️ Could not forward to [{project}] job {id}: {error}",
  },
  tr: {
    "label.running": "▶️ Çalışıyor",
    "label.queued": "⏳ Sırada",
    "label.awaiting_user": "⏸ Yanıtını bekliyor",
    "label.done": "✅ Tamamlandı",
    "label.blocked": "⛔ Engellendi",
    "label.error": "❌ Hata",
    "label.cancelled": "🛑 İptal",
    "label.paused": "⏸ Duraklatıldı",
    "job_ref": "iş {id}",
    "tasks.suffix": " · görev {done}/{total}",
    "where.telegram_run": "Telegram /yap",
    "where.telegram_job": "Telegram işi",
    "where.desktop": "masaüstü",
    "notify.reply_hint": "↩️ Cevap için bu mesajı Yanıtla (seçenekliyse yalnız harfi yaz).",
    "busy.header": "⛔ Başlatmadım: [{project}] projesinde zaten açık bir orkestra run var — \"{title}\"",
    "busy.footer": "Aynı projede ikinci run, açık olanı devralıp bozabilir. Bitmesini bekle (/durum) ya da o run'ı kendi oturumundan kapat.",
    "status.title": "🎼 Orkestra durumu",
    "status.runs_error": "⚠️ Run listesi alınamadı: {error}",
    "status.no_open_runs": "Açık run yok.",
    "status.open_runs": "Açık run'lar ({n}):",
    "status.run_head": "• {project} — {title}",
    "status.closed": "Son 12 saatte kapanan: {list}",
    "status.jobs_error": "⚠️ Telegram işleri alınamadı: {error}",
    "status.no_jobs": "Telegram'dan başlatılmış açık iş yok.",
    "status.open_jobs": "Telegram'dan başlatılan açık işler ({n}):",
    "status.reply_hint": "↩️ Bu işlerin bildirim mesajını Yanıtla ile cevaplayabilirsin.",
    "status.desktop_note": "ℹ️ Masaüstünde başlatılan run'lar buradan izlenir; soruları o projenin Claude penceresinden cevaplanır.",
    "cmd.usage": "Kullanım: /yap [claude:|cursor:|copilot:|pi:] <proje> <istek>\nÖrnek: /yap copilot: shop sipariş listesine CSV dışa aktarma ekle",
    "cmd.run_desc": "Orkestra işi başlat: /yap [engine:] <proje> <istek>",
    "cmd.runs_desc": "Orkestra durumu: tüm açık run'lar (masaüstü dahil) ve Telegram işleri",
    "cmd.cancel_desc": "Son açık orkestra işini durdur",
    "cmd.check_failed": "⚠️ Başlatmadım: açık run kontrolü yapılamadı ({error}). Güvenlik için bekliyorum; biraz sonra tekrar dene.",
    "cmd.start_failed": "⚠️ Başlatılamadı: {error}",
    "cmd.unknown_project": "⚠️ Başlatmadım: çalışma alanında \"{name}\" adlı proje klasörü yok.",
    "cmd.did_you_mean": "Bunlardan biri mi: {list}?",
    "cmd.started": "▶️ Başladım: [{project}] iş {id}{engine}",
    "cmd.started_note": "Varsayılan autopilot'ta tamamlayacağım; manual onay istediysen yalnız onay aşamasında bu mesaja Yanıtla.",
    "cmd.cancel_sent": "İptal isteği gönderildi.",
    "cmd.cancel_failed": "⚠️ İptal edilemedi: {error}",
    "route.empty": "⚠️ Boş yanıt orkestra'ya iletilmedi.",
    "route.forwarded": "↪️ [{project}] iş {id} → {text}",
    "route.failed": "⚠️ [{project}] iş {id}'ye iletilemedi: {error}",
  },
};

/** The `{name}` fields of a template, sorted (the parity test compares both languages). */
export function placeholders(template) {
  return [...new Set([...String(template).matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort();
}

/**
 * Message `key` in `lang` (default: the language resolved right now). Missing in that language: English, then the key
 * itself. A `{name}` without a param stays as written; nothing here ever throws.
 * @param {string} key
 * @param {Record<string, unknown>} [params]
 * @param {string} [lang] "en" | "tr"
 */
export function t(key, params = {}, lang = resolveLang()) {
  const text = MESSAGES[lang]?.[key] ?? MESSAGES[DEFAULT_LANG][key] ?? key;
  return text.replace(/\{(\w+)\}/g, (whole, name) => (params[name] === undefined ? whole : String(params[name])));
}
