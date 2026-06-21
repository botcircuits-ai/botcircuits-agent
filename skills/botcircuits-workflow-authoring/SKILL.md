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
  question), `systemAction` (engine-side bookkeeping, no LLM), `listDecision`
  (decide an outcome for **every item in a list** — see below).
- Branching lives at the **step root** under `conditions` (sibling of
  `type`/`next`/`settings`, NOT inside `settings`). Each entry is
  `{"condition": "<NL test>", "next": "<step_id>"}`. The step's own `next` is
  the default ("otherwise") branch — do NOT add a literal "otherwise" entry.
- A branching step needs BOTH `conditions` (real branches) AND `next`
  (default). A step with neither is terminal.

## Iterating over a list — use `listDecision`, NOT a self-loop

When the process applies the **same decision to every item in a collection**
(check each parcel, price each line item, screen each applicant), DO NOT build a
manual loop (`next_item → do_thing → record → next_item` with an
LLM-maintained "all processed" flag). That pattern makes the model — not the
engine — drive iteration: only the few segments the engine sees get traced, the
per-item work happens inside one LLM call, and the run is neither deterministic
nor per-item auditable.

Instead use a single **`listDecision`** step. The engine fans the step's
`conditions` across each element of the list and decides each one
deterministically, collecting one result record per item. For example,
fulfilling an order's line items (one decision per item against stock):

```json
"decide_line_items": {
  "type": "listDecision",
  "settings": { "action": "Decide each order line item against available stock." },
  "itemSource": { "file": "data/current_order.json", "path": "items" },
  "itemVariables": [
    { "variableName": "in_stock", "description": "whether the sku is in stock" },
    { "variableName": "enough", "description": "stock covers the requested qty" }
  ],
  "decisionKey": "decision",
  "collectInto": "line_results",
  "conditions": [
    { "condition": "the sku is not in stock",       "next": "reject" },
    { "condition": "in stock but not enough for qty", "next": "backorder" }
  ],
  "next": "fulfill"
}
```

`listDecision` rules:
- **Each `conditions[].next` (and the step's default `next`) is a DECISION WORD
  for the item** (`reject`, `backorder`, `fulfill`), **NOT** the id of another
  step. The step itself navigates to ONE next step after the whole list is
  decided — wire that via `decisionKey`/`collectInto`, then a following normal
  step (e.g. `save_results`).
- `itemSource` `{file, path}` points at the list (`path` is a JSON path into the
  file — e.g. `"items"` — or `""` for a plain one-item-per-line text file).
  `itemVariables` are the per-item facts the `conditions` test.
- If each item's facts come from running a script/HTTP-style lookup
  **deterministically**, add `itemFacts` (kind `exec`) so the ENGINE gathers
  them per item with NO AI call. Omit it to have the model report the per-item
  facts in one call. Prefer deterministic where possible.
- `collectInto` names the slot that receives the list of decided records;
  `decisionKey` names the field on each record holding its decision word.
  Optionally `nullOn` `{field: [decisionWords]}` blanks a field for some
  outcomes (e.g. a rejected line has no total: `{"line_total": ["reject"]}`).

One `listDecision` replaces the entire `next_item`/`do_thing`/`record`/loop-back
subgraph. The builder compiles its NL `conditions` into rule expressions just
like any other step.

## Editing

To change an existing workflow, read its raw
`.botcircuits/workflows/<name>.json`, apply the change to the full `steps` map,
overwrite the file (keep the same `name`), then rebuild. The build always
replaces the file whole.
