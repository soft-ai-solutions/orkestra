// integrations/openclaw/plugin/plugin.e2e.test.js — node --test plugin.e2e.test.js
// Real index.js against a real, isolated orkestra bridge (temp ORKESTRA_HOME, fake claude from
// bridge/tests). Never touches ~/.orkestra, the running bridge, Telegram or real projects.
import assert from "node:assert/strict";
import { execFile, execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

// The assertions below predate the English default and read Turkish texts: the plugin, the bridge and the CLI it
// spawns all inherit this. The English side of the same flow is the last test; logic/index tests cover the rest.
process.env.ORKESTRA_LANG = "tr";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ORKESTRA = path.resolve(HERE, "../../..");
const BRIDGE = path.join(ORKESTRA, "bridge/orkestra_bridge.py");
const CLIENT = path.join(ORKESTRA, "bridge/orkestra_client.py");
const ORKESTRA_CLI = path.join(ORKESTRA, "plugins/orkestra/bin/orkestra");
const FAKE_CLAUDE = path.join(ORKESTRA, "bridge/tests/fake_claude.py");
const OPENCLAW_PKG =
  process.env.OPENCLAW_PKG ?? "/opt/homebrew/lib/node_modules/openclaw";
const CHAT = "42";
// Local/CI honesty: e2e needs a real openclaw install (Homebrew path or OPENCLAW_PKG).
// Without it, skip the suite so `npm run test:e2e` exits 0; CI runs logic-only `npm test`.
const E2E_SKIP = !fs.existsSync(OPENCLAW_PKG)
  ? `OpenClaw package missing at ${OPENCLAW_PKG}; set OPENCLAW_PKG for local e2e`
  : false;

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "orkestra-openclaw-e2e-"));
const FAKE_COPILOT = path.join(tmp, "fake-copilot.py");
const COPILOT_PLUGIN = path.join(tmp, "copilot-plugin");
const orkHome = path.join(tmp, "home");
const workspace = path.join(tmp, "Projects");
const sent = [];
const commands = {};
const hooks = {};
let service;
let bridge;
let plugin;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const logger = { info() {}, warn() {}, error() {}, debug() {} };

