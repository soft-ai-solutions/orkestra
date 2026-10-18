# Phase: deliver

Goal: the user gets the value, the proof, and a project that remembers.

1. `orkestra changes -p <project>` and `orkestra diff --stat -p <project>`.
2. Plan deviations: diff `.orkestra/runs/<run>/approved/plan.md` vs `plan.md` (read both quickly) → summarize.
3. Write `report.md` (`orkestra template report`, in the user's language): value, changed behavior, deviations, deferred items, how to try
   it, risks, and a **suggested commit message** — explicitly "nothing was committed" (Turkish: "hiçbir şey commit'lenmedi"). Then run
   `orkestra report evidence --write -p <project>`: the CLI inserts the machine-generated evidence block (approval, gate
   baseline vs verify, red/green exits per task, integrity, changed files, triage). Never edit that block; `phase next`
   regenerates and compares it. Every number you state must come from that block.
4. Memory (keep it short, durable, true):
   - `orkestra memory feature-add -p <project> --title "<title>" --summary "<one line value>" --capabilities "<cap1,cap2>"`
   - Living specs: for each capability touched, update `.orkestra/memory/specs/<capability>.md` to describe the CURRENT behavior (merge, don't append history; ≤ 80 lines each).
   - `orkestra memory decision "<decision — why — cost if wrong>"` for real architectural choices only.
   - `orkestra memory learn "<lesson — evidence>"` only for a mistake the finish step does not already record.
     `orkestra phase next` indexes the run: a feature row when this run has none, plus blocked-task notes and
     triage rows marked fix and done. The next run's `orkestra start` prints the lessons whose words match the request.
   - If PROJECT.md is now wrong (new command, new structure), fix the affected lines.
5. `orkestra phase next` → done. Moving to `done` automatically removes regenerable tool dumps under the project
   (`test-results`, `playwright-report`, `android/**/build`, …). Manual: `orkestra clean -p <project>` (or
   `--dry-run`). Opt out: `"cleanup_artifacts": false` in `~/.orkestra/config.json` or the project's
   `.orkestra/config.json`.
6. Final chat message (in the user's language, ≤ 15 lines): what was delivered + value, evidence (only facts from the evidence block),
   how to try, anything not done or unverified (say so plainly), path to report.md, suggested commit message, and that
   nothing was committed or pushed.
