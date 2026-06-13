"""Engine-driven runner + build-time segment computation.

These cover the inversion-of-control core directly (no provider): the
engine owns the loop, batches non-branching steps into segments, evaluates
branches deterministically against captured slots, records per-branch audit
structs, and routes a REQUIRED-but-unfillable branch variable to a
clarification pause instead of a silent default-branch fallthrough.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.workflow.engine.runner import (
    SegmentResult,
    run_workflow_engine,
)
from botcircuits.agent.workflow.engine.segments import compute_segments


def _built(flow: dict) -> dict:
    """Attach build-time segments, mirroring what the workflow builder
    emits into `.build/`. The runner reads `flow['segments']`; without it
    it falls back to one-step-per-segment (covered separately)."""
    flow["segments"] = compute_segments(flow)
    return flow


def _linear_flow() -> dict:
    """start → a → b → c (no branches): one segment of three actions."""
    return {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "a"},
            "a": {"type": "agentAction", "settings": {"action": "do a"}, "next": "b"},
            "b": {"type": "agentAction", "settings": {"action": "do b"}, "next": "c"},
            "c": {"type": "agentAction", "settings": {"action": "do c"}},
        },
    }


def _branch_flow(required: bool = False) -> dict:
    """start → s1 (branch on color) → red | blue."""
    var = {"variableName": "color", "dataType": "string", "description": "the color"}
    if required:
        var["required"] = True
    return {
        "start": "start",
        "variables": [var],
        "steps": {
            "start": {"type": "start", "next": "s1"},
            "s1": {
                "type": "agentAction",
                "settings": {"action": "pick a color"},
                "next": "blue",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "color", "operator": "is", "value": "red"}
                    ],
                    "next": "red",
                }],
            },
            "red": {"type": "agentAction", "settings": {"action": "go red"}},
            "blue": {"type": "agentAction", "settings": {"action": "go blue"}},
        },
    }


# -- segment computation ----------------------------------------------------


def test_compute_segments_batches_linear_run():
    segs = compute_segments(_linear_flow())
    assert len(segs) == 1
    assert segs[0]["steps"] == ["a", "b", "c"]
    assert segs[0]["branchStep"] is None


def test_compute_segments_splits_on_branch():
    segs = compute_segments(_branch_flow())
    by_id = {s["id"]: s for s in segs}
    # s1 segment ends on the branch; red and blue are their own segments.
    assert by_id["start"]["branchStep"] == "s1"
    assert "red" in by_id and "blue" in by_id


# -- engine loop ------------------------------------------------------------


def _collect_runner():
    seen: list[list[str]] = []

    async def run(*, actions, branch_variables, system_notes, slots):
        seen.append(list(actions))
        return SegmentResult(text="ok", captured_slots={})

    return run, seen


def test_linear_runs_as_single_segment_call():
    run, seen = _collect_runner()
    res = asyncio.run(run_workflow_engine(
        _built(_linear_flow()), workflow_name="lin", run_segment=run))
    assert res.done and not res.paused
    # One LLM call for the whole 3-step linear run — cost scales with
    # branches (zero here → one segment), not steps.
    assert len(seen) == 1
    assert seen[0] == ["do a", "do b", "do c"]


def test_branch_takes_matching_path_from_captured_slot():
    order: list[list[str]] = []

    async def run(*, actions, branch_variables, system_notes, slots):
        order.append(list(actions))
        cap = {"color": "red"} if any(
            v["variableName"] == "color" for v in branch_variables) else {}
        return SegmentResult(text="ok", captured_slots=cap)

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.done
    flat = [a for tup in order for a in tup]
    assert "go red" in flat and "go blue" not in flat
    # A decision record was persisted for the branch.
    assert any(d.get("variable") == "color" for d in res.decisions)


def test_branch_defaults_when_optional_var_empty():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})  # never reports color

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=False)), workflow_name="br", run_segment=run))
    # Optional empty → default branch (blue), NOT a clarification pause.
    assert res.done and not res.paused


def test_required_unfilled_var_routes_to_clarification():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})  # never reports color

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=True)), workflow_name="br", run_segment=run))
    assert res.paused and not res.done
    assert res.paused_step == "start"
    assert "color" in res.question.lower() or "information" in res.question.lower()


def test_user_pause_yields_with_resume_cursor():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(paused=True, question="What color?")

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.paused
    assert res.question == "What color?"
    assert res.paused_step == "start"


def test_resolve_unfilled_backfills_before_branch():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})

    async def resolver(*, flow, step_id, variables, slots):
        return {"color": "red"}  # Tier-0/2 supplies what record_slots didn't

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=True)), workflow_name="br",
        run_segment=run, resolve_unfilled=resolver))
    # Backfilled → branch resolves, no clarification.
    assert res.done and not res.paused
    assert res.slots.get("color") == "red"
