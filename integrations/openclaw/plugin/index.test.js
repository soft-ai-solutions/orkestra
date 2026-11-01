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
  const tools = {};
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
    registerTool: (def) => {
      tools[def.name] = def;
    },
  };
  plugin.register(api);
  return { commands, hooks, calls, warnings, tools };
}

const ok = (body) => Promise.resolve({ code: 0, stdout: JSON.stringify(body), stderr: "" });
const bad = (error) => Promise.resolve({ code: 1, stdout: JSON.stringify({ error }), stderr: "" });
const lang = (code) => {
  process.env.ORKESTRA_LANG = code;
};

test("every command exists under its English and its Turkish name, with the same rules", () => {
  lang("en");
  const { commands } = harness();
  assert.deepEqual(Object.keys(commands).sort(), ["cancel", "durum", "ide", "iptal", "run", "runs", "yap"]);
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
  assert.deepEqual(Object.keys(commands).sort(), ["cancel", "durum", "ide", "iptal", "run", "yap"]);
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

// ---------------------------------------------------------------- plain-language requests: the agent's tools

const PROJECTS = { projects: [{ name: "namaz-vakitleri" }, { name: "pulse-race" }, { name: "pulse-arena" }, { name: "shop" }] };

test("the agent gets orkestra_run and orkestra_status with a strict parameter schema", () => {
  lang("en");
  const { tools } = harness();
  assert.deepEqual(Object.keys(tools).sort(), ["orkestra_run", "orkestra_status"]);
  const run = tools.orkestra_run;
  assert.deepEqual(run.parameters.required, ["project", "request"]);
  assert.equal(run.parameters.additionalProperties, false);
  assert.deepEqual(run.parameters.properties.engine.enum, ["claude", "cursor", "copilot", "pi"]);
  assert.doesNotMatch(run.description, TURKISH_LETTERS);
  const manifest = JSON.parse(fs.readFileSync(path.join(HERE, "openclaw.plugin.json"), "utf8"));
  assert.deepEqual(manifest.contracts.tools.sort(), Object.keys(tools).sort());      // declared, as OpenClaw requires
});

test("orkestra_run resolves a spoken project name and starts it like /run", async () => {
  lang("tr");
  const { tools, calls } = harness({
    projects: () => ok(PROJECTS),
    start: (argv) => ok({ id: JOB, project: argv[3], engine: "claude" }),
  });
  const res = await tools.orkestra_run.execute("call-1", { project: "Namaz Vakitleri", request: "bildirim sesini düzelt" });
  assert.equal(res.details.started, true);
  assert.equal(res.details.jobId, JOB);
  assert.equal(res.details.project, "namaz-vakitleri");
  const start = calls.find((c) => c[2] === "start");
  assert.deepEqual(start.slice(2), ["start", "namaz-vakitleri", "bildirim sesini düzelt", "--chat-id", CHAT, "--source", "openclaw"]);
  assert.match(res.content[0].text, new RegExp(`\\[namaz-vakitleri\\] iş ${JOB}`));      // Reply routing can parse it
});

test("a name that cannot be a folder starts nothing; a refused folder name gets the candidates", async () => {
  lang("en");
  const { tools, calls } = harness({ projects: () => ok(PROJECTS), start: () => bad("project 'pulse' not found") });
  const spaced = await tools.orkestra_run.execute("c", { project: "pulse game", request: "speed up the menu" });
  assert.equal(spaced.details.started, false);
  assert.match(spaced.content[0].text, /no project folder "pulse game"/);
  assert.equal(calls.filter((c) => c[2] === "start").length, 0);
  // a plausible folder name goes on as typed (the bridge decides, e.g. outside its allowlist), candidates ride along
  const res = await tools.orkestra_run.execute("c", { project: "pulse", request: "speed up the menu" });
  assert.equal(res.details.started, false);
  assert.match(res.content[0].text, /Could not start: project 'pulse' not found\nDid you mean: pulse-race, pulse-arena\?/);
  const none = await tools.orkestra_run.execute("c", { project: "zzz qq", request: "do it" });
  assert.doesNotMatch(none.content[0].text, /Did you mean/);
});

test("orkestra_run keeps the /run guarantees: no takeover of an open run, fail closed", async () => {
  lang("en");
  const open = [{ project_name: "shop", status: "active", title: "Checkout", mode: "interactive", phase: "implement" }];
  const busy = harness({ projects: () => ok(PROJECTS), watch: () => ok(open), start: () => ok({ id: JOB }) });
  const res = await busy.tools.orkestra_run.execute("c", { project: "shop", request: "add CSV export" });
  assert.equal(res.details.started, false);
  assert.match(res.content[0].text, /already has an open Orkestra run/);
  assert.equal(busy.calls.filter((c) => c[2] === "start").length, 0);
  const blind = harness({ projects: () => ok(PROJECTS), watch: () => bad("watch failed"), start: () => ok({ id: JOB }) });
  const res2 = await blind.tools.orkestra_run.execute("c", { project: "shop", request: "add CSV export" });
  assert.match(res2.content[0].text, /could not check for open runs/);
  assert.equal(blind.calls.filter((c) => c[2] === "start").length, 0);
});

test("without the project list (an older bridge) the name goes through as typed", async () => {
  lang("en");
  const { tools, calls } = harness({ projects: () => bad("not found"), start: (argv) => ok({ id: JOB, project: argv[3] }) });
  const res = await tools.orkestra_run.execute("c", { project: "shop", request: "add CSV export", engine: "copilot" });
  assert.equal(res.details.started, true);
  assert.deepEqual(calls.find((c) => c[2] === "start").slice(-4), ["--chat-id", CHAT, "--source", "openclaw"]);
  assert.ok(calls.find((c) => c[2] === "start").includes("copilot"));
});

test("orkestra_status is the same truthful view as /runs", async () => {
  lang("en");
  const h = harness({ watch: () => ok([]), status: () => ok({ jobs: [] }) });
  const tool = await h.tools.orkestra_status.execute("c", {});
  const cmd = await h.commands.runs.handler({});
  assert.equal(tool.content[0].text, cmd.text);
});

test("/run also understands a project name with spaces or capitals", async () => {
  lang("en");
  const { commands, calls } = harness({ projects: () => ok(PROJECTS), start: (argv) => ok({ id: JOB, project: argv[3] }) });
  await commands.run.handler({ args: "Shop add CSV export" });
  assert.equal(calls.find((c) => c[2] === "start")[3], "shop");
});

test("a host without registerTool still gets every command", () => {
  lang("en");
  const api = {
    pluginConfig: { chatId: CHAT, clientPath: CLIENT, orkestraCliPath: CLI, python: "py" },
    logger: { warn() {}, info() {}, error() {}, debug() {} },
    runtime: { system: { runCommandWithTimeout: () => ok({}) } },
    registerCommand: () => {}, on: () => {}, registerService: () => {},
  };
  assert.doesNotThrow(() => plugin.register(api));
});

// ---------------------------------------------------------------- owner command and revive
import { ownerPhraseDigest } from "./logic.js";

const OWNER = ownerPhraseDigest("salt-for-tests-1234", "open sesame now");
const OWNER_PROJECTS = { projects: [{ name: "namaz-vakitleri" }, { name: "pomi" }, { name: "tinko" }] };
const dm = (content, extra = {}) => ({ channel: "telegram", senderId: CHAT, content, ...extra });

function ownerHarness(extra = {}) {
  const script = {
    ensure: () => ok({ state: "running" }),
    projects: () => ok(OWNER_PROJECTS),
    start: (argv) => ok({ id: JOB, project: argv[3], owner: argv.includes("--owner") }),
    ...extra,
  };
  return harness(script, { config: { ownerCommand: OWNER } });
}

test("an owner command starts an owner job in the project the message names, and never reaches the agent", async () => {
  lang("tr");
  const { hooks, calls } = ownerHarness();
  const res = await hooks.before_dispatch(dm("Open Sesame now: namaz vakitlerinde bildirim sesini düzelt ve yayınla"), {});
  assert.equal(res.handled, true);
  assert.match(res.text, /Sahip komutu başladı: \[namaz-vakitleri\] iş 0123456789ab/);
  const start = calls.find((a) => a[2] === "start");
  assert.ok(start.includes("--owner"));
  assert.equal(start[3], "namaz-vakitleri");
  assert.equal(start[4], "namaz vakitlerinde bildirim sesini düzelt ve yayınla");   // the passphrase is gone
  assert.ok(!calls.flat().join(" ").toLowerCase().includes("sesame"));
  assert.ok(calls.findIndex((a) => a[2] === "ensure") < calls.findIndex((a) => a[2] === "start"));   // Orkestra first
});

test("without the passphrase nothing changes: the message goes to the agent", async () => {
  const { hooks, calls } = ownerHarness();
  assert.equal(await hooks.before_dispatch(dm("namaz vakitlerinde bildirim sesini düzelt"), {}), undefined);
  assert.equal(calls.length, 0);
  assert.equal(await hooks.before_dispatch(dm("open sesame now: pomi", { senderId: "999" }), {}), undefined);
});

test("an unclear project is asked once; the Reply with a letter starts the owner job without the passphrase", async () => {
  lang("en");
  const { hooks, calls } = ownerHarness();
  const ask = await hooks.before_dispatch(dm("open sesame now make tinko look like pomi"), {});
  assert.equal(ask.handled, true);
  assert.match(ask.text, /a\) tinko\nb\) pomi/);
  const code = /\[owner ([a-f0-9]{8})\]/.exec(ask.text)[1];
  const res = await hooks.before_dispatch(dm("b", { replyToBody: ask.text }), {});
  assert.match(res.text, /Owner command started: \[pomi\] job/);
  const start = calls.find((a) => a[2] === "start");
  assert.deepEqual([start[3], start[4], start.includes("--owner")], ["pomi", "make tinko look like pomi", true]);
  const again = await hooks.before_dispatch(dm("a", { replyToBody: ask.text }), {});
  assert.match(again.text, /older than 30 minutes or unknown/);                       // one start per command
  assert.ok(code);
});

