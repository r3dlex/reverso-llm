"""Unit tests for Codex-compatible model list responses."""

from __future__ import annotations

import asyncio
import json

from reverso.middleware.codex_models_compat import CodexModelsCompatMiddleware


def test_codex_models_compat_adds_models_field() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", b"999"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"data":[{"id":"gpt-5.5","object":"model"}]}',
                "more_body": False,
            }
        )

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        CodexModelsCompatMiddleware(app)(
            {
                "type": "http",
                "path": "/deepseek/v1/models",
                "query_string": b"client_version=0.136.0",
            },
            receive,
            send,
        )
    )

    payload = json.loads(sent[1]["body"])
    assert payload["data"][0]["id"] == "gpt-5.5"
    assert payload["models"] == []
    assert sent[0]["headers"][-1] == (
        b"content-length",
        str(len(sent[1]["body"])).encode("ascii"),
    )


def test_codex_models_compat_leaves_non_codex_models_response_unchanged() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"data":[{"id":"gpt-5.5","object":"model"}]}',
                "more_body": False,
            }
        )

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        CodexModelsCompatMiddleware(app)(
            {"type": "http", "path": "/v1/models", "query_string": b""}, receive, send
        )
    )

    payload = json.loads(sent[1]["body"])
    assert "models" not in payload
