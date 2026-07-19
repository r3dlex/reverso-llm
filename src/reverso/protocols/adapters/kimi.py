"""Kimi Code subscription adapter using OAuth bearer credentials.

Kimi Code exposes an OpenAI-compatible chat-completions endpoint, while its
official Python SDK is an agent-workflow abstraction rather than a Responses or
Anthropic Messages transport. Reverso therefore reuses its existing chat-to-
Responses adapter seam and reads the OAuth artifact written by ``kimi /login``.
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
from math import isfinite
from pathlib import Path
from typing import Any

import httpx

from reverso.protocols.adapter import ModelList, ResponseEnvelope, ResponsesRequest
from reverso.protocols.adapters.deepseek import DeepSeekAdapter
from reverso.protocols.openai_chat import parse_stream_event as _parse_stream_event

KIMI_API_BASE = "https://api.kimi.com/coding/v1"
KIMI_BEARER_TOKEN_ENV = "KIMI_BEARER_TOKEN"
KIMI_OAUTH_HOST = "https://auth.kimi.com"
KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_DEFAULT_MODEL = "kimi-k2.5"
_REFRESH_MARGIN_SECONDS = 300
_FORWARD_TIMEOUT_SECONDS = 300.0
_KIMI_CODE_PLATFORM = "kimi_code_cli"

logger = logging.getLogger(__name__)


class KimiError(RuntimeError):
    """Secret-free Kimi authentication or upstream failure."""


class KimiOAuthAuth:
    """Resolve and refresh the OAuth bearer artifact written by Kimi CLI."""

    def __init__(
        self,
        *,
        credentials_path: Path | None = None,
        oauth_host: str = KIMI_OAUTH_HOST,
        client_factory: Any | None = None,
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

    def _load_artifact(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

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
        if isinstance(expires_in_value, bool):
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

    async def resolve_bearer_token(self, *, force_refresh: bool = False) -> str:
        """Prefer Kimi CLI OAuth, falling back to an explicit bearer token."""
        artifact = self._load_artifact()
        if artifact is not None:
            access_token = artifact.get("access_token")
            expiry_valid, expires_at = self._expiry(artifact)
            has_access = isinstance(access_token, str) and bool(access_token.strip())
            needs_refresh = (
                expiry_valid
                and expires_at is not None
                and expires_at <= time.time() + _REFRESH_MARGIN_SECONDS
            )
            if expiry_valid and (force_refresh or needs_refresh):
                async with self._refresh_lock:
                    latest = self._load_artifact() or artifact
                    latest_token = latest.get("access_token")
                    latest_valid, latest_expiry = self._expiry(latest)
                    token_rotated = (
                        isinstance(latest_token, str)
                        and latest_token
                        and latest_token != access_token
                    )
                    latest_is_fresh = latest_valid and (
                        latest_expiry is None
                        or latest_expiry > time.time() + _REFRESH_MARGIN_SECONDS
                    )
                    if token_rotated or (not force_refresh and latest_is_fresh):
                        if isinstance(latest_token, str) and latest_token:
                            return latest_token
                    refreshed = await self._refresh(latest)
                    if refreshed:
                        return refreshed
                    if force_refresh:
                        raise KimiError("kimi OAuth credential cannot be refreshed")
            if expiry_valid and not needs_refresh and has_access:
                return access_token
        bearer = os.environ.get(KIMI_BEARER_TOKEN_ENV, "").strip()
        if bearer:
            return bearer
        raise KimiError(
            "Kimi OAuth credentials are unavailable; run kimi /login or set "
            "KIMI_BEARER_TOKEN"
        )


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

    async def _headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = await self._auth.resolve_bearer_token(force_refresh=force_refresh)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Msh-Platform": _KIMI_CODE_PLATFORM,
        }

    def _build_body(self, request: ResponsesRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)
        body["model"] = request.model or KIMI_DEFAULT_MODEL
        return body

    async def _post(
        self, body: dict[str, Any], *, force_refresh: bool = False
    ) -> httpx.Response:
        headers = await self._headers(force_refresh=force_refresh)
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
                headers = await self._headers(force_refresh=attempt == 1)
                async with self._client_factory() as client:
                    async with client.stream(
                        "POST",
                        f"{self._api_base}/chat/completions",
                        headers=headers,
                        content=json.dumps(body).encode("utf-8"),
                    ) as response:
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
            headers = await self._headers()
            async with self._client_factory() as client:
                response = await client.get(f"{self._api_base}/models", headers=headers)
            if response.status_code == 401:
                headers = await self._headers(force_refresh=True)
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
                data = []
                seen: set[str] = set()
                for row in payload["data"]:
                    if not isinstance(row, dict):
                        continue
                    model_id = row.get("id")
                    if (
                        not isinstance(model_id, str)
                        or not model_id
                        or model_id in seen
                    ):
                        continue
                    seen.add(model_id)
                    data.append(
                        {
                            "id": model_id,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": row.get("owned_by", "moonshotai"),
                        }
                    )
                if data:
                    self._model_discovery_source = "live"
                    return ModelList(data=data)
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
            ]
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
