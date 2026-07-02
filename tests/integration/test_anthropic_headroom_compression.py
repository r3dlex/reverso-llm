"""Anthropic Messages Headroom dispatch tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, AsyncIterator, cast

import httpx
import pytest

import reverso.protocols.anthropic_app as anthropic_app
from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.protocols.headroom_compression import HeadroomCompressionOutcome

BASE_URL = "http://127.0.0.1:64946"
ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie", "codex", "claude"]


class SpyAdapter:
    """Adapter spy that records Anthropic-translated dispatch requests."""

    def __init__(self) -> None:
        self.create_requests: list[ResponsesRequest] = []
        self.stream_requests: list[ResponsesRequest] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.create_requests.append(request)
        return ResponseEnvelope(
            id="resp_anthropic_spy",
            model=request.model,
            output=[
                {
                    "id": "msg_anthropic_spy",
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
            usage={"input_tokens": 2, "output_tokens": 1},
        )

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        self.stream_requests.append(request)
        return self._stream()

    async def _stream(self) -> AsyncIterator[SSEEvent]:
        yield SSEEvent(
            event="response.output_item.added",
            data={
                "type": "response.output_item.added",
                "item": {
                    "id": "msg_stream",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                },
            },
        )
        yield SSEEvent(
            event="response.output_text.delta",
            data={"type": "response.output_text.delta", "delta": "ok"},
        )
        yield SSEEvent(
            event="response.output_item.done",
            data={"type": "response.output_item.done"},
        )
        yield SSEEvent(
            event="response.completed",
            data={
                "type": "response.completed",
                "response": {
                    "id": "resp_stream",
                    "status": "completed",
                    "output": [],
                    "usage": {"output_tokens": 1},
                },
            },
        )

    async def list_models(self) -> ModelList:
        return ModelList()

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="m")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


def _client(adapters: dict[str, SpyAdapter]) -> httpx.AsyncClient:
    app = build_anthropic_app(cast(Any, adapters))
    transport = httpx.ASGITransport(app=cast(Any, app))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


def _first_input_text(request: ResponsesRequest) -> str:
    item = request.input[0]
    return item["content"][0]["text"]


def _replace_first_input_text(request: ResponsesRequest, text: str) -> ResponsesRequest:
    input_items = cast(list[dict[str, Any]], request.input)
    copied = [dict(item) for item in input_items]
    first = dict(copied[0])
    content = [dict(part) for part in first["content"]]
    content[0]["text"] = text
    first["content"] = content
    copied[0] = first
    return replace(request, input=copied)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_messages_dispatches_compressed_request_for_each_backend(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = {name: SpyAdapter() for name in ANTHROPIC_BACKENDS}
    seen: list[str] = []

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        seen.append(_first_input_text(request))
        return HeadroomCompressionOutcome(
            request=_replace_first_input_text(request, f"compressed for {backend}"),
            compressed=True,
            reason="compressed",
        )

    monkeypatch.setattr(anthropic_app, "compress_responses_request", fake_compress)

    async with _client(adapters) as client:
        resp = await client.post(
            f"/{backend}/v1/messages",
            json={
                "model": "m",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "original text"}],
            },
        )

    assert resp.status_code == 200
    assert seen == ["original text"]
    assert (
        _first_input_text(adapters[backend].create_requests[0])
        == f"compressed for {backend}"
    )
    untouched = [
        name for name, adapter in adapters.items() if not adapter.create_requests
    ]
    assert sorted(untouched) == sorted(set(ANTHROPIC_BACKENDS) - {backend})


@pytest.mark.asyncio
async def test_messages_compresses_tool_result_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        input_items = cast(list[dict[str, Any]], request.input)
        copied = [dict(item) for item in input_items]
        copied[1] = dict(copied[1])
        assert copied[1]["type"] == "function_call_output"
        assert copied[1]["output"] == "raw tool output with many tokens"
        copied[1]["output"] = "compressed tool output"
        return HeadroomCompressionOutcome(
            request=replace(request, input=copied),
            compressed=True,
            reason="compressed",
        )

    monkeypatch.setattr(anthropic_app, "compress_responses_request", fake_compress)

    async with _client({"deepseek": adapter}) as client:
        resp = await client.post(
            "/deepseek/v1/messages",
            json={
                "model": "deepseek-v4-pro",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "lookup",
                                "input": {"query": "x"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": "raw tool output with many tokens",
                            }
                        ],
                    },
                ],
            },
        )

    assert resp.status_code == 200
    request = adapter.create_requests[0]
    assert request.input[1]["output"] == "compressed tool output"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_messages_streaming_dispatches_compressed_request_for_each_backend(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        return HeadroomCompressionOutcome(
            request=_replace_first_input_text(
                request, f"compressed stream for {backend}"
            ),
            compressed=True,
            reason="compressed",
        )

    monkeypatch.setattr(anthropic_app, "compress_responses_request", fake_compress)

    async with _client({backend: adapter}) as client:
        async with client.stream(
            "POST",
            f"/{backend}/v1/messages",
            json={
                "model": "m",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "original stream text"}],
            },
        ) as resp:
            assert resp.status_code == 200
            body = "".join([chunk async for chunk in resp.aiter_text()])

    assert "message_start" in body
    assert "content_block_delta" in body
    assert (
        _first_input_text(adapter.stream_requests[0])
        == f"compressed stream for {backend}"
    )


@pytest.mark.asyncio
async def test_messages_count_tokens_does_not_call_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()
    calls = 0

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        nonlocal calls
        calls += 1
        return HeadroomCompressionOutcome(request=request)

    monkeypatch.setattr(anthropic_app, "compress_responses_request", fake_compress)

    async with _client({"deepseek": adapter}) as client:
        resp = await client.post(
            "/deepseek/v1/messages/count_tokens",
            json={
                "model": "deepseek-v4-pro",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "count me"}],
            },
        )

    assert resp.status_code == 200
    assert "input_tokens" in resp.json()
    assert calls == 0
    assert adapter.create_requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
async def test_messages_feature_gate_runs_before_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SpyAdapter()
    calls = 0

    async def fake_compress(request: ResponsesRequest) -> HeadroomCompressionOutcome:
        nonlocal calls
        calls += 1
        return HeadroomCompressionOutcome(request=request)

    monkeypatch.setattr(anthropic_app, "compress_responses_request", fake_compress)

    async with _client({"deepseek": adapter}) as client:
        resp = await client.post(
            "/deepseek/v1/messages",
            json={
                "model": "deepseek-v4-pro",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "QQ==",
                                },
                            }
                        ],
                    }
                ],
            },
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"
    assert calls == 0
    assert adapter.create_requests == []
    assert adapter.stream_requests == []
