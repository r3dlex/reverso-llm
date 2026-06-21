"""Provider-independent Anthropic Messages parity harness (ADR 0006, G007).

The Anthropic Messages surface is exercised over the SAME deterministic
FixtureAdapter seam the Responses parity suite uses (tests/integration/conftest),
across the THREE Anthropic-surface backends (copilot, deepseek, auggie). claude is
intentionally ABSENT from this matrix: it is excluded from the Anthropic surface
(ADR 0006 D2) and its exclusion is asserted as a separate negative conformance
suite (test_anthropic_claude_exclusion). No real Copilot, DeepSeek, or Auggie
endpoint, process, or credential is touched; FixtureAdapter replays fixture
bodies/events and authenticates through the fake-auth seam, UNCHANGED.

Backend reach (ADR 0006 D3): the bare /v1/messages auto-resolves the requested
model through the single surface_registry authority, but litellm_config only
carries deepseek model rows, so copilot and auggie are reached through their
per-profile prefixes (/copilot/v1/messages, /auggie/v1/messages) which pin the
named backend and bypass model auto-resolution. deepseek uses both the bare path
(with a real deepseek model id) and is consistent with the pinned path.

Each scenario is parametrized per backend with explicit ids, so a failure
isolates the exact backend + scenario cell. The feature subset per backend is the
capability ceiling fixed by responses_parity_surface.json:
  - copilot: tools.function native (tool_use OUTPUT), input.image native.
  - deepseek: tools.function translated (tool_use OUTPUT), input.image unsupported.
  - auggie:  tools.function partial (text-only ceiling, NO tool_use OUTPUT),
             input.image unsupported.
  - thinking and caching.cache_control are unsupported on ALL three backends.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import build_anthropic_app

# claude is NOT in the matrix: it is excluded from the Anthropic surface (D2).
PROVIDERS = ["copilot", "deepseek", "auggie"]

# A real deepseek model id from litellm_config so the bare /v1/messages path can
# auto-resolve a backend; copilot/auggie have no config rows and are reached via
# their pinned per-profile prefix instead.
_DEEPSEEK_MODEL = "deepseek-v4-pro"

# Backends that emit a tool_use OUTPUT block for a tools request (native or
# translated function calling). auggie is text-only (partial), so it degrades.
_TOOL_OUTPUT_PROVIDERS = ["copilot", "deepseek"]


def _build_client() -> httpx.AsyncClient:
    app = build_anthropic_app(
        {provider: FixtureAdapter(provider) for provider in PROVIDERS}
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _prefix(provider: str) -> str:
    """Pin the backend through its per-profile prefix (bypasses model resolution)."""
    return f"/{provider}/v1"


def _model_for(provider: str) -> str:
    """A model id that satisfies the gate; on a pinned prefix only the family matters."""
    return _DEEPSEEK_MODEL if provider == "deepseek" else f"{provider}-default"


def _messages_body(provider: str, text: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": _model_for(provider),
        "max_tokens": 64,
        "messages": [{"role": "user", "content": text}],
    }
    body.update(extra)
    return body


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Decode the Anthropic SSE body into ordered event payloads."""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def _collapse_repeated_deltas(types: list[str]) -> list[str]:
    """Collapse consecutive content_block_delta events into one delta phase.

    Adapters legitimately differ in how finely they chunk text deltas, so the
    canonical Anthropic event sequence treats any run of content_block_delta as a
    single delta phase (mirrors the Responses parity helper).
    """
    collapsed: list[str] = []
    for event_type in types:
        if (
            event_type == "content_block_delta"
            and collapsed
            and collapsed[-1] == "content_block_delta"
        ):
            continue
        collapsed.append(event_type)
    return collapsed


# --- non-streaming message body --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_nonstreaming_message_body_shape(provider: str) -> None:
    """POST /v1/messages -> a valid Anthropic non-streaming message body."""
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/messages",
            json=_messages_body(provider, "Say hi."),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["id"], str) and body["id"].startswith("msg_")
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert isinstance(body["content"], list)
    assert body["stop_reason"] in {
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
    }
    usage = body["usage"]
    assert isinstance(usage["input_tokens"], int)
    assert isinstance(usage["output_tokens"], int)
    text = "".join(
        block["text"] for block in body["content"] if block["type"] == "text"
    )
    assert text, "a text turn must surface a non-empty text content block"


