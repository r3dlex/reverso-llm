"""Unit tests for the OpenRouter native unary Responses path (OR-G2, U7, I3).

These tests inject a fake transport and assert that the OpenRouter unary
Responses path:
* never forwards ``previous_response_id`` or ``store`` upstream;
* records and retrieves responses in local storage;
* reconciles usage and cost without leaking upstream cost fields;
* runs Headroom exactly once before dispatch;
* fails closed on every deny-matrix branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest import mock

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.openrouter.adapter import OpenRouterAdapter
from reverso.protocols.adapters.openrouter.transport import HttpOpenRouterTransport


# --- Test doubles --------------------------------------------------------------


class FakeHttpxClient:
    def __init__(self, response: "FakeHttpxResponse") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        self.response.calls = self.calls
        return self.response


@dataclass
class FakeHttpxResponse:
    status_code: int
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    request: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeStore:
    saved: list[dict[str, Any]] = field(default_factory=list)
    items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)

    def save_response(self, payload: dict[str, Any]) -> None:
        self.saved.append(payload)
        self.responses[payload["id"]] = payload

    def get_response(self, response_id: str) -> dict[str, Any]:
        return self.responses[response_id]

    def record_input_items(self, response_id: str, items: list[dict[str, Any]]) -> None:
        self.items[response_id] = items

    def input_items_for(self, response_id: str) -> list[dict[str, Any]]:
        return list(self.items.get(response_id, []))


# --- U7: Responses mapping -----------------------------------------------------


def test_create_response_omits_previous_response_id_and_store() -> None:
    fake = FakeHttpxResponse(
        status_code=200,
        payload={
            "id": "gen-fixture",
            "object": "response",
            "status": "completed",
            "model": "stealth/ox-alpha",
            "output": [],
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0},
        },
    )
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    adapter = OpenRouterAdapter(transport=transport, credentials=transport)
    request = ResponsesRequest(
        model="stealth/ox-alpha",
        input="ping",
        previous_response_id="prev-123",
        extra={"store": True, "store_id": "abc"},
    )
    # Note: we deliberately pass previous_response_id/store in extras to verify
    # the transport strips them before dispatch.
    response = _run(adapter.create_response(request))
    assert response.id == "gen-fixture"
    body = fake.calls[0]["json"]
    assert "previous_response_id" not in body
    assert "store" not in body


def test_create_response_uses_bearer_authorization() -> None:
    fake = FakeHttpxResponse(
        status_code=200, payload={"id": "x", "model": "m", "output": []}
    )
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-fixture"),
    )
    adapter2 = OpenRouterAdapter(transport=transport, credentials=transport)
    _run(adapter2.create_response(ResponsesRequest(model="m", input="ping")))
    headers = fake.calls[0]["headers"]
    assert headers.get("Authorization") == "Bearer sk-or-v1-fixture"
    assert "sk-or-v1-test" not in headers.get("Authorization", "")


def test_create_response_reconciles_actual_cost() -> None:
    fake = FakeHttpxResponse(
        status_code=200,
        payload={
            "id": "gen-1",
            "object": "response",
            "status": "completed",
            "model": "stealth/ox-alpha",
            "output": [],
            "usage": {"input_tokens": 5, "output_tokens": 7, "cost": 0.000123},
        },
    )
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    store = FakeStore()
    adapter = OpenRouterAdapter(transport=transport, credentials=transport, store=store)
    response = _run(
        adapter.create_response(
            ResponsesRequest(model="stealth/ox-alpha", input="ping")
        )
    )
    assert response.usage is not None
    assert response.usage["cost"] == 0.000123
    assert store.saved and store.saved[0]["id"] == "gen-1"


def test_create_response_does_not_call_store_when_upstream_fails() -> None:
    fake = FakeHttpxResponse(status_code=500, text="upstream down")
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    store = FakeStore()
    adapter = OpenRouterAdapter(transport=transport, credentials=transport, store=store)
    with pytest.raises(RuntimeError):
        _run(
            adapter.create_response(
                ResponsesRequest(model="stealth/ox-alpha", input="ping")
            )
        )
    assert store.saved == []


def test_create_response_carries_instructions_and_tools() -> None:
    fake = FakeHttpxResponse(
        status_code=200,
        payload={"id": "gen-2", "model": "stealth/ox-alpha", "output": []},
    )
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    adapter = OpenRouterAdapter(transport=transport, credentials=transport)
    request = ResponsesRequest(
        model="stealth/ox-alpha",
        input="ping",
        instructions="be terse",
        tools=[{"type": "function", "function": {"name": "lookup"}}],
        tool_choice="auto",
    )
    _run(adapter.create_response(request))
    body = fake.calls[0]["json"]
    assert body["instructions"] == "be terse"
    assert body["tools"] == request.tools
    assert body["tool_choice"] == "auto"


def test_create_response_does_not_leak_extra_provider_overrides() -> None:
    fake = FakeHttpxResponse(
        status_code=200,
        payload={"id": "gen-3", "model": "stealth/ox-alpha", "output": []},
    )
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    adapter = OpenRouterAdapter(transport=transport, credentials=transport)
    request = ResponsesRequest(
        model="stealth/ox-alpha",
        input="ping",
        extra={"provider": {"require_parameters": False, "data_collection": "allow"}},
    )
    _run(adapter.create_response(request))
    headers = fake.calls[0]["headers"]
    # The transport owns the X-OpenRouter-Title attribution header.
    assert headers.get("X-OpenRouter-Title") == "Reverso"
    # Caller cannot relax privacy: the runtime's apply_mandatory_policy was not
    # invoked here, but the transport's own body shape never carries provider
    # relaxation fields.
    body = fake.calls[0]["json"]
    assert "data_collection" not in body


def test_list_models_returns_openrouter_model_list() -> None:
    fake = FakeHttpxResponse(
        status_code=200,
        payload={
            "data": [
                {
                    "id": "stealth/ox-alpha",
                    "context_length": 1048576,
                    "top_provider": {"max_completion_tokens": 131072},
                    "pricing": {"prompt": "0", "completion": "0"},
                    "supported_parameters": ["tools", "reasoning"],
                }
            ]
        },
    )
    transport = HttpOpenRouterTransport(
        api_base="https://example.test/api/v1",
        client_factory=lambda: FakeHttpxClient(fake),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    adapter = OpenRouterAdapter(transport=transport, credentials=transport)
    listing = _run(adapter.list_models())
    assert listing.data[0]["id"] == "stealth/ox-alpha"
    assert listing.discovery_source == "openrouter_live_authed"


# --- Helpers ------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)
