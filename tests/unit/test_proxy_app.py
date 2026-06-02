"""Unit tests for the Reverso proxy ASGI wrapper."""
from __future__ import annotations

import importlib
import json
import os

import litellm
import pytest

from reverso.middleware.x_gateway_callback import success_callback


def test_proxy_app_imports_with_config_file_path() -> None:
    os.environ["CONFIG_FILE_PATH"] = "config/litellm_config.yaml"

    module = importlib.import_module("reverso.proxy.app")

    assert module.app is not None
    assert success_callback in litellm.success_callback
    assert "anthropic_cli" in litellm.provider_list
    assert "openai_cli" in litellm.provider_list


@pytest.mark.asyncio
async def test_proxy_app_lifespan_starts_with_config_file_path() -> None:
    os.environ["CONFIG_FILE_PATH"] = "config/litellm_config.yaml"
    module = importlib.import_module("reverso.proxy.app")
    events = iter([
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ])
    sent: list[dict] = []

    async def receive() -> dict:
        return next(events)

    async def send(message: dict) -> None:
        sent.append(message)

    await module.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    assert sent[0]["type"] == "lifespan.startup.complete"
    assert sent[1]["type"] == "lifespan.shutdown.complete"


@pytest.mark.asyncio
async def test_profile_error_response_includes_x_gateway() -> None:
    os.environ["CONFIG_FILE_PATH"] = "config/litellm_config.yaml"
    module = importlib.import_module("reverso.proxy.app")
    sent: list[dict] = []

    async def receive() -> dict:
        return {
            "type": "http.request",
            "body": b'{"model":"not-a-model","messages":[{"role":"user","content":"x"}]}',
            "more_body": False,
        }

    async def send(message: dict) -> None:
        sent.append(message)

    await module.app(
        {
            "type": "http",
            "method": "POST",
            "path": "/deepseek/v1/chat/completions",
            "raw_path": b"/deepseek/v1/chat/completions",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1),
            "scheme": "http",
            "asgi": {"version": "3.0"},
        },
        receive,
        send,
    )

    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    payload = json.loads(body)

    assert sent[0]["status"] == 400
    assert payload["error"]
    assert payload["x_gateway"] == {
        "session_id": None,
        "observations": [],
        "provider": "deepseek",
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_direct_model_error_response_includes_x_gateway_provider() -> None:
    os.environ["CONFIG_FILE_PATH"] = "config/litellm_config.yaml"
    module = importlib.import_module("reverso.proxy.app")
    sent: list[dict] = []

    async def receive() -> dict:
        return {
            "type": "http.request",
            "body": b'{"model":"deepseek-chat","messages":[{"role":"user","content":"x"}]}',
            "more_body": False,
        }

    async def send(message: dict) -> None:
        sent.append(message)

    await module.app(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1),
            "scheme": "http",
            "asgi": {"version": "3.0"},
        },
        receive,
        send,
    )

    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    payload = json.loads(body)

    assert sent[0]["status"] >= 400
    assert payload["x_gateway"]["provider"] == "deepseek"
