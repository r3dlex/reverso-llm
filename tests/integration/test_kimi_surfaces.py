from __future__ import annotations

import asyncio
import json
import logging
import sys
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
    AnthropicMessagesApp,
    build_anthropic_adapters,
    build_anthropic_app,
)
from reverso.protocols.kimi_login import KimiLoginCoordinator, KimiLoginError
from reverso.protocols.responses_app import (
    ResponsesGatewayApp,
    build_app,
    split_provider_path,
)
from reverso.proxy.compose import CompositionRoot, build_adapters

BASE_URL = "http://127.0.0.1:64946"


class _StaticAuth:
    async def resolve_bearer_token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


class _Pipe:
    async def read(self, size: int) -> bytes:
        return b""


class _LoginProcess:
    def __init__(
        self,
        credentials_path: Path,
        *,
        ignore_terminate: bool = False,
        ignore_kill: bool = False,
        write_credentials: bool = True,
    ) -> None:
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode: int | None = None
        self.release = asyncio.Event()
        self.terminated = asyncio.Event()
        self.killed = asyncio.Event()
        self._credentials_path = credentials_path
        self._ignore_terminate = ignore_terminate
        self._ignore_kill = ignore_kill
        self._write_credentials = write_credentials

    async def wait(self) -> int:
        await self.release.wait()
        if self._write_credentials:
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
        self.terminated.set()
        if not self._ignore_terminate:
            self.release.set()

    def kill(self) -> None:
        self.killed.set()
        if not self._ignore_kill:
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
        return ResponseEnvelope(id=response_id, model="kimi-k3")

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
                "model": "k3",
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
                json={"model": "kimi-k3", "input": "hello"},
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
async def test_surface_login_drains_pipe_pressure_without_logging_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    credentials_path = tmp_path / "credentials" / "kimi-code.json"
    sensitive_output = "seeded-device-material"
    payload_bytes = 256 * 1024

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        credentials = json.dumps(
            {
                "access_token": "test-login-token",
                "expires_at": time.time() + 3600,
            }
        )
        script = (
            "import os,pathlib;"
            f"payload={sensitive_output!r}.encode()*"
            f"(({payload_bytes}//len({sensitive_output!r}))+1);"
            "os.write(1,payload);os.write(2,payload);"
            f"path=pathlib.Path({str(credentials_path)!r});"
            "path.parent.mkdir(parents=True,exist_ok=True);"
            f"path.write_text({credentials!r},encoding='utf-8')"
        )
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=cast(Any, kwargs["stdout"]),
            stderr=cast(Any, kwargs["stderr"]),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "k3",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "drained"},
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

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        timeout_seconds=2,
        exit_grace_seconds=0.1,
    )
    adapter = KimiAdapter(
        auth=KimiOAuthAuth(
            credentials_path=credentials_path,
            login_coordinator=coordinator,
        ),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    with caplog.at_level(logging.INFO):
        async with _asgi_client(
            build_app({"kimi": adapter}),
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/kimi/v1/responses",
                    json={"model": "kimi-k3", "input": "hello"},
                ),
                timeout=3,
            )

    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "drained"
    assert payload_bytes > 64 * 1024
    assert sensitive_output not in caplog.text
    assert "test-login-token" not in caplog.text


