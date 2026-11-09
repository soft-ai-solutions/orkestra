// integrations/openclaw/plugin/logic.i18n.test.js — node --test logic.i18n.test.js
// The pure helpers in both languages. Every call names its language, so the result never depends on the machine; a
// run started in one language must still be answered in the other.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  expandOptionReply, fold, formatBusyProject, formatNotification, formatStatus, parseJobRef, statusLabel,
} from "./logic.js";

const JOB = "0123456789ab";
const TURKISH_LETTERS = /[çğıöşüÇĞİÖŞÜ]/;

test("parseJobRef accepts the English and the Turkish bridge header and start confirmation", () => {
  const want = { project: "shop", jobId: JOB };
  assert.deepEqual(parseJobRef(`[shop] ⏸ Waiting for your reply · clarify · job ${JOB}\n\nQuestion?`), want);
  assert.deepEqual(parseJobRef(`[shop] ⏸ Yanıtını bekliyor · clarify · iş ${JOB}\n\nSoru?`), want);
  assert.deepEqual(parseJobRef(`▶️ Started: [shop] job ${JOB}\nI will write here…`), want);
  assert.deepEqual(parseJobRef(`▶️ Başladım: [shop] iş ${JOB}\nSoru ya da onay…`), want);
  assert.deepEqual(parseJobRef(`[shop] ✅ Done · job ${JOB} · cursor`), want);
  assert.equal(parseJobRef("An ordinary answer, no job here."), null);
  assert.equal(parseJobRef(`[x] job 0123`), null);
  assert.equal(parseJobRef(`[x] jobs ${JOB}`), null, "the word must stand alone");
});

test("formatNotification: same header as the bridge in each language, and parseJobRef reads both", () => {
  const job = { id: JOB, project: "demo", status: "awaiting_user", phase: "clarify", last_message: "Which one?" };
  const en = formatNotification(job, "en");
  assert.match(en, new RegExp(`^\\[demo\\] ⏸ Waiting for your reply · clarify · job ${JOB}\\n\\nWhich one\\?`));
  assert.ok(en.includes("Reply"));
  assert.doesNotMatch(en, TURKISH_LETTERS);
  const tr = formatNotification(job, "tr");
  assert.match(tr, new RegExp(`^\\[demo\\] ⏸ Yanıtını bekliyor · clarify · iş ${JOB}\\n\\nWhich one\\?`));
  assert.ok(tr.includes("Yanıtla"));
  for (const text of [en, tr]) {
    assert.deepEqual(parseJobRef(text), { project: "demo", jobId: JOB });
  }
  const done = { id: JOB, project: "demo", status: "done", phase: null, last_message: "" };
  assert.equal(formatNotification(done, "en"), `[demo] ✅ Done · job ${JOB}`);
  assert.equal(formatNotification(done, "tr"), `[demo] ✅ Tamamlandı · iş ${JOB}`);
  assert.equal(formatNotification({ ...done, status: "weird" }, "en"), `[demo] weird · job ${JOB}`);
});

test("statusLabel covers every job status in both languages", () => {
  for (const status of ["running", "queued", "awaiting_user", "done", "blocked", "error", "cancelled", "paused"]) {
    assert.notEqual(statusLabel(status, "en"), status);
    assert.notEqual(statusLabel(status, "tr"), status);
    assert.doesNotMatch(statusLabel(status, "en"), TURKISH_LETTERS);
  }
  assert.equal(statusLabel("stalled", "en"), "stalled");
});

test("the approve option is forwarded as the approval word of its own language", () => {
  const en = "Plan ready.\na) Approve and start implementing\nb) I want changes\nc) Cancel";
  assert.equal(expandOptionReply("a", en), "approve");
  assert.equal(expandOptionReply("b", en), "b) I want changes");
  assert.equal(expandOptionReply("C)", en), "c) Cancel");
  const tr = "Plan hazır.\na) Onayla — uygulamaya başla\nb) Değişiklik istiyorum\nc) İptal et";
  assert.equal(expandOptionReply("a", tr), "onayla");
  assert.equal(expandOptionReply("b", tr), "b) Değişiklik istiyorum");
  assert.equal(expandOptionReply("c", tr), "c) İptal et");
});

