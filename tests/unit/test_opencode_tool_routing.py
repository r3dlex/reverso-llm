"""OCG-G9: tool-aware native routing (fixes the gap OCG-G7 filed).

The endpoint deny-list answers "does this model speak the Anthropic format at
all". It does NOT answer "does it accept tools on that endpoint", and G7's
end-to-end proof showed those are different questions: 11 of the 22
/messages-capable ids return 400 when the request carries tools, while accepting
the same tool declaration on /chat/completions.

Claude Code sends tools on essentially every turn, so a model-only decision made
those 11 ids look healthy in the picker while failing every real session.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reverso.protocols.adapters.opencode.adapter import OpenCodeAdapter
from reverso.protocols.adapters.opencode.catalog import (
    ANTHROPIC_TOOL_UNSUPPORTED_MODELS,
    ANTHROPIC_UNSUPPORTED_MODELS,
    CHAT_COMPLETIONS_PATH,
    MESSAGES_PATH,
    anthropic_endpoint_for,
)
from reverso.protocols.adapters.opencode.credentials import OPENCODE_API_KEY_ENV

API_KEY_SENTINEL = "sk-OPENCODEsentinelKEY-do-not-leak-4b3a2c1d"
TOOL = {
    "name": "get_weather",
    "description": "w",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
}


@pytest.fixture(autouse=True)
def _key(monkeypatch) -> None:
    monkeypatch.setenv(OPENCODE_API_KEY_ENV, API_KEY_SENTINEL)


def _payload(model: str, *, tools: bool) -> dict:
    body = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    if tools:
        body["tools"] = [TOOL]
    return body


def test_the_tool_deny_list_matches_the_recorded_proof() -> None:
    from pathlib import Path

    from reverso.opencode_catalog_artifact import repo_root

    proof = json.loads(
        (repo_root() / Path("docs/reference/opencode-go-proof.json")).read_text()
    )
    assert set(proof["anthropic_tools_rejected"]) == set(
        ANTHROPIC_TOOL_UNSUPPORTED_MODELS
    )


def test_tool_support_is_not_inferred_from_endpoint_support() -> None:
    """The two sets are almost disjoint; neither implies the other."""
    assert ANTHROPIC_TOOL_UNSUPPORTED_MODELS
    assert not (ANTHROPIC_TOOL_UNSUPPORTED_MODELS & ANTHROPIC_UNSUPPORTED_MODELS)


def test_endpoint_choice_is_tool_aware() -> None:
    assert anthropic_endpoint_for("glm-5") == MESSAGES_PATH
    assert anthropic_endpoint_for("glm-5", has_tools=True) == CHAT_COMPLETIONS_PATH


def test_a_tool_supporting_model_still_goes_native() -> None:
    assert anthropic_endpoint_for("kimi-k3", has_tools=True) == MESSAGES_PATH


def test_the_endpoint_deny_list_still_wins_with_tools() -> None:
    assert anthropic_endpoint_for("grok-4.5", has_tools=True) == CHAT_COMPLETIONS_PATH


@pytest.mark.asyncio
async def test_tool_bearing_request_for_a_rejecting_model_avoids_messages() -> None:
    """The regression proper: this used to POST /messages and 400."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "model": "glm-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    adapter = OpenCodeAdapter(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=30.0
        )
    )
    message = await adapter.create_anthropic_message(_payload("glm-5", tools=True))
    assert all(not p.endswith("/messages") for p in paths), paths
    assert any(p.endswith("/chat/completions") for p in paths), paths
    assert message.get("type") == "message"


@pytest.mark.asyncio
async def test_tool_free_request_for_the_same_model_stays_native() -> None:
    """The fix must not cost the native path for requests that work on it."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "model": "glm-5",
            },
        )

    adapter = OpenCodeAdapter(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=30.0
        )
    )
    await adapter.create_anthropic_message(_payload("glm-5", tools=False))
    assert any(p.endswith("/messages") for p in paths), paths


@pytest.mark.asyncio
async def test_an_empty_tools_list_is_not_a_tool_request() -> None:
    """An empty list is what a client sends when it has no tools; treating it as
    tool-bearing would needlessly abandon the native path."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "glm-5",
            },
        )

    adapter = OpenCodeAdapter(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=30.0
        )
    )
    body = _payload("glm-5", tools=False)
    body["tools"] = []
    await adapter.create_anthropic_message(body)
    assert any(p.endswith("/messages") for p in paths), paths


@pytest.mark.asyncio
async def test_streaming_is_tool_aware_too() -> None:
    """The streaming fallback issues a UNARY translated call, per G5's design
    note, so the handler answers with a chat completion rather than SSE."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "model": "glm-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    adapter = OpenCodeAdapter(
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=30.0
        )
    )
    async for _ in adapter.stream_anthropic_message(_payload("glm-5", tools=True)):
        pass
    assert all(not p.endswith("/messages") for p in paths), paths
