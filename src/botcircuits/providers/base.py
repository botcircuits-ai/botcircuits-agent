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

#: Greedy decoding for reproducibility. botcircuits' core claim is run-to-run
#: predictability, so we pin temperature to 0 on every provider.
DEFAULT_TEMPERATURE: float = 0.0

#: Fixed sampling seed for providers that accept one (currently only Gemini
DEFAULT_SEED: int = 0


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
