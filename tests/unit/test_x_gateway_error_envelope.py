"""Unit tests for x_gateway error response envelope middleware."""

from __future__ import annotations

import asyncio
import json

import pytest

from reverso.middleware.x_gateway_error_envelope import XGatewayErrorEnvelopeMiddleware


def test_error_envelope_adds_profile_provider_to_json_errors() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b'{"error":{"message":"bad model"}}'}
        )

    middleware = XGatewayErrorEnvelopeMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "path": "/deepseek/v1/chat/completions"}, receive, send
        )
    )

    body = json.loads(sent[1]["body"])
    assert body["error"]["message"] == "bad model"
    assert body["x_gateway"] == {
        "session_id": None,
        "observations": [],
        "provider": "deepseek",
        "warnings": [],
    }
    assert (b"content-length", str(len(sent[1]["body"])).encode("ascii")) in sent[0][
        "headers"
    ]


def test_error_envelope_leaves_success_response_unmodified() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    middleware = XGatewayErrorEnvelopeMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "path": "/deepseek/v1/chat/completions"}, receive, send
        )
    )

    assert sent[1]["body"] == b'{"ok":true}'


def test_error_envelope_post_body_receive_waits_for_client_event() -> None:
    captured = {}
    release = asyncio.Event()

    async def app(scope, receive, send):
        first = await receive()
        captured["first"] = json.loads(first["body"])
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(receive(), timeout=0.01)
        release.set()
        captured["second"] = await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    middleware = XGatewayErrorEnvelopeMiddleware(app)
    sent = []
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": b'{"model":"deepseek-chat"}',
                "more_body": False,
            }
        await release.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "path": "/deepseek/v1/chat/completions"}, receive, send
        )
    )

    assert captured == {
        "first": {"model": "deepseek-chat"},
        "second": {"type": "http.disconnect"},
    }
    assert sent[1]["body"] == b'{"ok":true}'


def test_error_envelope_canonicalizes_claude_profile_to_anthropic() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b'{"error":{"message":"bad model"}}'}
        )

    middleware = XGatewayErrorEnvelopeMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware({"type": "http", "path": "/claude/v1/messages"}, receive, send)
    )

    body = json.loads(sent[1]["body"])
    assert body["x_gateway"]["provider"] == "anthropic"


def test_error_envelope_infers_direct_deepseek_model_from_request_body() -> None:
    async def app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b'{"error":{"message":"bad model"}}'}
        )

    middleware = XGatewayErrorEnvelopeMiddleware(app)
    sent = []

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"deepseek-chat"}',
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware({"type": "http", "path": "/v1/chat/completions"}, receive, send)
    )

    body = json.loads(sent[1]["body"])
    assert body["x_gateway"]["provider"] == "deepseek"


def test_error_envelope_no_longer_infers_direct_minimax_model_from_request_body() -> (
    None
):
    async def app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b'{"error":{"message":"bad model"}}'}
        )

    middleware = XGatewayErrorEnvelopeMiddleware(app)
    sent = []

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"MiniMax-M3"}',
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware({"type": "http", "path": "/v1/chat/completions"}, receive, send)
    )

    body = json.loads(sent[1]["body"])
    assert body["x_gateway"]["provider"] == "unknown"
