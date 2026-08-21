from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters import ollama as ollama_module
from reverso.protocols.adapters.ollama import build_ollama_runtime, validate_endpoint
from reverso.protocols.adapters.ollama.auth import OllamaAuthState
from reverso.protocols.anthropic_app import AnthropicMessagesApp
from reverso.protocols.responses_app import ResponsesGatewayApp


def _rich_request(*, stream: bool = False) -> ResponsesRequest:
    return ResponsesRequest(
        model="llava:latest",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AAAA",
                    },
                ],
            }
        ],
        stream=stream,
        tools=[
            {
                "type": "function",
                "name": "lookup",
                "description": "Lookup a value",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            }
        ],
        tool_choice={"type": "function", "name": "lookup"},
        instructions="Use the image and tool.",
        extra={"metadata": {"case": "transport"}},
    )


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_ollama_no_cloud_truthy_values_are_absolute_opt_out(value: str) -> None:
    state = OllamaAuthState.from_env(
        {"REVERSO_OLLAMA_CLOUD": "1", "OLLAMA_NO_CLOUD": value}
    )

    assert state.cloud_requested is False
    assert state.cloud_status == "disabled"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_ollama_no_cloud_empty_and_false_values_do_not_disable(value: str) -> None:
    state = OllamaAuthState.from_env({"OLLAMA_NO_CLOUD": value})

    assert state.cloud_requested is True
    assert state.cloud_status == "unavailable"


@pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
def test_reverso_cloud_false_values_disable_cloud(value: str) -> None:
    state = OllamaAuthState.from_env(
        {"REVERSO_OLLAMA_CLOUD": value, "OLLAMA_NO_CLOUD": "0"}
    )

    assert state.cloud_requested is False
    assert state.cloud_status == "disabled"


@pytest.mark.asyncio
async def test_runtime_factory_failure_closes_owned_client_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[httpx.AsyncClient] = []
    async_client = httpx.AsyncClient

    def client_factory(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        client = async_client()
        clients.append(client)
        return client

    def fail_catalog(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("catalog construction failed")

    monkeypatch.setattr(ollama_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(ollama_module, "OllamaCatalog", fail_catalog)

    with pytest.raises(RuntimeError, match="catalog construction failed"):
        build_ollama_runtime()

    assert len(clients) == 1
    assert clients[0].is_closed is True


def _transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3:8b"},
                    {"model": "gpt-oss:20b"},
                    {"name": "deepseek-v3.1:671b-cloud"},
                ]
            },
        )
    if request.url.path == "/v1/responses":
        return httpx.Response(
            200,
            json={
                "id": "resp_ollama",
                "object": "response",
                "status": "completed",
                "model": "qwen3:8b",
                "output": [{"type": "message", "role": "assistant", "content": []}],
            },
        )
    raise AssertionError(request.url)


@pytest.mark.asyncio
async def test_tags_discovers_every_validated_row_as_a_local_raw_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVERSO_OLLAMA_CLOUD", raising=False)
    monkeypatch.delenv("OLLAMA_NO_CLOUD", raising=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_transport))
    runtime = build_ollama_runtime(client=client)

    models = await runtime.adapter.list_models()

    assert [row["id"] for row in models.data] == [
        "qwen3:8b",
        "gpt-oss:20b",
        "deepseek-v3.1:671b-cloud",
    ]
    assert runtime.catalog.entries["qwen3:8b"].local is True
    assert runtime.catalog.entries["qwen3:8b"].cloud is False
    suffix_looking = runtime.catalog.entries["deepseek-v3.1:671b-cloud"]
    assert suffix_looking.local is True
    assert suffix_looking.cloud is False
    assert runtime.auth.cloud_status == "unavailable"
    await runtime.close()
    await runtime.close()


