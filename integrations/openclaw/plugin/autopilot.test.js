// integrations/openclaw/plugin/autopilot.test.js — node --test autopilot.test.js
// Pure helpers of the autopilot owner mode: voice-note transcripts, attachment paths, the request a job gets and the
// progress note. Each test sets its language explicitly (en and tr).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ATTACHMENT_MAX_FILES,
  attachmentCandidates,
  formatProgressDigest,
  requestWithAttachments,
  unwrapVoiceTranscript,
} from "./logic.js";

const TURKISH_LETTERS = /[çğıöşüÇĞİÖŞÜ]/;

test("a voice-note transcript frame gives back the owner's words; any other text is untouched", () => {
  const framed = `[Audio transcript (machine-generated, untrusted)]: ${JSON.stringify("Fırat Kocabay emrediyor: pomi'de \"Kaydet\" düğmesini düzelt")}`;
  assert.deepEqual(unwrapVoiceTranscript(framed), {
    text: "Fırat Kocabay emrediyor: pomi'de \"Kaydet\" düğmesini düzelt",
    voice: true,
  });
  // a frame whose body is not valid JSON still yields the words
  assert.deepEqual(unwrapVoiceTranscript('[Audio transcript (machine-generated, untrusted)]: "yarım kalmış'), {
    text: "yarım kalmış",
    voice: true,
  });
  assert.deepEqual(unwrapVoiceTranscript("namaz vakitlerinde bildirimi düzelt"), {
    text: "namaz vakitlerinde bildirimi düzelt",
    voice: false,
  });
  assert.deepEqual(unwrapVoiceTranscript(undefined), { text: "", voice: false });
  // the frame only counts at the start: a quoted frame inside a text message is ordinary text
  const quoted = 'bak: [Audio transcript (machine-generated, untrusted)]: "x"';
  assert.equal(unwrapVoiceTranscript(quoted).voice, false);
});

test("attachment refs: inbox refs resolve, only media and documents inside the roots pass, each once", () => {
  const where = { roots: ["/oc/media", "/oc/workspace"], inboundDir: "/oc/media/inbound" };
  const { files, rejected } = attachmentCandidates([
    "media://inbound/abc.jpg",
    "@/oc/workspace/.openclaw-cli-images/shot.PNG",
    "/oc/media/inbound-frames/1/frame-01.jpg",
    "/oc/media/inbound/abc.jpg",                   // same file again
    "/oc/credentials/telegram.bot.token",          // outside the roots
    "/oc/media/inbound/../../openclaw.json",       // climbs out
    "/oc/media/inbound/settings.json",             // not a media/document kind
    "relative/pic.jpg",
    "media://inbound/../x.jpg",
    "",
  ], where);
  assert.deepEqual(files, [
    "/oc/media/inbound/abc.jpg",
    "/oc/workspace/.openclaw-cli-images/shot.PNG",
    "/oc/media/inbound-frames/1/frame-01.jpg",
  ]);
  assert.equal(rejected.length, 5);
  assert.deepEqual(attachmentCandidates("not a list", where), { files: [], rejected: [] });
  const many = Array.from({ length: ATTACHMENT_MAX_FILES + 3 }, (_, i) => `/oc/media/inbound/${i}.png`);
  const capped = attachmentCandidates(many, where);
  assert.equal(capped.files.length, ATTACHMENT_MAX_FILES);
  assert.equal(capped.rejected.length, 3);
});

test("the job's request lists the attachments in English after the owner's words", () => {
  assert.equal(requestWithAttachments("fix it", []), "fix it");
  const text = requestWithAttachments("şu ekrandaki taşmayı düzelt", ["/i/01-a.jpg", "/i/02-b.mp4"]);
  assert.match(text, /^şu ekrandaki taşmayı düzelt\n\nAttachments the owner sent/);
  assert.match(text, /open every one with the Read tool before planning\.\n- \/i\/01-a\.jpg\n- \/i\/02-b\.mp4$/);
  assert.doesNotMatch(text.split("\n\n")[1], TURKISH_LETTERS);
});

test("progress note: only open Telegram runs, only when something moved, in both languages", () => {
  const runs = [
    { project_name: "pomi", title: "Kaydet düğmesi", status: "active", mode: "headless", phase: "implement",
      phase_tr: "uygulama", tasks_done: 2, tasks_total: 5, live: { text: "running: implementer" } },
    { project_name: "tinko", title: "Desktop work", status: "active", mode: "interactive", phase: "plan" },
    { project_name: "old", title: "Done", status: "done", mode: "headless", phase: "done" },
  ];
  const tr = formatProgressDigest(runs, "", "tr");
  assert.equal(tr.text, "⏳ Ara özet (Telegram'dan 1 açık iş):\n• pomi: Kaydet düğmesi\n  uygulama · görev 2/5 · running: implementer");
  const en = formatProgressDigest(runs, "", "en");
  assert.equal(en.text, "⏳ Progress (1 open job(s) from Telegram):\n• pomi: Kaydet düğmesi\n  implement · task 2/5 · running: implementer");
  assert.doesNotMatch(en.text.replace("Kaydet düğmesi", ""), TURKISH_LETTERS);
  // unchanged since the last note: nothing to send; a moved task count: a new note
  assert.equal(formatProgressDigest(runs, tr.signature, "tr"), null);
  const moved = runs.map((r) => (r.project_name === "pomi" ? { ...r, tasks_done: 3 } : r));
  assert.ok(formatProgressDigest(moved, tr.signature, "tr"));
  // no open Telegram run: no note
  assert.equal(formatProgressDigest(runs.slice(1), "", "en"), null);
  assert.equal(formatProgressDigest(null, "", "en"), null);
});