test("approval phrases of both languages are recognised, hedged ones stay verbatim", () => {
  for (const [option, want] of [
    ["Approve and start implementing", "approve"], ["Approve", "approve"], ["I approve", "approve"], ["Yes, go ahead", "approve"],
    ["Go ahead", "approve"], ["Proceed", "approve"], ["LGTM", "approve"], ["APPROVED", "approve"],
    ["Onayla — uygulamaya başla", "onayla"], ["Onay", "onayla"], ["Evet, başla", "onayla"], ["Uygula", "onayla"],
    ["Kabul", "onayla"], ["ONAYLA", "onayla"], ["Başla", "onayla"], ["BAŞLA", "onayla"],
  ]) {
    assert.equal(expandOptionReply("a", `a) ${option}\nb) other`), want, option);
  }
  for (const option of [
    "Approve but add tests", "Approve, however skip the docs", "Approve and fix the typo", "Yes?", "Approve (wait for CI)",
    "Do not approve", "Reject", "Onayla ama testleri de ekle", "Onaylıyor muyum?", "Onayla, sonra bekle", "Hayır", "Vazgeç", "Değiştir",
    "Apply (orkestra changes only)", "Apply including other diffs (--all)", "Never mind", "Medium", "Create a branch",
  ]) {
    assert.equal(expandOptionReply("a", `a) ${option}\nb) other`), `a) ${option}`, option);
  }
});

test("an approval that carries a condition, a change or an exception stays verbatim, in both languages", () => {
  // orkestra's own reading of the typed text (change request) must see these: the bare word would drop the condition
  for (const option of [
    "Approve and skip T002", "Approve, rename the module first", "Yes, drop the tests", "Approve and replace postgres with sqlite",
    "Approve and run the linter", "Approve and use postgres", "Approve and start implementing without the tests",
    "Approve only T001", "Approve and notify the team", "Approve (skip T002)", "Yes, but only the backend", "Approve T001 first",
    "Onayla ve T002 yi atla", "Onayla, modulu yeniden adlandir", "Evet, testleri sil", "Onayla sadece T001", "Onayla ve postgres kullan",
    "Onayla (T002 haric)", "Uygula ve T002'yi atla",
  ]) {
    assert.equal(expandOptionReply("a", `a) ${option}\nb) other`), `a) ${option}`, option);
  }
});

test("a plain approval with a harmless note or closer is still the approval word", () => {
  for (const [option, want] of [
    ["Approve and start implementing (Recommended)", "approve"], ["Approve and start implementing, thanks", "approve"],
    ["Yes, go ahead and start", "approve"], ["Approve the plan", "approve"], ["Approve it now, please", "approve"],
    ["Onayla — uygulamaya başla (Önerilen)", "onayla"], ["Evet, şimdi başla", "onayla"], ["Onayla, teşekkürler", "onayla"],
  ]) {
    assert.equal(expandOptionReply("a", `a) ${option}\nb) other`), want, option);
  }
});

test("a Turkish rollback option keeps its legacy behaviour (never-shrink vocabulary)", () => {
  // "uygula" was an approval word before the English table existed; the rollback wording shares it.
  assert.equal(expandOptionReply("a", "a) Uygula (yalnız orkestra değişiklikleri)\nb) Vazgeç"), "onayla");
});

test("fold lower-cases and strips Turkish letters and accents, nothing else", () => {
  assert.equal(fold("İPTAL Şimdi Çık ğüö ı"), "iptal simdi cik guo i");
  assert.equal(fold("Onayla — uygulamaya başla"), "onayla — uygulamaya basla");
  assert.equal(fold("café"), "cafe");
  assert.equal(fold(undefined), "");
});

test("expandOptionReply leaves everything else as typed, in both languages", () => {
  const en = "a) Approve\nb) Change it";
  assert.equal(expandOptionReply("approve", en), "approve");
  assert.equal(expandOptionReply("  I pick b  ", en), "I pick b");
  assert.equal(expandOptionReply("d", en), "d");
  assert.equal(expandOptionReply("a", undefined), "a");
});

