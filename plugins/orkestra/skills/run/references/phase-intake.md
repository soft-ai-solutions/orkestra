# Phase: intake

Goal: understand the request precisely enough to scope discovery. Output: a `## Classification` section (`## Sınıflandırma` when the conversation is Turkish) appended to `request.md`.

1. Restate the request in one sentence (in the user's language). Identify the **outcome the user values**, not just the literal ask.
2. Classify (append to `.orkestra/features/<run>/request.md`; use the English labels unless the conversation is Turkish):
   ```
   ## Classification
   - Type: feature | bugfix | refactor | perf | security | docs | infra | ui | advice | question | explore
     (`advice` / `question` / `explore` = no product code change expected; still a tracked run on the dashboard)
   - Size: XS | S | M | L — prefer the size already set by `orkestra start` (auto-inferred). Bump **up** only when risk/scope is clearly larger; never inflate a typo/fix or an advisory ask into M. A payment, login, schema, secret, or deploy change stays at least M even if the request says it is small. L → keep the full request in scope and order tasks by dependency; do not silently drop work. Full pipeline (scouts + critic + adversary) is for **M/L**; XS/S must stay on the fast track.
   - Risk: low | medium | high  (data/migration, auth, payments, public API, store release → high)
   - Affected surfaces: <web / iOS / Android / wear / API / CLI / DB ...>
   - Uncertainties (first impression): <bullets — candidates for clarify>
   ```
   In a Turkish conversation write the same block as `## Sınıflandırma` with `Tür` (same type tokens: `advice` / `question` / `explore` allowed), `Boyut`, `Risk`, `Etkilenen yüzeyler`
   and `Belirsizlikler (ilk izlenim)`. The CLI accepts either heading spelling.
3. Red flags → resolve from repository evidence and preserve safety before discovery. In manual mode, ask only when the answer changes the outcome; in autopilot, record the safest assumption or block without writing if no safe choice exists:
   - the request spans **two projects** (isolation rule) → select the project most directly named by the request; if no safe target can be identified, block without writing to either project.
   - deploy/commit/store actions remain forbidden unless explicitly requested in the user's current message; do not infer permission from autopilot.
   - it contradicts an AGENTS.md invariant → preserve the invariant and block only if the requested outcome cannot be achieved safely.
   - the target project is **orkestra itself** (the plugin that is running you) → read its
     `CONTRIBUTING.md` first: use an isolated copy while any run is open; do not edit live hook/guard/agent files in place.
4. If `orkestra start` reported uncommitted changes, tell the user in one line that they are backed up and will not be attributed to this run.
5. `orkestra phase next`.
