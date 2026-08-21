"""Frozen-contract Responses adapter for Ollama."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_native import AnthropicNativeAdapter
from reverso.protocols.replay import record_input_items
from reverso.protocols.store import ResponseStore

from .catalog import OllamaCatalog
from .messages import OllamaMessagesClient
from .responses import OllamaResponsesClient


def _envelope(raw: dict[str, Any], request: ResponsesRequest) -> ResponseEnvelope:
    response_id = raw.get("id")
    output = raw.get("output", [])
    if not isinstance(response_id, str) or not response_id:
        raise ValueError("ollama response id is required")
    if not isinstance(output, list):
        raise TypeError("ollama response output must be a list")
    return ResponseEnvelope(
        id=response_id,
        model=str(raw.get("model") or request.model),
        output=output,
        status=str(raw.get("status") or "completed"),
        usage=raw.get("usage") if isinstance(raw.get("usage"), dict) else None,
        previous_response_id=request.previous_response_id,
        raw=raw,
    )


class OllamaAdapter(AnthropicNativeAdapter):
    def __init__(
        self,
        catalog: OllamaCatalog,
        responses_client: OllamaResponsesClient,
        store: ResponseStore,
        messages_client: OllamaMessagesClient | None = None,
    ) -> None:
        self._catalog = catalog
        self._responses = responses_client
        self._messages = messages_client
        self._store = store

    async def create_anthropic_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._messages is None:
            raise RuntimeError("Ollama Messages client is unavailable")
        return await self._messages.create(payload)

    def stream_anthropic_message(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        if self._messages is None:
            raise RuntimeError("Ollama Messages client is unavailable")
        return self._messages.stream(payload)

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        envelope = _envelope(await self._responses.create(request), request)
        self._store.put_response(envelope, record_input_items(request))
        return envelope

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        completed: ResponseEnvelope | None = None
        async for data in self._responses.stream(request):
            event_name = data.get("type")
            if not isinstance(event_name, str) or not event_name:
                raise ValueError("ollama SSE event type is required")
            event = SSEEvent(event=event_name, data=data)
            if event_name == "response.completed":
                raw = data.get("response")
                if isinstance(raw, dict):
                    completed = _envelope(raw, request)
                    self._store.put_response(
                        completed,
                        record_input_items(request),
                    )
            yield event

    async def list_models(self) -> ModelList:
        entries = await self._catalog.refresh()
        return ModelList(
            data=[
                {"id": entry.raw_id, "object": "model", "owned_by": "ollama"}
                for entry in entries
            ],
            discovery_source=f"live-local-cloud-{self._catalog.cloud_status}",
        )

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        response = self._store.get_response(response_id)
        if response is None:
            raise ValueError(f"unknown response {response_id}")
        return response

    async def list_input_items(self, response_id: str) -> InputItemList:
        items = self._store.get_input_items(response_id)
        if items is None:
            raise ValueError(f"unknown response {response_id}")
        return items