const RUN = {
  project_name: "mobile-app", title: "Add dark mode", phase: "implement", phase_tr: "uygulama", status: "active",
  status_tr: "aktif", mode: "interactive", tasks_done: 4, tasks_total: 19, live: { level: "run", text: "running: implementer" },
};
const FINISHED = { project_name: "shop", title: "Old", phase: "done", phase_tr: "bitti", status: "done", status_tr: "bitti" };

test("formatBusyProject in English and Turkish", () => {
  const en = formatBusyProject("mobile-app", RUN, "en");
  assert.match(en, /^⛔ Not started: project \[mobile-app\] already has an open Orkestra run: "Add dark mode"\n/);
  assert.match(en, /\nimplement · task 4\/19 · desktop · running: implementer\n/);
  assert.match(en, /\(\/runs\)/);
  assert.doesNotMatch(en, TURKISH_LETTERS);
  const tr = formatBusyProject("mobile-app", RUN, "tr");
  assert.match(tr, /^⛔ Başlatmadım: \[mobile-app\] projesinde zaten açık bir orkestra run var: "Add dark mode"\n/);
  assert.match(tr, /\nuygulama · görev 4\/19 · masaüstü · running: implementer\n/);
  const headless = formatBusyProject("x", { ...RUN, mode: "headless", live: null }, "en");
  assert.match(headless, /implement · task 4\/19 · Telegram \/run · active\n/);
});

test("formatStatus in English and Turkish", () => {
  const input = {
    runs: [RUN, FINISHED],
    bridgeJobs: [{ id: JOB, project: "demo", status: "awaiting_user", phase: "clarify" }, { id: "fff000fff000", project: "x", status: "done" }],
  };
  const en = formatStatus(input, "en");
  assert.equal(en.split("\n")[0], "🎼 Orkestra status");
  assert.match(en, /Open runs \(1\):\n• mobile-app: Add dark mode\n {2}implement · task 4\/19 · desktop · running: implementer/);
  assert.match(en, /Closed in the last 12 hours: shop \(done\)/);
  assert.match(en, new RegExp(`Open jobs started from Telegram \\(1\\):\\n• ${JOB} · demo · ⏸ Waiting for your reply · clarify`));
  assert.match(en, /Use Reply on a job's notification message to answer it\./);
  assert.match(en, /Runs started on the desktop are monitored here/);
  assert.doesNotMatch(en, TURKISH_LETTERS);
  const tr = formatStatus(input, "tr");
  assert.match(tr, /Açık run'lar \(1\):\n• mobile-app: Add dark mode\n {2}uygulama · görev 4\/19 · masaüstü · running: implementer/);
  assert.match(tr, /Son 12 saatte kapanan: shop \(bitti\)/);
});

test("formatStatus reports failures and empty states in English", () => {
  const empty = formatStatus({ runs: [], bridgeJobs: [] }, "en");
  assert.match(empty, /No open runs\./);
  assert.match(empty, /No open job started from Telegram\./);
  const failed = formatStatus({ runs: null, runsError: "boom", bridgeJobs: null, bridgeError: "bridge not reachable" }, "en");
  assert.match(failed, /⚠️ Could not read the run list: boom/);
  assert.match(failed, /⚠️ Could not read the Telegram jobs: bridge not reachable/);
  assert.doesNotMatch(failed, /No open/);
});

test("an explicit language beats the environment, and the environment is the default", () => {
  const saved = process.env.ORKESTRA_LANG;
  const job = { id: JOB, project: "demo", status: "done", phase: null, last_message: "" };
  try {
    process.env.ORKESTRA_LANG = "tr";
    assert.equal(formatNotification(job), `[demo] ✅ Tamamlandı · iş ${JOB}`);
    assert.equal(formatNotification(job, "en"), `[demo] ✅ Done · job ${JOB}`);
    process.env.ORKESTRA_LANG = "en";
    assert.equal(formatNotification(job), `[demo] ✅ Done · job ${JOB}`);
    assert.match(formatBusyProject("p", RUN), /^⛔ Not started/);
    assert.match(formatBusyProject("p", RUN), /\nimplement · /);
    process.env.ORKESTRA_LANG = "tr";
    assert.match(formatBusyProject("p", RUN), /\nuygulama · /);
  } finally {
    if (saved === undefined) delete process.env.ORKESTRA_LANG;
    else process.env.ORKESTRA_LANG = saved;
  }
});
