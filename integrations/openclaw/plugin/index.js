// integrations/openclaw/plugin/index.js
// OpenClaw plugin "orkestra-bridge" (openclaw-agent side of CONTRACT.md):
//   /run <project> <request> · /runs · /cancel  → bridge/orkestra_client.py, no LLM turn. The Turkish names /yap
//   <proje> <istek> · /durum · /iptal are the same three commands (OpenClaw reserves /status for itself).
//   Text for a person is English or Turkish (lang.js + messages.js: ORKESTRA_LANG, config.json, OS locale).
//   A Telegram *reply* to an orkestra message → that job only (`reply <job-id>`); a bare option
//   letter becomes "b) <option text>". Every other message goes to the agent untouched.
//   Notifier service: polls `status --chat-id` and announces each stopped job state exactly once.
// OpenClaw keeps the Telegram bot (a Telegram bot token has ONE consumer; two pollers get 409 Conflict); the bridge never gets its token.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createOrkestraClient, createOrkestraMonitor } from "./client.js";
import {
  diffJobs,
  expandOptionReply,
  findOpenRun,
  formatBusyProject,
  formatNotification,
  formatStatus,
  parseJobRef,
  parseSentMessageIds,
} from "./logic.js";
import { t } from "./messages.js";

const PROJECT_NAME = /^[A-Za-z0-9._-]{1,80}$/;
const MAX_MESSAGE_MAP = 300;

function readState(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    return { seen: data.seen ?? null, messages: data.messages ?? {} };
  } catch {
    return { seen: null, messages: {} };
  }
}

function writeState(file, state) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, file);
}

function short(text, max = 160) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

