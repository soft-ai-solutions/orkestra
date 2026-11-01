// integrations/openclaw/plugin/logic.js
// Pure helpers for the OpenClaw ↔ orkestra bridge plugin: Telegram reply routing, option-letter
// expansion, notification text and change detection. No I/O here; see index.js / client.js.
// Text for a person comes from messages.js in English or Turkish (every formatter takes an optional `lang`, default:
// the language resolved by lang.js); everything that RECOGNISES text (job header, approval words) accepts both.
import { createHash } from "node:crypto";
import { resolveLang } from "./lang.js";
import { t } from "./messages.js";

const PROJECT = String.raw`[A-Za-z0-9._-]{1,80}`;
const JOB_ID = String.raw`[a-f0-9]{12}`;
// Matches the bridge notification header `[proj] <status> · <phase> · job <id>` (Turkish: `iş <id>`) and the start
// confirmation `Started: [proj] job <id>` (`Başladım: [proj] iş <id>`), wherever they sit in the quoted message.
// The job word is protocol: a run started in one language is answered in the other, so both are always accepted.
const JOB_REF = new RegExp(String.raw`\[(${PROJECT})\][^\n]*?\b(?:iş|job)\s+(${JOB_ID})\b`);

/** Statuses that need no notification (the job is still working). */
export const RUNNING_STATUSES = new Set(["running", "queued"]);
/** Open run statuses, same as orkestra's state.OPEN_STATUSES. */
export const OPEN_RUN_STATUSES = new Set(["active", "awaiting_user", "blocked", "paused"]);
const OPEN_JOB_STATUSES = new Set(["running", "queued", "awaiting_user"]);

const STATUS_KEYS = ["running", "queued", "awaiting_user", "done", "blocked", "error", "cancelled", "paused"];

/** The human label of a job status (an unknown status is shown as it is). */
export function statusLabel(status, lang = resolveLang()) {
  return STATUS_KEYS.includes(status) ? t(`label.${status}`, {}, lang) : status;
}

function tasksSuffix(done, total, lang) {
  return total ? t("tasks.suffix", { done: done ?? 0, total }, lang) : "";
}

/**
 * Finds the orkestra job a Telegram reply points at, from the quoted bot message text.
 * @param {string | undefined} quoted
 * @returns {{ project: string, jobId: string } | null}
 */
export function parseJobRef(quoted) {
  if (typeof quoted !== "string" || quoted === "") {
    return null;
  }
  const m = JOB_REF.exec(quoted);
  return m ? { project: m[1], jobId: m[2] } : null;
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// orkestra records a plan approval only for a whole-message approval word (intent.approval), so a chosen
// "a) Onayla — uygulamaya başla" / "a) Approve and start implementing" option is forwarded as the canonical word of
// its language ("onayla" / "approve"; orkestra accepts both). Options with conditions or change requests stay
// verbatim (orkestra then treats them as a change request). Matching works on a folded text (lower case, Turkish
// letters and accents reduced to ASCII, like protocol.fold in Python), so "İPTAL", "iptal" and "Iptal" are one word
// and an English "I approve" is not mangled by Turkish dotless-i lower-casing. Never shrink these vocabularies.
const APPROVAL_TR = /^(onayla|onay|evet|basla|uygula|kabul)\b/;
const APPROVAL_EN = /^(approve|approved|i approve|we approve|yes|go ahead|proceed|lgtm)\b/;
const APPROVAL_HEDGE = /(\?|\bdegil|\bama\b|\bfakat\b|\bancak\b|\bbut\b|\bnot\b|\bno\b|hayir|iptal|vazgec|degistir|revize|duzelt|ekle|cikar|reject|cancel|change|sonra|bekle|\bunless\b|\bexcept\b|\bhowever\b|\bwait\b|\blater\b|\binstead\b|\badd\b|\bremove\b|\bedit\b|\bmodify\b|\brevise\b|\bfix\b)/;
const TURKISH_ASCII = { "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c" };

/** Lower case with Turkish letters (and any other accent) reduced to their ASCII base; punctuation is kept. */
export function fold(text) {
  return String(text ?? "")
    .replace(/[ıİşŞğĞüÜöÖçÇ]/g, (c) => TURKISH_ASCII[c])
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "");
}

