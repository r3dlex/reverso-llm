"""Real-adapter Codex Responses parity re-run (ADR 0002, Lane D4).

The provider-agnostic harness (test_responses_provider_contract) proved contract
parity through the fake FixtureAdapter seam. This module re-runs the SAME fixture
matrix against the REAL adapters wired into build_app:
  - reverso.protocols.adapters.claude.ClaudeAdapter
  - reverso.protocols.adapters.copilot.CopilotAdapter

No real endpoint or credential is touched. The Claude adapter's auth is a fake
resolution on the OAuth path and its claude CLI subprocess is replaced by an
injected cli_runner that returns fixture text. The Copilot adapter's auth is a
fake bearer and its httpx client_factory is replaced by a stub that replays
fixture bodies and SSE bytes, so the ported direct-forward spine never reaches
api.githubcopilot.com.

The real adapters generate or echo their own response ids, so id-dependent
fixtures (get_response, input_items, previous_response_id) drive the adapter
end to end and assert on the runtime id rather than the fixture placeholder.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import load_fixture
from reverso.protocols.adapters.claude import ClaudeAdapter, OAUTH_METHOD
from reverso.protocols.adapters.copilot import CopilotAdapter
from reverso.protocols.auth import AuthResolution
from reverso.protocols.responses_app import build_app

PROVIDERS = ["claude", "copilot"]


# --- Claude fakes ---------------------------------------------------------


class _FakeClaudeAuth:
    """Authenticated OAuth resolution with no Keychain/file/network access."""

    def resolve(self) -> AuthResolution:
        return AuthResolution(
            authenticated=True,
            method=OAUTH_METHOD,
            subscription_type="max",
            details={"source": "fake"},
        )

    async def bearer_token(self) -> str:
        return "fake-oauth-token"


def _claude_cli_runner(prompt: str, model: str) -> str:
    """Stub for the claude CLI subprocess; returns fixture-shaped assistant text.

    The text is selected by prompt content so the same runner serves every
    fixture (plain, chain turns, tool follow-up) deterministically.
    """
    if "weather" in prompt.lower():
        return "It is 18 degrees and clear in Paris."
    if "my name is ada" in prompt.lower():
        return "Nice to meet you, Ada."
    if "what is my name" in prompt.lower():
        return "Your name is Ada."
    if "capital of france" in prompt.lower():
        return "The capital of France is Paris."
    if prompt.strip().lower().startswith("say hi"):
        return "Hi there."
    return "ok"


def _build_claude_adapter() -> ClaudeAdapter:
    return ClaudeAdapter(auth=_FakeClaudeAuth(), cli_runner=_claude_cli_runner)


# --- Copilot fakes --------------------------------------------------------


class _FakeCopilotAuth:
    def resolve(self) -> AuthResolution:
        return AuthResolution(
            authenticated=True, method="copilot_oauth", details={"source": "fake"}
        )

    async def bearer_token(self) -> str:
        return "fake-copilot-bearer"


def _sse_bytes_from_fixture(events: list[dict[str, Any]]) -> bytes:
    out = b""
    for event in events:
        payload = json.dumps(event, separators=(",", ":"))
        out += f"event: {event['type']}\ndata: {payload}\n\n".encode("utf-8")
    out += b"data: [DONE]\n\n"
    return out


class _FakeStreamResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        yield self._body

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeCopilotClient:
    """Replays fixture bodies for the Copilot adapter's forward calls.

    POST /responses returns the matching fixture body (selected by request
    content), GET /models returns a Copilot-style model payload, and a streaming
    POST replays the streaming fixture's SSE bytes. Nothing leaves the process.
    """

    def __init__(self) -> None:
        self._nonstream = load_fixture("create_response_nonstreaming.json")
        self._stream = load_fixture("create_response_streaming.json")
        self._tools = load_fixture("tools_function_call.json")
        self._chain = load_fixture("previous_response_id_chain.json")

    async def __aenter__(self) -> "_FakeCopilotClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def _select_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("tools"):
            return self._tools["expected"]["body"]
        input_value = payload.get("input")
        if payload.get("previous_response_id") is not None:
            if isinstance(input_value, list) and any(
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                for item in input_value
            ):
                return self._tools["followup"]["expected"]["body"]
            return self._chain["turns"][1]["expected"]["body"]
        if input_value == self._chain["turns"][0]["request"]["body"]["input"]:
            return self._chain["turns"][0]["expected"]["body"]
        return self._nonstream["expected"]["body"]

    async def post(
        self, url: str, *, headers: dict[str, str], content: bytes
    ) -> httpx.Response:
        payload = json.loads(content)
        body = self._select_body(payload)
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o-copilot", "vendor": "github-copilot"},
                ]
            },
            request=httpx.Request("GET", url),
        )

    def stream(
        self, method: str, url: str, *, headers: dict[str, str], content: bytes
    ) -> _FakeStreamResponse:
        body = _sse_bytes_from_fixture(self._stream["expected"]["events"])
        return _FakeStreamResponse(body)


def _build_copilot_adapter() -> CopilotAdapter:
    return CopilotAdapter(
        auth=_FakeCopilotAuth(),
        client_factory=lambda: _FakeCopilotClient(),
    )


# --- App / client wiring --------------------------------------------------


def _build_client() -> httpx.AsyncClient:
    app = build_app(
        {
            "claude": _build_claude_adapter(),
            "copilot": _build_copilot_adapter(),
        }
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _prefix(provider: str) -> str:
    return f"/{provider}/v1"


def _parse_sse(text: str) -> tuple[list[dict[str, Any]], bool]:
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


def _message_text(body: dict[str, Any]) -> str:
    message = next(item for item in body["output"] if item["type"] == "message")
    return "".join(
        part["text"] for part in message["content"] if part["type"] == "output_text"
    )


def _collapse_repeated_deltas(types: list[str]) -> list[str]:
    """Collapse consecutive response.output_text.delta events into one.

    Adapters legitimately differ in how finely they chunk text deltas, so the
    canonical event sequence treats any run of deltas as a single delta phase.
    """
    collapsed: list[str] = []
    for event_type in types:
        if (
            event_type == "response.output_text.delta"
            and collapsed
            and collapsed[-1] == "response.output_text.delta"
        ):
            continue
        collapsed.append(event_type)
    return collapsed


# --- Parity matrix (same fixtures, real adapters) -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_create_response_nonstreaming(provider: str) -> None:
    fixture = load_fixture("create_response_nonstreaming.json")
    asserts = fixture["assertions"]
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/responses", json=fixture["request"]["body"]
        )
    assert resp.status_code == asserts["status"]
    body = resp.json()
    assert body["object"] == asserts["object"]
    assert body["status"] == asserts["status_field"]
    assert isinstance(body["id"], str) and body["id"]
    assert _message_text(body) == asserts["output_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_create_response_streaming(provider: str) -> None:
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
    # Real adapters may chunk output_text.delta differently (one delta vs many);
    # the contract is the canonical event sequence with consecutive deltas
    # collapsed, plus delta text that concatenates to the full output.
    assert _collapse_repeated_deltas(types) == _collapse_repeated_deltas(
        asserts["event_order"]
    )
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
async def test_real_list_models(provider: str) -> None:
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
    assert isinstance(body["data"], list) and body["data"]
    assert body["data"][0]["object"] == asserts["data_item_object"]
    assert body["data"][0]["id"]
    assert "models" in body, "Codex refresh field must be present"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_get_response_roundtrip(provider: str) -> None:
    """create_response then GET /responses/{runtime_id} returns the same object."""
    create_fixture = load_fixture("create_response_nonstreaming.json")
    async with _build_client() as client:
        created = await client.post(
            f"{_prefix(provider)}/responses",
            json=create_fixture["request"]["body"],
        )
        assert created.status_code == 200
        response_id = created.json()["id"]
        assert response_id

        fetched = await client.get(f"{_prefix(provider)}/responses/{response_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["object"] == "response"
    assert body["id"] == response_id
    assert body["status"] == "completed"
    assert _message_text(body) == create_fixture["assertions"]["output_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_list_input_items_roundtrip(provider: str) -> None:
    """create_response records input items retrievable for the runtime id.

    Uses list-form input (the OpenAI input-item shape) which both real adapters
    record; a bare-string input is recorded differently per provider and is not
    part of the cross-provider contract.
    """
    expected_text = "What is the capital of France?"
    request_body = {
        "model": "gpt-5.5",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": expected_text}],
            }
        ],
        "stream": False,
    }
    async with _build_client() as client:
        created = await client.post(
            f"{_prefix(provider)}/responses",
            json=request_body,
        )
        response_id = created.json()["id"]
        items = await client.get(
            f"{_prefix(provider)}/responses/{response_id}/input_items"
        )
    assert items.status_code == 200
    body = items.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"]
    first = body["data"][0]
    text = "".join(
        part["text"]
        for part in first.get("content", [])
        if isinstance(part, dict) and part.get("type") == "input_text"
    )
    assert text == expected_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_previous_response_id_chain(provider: str) -> None:
    fixture = load_fixture("previous_response_id_chain.json")
    asserts = fixture["assertions"]
    turns = fixture["turns"]
    async with _build_client() as client:
        first = await client.post(
            f"{_prefix(provider)}/responses", json=turns[0]["request"]["body"]
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
        assert first_id

        second_body = dict(turns[1]["request"]["body"])
        second_body["previous_response_id"] = first_id
        second = await client.post(
            f"{_prefix(provider)}/responses", json=second_body
        )
    assert second.status_code == asserts["second_turn_status"]
    body = second.json()
    assert body["previous_response_id"] == first_id
    assert _message_text(body) == asserts["second_turn_output_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_real_tools_function_call(provider: str) -> None:
    """Tools surface: copilot forwards function_call shapes; claude has no native

    tool-call surface (text-only CLI spine) so the contract for it is a valid
    pass-through Response, not a synthesized function_call (test-spec class 5
    allows pass-through / translation / explicit unsupported handling).
    """
    fixture = load_fixture("tools_function_call.json")
    asserts = fixture["assertions"]
    async with _build_client() as client:
        resp = await client.post(
            f"{_prefix(provider)}/responses", json=fixture["request"]["body"]
        )
        assert resp.status_code == asserts["status"]
        body = resp.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"

        function_calls = [
            item for item in body["output"] if item.get("type") == "function_call"
        ]
        if provider == "copilot":
            call = function_calls[0]
            assert call["name"] == asserts["function_call_name"]
            assert isinstance(call["call_id"], str) and call["call_id"]
            json.loads(call["arguments"])
        else:
            # Claude text CLI cannot emit a native function_call item; it must
            # still return a well-formed Response (graceful pass-through).
            assert function_calls == []
            assert any(item.get("type") == "message" for item in body["output"])
            return

        followup_body = dict(fixture["followup"]["request"]["body"])
        followup_body["previous_response_id"] = body["id"]
        followup = await client.post(
            f"{_prefix(provider)}/responses", json=followup_body
        )
    assert followup.status_code == asserts["followup_status"]
    fbody = followup.json()
    assert fbody["previous_response_id"] == body["id"]
    assert _message_text(fbody) == asserts["followup_output_text"]