export default definePluginEntry({
  id: "orkestra-bridge",
  name: "Orkestra bridge",
  description:
    "Telegram → orkestra: /run (/yap), /runs (/durum), /cancel (/iptal), reply-to-answer and job notifications.",
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const chatId = typeof cfg.chatId === "string" ? cfg.chatId.trim() : "";
    if (!/^-?\d+$/.test(chatId)) {
      api.logger.warn(
        "orkestra-bridge: plugins.entries.orkestra-bridge.config.chatId missing; plugin inactive",
      );
      return;
    }
    const home = os.homedir();
    // the checkout this plugin was loaded from; the old fixed location when OpenClaw runs a copy of this folder
    const beside = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../bridge/orkestra_client.py",
    );
    const clientPath =
      cfg.clientPath ||
      (fs.existsSync(beside)
        ? beside
        : path.join(home, "Projects/orkestra/bridge/orkestra_client.py"));
    const pollMs = Math.max(5, Number(cfg.pollSeconds) || 15) * 1000;
    const runner = (argv, opts) =>
      api.runtime.system.runCommandWithTimeout(argv, {
        timeoutMs: opts.timeoutMs,
        ...(opts.env ? { env: opts.env } : {}),
      });
    const python = cfg.python || "python3";
    const client = createOrkestraClient({ runner, python, clientPath });
    const cliPath =
      cfg.orkestraCliPath ||
      path.resolve(
        path.dirname(clientPath),
        "../plugins/orkestra/bin/orkestra",
      );
    const monitor = createOrkestraMonitor({ runner, python, cliPath });
    // Telegram message id → job, filled by the notifier; fallback when the quote is unavailable.
    let messages = {};

    // Every command exists under an English and a Turkish name (the Turkish names are the original ones). A name the
    // host already reserves is skipped with a warning; it never stops the plugin.
    const registerBoth = (names, definition) => {
      for (const [index, name] of names.entries()) {
        // the prompt guidance is registered once (on the first name); an alias would only repeat it
        const { agentPromptGuidance, ...plain } = definition;
        try {
          api.registerCommand({ ...(index === 0 ? definition : plain), name });
        } catch (error) {
          api.logger.warn(`orkestra-bridge: /${name} not registered: ${short(String(error), 200)}`);
        }
      }
    };

    registerBoth(["yap", "run"], {
      description: t("cmd.run_desc"),
      channels: ["telegram"],
      acceptsArgs: true,
      requireAuth: true,
      agentPromptGuidance: [
        {
          text:
            "Orkestra: the owner starts runs with /run (or /yap) [claude:|cursor:|copilot:|pi:] <project> <request>, checks them with " +
            "/runs (or /durum), stops them with /cancel (or /iptal) and answers a run by replying (Telegram reply) to its " +
            "notification; the gateway routes all of that without you. In this chat never start, resume, continue or take " +
            "over an orkestra run (no /orkestra:* skills or commands, no orkestra_client.py start/reply, no `orkestra " +
            "resume`): open runs belong to their own desktop or /run sessions and a second driver breaks them. If the owner " +
            "asks for orkestra here, tell them to send /run <project> <request> or prefix an engine (for example /run copilot: " +
            "<project> <request>). Handle other development requests directly.",
          surfaces: ["cli_backend", "openclaw_main"],
        },
      ],
      handler: async (ctx) => {
        const m = /^\s*(\S+)\s+([\s\S]*\S)\s*$/.exec(ctx.args ?? "");
        if (!m) {
          return { text: t("cmd.usage") };
        }
        let project = m[1];
        let request = m[2];
        let engine;
        const enginePrefix = /^(claude|cursor|copilot|pi):$/i.exec(project);
        if (enginePrefix) {
          const target = /^\s*(\S+)\s+([\s\S]*\S)\s*$/.exec(request);
          if (!target) {
            return { text: t("cmd.usage") };
          }
          engine = enginePrefix[1].toLowerCase();
          project = target[1];
          request = target[2];
        }
        if (!PROJECT_NAME.test(project)) {
          return { text: t("cmd.usage") };
        }
        // Fail closed: without a reliable view of open runs, do not risk taking over one.
        const runs = await monitor.openRuns();
        if (!runs.ok) {
          return { text: t("cmd.check_failed", { error: runs.error }) };
        }
        const busy = findOpenRun(runs.runs, project);
        if (busy) {
          return { text: formatBusyProject(project, busy) };
        }
        const res = await client.start(project, request, chatId, engine);
        if (!res.ok) {
          return { text: t("cmd.start_failed", { error: res.error }) };
        }
        return {
          text:
            t("cmd.started", {
              project: res.body.project,
              id: res.body.id,
              engine: res.body.engine && res.body.engine !== "claude" ? ` · ${res.body.engine}` : "",
            }) +
            `\n${t("cmd.started_note")}`,
        };
      },
    });

    registerBoth(["durum", "runs"], {
      description: t("cmd.runs_desc"),
      channels: ["telegram"],
      requireAuth: true,
      handler: async () => {
        const [runs, jobs] = await Promise.all([
          monitor.openRuns(),
          client.statusAll(),
        ]);
        return {
          text: formatStatus({
            runs: runs.ok ? runs.runs : null,
            runsError: runs.ok ? null : runs.error,
            bridgeJobs: jobs.ok ? (jobs.body.jobs ?? []) : null,
            bridgeError: jobs.ok ? null : jobs.error,
          }),
        };
      },
    });

    registerBoth(["iptal", "cancel"], {
      description: t("cmd.cancel_desc"),
      channels: ["telegram"],
      requireAuth: true,
      handler: async () => {
        const res = await client.cancel(chatId);
        return {
          text: res.ok
            ? String(res.body.message ?? t("cmd.cancel_sent"))
            : t("cmd.cancel_failed", { error: res.error }),
        };
      },
    });

    api.on("before_dispatch", async (event, ctx) => {
      const channel = event.channel ?? ctx.channelId;
      if (channel !== "telegram" || event.isGroup) {
        return;
      }
      const sender = String(event.senderId ?? ctx.senderId ?? "").replace(
        /^telegram:/,
        "",
      );
      if (sender !== chatId) {
        return;
      }
      const quoted = event.replyToBody ?? ctx.replyToBody;
      const replyToId = String(event.replyToId ?? ctx.replyToId ?? "");
      const ref =
        parseJobRef(quoted) ??
        (replyToId ? (messages[replyToId] ?? null) : null);
      if (!ref) {
        return;
      }
      const text = expandOptionReply(event.content ?? event.body ?? "", quoted);
      if (!text) {
        return { handled: true, text: t("route.empty") };
      }
      const res = await client.reply(ref.jobId, text);
      return res.ok
        ? {
            handled: true,
            text: t("route.forwarded", { project: ref.project, id: ref.jobId, text: short(text) }),
          }
        : {
            handled: true,
            text: t("route.failed", { project: ref.project, id: ref.jobId, error: res.error }),
          };
    });

    let timer;
    let busy = false;
    let lastPollError = "";
    api.registerService({
      id: "orkestra-bridge-notifier",
      start: async (sctx) => {
        const stateFile = path.join(
          sctx.stateDir,
          "plugins",
          "orkestra-bridge",
          "state.json",
        );
        const state = readState(stateFile);
        messages = state.messages;
        const tick = async () => {
          if (busy) {
            return;
          }
          busy = true;
          try {
            const res = await client.status(chatId);
            if (!res.ok) {
              if (res.error !== lastPollError) {
                sctx.logger.warn(
                  `orkestra-bridge: status failed: ${short(res.error, 300)}`,
                );
                lastPollError = res.error;
              }
              return;
            }
            lastPollError = "";
            const { announce, seen } = diffJobs(
              res.body.jobs ?? [],
              state.seen,
            );
            state.seen = seen;
            for (const job of announce) {
              const sent = await runner(
                [
                  "openclaw",
                  "message",
                  "send",
                  "--channel",
                  "telegram",
                  "--target",
                  chatId,
                  "-m",
                  formatNotification(job),
                  "--json",
                ],
                { timeoutMs: 60_000 },
              );
              if (sent.code !== 0) {
                sctx.logger.warn(
                  `orkestra-bridge: notification for ${job.id} failed (exit ${sent.code}); retrying next poll`,
                );
                continue;
              }
              state.seen[job.id] = job.updated;
              for (const id of parseSentMessageIds(sent.stdout)) {
                state.messages[id] = { jobId: job.id, project: job.project };
              }
            }
            const ids = Object.keys(state.messages);
            for (const id of ids.slice(
              0,
              Math.max(0, ids.length - MAX_MESSAGE_MAP),
            )) {
              delete state.messages[id];
            }
            writeState(stateFile, state);
          } catch (error) {
            sctx.logger.warn(
              `orkestra-bridge: poll error: ${short(String(error), 300)}`,
            );
          } finally {
            busy = false;
          }
        };
        timer = setInterval(tick, pollMs);
        timer.unref?.();
        await tick();
        sctx.logger.info(
          `orkestra-bridge active: chat ${chatId}, poll ${pollMs / 1000}s`,
        );
      },
      stop: async () => {
        clearInterval(timer);
      },
    });
  },
});