@pytest.mark.asyncio
async def test_model_listing_uses_marker_owned_shared_inventory(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "ollama-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "reverso-client-sync/provider-ollama",
                "observed_at": "2026-08-21T08:00:00+00:00",
                "freshness": "partial",
                "auth_status": "required",
                "cloud_status": "auth_required",
                "entries": [
                    {
                        "raw_id": "local-a",
                        "local": True,
                        "cloud": False,
                        "stale": False,
                    },
                    {
                        "raw_id": "local-stale-cloud",
                        "local": True,
                        "cloud": True,
                        "stale": True,
                    },
                    {"raw_id": "cloud-a", "local": False, "cloud": True, "stale": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    def no_live_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"model publication must not rediscover {request.url}")

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(no_live_request)),
        inventory_path=inventory,
    )

    models = await runtime.adapter.list_anthropic_models()

    assert [(row["id"], row["ollama_stale"]) for row in models.data] == [
        ("local-a", False),
        ("local-stale-cloud", True),
    ]
    assert models.discovery_source == "ollama-inventory-auth_required"
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ("responses", "messages"))
@pytest.mark.parametrize(
    ("auth_status", "cloud_status", "expected_code"),
    (
        ("required", "auth_required", "auth_required"),
        ("failed", "timeout", "model_not_current"),
    ),
)
async def test_protocol_surface_rejects_stale_cloud_before_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    auth_status: str,
    cloud_status: str,
    expected_code: str,
) -> None:
    monkeypatch.setenv("REVERSO_HEADROOM_ENABLED", "0")
    inventory = tmp_path / "ollama-inventory.json"

    def write_inventory(*, stale: bool) -> None:
        inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "owner": "reverso-client-sync/provider-ollama",
                    "observed_at": "2026-08-21T08:00:00+00:00",
                    "freshness": "partial" if stale else "current",
                    "auth_status": auth_status if stale else "current",
                    "cloud_status": cloud_status if stale else "current",
                    "entries": [
                        {
                            "raw_id": "cloud-a",
                            "local": False,
                            "cloud": True,
                            "stale": stale,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    write_inventory(stale=False)
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        raise AssertionError("stale model reached an upstream generation endpoint")

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        inventory_path=inventory,
    )
    app = (
        ResponsesGatewayApp({"ollama": runtime.adapter})
        if surface == "responses"
        else AnthropicMessagesApp({"ollama": runtime.adapter})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:64946"
    ) as client:
        if surface == "messages":
            listing = await client.get(
                "/v1/models", headers={"x-reverso-model-catalog": "ollama"}
            )
            model = listing.json()["data"][0]["id"]
            path = "/v1/messages"
            headers = {"x-reverso-model-catalog": "ollama"}
            payload = {"model": model, "max_tokens": 8, "messages": []}
        else:
            path = "/ollama/v1/responses"
            headers = {}
            payload = {"model": "cloud-a", "input": "hello"}
        write_inventory(stale=True)
        response = await client.post(path, headers=headers, json=payload)

    assert response.status_code == 409
    assert expected_code in response.text
    assert seen == ["/api/tags"]
    await runtime.close()


@pytest.mark.asyncio
async def test_response_is_forwarded_and_stored() -> None:
    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(_transport))
    )
    request = ResponsesRequest(model="qwen3:8b", input="hello")

    response = await runtime.adapter.create_response(request)

    assert response.id == "resp_ollama"
    assert (await runtime.adapter.get_response(response.id)).raw == response.raw
    assert (await runtime.adapter.list_input_items(response.id)).data
    await runtime.close()


@pytest.mark.asyncio
async def test_unary_transport_preserves_model_tools_and_image_input() -> None:
    seen: list[dict[str, object]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_rich",
                "object": "response",
                "status": "completed",
                "model": "llava:latest",
                "output": [],
            },
        )

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    request = _rich_request()

    response = await runtime.adapter.create_response(request)

    assert response.id == "resp_rich"
    assert seen == [
        {
            "model": request.model,
            "input": request.input,
            "stream": False,
            "instructions": request.instructions,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "metadata": {"case": "transport"},
        }
    ]
    await runtime.close()
    assert runtime.client.is_closed is True