@pytest.mark.asyncio
async def test_responses_and_anthropic_share_one_login_and_resume(
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
        spawned.set()
        return process

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            json={
                "model": "k3",
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

    coordinator = KimiLoginCoordinator(process_factory=spawn)
    auth = KimiOAuthAuth(
        credentials_path=credentials_path,
        login_coordinator=coordinator,
    )

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    responses_adapter = KimiAdapter(auth=auth, client_factory=client_factory)
    anthropic_adapter = KimiAdapter(auth=auth, client_factory=client_factory)
    root = CompositionRoot(
        gateway=build_app({"kimi": responses_adapter}),
        anthropic_app=build_anthropic_app({"kimi": anthropic_adapter}),
        legacy_app=lambda scope, receive, send: pytest.fail(
            "Kimi surfaces must not reach legacy"
        ),
    )

    async with _asgi_client(root) as client:
        responses_requests = [
            asyncio.create_task(
                client.post(
                    "/kimi/v1/responses",
                    json={"model": "kimi-k3", "input": "hello"},
                )
            )
            for _ in range(2)
        ]
        anthropic_requests = [
            asyncio.create_task(
                client.post(
                    "/kimi/v1/messages",
                    json={
                        "model": "kimi-k3",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
            )
            for _ in range(2)
        ]
        await asyncio.wait_for(spawned.wait(), timeout=1)
        assert not any(request.done() for request in responses_requests)
        assert not any(request.done() for request in anthropic_requests)
        assert login_calls == 1
        assert responses_adapter._auth is anthropic_adapter._auth

        responses_requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await responses_requests[0]
        assert login_calls == 1

        process.release.set()
        responses, anthropic_one, anthropic_two = await asyncio.gather(
            responses_requests[1],
            *anthropic_requests,
        )

    assert responses.status_code == 200
    assert responses.json()["output"][0]["content"][0]["text"] == "resumed"
    assert anthropic_one.status_code == 200
    assert anthropic_one.json()["content"][0]["text"] == "resumed"
    assert anthropic_two.status_code == 200
    assert anthropic_two.json()["content"][0]["text"] == "resumed"
    assert upstream_calls == 3
    assert process.returncode == 0
    assert coordinator._task is None
    assert coordinator._waiters == 0


@pytest.mark.asyncio
async def test_responses_and_anthropic_share_one_login_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    credentials_path = tmp_path / "credentials" / "kimi-code.json"
    process = _LoginProcess(
        credentials_path,
        ignore_terminate=True,
        write_credentials=False,
    )
    spawned = asyncio.Event()
    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _LoginProcess:
        nonlocal login_calls
        login_calls += 1
        spawned.set()
        return process

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        timeout_seconds=0.2,
        exit_grace_seconds=0.01,
    )
    auth = KimiOAuthAuth(
        credentials_path=credentials_path,
        login_coordinator=coordinator,
    )

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("timeout must not reach upstream")
            )
        )

    root = CompositionRoot(
        gateway=build_app(
            {"kimi": KimiAdapter(auth=auth, client_factory=client_factory)}
        ),
        anthropic_app=build_anthropic_app(
            {"kimi": KimiAdapter(auth=auth, client_factory=client_factory)}
        ),
        legacy_app=lambda scope, receive, send: pytest.fail(
            "Kimi surfaces must not reach legacy"
        ),
    )

    async with _asgi_client(root) as client:
        requests = [
            asyncio.create_task(
                client.post(
                    "/kimi/v1/responses",
                    json={"model": "kimi-k3", "input": "hello"},
                )
            )
            for _ in range(2)
        ]
        requests.extend(
            asyncio.create_task(
                client.post(
                    "/kimi/v1/messages",
                    json={
                        "model": "kimi-k3",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
            )
            for _ in range(2)
        )
        await asyncio.wait_for(spawned.wait(), timeout=0.1)

        async def wait_for_all_callers() -> None:
            while coordinator._waiters != 4:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_all_callers(), timeout=0.1)
        responses = await asyncio.wait_for(
            asyncio.gather(*requests),
            timeout=0.5,
        )

    assert {response.status_code for response in responses} == {502}
    classification = "Kimi login timed out; run kimi login manually in a terminal"
    messages = [response.json()["error"]["message"] for response in responses]
    assert all(message.endswith(classification) for message in messages)
    assert login_calls == 1
    assert process.killed.is_set()
    assert process.returncode == 0
    assert coordinator._task is None
    assert coordinator._waiters == 0


@pytest.mark.asyncio
async def test_anthropic_generic_backend_failure_hides_exception_payload() -> None:
    class FailingAdapter(_AnthropicSpy):
        async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
            raise RuntimeError("private-upstream-url-and-token")

    app = build_anthropic_app({"kimi": FailingAdapter()})

    async with _asgi_client(app) as client:
        response = await client.post(
            "/kimi/v1/messages",
            json={
                "model": "kimi-k3",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error == {
        "type": "api_error",
        "message": "upstream backend error (RuntimeError)",
    }
    assert "private-upstream-url-and-token" not in response.text


@pytest.mark.asyncio
async def test_cancelled_last_request_cleanup_precedes_fresh_surface_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    credentials_path = tmp_path / "credentials" / "kimi-code.json"
    first_process = _LoginProcess(
        credentials_path,
        ignore_terminate=True,
        write_credentials=False,
    )
    second_process = _LoginProcess(credentials_path)
    processes = iter([first_process, second_process])
    login_calls = 0
    second_spawned = asyncio.Event()

    async def spawn(*args: object, **kwargs: object) -> _LoginProcess:
        nonlocal login_calls
        login_calls += 1
        process = next(processes)
        if process is second_process:
            second_spawned.set()
        return process

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "k3",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "fresh"},
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

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        exit_grace_seconds=0.05,
    )
    auth = KimiOAuthAuth(
        credentials_path=credentials_path,
        login_coordinator=coordinator,
    )
    adapter = KimiAdapter(
        auth=auth,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    root = CompositionRoot(
        gateway=build_app({"kimi": adapter}),
        legacy_app=lambda scope, receive, send: pytest.fail(
            "Kimi surface must not reach legacy"
        ),
    )

    async with _asgi_client(root) as client:
        abandoned = asyncio.create_task(
            client.post(
                "/kimi/v1/responses",
                json={"model": "kimi-k3", "input": "abandon"},
            )
        )
        while login_calls == 0:
            await asyncio.sleep(0)

        abandoned.cancel()
        await asyncio.wait_for(first_process.terminated.wait(), timeout=0.1)
        replacement = asyncio.create_task(
            client.post(
                "/kimi/v1/responses",
                json={"model": "kimi-k3", "input": "retry"},
            )
        )
        await asyncio.sleep(0)

        assert login_calls == 1
        assert not replacement.done()

        await asyncio.wait_for(second_spawned.wait(), timeout=0.2)
        second_process.release.set()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        response = await asyncio.wait_for(replacement, timeout=0.2)

    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "fresh"
    assert login_calls == 2
    assert first_process.killed.is_set()
    assert coordinator._task is None


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
            json={"model": "kimi-k3", "input": "hello"},
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
                "model": "k3",
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
            json={"model": "kimi-k3", "input": "first"},
        )
        response_id = first.json()["id"]
        stored = await client.get(f"/kimi/v1/responses/{response_id}")
        input_items = await client.get(f"/kimi/v1/responses/{response_id}/input_items")
        second = await client.post(
            "/kimi/v1/responses",
            json={
                "model": "kimi-k3",
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
@pytest.mark.parametrize("model", ["kimi-k2.5", "k3", "other"])
async def test_kimi_responses_surface_rejects_noncanonical_model(model: str) -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = build_app(
        {
            "kimi": KimiAdapter(
                auth=cast(Any, _StaticAuth()),
                client_factory=lambda: httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ),
            )
        }
    )

    async with _asgi_client(app) as client:
        response = await client.post(
            "/kimi/v1/responses",
            json={"model": model, "input": "hello"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Kimi supports only kimi-k3",
            "type": "invalid_request_error",
        }
    }
    assert upstream_calls == 0


@pytest.mark.asyncio
async def test_kimi_responses_surface_defaults_absent_model_to_k3() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "k3",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    app = build_app(
        {
            "kimi": KimiAdapter(
                auth=cast(Any, _StaticAuth()),
                client_factory=lambda: httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ),
            )
        }
    )

    async with _asgi_client(app) as client:
        response = await client.post(
            "/kimi/v1/responses",
            json={"input": "hello"},
        )

    assert response.status_code == 200
    assert bodies[0]["model"] == "k3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("/kimi/v1/messages", "kimi-k3"),
        ("/v1/messages", "kimi-k3"),
        ("/v1/messages", "kimi/kimi-k3"),
        ("/v1/messages", "anthropic-kimi-kimi-k3"),
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
    assert adapter.requests[0].model == "kimi-k3"


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
    responses_app = cast(ResponsesGatewayApp, root._gateway)
    anthropic_app = cast(AnthropicMessagesApp, root._anthropic_app)
    responses_kimi = cast(KimiAdapter, responses_app._adapters["kimi"])
    anthropic_kimi = cast(KimiAdapter, anthropic_app._adapters["kimi"])
    responses_auth = responses_kimi._auth
    anthropic_auth = anthropic_kimi._auth

    assert responses_auth is root._kimi_auth
    assert anthropic_auth is root._kimi_auth
    assert anthropic_auth._login_coordinator is root._kimi_login

    await root.close()
    await root.close()

    assert root._kimi_login is not None
    assert root._kimi_login._closed is True


def test_standalone_factories_keep_independent_kimi_auth() -> None:
    responses = build_adapters({"REVERSO_CODEX_DIRECT_BACKEND": "0"})
    anthropic = build_anthropic_adapters()
    responses_kimi = cast(KimiAdapter, responses["kimi"])
    anthropic_kimi = cast(KimiAdapter, anthropic["kimi"])

    assert responses_kimi._auth is not anthropic_kimi._auth
    assert responses_kimi._auth._login_coordinator is None
    assert anthropic_kimi._auth._login_coordinator is None


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
async def test_lifespan_reaps_active_kimi_login_before_shutdown_completion(
    tmp_path: Path,
) -> None:
    process = _LoginProcess(
        tmp_path / "credentials" / "kimi-code.json",
        ignore_terminate=True,
        write_credentials=False,
    )
    spawned = asyncio.Event()

    async def spawn(*args: object, **kwargs: object) -> _LoginProcess:
        spawned.set()
        return process

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []
    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        exit_grace_seconds=0.01,
    )
    waiter = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(spawned.wait(), timeout=0.1)
    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = coordinator
    root._lifespan_cleanup_timeout_seconds = 0.1

    await root(
        {"type": "lifespan", "asgi": {"version": "3.0"}},
        lambda: asyncio.sleep(0, result=next(events)),
        lambda message: asyncio.sleep(0, result=sent.append(message)),
    )
    result = await asyncio.gather(waiter, return_exceptions=True)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert isinstance(result[0], KimiLoginError)
    assert process.killed.is_set()
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_lifespan_reports_prior_fatal_kimi_cleanup_failure(
    tmp_path: Path,
) -> None:
    process = _LoginProcess(
        tmp_path / "credentials" / "kimi-code.json",
        ignore_terminate=True,
        ignore_kill=True,
        write_credentials=False,
    )

    async def spawn(*args: object, **kwargs: object) -> _LoginProcess:
        return process

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        timeout_seconds=0.01,
        exit_grace_seconds=0.01,
    )
    with pytest.raises(KimiLoginError, match="cleanup failed"):
        await coordinator.login()

    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = coordinator
    await root(
        {"type": "lifespan", "asgi": {"version": "3.0"}},
        lambda: asyncio.sleep(0, result=next(events)),
        lambda message: asyncio.sleep(0, result=sent.append(message)),
    )

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {
            "type": "lifespan.shutdown.failed",
            "message": "Kimi login shutdown cleanup failed",
        },
    ]


