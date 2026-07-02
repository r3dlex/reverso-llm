"""Responses surface Headroom dispatch tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, AsyncIterator, cast

import httpx
import pytest

import reverso.protocols.responses_app as responses_app
from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.headroom_compression import HeadroomCompressionOutcome
from reverso.protocols.responses_app import build_app

BASE_URL = "http://127.0.0.1:64946"


class SpyAdapter:
    """Adapter spy that records dispatch requests."""

    def __init__(self) -> None:
        self.create_requests: list[ResponsesRequest] = []
        self.stream_requests: list[ResponsesRequest] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.create_requests.append(request)
        return ResponseEnvelope(
            id="resp_spy",
            model=request.model,
            output=[
                {
                    "id": "msg_spy",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "ok",
                            "annotations": [],
                        }
                    ],
                }
            ],
        )

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        self.stream_requests.append(request)
        return self._stream()

    async def _stream(self) -> AsyncIterator[SSEEvent]:
        yield SSEEvent(
            event="response.created",
            data={
                "type": "response.created",
                "response": {"id": "resp_stream", "status": "in_progress"},
            },
        )
        yield SSEEvent(
            event="response.completed",
            data={
                "type": "response.completed",
                "response": {"id": "resp_stream", "status": "completed"},
            },
        )

    async def list_models(self) -> ModelList:
        return ModelList()

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="m")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(
            response_id=response_id,
            data=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "adapter compressed input"}
                    ],
                }
            ],
        )


def _client(adapter: SpyAdapter) -> httpx.AsyncClient:
    app = build_app({"deepseek": adapter, "claude": adapter})
    transport = httpx.ASGITransport(app=cast(Any, app))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


def _text_from_input_items(payload: dict[str, Any]) -> str:
    item = payload["data"][0]
    return "".join(
        part["text"] for part in item["content"] if part["type"] == "input_text"
    )


@pytest.mark.asyncio
async def test_responses_nonstreaming_dispatches_compressed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()
    seen_inputs: list[Any] = []

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        seen_inputs.append(request.input)
        return HeadroomCompressionOutcome(
            request=replace(request, input="compressed input"),
            compressed=True,
            reason="compressed",
        )

    monkeypatch.setattr(responses_app, "compress_responses_request", fake_compress)

    async with _client(adapter) as client:
        resp = await client.post(
            "/deepseek/v1/responses",
            json={"model": "m", "input": "original input"},
        )
        input_items = await client.get("/deepseek/v1/responses/resp_spy/input_items")

    assert resp.status_code == 200
    assert seen_inputs == ["original input"]
    assert adapter.create_requests[0].input == "compressed input"
    assert _text_from_input_items(input_items.json()) == "original input"


@pytest.mark.asyncio
async def test_responses_streaming_dispatches_compressed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        return HeadroomCompressionOutcome(
            request=replace(request, input="compressed stream input"),
            compressed=True,
            reason="compressed",
        )

    monkeypatch.setattr(responses_app, "compress_responses_request", fake_compress)

    async with _client(adapter) as client:
        async with client.stream(
            "POST",
            "/deepseek/v1/responses",
            json={"model": "m", "input": "original stream input", "stream": True},
        ) as resp:
            assert resp.status_code == 200
            body = "".join([chunk async for chunk in resp.aiter_text()])
        input_items = await client.get("/deepseek/v1/responses/resp_stream/input_items")

    assert "response.created" in body
    assert "response.completed" in body
    assert "data: [DONE]" in body
    assert adapter.stream_requests[0].input == "compressed stream input"
    assert _text_from_input_items(input_items.json()) == "original stream input"


@pytest.mark.asyncio
async def test_feature_gate_runs_before_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()
    calls = 0

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        nonlocal calls
        calls += 1
        return HeadroomCompressionOutcome(request=request)

    monkeypatch.setattr(responses_app, "compress_responses_request", fake_compress)

    async with _client(adapter) as client:
        resp = await client.post(
            "/claude/v1/responses",
            json={
                "model": "m",
                "input": "original input",
                "tools": [{"type": "file_search"}],
            },
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_feature"
    assert calls == 0
    assert adapter.create_requests == []
    assert adapter.stream_requests == []
