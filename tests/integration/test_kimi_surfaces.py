from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.adapters.kimi import KimiAdapter, KimiOAuthAuth
from reverso.protocols.anthropic_app import (
    build_anthropic_adapters,
    build_anthropic_app,
)
from reverso.protocols.kimi_login import KimiLoginCoordinator
from reverso.protocols.responses_app import build_app, split_provider_path
from reverso.proxy.compose import CompositionRoot, build_adapters

BASE_URL = "http://127.0.0.1:64946"


class _StaticAuth:
    async def resolve_bearer_token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


class _Pipe:
    async def read(self, size: int) -> bytes:
        return b""


class _LoginProcess:
    def __init__(self, credentials_path: Path) -> None:
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode: int | None = None
        self.release = asyncio.Event()
        self._credentials_path = credentials_path

    async def wait(self) -> int:
        await self.release.wait()
        self._credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self._credentials_path.write_text(
            json.dumps(
                {
                    "access_token": "test-login-token",
                    "expires_at": time.time() + 3600,
                }
            ),
            encoding="utf-8",
        )
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.release.set()

    def kill(self) -> None:
        self.release.set()


class _AnthropicSpy:
    def __init__(self) -> None:
        self.requests: list[ResponsesRequest] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.requests.append(request)
        return ResponseEnvelope(
            id="resp_kimi_messages",
            model=request.model,
            output=[
                {
                    "id": "msg_kimi_messages",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "ok", "annotations": []}
                    ],
                }
            ],
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        raise AssertionError("streaming is covered by the Headroom matrix")

    async def list_models(self) -> ModelList:
        return ModelList()

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="kimi-k2.5")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


def _asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast(Any, app)),
        base_url=BASE_URL,
    )


