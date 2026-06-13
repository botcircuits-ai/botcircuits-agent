# Tool System × LLM — workflow tool, auto-recall, and the Option 2 slot fix

> **⚠️ Superseded — historical.** This page documents the original **LLM-driven** workflow execution: the model drove the loop and re-called the workflow tool to advance (auto-recall + the Option 2 re-call-with-slots fix). Workflow execution has since been **inverted** — the engine now owns the loop and calls the LLM per branch-delimited *segment*, so auto-recall and the per-step re-call reminders no longer exist. See **[§8.6.13 Engine-driven execution](../implementations/05-local-tools-and-workflows.md#8613-engine-driven-execution-inversion-of-control)** for the current model. The diagrams below remain useful as the "before" picture that motivated the inversion (token blowup from replaying history per step; advancement depending on the model's choice to re-call).

This page explains, with ASCII diagrams, how the tool system talks to the LLM:

1. [The components and one loop round](#1-components--one-agent-loop-round)
2. [The workflow tool's full lifecycle](#2-workflow-tool-lifecycle--end-to-end-sequence) (kickoff → question pause → auto-recall → branch)
3. [Where slot values travel today](#3-the-re-call-auto-recall-and-the-slot-path-today) — and why branch resolution is fragile
4. [Option 2 — let the main loop's tool call carry the slots](#4-option-2--let-the-main-loops-tool-call-carry-the-slots) — the structural fix and its exact touch points

Companion deep-dive prose lives in [§8.6 of the implementation guide](../implementations/05-local-tools-and-workflows.md); this page is the picture version plus the Option 2 design.

---

## 1. Components & one agent-loop round

```
                          ┌─────────────────────────────────────────────────┐
                          │                  LLM PROVIDER                   │
                          │     anthropic / openai / gemini adapters        │
                          │   (native tool-use API, or ReAct text parse)    │
                          └────────▲───────────────────────────┬────────────┘
                  (1) system       │                           │ (2) response:
                      + reminders, │                           │     text +
                      messages[],  │                           │     tool_calls[]
                      tools[]      │                           ▼
┌──────────────────────────────────┴──────────────────────────────────────────────┐
│                        AGENT LOOP — agent/core.py                               │
│              Agent.chat() / chat_stream(), up to max_steps rounds               │
│                                                                                 │
│  per round:                                                                     │
│    1. provider.complete(system=_system_with_reminder(...), tools=...)           │
│    2. _interpret(resp)            →  (text, tool_calls, terminal)               │
│    3. terminal BUT a workflow is mid-run?                                       │
│         → tool_calls = _auto_recall_calls()      ◄── the "re-call" mechanism    │
│    4. tools.run(name, args, tool_context)        (all calls run concurrently)   │
│    5. append tool_result message;                                               │
│       a human_feedback call ran? → PAUSE: return its question to the user       │
└───────────────┬─────────────────────────────────────────────────────────────────┘
                │ (3) tools.run(name, args, context)
                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                  TOOL REGISTRY — agent/tools/registry.py                        │
│           dispatches to handler(args) or handler(args, context)                 │
│                                                                                 │
│ ┌─────────────┐ ┌───────────┐ ┌───────────┐ ┌─────────────────────────────────┐ │
│ │ builtins    │ │ MCP tools │ │ skills    │ │ WORKFLOW TOOLS                  │ │
│ │ shell_exec, │ │ (local /  │ │ (SKILL.md │ │ one LocalTool per built         │ │
│ │ read_file,  │ │  hosted)  │ │  → tool)  │ │ workflow; tagged with           │ │
│ │ human_feed- │ │           │ │           │ │ _workflow_state={"session_id"}  │ │
│ │ back, …     │ │           │ │           │ │ handler → run_workflow(…)       │ │
│ └─────────────┘ └───────────┘ └───────────┘ └───────────────┬─────────────────┘ │
└──────────────────────────────────────────────────────────────┼──────────────────┘
                                                               ▼
                                            ┌─────────────────────────────────────┐
                                            │ STM ENGINE — agent/workflow/engine/ │
                                            │ run_flow() walks steps until an     │
                                            │ agentAction / question pauses;      │
                                            │ paused session kept in _SESSIONS    │
                                            │ keyed by session_id                 │
                                            └─────────────────────────────────────┘
```

Key facts the diagrams below build on:

- **A workflow is just a tool.** Each built workflow registers as a `LocalTool` whose
  handler drives `run_workflow()`. The provider can't tell it apart from `read_file`.
- **One call = one step.** The engine runs until an `agentAction`/`question` step pauses,
  returns that step's `action` as a *directive* the model must perform, and parks the
  session (`currentStep`, `slots`, `pendingBranch`) in `_SESSIONS`.
- **The loop owns advancement.** The model only *performs* steps. Fetching the next step
  is the loop's job, via auto-recall (§3). The `[Active workflow]` system reminder
  explicitly forbids the model from re-calling the workflow tool itself today.

---

## 2. Workflow tool lifecycle — end-to-end sequence

Running example — `order_status` workflow:

```
start ──► ask_order_id (question: "ask the user for their order id")
              │  fills: order_id
              ▼
        lookup_order  (agentAction: "look up the order via the orders API")
              │  branches on: order_status
              ├── order_status is "delivered" ──► notify_delivered
              └── otherwise (static next)     ──► escalate
```

### Phase 1 — kickoff and the question pause

```
 USER            LLM                    AGENT LOOP                WORKFLOW TOOL + ENGINE
  │               │                         │                               │
  │ "where is my  │                         │                               │
  │  order?"      │                         │                               │
  ├────────────────────────────────────────►│                               │
  │               │ system gets the         │                               │
  │               │ [Available workflows]   │                               │
  │               │ reminder                │                               │
  │               │◄────────────────────────┤                               │
  │               │ tool_call:              │                               │
  │               │ order_status({})        │                               │
  │               ├────────────────────────►│ tools.run(…)                  │
  │               │                         ├──────────────────────────────►│ run_flow from
  │               │                         │                               │ `start`; pauses on
  │               │                         │ tool_result = directive:      │ question step
  │               │                         │ "call human_feedback with     │ `ask_order_id`;
  │               │                         │  the question; do NOT answer  │ session saved in
  │               │                         │  for the user"                │ _SESSIONS
  │               │                         │◄──────────────────────────────┤
  │               │◄────────────────────────┤                               │
  │               │ tool_call:              │                               │
  │               │ human_feedback(         │                               │
  │               │   {question: "..."})    │                               │
  │               ├────────────────────────►│ _human_feedback_pause():      │
  │◄────────────────────────────────────────┤ END TURN — the question       │
  │  "What's your │                         │ becomes the assistant reply   │
  │   order id?"  │                         │                               │
```

### Phase 2 — answer, auto-recall, perform the branching step

```
 USER            LLM                    AGENT LOOP                WORKFLOW TOOL + ENGINE
  │               │                         │                               │
  │ "it's 4711"   │                         │                               │
  ├────────────────────────────────────────►│                               │
  │               │ system now carries the  │                               │
  │               │ [Active workflow]       │                               │
  │               │ reminder ("do NOT call  │                               │
  │               │ 'order_status' yourself")│                              │
  │               │◄────────────────────────┤                               │
  │               │ short ack, NO tool calls│                               │
  │               ├────────────────────────►│ terminal… but workflow active │
  │               │                         │ → AUTO-RECALL: inject         │
  │               │                         │   order_status({})  ──────────┼──► re-entry:
  │               │                         │   (id wf-autorecall-…)        │    resolver fills
  │               │                         │                               │    order_id="4711"
  │               │                         │ tool_result = directive:      │    (verbatim reply);
  │               │                         │ "execute: look up the order   │    engine advances,
  │               │                         │  via the orders API"          │    pauses on
  │               │                         │◄──────────────────────────────┤    `lookup_order`,
  │               │◄────────────────────────┤                               │    sets pendingBranch
  │               │ tool_call:              │                               │
  │               │ orders_api({id:"4711"}) │                               │
  │               ├────────────────────────►│ tools.run(…)                  │
  │               │                         │ tool_result:                  │
  │               │◄────────────────────────┤ {"status": "delivered", …}    │
  │               │ "Your order was         │                               │
  │               │  delivered…" (no tools) │                               │
  │               ├────────────────────────►│ terminal… workflow active     │
  │               │                         │ → AUTO-RECALL again ──────────┼──► Phase 3
```

### Phase 3 — branch resolution on re-entry (where the slot problem lives)

```
 AGENT LOOP                                WORKFLOW TOOL (local.run_workflow)
     │                                                 │
     │  order_status({})   ◄── args are ALWAYS {}      │
     ├────────────────────────────────────────────────►│ pendingBranch = lookup_order
     │  tool_context = {                               │ needs: order_status
     │    last_user_message:      "it's 4711",         │
     │    last_assistant_message: "Your order was      ▼
     │                             delivered…"      ┌─────────────────────────────────┐
     │  }                                           │ 1. SLOT RESOLVER (deterministic)│
     │     ▲                                        │    raw args → choice literal in │
     │     │ 2KB snapshots of the last TEXT         │    user msg/args → typed extract│
     │     │ blocks only — tool_result blocks       │    → verbatim reply → saved slot│
     │     │ (where "delivered" actually came       │ 2. LAYER B (LLM extraction over │
     │     │ from) are NOT included                 │    args + action text + last    │
     │     │                                        │    user/assistant messages)     │
     │                                              │ 3. LAYER A (type coercion)      │
     │                                              └───────────────┬─────────────────┘
     │                                                              ▼
     │                                              slots → evaluate_choices(pendingBranch)
     │                                                              │
     │                                          matched → notify_delivered
     │                                          no match → defaultNext (escalate)  ◄── the bug
```

---

## 3. The re-call (auto-recall) and the slot path today

The auto-recall is the loop-side "re-call tool": when the model's turn is terminal (no
tool calls) but `active_workflow_names()` is non-empty, `_auto_recall_calls()` injects a
synthetic workflow tool call (`id` prefixed `wf-autorecall-`, **arguments `{}`**) so the
engine advances without the model having to remember to re-enter.

That design means the slot values a branch needs never ride *in* the tool call — they
have to be re-derived inside the workflow tool from a side channel:

```
                  WHERE SLOT VALUES TRAVEL TODAY (re-entry with pendingBranch)

   values produced while acting on the step          how they reach the resolver
   ─────────────────────────────────────────         ───────────────────────────────────
   user's reply text             ───────────────►    tool_context.last_user_message  ✔
   model's own prose             ───────────────►    tool_context.last_assistant_message ✔
                                                     (each truncated to 2 KB)
   tool_result payloads
   (orders_api → "delivered")    ───────╳            not in the snapshot AT ALL      ✘
   values across >1 turn ago     ───────╳            snapshot only holds the latest  ✘

   _auto_recall_calls() args     ───────────────►    raw_args = {}  → resolver source #1
                                                     (highest-priority source) never fires
```

Consequences:

- The **deterministic resolver** can only use the last user message + saved slots; a
  value that surfaced in a tool result (the `lookup_order` case above) is invisible.
- **Layer B** then guesses with an extra LLM round-trip over the same incomplete
  snapshot — non-deterministic, token-costly, and its hallucination guard rightly drops
  values it can't ground in that snapshot.
- Unresolved slot → the engine silently falls through to `defaultNext` — a workflow
  that "works" but always takes the wrong branch.

The structural smell: **the main-loop LLM is the one component that reliably knows the
values** (it just produced/read them, in full context), yet the current contract forbids
it from re-calling the workflow tool, and the loop's recall carries nothing.

---

## 4. Option 2 — let the main loop's tool call carry the slots

> **Status: implemented.** `run_workflow` returns `branch_variables`, `workflow_tool`
> mirrors them onto the tool's `input_schema` + `_workflow_state`, the step directive
> and `[Active workflow]` reminder ask for the re-call, and auto-recall is the fallback.
> Prose version: [implementation guide §8.6.12](../implementations/05-local-tools-and-workflows.md).

The fix inverts the carrier: when a branching step is pending, the **model's own tool
call in the main loop** re-enters the workflow and carries the slot values as plain
tool-call arguments. The workflow tool tells it which variables to bring (schema +
directive), and the loop's empty-args auto-recall is demoted to a fallback.

```
 BEFORE (today)                              AFTER (Option 2)
 ──────────────                              ────────────────
 LLM acts on step                            LLM acts on step
   │  values stay in the transcript            │  LLM knows the values it just
   ▼                                           │  produced / read in tool results
 LLM stops issuing tool calls                  ▼
   │                                         LLM re-calls the workflow tool ITSELF:
   ▼                                           order_status({"order_status":"delivered"})
 loop injects auto-recall                      │        ▲
   order_status({})                            │        │ the tool's input_schema now
   │     ▲                                     │        │ lists the pending step's
   │     │ slots NOT carried                   │        │ variables, and the step
   ▼     │                                     │        │ directive asks for them
 workflow tool re-derives slots                ▼
 from a 2KB text snapshot:                   raw_args hit resolver source #1 →
 resolver mostly blind,                      deterministic branch, NO Layer B
 Layer B = extra LLM call + guesswork        LLM call, tool results covered
   │                                           │
   ▼                                           ▼
 unresolved → silent defaultNext             loop auto-recall STAYS as fallback:
                                             model forgot to re-call → old path
                                             (resolver → B → A) still runs
```

### Sequence after the fix (Phase 3 replacement)

```
 LLM                                    AGENT LOOP                WORKFLOW TOOL + ENGINE
  │ tool_call: orders_api({id:"4711"})      │                               │
  ├────────────────────────────────────────►│                               │
  │◄── tool_result {"status":"delivered"} ──┤                               │
  │                                         │                               │
  │ tool_call:                              │                               │
  │ order_status(                           │                               │
  │   {"order_status": "delivered"})  ──────┼──────────────────────────────►│ raw_args carry the
  │   ▲ a NORMAL model-issued call —        │                               │ slot → resolver
  │     this is "the main loop's tool       │                               │ source #1 resolves,
  │     call carrying the slots"            │                               │ Layer B skipped,
  │                                         │ tool_result = next directive  │ branch → 
  │◄────────────────────────────────────────┤◄──────────────────────────────┤ notify_delivered
```

### Touch points

| # | Where | Today | Option 2 change |
|---|---|---|---|
| 1 | [`workflow_tool()`](../../src/botcircuits/agent/workflow/__init__.py) — `input_schema` | Hard-coded empty: `{"type": "object", "properties": {}}` | When the engine pauses on a branching step, surface that step's variables (from the `variables` the result already carries — name, `dataType`, description) as schema `properties`, so providers show the model exactly what to pass. Reset to empty when no branch is pending. |
| 2 | [`compose_workflow_step_directive()`](../../src/botcircuits/agent/workflow/cli_commands.py) | Says nothing about re-calling (auto-recall handles it) | For a step with a pending branch, append: *"when you have finished this step, call `<wf>` with `{order_status: …}` filled from what you observed."* Wording stays here so the Hermes out-of-process wrapper renders the same text. |
| 3 | [`_with_workflow_reminder()`](../../src/botcircuits/agent/core.py) — `[Active workflow]` block | *"Do NOT call `<name>` yourself — the next step is requested for you automatically."* | When the active workflow has a pending branch: *"after acting, call `<name>` with the step's variables."* The blanket prohibition stays only for non-branching steps (re-calling there is harmless but wasteful — loop recall already covers it). |
| 4 | [`_auto_recall_calls()`](../../src/botcircuits/agent/core.py) | The **primary** advancement mechanism | Unchanged code, demoted role: **fallback only**. It still fires when the model ends its turn without re-calling, so a forgetful model degrades to today's behavior (resolver → Layer B → Layer A), never to a stall. |
| 5 | [`run_workflow()` re-entry pipeline](../../src/botcircuits/agent/workflow/local.py) | Resolver source #1 (raw args) exists but never fires on auto-recall (args always `{}`) | **No structural change** — this is the payoff: model-supplied args flow through the existing `raw_args` path, the resolver resolves deterministically, and Layer B becomes the rare semantic fallback instead of the routine crutch. |

### Properties worth calling out

- **Backwards compatible by construction.** Every layer that exists today stays in the
  pipeline, in the same order. Option 2 only adds a higher-quality source at the top
  (model-supplied raw args) and the means for the model to use it (schema + wording).
- **Deterministic on the happy path.** A slot carried in the tool call hits resolver
  source #1 and is type-coerced by Layer A — same input, same branch, zero extra LLM
  calls. Layer B's hallucination guard already accepts raw-args values (args JSON is
  part of its source context), so nothing fights the new path.
- **Covers the tool-result gap.** Values that today never reach the resolver (they live
  in `tool_result` blocks, outside the 2KB text snapshot) are exactly what the model can
  now carry directly.
- **Double-advance guard still needed.** The model re-calling the workflow tool while
  the loop *also* auto-recalls would advance two steps. The contract that prevents it:
  auto-recall fires only on a turn with **no** model-issued tool calls, so a turn that
  contains the model's re-call never triggers the loop's recall on top.