// What may follow an approval phrase without changing what is approved ("Approve and start implementing, thanks"). Any
// other word ("skip", "rename", "T002", "postgres") is a condition or a change: that option goes to orkestra verbatim,
// where its own reading of the text decides, instead of being flattened into a bare approval. Folded, whole words.
const APPROVAL_FILLER = new Set([
  "approve", "approved", "i", "we", "yes", "go", "ahead", "proceed", "lgtm", "and", "then", "start", "implementing",
  "implementation", "it", "the", "plan", "spec", "this", "that", "now", "please", "thanks", "thank", "you", "continue",
  "sure", "ok", "okay", "recommended", "default",
  "onayla", "onay", "onayliyorum", "evet", "basla", "baslayin", "baslayalim", "uygula", "kabul", "ve", "uygulamaya",
  "uygulamayi", "simdi", "lutfen", "tesekkurler", "tesekkur", "plani", "bu", "devam", "et", "onerilen", "varsayilan",
]);
// Notes a label may carry: "(Recommended)", and the rollback wording that shares the legacy word "uygula".
const APPROVAL_NOTE = /\s*\((?:recommended|default|onerilen|varsayilan|yalniz orkestra degisiklikleri)\)/g;

/** The canonical approval word for an option text ("onayla" | "approve"), or null when it is not a plain approval. */
function approvalWord(option) {
  const text = fold(option).trim();
  if (APPROVAL_HEDGE.test(text)) {
    return null;
  }
  const word = APPROVAL_TR.test(text) ? "onayla" : APPROVAL_EN.test(text) ? "approve" : null;
  if (!word) {
    return null;
  }
  const rest = text.replace(APPROVAL_NOTE, " ").split(/[^a-z0-9]+/).filter(Boolean);
  return rest.every((w) => APPROVAL_FILLER.has(w)) ? word : null;
}

/** A name folded for matching a project folder: "Namaz Vakitleri", "namaz_vakitleri" and "NAMAZ-VAKITLERI" are one. */
export function projectKey(name) {
  return fold(name).replace(/[^a-z0-9]+/g, "");
}

/**
 * The project folder a spoken or typed name means, among the folders the bridge may start.
 * `{ project }` when the name is a folder, or exactly one folder folds to the same key; otherwise `{ suggestions }`
 * (up to 5 folders whose key contains the name's key or is contained in it). Nothing is ever guessed beyond that.
 * @param {string} name
 * @param {Array<string | { name: string }>} projects
 * @returns {{ project: string } | { suggestions: string[] }}
 */
export function resolveProject(name, projects) {
  const list = (projects ?? []).map((p) => (typeof p === "string" ? p : p?.name)).filter(Boolean);
  const raw = String(name ?? "").trim();
  if (list.includes(raw)) {
    return { project: raw };
  }
  const key = projectKey(raw);
  if (!key) {
    return { suggestions: [] };
  }
  const same = list.filter((p) => projectKey(p) === key);
  if (same.length === 1) {
    return { project: same[0] };
  }
  const near = same.length ? same : list.filter((p) => {
    const k = projectKey(p);
    return k.length >= 3 && (k.includes(key) || key.includes(k));
  });
  return { suggestions: near.slice(0, 5) };
}

/**
 * A bare option letter ("b", "B", "b)", "(b)") becomes "b) <option text>" when the quoted
 * question lists that option, so the answer stays unambiguous. Anything else is sent as typed.
 * @param {string} text the user's reply
 * @param {string | undefined} quoted the orkestra message being replied to
 * @returns {string}
 */
