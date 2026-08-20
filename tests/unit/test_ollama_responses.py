from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from reverso.protocols.adapters.ollama import build_ollama_runtime
from reverso.protocols.responses_app import APP_PROVIDER_PREFIXES, ResponsesGatewayApp
from reverso.protocols.surface_registry import SURFACE_BACKENDS
from reverso.proxy import compose
from reverso.proxy.compose import CompositionRoot


def test_ollama_is_responses_only_in_g1() -> None:
    assert "ollama" in APP_PROVIDER_PREFIXES
    assert "ollama" not in SURFACE_BACKENDS["anthropic"]


@pytest.mark.asyncio
async def test_composition_owns_one_runtime_and_closes_it_once() -> None:
    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    )
    root = CompositionRoot(
        ollama_runtime=runtime,
        legacy_app=lambda *_: None,
        anthropic_app=lambda *_: None,
    )

    assert isinstance(root._gateway, ResponsesGatewayApp)
    assert root._gateway._adapters["ollama"] is runtime.adapter
    await root.close()
    await root.close()
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_close_waits_for_active_request_and_stream_then_rejects_new_work() -> (
    None
):
    entered = {"request": asyncio.Event(), "stream": asyncio.Event()}
    release = asyncio.Event()

    async def gateway(scope: dict[str, Any], _receive: Any, send: Any) -> None:
        kind = "stream" if scope["path"].endswith("/stream") else "request"
        entered[kind].set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    )
    close_calls = 0
    original_close = runtime.close

    async def counted_close() -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close()

    runtime.close = counted_close  # type: ignore[method-assign]
    root = CompositionRoot(
        gateway=gateway,
        ollama_runtime=runtime,
        anthropic_app=gateway,
        legacy_app=gateway,
    )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    async def collect(message: dict[str, Any]) -> None:
        sent.append(message)

    async def collect_rejected(message: dict[str, Any]) -> None:
        rejected.append(message)

    request = asyncio.create_task(
        root({"type": "http", "path": "/ollama/v1/models"}, receive, collect)
    )
    stream = asyncio.create_task(
        root({"type": "http", "path": "/other/stream"}, receive, collect)
    )
    await asyncio.gather(*(event.wait() for event in entered.values()))

    closing = asyncio.create_task(root.close())
    await asyncio.sleep(0)
    assert not closing.done()
    await root({"type": "http", "path": "/health"}, receive, collect_rejected)
    assert rejected[0]["status"] == 503

    release.set()
    await asyncio.gather(request, stream, closing)
    await root.close()
    assert close_calls == 1


@pytest.mark.asyncio
async def test_lifespan_forces_hung_request_to_quiesce_and_closes_runtime_once() -> (
    None
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def gateway(_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def legacy_lifespan(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    runtime = build_ollama_runtime(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    )
    close_calls = 0
    original_close = runtime.close

    async def counted_close() -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close()

    runtime.close = counted_close  # type: ignore[method-assign]
    root = CompositionRoot(
        gateway=gateway,
        ollama_runtime=runtime,
        anthropic_app=gateway,
        legacy_app=legacy_lifespan,
    )
    root._lifespan_cleanup_timeout_seconds = 0.01

    async def receive_http() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send_http(_message: dict[str, Any]) -> None:
        return None

    request = asyncio.create_task(
        root(
            {"type": "http", "path": "/ollama/v1/models"},
            receive_http,
            send_http,
        )
    )
    await entered.wait()

    events = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    lifespan_sent: list[dict[str, Any]] = []
    lifespan = asyncio.create_task(
        root._run_lifespan(
            {"type": "lifespan", "asgi": {"version": "3.0"}},
            lambda: asyncio.sleep(0, result=next(events)),
            lambda message: asyncio.sleep(0, result=lifespan_sent.append(message)),
        )
    )
    while root._accepting_http:
        await asyncio.sleep(0)

    rejected: list[dict[str, Any]] = []
    await root(
        {"type": "http", "path": "/health"},
        receive_http,
        lambda message: asyncio.sleep(0, result=rejected.append(message)),
    )

    await asyncio.wait_for(lifespan, timeout=0.1)
    await asyncio.gather(request, return_exceptions=True)

    assert rejected[0]["status"] == 503
    assert cancelled.is_set()
    assert runtime.closed is True
    assert runtime.client.is_closed is True
    assert close_calls == 1
    assert lifespan_sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


def test_gateway_composition_failure_does_not_construct_owned_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_built = False

    def build_runtime() -> None:
        nonlocal runtime_built
        runtime_built = True

    monkeypatch.setattr(compose, "build_ollama_runtime", build_runtime)
    monkeypatch.setattr(compose, "build_app", lambda _adapters: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        CompositionRoot(anthropic_app=lambda *_: None)
    assert runtime_built is False
