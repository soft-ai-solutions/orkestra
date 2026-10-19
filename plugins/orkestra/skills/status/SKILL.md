---
name: status
description: Show where Orkestra runs stand — phase, tasks, evidence, approval, gate results — for the current or a named project, without changing anything. Use when the user asks about the progress or state of an orkestra run (status, durum, nerede kaldık, what is running, where are we).
argument-hint: "[project]"
---
Report orkestra status (read-only). Project hint: $ARGUMENTS

If no project is given, run `orkestra watch --once` (every open run in the workspace: phase, what it is doing now,
whether it waits for the user). With a project: `orkestra context -p <project>` and, if there is an active run,
`orkestra status -p <project>` plus `orkestra watch --once -p <project>`. Answer in the user's language (English by default, Turkish when they write Turkish) in ≤ 10 lines: project, run, phase, tasks progress, evidence gaps, what happens next, and whether anything waits for the user. Mention that live tracking is available with `orkestra watch` (terminal) or
`orkestra dashboard --open` (browser, http://127.0.0.1:47616/). Do not continue the run unless the user asks.
