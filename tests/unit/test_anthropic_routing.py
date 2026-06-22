"""Unit tests for the Anthropic Messages app routing/dispatch (ADR 0006 D3, G002).

Routing and dispatch are fully implemented in G002; the create handler is a
clearly-marked stub. These tests pin: unknown model -> 404 not_found_error;
/claude/v1/messages -> served by the claude backend (asserted through the
CompositionRoot so it is NOT delegated to the legacy app); mixed-case claude
auto-routes to the claude backend; /deepseek and /copilot prefixes reach the
named backend (stub ok); missing anthropic-version succeeds and echoes the
default; the build accepts a claude adapter (ADR 0008).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_app import (
    AnthropicMessagesApp,
    build_anthropic_app,
    build_anthropic_error,
    route_is_anthropic_surface,
    split_anthropic_path,
)
from reverso.proxy.compose import CompositionRoot


class _StubAdapter:
    """Minimal in-process adapter; never calls a real provider or LiteLLM."""

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        return ResponseEnvelope(id="resp_stub", model=request.model or "x")

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        yield SSEEvent(event="response.completed", data={"type": "response.completed"})

    async def list_models(self) -> ModelList:
        return ModelList(data=[{"id": "x", "object": "model"}])

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="x")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


def _stub_adapters() -> dict[str, Any]:
    return {
        "copilot": _StubAdapter(),
        "deepseek": _StubAdapter(),
        "auggie": _StubAdapter(),
        "claude": _StubAdapter(),
    }


async def _drive(
    app: Any,
    method: str,
    path: str,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[bytes, bytes], dict[str, Any] | None]:
    sent: list[dict[str, Any]] = []
    state = {"done": False}

    async def receive() -> dict[str, Any]:
        if state["done"]:
            return {"type": "http.disconnect"}
        state["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m.get("type") == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in sent if m.get("type") == "http.response.body"
    )
    payload = json.loads(raw) if raw else None
    return int(start["status"]), dict(start["headers"]), payload


# --- pure helpers -----------------------------------------------------------


def test_route_is_anthropic_surface() -> None:
    assert route_is_anthropic_surface("/v1/messages")
    assert route_is_anthropic_surface("/deepseek/v1/messages")
    assert route_is_anthropic_surface("/copilot/v1/messages")
    assert route_is_anthropic_surface("/auggie/v1/messages")
    assert route_is_anthropic_surface("/claude/v1/messages")
    # Mixed-case profile segments are normalized and claimed (MINOR-1).
    assert route_is_anthropic_surface("/CLAUDE/v1/messages")
    assert route_is_anthropic_surface("/Claude/v1/messages")
    assert route_is_anthropic_surface("/DEEPSEEK/v1/messages")
    # G006 auxiliary paths are claimed by the Anthropic surface.
    assert route_is_anthropic_surface("/v1/messages/count_tokens")
    assert route_is_anthropic_surface("/deepseek/v1/messages/count_tokens")
    # The BARE /v1/models is claimed by the Anthropic surface (ADR 0006 G006).
    assert route_is_anthropic_surface("/v1/models")
    # Responses paths are NOT claimed by the Anthropic surface. The per-provider
    # /<provider>/v1/models listing stays with the Responses gateway: only the
    # bare /v1/models is claimed here.
    assert not route_is_anthropic_surface("/v1/responses")
    assert not route_is_anthropic_surface("/deepseek/v1/models")
    assert not route_is_anthropic_surface("/deepseek/v1/responses")


def test_split_anthropic_path() -> None:
    bare = split_anthropic_path("/v1/messages")
    assert bare is not None and bare.profile is None
    pinned = split_anthropic_path("/deepseek/v1/messages")
    assert pinned is not None and pinned.profile == "deepseek"
    assert split_anthropic_path("/v1/responses") is None


def test_build_anthropic_error_shape() -> None:
    env = build_anthropic_error("not_found_error", "nope")
    assert env == {
        "type": "error",
        "error": {"type": "not_found_error", "message": "nope"},
    }


# --- app dispatch ------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_non_claude_model_returns_404_not_found() -> None:
    app = build_anthropic_app(_stub_adapters())
    status, _headers, body = await _drive(
        app, "POST", "/v1/messages", b'{"model":"totally-unknown"}'
    )
    assert status == 404
    assert body is not None
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_mixed_case_claude_model_auto_routes_to_claude_backend() -> None:
    app = build_anthropic_app(_stub_adapters())
    status, _headers, body = await _drive(
        app, "POST", "/v1/messages", b'{"model":"Claude-Opus"}'
    )
    # A claude model now resolves to the claude backend (ADR 0008): the stub
    # adapter returns a valid (empty-content) Anthropic message body, not a 404.
    assert status == 200
    assert body is not None
    assert body["type"] == "message"
    assert body["role"] == "assistant"


@pytest.mark.asyncio
async def test_deepseek_model_auto_routes_to_backend() -> None:
    app = build_anthropic_app(_stub_adapters())
    status, _headers, body = await _drive(
        app, "POST", "/v1/messages", b'{"model":"deepseek-v4-pro"}'
    )
    # Resolved backend reaches the real G003 translation, not a 404: the stub
    # adapter returns a valid (empty-content) Anthropic message body.
    assert status == 200
    assert body is not None
    assert body["type"] == "message"
    assert body["role"] == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["deepseek", "copilot"])
async def test_profile_prefix_reaches_named_backend(profile: str) -> None:
    app = build_anthropic_app(_stub_adapters())
    status, _headers, body = await _drive(app, "POST", f"/{profile}/v1/messages", b"{}")
    # Per-profile prefixes pin the backend and bypass model resolution; the real
    # G003 translation runs and yields a valid Anthropic message body.
    assert status == 200
    assert body is not None
    assert body["type"] == "message"
    assert body["role"] == "assistant"


@pytest.mark.asyncio
async def test_missing_anthropic_version_defaults_and_echoes() -> None:
    app = build_anthropic_app(_stub_adapters())
    status, headers, _body = await _drive(
        app, "POST", "/v1/messages", b'{"model":"deepseek-v4-pro"}'
    )
    # No anthropic-version header sent -> never a 400; default echoed.
    assert status == 200
    assert headers.get(b"anthropic-version") == b"2023-06-01"


@pytest.mark.asyncio
async def test_explicit_anthropic_version_is_echoed() -> None:
    app = build_anthropic_app(_stub_adapters())
    _status, headers, _body = await _drive(
        app,
        "POST",
        "/v1/messages",
        b'{"model":"deepseek-v4-pro"}',
        headers=[(b"anthropic-version", b"2099-01-01")],
    )
    assert headers.get(b"anthropic-version") == b"2099-01-01"


def test_build_accepts_claude_adapter() -> None:
    # claude is now a permitted Anthropic-surface backend (ADR 0008).
    app = AnthropicMessagesApp({"claude": _StubAdapter()})
    assert "claude" in app._adapters


def test_build_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        AnthropicMessagesApp({"mystery": _StubAdapter()})


# --- through the CompositionRoot (claude served first-party, not delegated) --


@pytest.mark.asyncio
async def test_uppercase_claude_prefix_claimed_and_served_first_party() -> None:
    """/CLAUDE/v1/messages is claimed and served by the Anthropic surface (MINOR-1).

    Profile segments are normalized to lowercase before the prefix membership
    check, so /CLAUDE and /Claude are treated identically to /claude and reach
    the claude backend (ADR 0008), never the legacy app.
    """
    legacy_calls: list[str] = []

    async def _legacy_tripwire(scope: Any, receive: Any, send: Any) -> None:
        legacy_calls.append(str(scope.get("path", "")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    root = CompositionRoot(
        anthropic_app=build_anthropic_app(_stub_adapters()),
        legacy_app=_legacy_tripwire,
    )
    for path in ("/CLAUDE/v1/messages", "/Claude/v1/messages"):
        status, _headers, body = await _drive(
            root, "POST", path, b'{"model":"claude-opus"}'
        )
        assert status == 200, f"{path} must be served (200)"
        assert body is not None
        assert body["type"] == "message"
        assert body["role"] == "assistant"
    assert legacy_calls == [], (
        "mixed-case /CLAUDE/v1/messages must not reach the legacy app; "
        f"observed {legacy_calls!r}"
    )


@pytest.mark.asyncio
async def test_claude_prefix_served_first_party_not_delegated_to_legacy() -> None:
    """POST /claude/v1/messages -> served by the claude backend, never legacy."""
    legacy_calls: list[str] = []

    async def _legacy_tripwire(scope: Any, receive: Any, send: Any) -> None:
        legacy_calls.append(str(scope.get("path", "")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    root = CompositionRoot(
        anthropic_app=build_anthropic_app(_stub_adapters()),
        legacy_app=_legacy_tripwire,
    )
    status, _headers, body = await _drive(
        root, "POST", "/claude/v1/messages", b'{"model":"claude-opus"}'
    )
    assert status == 200
    assert body is not None
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert legacy_calls == [], (
        "/claude/v1/messages must be answered by the Anthropic app, never "
        f"delegated to the legacy LiteLLM app; observed {legacy_calls!r}"
    )
