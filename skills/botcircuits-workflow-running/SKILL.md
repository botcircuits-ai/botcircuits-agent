---
name: botcircuits-workflow-running
description: Run a BotCircuits workflow by name as a deterministic state machine. Use whenever the user asks to run, start, execute, or kick off a named workflow or process (e.g. "run order fulfillment", "start the loan workflow", "process this order").
---

# Running a BotCircuits Workflow

When the user asks to **run** a workflow (e.g. _"run order fulfillment"_), drive
it as a deterministic state machine. **You** perform each action step in this
session using your own tools — the engine decides every branch, ordering, and
slot evaluation deterministically. You never decide which step comes next.

You step through the workflow with a small driver, one action at a time:

```
python -m botcircuits.runtime.step_workflow --name <wf> [--initial-args '{"k":"v"}']
```

It prints ONE JSON object per call telling you what to do next. **Always parse
the JSON; never guess the next step.**

## The loop

1. **Start.** Identify the workflow name from the user's request (slug form,
   e.g. "order fulfillment" → `order_fulfillment`). Confirm it is built — built
   workflows live in `.botcircuits/workflows/.build/<name>.json`. If only the
   raw source exists, build it first (`botcircuits workflow build --name <name>`
   or the **workflow-authoring** skill). Then:

   ```
   python -m botcircuits.runtime.step_workflow --name <name> --restart \
       [--initial-args '{...}']
   ```

   Put any values the user already gave you (order id, applicant name, …) in
   `--initial-args` as a JSON object.

2. **Read the result and act on `status`:**

   - `"action"` — **perform `actions` now**, in order, with your own tools /
     skills / replies. Then report what you observed by calling the driver
     again with `--observed`, matching `report`:

     ```
     python -m botcircuits.runtime.step_workflow --name <name> \
         --observed '{"slots": {"approved": true}, "items": []}'
     ```

     `report.slots` lists the branch variables to fill (name, type,
     description); `report.items` (only for list-decision steps) lists the
     per-item facts to report, one object per element. **Report only values
     you genuinely observed — never invent one.** Omit anything you don't have.

   - `"question"` — **ask the user** the `question` and stop. When they answer,
     continue with their reply:

     ```
     python -m botcircuits.runtime.step_workflow --name <name> --reply "<answer>"
     ```

   - `"done"` — the workflow is complete. Relay `summary` to the user and
     **return to normal conversation**. Do NOT keep stepping.

   - `"error"` — surface the `error` message; don't retry blindly.

3. Repeat step 2 until `status` is `"done"`.

## Rules

- The engine owns control flow. Your job per `action` is only: perform the
  action(s), then report the requested `report` values. You do not pick the
  next step, reorder, or skip — the engine does, from your reported values.
- One action step at a time. Don't batch ahead or assume what's next.
- Run state (the resume cursor + slots) persists in
  `.botcircuits/workflows/.runs/<name>.json` between calls and is cleared when
  the workflow ends.

> This is the **inline / self** runtime: you (the host agent) are the runtime,
> so you perform steps in-session — no nested process. A different host can run
> the same workflow over its own CLI; see the runtime-providers docs.
