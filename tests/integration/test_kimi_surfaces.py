from __future__ import annotations

import json
from typing import Any, AsyncIterator, cast

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.adapters.kimi import KimiAdapter
from reverso.protocols.anthropic_app import (
    build_anthropic_adapters,
    build_anthropic_app,
)
from reverso.protocols.responses_app import build_app, split_provider_path
from reverso.proxy.compose import build_adapters

BASE_URL = "http://127.0.0.1:64946"


class _StaticAuth:
    async def resolve_bearer_token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


class _AnthropicSpy:
    def __init__(self) -> None:
        self.requests: list[ResponsesRequest] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.requests.append(request)
        return ResponseEnvelope(
            id="resp_kimi_messages",
            model=request.model,
            output=[
                {
                    "id": "msg_kimi_messages",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "ok", "annotations": []}
                    ],
                }
            ],
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        raise AssertionError("streaming is covered by the Headroom matrix")

    async def list_models(self) -> ModelList:
        return ModelList()

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="kimi-k2.5")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


def _asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast(Any, app)),
        base_url=BASE_URL,
    )


@pytest.mark.asyncio
async def test_kimi_responses_surface_stores_and_chains_turns() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "kimi-k2.5",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    adapter = KimiAdapter(
        auth=cast(Any, _StaticAuth()),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    app = build_app({"kimi": adapter})

    async with _asgi_client(app) as client:
        first = await client.post(
            "/kimi/v1/responses",
            json={"model": "kimi-k2.5", "input": "first"},
        )
        response_id = first.json()["id"]
        stored = await client.get(f"/kimi/v1/responses/{response_id}")
        input_items = await client.get(f"/kimi/v1/responses/{response_id}/input_items")
        second = await client.post(
            "/kimi/v1/responses",
            json={
                "model": "kimi-k2.5",
                "input": "second",
                "previous_response_id": response_id,
            },
        )

    assert first.status_code == stored.status_code == input_items.status_code == 200
    assert stored.json()["id"] == response_id
    assert input_items.json()["data"][0]["content"][0]["text"] == "first"
    assert second.status_code == 200
    assert second.json()["previous_response_id"] == response_id
    assert bodies[1]["messages"] == [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("/kimi/v1/messages", "kimi-k2.5"),
        ("/v1/messages", "kimi/kimi-k2.5"),
        ("/v1/messages", "anthropic-kimi-kimi-k2.5"),
    ],
)
async def test_kimi_messages_routes_and_strips_routing_alias(
    path: str, model: str
) -> None:
    adapter = _AnthropicSpy()
    app = build_anthropic_app(cast(Any, {"kimi": adapter}))

    async with _asgi_client(app) as client:
        response = await client.post(
            path,
            json={
                "model": model,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert adapter.requests[0].model == "kimi-k2.5"


def test_kimi_prefix_is_owned_by_first_party_responses_app() -> None:
    route = split_provider_path("/kimi/v1/responses")

    assert route is not None
    assert route.provider == "kimi"
    assert route.path == "/v1/responses"


def test_real_composition_factories_mount_kimi_adapter() -> None:
    responses = build_adapters({"REVERSO_CODEX_DIRECT_BACKEND": "0"})
    anthropic = build_anthropic_adapters()

    assert isinstance(responses["kimi"], KimiAdapter)
    assert isinstance(anthropic["kimi"], KimiAdapter)
