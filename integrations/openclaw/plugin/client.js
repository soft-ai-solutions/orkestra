// integrations/openclaw/plugin/client.js
// Calls bridge/orkestra_client.py (CONTRACT.md) with an argv array — never a shell string.
// The bridge token stays inside orkestra_client.py; this module never reads ~/.orkestra.

/**
 * @typedef {(argv: string[], opts: { timeoutMs: number, env?: Record<string, string | undefined> }) =>
 *   Promise<{ stdout: string, stderr: string, code: number | null }>} Runner
 */

/**
 * @param {{ runner: Runner, python: string, clientPath: string, env?: Record<string, string | undefined>,
 *   timeoutMs?: number }} options
 */
export function createOrkestraClient({
  runner,
  python,
  clientPath,
  env,
  timeoutMs = 45_000,
}) {
  async function call(args) {
    const result = await runner([python, clientPath, ...args], {
      timeoutMs,
      env,
    });
    let body = null;
    try {
      body = JSON.parse(result.stdout || "null");
    } catch {
      body = null;
    }
    const ok = result.code === 0 && body !== null;
    const error =
      body?.error ??
      (ok
        ? undefined
        : (
            result.stderr ||
            result.stdout ||
            `orkestra_client.py exit ${result.code}`
          )
            .trim()
            .slice(-400));
    return { ok, body, error };
  }

  return {
    /** Starts a run: `/yap [engine] <project> <request>`. */
    start: (project, request, chatId, engine) =>
      call([
        "start",
        project,
        request,
        ...(engine ? ["--engine", engine] : []),
        "--chat-id",
        chatId,
        "--source",
        "openclaw",
      ]),
    /** Answers one specific job (the one the user replied to). */
    reply: (jobId, text) => call(["reply", jobId, text]),
    /** Jobs of this chat. */
    status: (chatId) => call(["status", "--chat-id", chatId]),
    /** Every bridge job (any source). */
    statusAll: () => call(["status"]),
    /** Stops the latest open job of this chat. */
    cancel: (chatId) => call(["cancel", "--chat-id", chatId]),
    /** The project folders the bridge may start (its allowlist applied). */
    projects: () => call(["projects"]),
  };
}

/**
 * Read-only view of every open orkestra run in the workspace, including runs started on the desktop:
 * `orkestra watch --json` (one snapshot; reads run state files, writes nothing).
 * @param {{ runner: Runner, python: string, cliPath: string, env?: Record<string, string | undefined> }} options
 */
export function createOrkestraMonitor({ runner, python, cliPath, env }) {
  return {
    async openRuns() {
      const result = await runner([python, cliPath, "watch", "--json"], {
        timeoutMs: 30_000,
        env,
      });
      try {
        const runs = JSON.parse(result.stdout || "null");
        if (result.code === 0 && Array.isArray(runs)) {
          return { ok: true, runs };
        }
      } catch {
        // fall through to the error below
      }
      return {
        ok: false,
        error: (
          result.stderr ||
          result.stdout ||
          `orkestra exit ${result.code}`
        )
          .trim()
          .slice(-400),
      };
    },
  };
}
