"""Local-loopback OpenAI pass-through provider.

This adapter is deliberately small and direct-HTTP only. It is mounted only by
the composition root's explicit local-loopback opt-in gate and forwards
OpenAI-compatible Responses API calls to the official OpenAI API shape:

- POST /responses
- GET /models

Auth resolution prefers an injected OAuth provider and falls back to an explicit
OPENAI_API_KEY only when present.
"""

from __future__ import annotations

import inspect
import os
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

import httpx

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.adapters.codex import CodexOAuthAuth
from reverso.protocols.adapters.codex_direct import _SSEParser
from reverso.protocols.auth import AuthResolution, ProviderAuth, redact_mapping
from reverso.protocols.replay import record_input_items
from reverso.protocols.store import ResponseStore

OPENAI_API_BASE = "https://api.openai.com/v1"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
_FORWARD_TIMEOUT_SECONDS = 300.0
_AUTH_FALSE_DETAILS = {"reason": "missing_openai_auth"}
_REDACTED = "[redacted]"
_MODEL_OBJECT = "model"
_ALLOWED_MODEL_PREFIXES = ("gpt-",)


class OpenAIPassThroughError(RuntimeError):
    """Secret-free OpenAI pass-through error."""


class OpenAIPassThroughHTTPError(OpenAIPassThroughError):
    """Sanitized upstream HTTP error suitable for pre-commit ASGI responses."""

    public_message = "openai upstream HTTP error"

    def __init__(
        self, status_code: int, payload: Mapping[str, Any] | None = None
    ) -> None:
        self.status_code = status_code
        self.payload = _sanitize_error_payload(status_code, payload)
        super().__init__(f"openai upstream HTTP {status_code}")


