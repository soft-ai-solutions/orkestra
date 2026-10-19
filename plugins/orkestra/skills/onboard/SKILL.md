---
name: onboard
description: Build or refresh Orkestra's verified project memory (PROJECT.md, gates) for a project in ~/Projects so future runs start informed and cheap. Point new machines at `orkestra setup` (or `orkestra cursor install` / `orkestra copilot install`) when engines are not ready yet. Use when the user asks to onboard, analyze or refresh memory for a project (onboard, projeyi tanı, PROJECT.md).
argument-hint: "[project]"
---
Onboard the project for orkestra. Project hint: $ARGUMENTS

If no engine is ready yet (`orkestra doctor`), tell the user to run `orkestra setup` (or `orkestra setup --yes`), or per-engine `orkestra cursor install` / `orkestra copilot install` — do not run those installs from this skill.

Run `orkestra context` (with `-p <project>`), then follow `orkestra playbook onboard` exactly. Do not modify source code. Report in the user's language (English by default, Turkish when they write Turkish) in ≤ 6 lines.