test("no project in the message: the question lists the folders and a typed name answers it", async () => {
  lang("en");
  const { hooks, calls } = ownerHarness();
  const ask = await hooks.before_dispatch(dm("open sesame now update the readme"), {});
  assert.match(ask.text, /Projects: namaz-vakitleri, pomi, tinko/);
  const res = await hooks.before_dispatch(dm("Namaz Vakitleri", { replyToBody: ask.text }), {});
  assert.match(res.text, /\[namaz-vakitleri\]/);
  assert.equal(calls.filter((a) => a[2] === "start").length, 1);
});

test("an owner command without a task, or with Orkestra down, says so and starts nothing", async () => {
  lang("en");
  let h = ownerHarness();
  assert.match((await h.hooks.before_dispatch(dm("open sesame now!"), {})).text, /no task next to it/);
  h = ownerHarness({ ensure: () => bad("launchd said no") });
  const res = await h.hooks.before_dispatch(dm("open sesame now fix pomi"), {});
  assert.match(res.text, /could not be started \(launchd said no\)/);
  assert.equal(h.calls.filter((a) => a[2] === "start").length, 0);
});

test("an owner command keeps the no-takeover rule of /run", async () => {
  lang("en");
  const { hooks, calls } = harness({
    ensure: () => ok({ state: "running" }),
    projects: () => ok(OWNER_PROJECTS),
    watch: () => ok([{ project_name: "pomi", status: "active", title: "other work", phase: "implement" }]),
  }, { config: { ownerCommand: OWNER } });
  const res = await hooks.before_dispatch(dm("open sesame now fix pomi"), {});
  assert.match(res.text, /already has an open Orkestra run/);
  assert.equal(calls.filter((a) => a[2] === "start").length, 0);
});

