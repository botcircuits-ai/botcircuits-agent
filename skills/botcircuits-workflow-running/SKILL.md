---
name: botcircuits-workflow-running
description: Run a BotCircuits workflow by name as a deterministic state machine. Use whenever the user asks to run, start, execute, or kick off a named workflow or process (e.g. "run order fulfillment", "start the loan workflow", "process this order").
---

# Running a BotCircuits Workflow

When the user asks to **run** a workflow (e.g. _"run order fulfillment"_), you
**kick it off and relay results** — you do NOT perform the steps yourself.

The deterministic engine owns control flow, and each **action step runs in a
separate headless agent process** (the `claude-code` runtime spawns one
`claude` process per segment). Your only jobs in this session are: start the
run, answer when it pauses for **human feedback**, and relay the final summary.

```
botcircuits workflow run --name <wf> [--initial-args '{"k":"v"}']
```

Each call prints ONE JSON object. **Always parse it; never guess.**

## The loop

1. **Start.** Resolve the workflow name (slug form, e.g. "order fulfillment" →
   `order_fulfillment`). Confirm it is built — built workflows live in
   `.botcircuits/workflows/.build/<name>.json`. If only the raw source exists,
   build it first (`botcircuits workflow build --name <name>` or the
   **botcircuits-workflow-authoring** skill). Then start the run:

   ```
   botcircuits workflow run --name <name> [--initial-args '{...}']
   ```

   Put any values the user already gave you (order id, applicant name, …) in
   `--initial-args` as a JSON object. The runtime auto-detects `claude-code`;
   pass `--runtime claude-code` to force it.

   This single call runs the engine to completion **in the background process**:
   it navigates every branch and dispatches each action step to its own headless
   `claude` process. It only returns control to you when the workflow finishes
   **or** when a step needs the user.

2. **Read the result and act on `status`:**

   - `"done"` — the workflow completed. Relay `summary` to the user and
     **return to normal conversation**.

   - `"paused"` — a step needs **human feedback**. Ask the user the `question`
     and stop. When they answer, resume the run with their reply:

     ```
     botcircuits workflow run --name <name> --reply "<answer>"
     ```

   - `"error"` — surface the `error` message; don't retry blindly.

3. Repeat step 2 until `status` is `"done"`.

## Rules

- **You are NOT the runtime.** Do not perform action steps, evaluate branches,
  or decide what comes next in this session — the engine and the per-segment
  agent processes do all of that. You only start the run, relay pauses for
  human feedback, and relay the final result.
- One `botcircuits workflow run` invocation runs the whole workflow (or until
  the next human-feedback pause). Don't hand-crank it step by step.
- Run state (resume cursor + slots) persists across the pause/resume boundary in
  `.botcircuits/workflows/.runs/<name>.json` and is cleared when the workflow
  ends.

> This is the **external host** runtime: the engine runs in a background process
> and spawns a separate `claude` process per action segment. The in-session
> ("self") driver where the host agent performs every step itself is a separate
> mode — not what this skill uses. See the runtime-providers docs.