@pytest.mark.asyncio
async def test_lifespan_timeout_does_not_cancel_active_kimi_cleanup(
    tmp_path: Path,
) -> None:
    process = _LoginProcess(
        tmp_path / "credentials" / "kimi-code.json",
        ignore_terminate=True,
        write_credentials=False,
    )
    spawned = asyncio.Event()

    async def spawn(*args: object, **kwargs: object) -> _LoginProcess:
        spawned.set()
        return process

    async def legacy_lifespan(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    events = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []
    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        exit_grace_seconds=0.05,
    )
    waiter = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(spawned.wait(), timeout=0.1)
    root = CompositionRoot(legacy_app=legacy_lifespan)
    root._kimi_login = coordinator
    root._lifespan_cleanup_timeout_seconds = 0.01

    await root(
        {"type": "lifespan", "asgi": {"version": "3.0"}},
        lambda: asyncio.sleep(0, result=next(events)),
        lambda message: asyncio.sleep(0, result=sent.append(message)),
    )

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {
            "type": "lifespan.shutdown.failed",
            "message": "Kimi login shutdown cleanup failed",
        },
    ]
    close_task = coordinator._close_task
    assert close_task is not None
    assert not close_task.cancelled()

    await asyncio.wait_for(coordinator.close(), timeout=0.2)
    result = await asyncio.gather(waiter, return_exceptions=True)

    assert coordinator._close_task is close_task
    assert isinstance(result[0], KimiLoginError)
    assert process.killed.is_set()
    assert process.returncode == 0


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
