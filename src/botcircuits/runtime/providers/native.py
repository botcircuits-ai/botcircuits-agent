"""Native runtime provider — the in-process BotCircuits agent loop.

A thin adapter, NOT a rewrite. It forwards the two engine callbacks to the
exact methods the engine used to receive directly:

  - `run_segment`    → `Agent._run_segment` (the cache-stable segment loop in
                       `agent/core.py`).
  - `resolve_slots`  → the Tier-0/Tier-2 closure built by
                       `agent.workflow._make_resolve_unfilled`.

Because it delegates to the existing code unchanged, the native path is
behavior-preserving: wiring the engine through this provider must produce the
same results as before. This is what keeps the refactor zero-regression and
lets `native` stay as the offline / CI fallback.
"""

from __future__ import annotations

from typing import Any

from botcircuits.runtime.base import AgentRuntimeProvider, EventSink
from botcircuits.agent.workflow.engine.runner import SegmentResult


class NativeRuntime(AgentRuntimeProvider):
    """Wrap a live `Agent` so the workflow engine can drive it as a provider."""

    name = "native"

    def __init__(self, agent, *, normalize_enabled: bool = True):
        # `agent` is a started `agent.core.Agent`. We hold it (not a copy) so
        # `_run_segment` reuses its tools / skills / MCP wiring.
        self._agent = agent
        # Build the Tier-0/Tier-2 backfill closure once, bound to the agent's
        # provider. Same factory the workflow tool used in-process, so slot
        # resolution behavior is identical.
        from botcircuits.agent.workflow import _make_resolve_unfilled

        self._resolve = _make_resolve_unfilled(
            provider=getattr(agent, "provider", None),
            normalize_enabled=normalize_enabled,
        )

    async def run_segment(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
        item_variables: list[dict] | None = None,
        data_variables: list[dict] | None = None,
        event_sink: EventSink | None = None,
    ) -> SegmentResult:
        return await self._agent._run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=system_notes,
            slots=slots,
            item_variables=item_variables,
            data_variables=data_variables,
            event_sink=event_sink,
        )

    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._resolve(
            flow=flow, step_id=step_id, variables=variables, slots=slots,
        )


__all__ = ["NativeRuntime"]
