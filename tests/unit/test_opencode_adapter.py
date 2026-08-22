"""OCG-G4: the OpenCode Go Codex/Responses vertical.

All HTTP traffic is FAKE via httpx.MockTransport; no real network call is made.
The credential is a unique sentinel so non-leakage can be asserted positively
rather than assumed.

The Codex vertical is deliberately first. The adapter contract is
Responses-shaped, so a chat-completions transport exercises no protocol
round-trip: a defect here is an adapter defect, not a translation defect. That
separation is the whole point of the slice order.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from reverso.protocols.adapter import (
    ModelList,
    ProviderAdapter,
    ResponsesRequest,
)
from reverso.protocols.adapters.opencode.adapter import (
    OpenCodeAdapter,
    OpenCodeError,
    OpenCodeQuotaError,
)
from reverso.protocols.adapters.opencode.catalog import (
    FALLBACK_MODEL_IDS,
    OPENCODE_GO_API_BASE,
    USER_AGENT,
)
from reverso.protocols.adapters.opencode.credentials import OPENCODE_API_KEY_ENV

API_KEY_SENTINEL = "sk-OPENCODEsentinelKEY-do-not-leak-4b3a2c1d"


def _mock_client(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


def _chat_response(text: str = "hello", **extra: Any) -> dict:
    message = {"role": "assistant", "content": text}
    message.update(extra)
    return {
        "id": "chatcmpl-fake",
        "model": "glm-5",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _adapter(handler) -> OpenCodeAdapter:
    return OpenCodeAdapter(client_factory=_mock_client(handler))


@pytest.fixture(autouse=True)
def _key(monkeypatch) -> None:
    monkeypatch.setenv(OPENCODE_API_KEY_ENV, API_KEY_SENTINEL)


def test_adapter_satisfies_the_frozen_protocol() -> None:
    assert isinstance(_adapter(lambda r: httpx.Response(200)), ProviderAdapter)


@pytest.mark.asyncio
async def test_create_response_maps_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json=_chat_response("hi there"))

    envelope = await _adapter(handler).create_response(
        ResponsesRequest(model="glm-5", input="hello")
    )
    assert envelope.status == "completed"
    assert "hi there" in json.dumps(envelope.output)


@pytest.mark.asyncio
async def test_request_carries_bearer_and_user_agent() -> None:
    """The bearer form is required on chat-completions, and a UA is mandatory:
    the edge rejects a default client fingerprint with Cloudflare error 1010."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=_chat_response())

    await _adapter(handler).create_response(ResponsesRequest(model="glm-5", input="x"))
    assert seen["authorization"] == f"Bearer {API_KEY_SENTINEL}"
    assert seen["user-agent"] == USER_AGENT


@pytest.mark.asyncio
async def test_targets_the_zen_go_base() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_chat_response())

    await _adapter(handler).create_response(ResponsesRequest(model="glm-5", input="x"))
    assert seen["url"] == f"{OPENCODE_GO_API_BASE}/chat/completions"


@pytest.mark.asyncio
async def test_missing_credential_raises_before_any_request() -> None:
    """Fail closed: never issue an unauthenticated upstream request."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json=_chat_response())

    adapter = OpenCodeAdapter(client_factory=_mock_client(handler), env={})
    with pytest.raises(OpenCodeError):
        await adapter.create_response(ResponsesRequest(model="glm-5", input="x"))
    assert called is False


@pytest.mark.asyncio
async def test_429_surfaces_as_a_quota_error_with_no_fallback() -> None:
    """A quota refusal must reach the client, never silently reroute."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(OpenCodeQuotaError) as excinfo:
        await _adapter(handler).create_response(
            ResponsesRequest(model="glm-5", input="x")
        )
    # One attempt only: no retry against another credential or backend.
    assert attempts == 1
    assert excinfo.value.status_code == 429
    assert isinstance(excinfo.value.payload, dict)


@pytest.mark.asyncio
async def test_quota_error_is_an_opencode_error() -> None:
    """Callers catching the provider's base error must still catch quota."""
    assert issubclass(OpenCodeQuotaError, OpenCodeError)


@pytest.mark.asyncio
async def test_upstream_error_never_leaks_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(OpenCodeError) as excinfo:
        await _adapter(handler).create_response(
            ResponsesRequest(model="glm-5", input="x")
        )
    assert API_KEY_SENTINEL not in str(excinfo.value)


@pytest.mark.asyncio
async def test_list_models_serves_the_live_catalog_with_limits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "glm-5"}, {"id": "kimi-k3"}]},
        )

    listing = await _adapter(handler).list_models()
    assert isinstance(listing, ModelList)
    by_id = {row["id"]: row for row in listing.data}
    assert set(by_id) == {"glm-5", "kimi-k3"}
    # Codex sizes context management from these, so they must be present.
    assert by_id["glm-5"]["context_window"] == 202752
    assert by_id["kimi-k3"]["context_window"] == 1048576


@pytest.mark.asyncio
async def test_list_models_omits_limits_it_does_not_know() -> None:
    """A guessed window makes the client compact at the wrong point."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "not-a-real-model"}]})

    listing = await _adapter(handler).list_models()
    assert "context_window" not in listing.data[0]


@pytest.mark.asyncio
async def test_list_models_falls_back_to_the_bounded_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await _adapter(handler).list_models()
    assert {row["id"] for row in listing.data} == set(FALLBACK_MODEL_IDS)
    assert listing.discovery_source == "fallback"


@pytest.mark.asyncio
async def test_live_listing_is_marked_live() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "glm-5"}]})

    assert (await _adapter(handler).list_models()).discovery_source == "live"


@pytest.mark.asyncio
async def test_model_listing_needs_no_credential() -> None:
    """GET /models is public upstream; a listing must not 503 without a key."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "glm-5"}]})

    adapter = OpenCodeAdapter(client_factory=_mock_client(handler), env={})
    assert (await adapter.list_models()).data


@pytest.mark.asyncio
async def test_stream_response_yields_a_completed_sequence() -> None:
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = (
        b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        + b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    events = [
        event
        async for event in _adapter(handler).stream_response(
            ResponsesRequest(model="glm-5", input="x", stream=True)
        )
    ]
    names = [event.event for event in events]
    assert names[-1] == "response.completed"
    assert any("delta" in name for name in names)


@pytest.mark.asyncio
async def test_streamed_429_also_raises_quota_not_a_generic_error() -> None:
    """The streaming path is the one Codex actually uses.

    The inherited chat-completions transport raises its OWN provider error on a
    non-2xx, which would surface a quota refusal as a generic 502 attributed to
    the wrong provider and drop the 429 entirely.
    """
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(OpenCodeQuotaError) as excinfo:
        async for _ in _adapter(handler).stream_response(
            ResponsesRequest(model="glm-5", input="x", stream=True)
        ):
            pass
    assert attempts == 1
    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_streamed_non_2xx_is_an_opencode_error_not_a_deepseek_one() -> None:
    """Inheriting the transport must not mean inheriting the provider identity."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(OpenCodeError) as excinfo:
        async for _ in _adapter(handler).stream_response(
            ResponsesRequest(model="glm-5", input="x", stream=True)
        ):
            pass
    assert "deepseek" not in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_stream_sends_bearer_and_user_agent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async for _ in _adapter(handler).stream_response(
        ResponsesRequest(model="glm-5", input="x", stream=True)
    ):
        pass
    assert seen["authorization"] == f"Bearer {API_KEY_SENTINEL}"
    assert seen["user-agent"] == USER_AGENT
