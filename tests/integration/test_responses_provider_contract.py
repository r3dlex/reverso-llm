"""Provider-agnostic Codex Responses parity harness (ADR 0002, test-spec).

The SAME Codex-observed fixture matrix (tests/fixtures/responses) runs against
BOTH the claude and copilot provider paths through the first-party app
(reverso.protocols.responses_app.build_app). Every provider is backed by the
deterministic FixtureAdapter (conftest), which authenticates through the
fake-auth seam and replays fixture bodies/events; no real Claude or Copilot
endpoint or credential is touched. Identical assertions apply per provider, so a
failure isolates which provider broke contract parity.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter, load_fixture
from reverso.protocols.responses_app import build_app

PROVIDERS = ["claude", "copilot"]


def _build_client() -> httpx.AsyncClient:
    app = build_app(
        {provider: FixtureAdapter(provider) for provider in PROVIDERS}
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:64946"
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
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
        call = next(
            item for item in body["output"] if item["type"] == "function_call"
        )
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
