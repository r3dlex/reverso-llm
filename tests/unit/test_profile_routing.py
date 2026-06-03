"""Unit tests for Codex profile-path model routing."""
from __future__ import annotations

import asyncio
import json

import pytest

from reverso.middleware.x_gateway_error_envelope import XGatewayErrorEnvelopeMiddleware
from reverso.proxy.profile_routing import (
    CURRENT_PROFILE_WORKSPACE,
    ProfileRoutingMiddleware,
    resolve_profile_model,
    split_profile_path,
)


def test_resolve_minimax_profile_models_to_m3() -> None:
    assert resolve_profile_model("minimax", "gpt-5.5") == "MiniMax-M3"
    assert resolve_profile_model("minimax", "gpt-5.4-mini") == "MiniMax-M3"
    assert resolve_profile_model("minimax", "gpt-5.3-codex-spark") == "MiniMax-M3"


def test_resolve_deepseek_profile_models_by_tier() -> None:
    assert resolve_profile_model("deepseek", "gpt-5.5") == "deepseek-v4-pro"
    assert resolve_profile_model("deepseek", "gpt-5.4") == "deepseek-v4-pro"
    assert resolve_profile_model("deepseek", "gpt-5.4-mini") == "deepseek-v4-flash"
    assert resolve_profile_model("deepseek", "gpt-5.3-codex-spark") == "deepseek-v4-flash"


def test_resolve_claude_profile_models_by_tier() -> None:
    assert resolve_profile_model("claude", "gpt-5.5") == "claude-opus-4-8"
    assert resolve_profile_model("claude", "gpt-5.4") == "claude-opus-4-8"
    assert resolve_profile_model("claude", "custom/gpt-5.5") == "claude-opus-4-8"
    assert resolve_profile_model("claude", "custom/gpt-5.4") == "claude-opus-4-8"
    assert resolve_profile_model("claude", "gpt-5.4-mini") == "claude-sonnet-4-6"
    assert resolve_profile_model("claude", "gpt-5.3-codex-spark") == "claude-sonnet-4-6"
    assert resolve_profile_model("claude", "gpt-5.3-codex-spark") != "claude-haiku-4-6"
    assert resolve_profile_model("claude", "gpt-5.5") != "claude-opus-4-7"
    assert resolve_profile_model("claude", "gpt-5.4") != "claude-opus-4-7"


@pytest.mark.parametrize(
    ("profile", "model", "expected"),
    [
        ("minimax", "gpt-5.5", "MiniMax-M3"),
        ("minimax", "gpt-5.4", "MiniMax-M3"),
        ("minimax", "gpt-5.4-mini", "MiniMax-M3"),
        ("minimax", "gpt-5.3-codex-spark", "MiniMax-M3"),
        ("deepseek", "gpt-5.5", "deepseek-v4-pro"),
        ("deepseek", "gpt-5.4", "deepseek-v4-pro"),
        ("deepseek", "gpt-5.4-mini", "deepseek-v4-flash"),
        ("deepseek", "gpt-5.3-codex-spark", "deepseek-v4-flash"),
        ("claude", "gpt-5.5", "claude-opus-4-8"),
        ("claude", "gpt-5.4", "claude-opus-4-8"),
        ("claude", "gpt-5.4-mini", "claude-sonnet-4-6"),
        ("claude", "gpt-5.3-codex-spark", "claude-sonnet-4-6"),
    ],
)
def test_requested_provider_profile_mapping_matrix(profile: str, model: str, expected: str) -> None:
    assert resolve_profile_model(profile, model) == expected


def test_split_profile_path() -> None:
    path = split_profile_path("/deepseek/v1/responses")
    assert path is not None
    assert path.profile == "deepseek"
    assert path.rewritten_path == "/v1/responses"
    assert split_profile_path("/v1/responses") is None


