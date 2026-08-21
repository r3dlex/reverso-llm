"""G2 contracts for Ollama native Anthropic Messages dispatch."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import reverso.protocols.anthropic_app as anthropic_app_module
from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.adapters.ollama.messages import OllamaMessagesClient
from reverso.protocols.anthropic_native import AnthropicNativeAdapter
from reverso.protocols.anthropic_app import AnthropicMessagesApp
from reverso.protocols.anthropic_translate import (
    prepare_anthropic_dispatch,
    project_compressed_request_to_anthropic_payload,
)


@pytest.mark.asyncio
async def test_messages_client_restores_raw_model_and_forwards_native_payload() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "MiXeD:7B",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        messages = OllamaMessagesClient(client, "http://127.0.0.1:11434")
        result = await messages.create(
            {
                "model": "MiXeD:7B",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hello"}],
            }
        )

    assert seen == {
        "path": "/v1/messages",
        "body": (
            b'{"model":"MiXeD:7B","max_tokens":8,'
            b'"messages":[{"role":"user","content":"hello"}]}'
        ),
    }
    assert result["model"] == "MiXeD:7B"


def test_projection_changes_only_addressed_text_and_preserves_structure() -> None:
    payload = {
        "model": "MiXeD:7B",
        "max_tokens": 32,
        "system": [{"type": "text", "text": "system"}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "long prompt"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AA==",
                        },
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [{"type": "text", "text": "tool output"}],
                    },
                ],
            }
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    prepared = prepare_anthropic_dispatch(copy.deepcopy(payload), "ollama")
    compressed_input = copy.deepcopy(prepared.request.input)
    compressed_input[0]["content"][0]["text"] = "short"
    compressed = replace(prepared.request, input=compressed_input)

    projected = project_compressed_request_to_anthropic_payload(prepared, compressed)

    assert projected["messages"][0]["content"][0]["text"] == "short"
    assert (
        projected["messages"][0]["content"][1:] == payload["messages"][0]["content"][1:]
    )
    assert projected["tools"] == payload["tools"]
    assert projected["model"] == "MiXeD:7B"


def test_lossy_projection_fails_open_atomically() -> None:
    payload = {
        "model": "raw:latest",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "a"}]}],
    }
    prepared = prepare_anthropic_dispatch(copy.deepcopy(payload), "ollama")
    lossy = replace(prepared.request, input=[])

    assert project_compressed_request_to_anthropic_payload(prepared, lossy) == payload


def test_ollama_adapter_exposes_internal_native_facet() -> None:
    from reverso.protocols.adapters.ollama.adapter import OllamaAdapter

    assert issubclass(OllamaAdapter, AnthropicNativeAdapter)


def test_g2_verification_wrapper_preserves_exact_eight_commands() -> None:
    commands = [
        line
        for line in Path("tests/verify_ollama_g2.sh")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith(("uv ", "uvx "))
    ]
    assert commands == [
        "uv run pytest tests/unit/test_ollama_messages.py tests/unit/test_anthropic_translate.py tests/unit/test_anthropic_stream.py tests/unit/test_headroom_compression.py -q",
        "uv run pytest tests/unit/test_claude_code_sync.py tests/unit/test_client_convergence_contract.py -q",
        "uv run pytest tests/integration/test_ollama_claude_launcher.py tests/integration/test_anthropic_messages_parity.py tests/integration/test_anthropic_messages_streaming.py -q",
        "uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/integration/test_ollama_codex_profile.py -q",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uvx prek run --all-files",
        "uv run pytest tests/ -v --ignore=tests/integration --tb=short",
    ]


class _NativeAdapter:
    def __init__(self, models: list[str]) -> None:
        self.models = models
        self.native_payloads: list[dict[str, object]] = []

    async def list_models(self) -> ModelList:
        return ModelList(data=[{"id": model} for model in self.models])

    async def create_anthropic_message(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        self.native_payloads.append(copy.deepcopy(payload))
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": payload["model"],
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    async def stream_anthropic_message(self, payload: dict[str, object]):
        self.native_payloads.append(copy.deepcopy(payload))
        yield {
            "type": "message_start",
            "message": {"id": "msg_1", "model": payload["model"]},
        }
        yield {"type": "message_stop"}

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        raise AssertionError("native adapter must not use Responses dispatch")

    async def stream_response(self, request: ResponsesRequest):
        yield SSEEvent(event="unused", data={})

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        raise KeyError(response_id)

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id, data=[])


@pytest.mark.asyncio
async def test_exact_header_bound_alias_is_only_ollama_messages_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVERSO_HEADROOM_ENABLED", "0")
    compressed_models: list[str] = []
    real_compress = anthropic_app_module.compress_responses_request

    async def capture_compress(request: ResponsesRequest, **kwargs: object):
        compressed_models.append(request.model)
        return await real_compress(request, **kwargs)

    monkeypatch.setattr(
        anthropic_app_module, "compress_responses_request", capture_compress
    )
    adapter = _NativeAdapter(["MiXeD.Name:7B"])
    app = AnthropicMessagesApp({"ollama": adapter})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:64946"
    ) as client:
        listing = await client.get(
            "/v1/models", headers={"x-reverso-model-catalog": "ollama"}
        )
        alias = listing.json()["data"][0]["id"]
        good = await client.post(
            "/v1/messages",
            headers={"x-reverso-model-catalog": "ollama"},
            json={
                "model": alias,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        bare = await client.post(
            "/v1/messages",
            json={"model": "MiXeD.Name:7B", "max_tokens": 8, "messages": []},
        )
        qualified = await client.post(
            "/v1/messages",
            json={"model": "ollama/MiXeD.Name:7B", "max_tokens": 8, "messages": []},
        )
        wrong_case = await client.post(
            "/v1/messages",
            headers={"x-reverso-model-catalog": "ollama"},
            json={"model": alias.lower(), "max_tokens": 8, "messages": []},
        )

    assert listing.status_code == 200
    assert alias == "anthropic-ollama-MiXeD.Name:7B"
    assert good.status_code == 200
    assert compressed_models == ["MiXeD.Name:7B"]
    assert adapter.native_payloads[0]["model"] == "MiXeD.Name:7B"
    assert bare.status_code == qualified.status_code == wrong_case.status_code == 404


@pytest.mark.asyncio
async def test_casefold_collision_blocks_entire_ollama_catalog_generation() -> None:
    adapter = _NativeAdapter(["Model:7B", "model:7b"])
    app = AnthropicMessagesApp({"ollama": adapter})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:64946"
    ) as client:
        listing = await client.get(
            "/v1/models", headers={"x-reverso-model-catalog": "ollama"}
        )
        routed = await client.post(
            "/v1/messages",
            headers={"x-reverso-model-catalog": "ollama"},
            json={
                "model": "anthropic-ollama-Model:7B",
                "max_tokens": 8,
                "messages": [],
            },
        )
    assert listing.json()["data"] == []
    assert routed.status_code == 404


@pytest.mark.asyncio
async def test_native_stream_preserves_structured_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVERSO_HEADROOM_ENABLED", "0")
    adapter = _NativeAdapter(["tool.model:latest"])
    app = AnthropicMessagesApp({"ollama": adapter})
    structured = [
        {"type": "text", "text": "inspect"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": [{"type": "text", "text": "done"}],
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:64946"
    ) as client:
        listing = await client.get(
            "/v1/models", headers={"x-reverso-model-catalog": "ollama"}
        )
        alias = listing.json()["data"][0]["id"]
        async with client.stream(
            "POST",
            "/v1/messages",
            headers={"x-reverso-model-catalog": "ollama"},
            json={
                "model": alias,
                "max_tokens": 8,
                "stream": True,
                "messages": [{"role": "user", "content": structured}],
                "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            },
        ) as response:
            wire = await response.aread()
    assert response.status_code == 200
    assert b"event: message_start" in wire and b"event: message_stop" in wire
    assert adapter.native_payloads[0]["messages"][0]["content"] == structured
