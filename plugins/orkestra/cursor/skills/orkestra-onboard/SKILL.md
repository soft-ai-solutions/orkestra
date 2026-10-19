---
name: orkestra-onboard
description: "Build or refresh Orkestra's verified project memory (PROJECT.md, gates) for a project in ~/Projects so future runs start informed and cheap. Point new machines at `orkestra setup` (or `orkestra cursor install` / `orkestra copilot install`) when engines are not ready yet. Use when the user asks to onboard, analyze or refresh memory for a project (onboard, projeyi tanı, PROJECT.md)."
---
[orkestra · Cursor engine] The instructions below were written for Claude Code. In Cursor:
- A subagent `orkestra:<name>` is the Cursor subagent `orkestra-<name>`: launch it with the Task tool
  (subagent_type "orkestra-<name>"), ALWAYS in the foreground (run_in_background: false). Its report comes back in
  the same turn. The "model" column of the agent table does not apply (subagents use the chat's model).
- Do NOT end your turn while the run is active: there is nothing to wait for. End it only after `orkestra await`
  (a question / the plan approval) or `orkestra block`.
- AskUserQuestion does not exist. Wherever the text says to ask with it: write the question(s) in the chat, run
  `orkestra await "<one-line summary>"`, then end your turn (`orkestra playbook headless` has the wording — the user
  reads your message right here in the chat). The user's next message is the answer; a hook marks the run active
  again. Plan approval is any clear approve intent the hook recognizes ("approve", "I approve", "go ahead", "lgtm",
  "onay", "onaylıyorum", "onay verdim", "bu planı onaylıyorum yap …") — not only the bare word "approve". Never
  approve yourself; after their message run `orkestra phase next`, which tells you whether the approval was recorded. The approval is
  never skipped ("don't ask questions" in the request does not waive it) and never recorded by you in any way.
- There is no SendMessage: to send findings back to a subagent, launch the same subagent type again with the
  findings and the path of its previous report.
- The shell keeps its working directory between calls and the guards cannot see it. Stay in the project root; if
  another directory is needed use absolute paths or `cd <absolute path> && …` in the same command.
- Skills are `/orkestra-run`, `/orkestra-resume`, `/orkestra-status`, `/orkestra-onboard`, `/orkestra-rollback`,
  `/orkestra-ship`. `orkestra` is on PATH. The same guards, gates and evidence rules apply as in Claude Code.

Onboard the project for orkestra. Project hint: (what the user wrote with this command)

If no engine is ready yet (`orkestra doctor`), tell the user to run `orkestra setup` (or `orkestra setup --yes`), or per-engine `orkestra cursor install` / `orkestra copilot install` — do not run those installs from this skill.

Run `orkestra context` (with `-p <project>`), then follow `orkestra playbook onboard` exactly. Do not modify source code. Report in the user's language (English by default, Turkish when they write Turkish) in ≤ 6 lines.