# --- streaming event order + delta concatenation ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_streaming_event_order_and_text(provider: str) -> None:
    """POST /v1/messages stream=true -> valid Anthropic event order; text concatenates."""
    async with _build_client() as client:
        async with client.stream(
            "POST",
            f"{_prefix(provider)}/messages",
            json=_messages_body(provider, "Say hi.", stream=True),
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_sse(text)
    types = [event["type"] for event in events]
    # Canonical Anthropic grammar with consecutive deltas collapsed to one phase.
    assert _collapse_repeated_deltas(types) == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert types[0] == "message_start"
    assert types.count("ping") == 1, "exactly one ping per Anthropic stream"
    assert types[-1] == "message_stop"

    start = next(e for e in events if e["type"] == "message_start")
    assert start["message"]["id"].startswith("msg_")
    assert start["message"]["role"] == "assistant"

    streamed = "".join(
        event["delta"]["text"]
        for event in events
        if event["type"] == "content_block_delta"
        and event["delta"]["type"] == "text_delta"
    )
    assert streamed == "Hi there.", "concatenated text_deltas must equal the full text"


# --- tool_use round-trip (copilot/deepseek) --------------------------------


_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _TOOL_OUTPUT_PROVIDERS)
async def test_tool_use_output_block(provider: str) -> None:
    """copilot/deepseek emit a tool_use OUTPUT block for a tools request."""
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/messages",
            json=_messages_body(
                provider,
                "What is the weather in Paris?",
                tools=_TOOLS,
                tool_choice={"type": "auto"},
            ),
        )
    assert resp.status_code == 200
    body = resp.json()
    tool_blocks = [b for b in body["content"] if b["type"] == "tool_use"]
    assert tool_blocks, f"{provider} must emit a tool_use output block"
    call = tool_blocks[0]
    assert call["name"] == "get_weather"
    assert isinstance(call["id"], str) and call["id"]
    assert isinstance(call["input"], dict)
    assert body["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_auggie_tools_text_only_ceiling() -> None:
    """auggie classifies tools.function as partial: NO tool_use OUTPUT, text instead."""
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix('auggie')}/messages",
            json=_messages_body(
                "auggie",
                "What is the weather in Paris?",
                tools=_TOOLS,
                tool_choice={"type": "auto"},
            ),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert all(
        block["type"] != "tool_use" for block in body["content"]
    ), "auggie text-only ceiling: no tool_use output block"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text, "auggie must degrade to a non-empty text message"
    assert body["stop_reason"] == "end_turn"


# --- count_tokens + /v1/models ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_count_tokens_shape(provider: str) -> None:
    """POST /v1/messages/count_tokens -> {"input_tokens": N}."""
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/messages/count_tokens",
            json=_messages_body(provider, "hello there general kenobi"),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"input_tokens"}
    assert isinstance(body["input_tokens"], int) and body["input_tokens"] > 0


@pytest.mark.asyncio
async def test_models_listing_shape() -> None:
    """GET /v1/models -> the Anthropic-shaped listing of the surface model set."""
    async with _build_client() as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "first_id", "last_id", "has_more"}
    assert isinstance(body["data"], list) and body["data"]
    for row in body["data"]:
        assert row["type"] == "model"
        assert isinstance(row["id"], str) and row["id"]
        assert isinstance(row["display_name"], str) and row["display_name"]
    assert body["first_id"] == body["data"][0]["id"]
    assert body["last_id"] == body["data"][-1]["id"]


# --- unsupported feature x backend (gated -> 400 invalid_request_error) -----

_IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
}
_THINKING_PARAM = {"type": "enabled", "budget_tokens": 1024}
_CACHE_CONTROL_BLOCK = {
    "type": "text",
    "text": "cached system",
    "cache_control": {"type": "ephemeral"},
}


def _image_body(provider: str) -> dict[str, Any]:
    return {
        "model": _model_for(provider),
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "describe"}, _IMAGE_BLOCK],
            }
        ],
    }


def _thinking_body(provider: str) -> dict[str, Any]:
    body = _messages_body(provider, "think hard")
    body["thinking"] = _THINKING_PARAM
    return body


def _cache_control_body(provider: str) -> dict[str, Any]:
    body = _messages_body(provider, "hi")
    body["system"] = [_CACHE_CONTROL_BLOCK]
    return body


async def _assert_unsupported(
    provider: str, payload: dict[str, Any], feature: str
) -> None:
    async with _build_client() as client:
        resp = await client.post(f"{_prefix(provider)}/messages", json=payload)
    assert resp.status_code == 400, f"{feature} on {provider} must be a hard 400"
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert feature in body["error"]["message"]
    assert provider in body["error"]["message"]


# image is unsupported on deepseek and auggie (native on copilot, so not gated).
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepseek", "auggie"])
async def test_image_unsupported_on_non_copilot(provider: str) -> None:
    await _assert_unsupported(provider, _image_body(provider), "input.image")


# extended thinking is unsupported on ALL three backends.
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_thinking_unsupported_on_all(provider: str) -> None:
    await _assert_unsupported(provider, _thinking_body(provider), "thinking")


# cache_control is unsupported on ALL three backends.
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_cache_control_unsupported_on_all(provider: str) -> None:
    await _assert_unsupported(
        provider, _cache_control_body(provider), "caching.cache_control"
    )
