"""Unit tests for the Copilot adapter ported direct-forward spine.

All credentials are FAKE fixtures: no Keychain, real GitHub login, or network.
Covers OAuth token read, bearer-token refresh/exchange, the runtime.protocols
contract surface, timeout handling, and that no token substring is ever logged.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
)
from reverso.protocols.adapters.copilot import (
    CopilotAdapter,
    CopilotAuth,
    GITHUB_TOKEN_URL,
)

FAKE_OAUTH_TOKEN = "gho_FAKEoauthTOKENvalue1234567890"
FAKE_BEARER_TOKEN = "tid=FAKEbearerTOKENvalue0987654321"


def _write_hosts(config_dir, token: str = FAKE_OAUTH_TOKEN) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "hosts.json").write_text(
        json.dumps({"github.com": {"oauth_token": token}})
    )


def _mock_client(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


def test_read_oauth_token_from_hosts(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    auth = CopilotAuth(config_dir=config_dir)

    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == "copilot_oauth"


def test_resolve_unauthenticated_when_missing(tmp_path):
    auth = CopilotAuth(config_dir=tmp_path / "github-copilot")

    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == "copilot_oauth"


async def test_bearer_token_exchanges_and_caches(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GITHUB_TOKEN_URL
        assert request.headers["Authorization"] == f"token {FAKE_OAUTH_TOKEN}"
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"token": FAKE_BEARER_TOKEN, "expires_at": time.time() + 3600},
        )

    auth = CopilotAuth(config_dir=config_dir)
    transport = httpx.MockTransport(handler)

    async def _exchange() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(
                GITHUB_TOKEN_URL,
                headers={"Authorization": f"token {auth._read_oauth_token()}"},
            )
        auth._copilot_token = response.json()
        auth._save_cached_token()

    await _exchange()
    token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN
    assert (config_dir / "token.json").exists()
    assert calls["n"] == 1


async def test_bearer_token_refreshes_expired(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    (config_dir / "token.json").write_text(
        json.dumps({"token": "tid=STALEfake", "expires_at": time.time() - 10})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"token": FAKE_BEARER_TOKEN, "expires_at": time.time() + 3600},
        )

    auth = CopilotAuth(config_dir=config_dir)
    transport = httpx.MockTransport(handler)
    auth._exchange_token = _patched_exchange(auth, transport)  # type: ignore[method-assign]

    token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN


def _patched_exchange(auth: CopilotAuth, transport: httpx.MockTransport):
    async def _exchange() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(
                GITHUB_TOKEN_URL,
                headers={"Authorization": f"token {auth._read_oauth_token()}"},
            )
        auth._copilot_token = response.json()
        auth._save_cached_token()

    return _exchange


async def test_bearer_token_timeout_propagates(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("token endpoint timed out")

    auth = CopilotAuth(config_dir=config_dir)
    transport = httpx.MockTransport(handler)

    async def _exchange() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(GITHUB_TOKEN_URL)

    auth._exchange_token = _exchange  # type: ignore[method-assign]

    with pytest.raises(httpx.TimeoutException):
        await auth.bearer_token()


def _fake_auth_adapter(handler, store=None) -> CopilotAdapter:
    class _FakeAuth:
        def resolve(self):  # pragma: no cover - not exercised here
            from reverso.protocols.auth import AuthResolution

            return AuthResolution(authenticated=True, method="copilot_oauth")

        async def bearer_token(self) -> str:
            return FAKE_BEARER_TOKEN

    return CopilotAdapter(
        auth=_FakeAuth(),
        store=store,
        client_factory=_mock_client(handler),
    )


def test_adapter_satisfies_protocol():
    adapter = _fake_auth_adapter(lambda r: httpx.Response(200, json={}))
    assert isinstance(adapter, ProviderAdapter)


async def test_create_response_forwards_and_stores():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["integration"] = request.headers.get("Copilot-Integration-Id")
        return httpx.Response(
            200,
            json={
                "id": "resp_fake_1",
                "model": "gpt-4o",
                "status": "completed",
                "output": [{"type": "message"}],
            },
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-4o", "input": [{"role": "user", "content": "hi"}]}
    )

    envelope = await adapter.create_response(request)

    assert isinstance(envelope, ResponseEnvelope)
    assert envelope.id == "resp_fake_1"
    assert captured["url"].endswith("/responses")
    assert captured["auth"] == f"Bearer {FAKE_BEARER_TOKEN}"
    assert captured["integration"] == "vscode-chat"

    stored = await adapter.get_response("resp_fake_1")
    assert stored.id == "resp_fake_1"
    items = await adapter.list_input_items("resp_fake_1")
    assert isinstance(items, InputItemList)
    assert items.data == [{"role": "user", "content": "hi"}]


async def test_stream_response_yields_events():
    sse = (
        b"event: response.output_text.delta\n"
        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-4o", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]

    assert events
    assert events[0].event == "response.output_text.delta"
    assert events[0].data["delta"] == "hi"
    assert events[0].raw is not None


async def test_list_models_normalizes_to_openai():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o", "vendor": "openai"}, "bad", {"id": "o1"}]},
        )

    adapter = _fake_auth_adapter(handler)

    models = await adapter.list_models()

    assert isinstance(models, ModelList)
    assert models.object == "list"
    assert models.models == []
    ids = [m["id"] for m in models.data]
    assert ids == ["gpt-4o", "o1"]
    assert models.data[0]["owned_by"] == "openai"
    assert models.data[1]["owned_by"] == "github-copilot"


async def test_create_response_timeout_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out")

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload({"model": "gpt-4o", "input": "hi"})

    with pytest.raises(httpx.TimeoutException):
        await adapter.create_response(request)


async def test_no_token_substring_in_logs(tmp_path, caplog):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    (config_dir / "token.json").write_text(
        json.dumps({"token": FAKE_BEARER_TOKEN, "expires_at": time.time() + 3600})
    )
    auth = CopilotAuth(config_dir=config_dir)

    with caplog.at_level(logging.DEBUG):
        token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN
    log_text = caplog.text
    assert FAKE_BEARER_TOKEN not in log_text
    assert FAKE_OAUTH_TOKEN not in log_text
    assert FAKE_BEARER_TOKEN[8:] not in log_text
    assert FAKE_OAUTH_TOKEN[4:] not in log_text
