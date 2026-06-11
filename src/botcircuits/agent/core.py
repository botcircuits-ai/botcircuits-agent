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
import json
import uuid
from pathlib import Path
from typing import AsyncIterator, Literal

from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, Message, StreamEvent, ToolCall
from botcircuits.agent.mcp import LocalMCPManager, MCPServer
from botcircuits.agent.react import (
    format_observation,
    parse_react_step,
    render_react_preamble,
)
from botcircuits.agent.skill import (
    DEFAULT_SKILL_ROOTS,
    SkillSpec,
    discover_skills,
    skill_to_tool,
)
from botcircuits.agent.store import ConversationStore
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.builtins.human_feedback import HUMAN_FEEDBACK_TOOL
from botcircuits.agent.workflow import (
    active_workflow_names,
    workflow_branch_variables,
)
from botcircuits.agent.workflow.cli_commands import render_branch_variable_lines

MAX_AGENT_STEPS = 500

# Synthetic id prefix for the workflow tool calls the loop injects to
# advance an active workflow after the model stops acting. Lets us tell
# loop-injected calls apart from model-issued ones in history if needed.
_AUTO_RECALL_ID_PREFIX = "wf-autorecall-"

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

    Three cases:
      - A workflow is mid-run on a NON-branching step: tell the model to
        act on the current step only. The agent loop auto-recalls the
        workflow tool once the model finishes acting (no tool calls
        left), so the model must NOT call the workflow tool itself —
        that would double-advance.
      - A workflow is mid-run on a BRANCHING step (it has pending branch
        variables): tell the model to act on the step first, then
        re-call the workflow tool with the values it observed — the
        slots ride the model's own tool call (the resolver's
        highest-priority source) instead of being re-derived from a
        transcript snapshot. The loop's empty-args auto-recall still
        covers a model that forgets.
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
        branch_lines = render_branch_variable_lines(
            workflow_branch_variables(reg, name)
        )
        if branch_lines:
            reminder = (
                f"\n\n[Active workflow] The workflow tool '{name}' is "
                f"mid-execution on a branching step. FIRST perform the "
                f"action of the current step (call a tool, send a reply, "
                f"or call 'human_feedback' if the step asks the user a "
                f"question — then wait for their reply). Once the step is "
                f"genuinely complete, call '{name}' passing the values you "
                f"observed for these arguments — they decide the next "
                f"step. Omit any you don't actually have; never invent "
                f"values:\n{branch_lines}"
            )
        else:
            reminder = (
                f"\n\n[Active workflow] The workflow tool '{name}' is "
                f"mid-execution. Perform ONLY the action of the current "
                f"step (call a tool, send a reply, or call "
                f"'human_feedback' if the step asks the user a question). "
                f"Do NOT call '{name}' yourself — the next step is "
                f"requested for you automatically once you finish acting."
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


def _human_feedback_pause(
    tool_calls: list[ToolCall],
    results: list[tuple[str, bool]],
) -> str | None:
    """If a `human_feedback` call ran this round, return the question to
    surface to the user (so the loop can pause); else None.

    `human_feedback`'s handler returns `{"paused": true, "question": ...}`,
    JSON-encoded into the result text. We match by tool name and pull the
    question back out of that payload, falling back to the model's own
    `question` argument, then the raw result text.
    """
    for tc, (output, _is_error) in zip(tool_calls, results):
        if tc.name != HUMAN_FEEDBACK_TOOL:
            continue
        question = ""
        try:
            payload = json.loads(output)
            if isinstance(payload, dict):
                question = payload.get("question") or ""
        except (ValueError, TypeError):
            question = ""
        if not question and isinstance(tc.arguments, dict):
            question = tc.arguments.get("question") or ""
        return question or output
    return None


def _auto_recall_calls(reg: ToolRegistry) -> list[ToolCall]:
    """Synthetic workflow tool calls that advance every active workflow.

    Called when the model produced no tool calls of its own but a
    workflow is still mid-run: the loop injects these to fetch the next
    step (re-entry runs slot normalization inside the workflow tool),
    instead of relying on the model to remember to re-call it. Empty
    args — the resolver/normalizer fall back to the recent transcript.

    For branching steps this is the FALLBACK path only: the step
    directive and the [Active workflow] reminder ask the model to
    re-call the workflow tool itself with the branch variables as args
    (which suppresses auto-recall, since the turn then has tool calls).
    A model that forgets degrades to this empty-args recall, never to a
    stall.

    Normally there's exactly one active workflow, but we handle several
    defensively (one call each).
    """
    return [
        ToolCall(
            id=f"{_AUTO_RECALL_ID_PREFIX}{uuid.uuid4().hex[:8]}",
            name=name,
            arguments={},
        )
        for name in active_workflow_names(reg)
    ]


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
        mode: Literal["native", "react"] = "native",
    ):
        self.provider = provider
        self.user_tools = tools or ToolRegistry()
        self.skills = skills or []
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.store = store or ConversationStore()
        # Tool-use strategy:
        #   "native" — hand tools to the provider's structured tool-use API
        #     and read resp.tool_calls back. The default; most robust.
        #   "react"  — describe tools in the system prompt and parse a
        #     Thought/Action/Action Input text block out of the model's
        #     reply (see agent/react.py). Works on any provider and yields
        #     a visible reasoning trace, at the cost of parse brittleness.
        self.mode = mode
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
        """Tool list handed to the provider's structured tool-use API.

        With workflows enabled (the default) this is just every tool
        on the registry. With workflows disabled, workflow tools —
        identified by the `_workflow_state` attribute set by
        `workflow.workflow_tool()` — are filtered out. They stay in
        the registry so the rest of the agent code can still see them
        (the eval framework, /tools listing, etc.); only the surface
        the model sees changes.

        In ReAct mode this returns [] — tools are described in the
        system prompt instead (see `_react_tools` / `_system_with_reminder`),
        so the provider gets no structured tool spec.
        """
        if self.mode == "react":
            return []
        return self._react_tools()

    def _react_tools(self) -> list:
        """The tools the model is allowed to use this call, after workflow
        gating. Shared by both modes: native passes these to the provider's
        tool API, react renders them into the prompt preamble."""
        all_tools = self.tools.all()
        if self.enable_workflows:
            return all_tools
        return [t for t in all_tools
                if getattr(t, "_workflow_state", None) is None]

    def _system_with_reminder(self, system: str | None) -> str | None:
        """System prompt with mode-specific blocks attached.

        Native mode appends the workflow reminder (unless workflows are
        disabled). ReAct mode additionally appends the tool preamble that
        teaches the Thought/Action/Action Input format — without it the
        model has no way to know which tools exist, since they never reach
        the provider's tool API.
        """
        if self.enable_workflows:
            system = _with_workflow_reminder(system, self.tools)
        if self.mode == "react":
            preamble = render_react_preamble(self._react_tools())
            if preamble:
                system = (system or "") + preamble
        return system

    def _interpret(self, resp: LLMResponse) -> tuple[str, list[ToolCall], bool]:
        """Normalize a provider response into (assistant_text, tool_calls,
        is_terminal) so the rest of the loop is mode-agnostic.

        - native: tool calls come straight off `resp.tool_calls`; the turn
          is terminal when the model didn't ask for tools.
        - react: parse the reply text for a Thought/Action block. A parsed
          Action becomes a single-element tool_calls list; a Final Answer
          (or unparseable text) is terminal with the answer as the text.

        `assistant_text` is what we persist as the assistant turn's text
        block. In react mode that's the full reasoning trace (so the
        Thought/Action lines stay in history and the model sees its own
        prior format), except on a terminal turn where we store the clean
        Final Answer rather than the scaffolding.
        """
        if self.mode != "react":
            terminal = resp.stop_reason != "tool_use" or not resp.tool_calls
            return resp.text, resp.tool_calls, terminal

        step = parse_react_step(resp.text)
        if step.action is None:
            # Terminal: store the extracted Final Answer, not the raw
            # "Thought: ... Final Answer: ..." scaffolding.
            return step.final or resp.text, [], True
        # Non-terminal: keep the full trace (Thought + Action) in history.
        return resp.text, [step.action], False

    def _result_message(
        self,
        tool_calls: list[ToolCall],
        results: list[tuple[str, bool]],
    ) -> Message:
        """Pack tool outputs into the user-role message fed back to the model.

        Native mode uses structured `tool_result` blocks keyed by call id —
        the provider matches them to the original tool_use blocks. ReAct
        mode instead emits a plain-text `Observation:` block, because the
        model was prompted to expect that literal format in its transcript;
        feeding back structured blocks it never produced would break the
        format it's imitating. ReAct is one-action-per-turn, so there's a
        single observation.
        """
        if self.mode == "react":
            output, is_error = results[0]
            return Message(role="user", blocks=[{
                "type": "text",
                "text": format_observation(output, is_error),
            }])
        result_blocks = [
            {
                "type": "tool_result",
                "tool_call_id": tc.id,
                "name": tc.name,
                "content": output,
                "is_error": is_error,
            }
            for tc, (output, is_error) in zip(tool_calls, results)
        ]
        return Message(role="user", blocks=result_blocks)

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

                text, tool_calls, terminal = self._interpret(resp)

                # The model stopped issuing tool calls. If a workflow is
                # still mid-run, don't end the turn — auto-recall the
                # workflow tool to advance to the next step (slot
                # normalization happens inside that call). Only when no
                # workflow is active is an empty-tool turn truly terminal.
                if terminal and self.enable_workflows:
                    recall = _auto_recall_calls(self.tools)
                    if recall:
                        tool_calls = recall
                        terminal = False

                assistant_blocks: list[dict] = []
                if text:
                    assistant_blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    assistant_blocks.append({
                        "type": "tool_call",
                        "id": tc.id, "name": tc.name, "arguments": tc.arguments,
                        # Carried for providers that must replay it (Gemini).
                        "thought_signature": getattr(tc, "thought_signature", None),
                    })
                convo.messages.append(Message(role="assistant",
                                              blocks=assistant_blocks))

                if terminal:
                    return text, convo.session_id

                # Build tool-invocation context once per turn. The same
                # snapshot is handed to every tool call in this round.
                tool_context = {
                    "last_assistant_message": _last_assistant_text(convo.messages),
                    "last_user_message": _last_user_text(convo.messages),
                    "session_id": convo.session_id,
                }
                # Run all tool calls concurrently (react mode yields exactly
                # one, native may yield several).
                results = await asyncio.gather(*[
                    self.tools.run(tc.name, tc.arguments, tool_context)
                    for tc in tool_calls
                ])
                convo.messages.append(self._result_message(tool_calls, results))

                # If the model asked the user a question via human_feedback,
                # pause the loop: surface the question as the reply and hand
                # control back to the user. Their next message resumes.
                paused = _human_feedback_pause(tool_calls, results)
                if paused is not None:
                    return paused, convo.session_id

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

                    text, tool_calls, terminal = self._interpret(final_resp)

                    # Auto-advance an active workflow: if the model stopped
                    # issuing tool calls but a workflow is still mid-run,
                    # inject a recall of the workflow tool (slot
                    # normalization runs inside it) instead of ending.
                    if terminal and self.enable_workflows:
                        recall = _auto_recall_calls(self.tools)
                        if recall:
                            tool_calls = recall
                            terminal = False

                    # Persist the assistant turn.
                    assistant_blocks: list[dict] = []
                    if text:
                        assistant_blocks.append({"type": "text", "text": text})
                    for tc in tool_calls:
                        assistant_blocks.append({
                            "type": "tool_call",
                            "id": tc.id, "name": tc.name,
                            "arguments": tc.arguments,
                            # Carried for providers that must replay it (Gemini).
                            "thought_signature": getattr(
                                tc, "thought_signature", None),
                        })
                    convo.messages.append(Message(role="assistant",
                                                  blocks=assistant_blocks))

                    # Surface tool-call decisions before running them. In
                    # react mode these are parsed from the text the UI
                    # already streamed, so the event is what lets a UI show
                    # 'calling X' rather than re-rendering the raw Action.
                    for tc in tool_calls:
                        yield StreamEvent(type="tool_call", tool_call=tc,
                                          session_id=sid)

                    yield StreamEvent(type="turn_end", session_id=sid)

                    if terminal:
                        final_text = text
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
                             for tc in tool_calls]
                    results: list[tuple[ToolCall, str, bool]] = []
                    for coro in asyncio.as_completed(tasks):
                        tc, out, err = await coro
                        results.append((tc, out, err))
                        yield StreamEvent(type="tool_result",
                                          tool_call_id=tc.id, text=out,
                                          is_error=err, session_id=sid)

                    # Re-pair results to calls in original order, then hand
                    # to _result_message (structured blocks for native, a
                    # single Observation: text block for react).
                    by_id = {tc.id: (out, err) for tc, out, err in results}
                    ordered = [by_id[tc.id] for tc in tool_calls]
                    convo.messages.append(
                        self._result_message(tool_calls, ordered))

                    # human_feedback pauses the loop: surface its question
                    # as the final reply and hand control back to the user
                    # (their next message resumes the run).
                    paused = _human_feedback_pause(tool_calls, ordered)
                    if paused is not None:
                        final_text = paused
                        hit_step_limit = False
                        break

                if hit_step_limit:
                    final_text = "[agent stopped: hit max_steps]"

                yield StreamEvent(type="done", text=final_text, session_id=sid)

            except Exception as e:
                yield StreamEvent(type="error",
                                  text=f"{type(e).__name__}: {e}",
                                  session_id=sid)