@pytest.mark.asyncio
async def test_stream_transport_rejects_non_success_before_yielding() -> None:
    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, json={"error": "offline"})
            )
        )
    )
    stream = runtime.adapter.stream_response(_rich_request(stream=True))

    with pytest.raises(httpx.HTTPStatusError):
        await anext(stream)

    await stream.aclose()
    await runtime.close()


@pytest.mark.asyncio
async def test_stream_transport_forwards_sse_tool_event_and_stores_completion() -> None:
    seen: list[dict[str, object]] = []
    completed = {
        "id": "resp_stream_transport",
        "object": "response",
        "status": "completed",
        "model": "llava:latest",
        "output": [],
    }
    wire_events = [
        {"type": "response.created", "response": {"id": completed["id"]}},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "call_1",
            "output_index": 0,
            "delta": '{"key":"value"}',
        },
        {"type": "response.completed", "response": completed},
    ]
    sse = (
        b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in wire_events)
        + b"data: [DONE]\n\n"
    )

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=sse)

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    request = _rich_request(stream=True)
    stream = runtime.adapter.stream_response(request)
    forwarded = []
    async for event in stream:
        forwarded.append(event)
        if event.event == "response.completed":
            break
    await stream.aclose()

    assert [event.data for event in forwarded] == wire_events
    assert forwarded[1].event == "response.function_call_arguments.delta"
    assert seen[0]["model"] == request.model
    assert seen[0]["input"] == request.input
    assert seen[0]["tools"] == request.tools
    assert seen[0]["stream"] is True
    stored = await runtime.adapter.get_response("resp_stream_transport")
    assert stored.raw == completed
    await runtime.close()
    assert runtime.client.is_closed is True


@pytest.mark.asyncio
async def test_cloud_disabled_still_keeps_all_current_local_tags() -> None:
    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(_transport)),
        auth=OllamaAuthState(cloud_requested=False, cloud_status="disabled"),
    )

    models = await runtime.adapter.list_models()

    assert [row["id"] for row in models.data] == [
        "qwen3:8b",
        "gpt-oss:20b",
        "deepseek-v3.1:671b-cloud",
    ]
    assert runtime.auth.cloud_status == "disabled"
    await runtime.close()


@pytest.mark.asyncio
async def test_ollama_no_cloud_discovery_performs_only_local_tags_request() -> None:
    paths: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        auth=OllamaAuthState.from_env(
            {"REVERSO_OLLAMA_CLOUD": "1", "OLLAMA_NO_CLOUD": "1"}
        ),
    )

    models = await runtime.adapter.list_models()

    assert [row["id"] for row in models.data] == ["qwen3:8b"]
    assert runtime.auth.cloud_status == "disabled"
    assert paths == ["/api/tags"]
    await runtime.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434",
        "http://0.0.0.0:11434",
        "http://example.com:11434",
        "http://user@127.0.0.1:11434",
        "http://127.0.0.1:11434/path",
        "http://127.0.0.1:11434?x=1",
    ],
)
def test_endpoint_validation_rejects_non_origin_or_non_loopback(endpoint: str) -> None:
    with pytest.raises(ValueError, match="plain loopback HTTP origin"):
        validate_endpoint(endpoint)


def test_endpoint_validation_accepts_supported_loopback_origins() -> None:
    assert validate_endpoint("http://127.0.0.1:11434/") == ("http://127.0.0.1:11434")
    assert validate_endpoint("http://localhost:11434") == "http://localhost:11434"


@pytest.mark.asyncio
async def test_completed_stream_is_stored_before_consumer_disconnect() -> None:
    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(_transport))
    )

    async def events(_request: ResponsesRequest):
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_stream",
                "object": "response",
                "status": "completed",
                "model": "qwen3:8b",
                "output": [],
            },
        }
        yield {"type": "response.trailing"}

    runtime.responses_client.stream = events  # type: ignore[method-assign]
    stream = runtime.adapter.stream_response(
        ResponsesRequest(model="qwen3:8b", input="hello", stream=True)
    )

    completed = await anext(stream)
    await stream.aclose()

    assert completed.event == "response.completed"
    assert (await runtime.adapter.get_response("resp_stream")).status == "completed"
    await runtime.close()