export function expandOptionReply(text, quoted) {
  const reply = String(text ?? "").trim();
  const letterMatch = /^\(?([a-hA-H])[).:]?$/.exec(reply);
  if (!letterMatch || typeof quoted !== "string") {
    return reply;
  }
  const letter = letterMatch[1].toLowerCase();
  const l = escapeRegExp(letter);
  const patterns = [
    // Line-start options: "b) text", "b. text", "(b) text", "- b) text".
    new RegExp(String.raw`^\s*(?:[-*•]\s*)?\(?${l}[).:]\s+(.+?)\s*$`, "im"),
    // Inline options: "... (a) medium (Önerilen) (b) high".
    new RegExp(String.raw`\(${l}\)\s+(.+?)(?=\s+\([a-h]\)\s|\n|$)`, "i"),
  ];
  for (const re of patterns) {
    const m = re.exec(quoted);
    if (m && m[1].trim() !== "") {
      const option = m[1].trim().slice(0, 300);
      return approvalWord(option) ?? `${letter}) ${option}`;
    }
  }
  return reply;
}

/**
 * Same text the bridge would send itself, plus a reply hint while the job waits for the user.
 * @param {{ id: string, project: string, status: string, phase?: string | null, last_message?: string | null }} job
 * @returns {string}
 */
export function formatNotification(job, lang = resolveLang()) {
  const header = `[${job.project}] ${statusLabel(job.status, lang)}` + (job.phase ? ` · ${job.phase}` : "") +
    ` · ${t("job_ref", { id: job.id }, lang)}`;
  const body = (job.last_message ?? "").trim();
  const hint = job.status === "awaiting_user" ? `\n\n${t("notify.reply_hint", {}, lang)}` : "";
  return header + (body ? `\n\n${body}` : "") + hint;
}

/**
 * The open orkestra run of a project, if any. A second run would make orkestra's run skill resume
 * (take over) the existing one, so /yap must refuse while this returns a run.
 * @param {any[]} runs `orkestra watch --json`
 * @param {string} project folder name under the workspace
 */
export function findOpenRun(runs, project) {
  return (runs ?? []).find((r) => OPEN_RUN_STATUSES.has(r.status) &&
    (r.project_name === project || String(r.project ?? "").split("/").pop() === project)) ?? null;
}

/** The phase / status of a run in the reader's language (`watch --json` carries a Turkish twin of each). */
function phaseText(run, lang) {
  return (lang === "tr" && run.phase_tr) || run.phase;
}

function statusText(run, lang) {
  return (lang === "tr" && run.status_tr) || run.status;
}

/** Why /run (/yap) did not start: the project already has an open run. */
export function formatBusyProject(project, run, lang = resolveLang()) {
  const where = run.mode === "headless" ? t("where.telegram_run", {}, lang) : t("where.desktop", {}, lang);
  const tasks = tasksSuffix(run.tasks_done, run.tasks_total, lang);
  return (
    `${t("busy.header", { project, title: run.title }, lang)}\n` +
    `${phaseText(run, lang)}${tasks} · ${where} · ${run.live?.text || statusText(run, lang)}\n` +
    t("busy.footer", {}, lang)
  );
}

/**
 * The truthful `/durum`: every open orkestra run in the workspace (`orkestra watch --json`, including
 * runs started on the desktop) plus the bridge jobs started from Telegram.
 * @param {{ runs?: any[] | null, runsError?: string | null, bridgeJobs?: any[] | null, bridgeError?: string | null }} input
 * @returns {string}
 */
export function formatStatus({ runs, runsError, bridgeJobs, bridgeError }, lang = resolveLang()) {
  const lines = [t("status.title", {}, lang)];
  const open = (runs ?? []).filter((r) => OPEN_RUN_STATUSES.has(r.status));
  const closed = (runs ?? []).filter((r) => !OPEN_RUN_STATUSES.has(r.status));
  if (runsError) {
    lines.push(t("status.runs_error", { error: runsError }, lang));
  } else if (open.length === 0) {
    lines.push(t("status.no_open_runs", {}, lang));
  } else {
    lines.push(t("status.open_runs", { n: open.length }, lang));
    for (const r of open) {
      const tasks = tasksSuffix(r.tasks_done, r.tasks_total, lang);
      const where = r.mode === "headless" ? t("where.telegram_job", {}, lang) : t("where.desktop", {}, lang);
      lines.push(t("status.run_head", { project: r.project_name, title: r.title }, lang));
      lines.push(`  ${phaseText(r, lang)}${tasks} · ${where} · ${r.live?.text || statusText(r, lang)}`);
    }
  }
  if (closed.length > 0) {
    const list = closed.map((r) => `${r.project_name} (${statusText(r, lang)})`).join(", ");
    lines.push(t("status.closed", { list }, lang));
  }
  const jobs = (bridgeJobs ?? []).filter((j) => OPEN_JOB_STATUSES.has(j.status));
  if (bridgeError) {
    lines.push(t("status.jobs_error", { error: bridgeError }, lang));
  } else if (jobs.length === 0) {
    lines.push(t("status.no_jobs", {}, lang));
  } else {
    lines.push(t("status.open_jobs", { n: jobs.length }, lang));
    for (const j of jobs) {
      lines.push(`• ${j.id} · ${j.project} · ${statusLabel(j.status, lang)}` + (j.phase ? ` · ${j.phase}` : ""));
    }
    lines.push(t("status.reply_hint", {}, lang));
  }
  if (open.some((r) => r.mode !== "headless")) {
    lines.push(t("status.desktop_note", {}, lang));
  }
  return lines.join("\n");
}

