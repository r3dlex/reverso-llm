"""Unit tests for the per-provider Responses feature gate (B1).

Covers the hybrid contract from .omc/plans/ralplan-codex-reverso-full-integration.md
section B1:

  * Fast path (responses_app pre-dispatch) returns the structured 400
    unsupported_feature body and the adapter runner is NEVER invoked.
  * Back-stop (an adapter raises UnsupportedFeature from inside create_response
    or stream_response) renders the IDENTICAL body via the same shared builder.
  * The capability tables are generated from
    .omc/research/responses-parity-surface.json (the package mirror in
    src/reverso/protocols/data/responses_parity_surface.json must stay
    byte-identical to the human-authored source of truth so the table cannot
    drift).
  * extract_features maps Responses request shapes to the feature keys the table
    uses so the gate enforces what the caller actually requested, including
    Codex-only fields the normalizer strips before adapter dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.feature_policy import (
    CAPABILITY_TABLES,
    FEATURES,
    PROVIDERS,
    UnsupportedFeature,
    build_unsupported_payload,
    check_features,
    extract_features,
)
from reverso.protocols.responses_app import build_app

BASE_URL = "http://127.0.0.1:64946"


class _NeverCalledAdapter:
    """Adapter whose handlers MUST never be invoked when the fast-path gate fires.

    Any call records the offense and raises so a leaked dispatch becomes a hard
    test failure rather than a silent pass through the runner.
    """

    def __init__(self) -> None:
        self.create_called = 0
        self.stream_called = 0

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.create_called += 1
        raise AssertionError("create_response must not be invoked when the gate fires")

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        self.stream_called += 1

        async def _empty() -> AsyncIterator[SSEEvent]:
            raise AssertionError(
                "stream_response must not be invoked when the gate fires"
            )
            yield  # pragma: no cover

        return _empty()

    async def list_models(self) -> ModelList:
        return ModelList()

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


class _BackstopAdapter:
    """Adapter that raises UnsupportedFeature from inside the runner.

    Exercises the back-stop path: the request shape passes the fast path table
    but the adapter discovers a capability gap mid-flight (e.g. a feature the
    table has not yet enumerated) and raises the typed exception so the app can
    render the same 400 body.
    """

    def __init__(self, provider: str, feature: str) -> None:
        self.provider = provider
        self.feature = feature

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        raise UnsupportedFeature(provider=self.provider, feature=self.feature)

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        provider = self.provider
        feature = self.feature

        async def _stream() -> AsyncIterator[SSEEvent]:
            raise UnsupportedFeature(provider=provider, feature=feature)
            yield  # pragma: no cover

        return _stream()

    async def list_models(self) -> ModelList:
        return ModelList()

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


class _RecordingAdapter(_NeverCalledAdapter):
    """Adapter that records the normalized request reaching each dispatch path."""

    def __init__(self) -> None:
        super().__init__()
        self.create_requests: list[ResponsesRequest] = []
        self.stream_requests: list[ResponsesRequest] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.create_requests.append(request)
        return ResponseEnvelope(id="resp_recorded", model=request.model)

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        self.stream_requests.append(request)

        async def _stream() -> AsyncIterator[SSEEvent]:
            yield SSEEvent(
                event="response.completed",
                data={
                    "type": "response.completed",
                    "response": {
                        "id": "resp_recorded",
                        "object": "response",
                        "status": "completed",
                        "model": request.model,
                        "output": [],
                    },
                },
            )

        return _stream()


def _client(adapter: Any, provider: str) -> httpx.AsyncClient:
    app = build_app({provider: adapter})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


def _expected_payload(provider: str, feature: str) -> dict[str, Any]:
    return {
        "error": {
            "type": "invalid_request_error",
            "code": "unsupported_feature",
            "message": f"{provider} does not support {feature}",
        }
    }


# --- provider-scoped catalog models -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("provider", "qualified_model", "bare_model"),
    [
        ("copilot", "copilot/claude-fable-5", "claude-fable-5"),
        ("auggie", "auggie/opus4.8", "opus4.8"),
    ],
)
async def test_matching_catalog_model_prefix_is_stripped_before_dispatch(
    provider: str, qualified_model: str, bare_model: str, stream: bool
) -> None:
    adapter = _RecordingAdapter()
    async with _client(adapter, provider) as client:
        async with client.stream(
            "POST",
            f"/{provider}/v1/responses",
            json={"model": qualified_model, "input": "hi", "stream": stream},
        ) as response:
            await response.aread()

    assert response.status_code == 200
    requests = adapter.stream_requests if stream else adapter.create_requests
    assert len(requests) == 1
    assert requests[0].model == bare_model


@pytest.mark.asyncio
async def test_foreign_catalog_model_prefix_is_not_stripped() -> None:
    adapter = _RecordingAdapter()
    async with _client(adapter, "copilot") as client:
        response = await client.post(
            "/copilot/v1/responses",
            json={"model": "auggie/opus4.8", "input": "hi"},
        )

    assert response.status_code == 200
    assert adapter.create_requests[0].model == "auggie/opus4.8"


# --- table sourcing -------------------------------------------------------


def test_capability_tables_match_research_json() -> None:
    """The package mirror MUST stay byte-identical to the research source.

    The plan requires capability tables to be GENERATED from the research JSON
    rather than hand-transcribed; this test guards against drift between the
    human-authored .omc/research/responses-parity-surface.json and the
    runtime-readable copy in src/reverso/protocols/data/.
    """
    repo_root = Path(__file__).resolve().parents[2]
    research_path = repo_root / ".omc" / "research" / "responses-parity-surface.json"
    package_path = (
        repo_root
        / "src"
        / "reverso"
        / "protocols"
        / "data"
        / "responses_parity_surface.json"
    )
    assert (
        research_path.read_bytes() == package_path.read_bytes()
    ), "package parity surface mirror must match .omc/research/ source"


def test_capability_tables_cover_all_providers_and_features() -> None:
    """Every feature must declare a classification for every provider.

    Belt-and-suspenders to the generator step: a partial JSON would silently
    leave a provider 'allowed' for a feature whose support is actually unknown.
    """
    # codex (Milestone 2) is the fifth capability column, served on the Anthropic
    # surface only; it mirrors auggie's text-only ceiling.
    assert set(PROVIDERS) == {
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "codex",
        "kimi",
    }
    for provider in PROVIDERS:
        assert set(CAPABILITY_TABLES[provider].keys()) == set(FEATURES)


def test_kimi_capabilities_match_deepseek_translation_ceiling() -> None:
    assert CAPABILITY_TABLES["kimi"] == CAPABILITY_TABLES["deepseek"]


def test_check_features_passes_when_no_unsupported() -> None:
    check_features("copilot", {"input.string", "instructions", "previous_response_id"})


def test_check_features_raises_on_unsupported() -> None:
    with pytest.raises(UnsupportedFeature) as exc_info:
        check_features("claude", {"tools.file_search"})
    assert exc_info.value.provider == "claude"
    assert exc_info.value.feature == "tools.file_search"


def test_user_field_translated_on_deepseek() -> None:
    """D2: deepseek lifts `user` to translated (architect-gated upstream OK).

    The deepseek chat-completions upstream accepts a `user` field as an
    end-user identifier (verified by the D2 architect probe). DeepSeekAdapter
    forwards it unchanged via `_build_body`'s extra carry-through, so the
    capability table classification is `translated`, the gate admits the
    feature, and `check_features` does not raise. claude/auggie keep `user`
    as `unsupported` because the CLI runners have no equivalent.
    """
    assert CAPABILITY_TABLES["deepseek"]["user"] == "translated"
    assert CAPABILITY_TABLES["claude"]["user"] == "unsupported"
    assert CAPABILITY_TABLES["auggie"]["user"] == "unsupported"
    check_features("deepseek", {"user"})
    with pytest.raises(UnsupportedFeature):
        check_features("claude", {"user"})


def test_stream_incremental_deltas_translated_on_deepseek() -> None:
    """D1: deepseek lifts `stream.incremental_deltas` from unsupported to translated.

    The DeepSeekAdapter._stream_response path now consumes upstream
    stream=true chat-completions chunks via replay_incremental, emitting one
    response.output_text.delta per upstream content chunk (ADR 0004). claude
    keeps translated (B2 stream_cli_runner), auggie stays unsupported (no
    streaming output mode in the auggie CLI; ACP rewrite is out of scope),
    copilot stays native (raw upstream SSE passthrough).
    """
    assert CAPABILITY_TABLES["deepseek"]["stream.incremental_deltas"] == "translated"
    assert CAPABILITY_TABLES["claude"]["stream.incremental_deltas"] == "translated"
    assert CAPABILITY_TABLES["auggie"]["stream.incremental_deltas"] == "unsupported"
    assert CAPABILITY_TABLES["copilot"]["stream.incremental_deltas"] == "native"
    check_features("deepseek", {"stream.incremental_deltas"})
    with pytest.raises(UnsupportedFeature):
        check_features("auggie", {"stream.incremental_deltas"})


def test_check_features_passes_when_codex_default_surface_partial() -> None:
    """codex 0.139.0 sends a fixed default surface in every Responses request.

    The capability table reclassifies parallel_tool_calls, tool_choice.auto,
    tools.function and tools.web_search so codex turns can complete on every
    first-party provider: `partial` on claude/auggie (CLI runners ignore the
    fields) and `partial` on deepseek for tools.web_search (`_chat_tools`
    drops it before the upstream chat call); deepseek translates the other
    three. This test pins down that the gate does NOT reject any of these
    features for any non-copilot provider.
    """
    codex_default_features = {
        "input.message_list_text",
        "instructions",
        "parallel_tool_calls",
        "store",
        "stream",
        "tool_choice.auto",
        "tools.function",
        "tools.web_search",
    }
    check_features("claude", codex_default_features)
    check_features("auggie", codex_default_features)
    check_features("deepseek", codex_default_features)


def test_claude_accepts_max_output_tokens_as_best_effort() -> None:
    """Claude accepts the Responses limit even though its CLI cannot enforce it."""
    assert CAPABILITY_TABLES["claude"]["max_output_tokens"] == "partial"
    check_features("claude", {"max_output_tokens"})


def test_encrypted_content_include_capability_matrix() -> None:
    feature = "include.reasoning.encrypted_content"
    for provider in ("claude", "copilot", "auggie", "deepseek", "kimi"):
        assert CAPABILITY_TABLES[provider][feature] == "partial"
        check_features(provider, {feature})

    assert CAPABILITY_TABLES["codex"][feature] == "unsupported"
    with pytest.raises(UnsupportedFeature):
        check_features("codex", {feature})


# --- extract_features ----------------------------------------------------


def test_extract_features_string_input() -> None:
    request = ResponsesRequest.from_payload({"model": "m", "input": "hello"})
    assert extract_features(request) == {"input.string"}


def test_extract_features_message_list_text_input() -> None:
    request = ResponsesRequest.from_payload(
        {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
        }
    )
    assert "input.message_list_text" in extract_features(request)


def test_extract_features_image_input() -> None:
    request = ResponsesRequest.from_payload(
        {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,...",
                        }
                    ],
                }
            ],
        }
    )
    assert {"input.image", "input.message_list_text"}.issubset(
        extract_features(request)
    )


def test_extract_features_tools_and_tool_choice_named() -> None:
    request = ResponsesRequest.from_payload(
        {
            "model": "m",
            "input": "hi",
            "tools": [{"type": "function", "name": "fn"}],
            "tool_choice": {"type": "function", "function": {"name": "fn"}},
        }
    )
    features = extract_features(request)
    assert {"tools.function", "tool_choice.named"}.issubset(features)


def test_extract_features_codex_extras_survive_from_payload() -> None:
    """Codex-only fields reach extract_features through ResponsesRequest.extra.

    The Codex normalizer strips fields like parallel_tool_calls and reasoning
    before adapter dispatch; the gate must run BEFORE that normalization so
    those features still trigger the table even on Codex-driven traffic.
    """
    request = ResponsesRequest.from_payload(
        {
            "model": "m",
            "input": "hi",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "high", "summary": "auto"},
            "temperature": 0.4,
            "top_p": 0.9,
            "max_output_tokens": 256,
            "metadata": {"tag": "v"},
            "include": ["reasoning.encrypted_content"],
            "background": True,
            "service_tier": "auto",
            "user": "u1",
            "safety_identifier": "sid",
            "store": True,
            "truncation": "auto",
            "text": {"format": {"type": "json_schema", "schema": {}}},
        }
    )
    features = extract_features(request)
    assert {
        "parallel_tool_calls",
        "reasoning.effort",
        "reasoning.summary",
        "sampling.temperature",
        "sampling.top_p",
        "max_output_tokens",
        "metadata",
        "include.reasoning.encrypted_content",
        "background",
        "service_tier",
        "user",
        "safety_identifier",
        "store",
        "truncation",
        "text.format.json_schema",
    }.issubset(features)


@pytest.mark.parametrize(
    ("include_value", "expected"),
    [
        (["reasoning.encrypted_content"], {"include.reasoning.encrypted_content"}),
        (["other"], {"include"}),
        (["reasoning.encrypted_content", "other"], {"include"}),
        (["reasoning.encrypted_content", "reasoning.encrypted_content"], {"include"}),
        ([], set()),
        ("reasoning.encrypted_content", set()),
    ],
)
def test_extract_features_include_requires_exact_sentinel_list(
    include_value: Any, expected: set[str]
) -> None:
    request = ResponsesRequest.from_payload(
        {"model": "m", "input": "hi", "include": include_value}
    )
    include_features = {
        feature
        for feature in extract_features(request)
        if feature.startswith("include")
    }
    assert include_features == expected


# --- fast path: 400 per provider x representative unsupported feature ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,feature,payload",
    [
        (
            "claude",
            "tools.file_search",
            {
                "model": "m",
                "input": "hi",
                "tools": [{"type": "file_search"}],
            },
        ),
        (
            "auggie",
            "tools.file_search",
            {
                "model": "m",
                "input": "hi",
                "tools": [{"type": "file_search"}],
            },
        ),
        (
            "deepseek",
            "input.image",
            {
                "model": "m",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,xx",
                            }
                        ],
                    }
                ],
            },
        ),
        (
            "copilot",
            "tools.computer_use",
            {
                "model": "m",
                "input": "hi",
                "tools": [{"type": "computer_use"}],
            },
        ),
    ],
)
async def test_fast_path_unsupported_feature_returns_structured_400(
    provider: str, feature: str, payload: dict[str, Any]
) -> None:
    adapter = _NeverCalledAdapter()
    async with _client(adapter, provider) as client:
        resp = await client.post(f"/{provider}/v1/responses", json=payload)
    assert resp.status_code == 400
    assert resp.json() == _expected_payload(provider, feature)
    assert adapter.create_called == 0
    assert adapter.stream_called == 0


@pytest.mark.asyncio
async def test_fast_path_blocks_streaming_request_before_runner() -> None:
    """A streaming request whose features include unsupported is rejected as 400.

    The gate runs before _stream is called, so the 200 SSE header is never
    sent and the adapter stream_response runner is never invoked.
    """
    adapter = _NeverCalledAdapter()
    async with _client(adapter, "claude") as client:
        resp = await client.post(
            "/claude/v1/responses",
            json={
                "model": "m",
                "input": "hi",
                "stream": True,
                "tools": [{"type": "file_search"}],
            },
        )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == _expected_payload("claude", "tools.file_search")
    assert adapter.stream_called == 0


@pytest.mark.asyncio
async def test_fast_path_allows_supported_feature_payload() -> None:
    """Allowed features must NOT trigger the gate (negative control).

    Without this guard a future change that classifies a common feature as
    unsupported would mask the gate breaking ordinary traffic.
    """

    class _OkAdapter(_NeverCalledAdapter):
        async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
            return ResponseEnvelope(
                id="resp_ok",
                model=request.model,
                output=[
                    {
                        "id": "msg_ok",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "ok", "annotations": []}
                        ],
                    }
                ],
                status="completed",
            )

    adapter = _OkAdapter()
    async with _client(adapter, "claude") as client:
        resp = await client.post(
            "/claude/v1/responses",
            json={"model": "m", "input": "hi", "instructions": "be brief"},
        )
    assert resp.status_code == 200
    assert resp.json()["id"] == "resp_ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "auggie", "deepseek", "kimi"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("include_case", "include_value", "allowed"),
    [
        ("absent", None, True),
        ("empty", [], True),
        ("exact", ["reasoning.encrypted_content"], True),
        ("unknown", ["unknown"], False),
        (
            "mixed",
            ["reasoning.encrypted_content", "unknown"],
            False,
        ),
    ],
)
async def test_affected_provider_include_matrix(
    provider: str,
    stream: bool,
    include_case: str,
    include_value: list[str] | None,
    allowed: bool,
) -> None:
    adapter: _RecordingAdapter | _NeverCalledAdapter
    adapter = _RecordingAdapter() if allowed else _NeverCalledAdapter()
    payload: dict[str, Any] = {"model": "m", "input": "hi", "stream": stream}
    if include_case != "absent":
        payload["include"] = include_value

    async with _client(adapter, provider) as client:
        resp = await client.post(
            f"/{provider}/v1/responses",
            json=payload,
        )

    if allowed:
        assert resp.status_code == 200
        assert isinstance(adapter, _RecordingAdapter)
        requests = adapter.stream_requests if stream else adapter.create_requests
        other_requests = adapter.create_requests if stream else adapter.stream_requests
        assert len(requests) == 1
        assert other_requests == []
        assert "include" not in requests[0].extra
    else:
        assert resp.status_code == 400
        assert resp.json() == _expected_payload(provider, "include")
        assert adapter.create_called == 0
        assert adapter.stream_called == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "include_value",
    [
        ["reasoning.encrypted_content"],
        ["unknown"],
        ["reasoning.encrypted_content", "unknown"],
    ],
)
async def test_copilot_include_matrix_retains_gate_and_normalizer_behavior(
    stream: bool, include_value: list[str]
) -> None:
    adapter = _RecordingAdapter()
    async with _client(adapter, "copilot") as client:
        resp = await client.post(
            "/copilot/v1/responses",
            json={
                "model": "m",
                "input": "hi",
                "stream": stream,
                "include": include_value,
            },
        )

    assert resp.status_code == 200
    requests = adapter.stream_requests if stream else adapter.create_requests
    other_requests = adapter.create_requests if stream else adapter.stream_requests
    assert len(requests) == 1
    assert other_requests == []
    assert "include" not in requests[0].extra


# --- back-stop: adapter raises UnsupportedFeature -----------------------


@pytest.mark.asyncio
async def test_backstop_unsupported_feature_renders_same_400_body() -> None:
    """Adapter raises UnsupportedFeature; the shared builder renders the body.

    The fast path and the back-stop MUST produce byte-identical bodies via
    build_unsupported_payload so clients see one error shape regardless of
    where the gap was detected.
    """
    adapter = _BackstopAdapter(provider="claude", feature="tools.function")
    expected = build_unsupported_payload("claude", "tools.function")

    async with _client(adapter, "claude") as client:
        # Use a request that the fast path does NOT block (string input only),
        # so only the back-stop can fire.
        resp = await client.post(
            "/claude/v1/responses",
            json={"model": "m", "input": "hi"},
        )
    assert resp.status_code == 400
    assert resp.json() == expected
    assert expected == _expected_payload("claude", "tools.function")


@pytest.mark.asyncio
async def test_backstop_unsupported_feature_renders_same_400_body_streaming() -> None:
    """Streaming back-stop before first event also renders the structured 400.

    A stream_response that raises UnsupportedFeature BEFORE emitting any event
    is reported as a normal 400 (not a 200 + response.failed), because the
    response status line is still uncommitted.
    """
    adapter = _BackstopAdapter(provider="auggie", feature="stream.incremental_deltas")
    expected = build_unsupported_payload("auggie", "stream.incremental_deltas")

    async with _client(adapter, "auggie") as client:
        resp = await client.post(
            "/auggie/v1/responses",
            json={"model": "m", "input": "hi", "stream": True},
        )
    assert resp.status_code == 400
    assert resp.json() == expected


def test_research_json_is_well_formed() -> None:
    """Sanity-check the research source so the generator step has a clean input.

    Verifies the values restricted to native/translated/partial/unsupported and
    that providers list matches the table the gate exposes.
    """
    repo_root = Path(__file__).resolve().parents[2]
    research_path = repo_root / ".omc" / "research" / "responses-parity-surface.json"
    payload = json.loads(research_path.read_text(encoding="utf-8"))
    assert payload["providers"] == list(PROVIDERS)
    allowed = {"native", "translated", "partial", "unsupported"}
    for feature, table in payload["features"].items():
        for provider, value in table.items():
            assert value in allowed, (feature, provider, value)