def test_profile_middleware_rewrites_path_and_model() -> None:
    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope["path"]
        message = await receive()
        captured["body"] = json.loads(message["body"])

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello"}',
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware({"type": "http", "method": "POST", "path": "/deepseek/v1/responses"}, receive, send))

    assert captured == {
        "path": "/v1/responses",
        "body": {"model": "deepseek-v4-pro", "input": "hello"},
    }


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4", "custom/gpt-5.5", "custom/gpt-5.4"])
def test_claude_profile_middleware_rewrites_frontier_models_to_opus_4_8(model: str) -> None:
    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope["path"]
        message = await receive()
        captured["body"] = json.loads(message["body"])

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps({"model": model, "input": "hello"}).encode("utf-8"),
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware({"type": "http", "method": "POST", "path": "/claude/v1/responses"}, receive, send))

    assert captured == {
        "path": "/v1/responses",
        "body": {"model": "claude-opus-4-8", "input": "hello"},
    }


def test_claude_profile_injects_single_codex_workspace_metadata() -> None:
    captured = {}
    metadata = {
        "workspaces": {
            "/Users/andreburgstahler/Ws/Rib": {
                "latest_git_commit_hash": "abc123",
            },
        },
    }

    async def app(scope, receive, send):
        message = await receive()
        captured["body"] = json.loads(message["body"])
        captured["context_workspace"] = CURRENT_PROFILE_WORKSPACE.get()

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello"}',
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/claude/v1/responses",
            "headers": [(b"x-codex-turn-metadata", json.dumps(metadata).encode("utf-8"))],
        },
        receive,
        send,
    ))

    assert captured["body"] == {
        "model": "claude-opus-4-8",
        "input": "hello",
        "x_gateway": {"workspace": "/Users/andreburgstahler/Ws/Rib"},
    }
    assert captured["context_workspace"] == "/Users/andreburgstahler/Ws/Rib"
    assert CURRENT_PROFILE_WORKSPACE.get() is None


def test_profile_routing_preserves_explicit_x_gateway_workspace() -> None:
    captured = {}
    metadata = {"workspaces": {"/Users/andreburgstahler/Ws/Rib": {}}}

    async def app(scope, receive, send):
        message = await receive()
        captured["body"] = json.loads(message["body"])

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello","x_gateway":{"workspace":"/tmp/explicit"}}',
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/claude/v1/responses",
            "headers": [(b"x-codex-turn-metadata", json.dumps(metadata).encode("utf-8"))],
        },
        receive,
        send,
    ))

    assert captured["body"]["x_gateway"]["workspace"] == "/tmp/explicit"




def test_non_claude_profiles_do_not_forward_x_gateway_to_custom_openai() -> None:
    captured = {}
    metadata = {"workspaces": {"/Users/andreburgstahler/Ws/Rib": {}}}

    async def app(scope, receive, send):
        message = await receive()
        captured["body"] = json.loads(message["body"])
        captured["context_workspace"] = CURRENT_PROFILE_WORKSPACE.get()

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello","x_gateway":{"workspace":"/tmp/explicit"}}',
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/minimax/v1/responses",
            "headers": [(b"x-codex-turn-metadata", json.dumps(metadata).encode("utf-8"))],
        },
        receive,
        send,
    ))

    assert captured["body"] == {"model": "MiniMax-M3", "input": "hello"}
    assert captured["context_workspace"] == "/tmp/explicit"
    assert CURRENT_PROFILE_WORKSPACE.get() is None


def test_profile_routing_ignores_malformed_codex_workspace_metadata() -> None:
    captured = {}

    async def app(scope, receive, send):
        message = await receive()
        captured["body"] = json.loads(message["body"])

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello"}',
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/claude/v1/responses",
            "headers": [(b"x-codex-turn-metadata", b"{not-json")],
        },
        receive,
        send,
    ))

    assert captured["body"] == {"model": "claude-opus-4-8", "input": "hello"}