/**
 * Decides which jobs to announce. Without prior state everything currently known is marked as
 * seen (no flood of old jobs on first start); later, every stopped job whose `updated` changed
 * is announced exactly once.
 * @param {Array<{ id: string, status: string, updated?: string }>} jobs
 * @param {Record<string, string> | null} seen jobId → last announced `updated`
 * @returns {{ announce: typeof jobs, seen: Record<string, string> }}
 */
export function diffJobs(jobs, seen) {
  const next = { ...(seen ?? {}) };
  const announce = [];
  for (const job of jobs) {
    if (!job?.id || !job.updated) {
      continue;
    }
    if (seen === null) {
      next[job.id] = job.updated;
      continue;
    }
    if (RUNNING_STATUSES.has(job.status) || next[job.id] === job.updated) {
      continue;
    }
    announce.push(job);
  }
  return { announce, seen: next };
}

/**
 * Collects message ids from `openclaw message send --json` output (shape-agnostic).
 * @param {string} stdout
 * @returns {string[]}
 */
export function parseSentMessageIds(stdout) {
  const ids = new Set();
  const visit = (node) => {
    if (Array.isArray(node)) {
      node.forEach(visit);
    } else if (node && typeof node === "object") {
      for (const [key, value] of Object.entries(node)) {
        if (/^(message_?id|messageIds?)$/i.test(key)) {
          (Array.isArray(value) ? value : [value]).forEach((v) => {
            if (typeof v === "string" || typeof v === "number") ids.add(String(v));
          });
        } else {
          visit(value);
        }
      }
    }
  };
  try {
    visit(JSON.parse(stdout));
  } catch {
    // Not JSON: no ids; routing still works through the quoted header.
  }
  return [...ids];
}

// ---------------------------------------------------------------- owner command (Telegram passphrase)
// The passphrase itself is never stored: the plugin config holds `{ salt, sha256, words }`, where sha256 is the hex
// digest of `${salt}\n${its folded letters and digits, spaces removed}` (ownerPhraseDigest) and `words` its number of
// words. A message carries the owner command when a run of consecutive words in it (up to `words` + 3 of them) folds
// to the same digest: folding (Turkish letters → ASCII, lower case, no punctuation or spaces) makes
// "Fırat Kocabay emrediyor!", "FiratKocabay Emrediyor" and "firatkocabay emrediyor" the same phrase.

/** The words of a text with their place in it: `[{ word (folded), start, end }]`. */
export function foldedWords(text) {
  const out = [];
  for (const m of String(text ?? "").matchAll(/[\p{L}\p{N}]+/gu)) {
    const word = fold(m[0]).replace(/[^a-z0-9]/g, "");
    if (word) {
      out.push({ word, start: m.index, end: m.index + m[0].length });
    }
  }
  return out;
}

/** The digest the config stores for a passphrase (the set-up script and the tests use it). */
export function ownerPhraseDigest(salt, phrase) {
  const words = foldedWords(phrase).map((w) => w.word);
  return { salt, words: words.length, sha256: createHash("sha256").update(`${salt}\n${words.join("")}`).digest("hex") };
}

