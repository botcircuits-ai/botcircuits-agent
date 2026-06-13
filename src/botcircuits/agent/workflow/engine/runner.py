"""Engine-driven workflow runner — the inversion-of-control loop.

Once a workflow starts, the ENGINE owns the loop. The LLM is a
subroutine the engine invokes per branch-delimited *segment* with a
constant-size, cache-stable prompt (see `agent.core.Agent._run_segment`).
The state machine — not the conversation history — is the memory.

Contrast with `executor.run_flow` (the old LLM-driven path): there the
LLM drove and re-called the workflow tool to advance one step at a time,
replaying the whole history every round. Here the engine walks the
compiled `flow["segments"]`, calls the LLM once per segment, captures the
branch slots the call produced, evaluates the branch deterministically
(`evaluate_choices` — unchanged), and advances itself. The model can no
longer skip a step, reorder, or imitate stale history.

The loop yields control back to the conversational agent on exactly two
events:
  - workflow end          → `EngineResult(done=True, summary=...)`
  - user-interaction pause → `EngineResult(paused=True, question=...)`
    (a `question`-kind step, or a clarification step the engine inserts
    when a branch slot can't be filled confidently).

`run_workflow_engine` is provider-agnostic: it never calls the provider
directly. It calls back into the passed-in agent via `agent._run_segment`,
which owns the single provider round-trip + tool execution and reuses the
agent's existing tools / skills / MCP wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from botcircuits.agent.workflow.engine.handlers.choice import evaluate_choices
from botcircuits.agent.workflow.engine.utils import fill_text_with_slots
from botcircuits.agent.workflow.variable_normalizer import variables_for_step

#: Upper bound on segments walked in one run — guards against a branch
#: cycle the deterministic graph could otherwise spin on forever.
_MAX_SEGMENTS = 500


@dataclass
class SegmentResult:
    """What `Agent._run_segment` returns for one segment call."""
    #: The assistant's final text for the segment (surfaced if the
    #: workflow ends right after).
    text: str = ""
    #: Branch slots the model reported via the synthetic `record_slots`
    #: tool (Tier 1), already filtered to the segment's branch variables.
    captured_slots: dict[str, Any] = field(default_factory=dict)
    #: True when the model asked the user a question via `human_feedback`
    #: during the segment — the engine yields so the user can reply.
    paused: bool = False
    #: The question to surface when `paused`.
    question: str = ""


@dataclass
class EngineResult:
    """What `run_workflow_engine` hands back to the workflow tool."""
    done: bool = False
    paused: bool = False
    summary: str = ""
    question: str = ""
    #: Segment head to resume from after a user-interaction pause.
    paused_step: str | None = None
    #: Final slot values, for the summary line and the eval harness.
    slots: dict[str, Any] = field(default_factory=dict)
    #: Per-branch audit records (§6).
    decisions: list[dict] = field(default_factory=list)


class SegmentRunner(Protocol):
    """The single capability the runner needs from the agent: run one
    segment (constant-size prompt + segment tools + record_slots) and
    return its result. Implemented by `Agent._run_segment`."""

    async def __call__(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
    ) -> SegmentResult: ...


def _segments_for(flow: dict) -> list[dict]:
    """The compiled segment list, or a one-step-per-segment fallback when
    a workflow predates segment computation (un-rebuilt `.build/`)."""
    segments = flow.get("segments")
    if isinstance(segments, list) and segments:
        return segments
    # Fallback: every pausing step is its own singleton segment. Branch
    # steps mark themselves so the runner still evaluates choices.
    steps = flow.get("steps") or {}
    out: list[dict] = []
    for step_id, step in steps.items():
        if step.get("type") not in ("agentAction", "question"):
            continue
        is_branch = bool(step.get("choices") or step.get("conditions"))
        out.append({
            "id": step_id,
            "steps": [step_id],
            "branchStep": step_id if is_branch else None,
        })
    return out


def _segment_index(segments: list[dict]) -> dict[str, dict]:
    return {s["id"]: s for s in segments}


def _action_texts(flow: dict, step_ids: list[str], slots: dict) -> list[str]:
    """Slot-interpolated action text for each pausing step in a segment."""
    steps = flow.get("steps") or {}
    ctx = {"slots": slots}
    out: list[str] = []
    for sid in step_ids:
        step = steps.get(sid) or {}
        action = (step.get("settings") or {}).get("action") or ""
        out.append(fill_text_with_slots(action, ctx) if action else "")
    return [a for a in out if a]


def _eval_message(workflow_name: str, slots: dict) -> dict:
    """Build the minimal `message` shape `evaluate_choices` reads from
    (it pulls `data.sessionContext.slots`)."""
    return {
        "inputText": "",
        "channel": "agent",
        "data": {"sessionContext": {"slots": slots}},
    }


def _record_decision(
    step: dict,
    matched_next: str | None,
    default_next: str | None,
    slots: dict,
    captured_keys: set[str],
) -> list[dict]:
    """One audit struct per condition the branch step evaluated (§6)."""
    records: list[dict] = []
    matched = matched_next is not None and matched_next != default_next
    for choice in step.get("choices") or []:
        for expr in choice.get("expressionList") or []:
            var = expr.get("variable")
            records.append({
                "variable": var,
                "operator": expr.get("operator"),
                "value": expr.get("value"),
                "slot_value": slots.get(var) if isinstance(var, str) else None,
                "slot_source": (
                    "llm_record_slots" if var in captured_keys else "deterministic"
                ),
                "matched_choice": (choice.get("next") == matched_next),
                "llm_extracted": var in captured_keys,
            })
    records.append({
        "matched_next": matched_next,
        "default_next": default_next,
        "branched": matched,
    })
    return records


async def run_workflow_engine(
    flow: dict,
    *,
    workflow_name: str,
    run_segment: SegmentRunner,
    start_step_id: str | None = None,
    slots: dict[str, Any] | None = None,
    resolve_unfilled: Callable[..., Awaitable[dict]] | None = None,
) -> EngineResult:
    """Drive `flow` segment-by-segment until it ends or pauses for the user.

    `run_segment` is the agent callback that performs one segment's
    actions and returns the branch slots it captured. `slots` seeds the
    slot context (e.g. args the trigger call carried).

    `resolve_unfilled` is the optional Tier-0/Tier-2 slot backfill hook,
    called at a branch point with `(flow, step_id, variables, slots)` when
    one or more branch variables are still empty after Tier-1 capture. It
    returns a `{variableName: value}` dict of any it could satisfy
    (deterministic resolver first, cheap-model extraction last). When a
    branch variable is STILL empty after this, the engine routes to a
    clarification question instead of silently taking the default branch.
    """
    segments = _segments_for(flow)
    if not segments:
        return EngineResult(done=True, summary=f"workflow {workflow_name}: no steps")

    by_id = _segment_index(segments)
    steps = flow.get("steps") or {}
    slots = dict(slots or {})
    decisions: list[dict] = []

    # Pick the starting segment: the one whose head is the requested start
    # step, else the first segment (graph entry).
    current = by_id.get(start_step_id) if start_step_id else None
    if current is None:
        current = segments[0]

    last_text = ""
    walked = 0
    while current is not None:
        walked += 1
        if walked > _MAX_SEGMENTS:
            return EngineResult(
                done=True,
                summary=f"workflow {workflow_name}: stopped after "
                        f"{_MAX_SEGMENTS} segments (branch cycle?)",
                slots=slots,
                decisions=decisions,
            )

        branch_step_id = current.get("branchStep")
        branch_variables = (
            variables_for_step(flow, branch_step_id) if branch_step_id else []
        )
        actions = _action_texts(flow, current.get("steps") or [], slots)

        seg = await run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=[],
            slots=slots,
        )
        last_text = seg.text or last_text

        # User-interaction pause: yield control so the user can reply. The
        # next workflow-tool call resumes from this same segment.
        if seg.paused:
            return EngineResult(
                paused=True,
                question=seg.question,
                paused_step=current.get("id"),
                slots=slots,
                decisions=decisions,
            )

        # Tier-1 capture: fold the reported branch slots into context.
        captured_keys = set(seg.captured_slots)
        if seg.captured_slots:
            slots.update(seg.captured_slots)

        # Non-branching segment: advance to the segment seeded by the last
        # step's static `next` (computed at build time as another segment
        # head), or end the workflow.
        if not branch_step_id:
            nxt = _static_next_after(current, steps)
            current = by_id.get(nxt) if nxt else None
            continue

        # Branch segment. Backfill any still-empty branch variable via the
        # Tier-0/Tier-2 hook before evaluating, so a value the model didn't
        # report through record_slots (but the user clearly supplied) still
        # routes correctly.
        branch_step = steps.get(branch_step_id) or {}
        missing = _unfilled(branch_variables, slots)
        if missing and resolve_unfilled is not None:
            backfilled = await resolve_unfilled(
                flow=flow,
                step_id=branch_step_id,
                variables=missing,
                slots=slots,
            )
            if backfilled:
                slots.update(backfilled)
                captured_keys |= set(backfilled)

        # Required-but-unfillable after backfill: route to clarification
        # rather than silently defaulting. The runner yields a question; the
        # resume cursor stays on this segment so the user's reply re-runs it.
        # Unmarked (optional) empties fall through to the default branch.
        still_missing = _required_unfilled(branch_variables, slots)
        if still_missing:
            question = _clarification_question(branch_step, still_missing)
            return EngineResult(
                paused=True,
                question=question,
                paused_step=current.get("id"),
                slots=slots,
                decisions=decisions,
            )

        # Evaluate deterministically against current slots.
        default_next = branch_step.get("next")
        chosen = evaluate_choices(
            branch_step.get("choices") or [],
            _eval_message(workflow_name, slots),
            default_next,
        )
        decisions.extend(_record_decision(
            branch_step, chosen, default_next, slots, captured_keys,
        ))
        current = by_id.get(chosen) if chosen else None

    summary = _summary_line(workflow_name, last_text, slots)
    return EngineResult(
        done=True, summary=summary, slots=slots, decisions=decisions,
    )


def _unfilled(variables: list[dict], slots: dict) -> list[dict]:
    """Branch variables whose slot value is still empty/absent."""
    out: list[dict] = []
    for v in variables:
        name = v.get("variableName")
        if not isinstance(name, str):
            continue
        if slots.get(name) in (None, ""):
            out.append(v)
    return out


def _required_unfilled(variables: list[dict], slots: dict) -> list[dict]:
    """Subset of `_unfilled` that should trigger a clarification rather than
    a silent default-branch fallthrough.

    A branch variable forces clarification only when it is explicitly marked
    `required: true` in the flow schema. An unmarked variable left empty is
    a legitimate "no value applies" — the deterministic default branch is the
    correct route (e.g. an optional early-termination id), and over-asking
    would regress the common path. The first-class clarification path (§4)
    is reserved for variables the workflow author declared mandatory.
    """
    return [
        v for v in _unfilled(variables, slots)
        if v.get("required") is True
    ]


def _clarification_question(branch_step: dict, missing: list[dict]) -> str:
    """A user-facing question asking for the branch variables that could
    not be filled — the first-class clarification path that replaces a
    silent default-branch fallthrough (§4)."""
    names = [v.get("description") or v.get("variableName")
             for v in missing if v.get("variableName")]
    action = (branch_step.get("settings") or {}).get("action") or ""
    asked = "; ".join(str(n) for n in names if n)
    if action:
        return (
            f"To continue, I need a bit more information: {asked}. "
            f"(Step: {action})"
        )
    return f"To continue, please provide: {asked}."


def _static_next_after(segment: dict, steps: dict) -> str | None:
    """The `next` of a non-branching segment's last step — the head of the
    segment that runs next."""
    ordered = segment.get("steps") or []
    if not ordered:
        return None
    last = steps.get(ordered[-1]) or {}
    nxt = last.get("next")
    return nxt if isinstance(nxt, str) and nxt else None


def _summary_line(workflow_name: str, last_text: str, slots: dict) -> str:
    """The single line injected into conversational history on completion
    (§5)."""
    slot_part = ""
    filled = {
        k: v for k, v in slots.items()
        if v not in (None, "") and not k.startswith("__")
    }
    if filled:
        slot_part = f", slots {filled}"
    outcome = (last_text or "completed").strip()
    if len(outcome) > 200:
        outcome = outcome[:200] + "…"
    return f"workflow {workflow_name} completed: {outcome}{slot_part}"
