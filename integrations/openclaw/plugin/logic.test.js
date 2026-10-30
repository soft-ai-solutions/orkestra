// integrations/openclaw/plugin/logic.test.js — node --test logic.test.js
// These assertions predate the English default and read the Turkish texts: pinned to Turkish like the Python suite
// (plugins/orkestra/tests/helpers.py). English, both-language and language-resolution cases: logic.i18n.test.js,
// lang.test.js, messages.test.js.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  diffJobs, expandOptionReply, findOpenRun, formatBusyProject, formatNotification, formatStatus, parseJobRef, parseSentMessageIds,
} from "./logic.js";

process.env.ORKESTRA_LANG = "tr";

const JOB = "0123456789ab";

test("parseJobRef finds the job in bridge headers and start confirmations", () => {
  assert.deepEqual(parseJobRef(`[shop] ⏸ Yanıtını bekliyor · clarify · iş ${JOB}\n\nSoru?`), {
    project: "shop",
    jobId: JOB,
  });
  assert.deepEqual(parseJobRef(`▶️ Başladım: [mobile-app] iş ${JOB}\nSoru ya da onay…`), {
    project: "mobile-app",
    jobId: JOB,
  });
  assert.equal(parseJobRef("Sıradan bir Claude cevabı, iş yok."), null);
  assert.equal(parseJobRef(`[x] iş 0123`), null);
  assert.equal(parseJobRef(undefined), null);
});

test("expandOptionReply maps a bare letter to the quoted option", () => {
  const lines = "Hangisi?\na) Onayla ve uygula\nb) Planı değiştir\nc) İptal";
  assert.equal(expandOptionReply("b", lines), "b) Planı değiştir");
  assert.equal(expandOptionReply("B)", lines), "b) Planı değiştir");
  assert.equal(expandOptionReply("(c)", lines), "c) İptal");
  // Inline form produced by the orkestra run (see bridge/tests/fake_claude.py).
  const inline = "1. Varsayılan öncelik ne olsun? (a) medium (Önerilen) (b) high";
  assert.equal(expandOptionReply("a", inline), "a) medium (Önerilen)");
  assert.equal(expandOptionReply("b", inline), "b) high");
});

test("choosing the approve option sends orkestra's exact approval word; hedged options stay verbatim", () => {
  const plan = "Plan hazır.\na) Onayla — uygulamaya başla\nb) Planı değiştir\nc) Onayla ama testleri de ekle";
  assert.equal(expandOptionReply("a", plan), "onayla");
  assert.equal(expandOptionReply("b", plan), "b) Planı değiştir");
  assert.equal(expandOptionReply("c", plan), "c) Onayla ama testleri de ekle");
  assert.equal(expandOptionReply("a", "a) Evet, başla\nb) Hayır"), "onayla");
  assert.equal(expandOptionReply("a", "a) Onaylıyor muyum?\nb) x"), "a) Onaylıyor muyum?");
});

test("expandOptionReply leaves everything else as typed", () => {
  const lines = "a) Onayla\nb) Değiştir";
  assert.equal(expandOptionReply("onay", lines), "onay");
  assert.equal(expandOptionReply("  b'yi seçiyorum  ", lines), "b'yi seçiyorum");
  assert.equal(expandOptionReply("d", lines), "d");
  assert.equal(expandOptionReply("a", undefined), "a");
});

test("formatNotification matches the bridge header and adds a reply hint only when waiting", () => {
  const waiting = formatNotification({ id: JOB, project: "demo", status: "awaiting_user", phase: "clarify", last_message: "Soru?" });
  assert.match(waiting, new RegExp(`^\\[demo\\] ⏸ Yanıtını bekliyor · clarify · iş ${JOB}\\n\\nSoru\\?`));
  assert.ok(waiting.includes("Yanıtla"));
  assert.deepEqual(parseJobRef(waiting), { project: "demo", jobId: JOB });
  const done = formatNotification({ id: JOB, project: "demo", status: "done", phase: null, last_message: "" });
  assert.equal(done, `[demo] ✅ Tamamlandı · iş ${JOB}`);
});

test("diffJobs seeds silently, then announces each stopped state once", () => {
  const jobs1 = [{ id: "a", status: "done", updated: "t1" }, { id: "b", status: "running", updated: "t1" }];
  const first = diffJobs(jobs1, null);
  assert.deepEqual(first.announce, []);
  assert.deepEqual(first.seen, { a: "t1", b: "t1" });

  const jobs2 = [{ id: "a", status: "done", updated: "t1" }, { id: "b", status: "awaiting_user", updated: "t2" },
    { id: "c", status: "queued", updated: "t2" }];
  const second = diffJobs(jobs2, first.seen);
  assert.deepEqual(second.announce.map((j) => j.id), ["b"]);

  const third = diffJobs(jobs2, { ...second.seen, b: "t2" });
  assert.deepEqual(third.announce, []);
});

