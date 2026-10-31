from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(slots=True)
class ToolCallRequest:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LlmResponse:
    text: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamChunk:
    text: str
    final: bool = False
    usage: dict[str, Any] = field(default_factory=dict)


class LlmProvider(ABC):
    provider_id: str
    provider_class: str

    @abstractmethod
    async def check_connectivity(self, timeout_sec: float) -> bool: ...

    @abstractmethod
    async def chat(self, messages: list[ChatMessage], tools: list[dict], timeout_sec: float) -> LlmResponse: ...

    async def list_models(self, timeout_sec: float = 10) -> list[dict[str, Any]]:
        return []

    async def stream(
        self, messages: list[ChatMessage], tools: list[dict], timeout_sec: float
    ) -> AsyncIterator[StreamChunk]:
        """Default: not a real stream — the whole answer as a single final chunk.

        OpenAI-compatible providers override this to yield incremental ``delta``
        chunks from the SSE endpoint. Stub/fake providers inherit this fallback,
        so existing subclasses stay valid without a ``stream`` implementation.
        """
        response = await self.chat(messages, tools, timeout_sec)
        if response.text:
            yield StreamChunk(text=response.text, final=True, usage=response.usage)
