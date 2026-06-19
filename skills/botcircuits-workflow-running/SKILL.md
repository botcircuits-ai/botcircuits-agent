---
name: botcircuits-workflow-running
description: Run a BotCircuits workflow by name as a deterministic state machine. Use whenever the user asks to run, start, execute, or kick off a named workflow or process (e.g. "run order fulfillment", "start the loan workflow", "process this order").
---

# Running a BotCircuits Workflow

When the user asks to **run** a workflow, the deterministic engine navigates the
state machine and hands **you** (this session) one action at a time. You perform
each action with your own tools, report what you observed, and the engine picks
the next step. **You never choose the next step — the engine does, from your
reported values.**

## Start

Resolve the workflow name from the request (slug form, e.g. "order fulfillment"
→ `order_fulfillment`), then start it:

```
botcircuits workflow run --name <name> --restart [--initial-args '{"order_id": "1024"}']
```

Pass any values the user already gave you in `--initial-args`. Each call prints
ONE JSON object. **Always parse it; never guess.** If the workflow isn't built
(`.botcircuits/workflows/.build/<name>.json` is missing), build it first with
`botcircuits workflow build --name <name>` (or the
**botcircuits-workflow-authoring** skill), then start.

## The loop — act on `status`

- `{"status": "action", "actions": [...], "report": {...}}` — **perform the
  `actions` now**, in order, with your own tools/skills. Then report what you
  observed and let the engine advance:

  ```
  botcircuits workflow run --name <name> --observed '{"slots": {...}, "items": [...]}'
  ```

  `report.slots` lists the branch variables to fill (name, type, description);
  `report.items` (list-decision steps only) lists per-item facts, one object per
  element. **Report only values you genuinely observed — never invent one.**
  Omit anything you don't have.

- `{"status": "question", "question": "..."}` — **ask the user** the question
  and stop. When they answer, resume with their reply:

  ```
  botcircuits workflow run --name <name> --reply "<their answer>"
  ```

- `{"status": "success", "message": "<summary>"}` — relay the summary. Done.

- `{"status": "failure", "message": "<reason>"}` — relay the reason. **Do not
  retry** — let the user decide what to do next.

Repeat until the outcome is `success` or `failure`.

## Rules

- The engine owns control flow. Per `action` your only job is: perform the
  action(s), then report the requested `report` values. You do not pick, reorder,
  or skip steps.
- One action step at a time. Don't batch ahead or assume what's next.
- Run state (resume cursor + slots) persists in
  `.botcircuits/workflows/.runs/<name>.json` between calls and is cleared when the
  workflow ends.

> You (the host agent) execute steps in-session, so actions use your own live
> tool permissions — no subprocess is spawned. (For non-interactive hosts, the
> engine can instead run headless one process per step via
> `--runtime claude-code`; that mode runs to completion and only pauses for human
> feedback. The skill does not use it.)
