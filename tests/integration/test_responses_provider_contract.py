"""Provider-agnostic Codex Responses parity harness (ADR 0002, test-spec).

The SAME Codex-observed fixture matrix (tests/fixtures/responses) runs against
all five routed provider paths (claude, copilot, auggie, deepseek, kimi)
through the first-party app (reverso.protocols.responses_app.build_app). Every
provider is backed by the deterministic FixtureAdapter (conftest), which
authenticates through the fake-auth seam and replays fixture bodies/events; no
real provider endpoint, process, or credential is touched. The providers share
a single loopback port via path-prefix routing, so no new listener or process
is spawned per provider. Identical assertions apply per provider, so a failure
isolates which provider broke contract parity.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx
import pytest

from conftest import FixtureAdapter, load_fixture
from reverso.protocols.adapter import ResponseEnvelope, ResponsesRequest, SSEEvent
from reverso.protocols.responses_app import build_app

PROVIDERS = ["claude", "copilot", "auggie", "deepseek", "kimi"]
AFFECTED_INCLUDE_PROVIDERS = ["claude", "auggie", "deepseek", "kimi"]
EXACT_ENCRYPTED_CONTENT_INCLUDE = ["reasoning.encrypted_content"]


def _build_client() -> httpx.AsyncClient:
    app = build_app({provider: FixtureAdapter(provider) for provider in PROVIDERS})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


class _RecordingFixtureAdapter(FixtureAdapter):
    """Record the normalized request that reaches the deterministic adapter."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider)
        self.create_requests: list[ResponsesRequest] = []
        self.stream_requests: list[ResponsesRequest] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.create_requests.append(request)
        return await super().create_response(request)

    async def stream_response(
        self,
        request: ResponsesRequest,
    ) -> AsyncIterator[SSEEvent]:
        self.stream_requests.append(request)
        async for event in super().stream_response(request):
            yield event


class _ProviderAuthError(PermissionError):
    """Deterministic provider authentication failure."""


class _OutcomeRecordingFixtureAdapter(_RecordingFixtureAdapter):
    """Return or raise a deterministic outcome after provider dispatch."""

    def __init__(self, provider: str, outcome: str) -> None:
        super().__init__(provider)
        self.outcome = outcome

    def _raise_if_needed(self) -> None:
        if self.outcome == "auth-error":
            raise _ProviderAuthError("deterministic provider auth failure")
        if self.outcome == "timeout":
            raise TimeoutError("deterministic provider timeout")

    def _envelope(self) -> ResponseEnvelope:
        text = "Provider quota exhausted." if self.outcome == "quota-message" else None
        output = (
            [
                {
                    "id": "msg_provider_health",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        }
                    ],
                }
            ]
            if text is not None
            else []
        )
        raw = {
            "id": "resp_provider_health",
            "object": "response",
            "status": "completed",
            "model": "fixture-model",
            "output": output,
        }
        return ResponseEnvelope(
            id=raw["id"],
            model=raw["model"],
            output=output,
            raw=raw,
        )

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.create_requests.append(request)
        self._raise_if_needed()
        return self._envelope()

    async def stream_response(
        self,
        request: ResponsesRequest,
    ) -> AsyncIterator[SSEEvent]:
        self.stream_requests.append(request)
        self._raise_if_needed()
        envelope = self._envelope()
        if self.outcome == "quota-message":
            yield SSEEvent(
                event="response.output_text.delta",
                data={
                    "type": "response.output_text.delta",
                    "delta": "Provider quota exhausted.",
                },
            )
        yield SSEEvent(
            event="response.completed",
            data={"type": "response.completed", "response": envelope.raw},
        )


def _build_recording_client(
    *,
    outcome_provider: str | None = None,
    outcome: str | None = None,
) -> tuple[httpx.AsyncClient, dict[str, _RecordingFixtureAdapter]]:
    adapters: dict[str, _RecordingFixtureAdapter] = {}
    for provider in PROVIDERS:
        if provider == outcome_provider and outcome is not None:
            adapters[provider] = _OutcomeRecordingFixtureAdapter(provider, outcome)
        else:
            adapters[provider] = _RecordingFixtureAdapter(provider)
    app = build_app(adapters)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:64946",
    )
    return client, adapters


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


