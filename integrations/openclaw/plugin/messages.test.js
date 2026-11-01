// integrations/openclaw/plugin/messages.test.js — node --test messages.test.js
// The EN and TR tables must stay twins: same keys, same {fields}, English without Turkish letters, no dangling or dead
// keys, and a `t()` that never throws.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { MESSAGES, placeholders, t } from "./messages.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TURKISH_LETTERS = /[çğıöşüÇĞİÖŞÜ]/;
const EM_DASH = "—";
// Legacy Turkish texts that still carry the em dash (kept byte-identical: existing tests and users match them).
const LEGACY_EM_DASH = new Set(["busy.header", "status.run_head"]);

test("both languages have the same keys and the same {fields}", () => {
  const en = Object.keys(MESSAGES.en).sort();
  const tr = Object.keys(MESSAGES.tr).sort();
  assert.deepEqual(en, tr);
  for (const key of en) {
    assert.deepEqual(placeholders(MESSAGES.en[key]), placeholders(MESSAGES.tr[key]), `${key}: {fields} differ`);
    for (const lang of ["en", "tr"]) {
      assert.equal(typeof MESSAGES[lang][key], "string", `${lang}.${key}`);
      assert.notEqual(MESSAGES[lang][key].trim(), "", `${lang}.${key} is empty`);
    }
  }
});

test("English has no Turkish letters; new text has no em dash", () => {
  for (const [key, text] of Object.entries(MESSAGES.en)) {
    assert.doesNotMatch(text, TURKISH_LETTERS, `en.${key}`);
    assert.ok(!text.includes(EM_DASH), `en.${key} has an em dash`);
  }
  for (const [key, text] of Object.entries(MESSAGES.tr)) {
    if (!LEGACY_EM_DASH.has(key)) {
      assert.ok(!text.includes(EM_DASH), `tr.${key} has an em dash`);
    }
  }
});

test("Turkish is translated, not a copy of the English text", () => {
  for (const key of Object.keys(MESSAGES.en)) {
    const letters = MESSAGES.en[key].replace(/\{\w+\}|[^A-Za-z]/g, "");
    if (letters.length > 12) {
      assert.notEqual(MESSAGES.en[key], MESSAGES.tr[key], key);
    }
  }
});

test("t() picks the language, falls back to English and then the key, and never throws", () => {
  assert.equal(t("job_ref", { id: "abc" }, "en"), "job abc");
  assert.equal(t("job_ref", { id: "abc" }, "tr"), "iş abc");
  assert.equal(t("job_ref", { id: "abc" }, "de"), "job abc");
  assert.equal(t("no.such.key", {}, "tr"), "no.such.key");
  assert.equal(t("job_ref", {}, "en"), "job {id}");
  assert.equal(t("job_ref", { other: 1 }, "en"), "job {id}");
  assert.equal(t("job_ref", { id: "$&" }, "en"), "job $&");
  assert.equal(t("job_ref", { id: 0 }, "en"), "job 0");
});

test("t() uses the resolved language when none is given", () => {
  const saved = process.env.ORKESTRA_LANG;
  try {
    process.env.ORKESTRA_LANG = "tr";
    assert.equal(t("job_ref", { id: "x" }), "iş x");
    process.env.ORKESTRA_LANG = "en";
    assert.equal(t("job_ref", { id: "x" }), "job x");
  } finally {
    if (saved === undefined) delete process.env.ORKESTRA_LANG;
    else process.env.ORKESTRA_LANG = saved;
  }
});

test("every key the code uses exists, and every key is used", () => {
  const source = ["index.js", "logic.js"].map((f) => fs.readFileSync(path.join(HERE, f), "utf8")).join("\n");
  const used = new Set([...source.matchAll(/\bt\("([\w.]+)"/g)].map((m) => m[1]));
  // statusLabel() builds `label.<status>` for the statuses it knows
  for (const status of ["running", "queued", "awaiting_user", "done", "blocked", "error", "cancelled", "paused"]) {
    used.add(`label.${status}`);
  }
  // formatRevive() builds `revive.state.<state>` for the states orkestra_client.py revive reports
  for (const state of ["running", "started", "failed", "missing", "invalid", "unsupported"]) {
    used.add(`revive.state.${state}`);
  }
  const known = new Set(Object.keys(MESSAGES.en));
  assert.deepEqual([...used].filter((k) => !known.has(k)).sort(), [], "keys used but missing from the tables");
  assert.deepEqual([...known].filter((k) => !used.has(k)).sort(), [], "keys in the tables that nothing uses");
});
