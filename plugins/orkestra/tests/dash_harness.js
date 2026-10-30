// Test harness for the dashboard page script (used by test_i18n_monitor.py and test_dashboard_ux.py).
// It runs the REAL script of the served page in node against a small DOM, a controllable clock and a fake server:
//   node dash_harness.js <scenario.json>
// scenario = {page: path, options: {...}, steps: [{op, ...}]}. Every step that returns something appends to `out`.
//   op "advance"  ms          move the fake clock (timers fire in order, promises settle in between)
//   op "set"      key, value  change the fake server: views | fail | status | headers | latency
//   op "eval"     code, as    run JS in the page's global scope (S, L, OrkDash ... are visible); result stored as `as`
//   op "snap"     as          the standard snapshot of the page (sections, chips, banners, connection chip, title)
//   op "fire"     target, type, props   dispatch an event ("#id" | "key:<card key>" | "doc" | "win")
const fs = require("fs");
const vm = require("vm");

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const opt = scenario.options || {};
const html = fs.readFileSync(scenario.page, "utf8");

// ------------------------------------------------------------------ clock and timers
const clock = { now: opt.now || Date.now() };
let timerSeq = 0;
const timers = new Map();
global.setTimeout = (fn, ms = 0) => { const id = ++timerSeq; timers.set(id, { at: clock.now + Math.max(0, ms), fn, every: 0 }); return id; };
global.clearTimeout = id => { timers.delete(id); };
global.setInterval = (fn, ms = 0) => { const id = ++timerSeq; timers.set(id, { at: clock.now + ms, fn, every: Math.max(1, ms) }); return id; };
global.clearInterval = global.clearTimeout;
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...a) { if (a.length) super(...a); else super(clock.now); }
  static now() { return clock.now; }
};
let seed = 12345; Math.random = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };   // repeatable jitter
const settle = () => new Promise(r => setImmediate(r));
async function advance(ms) {
  const target = clock.now + ms;
  for (;;) {
    await settle();
    let nextId = 0, next = null;
    for (const [id, t] of timers) if (t.at <= target && (!next || t.at < next.at)) { next = t; nextId = id; }
    if (!next) break;
    clock.now = Math.max(clock.now, next.at);
    if (next.every) next.at += next.every; else timers.delete(nextId);
    try { next.fn(); } catch (e) { errors.push(String(e && e.stack || e)); }
  }
  clock.now = target;
  await settle();
}
const errors = [];