def _assert_expected_dispatch(
    adapter: _RecordingFixtureAdapter,
    *,
    stream: bool,
) -> ResponsesRequest:
    expected = adapter.stream_requests if stream else adapter.create_requests
    opposite = adapter.create_requests if stream else adapter.stream_requests
    assert len(expected) == 1
    assert opposite == []
    return expected[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "include",
    [None, [], EXACT_ENCRYPTED_CONTENT_INCLUDE],
    ids=["absent", "empty", "exact-sentinel"],
)
async def test_encrypted_content_include_accepted_and_removed_before_dispatch(
    provider: str,
    stream: bool,
    include: list[str] | None,
) -> None:
    """Accepted include shapes reach every adapter only after normalization."""
    body: dict[str, Any] = {"model": "fixture-model", "input": "hello"}
    if include is not None:
        body["include"] = include
    if stream:
        body["stream"] = True

    client, adapters = _build_recording_client()
    async with client:
        resp = await client.post(f"{_prefix(provider)}/responses", json=body)

    assert resp.status_code == 200
    request = _assert_expected_dispatch(adapters[provider], stream=stream)
    assert "include" not in request.extra


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider", AFFECTED_INCLUDE_PROVIDERS)
@pytest.mark.parametrize(
    "include",
    [
        ["reasoning.encrypted_content", "message.output_text.logprobs"],
        ["message.output_text.logprobs"],
    ],
    ids=["mixed", "unknown"],
)
async def test_non_exact_include_rejected_before_affected_provider_dispatch(
    provider: str,
    stream: bool,
    include: list[str],
) -> None:
    """Every non-exact non-empty include keeps the canonical generic gate."""
    body = {
        "model": "fixture-model",
        "input": "hello",
        "include": include,
        "stream": stream,
    }
    client, adapters = _build_recording_client()
    async with client:
        resp = await client.post(f"{_prefix(provider)}/responses", json=body)

    assert resp.status_code == 400
    assert resp.json() == {
        "error": {
            "type": "invalid_request_error",
            "code": "unsupported_feature",
            "message": f"{provider} does not support include",
        }
    }
    assert adapters[provider].create_requests == []
    assert adapters[provider].stream_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "include",
    [
        ["reasoning.encrypted_content", "message.output_text.logprobs"],
        ["message.output_text.logprobs"],
    ],
    ids=["mixed", "unknown"],
)
async def test_copilot_preserves_generic_include_acceptance_and_normalization(
    stream: bool,
    include: list[str],
) -> None:
    """Copilot still accepts generic include while the normalizer omits it."""
    body = {
        "model": "fixture-model",
        "input": "hello",
        "include": include,
        "stream": stream,
    }
    client, adapters = _build_recording_client()
    async with client:
        resp = await client.post("/copilot/v1/responses", json=body)

    assert resp.status_code == 200
    request = _assert_expected_dispatch(adapters["copilot"], stream=stream)
    assert "include" not in request.extra


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "outcome",
    ["auth-error", "timeout", "quota-message", "empty-output"],
)
async def test_provider_outcome_after_exact_include_dispatch_is_not_gate_error(
    provider: str,
    stream: bool,
    outcome: str,
) -> None:
    """Post-dispatch provider outcomes remain independent of include support."""
    body = {
        "model": "fixture-model",
        "input": "hello",
        "include": EXACT_ENCRYPTED_CONTENT_INCLUDE,
        "stream": stream,
    }
    client, adapters = _build_recording_client(
        outcome_provider=provider,
        outcome=outcome,
    )
    async with client:
        resp = await client.post(f"{_prefix(provider)}/responses", json=body)

    if outcome in {"auth-error", "timeout"}:
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "server_error"
    else:
        assert resp.status_code == 200
        if outcome == "quota-message":
            assert "Provider quota exhausted." in resp.text
        elif stream:
            events, saw_done = _parse_sse(resp.text)
            assert [event["type"] for event in events] == ["response.completed"]
            assert saw_done is True
        else:
            assert resp.json()["output"] == []
    assert "unsupported_feature" not in resp.text
    request = _assert_expected_dispatch(adapters[provider], stream=stream)
    assert "include" not in request.extra


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


TOOL_PROVIDERS = ["copilot", "deepseek", "kimi"]
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
async def test_claude_accepts_max_output_tokens_best_effort() -> None:
    """Codex may send max_output_tokens; Claude must not reject the whole turn."""
    body_in = {
        "model": "claude-haiku-4-5-20251001",
        "input": "Reply with one word.",
        "max_output_tokens": 32,
    }
    async with _build_client() as client:
        resp = await client.post("/claude/v1/responses", json=body_in)
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


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