function freePort() {
  return new Promise((resolve) => {
    const srv = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// Stand-in for api.runtime.system.runCommandWithTimeout: python goes to the isolated bridge,
// `openclaw message send` is captured instead of reaching Telegram.
function runner(argv, opts) {
  if (argv[0] === "openclaw") {
    const text = argv[argv.indexOf("-m") + 1];
    sent.push(text);
    return Promise.resolve({
      code: 0,
      stdout: JSON.stringify({ result: { messageId: String(sent.length) } }),
      stderr: "",
    });
  }
  return new Promise((resolve) => {
    execFile(
      argv[0],
      argv.slice(1),
      {
        timeout: opts.timeoutMs,
        env: {
          ...process.env,
          ORKESTRA_HOME: orkHome,
          ORKESTRA_WORKSPACE: workspace,
        },
      },
      (error, stdout, stderr) =>
        resolve({ code: error ? (error.code ?? 1) : 0, stdout, stderr }),
    );
  });
}

async function waitFor(predicate, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) {
      return value;
    }
    await sleep(200);
  }
  assert.fail(`timed out waiting for ${label}; sent=${JSON.stringify(sent)}`);
}

before(async () => {
  if (E2E_SKIP) {
    return;
  }
  // Plugin copy whose `openclaw/plugin-sdk/*` import resolves to the installed OpenClaw.
  const pluginDir = path.join(tmp, "plugin");
  fs.mkdirSync(path.join(pluginDir, "node_modules"), { recursive: true });
  for (const f of ["index.js", "logic.js", "client.js", "lang.js", "messages.js", "package.json"]) {
    fs.copyFileSync(path.join(HERE, f), path.join(pluginDir, f));
  }
  fs.symlinkSync(
    OPENCLAW_PKG,
    path.join(pluginDir, "node_modules", "openclaw"),
  );

  for (const name of ["demo", "demo2"]) {
    fs.mkdirSync(path.join(workspace, name), { recursive: true });
    fs.writeFileSync(path.join(workspace, name, "AGENTS.md"), `# ${name}\n`);
    execFileSync("git", ["init", "-q", path.join(workspace, name)]);
  }
  fs.writeFileSync(
    FAKE_COPILOT,
    "#!/usr/bin/env python3\nimport json, os, sys\n" +
      "args = sys.argv[1:]\n" +
      "sid = args[args.index('--session-id') + 1] if '--session-id' in args else args[args.index('--resume') + 1]\n" +
      "with open(os.path.join(os.getcwd(), '.fake-copilot.json'), 'w') as f: f.write(json.dumps(args))\n" +
      "print(json.dumps({'type': 'result', 'result': 'copilot-e2e-ok', 'session_id': sid}))\n",
    { mode: 0o755 },
  );
  const copilotHooks = path.join(COPILOT_PLUGIN, "com.github.copilot", "hooks");
  fs.mkdirSync(copilotHooks, { recursive: true });
  const copilotHookScript = path.join(
    ORKESTRA,
    "plugins/orkestra/hooks/copilot_hook.py",
  );
  fs.writeFileSync(
    path.join(copilotHooks, "hooks.json"),
    JSON.stringify({
      version: 1,
      hooks: Object.fromEntries(
        ["UserPromptSubmit", "PreToolUse", "PostToolUse"].map((event) => [
          event,
          [
            {
              command: `python3 ${copilotHookScript} ${event}`,
              timeoutSec: 20,
            },
          ],
        ]),
      ),
    }),
  );
  const port = await freePort();
  fs.mkdirSync(orkHome, { recursive: true });
  // A run started outside the bridge (like a desktop /orkestra:run): the bridge never sees it.
  execFileSync(
    "python3",
    [
      ORKESTRA_CLI,
      "start",
      "--title",
      "masaüstü işi",
      "--request",
      "desktop run",
      "--size",
      "S",
      "-p",
      path.join(workspace, "demo2"),
    ],
    {
      env: {
        ...process.env,
        ORKESTRA_HOME: orkHome,
        ORKESTRA_WORKSPACE: workspace,
      },
    },
  );
  fs.writeFileSync(
    path.join(orkHome, "bridge.secret.json"),
    JSON.stringify({
      port,
      token: "e2e-token",
      workspace,
      claude_bin: FAKE_CLAUDE,
      copilot_bin: FAKE_COPILOT,
      copilot_plugin_dir: COPILOT_PLUGIN,
      allowed_projects: ["demo"],
      telegram: { bot_token: "", allowed_chat_ids: [] },
      copilot_bin: FAKE_COPILOT,
      copilot_plugin_dir: COPILOT_PLUGIN,
    }),
  );
  bridge = spawn("python3", [BRIDGE, "serve"], {
    env: {
      ...process.env,
      ORKESTRA_HOME: orkHome,
      ORKESTRA_WORKSPACE: workspace,
    },
    stdio: "ignore",
  });
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}/health`)).ok) break;
    } catch {
      await sleep(100);
    }
  }

  plugin = (await import(path.join(pluginDir, "index.js"))).default;
  plugin.register({
    pluginConfig: { chatId: CHAT, clientPath: CLIENT, pollSeconds: 5 },
    logger,
    runtime: { system: { runCommandWithTimeout: runner } },
    registerCommand: (def) => {
      commands[def.name] = def;
    },
    on: (name, fn) => {
      hooks[name] = fn;
    },
    registerService: (svc) => {
      service = svc;
    },
  });
  await service.start({ stateDir: path.join(tmp, "state"), logger });
});

after(async () => {
  if (!E2E_SKIP) {
    await service?.stop?.({});
    bridge?.kill("SIGTERM");
  }
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("registers /yap, /durum, /iptal (and their English names), before_dispatch and the notifier", { skip: E2E_SKIP }, () => {
  // Each Turkish command also exists as /run, /runs and /cancel (/status is reserved by OpenClaw itself).
  assert.deepEqual(Object.keys(commands).sort(), ["cancel", "durum", "ide", "iptal", "run", "runs", "yap"]);
  assert.equal(typeof hooks.before_dispatch, "function");
  assert.equal(service.id, "orkestra-bridge-notifier");
  for (const def of Object.values(commands)) {
    assert.equal(def.requireAuth, true);
    assert.deepEqual(def.channels, ["telegram"]);
  }
});

test("/durum tells the truth: desktop runs are listed even when the bridge has no jobs", { skip: E2E_SKIP }, async () => {
  const text = (await commands.durum.handler({})).text;
  assert.match(
    text,
    /Açık run'lar \(1\):\n• demo2 — masaüstü işi\n {2}\S+ · masaüstü · /,
  );
  assert.match(text, /Telegram'dan başlatılmış açık iş yok\./);
  assert.doesNotMatch(text, /Kayıtlı orkestra işi yok/);
});

test("/yap refuses a project whose run is already open elsewhere (no takeover, no bridge job)", { skip: E2E_SKIP }, async () => {
  const text = (await commands.yap.handler({ args: "demo2 başka bir iş" }))
    .text;
  assert.match(
    text,
    /⛔ Başlatmadım: \[demo2\] projesinde zaten açık bir orkestra run var — "masaüstü işi"/,
  );
  assert.match(text, /masaüstü/);
  const jobs = await new Promise((resolve) =>
    execFile(
      "python3",
      [CLIENT, "status"],
      { env: { ...process.env, ORKESTRA_HOME: orkHome } },
      (e, stdout) => resolve(JSON.parse(stdout).jobs),
    ),
  );
  assert.equal(jobs.filter((j) => j.project === "demo2").length, 0);
});

test("/yap copilot: selects the guarded Copilot engine and default profile", { skip: E2E_SKIP }, async () => {
  const started = (
    await commands.yap.handler({ args: "copilot: demo kısa görev" })
  ).text;
  const jobId = /iş ([a-f0-9]{12})/.exec(started)?.[1];
  assert.ok(jobId, started);
  await waitFor(
    () =>
      sent.find(
        (message) =>
          message.includes(`iş ${jobId}`) && message.includes("Tamamlandı"),
      ),
    "Copilot job completion",
  );
  const job = JSON.parse(
    fs.readFileSync(
      path.join(orkHome, "bridge", "jobs", `${jobId}.json`),
      "utf8",
    ),
  );
  assert.equal(job.engine, "copilot");
  const args = JSON.parse(
    fs.readFileSync(path.join(workspace, "demo", ".fake-copilot.json"), "utf8"),
  );
  assert.ok(args.includes("--allow-all-tools"));
  assert.ok(args.includes("--autopilot"));
  assert.ok(args.includes("--no-ask-user"));
  assert.equal(args[args.indexOf("--model") + 1], "gpt-6-luna");
  assert.equal(args[args.indexOf("--reasoning-effort") + 1], "max");
  assert.equal(args[args.indexOf("--plugin-dir") + 1], COPILOT_PLUGIN);
});

test("full flow: /yap → question → reply-to with a letter → that job gets the option → /iptal", { skip: E2E_SKIP }, async () => {
  assert.match((await commands.yap.handler({ args: "" })).text, /Kullanım/);
  assert.match(
    (await commands.yap.handler({ args: "../etc bir şey" })).text,
    /Kullanım/,
  );

  const started = (
    await commands.yap.handler({
      args: "demo öncelik alanı ekle; ben karar verici olayım",
    })
  ).text;
  const jobId = /iş ([a-f0-9]{12})/.exec(started)?.[1];
  assert.ok(jobId, started);
  await waitFor(
    () => sent.find((m) => m.includes(`iş ${jobId}`)),
    "first notification",
  );
  assert.match(
    (await commands.yap.handler({ args: "demo ikinci iş" })).text,
    /⛔ Başlatmadım: \[demo\] projesinde zaten açık bir orkestra run var/,
  );

  const question = await waitFor(
    () =>
      sent.find(
        (m) => m.includes(`iş ${jobId}`) && m.includes("Yanıtını bekliyor"),
      ),
    "question",
  );
  assert.match(question, /\(a\) medium/);

  // Unrelated chat messages and other senders stay with the agent.
  assert.equal(
    await hooks.before_dispatch(
      { channel: "telegram", senderId: CHAT, content: "selam" },
      {},
    ),
    undefined,
  );
  assert.equal(
    await hooks.before_dispatch(
      {
        channel: "telegram",
        senderId: "999",
        content: "a",
        replyToBody: question,
      },
      {},
    ),
    undefined,
  );
  assert.equal(
    await hooks.before_dispatch(
      {
        channel: "telegram",
        senderId: CHAT,
        content: "a",
        replyToBody: "Claude'un normal cevabı",
      },
      {},
    ),
    undefined,
  );

  const routed = await hooks.before_dispatch(
    {
      channel: "telegram",
      senderId: CHAT,
      content: "a",
      replyToBody: question,
    },
    {},
  );
  assert.equal(routed.handled, true);
  assert.match(
    routed.text,
    new RegExp(`iş ${jobId} → a\\) medium \\(Önerilen\\)`),
  );
  // The fake orkestra session echoes what it received: the expanded option reached this job.
  await waitFor(
    () =>
      sent.find(
        (m) =>
          m.includes(`iş ${jobId}`) &&
          m.includes("devam ediyorum: a) medium (Önerilen)"),
      ),
    "echo",
  );

  // Same reply routed by Telegram message id when the quote is unavailable.
  const questionMessageId = String(sent.indexOf(question) + 1);
  const byId = await hooks.before_dispatch(
    {
      channel: "telegram",
      senderId: CHAT,
      content: "onay",
      replyToId: questionMessageId,
    },
    {},
  );
  assert.equal(byId.handled, true);
  assert.match(byId.text, new RegExp(`iş ${jobId}`));

  await waitFor(
    () => sent.filter((m) => m.includes(`iş ${jobId}`)).length >= 3,
    "second answer",
  );
  const status = (await commands.durum.handler({})).text;
  assert.match(status, /Açık run'lar \(2\):/);
  assert.match(status, /• demo — [^\n]+\n {2}[^\n]+ · Telegram işi · /);
  assert.match(status, /• demo2 — masaüstü işi/);
  assert.match(status, new RegExp(`• ${jobId} · demo · ⏸ Yanıtını bekliyor`));
  assert.match((await commands.iptal.handler({})).text, /İptal edildi/);
  await waitFor(
    () => sent.find((m) => m.includes(`iş ${jobId}`) && m.includes("İptal")),
    "cancel notice",
  );
});

test("the same plugin speaks English when asked to, and refuses the busy desktop project in English", { skip: E2E_SKIP }, async () => {
  process.env.ORKESTRA_LANG = "en";
  try {
    const status = (await commands.runs.handler({})).text;
    assert.match(status, /^🎼 Orkestra status\nOpen runs \(1\):\n• demo2: masaüstü işi\n/);
    assert.doesNotMatch(status, /Açık run/);
    const busy = (await commands.run.handler({ args: "demo2 another job" })).text;
    assert.match(busy, /^⛔ Not started: project \[demo2\] already has an open Orkestra run: "masaüstü işi"/);
    assert.match((await commands.run.handler({ args: "" })).text, /^Usage: \/run /);
  } finally {
    process.env.ORKESTRA_LANG = "tr";
  }
});

test("a restarted notifier does not repeat old notifications", { skip: E2E_SKIP }, async () => {
  const before = sent.length;
  await service.stop({});
  await service.start({ stateDir: path.join(tmp, "state"), logger });
  await sleep(1500);
  assert.equal(sent.length, before);
});

test("owner command: the passphrase starts an owner job on the real bridge, with the grant until /iptal", { skip: E2E_SKIP }, async () => {
  const { ownerPhraseDigest } = await import(path.join(tmp, "plugin", "logic.js"));
  const ownerHooks = {};
  const ownerCommands = {};
  plugin.register({
    pluginConfig: { chatId: CHAT, clientPath: CLIENT, ownerCommand: ownerPhraseDigest("e2e-salt-0123456789", "open sesame now") },
    logger,
    runtime: { system: { runCommandWithTimeout: runner } },
    registerCommand: (def) => {
      ownerCommands[def.name] = def;
    },
    on: (name, fn) => {
      ownerHooks[name] = fn;
    },
    registerService: () => {},
  });
  const res = await ownerHooks.before_dispatch(
    { channel: "telegram", senderId: CHAT, content: "Open Sesame Now: demo projesinde README'ye bir satır ekle" },
    {},
  );
  assert.equal(res.handled, true);
  const jobId = /Sahip komutu başladı: \[demo\] iş ([a-f0-9]{12})/.exec(res.text)?.[1];
  assert.ok(jobId, res.text);
  const job = JSON.parse(fs.readFileSync(path.join(orkHome, "bridge", "jobs", `${jobId}.json`), "utf8"));
  assert.equal(job.owner, true);
  assert.equal(job.engine, "claude");
  assert.ok(!JSON.stringify(job.turns).toLowerCase().includes("sesame"));     // the passphrase never reaches the run
  const grant = path.join(orkHome, "grants", "owner", `${job.session_id}.json`);
  assert.ok(fs.existsSync(grant));
  assert.match((await ownerCommands.iptal.handler({})).text, /İptal/);
  await waitFor(() => !fs.existsSync(grant), "owner grant removed after /iptal");
});
