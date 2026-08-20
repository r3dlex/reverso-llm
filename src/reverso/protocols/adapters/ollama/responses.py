"""Ollama's OpenAI-compatible Responses HTTP client."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from reverso.protocols.adapter import ResponsesRequest


def request_body(request: ResponsesRequest, *, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "input": request.input,
        "stream": stream,
    }
    if request.previous_response_id is not None:
        body["previous_response_id"] = request.previous_response_id
    if request.instructions is not None:
        body["instructions"] = request.instructions
    if request.tools is not None:
        body["tools"] = request.tools
    if request.tool_choice is not None:
        body["tool_choice"] = request.tool_choice
    body.update(request.extra)
    return body


class OllamaResponsesClient:
    def __init__(self, client: httpx.AsyncClient, endpoint: str) -> None:
        self._client = client
        self._endpoint = endpoint

    async def create(self, request: ResponsesRequest) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._endpoint}/v1/responses", json=request_body(request, stream=False)
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("ollama response must be an object")
        return payload

    async def stream(self, request: ResponsesRequest) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream(
            "POST",
            f"{self._endpoint}/v1/responses",
            json=request_body(request, stream=True),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    yield payload