test("a reply to a job with the passphrase forwards the text without it", async () => {
  lang("en");
  const { hooks, calls } = ownerHarness({ reply: () => ok({ ok: true }) });
  await hooks.before_dispatch(dm("open sesame now: use the blue one", { replyToBody: `[pomi] ⛔ Blocked · implement · job ${JOB}` }), {});
  const reply = calls.find((a) => a[2] === "reply");
  assert.deepEqual(reply.slice(3), [JOB, "use the blue one"]);
});

test("an invalid ownerCommand config turns owner commands off with a warning", async () => {
  const { hooks, warnings } = harness({}, { config: { ownerCommand: { salt: "short", sha256: "x", words: 2 } } });
  assert.ok(warnings.some((w) => /ownerCommand is invalid/.test(w)));
  assert.equal(await hooks.before_dispatch(dm("open sesame now fix pomi"), {}), undefined);
});

test("/ide and a short 'ide ayaklandır' bring the tools back and report each one", async () => {
  lang("tr");
  const revive = (argv) => Promise.resolve({
    code: 1,
    stdout: JSON.stringify({ bridge: { state: "running" }, dashboard: { state: "started" },
      apps: [{ app: argv[4], state: "failed" }], ok: false }),
    stderr: "",
  });
  const { commands, hooks, calls } = harness({ revive });
  const viaText = await hooks.before_dispatch(dm("IDE'yi ayaklandır"), {});
  assert.equal(viaText.handled, true);
  assert.match(viaText.text, /✅ Orkestra bridge: zaten çalışıyor\n✅ Orkestra dashboard: başlatıldı\n⚠️ Cursor: başlatılamadı/);
  assert.deepEqual(calls[0].slice(2), ["revive", "--app", "Cursor"]);
  const viaCommand = await commands.ide.handler({});
  assert.match(viaCommand.text, /Araçlar ayağa kaldırılıyor/);
  assert.equal(await hooks.before_dispatch(dm("namaz vakitleri projesinde ide ayarlarını düzelt lütfen bugün"), {}), undefined);
});

test("ideApps in the config chooses the apps", async () => {
  const { commands, calls } = harness({ revive: () => ok({ bridge: { state: "running" }, dashboard: { state: "running" }, apps: [] }) },
    { config: { ideApps: ["Visual Studio Code", "Cursor"] } });
  await commands.ide.handler({});
  assert.deepEqual(calls[0].slice(2), ["revive", "--app", "Visual Studio Code", "--app", "Cursor"]);
});
