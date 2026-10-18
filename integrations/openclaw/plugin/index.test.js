// integrations/openclaw/plugin/index.test.js — node --test index.test.js
// The real index.js, hermetic: the OpenClaw SDK import is replaced by a stub package inside a temp copy of the plugin
// and every command runs against a scripted runner (no bridge, no Python, no OpenClaw, no network). The language is
// switched per call through ORKESTRA_LANG, which the plugin resolves at call time. The full flow against a real
// bridge is plugin.e2e.test.js (needs an OpenClaw install).
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TURKISH_LETTERS = /[çğıöşüÇĞİÖŞÜ]/;
const JOB = "0123456789ab";
const CHAT = "42";
const CLIENT = "/fake/bridge/orkestra_client.py";
const CLI = "/fake/plugins/orkestra/bin/orkestra";

let tmp;
let plugin;
let savedLang;
let savedHome;

before(async () => {
  savedLang = process.env.ORKESTRA_LANG;
  savedHome = process.env.ORKESTRA_HOME;
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "orkestra-index-test-"));
  process.env.ORKESTRA_HOME = path.join(tmp, "orkestra-home"); // never the real one
  const dir = path.join(tmp, "plugin");
  const sdk = path.join(dir, "node_modules", "openclaw");
  fs.mkdirSync(path.join(sdk, "plugin-sdk"), { recursive: true });
  fs.writeFileSync(
    path.join(sdk, "package.json"),
    JSON.stringify({ name: "openclaw", version: "0.0.0", type: "module", exports: { "./plugin-sdk/plugin-entry": "./plugin-sdk/plugin-entry.js" } }),
  );
  fs.writeFileSync(path.join(sdk, "plugin-sdk", "plugin-entry.js"), "export const definePluginEntry = (entry) => entry;\n");
  for (const f of ["index.js", "logic.js", "client.js", "lang.js", "messages.js", "package.json"]) {
    fs.copyFileSync(path.join(HERE, f), path.join(dir, f));
  }
  plugin = (await import(path.join(dir, "index.js"))).default;
});

after(() => {
  for (const [name, value] of [["ORKESTRA_LANG", savedLang], ["ORKESTRA_HOME", savedHome]]) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  fs.rmSync(tmp, { recursive: true, force: true });
});

/** A plugin API whose runner answers the bridge client and `orkestra watch` from `script`. */
function harness(script = {}, { config = {}, failRegister = [] } = {}) {
  const calls = [];
  const commands = {};
  const hooks = {};
  const warnings = [];
  const runner = (argv) => {
    calls.push(argv);
    const reply = (code, body) => Promise.resolve({ code, stdout: typeof body === "string" ? body : JSON.stringify(body), stderr: "" });
    if (argv[1] === CLI) {
      return script.watch ? script.watch(argv) : reply(0, []);
    }
    const verb = argv[2];
    const handler = script[verb];
    return handler ? handler(argv) : reply(0, {});
  };
  const api = {
    pluginConfig: { chatId: CHAT, clientPath: CLIENT, orkestraCliPath: CLI, python: "py", ...config },
    logger: { warn: (m) => warnings.push(String(m)), info() {}, error() {}, debug() {} },
    runtime: { system: { runCommandWithTimeout: runner } },
    registerCommand: (def) => {
      if (failRegister.includes(def.name)) {
        throw new Error(`Command name "${def.name}" is reserved by a built-in command`);
      }
      commands[def.name] = def;
    },
    on: (name, fn) => {
      hooks[name] = fn;
    },
    registerService: () => {},
  };
  plugin.register(api);
  return { commands, hooks, calls, warnings };
}

const ok = (body) => Promise.resolve({ code: 0, stdout: JSON.stringify(body), stderr: "" });
const bad = (error) => Promise.resolve({ code: 1, stdout: JSON.stringify({ error }), stderr: "" });
const lang = (code) => {
  process.env.ORKESTRA_LANG = code;
};

