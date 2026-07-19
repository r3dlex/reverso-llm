from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.kimi import KimiAdapter, KimiError, KimiOAuthAuth


OAUTH_SENTINEL = "kimi-oauth-sentinel-7f31"
BEARER_SENTINEL = "kimi-bearer-sentinel-92ac"


def _write_token(path: Path, *, access_token: str, expires_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": "refresh-sentinel",
                "expires_at": expires_at,
                "scope": "",
                "token_type": "Bearer",
            }
        )
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_default_oauth_path_matches_current_kimi_code_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    auth = KimiOAuthAuth()

    assert auth._credentials_path == (  # noqa: SLF001 - credential contract fixture
        tmp_path / ".kimi-code" / "credentials" / "kimi-code.json"
    )


@pytest.mark.asyncio
async def test_oauth_access_token_is_primary_bearer_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=time.time() + 3600)
    monkeypatch.setenv("KIMI_BEARER_TOKEN", BEARER_SENTINEL)

    auth = KimiOAuthAuth(credentials_path=token_path)

    assert await auth.resolve_bearer_token() == OAUTH_SENTINEL


@pytest.mark.asyncio
async def test_explicit_bearer_is_fallback_when_oauth_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_BEARER_TOKEN", BEARER_SENTINEL)

    auth = KimiOAuthAuth(credentials_path=tmp_path / "missing.json")

    assert await auth.resolve_bearer_token() == BEARER_SENTINEL


@pytest.mark.asyncio
async def test_expired_oauth_token_is_refreshed_and_persisted(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="expired", expires_at=time.time() - 1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/oauth/token"
        assert request.headers["x-msh-platform"] == "kimi_code_cli"
        return httpx.Response(
            200,
            json={
                "access_token": OAUTH_SENTINEL,
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "scope": "",
                "token_type": "Bearer",
            },
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    assert await auth.resolve_bearer_token() == OAUTH_SENTINEL
    persisted = json.loads(token_path.read_text())
    assert persisted["access_token"] == OAUTH_SENTINEL
    assert persisted["refresh_token"] == "rotated-refresh"
    assert persisted["expires_at"] > time.time()


@pytest.mark.asyncio
async def test_invalid_artifacts_fall_back_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_BEARER_TOKEN", BEARER_SENTINEL)
    fixtures = ("not-json", "[]", '{"access_token":"x","expires_at":"bad"}')
    for index, raw in enumerate(fixtures):
        token_path = tmp_path / f"invalid-{index}.json"
        token_path.write_text(raw)
        auth = KimiOAuthAuth(credentials_path=token_path)
        assert await auth.resolve_bearer_token() == BEARER_SENTINEL


@pytest.mark.asyncio
@pytest.mark.parametrize("access_token", [42, True, "", " "])
async def test_invalid_persisted_access_token_uses_bearer_without_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access_token: object,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": "refresh-sentinel",
                "expires_at": time.time() + 3600,
            }
        )
    )
    monkeypatch.setenv("KIMI_BEARER_TOKEN", BEARER_SENTINEL)
    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: pytest.fail("invalid artifact must not refresh"),
    )

    assert await auth.resolve_bearer_token() == BEARER_SENTINEL


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_token", [42, True, "", " "])
async def test_invalid_persisted_refresh_token_uses_bearer_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_token: object,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": "expired",
                "refresh_token": refresh_token,
                "expires_at": 0,
            }
        )
    )
    monkeypatch.setenv("KIMI_BEARER_TOKEN", BEARER_SENTINEL)
    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: pytest.fail("invalid artifact must not refresh"),
    )

    assert await auth.resolve_bearer_token() == BEARER_SENTINEL


@pytest.mark.asyncio
async def test_whitespace_artifact_without_fallback_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "kimi-code.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": " ",
                "refresh_token": " ",
                "expires_at": 0,
            }
        )
    )
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: pytest.fail("invalid artifact must not refresh"),
    )

    with pytest.raises(KimiError, match=r"kimi /login.*KIMI_BEARER_TOKEN"):
        await auth.resolve_bearer_token()


@pytest.mark.asyncio
async def test_cli_artifact_without_expiry_keeps_static_access_token(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    token_path.write_text(json.dumps({"access_token": OAUTH_SENTINEL}))

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: pytest.fail("fresh token must not refresh"),
    )

    assert await auth.resolve_bearer_token() == OAUTH_SENTINEL


@pytest.mark.asyncio
async def test_refresh_preserves_omitted_fields_and_rotated_fields_win(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": "expired",
                "refresh_token": "preserved-refresh",
                "expires_at": 0,
                "scope": "preserved-scope",
                "token_type": "Bearer",
                "provider_metadata": {"keep": True},
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": OAUTH_SENTINEL, "expires_in": 3600},
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
    )

    assert await auth.resolve_bearer_token() == OAUTH_SENTINEL
    persisted = json.loads(token_path.read_text())
    assert persisted["refresh_token"] == "preserved-refresh"
    assert persisted["scope"] == "preserved-scope"
    assert persisted["provider_metadata"] == {"keep": True}
    assert persisted["access_token"] == OAUTH_SENTINEL


@pytest.mark.asyncio
async def test_concurrent_expiry_refresh_uses_one_exchange(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="expired", expires_at=0)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={"access_token": OAUTH_SENTINEL, "expires_in": 3600},
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
    )

    resolved = await asyncio.gather(*(auth.resolve_bearer_token() for _ in range(8)))

    assert resolved == [OAUTH_SENTINEL] * 8
    assert calls == 1


@pytest.mark.asyncio
async def test_atomic_persistence_is_0600_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "credentials" / "kimi-code.json"
    _write_token(token_path, access_token="expired", expires_at=0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": OAUTH_SENTINEL, "expires_in": 3600},
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
    )
    await auth.resolve_bearer_token()

    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert list(token_path.parent.glob("*.tmp*")) == []


