"""LLMProvider abstract base class.

Every concrete provider must implement `complete()` (single non-streaming
call) and `stream()` (async generator that yields text deltas and exactly
one `('final', LLMResponse)` at the end).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from botcircuits.agent.mcp import MCPServer
from botcircuits.agent.skill import SkillSpec
from botcircuits.agent.tools import LocalTool
from botcircuits.types import LLMResponse, Message, ProviderStreamEvent


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[LocalTool],
        hosted_mcp: list[MCPServer],
        skills: list[SkillSpec],
        max_tokens: int,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[LocalTool],
        hosted_mcp: list[MCPServer],
        skills: list[SkillSpec],
        max_tokens: int,
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Yields provider-stream events. Must yield exactly one
        ('final', LLMResponse) at the end."""
        ...
        yield  # pragma: no cover  (marks this as an async generator)

    def supports_hosted_mcp(self) -> bool:
        return False

    async def aclose(self) -> None:
        """Override if the provider holds an async client to clean up."""
        return None