test("every command exists under its English and its Turkish name, with the same rules", () => {
  lang("en");
  const { commands } = harness();
  assert.deepEqual(Object.keys(commands).sort(), ["cancel", "durum", "iptal", "run", "runs", "yap"]);
  for (const def of Object.values(commands)) {
    assert.equal(def.requireAuth, true);
    assert.deepEqual(def.channels, ["telegram"]);
  }
  assert.equal(commands.run.acceptsArgs, true);
  assert.equal(commands.yap.acceptsArgs, true);
  // the prompt guidance is attached once, to the original name, and names both vocabularies
  assert.equal(commands.yap.agentPromptGuidance.length, 1);
  assert.equal(commands.run.agentPromptGuidance, undefined);
  assert.match(commands.yap.agentPromptGuidance[0].text, /\/run \(or \/yap\)/);
  assert.match(commands.yap.agentPromptGuidance[0].text, /\/cancel \(or \/iptal\)/);
  assert.doesNotMatch(commands.yap.agentPromptGuidance[0].text, TURKISH_LETTERS);
  for (const def of Object.values(commands)) {
    assert.equal(typeof def.handler, "function");
  }
});

test("command descriptions follow the language at registration", () => {
  lang("en");
  const en = harness().commands;
  assert.match(en.run.description, /^Start an Orkestra job: \/run/);
  assert.doesNotMatch(Object.values(en).map((d) => d.description).join("\n"), TURKISH_LETTERS);
  lang("tr");
  const tr = harness().commands;
  assert.match(tr.yap.description, /^Orkestra işi başlat: \/yap/);
  assert.match(tr.durum.description, /tüm açık run'lar/);
});

test("a name the host reserves is skipped with a warning; the rest still registers", () => {
  lang("en");
  const { commands, warnings } = harness({}, { failRegister: ["runs"] });
  assert.deepEqual(Object.keys(commands).sort(), ["cancel", "durum", "iptal", "run", "yap"]);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /\/runs not registered/);
});

test("without a numeric chatId the plugin stays inactive", () => {
  lang("en");
  const { commands, hooks, warnings } = harness({}, { config: { chatId: "not-a-number" } });
  assert.deepEqual(commands, {});
  assert.deepEqual(hooks, {});
  assert.match(warnings[0], /chatId missing; plugin inactive/);
});

test("/run in English: usage, engine prefix, start confirmation that routing can parse back", async () => {
  lang("en");
  const started = [];
  const { commands } = harness({
    start: (argv) => {
      started.push(argv);
      return ok({ id: JOB, project: "shop", engine: argv.includes("--engine") ? argv[argv.indexOf("--engine") + 1] : "claude" });
    },
  });
  for (const args of ["", "shop", "../etc do it", "copilot: onlyproject"]) {
    const text = (await commands.run.handler({ args })).text;
    assert.match(text, /^Usage: \/run \[claude:\|cursor:\|copilot:\|pi:\] <project> <request>\nExample: \/run copilot: shop/, args);
  }
  const res = (await commands.run.handler({ args: "copilot: shop add CSV export" })).text;
  assert.equal(res.split("\n")[0], `▶️ Started: [shop] job ${JOB} · copilot`);
  assert.match(res, /autopilot/);
  assert.doesNotMatch(res, TURKISH_LETTERS);
  assert.deepEqual(started[0].slice(2), ["start", "shop", "add CSV export", "--engine", "copilot", "--chat-id", CHAT, "--source", "openclaw"]);
  const claude = (await commands.run.handler({ args: "shop add CSV export" })).text;
  assert.equal(claude.split("\n")[0], `▶️ Started: [shop] job ${JOB}`);
  // the plugin's own confirmation is routable in a later reply, in either language
  const { parseJobRef } = await import("./logic.js");
  assert.deepEqual(parseJobRef(res), { project: "shop", jobId: JOB });
});

test("/yap in Turkish: same command, Turkish texts", async () => {
  lang("tr");
  const { commands } = harness({ start: () => ok({ id: JOB, project: "shop", engine: "cursor" }) });
  assert.match((await commands.yap.handler({ args: "" })).text, /^Kullanım: \/yap \[claude:\|cursor:\|copilot:\|pi:\] <proje> <istek>/);
  const res = (await commands.yap.handler({ args: "cursor: shop sipariş listesine CSV ekle" })).text;
  assert.equal(res.split("\n")[0], `▶️ Başladım: [shop] iş ${JOB} · cursor`);
  assert.match(res, /Varsayılan autopilot/);
});

