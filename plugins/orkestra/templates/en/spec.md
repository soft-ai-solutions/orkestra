---
feature: {{run_id}}
status: draft
size: {{size}}
created: {{date}}
---
# {{title}}

> WHAT and WHY. The plan (plan.md) explains HOW. After approval this file is FROZEN (enforced by a hook).

## Purpose and value
<Which problem is solved for the user/product, who benefits, what success looks like. 2-4 sentences.>

## Reproduction
<Bugfix only, otherwise delete this section: the command that shows the bug, observed vs expected.>

## Scope
### In scope
- <item>
### Out of scope
- <things deliberately not done, to guard against scope creep>

## User stories
- **US1 (P1)**: As a <role>, I want <need>, so that <benefit>.
  Independent test: <how this story can be verified on its own>

## Requirements
- **FR-001**: The system MUST <measurable behavior>.
- **FR-002**: ...

## Success criteria
- **SC-001**: <measurable, technology-agnostic outcome, e.g. "submitting an empty form shows an error message for 3 fields">

## Edge-case matrix
| # | Case | Input | Expected | Requirement |
|---|---|---|---|---|
| E1 | <empty/null/invalid/concurrent/offline/unauthorized...> | ... | ... | FR-00x |

## Boundaries
- Always: <AGENTS.md + project invariants; keep existing behavior; minimal diff>
- Never: <no commit/push/deploy; no unrequested refactors or dependencies; no mixing of projects>

## Suggested extra scope (enrichment)
<Additions Orkestra suggests, which the user ACCEPTED or REJECTED. Nothing that is not accepted enters the scope.>
- [accepted|rejected] <suggestion>: <why it is valuable>

## Clarifications
### Session {{date}}
- Q: <question> → A: <answer>  (or: "No questions needed. Reason: ...")

## Assumptions and open risks
- <assumption>: <impact if it is wrong>
