"""Unit tests for Responses API SSE completion guarding."""
from __future__ import annotations

import asyncio
import json

from reverso.middleware.responses_sse_completion import ResponsesSSECompletionMiddleware
from reverso.proxy.profile_routing import ProfileRoutingMiddleware


def _sse_payloads(sent: list[dict]) -> list[dict]:
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    payloads = []
    for line in body.splitlines():
        if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
            continue
        payloads.append(json.loads(line[6:]))
    return payloads


def test_responses_sse_guard_inserts_completed_before_done() -> None:
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"content-length", b"999"),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.output_text.delta","response_id":"resp_123","delta":"hi"}\n\ndata: [DONE]\n\n',
            "more_body": False,
        })

    middleware = ResponsesSSECompletionMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    payloads = _sse_payloads(sent)
    assert [payload["type"] for payload in payloads] == [
        "response.output_text.delta",
        "response.completed",
    ]
    assert payloads[-1]["response"] == {
        "id": "resp_123",
        "object": "response",
        "status": "completed",
    }
    assert sent[0]["headers"] == [(b"content-type", b"text/event-stream; charset=utf-8")]
    assert sent[1]["body"].find(b'"type":"response.completed"') < sent[1]["body"].find(b"data: [DONE]")


def test_responses_sse_guard_does_not_duplicate_completed() -> None:
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.completed","response":{"id":"resp_done","status":"completed"}}\n\ndata: [DONE]\n\n',
            "more_body": False,
        })

    middleware = ResponsesSSECompletionMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert body.count(b"response.completed") == 1
    assert body.count(b"data: [DONE]") == 1


def test_responses_sse_guard_appends_done_after_existing_completed() -> None:
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.completed","response":{"id":"resp_done","status":"completed"}}\n\n',
            "more_body": False,
        })

    middleware = ResponsesSSECompletionMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert body.count(b"response.completed") == 1
    assert body.endswith(b"data: [DONE]\n\n")


def test_responses_sse_guard_does_not_duplicate_completed_split_across_chunks() -> None:
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.',
            "more_body": True,
        })
        await send({
            "type": "http.response.body",
            "body": b'completed","response":{"id":"resp_done","status":"completed"}}\n\ndata: [DONE]\n\n',
            "more_body": False,
        })

    middleware = ResponsesSSECompletionMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert body.count(b"response.completed") == 1


def test_responses_sse_guard_inserts_completed_before_split_done() -> None:
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.output_text.delta","response_id":"resp_split","delta":"ok"}\n\ndata: [DO',
            "more_body": True,
        })
        await send({
            "type": "http.response.body",
            "body": b"NE]\n\n",
            "more_body": False,
        })

    middleware = ResponsesSSECompletionMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert body.find(b"response.completed") < body.find(b"data: [DONE]")
    assert body.count(b"response.completed") == 1
    assert body.count(b"data: [DONE]") == 1


def test_responses_sse_guard_only_applies_to_responses_streams() -> None:
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b"data: [DONE]\n\n",
            "more_body": False,
        })

    middleware = ResponsesSSECompletionMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/chat/completions"}, receive, send))

    assert sent[1]["body"] == b"data: [DONE]\n\n"


def test_profile_responses_stack_rewrites_model_and_inserts_completed() -> None:
    captured = {}

    async def app(scope, receive, send):
        request = await receive()
        captured["path"] = scope["path"]
        captured["body"] = json.loads(request["body"])
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.output_text.delta","response_id":"resp_profile","delta":"ok"}\n\ndata: [DONE]\n\n',
            "more_body": False,
        })

    stack = ProfileRoutingMiddleware(ResponsesSSECompletionMiddleware(app))
    sent = []

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello","stream":true}',
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    asyncio.run(stack({"type": "http", "method": "POST", "path": "/minimax/v1/responses"}, receive, send))

    assert captured == {
        "path": "/v1/responses",
        "body": {"model": "MiniMax-M3", "input": "hello", "stream": True},
    }
    payloads = _sse_payloads(sent)
    assert payloads[-1]["type"] == "response.completed"


def test_profile_responses_stack_preserves_malformed_body_and_inserts_completed() -> None:
    captured = {}

    async def app(scope, receive, send):
        request = await receive()
        captured["path"] = scope["path"]
        captured["body"] = request["body"]
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({
            "type": "http.response.body",
            "body": b'data: {"type":"response.output_text.delta","response_id":"resp_bad","delta":"ok"}\n\ndata: [DONE]\n\n',
            "more_body": False,
        })

    stack = ProfileRoutingMiddleware(ResponsesSSECompletionMiddleware(app))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{not-json", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(stack({"type": "http", "method": "POST", "path": "/deepseek/v1/responses"}, receive, send))

    assert captured == {"path": "/v1/responses", "body": b"{not-json"}
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert b'"type":"response.completed"' in body
