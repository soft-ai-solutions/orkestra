# Phase: verify

Goal: fresh proof that nothing that worked is broken and an honest, traceable outcome: `PASS` when every requirement is proven; `PASS_WITH_NOTES` only for explicitly deferred, non-passing evidence.

1. `orkestra gate verify -p <project>`.
   - When this run changed no product file and the baseline is green, verify reuses that baseline. It does not run the suite again.
   - A product edit still runs the gates. REGRESSION → `orkestra phase back implement --reason "<gate> regressed"`, brief an implementer with the failing tail (fix the cause, never the test), then return here.
2. Dispatch `orkestra:verifier` (project path + run folder). It reads the gate result with `orkestra gate last` (it does
   NOT re-run the full suite), runs only narrow tests it needs as evidence, and writes `verification.md` ending in `VERDICT: …`.
   End your turn after dispatching; its report resumes you.
   **XS/S carve-out (policy):** no separate verifier agent. The orchestrator writes `verification.md` itself from
   `orkestra template verification`, citing `orkestra gate last`, then runs `orkestra trace check` and
   `orkestra integrity check` and ends with `VERDICT: …`. Same evidence rules as above (no invented files/tests, no
   manual check without an `orkestra evidence` log). `orkestra next` says `work` instead of dispatching the verifier.
   The final reviewer stays mandatory for every size, and `phase next` preconditions are unchanged.
3. FAIL → fix via implementer (back to implement) and repeat. Unverifiable items must be stated as such — never upgraded to PASS.
   `PASS_WITH_NOTES` may proceed only when accepted deferrals are explicitly marked `DEFERRED/UNVERIFIED`; failed acceptance criteria remain `FAIL`.
4. `orkestra phase next` re-checks deterministically: gates green after the last change, traceability with verifiable
   references only (no invented files/tests), test integrity (no removed tests, skip/only markers or new suppressions
   unless listed in the approved plan's `## Exceptions` or, in a Turkish plan, `## İstisnalar`), and dependencies.
