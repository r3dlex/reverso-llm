"""Ollama's Anthropic-compatible Messages HTTP client."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


def _validate_message(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("ollama message must be an object")
    if not isinstance(payload.get("type"), str):
        raise ValueError("ollama message type is required")
    return payload


class OllamaMessagesClient:
    def __init__(self, client: httpx.AsyncClient, endpoint: str) -> None:
        self._client = client
        self._endpoint = endpoint

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._endpoint}/v1/messages",
            json={**payload, "stream": False} if payload.get("stream") else payload,
        )
        response.raise_for_status()
        return _validate_message(response.json())

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        body = {**payload, "stream": True}
        async with self._client.stream(
            "POST", f"{self._endpoint}/v1/messages", json=body
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                event = json.loads(raw)
                if not isinstance(event, dict) or not isinstance(
                    event.get("type"), str
                ):
                    raise TypeError(
                        "ollama message stream event must be an object with type"
                    )
                yield event
