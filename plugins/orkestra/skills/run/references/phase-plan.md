# Phase: plan

Goal: an approval-ready, implementation-proof plan. Output: `plan.md` + `reviews/plan-critic.md` with PASS.

1. Dispatch `orkestra:architect`:
   ```
   project: <abs path> · run folder: <path>
   inputs: request.md, context.md, scout-*.md, research*.md, spec.md, memory/PROJECT.md (+ relevant memory/specs/*.md, DECISIONS.md, LEARNINGS.md), AGENTS.md
   task: write plan.md for this spec (template: `orkestra template plan`); size <XS|S|M|L>; then run `orkestra plan check`.
   ```
   XS/S: write the plan yourself (1–3 tasks) instead of architect. After each dispatch end your turn; the report resumes you.
2. Dispatch `orkestra:plan-critic` with the same paths. It writes `reviews/plan-critic.md` ending in `VERDICT: …`.
   **XS/S skip the critic** (CLI enforces this); M/L require PASS. `orkestra plan check` must still pass for every size.
3. FAIL → send the blockers back to the architect (same agent via SendMessage if available, else a new call with the critic file path). After 3 failed rounds, try a materially different safe plan; if no passing plan is possible, block with the unresolved finding (manual mode may ask the user).
4. `orkestra plan check -p <project>` must print OK. If it adds `NOTE: LARGE PLAN`, preserve the full requested scope,
   choose a dependency-safe order and avoid optional work. Do not ask the user to choose a split in autopilot; in manual
   mode, include a materially necessary split in the approval summary.
5. Plan rules the CLI enforces:
   - **Bugfix / perf** (`Type:` in the classification block): at least one task has `TDD: required` (repro-first) and T001 references the symptom FR.
   - **Size only rises:** leaving plan, an XS/S run whose plan touches security or data paths, has more than 3 tasks or more than 8 files becomes M (ledger line says why), so the plan-critic is then required. M/L are never changed.
6. `orkestra phase next`.