def test_atomic_persistence_cleans_temporary_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "credentials" / "kimi-code.json"
    auth = KimiOAuthAuth(credentials_path=token_path)

    def fail_replace(source: str, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        auth._save_artifact({"access_token": OAUTH_SENTINEL})  # noqa: SLF001
    assert list(token_path.parent.glob("*.tmp*")) == []


@pytest.mark.asyncio
async def test_refresh_errors_do_not_expose_credentials(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"{OAUTH_SENTINEL} refresh-sentinel")

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
    )

    with pytest.raises(KimiError) as caught:
        await auth.resolve_bearer_token()
    assert OAUTH_SENTINEL not in str(caught.value)
    assert "refresh-sentinel" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("access_token", [42, True, "", " "])
async def test_refresh_rejects_non_string_or_empty_access_token(
    tmp_path: Path, access_token: object
) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="expired", expires_at=0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": access_token, "expires_in": 3600},
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
    )

    with pytest.raises(KimiError, match="no access token"):
        await auth.resolve_bearer_token()
    assert json.loads(token_path.read_text())["access_token"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [True, False, "invalid", 0, -1])
async def test_refresh_rejects_boolean_or_invalid_expiry(
    tmp_path: Path, expires_in: object
) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="expired", expires_at=0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": OAUTH_SENTINEL, "expires_in": expires_in},
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
    )

    with pytest.raises(KimiError, match="invalid expiry"):
        await auth.resolve_bearer_token()
    assert json.loads(token_path.read_text())["access_token"] == "expired"


@pytest.mark.asyncio
async def test_adapter_translates_responses_to_kimi_chat_with_bearer(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=time.time() + 3600)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["platform"] = request.headers.get("x-msh-platform")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-kimi",
                "model": "kimi-k2.5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(credentials_path=token_path),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    response = await adapter.create_response(
        ResponsesRequest(model="kimi-k2.5", input="hi")
    )

    assert seen == {
        "authorization": f"Bearer {OAUTH_SENTINEL}",
        "platform": "kimi_code_cli",
        "path": "/coding/v1/chat/completions",
        "body": {
            "model": "kimi-k2.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    }
    assert response.output[0]["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_live_model_listing_uses_kimi_bearer(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=time.time() + 3600)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {OAUTH_SENTINEL}"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "kimi-k2.5",
                        "object": "model",
                        "owned_by": "moonshotai",
                    }
                ]
            },
        )

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(credentials_path=token_path),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    models = await adapter.list_models()

    assert [row["id"] for row in models.data] == ["kimi-k2.5"]
    assert adapter.model_discovery_source == "live"


@pytest.mark.asyncio
async def test_model_listing_deduplicates_ids_and_marks_fallback_provenance(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=time.time() + 3600)
    responses = [
        httpx.Response(
            200,
            json={
                "data": [
                    {"id": "kimi-k2.5"},
                    {"id": "kimi-k2.5"},
                    {"id": ""},
                    {"id": 42},
                    "bad",
                ]
            },
        ),
        httpx.Response(503),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(credentials_path=token_path),
        client_factory=lambda: _client(handler),
    )

    live = await adapter.list_models()
    fallback = await adapter.list_models()

    assert [row["id"] for row in live.data] == ["kimi-k2.5"]
    assert [row["id"] for row in fallback.data] == ["kimi-k2.5"]
    assert adapter.model_discovery_source == "fallback"


@pytest.mark.asyncio
async def test_unary_401_refreshes_and_retries_only_once(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="rejected", expires_at=time.time() + 3600)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/api/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": OAUTH_SENTINEL, "expires_in": 3600},
            )
        chat_calls += 1
        return httpx.Response(401, text=f"do-not-surface-{OAUTH_SENTINEL}")

    def factory() -> httpx.AsyncClient:
        return _client(handler)

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(credentials_path=token_path, client_factory=factory),
        client_factory=factory,
    )

    with pytest.raises(KimiError) as caught:
        await adapter.create_response(ResponsesRequest(model="kimi-k2.5", input="hi"))
    assert chat_calls == 2
    assert OAUTH_SENTINEL not in str(caught.value)


@pytest.mark.asyncio
async def test_streaming_401_retries_before_first_response_event(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="rejected", expires_at=time.time() + 3600)
    stream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stream_calls
        if request.url.path == "/api/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": OAUTH_SENTINEL, "expires_in": 3600},
            )
        stream_calls += 1
        if stream_calls == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            content=(
                b": keepalive\n\n"
                b'data: {"choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}]}\n\n'
            ),
        )

    def factory() -> httpx.AsyncClient:
        return _client(handler)

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(credentials_path=token_path, client_factory=factory),
        client_factory=factory,
    )

    events = [
        event
        async for event in adapter.stream_response(
            ResponsesRequest(model="kimi-k2.5", input="hi", stream=True)
        )
    ]

    assert stream_calls == 2
    assert events[0].event == "response.created"
    assert any(event.event == "response.output_text.delta" for event in events)


@pytest.mark.asyncio
async def test_response_storage_input_items_and_continuity(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=time.time() + 3600)
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
            },
        )

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(credentials_path=token_path),
        client_factory=lambda: _client(handler),
    )
    first = await adapter.create_response(
        ResponsesRequest(model="kimi-k2.5", input="first")
    )
    stored = await adapter.get_response(first.id)
    items = await adapter.list_input_items(first.id)
    second = await adapter.create_response(
        ResponsesRequest(
            model="kimi-k2.5", input="second", previous_response_id=first.id
        )
    )

    assert stored.id == first.id
    assert items.data
    assert second.previous_response_id == first.id
    assert bodies[1]["messages"] == [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
