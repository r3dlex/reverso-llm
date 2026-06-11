"""Provider-agnostic Codex Responses parity harness (ADR 0002, test-spec).

The SAME Codex-observed fixture matrix (tests/fixtures/responses) runs against
ALL FOUR provider paths (claude, copilot, auggie, deepseek) through the
first-party app (reverso.protocols.responses_app.build_app). Every provider is
backed by the deterministic FixtureAdapter (conftest), which authenticates
through the fake-auth seam and replays fixture bodies/events; no real Claude,
Copilot, Auggie, or DeepSeek endpoint, process, or credential is touched. The
four providers share a single loopback port via path-prefix routing, so no new
listener or process is spawned per provider. Identical assertions apply per
provider, so a failure isolates which provider broke contract parity.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter, load_fixture
from reverso.protocols.responses_app import build_app

PROVIDERS = ["claude", "copilot", "auggie", "deepseek"]


def _build_client() -> httpx.AsyncClient:
    app = build_app({provider: FixtureAdapter(provider) for provider in PROVIDERS})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _prefix(provider: str) -> str:
    return f"/{provider}/v1"


def _parse_sse(text: str) -> tuple[list[dict[str, Any]], bool]:
    """Return (decoded data-event payloads, saw_done) from an SSE body."""
    events: list[dict[str, Any]] = []
    saw_done = False
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                saw_done = True
                continue
            events.append(json.loads(data))
    return events, saw_done


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_create_response_nonstreaming(provider: str) -> None:
    fixture = load_fixture("create_response_nonstreaming.json")
    asserts = fixture["assertions"]
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/responses",
            json=fixture["request"]["body"],
        )
    assert resp.status_code == asserts["status"]
    body = resp.json()
    assert body["object"] == asserts["object"]
    assert body["status"] == asserts["status_field"]
    assert isinstance(body["id"], str) and body["id"]
    message = next(item for item in body["output"] if item["type"] == "message")
    assert message["role"] == asserts["output_role"]
    text = "".join(
        part["text"] for part in message["content"] if part["type"] == "output_text"
    )
    assert text == asserts["output_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_create_response_streaming(provider: str) -> None:
    fixture = load_fixture("create_response_streaming.json")
    asserts = fixture["assertions"]
    async with _build_client() as client:
        async with client.stream(
            "POST",
            f"{_prefix(provider)}/responses",
            json=fixture["request"]["body"],
        ) as resp:
            assert resp.status_code == asserts["status"]
            assert "text/event-stream" in resp.headers["content-type"]
            text = "".join([chunk async for chunk in resp.aiter_text()])

    events, saw_done = _parse_sse(text)
    types = [event["type"] for event in events]
    assert types == asserts["event_order"]
    assert types[0] == asserts["first_event_type"]
    assert types[-1] == asserts["terminal_completed_event"]
    assert saw_done, "stream must terminate with [DONE]"
    deltas = "".join(
        event.get("delta", "")
        for event in events
        if event["type"] == "response.output_text.delta"
    )
    assert deltas == asserts["concatenated_deltas"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_list_models(provider: str) -> None:
    fixture = load_fixture("list_models.json")
    asserts = fixture["assertions"]
    async with _build_client() as client:
        resp = await client.get(
            f"{_prefix(provider)}/models",
            params={"client_version": "0.0.0-fixture"},
        )
    assert resp.status_code == asserts["status"]
    body = resp.json()
    assert body["object"] == asserts["object"]
    assert isinstance(body["data"], list)
    assert body["data"][0]["object"] == asserts["data_item_object"]
    assert body["data"][0]["id"]
    assert "models" in body, "Codex refresh field must be present"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_get_response(provider: str) -> None:
    fixture = load_fixture("get_response.json")
    asserts = fixture["assertions"]
    response_id = fixture["request"]["path_params"]["response_id"]
    async with _build_client() as client:
        resp = await client.get(f"{_prefix(provider)}/responses/{response_id}")
    assert resp.status_code == asserts["status"]
    body = resp.json()
    assert body["object"] == asserts["object"]
    assert body["id"] == asserts["id_matches_request"]
    assert body["status"] == asserts["status_field"]
    message = next(item for item in body["output"] if item["type"] == "message")
    text = "".join(
        part["text"] for part in message["content"] if part["type"] == "output_text"
    )
    assert text == asserts["output_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_list_input_items(provider: str) -> None:
    fixture = load_fixture("list_input_items.json")
    asserts = fixture["assertions"]
    response_id = fixture["request"]["path_params"]["response_id"]
    async with _build_client() as client:
        resp = await client.get(
            f"{_prefix(provider)}/responses/{response_id}/input_items"
        )
    assert resp.status_code == asserts["status"]
    body = resp.json()
    assert body["object"] == asserts["object"]
    assert isinstance(body["data"], list)
    first = body["data"][0]
    assert first["role"] == asserts["first_item_role"]
    text = "".join(
        part["text"] for part in first["content"] if part["type"] == "input_text"
    )
    assert text == asserts["first_item_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_previous_response_id_chain(provider: str) -> None:
    fixture = load_fixture("previous_response_id_chain.json")
    asserts = fixture["assertions"]
    turns = fixture["turns"]
    async with _build_client() as client:
        first = await client.post(
            f"{_prefix(provider)}/responses",
            json=turns[0]["request"]["body"],
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
        assert first_id == asserts["first_turn_id"]

        second = await client.post(
            f"{_prefix(provider)}/responses",
            json=turns[1]["request"]["body"],
        )
    assert second.status_code == asserts["second_turn_status"]
    body = second.json()
    assert body["previous_response_id"] == asserts["second_turn_previous_response_id"]
    message = next(item for item in body["output"] if item["type"] == "message")
    text = "".join(
        part["text"] for part in message["content"] if part["type"] == "output_text"
    )
    assert text == asserts["second_turn_output_text"]


TOOL_PROVIDERS = ["copilot", "deepseek"]
TOOL_PARTIAL_PROVIDERS = ["claude", "auggie"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", TOOL_PROVIDERS)
async def test_tools_function_call(provider: str) -> None:
    fixture = load_fixture("tools_function_call.json")
    asserts = fixture["assertions"]
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/responses",
            json=fixture["request"]["body"],
        )
        assert resp.status_code == asserts["status"]
        body = resp.json()
        call = next(item for item in body["output"] if item["type"] == "function_call")
        assert call["name"] == asserts["function_call_name"]
        assert isinstance(call["call_id"], str) and call["call_id"]
        json.loads(call["arguments"])

        followup = await client.post(
            f"{_prefix(provider)}/responses",
            json=fixture["followup"]["request"]["body"],
        )
    assert followup.status_code == asserts["followup_status"]
    fbody = followup.json()
    assert fbody["previous_response_id"] == asserts["followup_previous_response_id"]
    message = next(item for item in fbody["output"] if item["type"] == "message")
    text = "".join(
        part["text"] for part in message["content"] if part["type"] == "output_text"
    )
    assert text == asserts["followup_output_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", TOOL_PARTIAL_PROVIDERS)
async def test_tools_function_call_partial_text_only(provider: str) -> None:
    """claude/auggie accept tools.function for codex-compat but emit no function_call items.

    Codex 0.139.0 sends a 22-entry built-in tool surface (exec_command, MCP
    tools, web_search, etc.) plus parallel_tool_calls and tool_choice="auto"
    in every Responses request. The capability table classifies those fields
    as `partial` for the CLI-spine providers: the gate accepts them (so codex
    turns can complete) but the CLI runners cannot execute client tools, so
    the response contract is a 200 with a text-only message and no
    function_call output items.
    """
    fixture = load_fixture("tools_function_call.json")
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/responses",
            json=fixture["request"]["body"],
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert all(
        item.get("type") != "function_call" for item in body.get("output", [])
    ), "claude/auggie must NOT emit function_call output items for tools.function"
    message = next(item for item in body["output"] if item["type"] == "message")
    text = "".join(
        part["text"] for part in message["content"] if part["type"] == "output_text"
    )
    assert text, "claude/auggie must return a non-empty text message"


WEB_SEARCH_PARTIAL_PROVIDERS = ["claude", "auggie", "deepseek"]


def _web_search_model(provider: str) -> str:
    if provider == "claude":
        return "claude-haiku-4-5-20251001"
    if provider == "auggie":
        return "prism-a"
    return "deepseek-v4-flash"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", WEB_SEARCH_PARTIAL_PROVIDERS)
async def test_tools_web_search_partial_text_only(provider: str) -> None:
    """tools.web_search is classified partial on claude, auggie, AND deepseek.

    The codex default tool surface includes a built-in `{"type":"web_search"}`
    entry on every request. claude/auggie ignore it in the CLI runner;
    deepseek's `_chat_tools` filters non-`function` tool entries before the
    upstream call. The gate accepts the field in all three cases so the turn
    completes with a text-only reply and no function_call output items.
    """
    body_in = {
        "model": _web_search_model(provider),
        "input": "Find me the latest news.",
        "tools": [{"type": "web_search"}],
    }
    async with _build_client() as client:
        resp = await client.post(f"{_prefix(provider)}/responses", json=body_in)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert all(item.get("type") != "function_call" for item in body.get("output", []))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", TOOL_PARTIAL_PROVIDERS)
async def test_still_unsupported_tool_returns_400(provider: str) -> None:
    """A tool type the capability table still classifies as unsupported keeps the 400.

    The codex-compat partial reclassification only covers the default surface
    (tools.function, tools.web_search, tool_choice.auto, parallel_tool_calls);
    explicitly requested tools that fall outside the default surface (e.g.
    file_search, computer_use, code_interpreter) still raise
    unsupported_feature.
    """
    body_in = {
        "model": "claude-haiku-4-5-20251001" if provider == "claude" else "prism-a",
        "input": "hi",
        "tools": [{"type": "file_search"}],
    }
    async with _build_client() as client:
        resp = await client.post(f"{_prefix(provider)}/responses", json=body_in)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "unsupported_feature"
    assert body["error"]["message"] == f"{provider} does not support tools.file_search"
