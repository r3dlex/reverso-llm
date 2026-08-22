"""OpenRouter ``ProviderAdapter`` (OR-G1, U7, U8, I1, I3, I4).

The adapter implements the frozen ``ProviderAdapter`` Protocol and forwards
Responses requests to ``POST /api/v1/responses``. Streaming is delegated to
``OpenRouterResponsesTransport`` which yields SSE events verbatim.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)

from .catalog import entry_from_model_payload

__all__ = [
    "OpenRouterAdapter",
    "OpenRouterError",
    "OpenRouterResponsesTransport",
]


logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    """An OpenRouter upstream call failed. Never carries the credential."""


class OpenRouterResponsesTransport(Protocol):
    """A minimal httpx transport surface for the OpenRouter adapter."""

    async def list_models(self) -> tuple[int, dict[str, Any]]: ...

    async def create_response(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]: ...

    def stream_response(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[SSEEvent]: ...


class OpenRouterAdapter:
    """ProviderAdapter that proxies Responses traffic to OpenRouter."""

    def __init__(
        self,
        *,
        transport: OpenRouterResponsesTransport | Any,
        credentials: Any,
    ) -> None:
        self._transport = transport
        self._credentials = credentials

    async def list_models(self) -> ModelList:
        status, payload = await self._transport.list_models()
        if status != 200 or not isinstance(payload, dict):
            raise OpenRouterError(f"GET /api/v1/models returned {status}")
        raw_entries = payload.get("data") or []
        entries = [
            entry_from_model_payload(entry)
            for entry in raw_entries
            if isinstance(entry, dict)
        ]
        return ModelList(
            data=[
                {
                    "id": entry.id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "openrouter",
                    "context_length": entry.context_length,
                }
                for entry in entries
            ],
            object="list",
            models=[
                {
                    "id": entry.id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "openrouter",
                    "context_length": entry.context_length,
                }
                for entry in entries
            ],
            discovery_source="openrouter_live_authed",
        )

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        payload = self._translate(request)
        status, body = await self._transport.create_response(payload)
        if status != 200:
            raise OpenRouterError(f"POST /api/v1/responses returned {status}")
        return _to_envelope(body)

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:  # type: ignore[override]
        payload = self._translate(request)
        async for event in self._transport.stream_response(payload):
            yield event

    async def get_response(self, response_id: str) -> ResponseEnvelope:  # type: ignore[override]
        raise OpenRouterError(
            "OpenRouter Responses is stateless; previous_response_id is resolved locally."
        )

    async def list_input_items(self, response_id: str) -> InputItemList:  # type: ignore[override]
        raise OpenRouterError(
            "OpenRouter Responses is stateless; input_items are read from local storage."
        )

    def _translate(self, request: ResponsesRequest) -> dict[str, Any]:
        """Translate a ResponsesRequest into the OpenRouter request body.

        ``provider``, ``X-OpenRouter-Title`` and ``HTTP-Referer`` are merged into
        ``extra`` by the runtime; this method carries them through verbatim and
        never overrides caller-set routing fields.
        """
        body: dict[str, Any] = {
            "model": request.model,
            "input": request.input,
            "stream": bool(request.stream),
        }
        if request.instructions is not None:
            body["instructions"] = request.instructions
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        for key, value in request.extra.items():
            if key in body:
                continue
            body[key] = value
        return body


def _to_envelope(body: dict[str, Any]) -> ResponseEnvelope:
    """Wrap an OpenRouter Responses object into the reverso envelope."""
    response_id = str(body.get("id") or "")
    model = str(body.get("model") or "")
    output = list(body.get("output") or [])
    status = str(body.get("status") or "completed")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    previous = body.get("previous_response_id")
    return ResponseEnvelope(
        id=response_id,
        model=model,
        output=output,
        status=status,
        usage=usage,
        previous_response_id=previous,
        raw=body,
    )