// ------------------------------------------------------------------ DOM
const mut = { n: 0, log: [] };
function bump(el, kind, node) { mut.n += 1; mut.log.push({ el, kind, node }); }
class TextNode {
  constructor(s) { this.nodeType = 3; this.data = String(s); this.parentNode = null; }
  get textContent() { return this.data; }
  contains(n) { return n === this; }
}
const DETAILS_OPEN = new WeakMap();
class El {
  constructor(tag) {
    this.nodeType = 1; this.tag = tag; this.tagName = tag.toUpperCase(); this.childNodes = []; this.parentNode = null;
    this.attrs = {}; this.dataset = {}; this.style = {}; this.listeners = {}; this._class = ""; this._hidden = false;
    this._title = ""; this._id = ""; this._open = false; this.tabIndex = -1; this.type = ""; this.value = ""; this.checked = false;
    const self = this;
    this.classList = {
      add: (...c) => { const s = new Set(self._class.split(/\s+/).filter(Boolean)); c.forEach(x => s.add(x)); self.className = [...s].join(" "); },
      remove: (...c) => { const s = new Set(self._class.split(/\s+/).filter(Boolean)); c.forEach(x => s.delete(x)); self.className = [...s].join(" "); },
      toggle: (c, f) => { const has = self._class.split(/\s+/).includes(c); const want = f === undefined ? !has : f; self.classList[want ? "add" : "remove"](c); return want; },
      contains: c => self._class.split(/\s+/).includes(c),
    };
  }
  get className() { return this._class; }
  set className(v) { v = String(v); if (v !== this._class) { this._class = v; bump(this, "class"); } }
  get hidden() { return this._hidden; }
  set hidden(v) { v = !!v; if (v !== this._hidden) { this._hidden = v; bump(this, "hidden"); } }
  get title() { return this._title; }
  set title(v) { this._title = String(v); bump(this, "title"); }
  get id() { return this._id; }
  set id(v) { this._id = String(v); bump(this, "id"); }
  get open() { return this._open; }
  set open(v) {
    v = !!v; if (v === this._open) return; this._open = v; bump(this, "open");
    if (this.tag === "details") queueMicrotask(() => this.fire("toggle", {}));   // the browser fires it asynchronously
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() { const p = this.parentNode; if (!p) return null; const i = p.childNodes.indexOf(this); return p.childNodes[i + 1] || null; }
  get previousSibling() { const p = this.parentNode; if (!p) return null; const i = p.childNodes.indexOf(this); return i > 0 ? p.childNodes[i - 1] : null; }
  get textContent() { return this.childNodes.map(n => n.textContent).join(""); }
  set textContent(v) {
    v = String(v); this.childNodes.forEach(n => { n.parentNode = null; }); this.childNodes = [];
    if (v !== "") { const t = new TextNode(v); t.parentNode = this; this.childNodes.push(t); }
    bump(this, "text");
  }
  _adopt(n) {
    if (typeof n === "string" || typeof n === "number") n = new TextNode(n);
    if (n.parentNode) { const p = n.parentNode; p.childNodes.splice(p.childNodes.indexOf(n), 1); }
    n.parentNode = this; return n;
  }
  append(...ns) { ns.forEach(n => { n = this._adopt(n); this.childNodes.push(n); bump(this, "append", n); }); }
  appendChild(n) { this.append(n); return n; }
  insertBefore(n, ref) {
    n = this._adopt(n);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n);
    bump(this, "insert", n); return n;
  }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) { this.childNodes.splice(i, 1); n.parentNode = null; bump(this, "remove"); } return n; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...ns) { this.childNodes.forEach(n => { n.parentNode = null; }); this.childNodes = []; ns.forEach(n => { n = this._adopt(n); this.childNodes.push(n); }); bump(this, "replace"); }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  setAttribute(k, v) { v = String(v); this.attrs[k] = v; if (k === "id") this._id = v; if (k === "class") this._class = v; if (k === "title") this._title = v; bump(this, "attr:" + k); }
  getAttribute(k) {
    if (k === "title" && this._title) return this._title;
    if (k === "id") return this._id || (k in this.attrs ? this.attrs[k] : null);
    return k in this.attrs ? this.attrs[k] : null;
  }
  removeAttribute(k) { if (k in this.attrs) { delete this.attrs[k]; bump(this, "attr:" + k); } }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(type, props) {
    const ev = Object.assign({ type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, props || {});
    let n = this;
    while (n) { (n.listeners && n.listeners[type] || []).forEach(fn => fn(ev)); if (!n.parentNode && n !== document.body) break; n = n.parentNode; }
    return ev;
  }
  click() { this.fire("click", {}); }
  focus() { document.activeElement = this; this.fire("focus", {}); }
  blur() { if (document.activeElement === this) document.activeElement = document.body; }
  scrollIntoView() {}
  getBoundingClientRect() {
    const r = this._rect || {};
    const o = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    return Object.assign(o, r, { height: r.height !== undefined ? r.height : (r.bottom || 0) - (r.top || 0) });
  }
}
const byId = {};
function seedText(id, el) {
  const m = new RegExp('id="' + id + '"[^>]*>([^<]*)<').exec(html);
  if (m) { const t = new TextNode(m[1].replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#x27;/g, "'")); t.parentNode = el; el.childNodes.push(t); }
}
const labelsJson = html.match(/<script type="application\/json" id="labels">([\s\S]*?)<\/script>/)[1];
const docBody = new El("body");
let titleWrites = 0, titleText = (html.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || "";
const doc = {
  body: docBody, activeElement: docBody, hidden: !!opt.hidden,
  get title() { return titleText; }, set title(v) { titleWrites += 1; titleText = String(v); },
  documentElement: { lang: (html.match(/<html lang="([^"]*)"/) || [])[1] || "en", style: { props: {}, setProperty(k, v) { this.props[k] = v; } } },
  listeners: {},
  createElement: t => new El(t),
  createTextNode: s => new TextNode(s),
  createRange: () => ({ selectNodeContents(n) { this.node = n; } }),
  getElementById(id) {
    if (!byId[id]) {
      const e = new El(id === "labels" ? "script" : "div"); e._id = id; byId[id] = e;
      if (id === "labels") e.childNodes.push(Object.assign(new TextNode(labelsJson), { parentNode: e }));
      else seedText(id, e);
      if (id === "stale" || id === "offline" || id === "rerr" || id === "help") e._hidden = true;
      if (id === "keys-on") e.checked = true;
      if (id === "main") docBody.append(e);
    }
    return byId[id];
  },
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
  fire(type, props) { const ev = Object.assign({ type, target: docBody, preventDefault() { this.defaultPrevented = true; } }, props || {}); (this.listeners[type] || []).forEach(fn => fn(ev)); return ev; },
};
docBody.parentNode = null;
global.document = doc;
const winListeners = {};
global.window = global;
global.addEventListener = (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); };
const sel = { isCollapsed: true, anchorNode: null, _text: "", removeAllRanges() { this.isCollapsed = true; this.anchorNode = null; }, addRange() {}, toString() { return this._text; } };
global.getSelection = () => sel;
let reloads = 0; const replaced = [];
global.location = { search: opt.search || "", href: "http://127.0.0.1:1/" + (opt.search || ""), reload() { reloads += 1; }, replace(u) { replaced.push(u); } };
function storage(throwing) {
  const m = {};
  return { getItem(k) { if (throwing) throw new Error("blocked"); return k in m ? m[k] : null; },
           setItem(k, v) { if (throwing) throw new Error("blocked"); m[k] = String(v); }, removeItem(k) { delete m[k]; }, _m: m };
}
global.localStorage = storage(opt.storage === "throw");
global.sessionStorage = storage(opt.storage === "throw");
if (opt.local) Object.entries(opt.local).forEach(([k, v]) => localStorage._m[k] = v);
const scrolls = [];
global.scrollBy = (x, y) => { scrolls.push([x, y]); };
global.innerHeight = opt.innerHeight || 800;
global.getComputedStyle = n => ({ position: n._pos || "static" });
const clipboard = []; const consoleErrors = [];
Object.defineProperty(global, "navigator", { value: { clipboard: opt.noClipboard ? undefined : { writeText: t => { clipboard.push(t); return Promise.resolve(); } } }, configurable: true });
global.console = Object.assign({}, console, { error: (...a) => consoleErrors.push(a.map(String).join(" ")) });
global.AbortController = class { constructor() { const l = []; this.signal = { aborted: false, addEventListener: (t, f) => l.push(f) }; this._l = l; }
  abort() { this.signal.aborted = true; this._l.forEach(f => f()); } };

// ------------------------------------------------------------------ the fake server
const sc = { views: [], fail: false, status: opt.status || 200, headers: {}, latency: 0, calls: [], inflight: 0, maxInflight: 0,
             control: { ok: true, status: "paused", changed: true, stopped: 1 }, controlStatus: 200, controlFail: false, posts: [] };
global.fetch = (url, init = {}) => new Promise((resolve, reject) => {
  if (init.method === "POST") {      // the person's clicks: answered from sc.answers[path] (else sc.control), recorded in sc.posts
    sc.posts.push({ url, at: clock.now, headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null });
    const path = String(url).split("?")[0], answer = (sc.answers || {})[path];
    setTimeout(() => {
      if (sc.controlFail) return reject(new Error("down"));
      const body = answer !== undefined ? answer : sc.control;
      resolve({ ok: sc.controlStatus < 400, status: sc.controlStatus, headers: { get: () => null },
                json: async () => JSON.parse(JSON.stringify(body)) });
    }, sc.latency);
    return;
  }
  if (String(url).indexOf("/api/console/") === 0) {      // console reads: sc.console[path], recorded in sc.gets (not a poll)
    sc.gets = sc.gets || [];
    sc.gets.push(url);
    const path = String(url).split("?")[0], body = (sc.console || {})[path];
    setTimeout(() => resolve({ ok: body !== undefined, status: body !== undefined ? 200 : 404, headers: { get: () => null },
                               json: async () => JSON.parse(JSON.stringify(body !== undefined ? body : { ok: false, error: "not_found" })) }),
               sc.latency);
    return;
  }
  sc.calls.push({ url, at: clock.now }); sc.inflight += 1; sc.maxInflight = Math.max(sc.maxInflight, sc.inflight);
  let done = false;
  const finish = fn => { if (done) return; done = true; sc.inflight -= 1; fn(); };
  if (init.signal) init.signal.addEventListener("abort", () => finish(() => reject(Object.assign(new Error("aborted"), { name: "AbortError" }))));
  setTimeout(() => finish(() => {
    if (sc.fail) return reject(new Error("down"));
    const hdr = Object.assign({ "X-Orkestra-Time": String(clock.now / 1000) }, sc.headers);
    resolve({ ok: sc.status < 400, status: sc.status, headers: { get: n => (n in hdr ? hdr[n] : null) },
              json: async () => JSON.parse(JSON.stringify(sc.views)) });
  }), sc.latency);
});

// ------------------------------------------------------------------ helpers visible to the steps
function walk(root, fn) { fn(root); (root.childNodes || []).forEach(c => c.nodeType === 1 && walk(c, fn)); }
function find(root, pred) { const out = []; walk(root, n => { if (pred(n)) out.push(n); }); return out; }
function cardKey(n) { while (n) { if (n.dataset && n.dataset.key) return n.dataset.key; n = n.parentNode; } return null; }
global.H = {
  get titleWrites() { return titleWrites; }, scrolls, settle, find, walk, cardKey, byId: id => doc.getElementById(id), sc, clock, mut, sel, clipboard, errors, consoleErrors,
  card(key) { return find(doc.getElementById("main"), n => n.tag === "article" && n.dataset.key === key)[0]; },
  item(key) { return find(doc.getElementById("main"), n => n.dataset && n.dataset.key === key)[0]; },
  select(node, from, to) { sel.anchorNode = node.firstChild || node; sel.isCollapsed = false; sel._text = node.textContent.slice(from, to); doc.fire("selectionchange"); },
  collapse() { sel.isCollapsed = true; sel.anchorNode = null; sel._text = ""; doc.fire("selectionchange"); },
  mutationsUnder(root) { return mut.log.filter(m => root.contains(m.el)); },
  replaced, reloads: () => reloads, winFire(type) { (winListeners[type] || []).forEach(f => f({ type })); },
  nodes() { return find(doc.getElementById("main"), () => true).length; },
};
function snapshot() {
  const main = doc.getElementById("main");
  const sections = main.children.filter(c => c.tag === "section").map(sec => {
    const list = sec.children.find(c => c.tag === "div");
    return { g: sec.dataset.g, title: sec.children[0].textContent, count: sec.children[0].children[2].textContent,
             items: list.children.map(c => ({ key: c.dataset.key, kind: c.tag === "article" ? "card" : "item", text: c.textContent })),
             more: sec.children[2] && !sec.children[2].hidden ? sec.children[2].textContent : "" };
  });
  const chips = doc.getElementById("sum").children.map(b => ({ g: b.dataset.g, text: b.textContent, pressed: b.getAttribute("aria-pressed"), n: b.getAttribute("data-n") }));
  const stale = doc.getElementById("stale");
  return {
    sections, chips, title: doc.title, calm: doc.getElementById("calm").textContent,
    other: main.children.filter(c => c.tag !== "section").map(c => c.textContent),
    conn: doc.getElementById("conn-text").textContent, connState: doc.getElementById("conn").getAttribute("data-state"),
    banners: { stale: !stale.hidden, staleMsg: doc.getElementById("stale-msg").textContent, offline: !doc.getElementById("offline").hidden,
               offlineText: doc.getElementById("offline-msg").textContent, rerr: !doc.getElementById("rerr").hidden,
               rerrText: doc.getElementById("rerr-msg").textContent },
    live: doc.getElementById("live").textContent, foot: doc.getElementById("foot-build").textContent, reloads,
    replaced: replaced.slice(), help: !doc.getElementById("help").hidden,
  };
}

// ------------------------------------------------------------------ run
(async () => {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(x => x[1]);
  const out = { results: {}, errors, consoleErrors };
  try {
    vm.runInThisContext(scripts[scripts.length - 1], { filename: "page.js" });
  } catch (e) { errors.push("boot: " + String(e && e.stack || e)); }
  for (const step of scenario.steps || []) {
    try {
      if (step.op === "advance") await advance(step.ms);
      else if (step.op === "set") sc[step.key] = step.value;
      else if (step.op === "snap") out.results[step.as] = snapshot();
      else if (step.op === "eval") { const v = await vm.runInThisContext("(async()=>{" + step.code + "\n})()"); if (step.as) out.results[step.as] = v; await settle(); }
      else if (step.op === "fire") {
        const t = step.target === "doc" ? null : step.target.startsWith("#") ? doc.getElementById(step.target.slice(1)) : null;
        if (step.target === "doc") doc.fire(step.type, step.props); else if (step.target === "win") H.winFire(step.type); else t.fire(step.type, step.props);
        await settle();
      }
    } catch (e) { errors.push(`step ${step.op}: ` + String(e && e.stack || e)); }
  }
  out.calls = sc.calls; out.maxInflight = sc.maxInflight; out.mutations = mut.n; out.reloads = reloads;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
