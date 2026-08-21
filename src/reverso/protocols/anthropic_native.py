"""Internal optional native Anthropic Messages adapter facet."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AnthropicNativeAdapter(Protocol):
    async def create_anthropic_message(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def stream_anthropic_message(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]: ...
