---
name: botcircuits-workflow-authoring
description: Create or edit a BotCircuits workflow from a natural-language description. Use whenever the user asks to create, author, design, build, or edit a workflow / journey / process flow (e.g. "create an order fulfillment workflow with ...", "add a refund branch to the loan workflow").
---

# Authoring a BotCircuits Workflow

When the user asks to **create or edit** a workflow (e.g. _"create an order
fulfillment workflow with ..."_), turn their description into a runnable
workflow. A workflow is a deterministic state machine the **workflow-running**
skill later executes; authoring is generation + validation, performed by you in
this session.

## Steps

1. **Clarify if needed.** If scope, inputs, or branching is ambiguous, ask ONE
   focused round of questions first. Otherwise proceed.

2. **Write the workflow JSON** to `.botcircuits/workflows/<name>.json` (the
   human-editable source of truth). Pick a slug-safe `name`
   (`^[a-zA-Z0-9_-]+$`) from the user's intent (e.g. "order fulfillment" →
   `order_fulfillment`); it doubles as the filename and the run identifier.

3. **Build it** — this compiles natural-language `conditions` into deterministic
   `choices` + an aggregated `flow.variables` list, and writes the runnable copy
   to `.build/`:

   ```
   botcircuits workflow build --name <name>
   ```

   Only built workflows are runnable.

4. **Confirm** to the user: name, what it does, and the step/branch outline.

## Workflow shape

```json
{
  "name": "order_fulfillment",
  "description": "when to run this workflow",
  "start": "start",
  "steps": {
    "start": { "type": "start", "next": "check_stock" },
    "check_stock": {
      "type": "agentAction",
      "settings": { "action": "Check stock for the order items." },
      "next": "backorder",
      "conditions": [
        { "condition": "all items are in stock", "next": "ship" }
      ]
    },
    "ship":      { "type": "agentAction", "settings": { "action": "Ship the order." } },
    "backorder": { "type": "agentAction", "settings": { "action": "Create a backorder and notify the customer." } }
  }
}
```

Rules:
- Step types: `start` (entry, no action), `agentAction` (the runtime performs
  `settings.action`), `question` (ask the user; `settings.action` is the
  question), `systemAction` (engine-side bookkeeping, no LLM).
- Branching lives at the **step root** under `conditions` (sibling of
  `type`/`next`/`settings`, NOT inside `settings`). Each entry is
  `{"condition": "<NL test>", "next": "<step_id>"}`. The step's own `next` is
  the default ("otherwise") branch — do NOT add a literal "otherwise" entry.
- A branching step needs BOTH `conditions` (real branches) AND `next`
  (default). A step with neither is terminal.

## Editing

To change an existing workflow, read its raw
`.botcircuits/workflows/<name>.json`, apply the change to the full `steps` map,
overwrite the file (keep the same `name`), then rebuild. The build always
replaces the file whole.
