"""Kimi Code subscription adapter using OAuth bearer credentials.

Kimi Code exposes an OpenAI-compatible chat-completions endpoint, while its
official Python SDK is an agent-workflow abstraction rather than a Responses or
Anthropic Messages transport. Reverso therefore reuses its existing chat-to-
Responses adapter seam and reads the OAuth artifact written by ``kimi login``.
An explicit ``KIMI_BEARER_TOKEN`` remains a fallback for non-CLI deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any

import httpx

from reverso.protocols.adapter import ModelList, ResponseEnvelope, ResponsesRequest
from reverso.protocols.adapters.deepseek import DeepSeekAdapter
from reverso.protocols.kimi_login import KimiLoginCoordinator, KimiLoginError
from reverso.protocols.openai_chat import parse_stream_event as _parse_stream_event

KIMI_API_BASE = "https://api.kimi.com/coding/v1"
KIMI_BEARER_TOKEN_ENV = "KIMI_BEARER_TOKEN"
KIMI_OAUTH_HOST = "https://auth.kimi.com"
KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_DEFAULT_MODEL = "kimi-k3"
KIMI_UPSTREAM_MODEL = "k3"
_REFRESH_MARGIN_SECONDS = 300
_FORWARD_TIMEOUT_SECONDS = 300.0
_KIMI_CODE_PLATFORM = "kimi_code_cli"

logger = logging.getLogger(__name__)


class KimiError(RuntimeError):
    """Secret-free Kimi authentication or upstream failure."""

    @property
    def public_message(self) -> str:
        """Return the curated message safe for the gateway error envelope."""
        return str(self)


class KimiModelError(KimiError):
    """Noncanonical Kimi model selection."""

    status_code = 400

    def __init__(self) -> None:
        message = "Kimi supports only kimi-k3"
        super().__init__(message)
        self.payload = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
            }
        }


class _ArtifactState(Enum):
    ABSENT = "absent"
    MALFORMED = "malformed"
    LOADED = "loaded"


class KimiOAuthAuth:
    """Resolve and refresh the OAuth bearer artifact written by Kimi CLI."""

    def __init__(
        self,
        *,
        credentials_path: Path | None = None,
        oauth_host: str = KIMI_OAUTH_HOST,
        client_factory: Any | None = None,
        login_coordinator: KimiLoginCoordinator | None = None,
    ) -> None:
        kimi_home = Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code")
        self._credentials_path = credentials_path or (
            kimi_home / "credentials" / "kimi-code.json"
        )
        self._oauth_host = oauth_host.rstrip("/")
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=30.0)
        )
        self._refresh_lock = asyncio.Lock()
        self._login_coordinator = login_coordinator

    @property
    def credentials_path(self) -> Path:
        return self._credentials_path

    def _read_artifact(self) -> tuple[_ArtifactState, dict[str, Any] | None]:
        try:
            payload = json.loads(self._credentials_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _ArtifactState.ABSENT, None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _ArtifactState.MALFORMED, None
        except OSError as exc:
            raise KimiError("kimi OAuth credential artifact could not be read") from exc
        if not isinstance(payload, dict):
            return _ArtifactState.MALFORMED, None
        return _ArtifactState.LOADED, payload

    def _load_artifact(self) -> dict[str, Any] | None:
        _, artifact = self._read_artifact()
        return artifact

    def _save_artifact(self, payload: dict[str, Any]) -> None:
        path = self._credentials_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f"{path.name}.tmp.",
        )
        try:
            os.chmod(temporary, 0o600)
            encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def _refresh(self, artifact: dict[str, Any]) -> str | None:
        refresh_token = artifact.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return None
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{self._oauth_host}/api/oauth/token",
                    data={
                        "client_id": KIMI_OAUTH_CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={"X-Msh-Platform": _KIMI_CODE_PLATFORM},
                )
        except httpx.HTTPError as exc:
            raise KimiError("kimi OAuth refresh transport failed") from exc
        if response.status_code != 200:
            raise KimiError(
                f"kimi OAuth refresh returned status {response.status_code}"
            )
        try:
            refreshed = response.json()
        except ValueError as exc:
            raise KimiError("kimi OAuth refresh returned invalid JSON") from exc
        access_token = (
            refreshed.get("access_token") if isinstance(refreshed, dict) else None
        )
        if not isinstance(access_token, str) or not access_token.strip():
            raise KimiError("kimi OAuth refresh returned no access token")
        expires_in_value = refreshed.get("expires_in")
        if isinstance(expires_in_value, bool) or not isinstance(
            expires_in_value, (int, float, str)
        ):
            raise KimiError("kimi OAuth refresh returned invalid expiry")
        try:
            expires_in = float(expires_in_value)
        except (TypeError, ValueError) as exc:
            raise KimiError("kimi OAuth refresh returned invalid expiry") from exc
        if not isfinite(expires_in) or expires_in <= 0:
            raise KimiError("kimi OAuth refresh returned invalid expiry")
        persisted = {**artifact, **refreshed}
        persisted["expires_at"] = time.time() + expires_in
        self._save_artifact(persisted)
        return access_token

    @staticmethod
    def _expiry(artifact: dict[str, Any]) -> tuple[bool, float | None]:
        value = artifact.get("expires_at")
        if value is None:
            return True, None
        if isinstance(value, bool):
            return False, None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return False, None
        if not isfinite(parsed):
            return False, None
        return True, parsed

    @staticmethod
    def _usable_token(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _has_usable_refresh(self, artifact: dict[str, Any] | None) -> bool:
        return artifact is not None and self._usable_token(
            artifact.get("refresh_token")
        )

    def _has_usable_access(self, artifact: dict[str, Any] | None) -> bool:
        if artifact is None or not self._usable_token(artifact.get("access_token")):
            return False
        expiry_valid, expires_at = self._expiry(artifact)
        return expiry_valid and (
            expires_at is None or expires_at > time.time() + _REFRESH_MARGIN_SECONDS
        )

    async def _resolve_bearer_token_once(self, *, force_refresh: bool = False) -> str:
        artifact = self._load_artifact()
        if artifact is not None:
            access_token = artifact.get("access_token")
            expiry_valid, expires_at = self._expiry(artifact)
            has_access = self._usable_token(access_token)
            has_refresh = self._has_usable_refresh(artifact)
            access_is_fresh = (
                has_access
                and expiry_valid
                and (
                    expires_at is None
                    or expires_at > time.time() + _REFRESH_MARGIN_SECONDS
                )
            )
            # A usable refresh credential is authoritative even when the access
            # credential is absent. It must stay on the refresh path, never login.
            if force_refresh or (has_refresh and not access_is_fresh):
                async with self._refresh_lock:
                    latest = self._load_artifact() or artifact
                    latest_token = latest.get("access_token")
                    latest_valid, latest_expiry = self._expiry(latest)
                    latest_has_refresh = self._has_usable_refresh(latest)
                    token_rotated = (
                        self._usable_token(latest_token)
                        and latest_token != access_token
                    )
                    latest_is_fresh = (
                        self._usable_token(latest_token)
                        and latest_valid
                        and (
                            latest_expiry is None
                            or latest_expiry > time.time() + _REFRESH_MARGIN_SECONDS
                        )
                    )
                    if token_rotated or (not force_refresh and latest_is_fresh):
                        return str(latest_token)
                    if not latest_has_refresh:
                        if force_refresh:
                            raise KimiError("kimi OAuth credential cannot be refreshed")
                    else:
                        refreshed = await self._refresh(latest)
                        if refreshed:
                            return refreshed
                        if force_refresh:
                            raise KimiError("kimi OAuth credential cannot be refreshed")
            if access_is_fresh:
                return str(access_token)
        bearer = os.environ.get(KIMI_BEARER_TOKEN_ENV, "").strip()
        if bearer:
            return bearer
        raise KimiError(
            "Kimi OAuth credentials are unavailable; run kimi login or set "
            "KIMI_BEARER_TOKEN"
        )

    async def resolve_bearer_token(self, *, force_refresh: bool = False) -> str:
        """Resolve credentials, starting one shared CLI login only when needed."""
        try:
            return await self._resolve_bearer_token_once(force_refresh=force_refresh)
        except KimiError:
            if force_refresh or self._login_coordinator is None:
                raise
            # A bearer fallback, refresh failure, or upstream-triggered forced
            # refresh never reaches this branch. Only locally missing auth starts
            # the official CLI flow.
            if os.environ.get(KIMI_BEARER_TOKEN_ENV, "").strip():
                raise
            _state, artifact = self._read_artifact()
            if self._has_usable_refresh(artifact):
                raise
            try:
                await self._login_coordinator.ensure_authenticated()
            except KimiLoginError as login_exc:
                raise KimiError(login_exc.public_message) from login_exc
            # Do not recursively start another login when the CLI exits without a
            # usable artifact; return the post-login error to this request.
            state, artifact = self._read_artifact()
            if state is _ArtifactState.ABSENT:
                raise KimiError("kimi login did not create the credential artifact")
            if state is _ArtifactState.MALFORMED:
                raise KimiError("kimi login created a malformed credential artifact")
            if not (
                self._has_usable_access(artifact) or self._has_usable_refresh(artifact)
            ):
                raise KimiError("kimi login created an unusable credential artifact")
            return await self._resolve_bearer_token_once(force_refresh=False)


class KimiAdapter(DeepSeekAdapter):
    """ProviderAdapter for Kimi's OpenAI-compatible chat API."""

    def __init__(
        self,
        store: Any | None = None,
        *,
        auth: KimiOAuthAuth | None = None,
        api_base: str = KIMI_API_BASE,
        client_factory: Any | None = None,
    ) -> None:
        super().__init__(
            store,
            api_base=api_base,
            client_factory=client_factory
            or (lambda: httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SECONDS)),
        )
        self._auth = auth or KimiOAuthAuth()
        self._model_discovery_source = "fallback"

    @property
    def model_discovery_source(self) -> str:
        """Report whether the most recent model list came from live Kimi."""
        return self._model_discovery_source

    async def _async_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = await self._auth.resolve_bearer_token(force_refresh=force_refresh)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Msh-Platform": _KIMI_CODE_PLATFORM,
        }

    def _build_body(self, request: ResponsesRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)
        if request.model and request.model != KIMI_DEFAULT_MODEL:
            raise KimiModelError
        body["model"] = KIMI_UPSTREAM_MODEL
        return body

    async def _post(
        self, body: dict[str, Any], *, force_refresh: bool = False
    ) -> httpx.Response:
        headers = await self._async_headers(force_refresh=force_refresh)
        try:
            async with self._client_factory() as client:
                return await client.post(
                    f"{self._api_base}/chat/completions",
                    headers=headers,
                    content=json.dumps(body).encode("utf-8"),
                )
        except httpx.HTTPError as exc:
            raise KimiError("kimi upstream request failed") from exc

    async def _call_upstream(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._post(body)
        if response.status_code == 401:
            response = await self._post(body, force_refresh=True)
        if not 200 <= response.status_code < 300:
            logger.warning("kimi upstream returned %s", response.status_code)
            raise KimiError(f"kimi upstream returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise KimiError("kimi upstream returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise KimiError("kimi upstream returned non-object JSON")
        return payload

    async def _call_upstream_stream(
        self, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            for attempt in range(2):
                headers = await self._async_headers(force_refresh=attempt == 1)
                async with (
                    self._client_factory() as client,
                    client.stream(
                        "POST",
                        f"{self._api_base}/chat/completions",
                        headers=headers,
                        content=json.dumps(body).encode("utf-8"),
                    ) as response,
                ):
                    if response.status_code == 401 and attempt == 0:
                        continue
                    if not 200 <= response.status_code < 300:
                        logger.warning(
                            "kimi upstream returned %s", response.status_code
                        )
                        raise KimiError(
                            f"kimi upstream returned status {response.status_code}"
                        )
                    pending = b""
                    async for raw in response.aiter_bytes():
                        if not raw:
                            continue
                        pending += raw
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            parsed = _parse_kimi_stream_line(line)
                            if parsed is not None:
                                yield parsed
                                if parsed.get("done"):
                                    return
                    if pending:
                        parsed = _parse_kimi_stream_line(pending)
                        if parsed is not None:
                            yield parsed
                    return
        except KimiError:
            raise
        except httpx.HTTPError as exc:
            raise KimiError("kimi streaming transport failed") from exc

    def _finalize_streaming_envelope(
        self, request: ResponsesRequest, **kwargs: Any
    ) -> ResponseEnvelope:
        # The inherited implementation only uses its DeepSeek resolver to fill a
        # temporary upstream model field; _map_completion echoes request.model.
        return super()._finalize_streaming_envelope(request, **kwargs)

    async def list_models(self) -> ModelList:
        self._model_discovery_source = "fallback"
        try:
            headers = await self._async_headers()
            async with self._client_factory() as client:
                response = await client.get(f"{self._api_base}/models", headers=headers)
            if response.status_code == 401:
                headers = await self._async_headers(force_refresh=True)
                async with self._client_factory() as client:
                    response = await client.get(
                        f"{self._api_base}/models", headers=headers
                    )
            if 200 <= response.status_code < 300:
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("data"), list
                ):
                    raise ValueError("invalid model listing")
                has_k3 = False
                owned_by = "moonshotai"
                for row in payload["data"]:
                    if not isinstance(row, dict):
                        continue
                    model_id = row.get("id")
                    if model_id == KIMI_UPSTREAM_MODEL:
                        has_k3 = True
                        owned_by = row.get("owned_by", owned_by)
                if has_k3:
                    self._model_discovery_source = "live"
                    return ModelList(
                        data=[
                            {
                                "id": KIMI_DEFAULT_MODEL,
                                "object": "model",
                                "created": int(time.time()),
                                "owned_by": owned_by,
                            }
                        ],
                        discovery_source="live",
                    )
        except (KimiError, httpx.HTTPError, ValueError) as exc:
            logger.warning("kimi model listing unavailable (%s)", type(exc).__name__)
        return ModelList(
            data=[
                {
                    "id": KIMI_DEFAULT_MODEL,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "moonshotai",
                }
            ],
            discovery_source="fallback",
        )


def _parse_kimi_stream_line(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line or not line.startswith(b"data:"):
        return None
    payload = line[len(b"data:") :].strip()
    if not payload:
        return None
    if payload == b"[DONE]":
        return {
            "text": "",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": True,
        }
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return _parse_stream_event(event)
