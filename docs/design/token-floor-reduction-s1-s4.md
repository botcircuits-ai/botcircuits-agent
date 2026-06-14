# Token-Floor Reduction (S1–S4) — Beating a One-Shot Agent

**Status:** Proposed
**Repo:** `botcircuits-ai/botcircuits-agent`
**Depends on:** [Engine-Driven Workflow Execution](#) (§8.6.13) — this builds on it.
**Goal:** Make engine-driven execution reach **100% decision accuracy** AND **fewer tokens than a bare one-shot agent (hermes)**, not just fewer than prompt-driven agents.

---

## 1. Problem

Engine-driven execution (§8.6.13) already beats *prompt-driven* agents (Claude-Code style) on tokens. It does **not** beat a *one-shot* agent (hermes), and it isn't 100% accurate on list-shaped workflows. Measured on `order_fulfillment` (gemini-2.5-flash):

| Agent | Accuracy (single-run) | Output tok/run | Cost/case |
|---|---|---|---|
| hermes (one-shot) | 100% | ~115–286 | $0.0076 |
| botcircuits (current, collapsed) | ~92% | ~2,400 | $0.0153 |

Two root causes, each tied to a plan clause:

1. **Accuracy < 100%** — per-item decisions are made by the *model in prose*, not the engine. Violates §4 (decisions must be deterministic `evaluate_choices`). The model misclassifies (backorder→reject) or stalls.
2. **Output tokens 8–20× hermes** — every segment *acts and explains*: it narrates a checklist, restates tool output, and re-emits the final answer. On flash, output is priced ~8× input, so this output is the entire cost gap. The model is spent producing tokens **the engine already has in its own state**.

The thesis: *spend the model only on what the engine cannot do — gathering facts and understanding language. Decisions, validation, and the final answer are engine output, which costs zero LLM tokens.* Today we pay the model to duplicate work the state machine could do for free.

---

## 2. The four changes

### S4 — Tier-0 deterministic slots (engine computes, no LLM call)

Plan §3.2 Tier 0, made real. A branch variable that is a **pure function of data the engine can already read** is computed by the engine in code — the segment is never sent to the LLM.

- A `flow.variables` entry may carry a `resolver` spec: `{kind, ...}` describing a deterministic computation:
  - `enum_check` — value ∈ a set (e.g. `region ∈ {US,EU,APAC}`).
  - `file_membership` — a key's presence/absence in a workspace file (e.g. `customer_id` in `fraud_blocklist.txt`).
  - `regex` / `range` — pattern / numeric bound.
  - `jsonpath` — pull a typed value from a named JSON file.
- Before running a segment, the engine asks: *are ALL of this segment's branch variables Tier-0 resolvable?* If yes, it resolves them in code, evaluates the branch, and advances — **the LLM call is skipped entirely**.
- For `order_fulfillment`: `header_status` (enum/empty checks on the order JSON) and `fraud_status` (membership in the blocklist file) become Tier-0. The whole `screen` segment runs with **no LLM call**.

**Token impact:** removes 1 LLM call + its entire input/output for every run. This is the largest single lever.

### S3 — List-decision primitive (the accuracy fix, in ONE call)

A new step kind, `listDecision`. The model reports a **list** of per-element fact-sets in one structured-output call; the engine applies `evaluate_choices` to **each** element and records a per-element decision. Cost scales with branches (§3.1), not items; decisions are engine-deterministic (§4).

- Step shape:
  ```json
  {
    "type": "listDecision",
    "settings": { "action": "<gather facts per item>", "overList": "items" },
    "itemVariables": [ {"variableName":"sku_found","dataType":"boolean"}, ... ],
    "choices": [ /* same expressionList shape, evaluated PER element */ ],
    "next": "<default per-element outcome>",
    "collectInto": "decisions"
  }
  ```
- The segment call asks the model to return, via structured output, a list: `[{item_ref, sku_found, stock_sufficient, current_line_total}, ...]` — **facts only, no decision words.**
- The engine loops the reported list, runs `evaluate_choices` per element → a decision per item, appended to `collectInto`. One LLM call, N deterministic decisions.

**Accuracy impact:** the decision is code, not model prose → the misclassification failure mode is gone.
**Token impact:** one segment instead of N looped segments; no per-item re-reads.

### S2 — Engine emits the final answer (model emits nothing)

The engine builds the answer from its own decision records and injects it as the workflow summary. The model never re-outputs the result.

- A workflow may declare `flow.result`: a template/JSONPath assembling the final payload from slots + collected decision lists (e.g. `{customer: slots.customer_id, decisions: collected.decisions}`).
- On workflow end, the engine renders `flow.result` from state and returns it AS the summary — the conversational history receives the finished answer with **zero model output tokens** spent on it.
- The terminal `emit_result` step is deleted from authored workflows; the engine is the reporter.

**Token impact:** removes the entire emit step (a full LLM call whose output IS the answer — hundreds of tokens every run).

### S1 — Silent segments (structured output only, no prose)

Engine-mode segments return slot data via structured output / `record_slots` and **emit no assistant prose**. The model acts (tool calls) and reports (structured slots); it does not narrate.

- `ENGINE_SYSTEM_PROMPT` gains a hard rule: *produce NO assistant text; communicate only through tool calls and `record_slots`. The engine reports outcomes — you do not.*
- `_run_segment` stops surfacing `final_text` as answer-bearing (the answer now comes from S2). Any stray prose is dropped, not billed downstream as the result.
- The segment's job shrinks to: call the tools, call `record_slots`, stop.

**Token impact:** collapses per-segment output from ~hundreds of narration tokens to ~the structured slots only (~5× reduction in output).

---

## 3. How they compose on `order_fulfillment`

```
Today (collapsed):  screen(LLM) → process_items(LLM, decides+emits)        ~14 calls, 2400 out
Engine-looped:      screen(LLM) → [select→lookup→mark]×N(LLM) → emit(LLM)  ~25 calls, worse

S1–S4:              screen        → process_items(listDecision) → result
                    (Tier-0, NO   → ONE LLM call: facts list,   → engine
                     LLM call)       structured, silent           renders answer
                                                                   NO LLM call
                    = ONE LLM call total; output ≈ the item fact list only
```

Projected: **1 LLM call**, output ≈ a structured fact list (~300–600 tokens), decisions + answer produced by the engine. That is the only shape that can land at or below hermes, because every token the model previously produced that the engine could produce is removed.

---

## 4. Accuracy (100%) — why this gets there

| Failure mode today | Removed by |
|---|---|
| Per-item misclassification (model picks wrong word) | S3 — engine decides via `evaluate_choices` |
| Stall writing the final JSON | S2 — engine renders the answer; model never writes it |
| Header/fraud slips | S4 — engine computes them deterministically |
| Narration thrash / re-reads | S1 — model only reports facts; fewer turns to derail |

Residual risk: the model misreports a *fact* (misreads a number into structured output). Far rarer than judgment slips, and guarded by the existing per-variable validators + hallucination guard; a fact that fails validation routes to Tier-2 (cheap-model re-extract) or a clarification, never a wrong decision.

---

## 5. Tokens (< hermes) — honest target

hermes floor ≈ 115–286 output. S1–S4 best case ≈ one structured fact list. Whether botcircuits lands **under** hermes depends on how small that fact list is vs hermes' final answer — they're the same order of magnitude. Expected range: **0.8–1.3× hermes**, i.e. a real shot at under, contingent on S1 driving output to structured-only and S4 removing the screen call. This page's §7 measurement replaces this estimate.

---

## 6. Implementation order (each builds on the last)

1. **S1 — silent segments.** Smallest, isolating change; proves output drops without touching structure. (`segment_exec.ENGINE_SYSTEM_PROMPT`, `_run_segment`.)
2. **S2 — engine-rendered result.** `flow.result` template + engine renders summary; drop `emit_result`. (`runner.py`, workflow schema.)
3. **S4 — Tier-0 resolvers.** `variables[].resolver` + an engine pre-segment resolve pass that skips the LLM call when all branch vars are Tier-0. (`runner.py`, new `tier0_resolver.py`.)
4. **S3 — listDecision.** New step kind: structured list capture + per-element `evaluate_choices` + `collectInto`. (`runner.py`, `segment_exec.py`, `segments.py`, `build_workflow`.)

Each lands with unit tests and is independently verifiable. S1+S2 are pure wins on any workflow; S3+S4 are the list/deterministic capabilities.

---

## 7. Validation (acceptance gate)

Run `order_fulfillment` three ways (hermes, botcircuits-collapsed, botcircuits-S1–S4) on the **same model**, `repeats ≥ 3`, per-purpose token tags. Acceptance:

- **Accuracy = 100%** across all repeats (oracle match + branch-decision correctness).
- **Tokens/cost < hermes**, or — if not under — a documented, defensible figure with the exact output-token gap explained.

The resulting table replaces §5 and becomes the README headline.
