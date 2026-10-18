"use strict";

// All text a person reads comes from i18n.js (English + Turkish): this file has none of its own.
const I18n = window.OrkestraI18n;
const LANG_KEY = "orkestra.lang";
const $ = (id) => document.getElementById(id);
const state = {
  token: "",
  timer: null,
  flowTimer: null,
  polling: false,
  flowPolling: false,
  enginesLoading: false,
  engineRequestId: 0,
  messages: new Map(),
  lang: I18n.DEFAULT,
  statusKey: "status.disconnected",
  projects: null,
  engines: null,
  jobs: null,
  runs: null,
};
const flowPhaseIds = ["intake", "discover", "clarify", "plan", "approve", "implement", "verify", "review", "deliver", "done"];
const dynamic = new Map(); // element id -> () => text, run again when the language changes

function t(key, params) {
  return I18n.translate(state.lang, key, params);
}

function hasMessage(key) {
  return Object.prototype.hasOwnProperty.call(I18n.MESSAGES[I18n.DEFAULT], key);
}

function locale() {
  return I18n.LOCALES[state.lang] || I18n.LOCALES[I18n.DEFAULT];
}

function readStoredLang() {
  try {
    return window.localStorage.getItem(LANG_KEY);
  } catch (error) {
    return null; // storage blocked: the page still works, the choice just is not remembered
  }
}

function storeLang(code) {
  try {
    window.localStorage.setItem(LANG_KEY, code);
  } catch (error) {
    // not remembered
  }
}

/** Puts `factory()` into element `id` now and again whenever the language changes. */
function bind(id, factory) {
  dynamic.set(id, factory);
  $(id).textContent = factory();
}

function renderRich(node, text) {
  node.replaceChildren();
  text.split("`").forEach((part, index) => {
    if (!part) return;
    if (index % 2) {
      const code = document.createElement("code");
      code.textContent = part;
      node.append(code);
    } else {
      node.append(document.createTextNode(part));
    }
  });
}

function applyStatic() {
  document.documentElement.lang = state.lang;
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-rich]")) renderRich(node, t(node.dataset.i18nRich));
  for (const node of document.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of node.dataset.i18nAttr.split(";")) {
      const [attribute, key] = pair.split(":");
      if (attribute && key) node.setAttribute(attribute.trim(), t(key.trim()));
    }
  }
  const other = I18n.SUPPORTED.find((code) => code !== state.lang);
  $("lang-button").textContent = I18n.LANGUAGE_NAMES[other];
  $("lang-button").lang = other;
}

function relabelSelects() {
  const projectFirst = $("project-select").options[0];
  if (projectFirst && projectFirst.value === "") {
    projectFirst.textContent = state.projects ? t("project.choose") : t("project.loading");
  }
  if (state.engines) {
    for (const option of $("engine-select").options) {
      const engine = (state.engines.engines || []).find((item) => item.id === option.value);
      if (engine) option.textContent = engineOptionLabel(engine);
    }
  }
}

function applyLanguage() {
  applyStatic();
  for (const [id, factory] of dynamic) $(id).textContent = factory();
  relabelSelects();
  if (state.jobs) renderJobs(state.jobs);
  if (state.runs) renderFlows(state.runs);
  updatePhaseTimers();
}

function toggleLanguage() {
  state.lang = I18n.SUPPORTED.find((code) => code !== state.lang);
  storeLang(state.lang);
  applyLanguage();
  if (state.token) {              // the texts the bridge wrote are fetched again in the new language
    refreshFlows();
    loadEngines().catch(() => {});
  }
}

/** ?lang= and the button's stored choice are final; otherwise the bridge's own language (GET /health) decides. */
async function initLanguage() {
  const browser = window.navigator.languages && window.navigator.languages.length
    ? window.navigator.languages
    : [window.navigator.language];
  const search = window.location.search;
  const stored = readStoredLang();
  state.lang = I18n.pickLanguage({ search, stored, browser });
  const explicit = Boolean(I18n.normalize(new URLSearchParams(search).get("lang")) || I18n.normalize(stored));
  bind("connection-status", () => t(state.statusKey));
  bind("flow-summary", () => t("flow.waiting"));
  bind("flows-updated", () => t("flow.updated_none"));
  bind("jobs-updated", () => t("jobs.updated_none"));
  bind("engine-hint", () => t("engine.hint"));
  bind("request-count", requestCountText);
  applyLanguage();
  if (explicit) return;
  try {
    const response = await fetch("/health", { cache: "no-store" });
    const server = I18n.normalize((await response.json()).lang);
    if (server && server !== state.lang) {
      state.lang = server;
      applyLanguage();
    }
  } catch (error) {
    // no health answer: keep the browser language
  }
}

