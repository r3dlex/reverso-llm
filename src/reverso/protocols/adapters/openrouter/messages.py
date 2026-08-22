"""OpenRouter native Anthropic Messages adapter (OR-G4, U8, I4).

Maps reverso's ``AnthropicMessagesRequest`` shape onto OpenRouter's native
``POST /api/v1/messages`` endpoint and normalizes the response back into the
Anthropic Messages envelope. The adapter never forwards Responses-shaped
fields; Headroom runs once before dispatch and never touches the Messages
client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

__all__ = [
    "OpenRouterMessagesAdapter",
    "OpenRouterMessagesError",
    "build_messages_payload",
    "normalize_messages_response",
]


class OpenRouterMessagesError(RuntimeError):
    """An OpenRouter Messages call failed. Never carries the credential."""


class OpenRouterMessagesTransport(Protocol):
    """A minimal transport surface for the OpenRouter Messages adapter."""

    async def create_message(
        self, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]: ...


def build_messages_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an AnthropicMessagesRequest into the OpenRouter request body."""
    body: dict[str, Any] = {
        "model": request.get("model"),
        "max_tokens": request.get("max_tokens"),
        "messages": list(request.get("messages") or []),
    }
    if "system" in request and request["system"] is not None:
        body["system"] = request["system"]
    if "tools" in request and request["tools"] is not None:
        body["tools"] = request["tools"]
    if "tool_choice" in request and request["tool_choice"] is not None:
        body["tool_choice"] = request["tool_choice"]
    if "thinking" in request and request["thinking"] is not None:
        body["thinking"] = request["thinking"]
    if "metadata" in request and request["metadata"] is not None:
        body["metadata"] = request["metadata"]
    if "stop_sequences" in request and request["stop_sequences"] is not None:
        body["stop_sequences"] = request["stop_sequences"]
    if "temperature" in request and request["temperature"] is not None:
        body["temperature"] = request["temperature"]
    if "top_p" in request and request["top_p"] is not None:
        body["top_p"] = request["top_p"]
    if "top_k" in request and request["top_k"] is not None:
        body["top_k"] = request["top_k"]
    return body


def normalize_messages_response(body: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an OpenRouter Messages body into the Anthropic Messages envelope."""
    usage_raw = body.get("usage") or {}
    usage: dict[str, Any] = dict(usage_raw) if isinstance(usage_raw, dict) else {}
    return {
        "id": str(body.get("id") or ""),
        "model": str(body.get("model") or ""),
        "type": "message",
        "role": "assistant",
        "stop_reason": body.get("stop_reason"),
        "content": list(body.get("content") or []),
        "usage": usage,
    }


class OpenRouterMessagesAdapter:
    """Adapter for the native OpenRouter Anthropic Messages endpoint."""

    def __init__(
        self,
        *,
        transport: OpenRouterMessagesTransport,
        credentials: Any,
    ) -> None:
        self._transport = transport
        self._credentials = credentials

    async def create_message(self, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = build_messages_payload(request)
        api_key = self._credentials.resolve_api_key()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-OpenRouter-Title": "Reverso",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        status, body = await self._transport.create_message(payload, headers=headers)
        if status != 200 or not isinstance(body, dict):
            raise OpenRouterMessagesError(f"POST /api/v1/messages returned {status}")
        return normalize_messages_response(body)
