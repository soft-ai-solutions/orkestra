# Delivery report: {{title}}

- Run: `{{run_id}}` · Project: {{project}} · Date: {{date}}
- Status: COMPLETED | PARTIAL (why). Be honest.

## What was done and its value
<2-5 sentences: what changed from the user's point of view and which problem it solved.>

## Evidence
`orkestra report evidence --write` adds the machine evidence block (approval, gate baseline→verify, red/green per task,
test integrity, changed files, triage). Do not edit that block by hand; every number stated here must come from it.

## Deviations from the plan
<difference between the approved plan and the final plan; "None" if there is none>

## Not done / deferred
<review items that were deferred, things left out of scope; "None" if there are none>

## Assumptions I made (autopilot)
<Top 3 assumptions from spec.md and the plan changelog, each with how to reverse it; "None" if there are none.>

## How to try it
<step by step: command / screen / URL>

## Risks and follow-ups
<...>

## Commit suggestion
Nothing was committed: the changes are in the working tree. Suggested message:
```
<type>(<scope>): <summary>

<body>
```