// Shape of `orkestra watch --json` entries (fields used by formatStatus).
const DESKTOP_RUNS = [
  { project_name: "deals-app", title: "Derin analiz ve hata düzeltme", phase: "clarify", phase_tr: "netleştirme",
    status: "active", status_tr: "aktif", mode: "interactive", tasks_done: 0, tasks_total: 0,
    live: { level: "wait", text: "muhtemelen SENİ BEKLİYOR — clarify fazında 3 dk hareket yok" } },
  { project_name: "mobile-app", title: "Önerilen geliştirmelerin tamamı", phase: "implement", phase_tr: "uygulama",
    status: "active", status_tr: "aktif", mode: "interactive", tasks_done: 4, tasks_total: 19,
    live: { level: "run", text: "çalışıyor — implementer" } },
  { project_name: "shop", title: "Eski iş", phase: "done", phase_tr: "bitti", status: "done", status_tr: "bitti",
    mode: "interactive", tasks_done: 3, tasks_total: 3 },
];

test("formatStatus reports desktop runs even when the bridge has no jobs", () => {
  const text = formatStatus({ runs: DESKTOP_RUNS, bridgeJobs: [] });
  assert.match(text, /Açık run'lar \(2\):/);
  assert.match(text, /• deals-app — Derin analiz ve hata düzeltme\n {2}netleştirme · masaüstü · muhtemelen SENİ BEKLİYOR/);
  assert.match(text, /• mobile-app — Önerilen geliştirmelerin tamamı\n {2}uygulama · görev 4\/19 · masaüstü · çalışıyor — implementer/);
  assert.match(text, /Son 12 saatte kapanan: shop \(bitti\)/);
  assert.match(text, /Telegram'dan başlatılmış açık iş yok\./);
  assert.match(text, /Claude penceresinden cevaplanır/);
  assert.doesNotMatch(text, /Kayıtlı orkestra işi yok/);
});

test("formatStatus lists open Telegram jobs and reports failures instead of 'none'", () => {
  const jobs = [{ id: JOB, project: "demo", status: "awaiting_user", phase: "clarify" }, { id: "fff000fff000", project: "x", status: "done" }];
  const text = formatStatus({ runs: [], bridgeJobs: jobs });
  assert.match(text, /Açık run yok\./);
  assert.match(text, new RegExp(`Telegram'dan başlatılan açık işler \\(1\\):\\n• ${JOB} · demo · ⏸ Yanıtını bekliyor · clarify`));
  const failed = formatStatus({ runs: null, runsError: "boom", bridgeJobs: null, bridgeError: "bridge not reachable" });
  assert.match(failed, /⚠️ Run listesi alınamadı: boom/);
  assert.match(failed, /⚠️ Telegram işleri alınamadı: bridge not reachable/);
  assert.doesNotMatch(failed, /yok\./);
});

test("findOpenRun blocks only projects with an open run", () => {
  const runs = [...DESKTOP_RUNS, { project: "/Users/u/Projects/game-app", project_name: "game-app", status: "paused", title: "NX" }];
  assert.equal(findOpenRun(runs, "mobile-app")?.title, "Önerilen geliştirmelerin tamamı");
  assert.equal(findOpenRun(runs, "game-app")?.title, "NX");
  assert.equal(findOpenRun(runs, "shop"), null, "finished runs do not block");
  assert.equal(findOpenRun(runs, "ledger"), null, "exact folder names only");
  assert.equal(findOpenRun(null, "x"), null);
  const msg = formatBusyProject("mobile-app", DESKTOP_RUNS[1]);
  assert.match(msg, /^⛔ Başlatmadım: \[mobile-app\] projesinde zaten açık bir orkestra run var — "Önerilen geliştirmelerin tamamı"/);
  assert.match(msg, /uygulama · görev 4\/19 · masaüstü · çalışıyor — implementer/);
});

test("parseSentMessageIds is shape-agnostic", () => {
  assert.deepEqual(parseSentMessageIds('{"ok":true,"result":{"messageId":"123"}}'), ["123"]);
  assert.deepEqual(parseSentMessageIds('{"deliveries":[{"message_id":5},{"message_id":6}]}'), ["5", "6"]);
  assert.deepEqual(parseSentMessageIds("not json"), []);
});

test("resolveProject: exact, folded, ambiguous and unknown names", async () => {
  const { resolveProject, projectKey } = await import("./logic.js");
  const list = [{ name: "namaz-vakitleri" }, { name: "pulse-race" }, { name: "pulse-arena" }, "docs"];
  assert.deepEqual(resolveProject("pulse-race", list), { project: "pulse-race" });
  assert.deepEqual(resolveProject("Namaz Vakitleri", list), { project: "namaz-vakitleri" });
  assert.deepEqual(resolveProject("NAMAZ_VAKİTLERİ", list), { project: "namaz-vakitleri" });
  assert.deepEqual(resolveProject("pulse", list), { suggestions: ["pulse-race", "pulse-arena"] });
  assert.deepEqual(resolveProject("nothing-like-it", list), { suggestions: [] });
  assert.deepEqual(resolveProject("  ", list), { suggestions: [] });
  assert.equal(projectKey("Çalışma Alanı-2"), "calismaalani2");
});