test("both names are the same command whatever the language", async () => {
  const { commands } = harness({ start: () => ok({ id: JOB, project: "shop", engine: "claude" }) });
  lang("en");
  const viaYap = (await commands.yap.handler({ args: "shop add something" })).text;
  const viaRun = (await commands.run.handler({ args: "shop add something" })).text;
  assert.equal(viaYap, viaRun);
});

test("/run never takes over a project whose run is open, and fails closed when it cannot tell", async () => {
  const open = [{ project_name: "shop", title: "Refactor", phase: "implement", phase_tr: "uygulama", status: "active", status_tr: "aktif", mode: "interactive" }];
  let started = 0;
  const script = {
    start: () => {
      started += 1;
      return ok({ id: JOB, project: "shop" });
    },
    watch: () => ok(open),
  };
  lang("en");
  const { commands } = harness(script);
  const busy = (await commands.run.handler({ args: "shop add something" })).text;
  assert.match(busy, /^⛔ Not started: project \[shop\] already has an open Orkestra run: "Refactor"/);
  assert.match(busy, /implement · desktop · active/);
  assert.equal(started, 0);
  lang("tr");
  assert.match((await commands.yap.handler({ args: "shop bir şey ekle" })).text, /^⛔ Başlatmadım: \[shop\] projesinde zaten açık/);
  assert.equal(started, 0);

  const broken = harness({ watch: () => Promise.resolve({ code: 2, stdout: "", stderr: "orkestra: boom" }) }).commands;
  lang("en");
  assert.match((await broken.run.handler({ args: "shop add something" })).text, /^⚠️ Not started: could not check for open runs \(orkestra: boom\)\. Waiting, to be safe/);
  lang("tr");
  assert.match((await broken.yap.handler({ args: "shop bir şey" })).text, /^⚠️ Başlatmadım: açık run kontrolü yapılamadı \(orkestra: boom\)/);
});

test("/run reports a bridge failure in the reader's language", async () => {
  const { commands } = harness({ start: () => bad("bridge not reachable (refused)") });
  lang("en");
  assert.equal((await commands.run.handler({ args: "shop add something" })).text, "⚠️ Could not start: bridge not reachable (refused)");
  lang("tr");
  assert.equal((await commands.yap.handler({ args: "shop bir şey" })).text, "⚠️ Başlatılamadı: bridge not reachable (refused)");
});