/** Whether the owner config is usable. */
export function ownerConfigValid(owner) {
  return Boolean(owner && typeof owner.salt === "string" && owner.salt.length >= 16 &&
    /^[a-f0-9]{64}$/.test(String(owner.sha256 ?? "")) && Number.isInteger(owner.words) && owner.words >= 1 &&
    owner.words <= 12);
}

/**
 * The owner command in a message: `{ request }` (the message without the passphrase and the punctuation around it)
 * or null when the passphrase is not in it.
 */
export function matchOwnerPhrase(text, owner) {
  if (!ownerConfigValid(owner)) {
    return null;
  }
  const raw = String(text ?? "");
  const words = foldedWords(raw);
  for (let i = 0; i < words.length; i += 1) {
    for (let n = 1; n <= owner.words + 3 && i + n <= words.length; n += 1) {
      const span = words.slice(i, i + n);
      const digest = createHash("sha256").update(`${owner.salt}\n${span.map((w) => w.word).join("")}`).digest("hex");
      if (digest !== owner.sha256) {
        continue;
      }
      const before = raw.slice(0, span[0].start).replace(/[\s:;,.!\-–]+$/u, "");
      const after = raw.slice(span[span.length - 1].end).replace(/^[\s:;,.!\-–]+/u, "");
      return { request: [before, after].filter((x) => x.trim()).join(" ").trim() };
    }
  }
  return null;
}

/**
 * Project folders a request names, in the order they appear: "namaz vakitlerinde", "Namaz-Vakitleri'nde" and
 * "namazvakitleri" all name namaz-vakitleri (a Turkish suffix of up to 6 letters may follow the last word).
 * @param {string} text
 * @param {Array<string | { name: string }>} projects
 * @returns {string[]}
 */
export function mentionedProjects(text, projects) {
  const words = foldedWords(text).map((w) => w.word);
  const found = [];
  for (const name of (projects ?? []).map((p) => (typeof p === "string" ? p : p?.name)).filter(Boolean)) {
    const key = projectKey(name);
    if (key.length < 3) {
      continue;
    }
    let at = -1;
    for (let i = 0; i < words.length && at < 0; i += 1) {
      let joined = "";
      for (let j = i; j < Math.min(words.length, i + 4); j += 1) {
        const prev = joined;
        joined += words[j];
        if (joined === key || (joined.startsWith(key) && prev.length < key.length && joined.length - key.length <= 6)) {
          at = i;
          break;
        }
        if (!key.startsWith(joined)) {
          break;
        }
      }
    }
    if (at >= 0) {
      found.push({ name, at });
    }
  }
  return found.sort((a, b) => a.at - b.at).map((f) => f.name);
}

// "ide ayaklandır", "IDE'yi ayağa kaldır", "ide'yi yeniden başlat", "restart the ide", "revive ide", "/ide"
const REVIVE_VERB = /^(ayaklandir|ayaga|kaldir|baslat|yeniden|restart|revive|relaunch|start|reopen|ac|acar|acin)/;

/** Whether a short message asks to bring the IDE and the Orkestra tools back. */
export function isReviveRequest(text) {
  const words = foldedWords(text).map((w) => w.word);
  return words.length >= 1 && words.length <= 8 && words.includes("ide") &&
    (words.length === 1 || words.some((w) => w !== "ide" && REVIVE_VERB.test(w)));
}

/** The answer to a revive: one line per tool with its state. */
export function formatRevive(result, lang = resolveLang()) {
  const states = ["running", "started", "failed", "missing", "invalid", "unsupported"];
  const line = (name, state) => t("revive.line", {
    mark: state === "running" || state === "started" ? "✅" : "⚠️",
    name,
    state: states.includes(state) ? t(`revive.state.${state}`, {}, lang) : String(state),
  }, lang);
  const lines = [t("revive.title", {}, lang)];
  lines.push(line("Orkestra bridge", result?.bridge?.state));
  lines.push(line("Orkestra dashboard", result?.dashboard?.state));
  for (const app of result?.apps ?? []) {
    lines.push(line(app.app, app.state));
  }
  return lines.join("\n");
}
