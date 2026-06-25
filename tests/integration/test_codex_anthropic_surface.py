"""Codex backend wiring on the Anthropic Messages surface (Milestone 2, G004).

These tests exercise the codex backend through the SAME deterministic
FixtureAdapter seam the parity harness uses (no real ``codex`` CLI, endpoint, or
credential is touched). They pin the G004 contract:

- routing: POST /v1/messages with a gpt-* model auto-resolves to the codex
  backend via the single surface_registry authority and is served;
- tools: codex classifies tools.function as PARTIAL (text-only ceiling, mirror of
  auggie): the field is accepted (200) but no tool_use OUTPUT block is emitted;
- gating: image is UNSUPPORTED on codex and rejected with a 400
  invalid_request_error before dispatch; thinking and cache_control are DEGRADED
  (stripped) before gating, so a request carrying either succeeds (200) instead
  of 400.

codex is Anthropic-surface-ONLY; the Responses-surface exclusion is covered
separately (G006 test_codex_responses_exclusion). The FixtureAdapter is reused
under the ``codex`` backend key; its internal ``provider="auggie"`` gives the
same text-only tool ceiling codex declares in the parity surface, while routing
and capability gating key on the resolved ``codex`` backend name, not the
adapter's internal provider label.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.protocols.model_exposure import CODEX_BUILTIN_MODELS

# The five gpt ids served first-party on the Anthropic surface (PRD / ADR 0007).
_GPT_MODELS = list(CODEX_BUILTIN_MODELS)


def _build_client() -> httpx.AsyncClient:
    # Register the FixtureAdapter under the codex backend key. provider="auggie"
    # reuses auggie's text-only tool ceiling, which codex mirrors; routing and
    # gating key on the resolved "codex" backend name regardless.
    app = build_anthropic_app({"codex": FixtureAdapter("auggie")})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _messages_body(model: str, text: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": text}],
    }
    body.update(extra)
    return body


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


# --- routing: gpt-* auto-resolves to codex and is served ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _GPT_MODELS)
async def test_gpt_model_routes_to_codex_backend(model: str) -> None:
    """POST /v1/messages with a gpt-* model resolves to codex and returns a body."""
    async with _build_client() as client:
        resp = await client.post("/v1/messages", json=_messages_body(model, "Say hi."))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text, "a codex text turn must surface a non-empty text content block"


# --- tools: text-only ceiling (mirror of auggie) -----------------------------


@pytest.mark.asyncio
async def test_codex_tools_text_only_ceiling() -> None:
    """codex tools.function is partial: accepted (200), NO tool_use OUTPUT block."""
    async with _build_client() as client:
        resp = await client.post(
            "/v1/messages",
            json=_messages_body(
                "gpt-5.5",
                "What is the weather in Paris?",
                tools=_TOOLS,
                tool_choice={"type": "auto"},
            ),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert all(
        block["type"] != "tool_use" for block in body["content"]
    ), "codex text-only ceiling: no tool_use output block"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text, "codex must degrade to a non-empty text message"


# --- gating: image is a 400; thinking + cache_control degrade to 200 ---------


async def _assert_unsupported(payload: dict[str, Any], feature: str) -> None:
    async with _build_client() as client:
        resp = await client.post("/v1/messages", json=payload)
    assert (
        resp.status_code == 400
    ), f"{feature} on codex must be a hard 400: {resp.text}"
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert feature in body["error"]["message"]
    assert "codex" in body["error"]["message"]


@pytest.mark.asyncio
async def test_image_unsupported_on_codex() -> None:
    payload = {
        "model": "gpt-5.5",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "aGk=",
                        },
                    },
                ],
            }
        ],
    }
    await _assert_unsupported(payload, "input.image")


@pytest.mark.asyncio
async def test_thinking_degraded_on_codex() -> None:
    # codex cannot emit an extended-thinking trace, so the thinking param is
    # stripped (degraded) before gating: this SAME payload that used to 400 now
    # succeeds with a normal codex text turn. This is the gpt-5.5 "hello" case.
    payload = _messages_body("gpt-5.5", "think hard")
    payload["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    async with _build_client() as client:
        resp = await client.post("/v1/messages", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text, "a codex text turn must surface a non-empty text content block"


@pytest.mark.asyncio
async def test_cache_control_degraded_on_codex() -> None:
    # cache_control is degraded (stripped) before gating, so this SAME payload
    # that used to 400 now succeeds with a normal codex text turn.
    payload = _messages_body("gpt-5.5", "hi")
    payload["system"] = [
        {
            "type": "text",
            "text": "cached system",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    async with _build_client() as client:
        resp = await client.post("/v1/messages", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text, "a codex text turn must surface a non-empty text content block"


# --- streaming smoke (codex text turn streams a valid Anthropic sequence) -----


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


@pytest.mark.asyncio
async def test_codex_streaming_event_order() -> None:
    """POST /v1/messages stream=true on a gpt-* model yields the Anthropic grammar."""
    async with _build_client() as client:
        async with client.stream(
            "POST",
            "/v1/messages",
            json=_messages_body("gpt-5.5", "Say hi.", stream=True),
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            text = "".join([chunk async for chunk in resp.aiter_text()])
    types = [event["type"] for event in _parse_sse(text)]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert types.count("ping") == 1