test("/runs and /durum show open runs and Telegram jobs", async () => {
  const runs = [{ project_name: "deals", title: "Fix", phase: "clarify", phase_tr: "netleştirme", status: "active", status_tr: "aktif", mode: "interactive", live: { text: "waiting for you" } }];
  const { commands } = harness({
    watch: () => ok(runs),
    status: () => ok({ jobs: [{ id: JOB, project: "shop", status: "awaiting_user", phase: "plan" }] }),
  });
  lang("en");
  const en = (await commands.runs.handler({})).text;
  assert.match(en, /^🎼 Orkestra status\nOpen runs \(1\):\n• deals: Fix\n {2}clarify · desktop · waiting for you/);
  assert.match(en, new RegExp(`Open jobs started from Telegram \\(1\\):\\n• ${JOB} · shop · ⏸ Waiting for your reply · plan`));
  assert.doesNotMatch(en, TURKISH_LETTERS);
  lang("tr");
  const tr = (await commands.durum.handler({})).text;
  assert.match(tr, /^🎼 Orkestra durumu\nAçık run'lar \(1\):\n• deals — Fix\n {2}netleştirme · masaüstü · waiting for you/);
  assert.equal((await commands.runs.handler({})).text, tr, "the alias answers in the same language");
});

test("/cancel and /iptal", async () => {
  lang("en");
  let h = harness({ cancel: () => ok({ message: "Cancelled: abc" }) });
  assert.equal((await h.commands.cancel.handler({})).text, "Cancelled: abc");
  h = harness({ cancel: () => ok({}) });
  assert.equal((await h.commands.cancel.handler({})).text, "Cancel request sent.");
  h = harness({ cancel: () => bad("bridge not reachable") });
  assert.equal((await h.commands.cancel.handler({})).text, "⚠️ Could not cancel: bridge not reachable");
  lang("tr");
  assert.equal((await h.commands.iptal.handler({})).text, "⚠️ İptal edilemedi: bridge not reachable");
  h = harness({ cancel: () => ok({}) });
  assert.equal((await h.commands.iptal.handler({})).text, "İptal isteği gönderildi.");
});

const dispatch = (hooks, content, quoted, extra = {}) =>
  hooks.before_dispatch({ channel: "telegram", senderId: CHAT, content, replyToBody: quoted, ...extra }, {});

test("a Telegram reply to an English notification answers that job; the approve option becomes \"approve\"", async () => {
  const replies = [];
  const { hooks } = harness({ reply: (argv) => (replies.push(argv.slice(3)), ok({})) });
  lang("en");
  const quoted = `[shop] ⏸ Waiting for your reply · plan · job ${JOB}\n\nPlan ready.\na) Approve and start implementing\nb) I want changes\nc) Cancel`;
  let res = await dispatch(hooks, "a", quoted);
  assert.deepEqual(replies[0], [JOB, "approve"]);
  assert.deepEqual(res, { handled: true, text: `↪️ [shop] job ${JOB} → approve` });
  res = await dispatch(hooks, "b", quoted);
  assert.deepEqual(replies[1], [JOB, "b) I want changes"]);
  assert.equal(res.text, `↪️ [shop] job ${JOB} → b) I want changes`);
  res = await dispatch(hooks, "  ", quoted);
  assert.deepEqual(res, { handled: true, text: "⚠️ An empty reply was not forwarded to Orkestra." });
  assert.equal(replies.length, 2);
});

test("a Telegram reply to a Turkish notification answers that job, whatever language the plugin speaks", async () => {
  const replies = [];
  const { hooks } = harness({ reply: (argv) => (replies.push(argv.slice(3)), ok({})) });
  const quoted = `[shop] ⏸ Yanıtını bekliyor · plan · iş ${JOB}\n\nPlan hazır.\na) Onayla — uygulamaya başla\nb) Değişiklik istiyorum`;
  lang("tr");
  let res = await dispatch(hooks, "a", quoted);
  assert.deepEqual(replies[0], [JOB, "onayla"]);
  assert.deepEqual(res, { handled: true, text: `↪️ [shop] iş ${JOB} → onayla` });
  lang("en"); // a run started in Turkish, answered by an English-speaking plugin: still routed, still approved
  res = await dispatch(hooks, "a", quoted);
  assert.deepEqual(replies[1], [JOB, "onayla"]);
  assert.equal(res.text, `↪️ [shop] job ${JOB} → onayla`);
});

test("a failed forward is reported in the reader's language; other messages stay with the agent", async () => {
  const { hooks } = harness({ reply: () => bad("job is closed") });
  const quoted = `[shop] ✅ Done · job ${JOB}`;
  lang("en");
  assert.equal((await dispatch(hooks, "thanks", quoted)).text, `⚠️ Could not forward to [shop] job ${JOB}: job is closed`);
  lang("tr");
  assert.equal((await dispatch(hooks, "teşekkürler", quoted)).text, `⚠️ [shop] iş ${JOB}'ye iletilemedi: job is closed`);
  assert.equal(await dispatch(hooks, "hello", "An ordinary agent answer"), undefined);
  assert.equal(await dispatch(hooks, "hello", undefined), undefined);
  assert.equal(await dispatch(hooks, "a", quoted, { senderId: "999" }), undefined);
  assert.equal(await dispatch(hooks, "a", quoted, { channel: "discord" }), undefined);
  assert.equal(await dispatch(hooks, "a", quoted, { isGroup: true }), undefined);
});
