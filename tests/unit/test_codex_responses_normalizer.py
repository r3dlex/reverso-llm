"""Tests for Codex Responses payload normalization."""

from __future__ import annotations

import asyncio
import json

from reverso.middleware.codex_responses_normalizer import (
    CodexResponsesNormalizerMiddleware,
    normalize_codex_responses_payload,
)


def test_normalize_drops_codex_custom_tools_when_no_function_tools() -> None:
    payload = {
        "model": "gpt-5.5",
        "input": "hello",
        "tools": [{"type": "custom", "name": "shell"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": "medium"},
    }

    normalized = normalize_codex_responses_payload(payload)

    assert normalized == {"model": "gpt-5.5", "input": "hello"}


def test_normalize_keeps_function_tools_and_drops_custom_tools() -> None:
    payload = {
        "model": "gpt-5.5",
        "input": "hello",
        "tools": [
            {"type": "custom", "name": "shell"},
            {"type": "function", "name": "lookup", "parameters": {}},
        ],
    }

    normalized = normalize_codex_responses_payload(payload)

    assert normalized["tools"] == [
        {"type": "function", "name": "lookup", "parameters": {}}
    ]


def test_normalize_drops_tool_choice_for_removed_custom_tool() -> None:
    payload = {
        "model": "gpt-5.5",
        "input": "hello",
        "tools": [
            {"type": "custom", "name": "shell"},
            {"type": "function", "name": "lookup", "parameters": {}},
        ],
        "tool_choice": {"type": "custom", "name": "shell"},
    }

    normalized = normalize_codex_responses_payload(payload)

    assert normalized == {
        "model": "gpt-5.5",
        "input": "hello",
        "tools": [{"type": "function", "name": "lookup", "parameters": {}}],
    }


def test_normalize_keeps_tool_choice_for_retained_function_tool() -> None:
    payload = {
        "model": "gpt-5.5",
        "input": "hello",
        "tools": [{"type": "function", "name": "lookup", "parameters": {}}],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    }

    normalized = normalize_codex_responses_payload(payload)

    assert normalized["tool_choice"] == {
        "type": "function",
        "function": {"name": "lookup"},
    }


def test_normalizer_middleware_applies_before_profile_routing() -> None:
    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope["path"]
        request = await receive()
        captured["body"] = json.loads(request["body"])

    middleware = CodexResponsesNormalizerMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello","tools":[{"type":"custom","name":"shell"}],"tool_choice":"auto"}',
            "more_body": False,
        }

    async def send(message):
        return None

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/deepseek/v1/responses"},
            receive,
            send,
        )
    )

    assert captured == {
        "path": "/deepseek/v1/responses",
        "body": {"model": "gpt-5.5", "input": "hello"},
    }
