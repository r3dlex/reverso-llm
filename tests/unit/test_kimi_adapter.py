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
from reverso.protocols.kimi_login import KimiLoginCoordinator
from reverso.protocols.responses_app import _models_to_payload

OAUTH_SENTINEL = "kimi-oauth-sentinel-7f31"
BEARER_SENTINEL = "kimi-bearer-sentinel-92ac"


class _Pipe:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)
        self.read_started = asyncio.Event()

    async def read(self, size: int) -> bytes:
        self.read_started.set()
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _Process:
    def __init__(
        self,
        return_code: int = 0,
        *,
        stdout: tuple[bytes, ...] = (),
        stderr: tuple[bytes, ...] = (),
    ) -> None:
        self.stdout = _Pipe(*stdout)
        self.stderr = _Pipe(*stderr)
        self.returncode: int | None = None
        self._return_code = return_code
        self.release = asyncio.Event()
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        await self.release.wait()
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        self.release.set()

    def kill(self) -> None:
        self.release.set()


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

    assert auth._credentials_path == (
        tmp_path / ".kimi-code" / "credentials" / "kimi-code.json"
    )


def test_kimi_code_home_overrides_default_oauth_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "custom-kimi-home"))

    auth = KimiOAuthAuth()

    assert auth.credentials_path == (
        tmp_path / "custom-kimi-home" / "credentials" / "kimi-code.json"
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
    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        return _Process()

    auth = KimiOAuthAuth(
        credentials_path=tmp_path / "missing.json",
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    assert await auth.resolve_bearer_token() == BEARER_SENTINEL
    assert login_calls == 0


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
async def test_invalid_persisted_access_token_uses_usable_refresh(
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

    with pytest.raises(KimiError, match=r"kimi login.*KIMI_BEARER_TOKEN"):
        await auth.resolve_bearer_token()


@pytest.mark.asyncio
async def test_missing_artifact_callers_share_one_login_and_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    token_path = tmp_path / "credentials" / "kimi-code.json"
    process = _Process()
    spawned = asyncio.Event()
    calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal calls
        calls += 1
        assert args == ("kimi", "login")
        spawned.set()
        return process

    coordinator = KimiLoginCoordinator(process_factory=spawn)
    auth = KimiOAuthAuth(
        credentials_path=token_path,
        login_coordinator=coordinator,
    )
    waiters = [
        asyncio.create_task(auth.resolve_bearer_token()),
        asyncio.create_task(auth.resolve_bearer_token()),
    ]
    await asyncio.wait_for(spawned.wait(), timeout=1)
    assert calls == 1
    assert not any(waiter.done() for waiter in waiters)

    _write_token(
        token_path,
        access_token=OAUTH_SENTINEL,
        expires_at=time.time() + 3600,
    )
    process.release.set()

    assert await asyncio.gather(*waiters) == [OAUTH_SENTINEL, OAUTH_SENTINEL]
    assert process.wait_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "did not create"),
        ("not-json", "malformed"),
        (b"\xff\xfe", "malformed"),
        ('{"access_token":" ","refresh_token":" "}', "unusable"),
    ],
)
async def test_successful_login_requires_usable_reloaded_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str | bytes | None,
    message: str,
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    token_path = tmp_path / "kimi-code.json"
    process = _Process()

    async def spawn(*args: object, **kwargs: object) -> _Process:
        if isinstance(raw, bytes):
            token_path.write_bytes(raw)
        elif raw is not None:
            token_path.write_text(raw)
        process.release.set()
        return process

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    with pytest.raises(KimiError, match=message):
        await auth.resolve_bearer_token()
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_invalid_utf8_artifact_starts_login_then_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    token_path = tmp_path / "kimi-code.json"
    token_path.write_bytes(b"\xff\xfe")
    process = _Process()
    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        _write_token(
            token_path,
            access_token=OAUTH_SENTINEL,
            expires_at=time.time() + 3600,
        )
        process.release.set()
        return process

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    assert await auth.resolve_bearer_token() == OAUTH_SENTINEL
    assert login_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_login_failures_are_classified_without_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    child_secret = b"child-secret-shaped-access-token"

    async def missing(*args: object, **kwargs: object) -> _Process:
        raise FileNotFoundError("seeded-secret")

    auth = KimiOAuthAuth(
        credentials_path=tmp_path / "missing.json",
        login_coordinator=KimiLoginCoordinator(process_factory=missing),
    )
    with pytest.raises(KimiError) as caught:
        await auth.resolve_bearer_token()
    assert "seeded-secret" not in str(caught.value)

    process = _Process(
        return_code=9,
        stdout=(child_secret,),
        stderr=(child_secret,),
    )

    async def failed(*args: object, **kwargs: object) -> _Process:
        return process

    auth = KimiOAuthAuth(
        credentials_path=tmp_path / "still-missing.json",
        login_coordinator=KimiLoginCoordinator(process_factory=failed),
    )
    failed_login = asyncio.create_task(auth.resolve_bearer_token())
    await asyncio.wait_for(process.stdout.read_started.wait(), timeout=0.1)
    await asyncio.wait_for(process.stderr.read_started.wait(), timeout=0.1)
    process.release.set()
    with pytest.raises(KimiError) as caught:
        await failed_login
    assert child_secret.decode() not in str(caught.value)
    assert child_secret.decode() not in caplog.text
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_artifact_io_failure_does_not_start_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    token_path = tmp_path / "kimi-code.json"
    login_calls = 0

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("private-path-detail")

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        return _Process()

    monkeypatch.setattr(Path, "read_text", unreadable)
    auth = KimiOAuthAuth(
        credentials_path=token_path,
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    with pytest.raises(KimiError, match="artifact could not be read") as caught:
        await auth.resolve_bearer_token()

    assert "private-path-detail" not in str(caught.value)
    assert login_calls == 0


@pytest.mark.asyncio
async def test_refresh_failure_after_login_is_not_reclassified_as_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    token_path = tmp_path / "kimi-code.json"
    process = _Process()
    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        token_path.write_text(json.dumps({"refresh_token": "refresh-sentinel"}))
        process.release.set()
        return process

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    with pytest.raises(KimiError, match="refresh returned status 503"):
        await auth.resolve_bearer_token()

    assert login_calls == 1


@pytest.mark.asyncio
async def test_pipe_drains_are_cancelled_after_child_exit() -> None:
    class BlockingPipe:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def read(self, size: int) -> bytes:
            try:
                self.started.set()
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
            return b""

    process = _Process()
    process.stdout = BlockingPipe()
    process.stderr = BlockingPipe()

    async def spawn(*args: object, **kwargs: object) -> _Process:
        return process

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        exit_grace_seconds=0.01,
    )

    login = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(process.stdout.started.wait(), timeout=0.1)
    await asyncio.wait_for(process.stderr.started.wait(), timeout=0.1)
    process.release.set()
    await asyncio.wait_for(login, timeout=0.1)

    assert process.stdout.cancelled.is_set()
    assert process.stderr.cancelled.is_set()


