"""Agent — the multi-round tool-use loop.

Coordinates a single `LLMProvider`, a `ToolRegistry`, optional MCP
servers, and optional skills. Owns the `ConversationStore` so callers
can resume sessions across calls.

Use as an async context manager:

    async with Agent(provider=...) as agent:
        reply, sid = await agent.chat("hello")
        async for ev in agent.chat_stream("..."):
            ...
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, Message, StreamEvent, ToolCall
from botcircuits.agent.mcp import LocalMCPManager, MCPServer
from botcircuits.agent.skill import (
    DEFAULT_SKILL_ROOTS,
    SkillSpec,
    discover_skills,
    skill_to_tool,
)
from botcircuits.agent.store import ConversationStore
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.workflow import active_workflow_names

MAX_AGENT_STEPS = 500

# Truncation cap on the last-assistant-message we hand to tools via context.
# Variable normalization (the workflow tool's main consumer of this field)
# only needs the most recent prose-y reply, not the model's entire monologue.
_CONTEXT_LAST_ASSISTANT_CHARS = 2000


def _last_assistant_text(messages: list[Message]) -> str:
    """Pull the most recent assistant `text` block out of `messages` and
    truncate it. Returns "" when no assistant text exists yet (e.g., the
    workflow tool is called on the very first turn before the model has
    said anything beyond a tool call).
    """
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for b in m.blocks:
            if b.get("type") == "text" and b.get("text"):
                text = b["text"]
                if len(text) > _CONTEXT_LAST_ASSISTANT_CHARS:
                    return text[:_CONTEXT_LAST_ASSISTANT_CHARS] + "…"
                return text
    return ""


def _last_user_text(messages: list[Message]) -> str:
    """Pull the most recent user `text` block out of `messages` and
    truncate it. Tool-result blocks (which also live on user-role messages)
    are skipped — we want the human's actual utterance, not tool output.
    Returns "" when no user text exists yet.
    """
    for m in reversed(messages):
        if m.role != "user":
            continue
        for b in m.blocks:
            if b.get("type") == "text" and b.get("text"):
                text = b["text"]
                if len(text) > _CONTEXT_LAST_ASSISTANT_CHARS:
                    return text[:_CONTEXT_LAST_ASSISTANT_CHARS] + "…"
                return text
    return ""


def _available_workflow_tools(reg: ToolRegistry) -> list[tuple[str, str]]:
    """Return (name, description) for every workflow tool on `reg`.

    Workflow tools are tagged with `_workflow_state` by `workflow_tool()`,
    which is how we tell them apart from built-ins and MCP tools.
    """
    out: list[tuple[str, str]] = []
    for tool in reg.all():
        if getattr(tool, "_workflow_state", None) is None:
            continue
        out.append((tool.name, tool.description or ""))
    return out


def _with_workflow_reminder(system: str | None, reg: ToolRegistry) -> str | None:
    """Append a workflow-related reminder to `system`.

    Two cases:
      - A workflow is mid-run: remind the model to re-call it to advance.
      - No workflow is active but workflow tools exist: remind the model
        that those tools MUST be called as the first action when the
        user's request matches one — do NOT ask clarifying questions in
        prose first, because the workflow itself drives the conversation.
        Without this, long histories cause the model to imitate its own
        prior "ask topic, then call tool" pattern and skip the tool call.
    """
    names = active_workflow_names(reg)
    if names:
        name = names[0]
        reminder = (
            f"\n\n[Active workflow] The workflow tool '{name}' is mid-execution. "
            f"After you finish the action of the current step, you MUST call "
            f"'{name}' again (with empty args) to receive the next step. "
            f"Skip the re-call only when the current step asks the user a "
            f"question and you need their reply first."
        )
        return (system or "") + reminder

    available = _available_workflow_tools(reg)
    if not available:
        return system
    lines = [f"  - '{n}': {d}" for n, d in available]
    reminder = (
        "\n\n[Available workflows] The following workflow tools are "
        "registered:\n"
        + "\n".join(lines)
        + "\n\nCalling the matching workflow tool is MANDATORY and MUST be "
        "your VERY FIRST action whenever the user's request matches one of "
        "the workflows above. Do NOT ask clarifying questions in prose "
        "first — the workflow itself drives the conversation (its first "
        "step may ask the user for inputs). Call the tool with empty args "
        "`{}` to begin; the tool will return the next step to perform. "
        "Do not imitate earlier turns where you appeared to ask a question "
        "directly — that question came from the workflow tool's output, "
        "not from you skipping the call."
    )
    return (system or "") + reminder


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry | None = None,
        mcp_servers: list[MCPServer] | None = None,
        skills: list[SkillSpec] | None = None,
        local_skills_paths: list[str | Path] | None = None,
        max_tokens: int = 4096,
        max_steps: int = MAX_AGENT_STEPS,
        store: ConversationStore | None = None,
        enable_workflows: bool = True,
    ):
        self.provider = provider
        self.user_tools = tools or ToolRegistry()
        self.skills = skills or []
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.store = store or ConversationStore()
        # When False, workflow tools registered on the registry are
        # hidden from the provider call and the workflow-related
        # system-prompt reminder is suppressed. The tools stay in the
        # registry (so other code can still inspect them) — only the
        # surface exposed to the LLM changes. Default True keeps the
        # existing behavior for every caller that doesn't ask
        # otherwise; the eval framework's "no workflow" baseline mode
        # is the one consumer that flips this off.
        self.enable_workflows = enable_workflows

        # Roots scanned for filesystem skills on start(). Callers can
        # pass an explicit list (or []) to override the default project
        # locations. Paths are resolved against the current working
        # directory at discovery time, not __init__ time, so an Agent
        # created in one cwd and started in another still finds the
        # right skills.
        raw_roots = (local_skills_paths
                     if local_skills_paths is not None
                     else list(DEFAULT_SKILL_ROOTS))
        self.local_skills_paths: list[Path] = [Path(p) for p in raw_roots]
        self.local_skills: list = []  # populated on start()

        # Split MCP servers by mode; auto-promote hosted -> local on
        # providers that don't support hosted MCP, so a single config
        # works across providers.
        servers = mcp_servers or []
        if not provider.supports_hosted_mcp():
            for s in servers:
                if s.mode == "hosted":
                    print(f"[info] Provider '{provider.name}' lacks hosted MCP; "
                          f"running '{s.name}' locally.")
                    s.mode = "local"

        self.hosted_mcp = [s for s in servers if s.mode == "hosted"]
        self._local_mcp = LocalMCPManager([s for s in servers if s.mode == "local"])
        self._tools_built = False
        self.tools = ToolRegistry()

    # -- async lifecycle ----------------------------------------------------

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Open MCP sessions and merge user tools + MCP tools + filesystem
        skills into one registry."""
        if self._tools_built:
            return
        await self._local_mcp.start()
        for t in self.user_tools.all():
            self.tools.register(t)
        for t in self._local_mcp.tools():
            self.tools.register(t)
        # Filesystem skills become tools. They run last so user tools
        # and MCP tools win on name collisions — a skill named `shell`
        # shouldn't shadow the real shell tool. Skills marked
        # `disable-model-invocation: true` are still loaded (so the CLI
        # can dispatch them on /skill-name) but not registered as
        # callable tools.
        self.local_skills = discover_skills(self.local_skills_paths)
        for sk in self.local_skills:
            if sk.disable_model_invocation:
                continue
            if self.tools.has(sk.name):
                continue
            self.tools.register(skill_to_tool(sk))
        self._tools_built = True

    async def aclose(self) -> None:
        # Terminate any background shell processes the model started.
        # Imported lazily so this module doesn't pull tools at import time
        # (tools depend on registry, which lives next door).
        from .tools.builtins import _bg
        await _bg.terminate_all()
        await self._local_mcp.stop()
        await self.provider.aclose()

    # -- workflow gating ---------------------------------------------------

    def _exposed_tools(self) -> list:
        """Tool list handed to the provider for the next call.

        With workflows enabled (the default) this is just every tool
        on the registry. With workflows disabled, workflow tools —
        identified by the `_workflow_state` attribute set by
        `workflow.workflow_tool()` — are filtered out. They stay in
        the registry so the rest of the agent code can still see them
        (the eval framework, /tools listing, etc.); only the surface
        the model sees changes.
        """
        all_tools = self.tools.all()
        if self.enable_workflows:
            return all_tools
        return [t for t in all_tools
                if getattr(t, "_workflow_state", None) is None]

    def _system_with_reminder(self, system: str | None) -> str | None:
        """System prompt with the workflow reminder block conditionally
        attached. Skipped entirely when workflows are disabled — the
        reminder talks about workflow tools the model can't see, which
        would just confuse it."""
        if not self.enable_workflows:
            return system
        return _with_workflow_reminder(system, self.tools)

    # -- non-streaming chat -------------------------------------------------

    async def chat(self, user_input: str, session_id: str | None = None,
                   system: str | None = None) -> tuple[str, str]:
        """Send a user message, run the loop until the model stops asking
        for tools, return (assistant_text, session_id)."""
        if not self._tools_built:
            await self.start()

        convo = self.store.get_or_create(session_id, system=system)
        async with convo.lock:
            convo.messages.append(Message(
                role="user",
                blocks=[{"type": "text", "text": user_input}],
            ))

            for _ in range(self.max_steps):
                resp = await self.provider.complete(
                    system=self._system_with_reminder(convo.system),
                    messages=convo.messages,
                    tools=self._exposed_tools(),
                    hosted_mcp=self.hosted_mcp, skills=self.skills,
                    max_tokens=self.max_tokens,
                )

                assistant_blocks: list[dict] = []
                if resp.text:
                    assistant_blocks.append({"type": "text", "text": resp.text})
                for tc in resp.tool_calls:
                    assistant_blocks.append({
                        "type": "tool_call",
                        "id": tc.id, "name": tc.name, "arguments": tc.arguments,
                    })
                convo.messages.append(Message(role="assistant",
                                              blocks=assistant_blocks))

                if resp.stop_reason != "tool_use" or not resp.tool_calls:
                    return resp.text, convo.session_id

                # Build tool-invocation context once per turn. The same
                # snapshot is handed to every tool call in this round.
                tool_context = {
                    "last_assistant_message": _last_assistant_text(convo.messages),
                    "last_user_message": _last_user_text(convo.messages),
                    "session_id": convo.session_id,
                }
                # Run all tool calls concurrently.
                results = await asyncio.gather(*[
                    self.tools.run(tc.name, tc.arguments, tool_context)
                    for tc in resp.tool_calls
                ])
                result_blocks = [
                    {
                        "type": "tool_result",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": output,
                        "is_error": is_error,
                    }
                    for tc, (output, is_error) in zip(resp.tool_calls, results)
                ]
                convo.messages.append(Message(role="user", blocks=result_blocks))

            return "[agent stopped: hit max_steps]", convo.session_id

    # -- streaming chat -----------------------------------------------------

    async def chat_stream(self, user_input: str, session_id: str | None = None,
                          system: str | None = None) -> AsyncIterator[StreamEvent]:
        """Async generator yielding StreamEvents through the full agent loop.

        Tool calls and tool results are surfaced as discrete events so a UI
        can show 'calling tool X...' between text deltas.
        """
        if not self._tools_built:
            await self.start()

        convo = self.store.get_or_create(session_id, system=system)
        sid = convo.session_id

        async with convo.lock:
            convo.messages.append(Message(
                role="user",
                blocks=[{"type": "text", "text": user_input}],
            ))

            try:
                final_text = ""
                hit_step_limit = True
                for _ in range(self.max_steps):
                    final_resp: LLMResponse | None = None
                    async for kind, payload in self.provider.stream(
                        system=self._system_with_reminder(convo.system),
                        messages=convo.messages,
                        tools=self._exposed_tools(),
                        hosted_mcp=self.hosted_mcp,
                        skills=self.skills, max_tokens=self.max_tokens,
                    ):
                        if kind == "text_delta":
                            yield StreamEvent(type="text_delta", text=payload,
                                              session_id=sid)
                        elif kind == "final":
                            final_resp = payload
                    assert final_resp is not None, "provider didn't yield 'final'"

                    # Persist the assistant turn.
                    assistant_blocks: list[dict] = []
                    if final_resp.text:
                        assistant_blocks.append({"type": "text",
                                                 "text": final_resp.text})
                    for tc in final_resp.tool_calls:
                        assistant_blocks.append({
                            "type": "tool_call",
                            "id": tc.id, "name": tc.name,
                            "arguments": tc.arguments,
                        })
                    convo.messages.append(Message(role="assistant",
                                                  blocks=assistant_blocks))

                    # Surface tool-call decisions before running them.
                    for tc in final_resp.tool_calls:
                        yield StreamEvent(type="tool_call", tool_call=tc,
                                          session_id=sid)

                    yield StreamEvent(type="turn_end", session_id=sid)

                    if final_resp.stop_reason != "tool_use" or not final_resp.tool_calls:
                        final_text = final_resp.text
                        hit_step_limit = False
                        break

                    # Build tool-invocation context once per turn.
                    tool_context = {
                        "last_assistant_message":
                            _last_assistant_text(convo.messages),
                        "last_user_message":
                            _last_user_text(convo.messages),
                        "session_id": sid,
                    }

                    # Execute tools concurrently; surface each as it lands.
                    async def _run(tc: ToolCall):
                        out, err = await self.tools.run(
                            tc.name, tc.arguments, tool_context,
                        )
                        return tc, out, err

                    tasks = [asyncio.create_task(_run(tc))
                             for tc in final_resp.tool_calls]
                    results: list[tuple[ToolCall, str, bool]] = []
                    for coro in asyncio.as_completed(tasks):
                        tc, out, err = await coro
                        results.append((tc, out, err))
                        yield StreamEvent(type="tool_result",
                                          tool_call_id=tc.id, text=out,
                                          is_error=err, session_id=sid)

                    # Append results in original order (call/result pairing).
                    by_id = {tc.id: (out, err) for tc, out, err in results}
                    result_blocks = []
                    for tc in final_resp.tool_calls:
                        out, err = by_id[tc.id]
                        result_blocks.append({
                            "type": "tool_result",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": out,
                            "is_error": err,
                        })
                    convo.messages.append(Message(role="user",
                                                  blocks=result_blocks))

                if hit_step_limit:
                    final_text = "[agent stopped: hit max_steps]"

                yield StreamEvent(type="done", text=final_text, session_id=sid)

            except Exception as e:
                yield StreamEvent(type="error",
                                  text=f"{type(e).__name__}: {e}",
                                  session_id=sid)
