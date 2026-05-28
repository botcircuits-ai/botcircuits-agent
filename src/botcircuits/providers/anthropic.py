"""Anthropic Claude provider."""

from __future__ import annotations

from typing import Any

from botcircuits.types import LLMResponse, Message, ToolCall
from botcircuits.providers.base import LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-7", api_key: str | None = None):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self.model = model

    def supports_hosted_mcp(self) -> bool:
        return True

    async def aclose(self) -> None:
        await self.client.close()

    def _msg_to_api(self, m: Message) -> dict:
        content: list[dict] = []
        for b in m.blocks:
            t = b["type"]
            if t == "text":
                content.append({"type": "text", "text": b["text"]})
            elif t == "tool_call":
                content.append({"type": "tool_use", "id": b["id"],
                                "name": b["name"], "input": b["arguments"]})
            elif t == "tool_result":
                content.append({"type": "tool_result",
                                "tool_use_id": b["tool_call_id"],
                                "content": b["content"],
                                "is_error": b.get("is_error", False)})
        return {"role": m.role, "content": content}

    def _build_kwargs(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        """Shared between complete() and stream() so they use identical config."""
        api_tools = [{"name": t.name, "description": t.description,
                      "input_schema": t.input_schema} for t in tools]
        betas: list[str] = []
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "system": system,
            "messages": [self._msg_to_api(m) for m in messages],
        }
        if hosted_mcp:
            betas.append("mcp-client-2025-11-20")
            kwargs["mcp_servers"] = [
                {"type": "url", "name": s.name, "url": s.url,
                 **({"authorization_token": s.authorization_token}
                    if s.authorization_token else {})}
                for s in hosted_mcp
            ]
        if skills:
            betas += ["code-execution-2025-08-25", "skills-2025-10-02",
                      "files-api-2025-04-14"]
            api_tools.append({"type": "code_execution_20250825",
                              "name": "code_execution"})
            named = [s for s in skills if s.skill_id]
            if named:
                kwargs["container"] = {"skills": [
                    {"type": "anthropic", "skill_id": s.skill_id,
                     "version": s.version}
                    for s in named
                ]}
        if api_tools:
            kwargs["tools"] = api_tools
        if betas:
            kwargs["betas"] = betas
        return kwargs, betas

    def _normalize(self, resp, tools) -> LLMResponse:
        text_parts, tool_calls = [], []
        local_names = {t.name for t in tools}
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name in local_names:
                # MCP and code_execution tool_use blocks are skipped — they
                # already executed server-side and their results are inline.
                tool_calls.append(ToolCall(id=block.id, name=block.name,
                                            arguments=block.input or {}))
        stop_map = {"end_turn": "end_turn", "tool_use": "tool_use",
                    "max_tokens": "max_tokens"}
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            stop_reason=stop_map.get(resp.stop_reason, "other"),
            raw=resp,
        )

    async def complete(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        kwargs, betas = self._build_kwargs(system, messages, tools, hosted_mcp,
                                            skills, max_tokens)
        api = self.client.beta.messages if betas else self.client.messages
        resp = await api.create(**kwargs)
        return self._normalize(resp, tools)

    async def stream(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        kwargs, betas = self._build_kwargs(system, messages, tools, hosted_mcp,
                                            skills, max_tokens)
        api = self.client.beta.messages if betas else self.client.messages
        async with api.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "text" and getattr(event, "text", None):
                    yield ("text_delta", event.text)
            final = await stream.get_final_message()
        yield ("final", self._normalize(final, tools))
