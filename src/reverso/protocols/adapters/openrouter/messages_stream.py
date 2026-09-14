"""OpenRouter native Anthropic Messages streaming (OR-G5, U8, I5).

Maps reverso's Anthropic Messages shape onto OpenRouter's native streaming
endpoint ``POST /api/v1/messages`` and normalizes each SSE event payload.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

__all__ = [
    "OpenRouterMessagesStreamError",
    "build_stream_payload",
    "normalize_stream_event",
]


class OpenRouterMessagesStreamError(RuntimeError):
    """A streaming Messages call failed. Never carries the credential."""


def build_stream_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the upstream body with ``stream: True`` set."""
    body = dict(request)
    body["stream"] = True
    return body


def normalize_stream_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Pass-through normalizer: the OpenRouter SSE event already uses the
    Anthropic ``type`` field, so we only validate the shape."""
    if not isinstance(payload, Mapping):
        raise TypeError(f"stream event must be a mapping, got {type(payload)!r}")
    return dict(payload)


class OpenRouterMessagesStreamingClient:
    """Streaming client for the OpenRouter Anthropic Messages endpoint."""

    def __init__(self, *, transport: Any, credentials: Any) -> None:
        self._transport = transport
        self._credentials = credentials

    async def stream_message(
        self, request: Mapping[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        payload = build_stream_payload(request)
        api_key = self._credentials.resolve_api_key()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-OpenRouter-Title": "Reverso",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async for raw_event in self._transport.stream_message(payload, headers=headers):
            event_type = str(raw_event.get("type") or "")
            if event_type == "ping":
                continue
            yield normalize_stream_event(raw_event)