@pytest.mark.asyncio
async def test_usable_refresh_material_never_starts_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    token_path = tmp_path / "kimi-code.json"
    token_path.write_text(json.dumps({"refresh_token": "refresh-sentinel"}))
    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        return _Process()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": OAUTH_SENTINEL, "expires_in": 3600},
        )

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    assert await auth.resolve_bearer_token() == OAUTH_SENTINEL
    assert login_calls == 0


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
        auth._save_artifact({"access_token": OAUTH_SENTINEL})
    assert list(token_path.parent.glob("*.tmp*")) == []


@pytest.mark.asyncio
async def test_refresh_errors_do_not_expose_credentials(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"{OAUTH_SENTINEL} refresh-sentinel")

    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        return _Process()

    auth = KimiOAuthAuth(
        credentials_path=token_path,
        client_factory=lambda: _client(handler),
        login_coordinator=KimiLoginCoordinator(process_factory=spawn),
    )

    with pytest.raises(KimiError) as caught:
        await auth.resolve_bearer_token()
    assert OAUTH_SENTINEL not in str(caught.value)
    assert "refresh-sentinel" not in str(caught.value)
    assert login_calls == 0


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
    assert models.discovery_source == "live"
    assert _models_to_payload(models)["model_discovery_source"] == "live"
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
    assert live.discovery_source == "live"
    assert fallback.discovery_source == "fallback"
    assert _models_to_payload(fallback)["model_discovery_source"] == "fallback"
    assert adapter.model_discovery_source == "fallback"


@pytest.mark.asyncio
async def test_unary_401_refreshes_and_retries_only_once(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token="rejected", expires_at=time.time() + 3600)
    chat_calls = 0
    login_calls = 0

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

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        return _Process()

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(
            credentials_path=token_path,
            client_factory=factory,
            login_coordinator=KimiLoginCoordinator(process_factory=spawn),
        ),
        client_factory=factory,
    )

    with pytest.raises(KimiError) as caught:
        await adapter.create_response(ResponsesRequest(model="kimi-k2.5", input="hi"))
    assert chat_calls == 2
    assert OAUTH_SENTINEL not in str(caught.value)
    assert login_calls == 0


@pytest.mark.asyncio
async def test_upstream_failure_never_starts_login(tmp_path: Path) -> None:
    token_path = tmp_path / "kimi-code.json"
    _write_token(token_path, access_token=OAUTH_SENTINEL, expires_at=time.time() + 3600)
    login_calls = 0

    async def spawn(*args: object, **kwargs: object) -> _Process:
        nonlocal login_calls
        login_calls += 1
        return _Process()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    adapter = KimiAdapter(
        auth=KimiOAuthAuth(
            credentials_path=token_path,
            login_coordinator=KimiLoginCoordinator(process_factory=spawn),
        ),
        client_factory=lambda: _client(handler),
    )

    with pytest.raises(KimiError, match="status 503"):
        await adapter.create_response(ResponsesRequest(model="kimi-k2.5", input="hi"))
    assert login_calls == 0


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
