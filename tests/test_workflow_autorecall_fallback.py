"""Fallback verification for Option 2 (model re-calls with args).

Two guarantees when the model FORGETS to re-call the workflow tool on a
branching step:

  1. The agent loop still advances the workflow deterministically — a
     terminal model turn with an active workflow injects an empty-args
     auto-recall (`wf-autorecall-*`), exactly as before Option 2.
  2. That empty-args re-entry runs the same resolution pipeline as the
     pre-Option-2 implementation: the deterministic slot resolver sees
     `raw_args={}` + the last user message, and Layer B still fires for
     whatever the resolver leaves unresolved.
"""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.core import Agent, _AUTO_RECALL_ID_PREFIX
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.workflow import workflow_tool
from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, ToolCall


def _var(name: str, dtype: str = "string", description: str = "") -> dict:
    return {"variableName": name, "dataType": dtype, "description": description}


def _branching_record() -> dict:
    """start → s1 (branches on order_status) → s_delivered | s_escalate."""
    return {
        "name": "wf_branch",
        "description": "test workflow",
        "flow": {
            "start": "start",
            "variables": [_var("order_status", "string", "delivery state")],
            "steps": {
                "start": {"type": "start", "next": "s1"},
                "s1": {
                    "type": "agentAction",
                    "settings": {"action": "look up the order status"},
                    "next": "s_escalate",
                    "choices": [
                        {
                            "operator": "OR",
                            "expressionList": [
                                {
                                    "variable": "order_status",
                                    "operator": "is",
                                    "value": "delivered",
                                }
                            ],
                            "next": "s_delivered",
                        }
                    ],
                },
                "s_delivered": {
                    "type": "agentAction",
                    "settings": {"action": "tell the user it was delivered"},
                },
                "s_escalate": {
                    "type": "agentAction",
                    "settings": {"action": "escalate to support"},
                },
            },
        },
    }


def _write_build(tmp_path, record: dict) -> None:
    build = tmp_path / ".build"
    build.mkdir(parents=True, exist_ok=True)
    (build / f"{record['name']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


class ScriptedProvider(LLMProvider):
    """Plays back canned LLMResponses; records each system prompt seen."""

    name = "scripted"
    model = "test"

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.seen_systems: list[str] = []

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse:
        self.seen_systems.append(system or "")
        return self.responses.pop(0)

    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):
        yield ("final", await self.complete(
            system, messages, tools, hosted_mcp, skills, max_tokens))


def _text(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn", raw=None)


def _call(name: str, args: dict) -> LLMResponse:
    return LLMResponse(
        text="", stop_reason="tool_use", raw=None,
        tool_calls=[ToolCall(id="t1", name=name, arguments=args)],
    )


def test_loop_auto_recalls_when_model_forgets(tmp_path, monkeypatch):
    """End-to-end through the REAL agent loop: the model kicks off the
    workflow, is asked (directive + reminder) to re-call with the branch
    values, ignores it — and the loop's empty-args auto-recall still
    advances the workflow, branching deterministically via the slot
    resolver (no Layer B: the workflow tool is registered provider-less).
    """
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    provider = ScriptedProvider([
        _call("wf_branch", {}),       # round 1: model starts the workflow
        _text("I checked the order."),  # round 2: model acts, FORGETS to re-call
        _text("all done"),            # round 3: workflow finished → terminal
    ])
    reg = ToolRegistry()
    reg.register(workflow_tool(_branching_record()))  # provider=None → no Layer B

    async def run():
        async with Agent(provider=provider, tools=reg,
                         local_skills_paths=[]) as agent:
            return await agent.chat("the courier says delivered"), agent

    (reply, sid), agent = asyncio.run(run())
    assert reply == "all done"

    convo = agent.store.get_or_create(sid)
    auto_calls = [
        b for m in convo.messages if m.role == "assistant"
        for b in m.blocks
        if b.get("type") == "tool_call"
        and b["id"].startswith(_AUTO_RECALL_ID_PREFIX)
    ]
    # Exactly one loop-injected recall, with EMPTY args — deterministic
    # fallback, not dependent on anything the model did or didn't pass.
    assert len(auto_calls) == 1
    assert auto_calls[0]["name"] == "wf_branch"
    assert auto_calls[0]["arguments"] == {}

    # The recall's result shows the branch resolved to s_delivered: the
    # resolver matched the authored choice literal in the user's message.
    recall_results = [
        b for m in convo.messages if m.role == "user"
        for b in m.blocks
        if b.get("type") == "tool_result"
        and b["tool_call_id"] == auto_calls[0]["id"]
    ]
    assert len(recall_results) == 1
    assert "tell the user it was delivered" in recall_results[0]["content"]

    # Round 2's system prompt DID ask the model to re-call with values
    # (which it ignored) — proving the fallback covered a real "forgot".
    assert "call 'wf_branch' passing the values" in provider.seen_systems[1]
    assert "- order_status (string): delivery state" in provider.seen_systems[1]


def test_empty_args_recall_invokes_resolver_like_before(tmp_path, monkeypatch):
    """run_workflow-level parity: an empty-args re-entry hands the slot
    resolver exactly what the pre-Option-2 implementation did —
    raw_args={} plus the last user message — and branches on its result.
    """
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    seen: dict = {}
    orig_resolve = wf_local.resolve_slots

    def spy(**kwargs):
        seen.update(kwargs)
        return orig_resolve(**kwargs)

    monkeypatch.setattr(wf_local, "resolve_slots", spy)

    first = asyncio.run(wf_local.run_workflow("wf_branch", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_branch", {},
        session_id=first["session_id"],
        last_user_message="the courier says delivered",
    ))

    assert seen["raw_args"] == {}
    assert seen["last_user_message"] == "the courier says delivered"
    assert seen["step_id"] == "s1"
    assert [v["variableName"] for v in seen["variables"]] == ["order_status"]
    assert second["running_step"] == "s_delivered"


def test_empty_args_recall_still_falls_back_to_layer_b(tmp_path, monkeypatch):
    """When the resolver can't satisfy the branch variable from an
    empty-args recall, Layer B still runs over the transcript context —
    same degradation chain as before Option 2.
    """
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    recorded: dict = {}

    async def fake_normalize(**kwargs):
        recorded.update(kwargs)
        return {"order_status": "delivered"}

    monkeypatch.setattr(wf_local, "normalize_variables", fake_normalize)
    provider = object()  # run_workflow only checks `is not None`

    first = asyncio.run(wf_local.run_workflow("wf_branch", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_branch", {},
        session_id=first["session_id"],
        provider=provider,
        # No choice literal, no number, no yes/no → resolver leaves
        # order_status unresolved → Layer B must be consulted.
        last_user_message="it arrived at my door this morning",
    ))

    assert [v["variableName"] for v in recorded["variables"]] == ["order_status"]
    assert recorded["raw_args"] == {}
    assert recorded["last_user_message"] == "it arrived at my door this morning"
    assert second["running_step"] == "s_delivered"