function setStatus(key, connected = false) {
  state.statusKey = key;
  const node = $("connection-status");
  node.textContent = t(key);
  node.classList.toggle("connected", connected);
}

function requestCountText() {
  return `${$("request-input").value.length.toLocaleString(locale())} / ${(10000).toLocaleString(locale())}`;
}

function errorText(error) {
  return String(error.message || "").includes("unauthorized") ? t("status.bad_token") : error.message;
}

/** The page's language travels with every call (?lang=): the bridge writes its own texts (live status, engine reasons,
 * errors) in it, so a page switched to English no longer shows Turkish status lines from a Turkish bridge. */
function withLang(path) {
  return `${path}${path.includes("?") ? "&" : "?"}lang=${encodeURIComponent(state.lang)}`;
}

async function api(path, options = {}) {
  const headers = new Headers({ Authorization: `Bearer ${state.token}` });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(withLang(path), {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
    credentials: "same-origin",
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) throw new Error(payload.error || t("error.http", { status: response.status }));
  return payload;
}

function fillProjects(data) {
  state.projects = data;
  const select = $("project-select");
  select.replaceChildren(new Option(t("project.choose"), ""));
  for (const project of data.projects || []) {
    select.add(new Option(project.name, project.name));
  }
  const count = (data.projects || []).length;
  bind("project-scope", () => {
    if (count === 0) return t("project.none");
    return t(data.restricted ? "project.scope_restricted" : "project.scope_open", { n: count });
  });
  updateStartButton();
}

function engineOptionLabel(engine) {
  return engine.available ? engine.label : t("engine.unavailable", { label: engine.label });
}

function fillEngines(data) {
  state.engines = data;
  const select = $("engine-select");
  const available = (data.engines || []).filter((engine) => engine.available);
  select.replaceChildren();
  for (const engine of data.engines || []) {
    const option = new Option(engineOptionLabel(engine), engine.id);
    option.disabled = !engine.available;
    if (engine.reason) option.title = engine.reason;
    select.add(option);
  }
  const selected = available.find((engine) => engine.id === data.default) || available[0];
  select.value = selected ? selected.id : "";
  const selectedEngine = (data.engines || []).find((engine) => engine.id === select.value);
  $("engine-select").title = selectedEngine && selectedEngine.reason ? selectedEngine.reason : "";
  const unavailable = (data.engines || []).filter((engine) => !engine.available && engine.reason);
  bind("engine-hint", () => t("engine.hint")
    + (unavailable.length
      ? t("engine.unavailable_list", { list: unavailable.map((engine) => `${engine.label}: ${engine.reason}`).join("; ") })
      : ""));
  updateStartButton();
}

async function loadEngines() {
  const project = $("project-select").value;
  const query = project ? `?project=${encodeURIComponent(project)}` : "";
  const requestId = ++state.engineRequestId;
  state.enginesLoading = true;
  updateStartButton();
  try {
    const data = await api(`/engines${query}`);
    if (requestId === state.engineRequestId) fillEngines(data);
  } finally {
    if (requestId === state.engineRequestId) {
      state.enginesLoading = false;
      updateStartButton();
    }
  }
}

async function connect(event) {
  event.preventDefault();
  const entered = $("bridge-token").value.trim();
  $("bridge-token").value = "";
  $("connect-error").hidden = true;
  if (!entered) return;
  state.token = entered;
  try {
    const projects = await api("/projects");
    fillProjects(projects);
    await loadEngines();
    $("connect-panel").hidden = true;
    $("workbench").hidden = false;
    $("disconnect-button").hidden = false;
    setStatus("status.connected", true);
    await Promise.all([refreshJobs(), refreshFlows()]);
    schedulePoll();
    scheduleFlowPoll();
  } catch (error) {
    state.token = "";
    $("connect-error").textContent = errorText(error);
    $("connect-error").hidden = false;
    setStatus("status.failed");
  }
}

function updateStartButton() {
  const engine = [...$("engine-select").options].find((option) => option.value === $("engine-select").value);
  $("start-button").disabled = !state.token
    || state.enginesLoading
    || !$("project-select").value
    || !engine
    || engine.disabled
    || !$("consent-check").checked
    || $("request-input").value.trim().length < 3;
}

function statusClass(status) {
  return String(status || "").replace(/[^a-z0-9_-]/gi, "");
}

function engineLabel(id) {
  return hasMessage(`engine.${id}`) ? t(`engine.${id}`) : t("engine.claude");
}

function makeJobCard(job) {
  const card = document.createElement("article");
  card.className = "job-card";
  card.dataset.jobId = job.id;

  const main = document.createElement("div");
  main.className = "job-main";
  const titleRow = document.createElement("div");
  titleRow.className = "job-title-row";
  const project = document.createElement("span");
  project.className = "job-project";
  const status = document.createElement("span");
  status.className = "job-status";
  status.setAttribute("role", "status");
  titleRow.append(project, status);
  const meta = document.createElement("p");
  meta.className = "job-meta";
  main.append(titleRow, meta);

  const actions = document.createElement("div");
  actions.className = "job-actions";
  const cancel = document.createElement("button");
  cancel.className = "button button-quiet";
  cancel.type = "button";
  cancel.addEventListener("click", () => cancelJob(job.id, cancel));
  actions.append(cancel);

  const conversation = document.createElement("div");
  conversation.className = "chat-log";
  conversation.setAttribute("aria-live", "polite");
  const reply = document.createElement("form");
  reply.className = "reply-form";
  reply.hidden = true;
  const replyInput = document.createElement("textarea");
  replyInput.required = true;
  replyInput.maxLength = 10000;
  replyInput.rows = 2;
  const replyButton = document.createElement("button");
  replyButton.className = "button button-primary";
  replyButton.type = "submit";
  reply.append(replyInput, replyButton);
  reply.addEventListener("submit", (event) => sendReply(event, job.id, replyInput, replyButton));

  const error = document.createElement("p");
  error.className = "job-error";
  error.hidden = true;
  card.append(main, actions, conversation, reply, error);
  return card;
}

/** The fixed labels of a job card, in the current language (run on every render, so a language switch reaches old cards). */
function labelJobCard(card) {
  card.querySelector(".job-actions button").textContent = t("job.stop");
  const input = card.querySelector(".reply-form textarea");
  input.setAttribute("aria-label", t("job.reply_aria"));
  input.placeholder = t("job.reply_placeholder");
  card.querySelector(".reply-form button").textContent = t("job.reply_button");
}

function renderConversation(card, job) {
  const messages = state.messages.get(job.id) || [];
  const lastMessage = typeof job.last_message === "string" ? job.last_message : "";
  if (lastMessage && lastMessage !== card.dataset.lastAgentMessage) {
    messages.push({ role: "agent", text: lastMessage });
    card.dataset.lastAgentMessage = lastMessage;
  }
  const content = document.createDocumentFragment();
  for (const item of messages.slice(-30)) {
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${item.role === "user" ? "user" : "agent"}`;
    const label = document.createElement("b");
    label.textContent = item.role === "user" ? t("job.you") : t("job.orkestra");
    const text = document.createElement("p");
    text.textContent = item.text;
    bubble.append(label, text);
    content.append(bubble);
  }
  const log = card.querySelector(".chat-log");
  log.replaceChildren(content);
  log.hidden = messages.length === 0;
  state.messages.set(job.id, messages.slice(-30));
}

function updateJobCard(card, job) {
  labelJobCard(card);
  const status = hasMessage(`jobstatus.${job.status}`) ? t(`jobstatus.${job.status}`) : job.status || t("status.unknown");
  card.querySelector(".job-project").textContent = job.project || t("job.project");
  const badge = card.querySelector(".job-status");
  badge.textContent = status;
  badge.className = `job-status ${statusClass(job.status)}`;
  const run = job.run_id ? t("job.meta_run", { run: job.run_id }) : "";
  card.querySelector(".job-meta").textContent = t("job.meta", {
    engine: engineLabel(job.engine),
    id: String(job.id || "").slice(0, 8),
    run,
  });
  renderConversation(card, job);
  const canReply = ["awaiting_user", "blocked", "paused", "stalled"].includes(job.status);
  card.querySelector(".reply-form").hidden = !canReply;
  card.querySelector(".job-actions button").hidden = ["done", "error", "cancelled"].includes(job.status);
}

function renderJobs(jobs) {
  const list = $("jobs-list");
  const recent = (jobs || []).slice(-12).reverse();
  const existing = new Map([...list.querySelectorAll(".job-card")].map((card) => [card.dataset.jobId, card]));
  const keep = new Set();
  if (recent.length === 0) {
    list.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = t("jobs.empty");
    list.append(empty);
    return;
  }
  for (const job of recent) {
    if (!job || !job.id) continue;
    keep.add(job.id);
    const card = existing.get(job.id) || makeJobCard(job);
    updateJobCard(card, job);
    list.append(card);
  }
  for (const [id, card] of existing) {
    if (!keep.has(id)) card.remove();
  }
  for (const stale of list.querySelectorAll(":scope > .empty-state")) stale.remove();
}

function flowElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function flowPhaseLabel(id) {
  return t(`phase.${id}`);
}

function flowPhaseIndex(run) {
  const index = Number(run.phase_index);
  if (Number.isInteger(index) && index >= 0 && index < flowPhaseIds.length) return index;
  const found = flowPhaseIds.indexOf(run.phase);
  return Math.max(0, found);
}

function flowUpdatedText(run) {
  const date = run.updated ? new Date(run.updated) : null;
  return date && !Number.isNaN(date.valueOf())
    ? t("flowcard.last_activity", { time: date.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" }) })
    : t("flowcard.run", { id: String(run.run_id || "").slice(0, 10) });
}

function updatePhaseTimers() {
  for (const timer of document.querySelectorAll(".flow-current-duration")) {
    const duration = phaseDuration(timer.dataset.phaseSince);
    timer.textContent = duration ? t("flowcard.in_phase", { duration }) : t("flowcard.current_step");
  }
}

function phaseDuration(phaseSince) {
  const start = Number(phaseSince);
  if (!Number.isFinite(start) || start <= 0) return "";
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - start));
  if (elapsed < 60) return t("duration.sec", { n: elapsed });
  if (elapsed < 3600) return t("duration.min", { n: Math.floor(elapsed / 60) });
  return t("duration.hour", { h: Math.floor(elapsed / 3600), m: Math.floor((elapsed % 3600) / 60) });
}

function agentProfile(name) {
  const key = String(name || "agent").toLowerCase().split(":").pop();
  if (hasMessage(`agent.${key}`)) return [t(`agent.${key}`), t(`agent.${key}.desc`)];
  return [key.replace(/[-_]/g, " "), t("agent.other.desc")];
}

function flowGate(label, value) {
  const text = typeof value === "string" ? value : "";
  const shown = text === "" || text === "\u2014" ? t("flowcard.gate_none") : text;
  const gate = flowElement("div", "flow-gate");
  gate.append(flowElement("span", "", label), flowElement("b", "", shown));
  return gate;
}

function renderAgentNetwork(run) {
  const panel = flowElement("section", "flow-subpanel");
  panel.setAttribute("aria-label", t("network.aria"));
  const agents = Array.isArray(run.agents)
    ? [...new Set(run.agents.filter((agent) => typeof agent === "string" && agent.trim()).map((agent) => agent.trim()))]
    : [];
  const tasks = Array.isArray(run.current_tasks) ? run.current_tasks.slice(0, 4) : [];
  const tasksTotal = Number(run.tasks_total) || 0;
  const tasksDone = Number(run.tasks_done) || 0;
  const heading = flowElement("div", "flow-subheading");
  heading.append(flowElement("h3", "", t("network.heading")));
  heading.append(flowElement("span", "", agents.length ? t("network.records", { n: agents.length }) : t("network.current_team")));
  panel.append(heading);

  const network = flowElement("div", "agent-network");
  const orchestrator = flowElement("div", "orchestrator-node");
  orchestrator.append(flowElement("span", "orchestrator-glyph", "O"));
  const orchestratorCopy = flowElement("div", "orchestrator-copy");
  orchestratorCopy.append(
    flowElement("b", "", t("network.orchestrator")),
    flowElement("span", "", t("network.orchestrator_desc")),
  );
  orchestrator.append(orchestratorCopy);
  network.append(orchestrator, flowElement("div", "agent-connector"));

  const agentGrid = flowElement("div", "flow-agent-grid");
  if (agents.length) {
    for (const name of agents.slice(0, 8)) {
      const [label, description] = agentProfile(name);
      const task = tasks.find((item) => String(item.agent || "").split(":").pop() === name.split(":").pop());
      const card = flowElement("div", "flow-agent-card");
      const initials = label.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toLocaleUpperCase(locale());
      card.append(flowElement("span", "agent-glyph", initials || "A"));
      const copy = flowElement("div", "agent-copy");
      copy.append(
        flowElement("b", "", label),
        flowElement("span", "", task ? `${task.id || t("network.task")} · ${task.title || description}` : description),
      );
      card.append(copy);
      agentGrid.append(card);
    }
  } else {
    const detail = run.status === "awaiting_user" ? t("network.waiting") : t("network.none");
    agentGrid.append(flowElement("div", "flow-agent-empty", detail));
  }
  network.append(agentGrid);
  if (agents.length) {
    network.append(flowElement("p", "flow-agent-note", t("network.note")));
  }
  panel.append(network);

  if (tasks.length) {
    const taskList = flowElement("div", "flow-current-tasks");
    for (const task of tasks) {
      const row = flowElement("div", "flow-task");
      row.append(flowElement("span", "flow-task-id", task.id || t("network.task")));
      const copy = flowElement("div", "flow-task-copy");
      copy.append(
        flowElement("b", "", task.title || t("network.active_task")),
        flowElement("span", "", task.agent ? t("network.assigned", { agent: agentProfile(task.agent)[0] }) : t("network.task_assigned")),
      );
      row.append(copy);
      taskList.append(row);
    }
    panel.append(taskList);
  } else if (tasksTotal) {
    panel.append(flowElement("p", "flow-agent-note", t("network.progress", { done: tasksDone, total: tasksTotal })));
  }
  return panel;
}

function activityLabel(kind) {
  return hasMessage(`activity.${kind}`) ? t(`activity.${kind}`) : "";
}

function renderFlowActivity(run) {
  const panel = flowElement("section", "flow-subpanel");
  panel.setAttribute("aria-label", t("events.aria"));
  const heading = flowElement("div", "flow-subheading");
  heading.append(
    flowElement("h3", "", t("events.heading")),
    flowElement("span", "", t("events.tag")),
  );
  panel.append(heading);

  const entries = Array.isArray(run.activity) ? run.activity.slice(-6).reverse() : [];
  if (!entries.length) {
    panel.append(flowElement("div", "flow-agent-empty", t("events.empty")));
    return panel;
  }
  const list = flowElement("ol", "flow-event-list flow-activity");
  list.setAttribute("aria-label", t("events.list_aria"));
  for (const entry of entries) {
    const kind = typeof entry.kind === "string" ? entry.kind : "";
    const row = flowElement("li", `flow-event${kind === "deny" ? " is-deny" : ""}${["user", "answer"].includes(kind) ? ` is-${kind}` : ""}`);
    const time = flowElement("time", "flow-event-time", entry.time || "");
    if (entry.time) time.dateTime = entry.time;
    const body = flowElement("div", "flow-event-body");
    const meta = flowElement("div", "flow-event-meta");
    const agent = flowElement("span", "flow-event-agent", agentProfile(entry.agent || "main")[0]);
    meta.append(agent, flowElement("span", "flow-event-kind", activityLabel(kind) || kind || t("activity.other")));
    if (entry.tool) meta.append(flowElement("span", "flow-event-tool", entry.tool));
    const description = flowElement("p", "flow-event-description", entry.what || entry.tool || activityLabel(kind) || t("activity.recorded"));
    description.title = description.textContent;
    body.append(meta, description);
    row.append(time, body);
    list.append(row);
  }
  panel.append(list);
  return panel;
}

function buildFlowCard(run) {
  const card = flowElement("article", "flow-card");
  card.dataset.status = statusClass(run.status);
  card.dataset.runId = String(run.run_id || "");
  const inner = flowElement("div", "flow-card-inner");
  const header = flowElement("div", "flow-card-header");
  const identity = flowElement("div", "flow-identity");
  const projectLine = flowElement("div", "flow-project-line");
  projectLine.append(flowElement("span", "flow-project-name", run.project_name || t("job.project")));
  projectLine.append(flowElement("span", "flow-run-id", `run ${String(run.run_id || "").slice(0, 10)}`));
  projectLine.append(flowElement("span", "flow-engine-tag", engineLabel(run.engine)));
  identity.append(projectLine, flowElement("p", "flow-title", run.title || t("flowcard.untitled")));
  const statusText = hasMessage(`flowstatus.${run.status}`) ? t(`flowstatus.${run.status}`) : run.status || t("status.unknown");
  header.append(identity, flowElement("span", "flow-status-pill", statusText));
  inner.append(header);

  const completed = run.status === "done" || run.phase === "done";
  const phaseIndex = completed ? flowPhaseIds.length - 1 : flowPhaseIndex(run);
  const progress = completed ? 100 : Math.round((phaseIndex / (flowPhaseIds.length - 1)) * 100);
  const currentPhase = flowPhaseLabel(flowPhaseIds[phaseIndex] || flowPhaseIds[0]);
  const progressRow = flowElement("div", "flow-progress-row");
  progressRow.append(
    flowElement("span", "", t("flowcard.now", { phase: currentPhase })),
    flowElement("strong", "", t("flowcard.phase_count", { i: completed ? flowPhaseIds.length : phaseIndex + 1, n: flowPhaseIds.length })),
  );
  inner.append(progressRow);
  const progressTrack = flowElement("progress", "flow-progress-track");
  progressTrack.max = 100;
  progressTrack.value = progress;
  progressTrack.setAttribute("aria-label", t("flowcard.progress_aria"));
  inner.append(progressTrack);

  const scroll = flowElement("div", "flow-stage-scroll");
  scroll.tabIndex = 0;
  scroll.setAttribute("role", "group");
  scroll.setAttribute("aria-label", t("flowcard.stages_aria"));
  const stageList = flowElement("ol", "flow-stage-list");
  for (let index = 0; index < flowPhaseIds.length; index += 1) {
    const label = flowPhaseLabel(flowPhaseIds[index]);
    const step = flowElement("li", "flow-stage");
    step.setAttribute("role", "listitem");
    if (index < phaseIndex || (completed && index <= phaseIndex)) step.classList.add("is-complete");
    else if (index === phaseIndex) {
      step.classList.add("is-current");
      step.setAttribute("aria-current", "step");
    }
    const marker = index < phaseIndex || (completed && index <= phaseIndex) ? "✓" : String(index + 1).padStart(2, "0");
    step.append(flowElement("span", "flow-stage-dot", marker), flowElement("span", "flow-stage-label", label));
    step.title = `${label}${index === phaseIndex ? t("flowcard.current_phase") : ""}`;
    stageList.append(step);
  }
  scroll.append(stageList);
  inner.append(scroll);

  const current = flowElement("div", "flow-current");
  current.append(flowElement("span", "flow-current-mark", completed ? "✓" : String(phaseIndex + 1).padStart(2, "0")));
  const currentCopy = flowElement("div", "flow-current-copy");
  const liveText = run.live && typeof run.live.text === "string" ? run.live.text : "";
  const currentHeading = flowElement("b", "", currentPhase);
  const duration = flowElement("span", "flow-current-duration");
  duration.dataset.phaseSince = String(run.phase_since || "");
  currentHeading.append(document.createTextNode(" · "), duration);
  currentCopy.append(currentHeading, flowElement("p", "", liveText || (completed ? t("flowcard.run_done") : t("flowcard.live_fallback"))));
  current.append(currentCopy);
  inner.append(current);

  const lower = flowElement("div", "flow-lower-grid");
  lower.append(renderAgentNetwork(run), renderFlowActivity(run));
  inner.append(lower);

  const gateRow = flowElement("div", "flow-gates");
  const approveIndex = flowPhaseIds.indexOf("approve");
  const approval = run.approved
    ? t("flowcard.approved")
    : phaseIndex < approveIndex
      ? t("flowcard.not_requested")
      : run.status === "awaiting_user"
        ? t("flowcard.waiting_you")
        : t("flowcard.pending");
  const gates = run.gates || {};
  gateRow.append(
    flowGate(t("flowcard.gate_approval"), approval),
    flowGate(t("flowcard.gate_baseline"), gates.baseline),
    flowGate(t("flowcard.gate_regression"), gates.verify),
  );
  inner.append(gateRow);

  const footer = flowElement("div", "flow-run-foot");
  footer.append(
    flowElement("span", "", flowUpdatedText(run)),
    flowElement("span", "", Number(run.tasks_total)
      ? t("flowcard.tasks_done", { done: Number(run.tasks_done) || 0, total: Number(run.tasks_total) })
      : t("flowcard.no_task_plan")),
  );
  card.append(inner, footer);
  return card;
}

function flowSignature(run) {
  return JSON.stringify([
    state.lang, run.updated, run.status, run.phase, run.phase_index, run.approved, run.live,
    run.agents, run.current_tasks, run.tasks_done, run.tasks_total, run.gates, run.activity,
  ]);
}

function renderFlows(runs) {
  const openStatuses = new Set(["active", "awaiting_user", "blocked", "paused"]);
  const finishedStatuses = new Set(["done", "error", "aborted"]);
  const timeValue = (run) => {
    const value = Date.parse(run.updated || "");
    return Number.isNaN(value) ? 0 : value;
  };
  const valid = (Array.isArray(runs) ? runs : []).filter((run) => run && typeof run === "object");
  const open = valid.filter((run) => openStatuses.has(run.status))
    .sort((left, right) => timeValue(right) - timeValue(left));
  const recent = valid.filter((run) => finishedStatuses.has(run.status))
    .sort((left, right) => timeValue(right) - timeValue(left))
    .slice(0, 4);
  const visible = [...open, ...recent];
  bind("flow-summary", () => (open.length
    ? t("flow.summary_open", { n: open.length }) + (recent.length ? t("flow.summary_recent", { n: recent.length }) : "")
    : recent.length
      ? t("flow.summary_idle", { n: recent.length })
      : t("flow.summary_none")));
  $("flow-summary").parentElement.classList.toggle("has-active", open.length > 0);

  const list = $("flows-list");
  const existing = new Map([...list.querySelectorAll(".flow-card")].map((card) => [card.dataset.runId, card]));
  const retained = new Set();
  if (!visible.length) {
    list.replaceChildren();
    const empty = flowElement("div", "empty-state flow-empty");
    const orbit = flowElement("span", "empty-orbit");
    orbit.setAttribute("aria-hidden", "true");
    orbit.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
    empty.append(orbit, flowElement("b", "", t("flow.empty_title")));
    empty.append(flowElement("span", "", t("flow.empty_body")));
    list.append(empty);
    return;
  }

  for (const run of visible) {
    const runId = String(run.run_id || `${run.project_name || "run"}-${run.updated || ""}`);
    retained.add(runId);
    const previous = existing.get(runId);
    const signature = flowSignature(run);
    if (previous && previous.dataset.signature === signature) {
      list.append(previous);
      continue;
    }
    const card = buildFlowCard(run);
    card.dataset.runId = runId;
    card.dataset.signature = signature;
    if (previous) {
      const oldScroll = previous.querySelector(".flow-stage-scroll");
      const newScroll = card.querySelector(".flow-stage-scroll");
      if (oldScroll && newScroll) newScroll.scrollLeft = oldScroll.scrollLeft;
      previous.replaceWith(card);
    } else {
      list.append(card);
    }
  }
  for (const [runId, card] of existing) {
    if (!retained.has(runId)) card.remove();
  }
  for (const stale of list.querySelectorAll(":scope > .flow-empty")) stale.remove();
}

async function refreshJobs() {
  if (!state.token || state.polling) return;
  state.polling = true;
  try {
    const data = await api("/jobs");
    state.jobs = data.jobs;
    renderJobs(data.jobs);
    const when = new Date();
    bind("jobs-updated", () => t("jobs.updated", { time: when.toLocaleTimeString(locale()) }));
  } catch (error) {
    bind("jobs-updated", () => t("jobs.refresh_failed"));
    const list = $("jobs-list");
    if (!list.querySelector(".job-card")) {
      list.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = t("jobs.refresh_failed");
      list.append(empty);
    }
    if (error.message.includes("unauthorized")) setStatus("status.bad_token");
  } finally {
    state.polling = false;
  }
}

async function refreshFlows() {
  if (!state.token || state.flowPolling) return;
  state.flowPolling = true;
  try {
    const data = await api("/runs");
    state.runs = data.runs;
    renderFlows(data.runs);
    updatePhaseTimers();
    const when = new Date();
    bind("flows-updated", () => t("flow.updated", {
      time: when.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
    }));
  } catch (error) {
    bind("flows-updated", () => t("flow.refresh_failed", { error: error.message }));
    if (error.message.includes("unauthorized")) setStatus("status.bad_token");
  } finally {
    state.flowPolling = false;
  }
}

function schedulePoll() {
  clearTimeout(state.timer);
  if (!state.token) return;
  state.timer = setTimeout(async () => {
    await refreshJobs();
    schedulePoll();
  }, 2500);
}

function scheduleFlowPoll() {
  clearTimeout(state.flowTimer);
  if (!state.token) return;
  state.flowTimer = setTimeout(async () => {
    await refreshFlows();
    scheduleFlowPoll();
  }, 6000);
}

async function startJob(event) {
  event.preventDefault();
  $("job-error").hidden = true;
  const request = $("request-input").value.trim();
  const button = $("start-button");
  button.disabled = true;
  try {
    const job = await api("/jobs", {
      method: "POST",
      body: {
        project: $("project-select").value,
        request,
        engine: $("engine-select").value,
        source: "web-ui",
      },
    });
    state.messages.set(job.id, [{ role: "user", text: request }]);
    $("request-input").value = "";
    $("consent-check").checked = false;
    $("request-count").textContent = requestCountText();
    await Promise.all([refreshJobs(), refreshFlows()]);
  } catch (error) {
    $("job-error").textContent = error.message;
    $("job-error").hidden = false;
  } finally {
    updateStartButton();
  }
}

async function sendReply(event, id, input, button) {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  button.disabled = true;
  const card = button.closest(".job-card");
  const error = card.querySelector(".job-error");
  error.hidden = true;
  try {
    await api(`/jobs/${encodeURIComponent(id)}/reply`, { method: "POST", body: { text } });
    const messages = state.messages.get(id) || [];
    messages.push({ role: "user", text });
    state.messages.set(id, messages.slice(-30));
    input.value = "";
    await Promise.all([refreshJobs(), refreshFlows()]);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function cancelJob(id, button) {
  if (!window.confirm(t("job.confirm_stop"))) return;
  button.disabled = true;
  const card = button.closest(".job-card");
  const error = card.querySelector(".job-error");
  error.hidden = true;
  try {
    await api(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} });
    await Promise.all([refreshJobs(), refreshFlows()]);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function disconnect() {
  state.token = "";
  state.jobs = null;
  state.runs = null;
  clearTimeout(state.timer);
  clearTimeout(state.flowTimer);
  state.flowPolling = false;
  state.engineRequestId += 1;
  state.enginesLoading = false;
  state.messages.clear();
  $("workbench").hidden = true;
  $("connect-panel").hidden = false;
  $("disconnect-button").hidden = true;
  $("jobs-list").replaceChildren();
  $("flows-list").replaceChildren();
  $("bridge-token").value = "";
  $("connect-error").hidden = true;
  setStatus("status.disconnected");
}

$("connect-form").addEventListener("submit", connect);
$("job-form").addEventListener("submit", startJob);
$("project-select").addEventListener("change", () => {
  loadEngines().catch((error) => {
    $("job-error").textContent = error.message;
    $("job-error").hidden = false;
  });
});
$("request-input").addEventListener("input", () => {
  $("request-count").textContent = requestCountText();
  updateStartButton();
});
$("engine-select").addEventListener("change", updateStartButton);
$("consent-check").addEventListener("change", updateStartButton);
$("refresh-button").addEventListener("click", () => Promise.all([refreshJobs(), refreshFlows()]));
$("disconnect-button").addEventListener("click", disconnect);
$("lang-button").addEventListener("click", toggleLanguage);
initLanguage();
