"""B4 contract: OpenCode-shaped Messages requests traverse Headroom compression.

OpenCode fragments bind the ``reverso`` provider to the gateway root, so every
selector a generated fragment emits rides POST /v1/messages. These tests pin
that each selector family reaches ``compress_responses_request`` on the
Anthropic Messages surface, and a negative control proves the probe would
detect a bypass.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_app import build_anthropic_app


class _StubAdapter:
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
        "codex": _StubAdapter(),
        "kimi": _StubAdapter(),
    }


OPENCODE_SELECTORS = [
    "claude-sonnet-4-6",
    "deepseek-v4-pro",
    "kimi-k3",
    "anthropic-copilot-gpt-5.5",
    "anthropic-codex-gpt-5.5",
]


async def _drive(
    app: Any,
    body: bytes,
) -> tuple[int, dict[str, Any] | None]:
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
        "method": "POST",
        "path": "/v1/messages",
        "headers": [(b"anthropic-version", b"2023-06-01")],
        "query_string": b"",
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m.get("type") == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in sent if m.get("type") == "http.response.body"
    )
    payload = json.loads(raw) if raw else None
    return int(start["status"]), payload


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", OPENCODE_SELECTORS)
async def test_opencode_selector_traverses_headroom_on_messages(
    selector: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.protocols import anthropic_app

    calls: list[dict[str, Any]] = []
    real_compress = anthropic_app.compress_responses_request

    async def spying_compress(request: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await real_compress(request, **kwargs)

    monkeypatch.setattr(anthropic_app, "compress_responses_request", spying_compress)
    app = build_anthropic_app(_stub_adapters())
    body = json.dumps(
        {
            "model": selector,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "word " * 400}],
        }
    ).encode("utf-8")
    status, payload = await _drive(app, body)
    assert status == 200
    assert payload is not None and payload["type"] == "message"
    assert len(calls) == 1
    assert calls[0]["surface"] == "anthropic_messages"


@pytest.mark.asyncio
async def test_headroom_bypass_is_detectable_negative_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.protocols import anthropic_app

    async def poisoned_compress(request: Any, **kwargs: Any) -> Any:
        raise AssertionError("headroom compression was bypassed on the path")

    monkeypatch.setattr(anthropic_app, "compress_responses_request", poisoned_compress)
    app = build_anthropic_app(_stub_adapters())
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "probe"}],
        }
    ).encode("utf-8")
    with pytest.raises(AssertionError, match="bypassed"):
        await _drive(app, body)