@pytest.mark.asyncio
async def test_missing_auth_waits_for_login_then_resumes_responses_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    credentials_path = tmp_path / "credentials" / "kimi-code.json"
    process = _LoginProcess(credentials_path)
    spawned = asyncio.Event()
    login_calls = 0
    upstream_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _LoginProcess:
        nonlocal login_calls
        login_calls += 1
        assert args == ("kimi", "login")
        spawned.set()
        return process

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        assert request.headers["authorization"] == "Bearer test-login-token"
        return httpx.Response(
            200,
            json={
                "model": "kimi-k2.5",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "resumed"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    auth = KimiOAuthAuth(
        credentials_path=credentials_path,
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )
    adapter = KimiAdapter(
        auth=auth,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    app = build_app({"kimi": adapter})

    async with _asgi_client(app) as client:
        request = asyncio.create_task(
            client.post(
                "/kimi/v1/responses",
                json={"model": "kimi-k2.5", "input": "hello"},
            )
        )
        await asyncio.wait_for(spawned.wait(), timeout=1)
        assert not request.done()
        assert upstream_calls == 0

        process.release.set()
        response = await asyncio.wait_for(request, timeout=1)

    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "resumed"
    assert login_calls == 1
    assert upstream_calls == 1


@pytest.mark.asyncio
async def test_login_failure_has_actionable_secret_safe_http_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)

    async def missing(*args: object, **kwargs: object) -> _LoginProcess:
        raise FileNotFoundError("private-executable-detail")

    auth = KimiOAuthAuth(
        credentials_path=tmp_path / "missing.json",
        login_coordinator=KimiLoginCoordinator(process_factory=missing),
    )
    app = build_app({"kimi": KimiAdapter(auth=auth)})

    async with _asgi_client(app) as client:
        response = await client.post(
            "/kimi/v1/responses",
            json={"model": "kimi-k2.5", "input": "hello"},
        )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "server_error"
    assert "Kimi CLI is unavailable" in error["message"]
    assert "run kimi login manually" in error["message"]
    assert "private-executable-detail" not in error["message"]


@pytest.mark.asyncio
async def test_kimi_responses_surface_stores_and_chains_turns() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "kimi-k2.5",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    adapter = KimiAdapter(
        auth=cast(Any, _StaticAuth()),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    app = build_app({"kimi": adapter})

    async with _asgi_client(app) as client:
        first = await client.post(
            "/kimi/v1/responses",
            json={"model": "kimi-k2.5", "input": "first"},
        )
        response_id = first.json()["id"]
        stored = await client.get(f"/kimi/v1/responses/{response_id}")
        input_items = await client.get(f"/kimi/v1/responses/{response_id}/input_items")
        second = await client.post(
            "/kimi/v1/responses",
            json={
                "model": "kimi-k2.5",
                "input": "second",
                "previous_response_id": response_id,
            },
        )

    assert first.status_code == stored.status_code == input_items.status_code == 200
    assert stored.json()["id"] == response_id
    assert input_items.json()["data"][0]["content"][0]["text"] == "first"
    assert second.status_code == 200
    assert second.json()["previous_response_id"] == response_id
    assert bodies[1]["messages"] == [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("/kimi/v1/messages", "kimi-k2.5"),
        ("/v1/messages", "kimi/kimi-k2.5"),
        ("/v1/messages", "anthropic-kimi-kimi-k2.5"),
    ],
)
async def test_kimi_messages_routes_and_strips_routing_alias(
    path: str, model: str
) -> None:
    adapter = _AnthropicSpy()
    app = build_anthropic_app(cast(Any, {"kimi": adapter}))

    async with _asgi_client(app) as client:
        response = await client.post(
            path,
            json={
                "model": model,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert adapter.requests[0].model == "kimi-k2.5"


def test_kimi_prefix_is_owned_by_first_party_responses_app() -> None:
    route = split_provider_path("/kimi/v1/responses")

    assert route is not None
    assert route.provider == "kimi"
    assert route.path == "/v1/responses"


def test_real_composition_factories_mount_kimi_adapter() -> None:
    responses = build_adapters({"REVERSO_CODEX_DIRECT_BACKEND": "0"})
    anthropic = build_anthropic_adapters()

    assert isinstance(responses["kimi"], KimiAdapter)
    assert isinstance(anthropic["kimi"], KimiAdapter)


@pytest.mark.asyncio
async def test_composition_owns_responses_kimi_auth_and_idempotent_close() -> None:
    root = CompositionRoot()
    responses_auth = root._gateway._adapters["kimi"]._auth
    anthropic_auth = root._anthropic_app._adapters["kimi"]._auth

    assert responses_auth is root._kimi_auth
    assert anthropic_auth is not root._kimi_auth
    assert anthropic_auth._login_coordinator is None

    await root.close()
    await root.close()

    assert root._kimi_login is not None
    assert root._kimi_login._closed is True


@pytest.mark.asyncio
async def test_lifespan_waits_for_kimi_cleanup_before_one_shutdown_completion() -> None:
    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    legacy_received: list[str] = []

    class CleanupSpy:
        async def close(self) -> None:
            cleanup_started.set()
            await cleanup_release.wait()

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert scope["type"] == "lifespan"
        legacy_received.append((await receive())["type"])
        await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.startup.complete"})
        legacy_received.append((await receive())["type"])
        await send({"type": "lifespan.shutdown.complete"})

    async def receive() -> dict[str, Any]:
        return next(events)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = cast(Any, CleanupSpy())
    lifespan = asyncio.create_task(
        root({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    )

    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert sent == [{"type": "lifespan.startup.complete"}]
    cleanup_release.set()
    await asyncio.wait_for(lifespan, timeout=1)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert legacy_received == ["lifespan.startup", "lifespan.shutdown"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_terminal", "cleanup_error", "failure"),
    [
        (
            "lifespan.shutdown.failed",
            None,
            "legacy application shutdown failed",
        ),
        (
            "lifespan.shutdown.complete",
            RuntimeError("private-cleanup-detail"),
            "Kimi login shutdown cleanup failed",
        ),
        (
            "lifespan.shutdown.failed",
            RuntimeError("private-cleanup-detail"),
            "legacy shutdown and Kimi login cleanup failed",
        ),
    ],
    ids=["legacy-failure", "cleanup-failure", "combined-failure"],
)
async def test_lifespan_emits_one_secret_free_shutdown_failure(
    legacy_terminal: str,
    cleanup_error: Exception | None,
    failure: str,
) -> None:
    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []
    cleanup_calls = 0

    class CleanupSpy:
        async def close(self) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_error is not None:
                raise cleanup_error

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert scope["type"] == "lifespan"
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send(
            {
                "type": legacy_terminal,
                "message": "private-legacy-detail",
            }
        )

    async def receive() -> dict[str, Any]:
        return next(events)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = cast(Any, CleanupSpy())

    await root({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.failed", "message": failure},
    ]
    assert "private" not in json.dumps(sent)
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_lifespan_preserves_startup_failure_without_forwarding_detail() -> None:
    sent: list[dict[str, Any]] = []
    legacy_received: list[str] = []

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert scope["type"] == "lifespan"
        legacy_received.append((await receive())["type"])
        await send(
            {
                "type": "lifespan.startup.failed",
                "message": "private-startup-detail",
            }
        )

    root = CompositionRoot(legacy_app=legacy_lifespan)

    await root(
        {"type": "lifespan", "asgi": {"version": "3.0"}},
        lambda: asyncio.sleep(0, result={"type": "lifespan.startup"}),
        lambda message: asyncio.sleep(0, result=sent.append(message)),
    )

    assert sent == [
        {
            "type": "lifespan.startup.failed",
            "message": "legacy application startup failed",
        }
    ]
    assert legacy_received == ["lifespan.startup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_terminal", ["startup", "shutdown"])
async def test_lifespan_return_without_matching_terminal_fails_once(
    missing_terminal: str,
) -> None:
    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []
    cleanup_calls = 0

    class CleanupSpy:
        async def close(self) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert scope["type"] == "lifespan"
        assert (await receive())["type"] == "lifespan.startup"
        if missing_terminal == "startup":
            return
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"

    async def receive() -> dict[str, Any]:
        return next(events)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = cast(Any, CleanupSpy())

    await root({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    if missing_terminal == "startup":
        assert sent == [
            {
                "type": "lifespan.startup.failed",
                "message": "legacy application startup failed",
            }
        ]
        assert cleanup_calls == 0
    else:
        assert sent == [
            {"type": "lifespan.startup.complete"},
            {
                "type": "lifespan.shutdown.failed",
                "message": "legacy application shutdown failed",
            },
        ]
        assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_lifespan_bounds_kimi_cleanup_before_shutdown_failure() -> None:
    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []
    cleanup_cancelled = asyncio.Event()

    class HangingCleanup:
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert scope["type"] == "lifespan"
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    async def receive() -> dict[str, Any]:
        return next(events)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = cast(Any, HangingCleanup())
    root._lifespan_cleanup_timeout_seconds = 0.01

    await asyncio.wait_for(
        root({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send),
        timeout=0.1,
    )

    assert cleanup_cancelled.is_set()
    assert sent == [
        {"type": "lifespan.startup.complete"},
        {
            "type": "lifespan.shutdown.failed",
            "message": "Kimi login shutdown cleanup failed",
        },
    ]