def test_profile_middleware_post_body_receive_waits_for_client_event() -> None:
    captured = {}
    release = asyncio.Event()

    async def app(scope, receive, send):
        first = await receive()
        captured["first"] = json.loads(first["body"])
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(receive(), timeout=0.01)
        release.set()
        captured["second"] = await receive()

    middleware = ProfileRoutingMiddleware(app)
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": b'{"model":"gpt-5.5","input":"hello"}',
                "more_body": False,
            }
        await release.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    asyncio.run(middleware({"type": "http", "method": "POST", "path": "/deepseek/v1/responses"}, receive, send))

    assert captured == {
        "first": {"model": "deepseek-v4-pro", "input": "hello"},
        "second": {"type": "http.disconnect"},
    }


@pytest.mark.parametrize(
    ("path", "expected_model"),
    [
        ("/minimax/v1/chat/completions", "MiniMax-M3"),
        ("/deepseek/v1/chat/completions", "deepseek-v4-pro"),
        ("/claude/v1/chat/completions", "claude-opus-4-8"),
    ],
)
def test_profile_and_error_middlewares_keep_stream_open_until_completion(path: str, expected_model: str) -> None:
    release = asyncio.Event()
    captured = {}
    sent = []

    async def streaming_app(scope, receive, send):
        first = await receive()
        captured["model"] = json.loads(first["body"])["model"]
        disconnect_task = asyncio.create_task(receive())
        await asyncio.sleep(0.01)
        captured["disconnect_before_stream_done"] = disconnect_task.done()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })
        await send({"type": "http.response.body", "body": b"data: one\\n\\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"data: [DONE]\\n\\n", "more_body": False})
        release.set()
        captured["disconnect_after_stream_done"] = await disconnect_task

    app = XGatewayErrorEnvelopeMiddleware(ProfileRoutingMiddleware(streaming_app))
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": b'{"model":"gpt-5.5","messages":[{"role":"user","content":"hello"}],"stream":true}',
                "more_body": False,
            }
        await release.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "method": "POST", "path": path}, receive, send))

    assert captured == {
        "model": expected_model,
        "disconnect_before_stream_done": False,
        "disconnect_after_stream_done": {"type": "http.disconnect"},
    }
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]


def test_profile_middleware_replays_disconnect_without_looping() -> None:
    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope["path"]
        captured["message"] = await receive()

    middleware = ProfileRoutingMiddleware(app)
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    asyncio.run(middleware({"type": "http", "method": "POST", "path": "/minimax/v1/responses"}, receive, send))

    assert calls == 1
    assert captured == {
        "path": "/v1/responses",
        "message": {"type": "http.disconnect"},
    }


def test_profile_routing_targets_exist_in_configs() -> None:
    import yaml

    with open("config/litellm_config.yaml") as f:
        runtime_config = yaml.safe_load(f)
    with open("config/models.yaml") as f:
        registry_config = yaml.safe_load(f)

    runtime_names = {entry["model_name"] for entry in runtime_config["model_list"]}
    registry_names = {entry["model_name"] for entry in registry_config["model_list"]}
    routed_models = {
        resolve_profile_model(profile, model)
        for profile in ("minimax", "deepseek", "claude")
        for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark", "gpt-4.1")
    }

    assert routed_models <= runtime_names
    assert routed_models <= registry_names


def test_claude_profile_active_configs_do_not_expose_opus_4_7() -> None:
    import yaml

    with open("config/litellm_config.yaml") as f:
        runtime_config = yaml.safe_load(f)
    with open("config/models.yaml") as f:
        registry_config = yaml.safe_load(f)

    active_model_values = json.dumps({
        "runtime": runtime_config["model_list"],
        "registry": registry_config["model_list"],
    })

    assert "claude-opus-4-8" in active_model_values
    assert "claude-opus-4-7" not in active_model_values


def test_direct_codex_route_keeps_gpt_model_names() -> None:
    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope["path"]
        message = await receive()
        captured["body"] = json.loads(message["body"])

    middleware = ProfileRoutingMiddleware(app)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-5.5","input":"hello"}',
            "more_body": False,
        }

    async def send(_message):
        return None

    asyncio.run(middleware({"type": "http", "method": "POST", "path": "/v1/responses"}, receive, send))

    assert captured == {
        "path": "/v1/responses",
        "body": {"model": "gpt-5.5", "input": "hello"},
    }