class OpenAIPassThroughAuth:
    """OAuth-first, explicit API-key fallback auth for OpenAI pass-through."""

    def __init__(
        self,
        oauth_auth: ProviderAuth | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._oauth_auth = oauth_auth
        self._env = env

    @property
    def _source(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def resolve(self) -> AuthResolution:
        oauth_resolution = self._resolve_oauth()
        if oauth_resolution.authenticated:
            return AuthResolution(
                authenticated=True,
                method="oauth",
                subscription_type=oauth_resolution.subscription_type,
                details={**dict(oauth_resolution.details), "source": "oauth"},
            )
        if self._api_key():
            return AuthResolution(
                authenticated=True,
                method="api-key",
                details={"source": OPENAI_API_KEY_ENV},
            )
        details = dict(_AUTH_FALSE_DETAILS)
        if oauth_resolution.details:
            details["oauth"] = redact_mapping(dict(oauth_resolution.details))
        return AuthResolution(authenticated=False, method="none", details=details)

    def bearer_token(self) -> str:
        oauth_resolution = self._resolve_oauth()
        if oauth_resolution.authenticated and self._oauth_auth is not None:
            token = self._oauth_auth.bearer_token()
            if inspect.isawaitable(token):
                raise OpenAIPassThroughError("openai oauth bearer token is async")
            return str(token)
        api_key = self._api_key()
        if api_key:
            return api_key
        raise OpenAIPassThroughError("openai auth unavailable")

    def _resolve_oauth(self) -> AuthResolution:
        if self._oauth_auth is None:
            return AuthResolution(authenticated=False, method="none", details={})
        try:
            return self._oauth_auth.resolve()
        except Exception as exc:  # noqa: BLE001 - keep auth diagnostics secret-free.
            return AuthResolution(
                authenticated=False,
                method="oauth-error",
                details={"reason": f"oauth_error:{type(exc).__name__}"},
            )

    def _api_key(self) -> str | None:
        raw = self._source.get(OPENAI_API_KEY_ENV)
        if raw is None:
            return None
        stripped = raw.strip()
        return stripped or None


class OpenAIPassThroughUpstream(Protocol):
    """HTTP boundary for fake-proof unit tests and direct upstream calls."""

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Return one non-streaming OpenAI response payload."""

    def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield OpenAI streaming response event payloads."""

    async def list_models(self, *, token: str) -> dict[str, Any]:
        """Return the OpenAI model list payload."""


class HttpOpenAIUpstream:
    """Direct HTTP client for the official OpenAI API."""

    def __init__(
        self,
        api_base: str = OPENAI_API_BASE,
        client_factory: Any | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SECONDS)
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._client_factory() as client:
            response = await client.post(
                f"{self._api_base}/responses",
                headers=self._headers(token),
                json=body,
            )
        if response.status_code >= 400:
            raise _http_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise OpenAIPassThroughError("openai non-object JSON")
        return payload

    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        async with (
            self._client_factory() as client,
            client.stream(
                "POST",
                f"{self._api_base}/responses",
                headers=self._headers(token),
                json=body,
            ) as response,
        ):
            if response.status_code >= 400:
                raise _http_error(response)
            parser = _SSEParser()
            async for line in response.aiter_lines():
                for event in parser.feed(line):
                    yield event
            for event in parser.close():
                yield event

    async def list_models(self, *, token: str) -> dict[str, Any]:
        async with self._client_factory() as client:
            response = await client.get(
                f"{self._api_base}/models",
                headers=self._headers(token),
            )
        if response.status_code >= 400:
            raise _http_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise OpenAIPassThroughError("openai non-object JSON")
        return payload


class OpenAIPassThroughAdapter:
    """ProviderAdapter for the local `/openai/v1/...` pass-through surface."""

    def __init__(
        self,
        auth: ProviderAuth,
        upstream: OpenAIPassThroughUpstream | None = None,
        store: ResponseStore | None = None,
    ) -> None:
        self._auth = auth
        self._upstream = upstream or HttpOpenAIUpstream()
        self._store = store or ResponseStore()

    async def _bearer_token(self) -> str:
        resolution = self._auth.resolve()
        if not resolution.authenticated:
            details = redact_mapping(dict(resolution.details))
            reason = details.get("reason", "unauthenticated")
            raise OpenAIPassThroughError(f"openai auth unavailable: {reason}")
        try:
            token = self._auth.bearer_token()
            if inspect.isawaitable(token):
                token = await token
            token = str(token).strip()
        except Exception as exc:
            raise OpenAIPassThroughError(
                f"openai bearer token unavailable: {type(exc).__name__}"
            ) from exc
        if not token:
            raise OpenAIPassThroughError("openai bearer token unavailable")
        return token

    def _body(self, request: ResponsesRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "input": request.input,
            "stream": stream,
        }
        if request.previous_response_id:
            body["previous_response_id"] = request.previous_response_id
        if request.instructions:
            body["instructions"] = request.instructions
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        body.update(request.extra)
        return body

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        token = await self._bearer_token()
        raw = await self._upstream.create_response(
            token=token, body=self._body(request, stream=False)
        )
        envelope = _response_envelope_from_openai(raw, request)
        self._store.put_response(envelope, record_input_items(request))
        return envelope

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        token = await self._bearer_token()
        completed: ResponseEnvelope | None = None
        async for raw_event in self._upstream.stream_response(
            token=token, body=self._body(request, stream=True)
        ):
            event = _sse_event_from_openai(raw_event)
            if _is_completed_event(event):
                completed = _completed_envelope_from_event(event, request)
            yield event
        if completed is not None:
            self._store.put_response(completed, record_input_items(request))

    async def list_models(self) -> ModelList:
        token = await self._bearer_token()
        payload = await self._upstream.list_models(token=token)
        raw_rows = payload.get("data")
        if not isinstance(raw_rows, list):
            raise OpenAIPassThroughError("openai models payload missing data")
        created = int(time.time())
        rows: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id") or row.get("name") or row.get("model")
            if not isinstance(model_id, str) or not _is_allowed_model_id(model_id):
                continue
            rows.append(
                {
                    "id": model_id,
                    "object": row.get("object", _MODEL_OBJECT),
                    "created": row.get("created", created),
                    "owned_by": row.get("owned_by", "openai"),
                }
            )
        return ModelList(data=rows)

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise OpenAIPassThroughError(f"unknown response {response_id}")
        return envelope

    async def get_input_items(self, response_id: str) -> InputItemList:
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items


def build_openai_pass_through_adapter(
    api_base: str = OPENAI_API_BASE,
    auth: ProviderAuth | None = None,
    upstream: OpenAIPassThroughUpstream | None = None,
) -> OpenAIPassThroughAdapter:
    """Build the opt-in OpenAI pass-through adapter for the composition root."""

    return OpenAIPassThroughAdapter(
        auth=auth or OpenAIPassThroughAuth(oauth_auth=CodexOAuthAuth()),
        upstream=upstream or HttpOpenAIUpstream(api_base=api_base),
    )


def _response_envelope_from_openai(
    raw: dict[str, Any], request: ResponsesRequest
) -> ResponseEnvelope:
    response_id = str(raw.get("id") or f"resp_{uuid.uuid4().hex}")
    model = str(raw.get("model") or request.model)
    output = raw.get("output") if isinstance(raw.get("output"), list) else []
    status = str(raw.get("status") or "completed")
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    return ResponseEnvelope(
        id=response_id,
        model=model,
        output=output,
        status=status,
        usage=usage,
        previous_response_id=request.previous_response_id,
        raw=dict(raw),
    )


def _sse_event_from_openai(raw: dict[str, Any]) -> SSEEvent:
    event_type = str(raw.get("event") or raw.get("type") or "response.event")
    data = raw.get("data")
    if isinstance(data, dict):
        event_data = data
    else:
        event_data = {
            key: value for key, value in raw.items() if key not in {"event", "data"}
        }
    if "type" not in event_data:
        event_data = {"type": event_type, **event_data}
    return SSEEvent(event=event_type, data=event_data)


def _is_completed_event(event: SSEEvent) -> bool:
    return event.event == "response.completed"


def _completed_envelope_from_event(
    event: SSEEvent, request: ResponsesRequest
) -> ResponseEnvelope | None:
    response = event.data.get("response")
    if isinstance(response, dict):
        return _response_envelope_from_openai(response, request)
    if isinstance(event.data.get("id"), str):
        return _response_envelope_from_openai(dict(event.data), request)
    return None


def _is_allowed_model_id(model_id: str) -> bool:
    return model_id.startswith(_ALLOWED_MODEL_PREFIXES)


def _http_error(response: httpx.Response) -> OpenAIPassThroughHTTPError:
    payload: Mapping[str, Any] | None
    try:
        decoded = response.json()
    except ValueError:
        decoded = None
    payload = decoded if isinstance(decoded, Mapping) else None
    return OpenAIPassThroughHTTPError(response.status_code, payload)


def _sanitize_error_payload(
    status_code: int, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    if payload is not None:
        sanitized = _redact_value(payload)
        if isinstance(sanitized, dict):
            return sanitized
    return {
        "error": {
            "message": f"openai upstream HTTP {status_code}",
            "type": "upstream_error",
        }
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if any(
                marker in lowered
                for marker in (
                    "authorization",
                    "api_key",
                    "token",
                    "secret",
                    "password",
                )
            ):
                redacted[text_key] = _REDACTED
            else:
                redacted[text_key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
