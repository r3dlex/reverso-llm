"""Experimental direct Codex OAuth provider proof.

This module is intentionally isolated from the existing CLI-backed CodexAdapter.
It proves that Reverso's ProviderAdapter boundary can be satisfied by a direct
ChatGPT/Codex OAuth HTTP backend while keeping the production CLI path intact.

The default endpoint is based on public prior art around the Codex backend. It is
not an official stable OpenAI API contract, so callers should keep this adapter
behind an explicit experimental flag until a follow-up ADR accepts the risk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol
import inspect
import json
import time
import uuid

import httpx

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.auth import AuthResolution, ProviderAuth, redact_mapping
from reverso.protocols.replay import flatten_input, record_input_items
from reverso.protocols.store import ResponseStore

CODEX_DIRECT_API_BASE = "https://chatgpt.com/backend-api/codex"
_FORWARD_TIMEOUT_SECONDS = 300.0
_LIFECYCLE_EVENTS = {"response.created", "response.in_progress"}


class CodexDirectError(RuntimeError):
    """Secret-free direct Codex provider failure."""


class CodexDirectUpstream(Protocol):
    """Minimal upstream contract used to fake-proof direct Codex transport."""

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Create one non-streaming response."""
        ...

    def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded streaming events or chunks."""
        ...

    async def list_models(self, *, token: str) -> list[dict[str, Any]]:
        """Return model rows."""
        ...


class HttpCodexDirectUpstream:
    """HTTP transport for the experimental direct Codex backend.

    The request shape is deliberately Responses-like. Prior-art backends have
    drifted over time, so this class is small and easy to replace after the
    spike captures the exact accepted body/headers.
    """

    def __init__(
        self,
        api_base: str = CODEX_DIRECT_API_BASE,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
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
        payload = dict(body)
        payload["stream"] = False
        async with self._client_factory() as client:
            response = await client.post(
                f"{self._api_base}/responses",
                headers=self._headers(token),
                json=payload,
            )
            if response.status_code >= 400:
                raise CodexDirectError(
                    f"codex direct upstream HTTP {response.status_code}"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise CodexDirectError("codex direct upstream returned non-object JSON")
            return data

    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        payload = dict(body)
        payload["stream"] = True
        async with self._client_factory() as client:
            async with client.stream(
                "POST",
                f"{self._api_base}/responses",
                headers=self._headers(token),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    raise CodexDirectError(
                        f"codex direct upstream HTTP {response.status_code}"
                    )
                parser = _SSEParser()
                async for line in response.aiter_lines():
                    for chunk in parser.feed(line):
                        yield chunk
                for chunk in parser.close():
                    yield chunk

    async def list_models(self, *, token: str) -> list[dict[str, Any]]:
        async with self._client_factory() as client:
            response = await client.get(
                f"{self._api_base}/models",
                headers=self._headers(token),
            )
            if response.status_code >= 400:
                raise CodexDirectError(
                    f"codex direct upstream HTTP {response.status_code}"
                )
            payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise CodexDirectError("codex direct upstream returned no model list")
        return [row for row in rows if isinstance(row, dict)]


class CodexDirectAdapter:
    """Experimental ProviderAdapter that bypasses the Codex CLI process.

    This adapter is not mounted by default. It is a proof surface for the first
    Autobahn slice and must stay opt-in until ADR approval.
    """

    def __init__(
        self,
        auth: ProviderAuth,
        upstream: CodexDirectUpstream | None = None,
        store: ResponseStore | None = None,
    ) -> None:
        if upstream is None:
            raise CodexDirectError(
                "codex direct upstream must be injected; live HTTP is explicit-only"
            )
        self._auth = auth
        self._upstream = upstream
        self._store = store or ResponseStore()

    async def _bearer_token(self) -> str:
        resolution: AuthResolution = self._auth.resolve()
        if not resolution.authenticated:
            details = redact_mapping(dict(resolution.details))
            reason = details.get("reason", "unauthenticated")
            raise CodexDirectError(f"codex direct auth failed: {reason}")
        try:
            token = self._auth.bearer_token()
            if inspect.isawaitable(token):
                token = await token
        except Exception as exc:  # noqa: BLE001 - keep auth failure secret-free.
            raise CodexDirectError(
                f"codex direct auth token unavailable: {type(exc).__name__}"
            ) from exc
        if not token:
            raise CodexDirectError("codex direct auth token unavailable")
        return token

    def _body(
        self, request: ResponsesRequest, *, stream: bool = False
    ) -> dict[str, Any]:
        input_value: Any = request.input
        if isinstance(input_value, str):
            input_value = [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": input_value}],
                }
            ]

        body: dict[str, Any] = {
            "model": request.model,
            "input": input_value,
            "stream": stream,
            "store": False,
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
        body["store"] = False
        return body

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        completed: ResponseEnvelope | None = None
        async for event in self.stream_response(request):
            if event.event != "response.completed":
                continue
            raw = event.data.get("response")
            if not isinstance(raw, dict):
                continue
            completed = ResponseEnvelope(
                id=str(raw.get("id") or f"resp_{uuid.uuid4().hex}"),
                model=str(raw.get("model") or request.model),
                output=raw.get("output") if isinstance(raw.get("output"), list) else [],
                usage=raw.get("usage") if isinstance(raw.get("usage"), dict) else None,
                raw=raw,
            )
        if completed is None:
            raise CodexDirectError("codex direct stream completed without response")
        return completed

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        token = await self._bearer_token()
        response_id = f"resp_{uuid.uuid4().hex}"
        output_text: list[str] = []
        completed: ResponseEnvelope | None = None
        started = False
        synthetic_started = False

        async for chunk in self._upstream.stream_response(
            token=token,
            body=self._body(request, stream=True),
        ):
            event = _event_from_chunk(chunk)
            if event is None:
                continue

            event_response_id = _response_id_from_event(event)
            if event_response_id and not synthetic_started:
                response_id = event_response_id

            if not started and event.event not in _LIFECYCLE_EVENTS:
                synthetic_started = True
                started = True
                for start_event in _synthetic_start_events(
                    response_id=response_id, request=request
                ):
                    yield start_event

            if event.event in _LIFECYCLE_EVENTS:
                if synthetic_started:
                    # We already emitted a synthetic lifecycle because upstream sent
                    # content first. Do not create a second response id for clients.
                    continue
                started = True

            if synthetic_started:
                event = _normalize_response_id(event, response_id)

            if event.event == "response.output_text.delta":
                delta = event.data.get("delta")
                if isinstance(delta, str):
                    output_text.append(delta)
            elif event.event == "response.completed":
                response = event.data.get("response")
                if isinstance(response, dict):
                    if synthetic_started:
                        response = dict(response)
                        response["id"] = response_id
                    completed = _envelope_from_raw(response, request=request)
                    self._store.put_response(completed, record_input_items(request))
            yield event

        if completed is None:
            if not started:
                started = True
                for start_event in _synthetic_start_events(
                    response_id=response_id, request=request
                ):
                    yield start_event
            completed = _text_envelope(
                request=request,
                text="".join(output_text),
                response_id=response_id,
            )
            self._store.put_response(completed, record_input_items(request))
            yield SSEEvent(
                event="response.completed",
                data={"type": "response.completed", "response": completed.raw},
            )

    async def list_models(self) -> ModelList:
        token = await self._bearer_token()
        created = int(time.time())
        rows = []
        for row in await self._upstream.list_models(token=token):
            model_id = row.get("id") or row.get("name") or row.get("model")
            if not isinstance(model_id, str) or not model_id:
                continue
            rows.append(
                {
                    "id": model_id,
                    "object": row.get("object", "model"),
                    "created": row.get("created", created),
                    "owned_by": row.get("owned_by", "openai"),
                }
            )
        return ModelList(data=rows, models=rows)

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise CodexDirectError(f"unknown response id: {response_id}")
        return envelope

    async def list_input_items(self, response_id: str) -> InputItemList:
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items


def experimental_http_codex_direct_adapter(
    auth: ProviderAuth,
    *,
    api_base: str = CODEX_DIRECT_API_BASE,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    store: ResponseStore | None = None,
) -> CodexDirectAdapter:
    """Create the live HTTP proof adapter with explicit caller opt-in.

    Gateway composition must not call this factory without a follow-up ADR that
    accepts the direct-backend risk and mount policy.
    """

    return CodexDirectAdapter(
        auth=auth,
        upstream=HttpCodexDirectUpstream(
            api_base=api_base, client_factory=client_factory
        ),
        store=store,
    )


def _synthetic_start_events(
    *, response_id: str, request: ResponsesRequest
) -> list[SSEEvent]:
    return [
        SSEEvent(
            event="response.created",
            data={
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "status": "in_progress",
                    "model": request.model,
                    "output": [],
                },
            },
        ),
        SSEEvent(
            event="response.in_progress",
            data={"type": "response.in_progress", "response": {"id": response_id}},
        ),
    ]


def _response_id_from_event(event: SSEEvent) -> str | None:
    response = event.data.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"]
    event_id = event.data.get("id")
    if isinstance(event_id, str) and event_id.startswith("resp_"):
        return event_id
    return None


def _normalize_response_id(event: SSEEvent, response_id: str) -> SSEEvent:
    response = event.data.get("response")
    if not isinstance(response, dict):
        return event
    data: dict[str, Any] = dict(event.data)
    normalized_response: dict[str, Any] = dict(response)
    normalized_response["id"] = response_id
    data["response"] = normalized_response
    return SSEEvent(event=event.event, data=data, raw=None)


def _envelope_from_raw(
    raw: dict[str, Any], *, request: ResponsesRequest
) -> ResponseEnvelope:
    response_id = str(raw.get("id") or f"resp_{uuid.uuid4().hex}")
    model = str(raw.get("model") or request.model)
    output = raw.get("output")
    if not isinstance(output, list):
        output = _message_output(_extract_text(raw))
    status = str(raw.get("status") or "completed")
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    envelope_raw = {
        "id": response_id,
        "object": raw.get("object", "response"),
        "status": status,
        "model": model,
        "output": output,
    }
    if usage is not None:
        envelope_raw["usage"] = usage
    return ResponseEnvelope(
        id=response_id,
        model=model,
        output=output,
        status=status,
        usage=usage,
        previous_response_id=request.previous_response_id,
        raw=envelope_raw,
    )


def _text_envelope(
    *, request: ResponsesRequest, text: str, response_id: str
) -> ResponseEnvelope:
    input_tokens = len(flatten_input(request.input).split())
    output_tokens = len(text.split())
    raw = {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "model": request.model,
        "output": _message_output(text),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return ResponseEnvelope(
        id=response_id,
        model=request.model,
        output=raw["output"],
        usage=raw["usage"],
        previous_response_id=request.previous_response_id,
        raw=raw,
    )


def _message_output(text: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    ]


def _extract_text(raw: dict[str, Any]) -> str:
    for key in ("text", "content", "message"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]
    return ""


def _event_from_chunk(chunk: dict[str, Any]) -> SSEEvent | None:
    event_name = chunk.get("event") or chunk.get("type")
    if not isinstance(event_name, str) or not event_name:
        return None
    raw_data = chunk.get("data")
    if isinstance(raw_data, dict):
        data: dict[str, Any] = dict(raw_data)
    else:
        data = {k: v for k, v in chunk.items() if k != "event"}
    if "type" not in data:
        data = {"type": event_name, **data}
    return SSEEvent(event=event_name, data=data, raw=None)


class _SSEParser:
    """Stateful parser for standard SSE event/data framing."""

    def __init__(self) -> None:
        self._event: str | None = None
        self._data_lines: list[str] = []

    def feed(self, line: str) -> list[dict[str, Any]]:
        stripped = line.rstrip("\r")
        if stripped == "":
            return self._dispatch()
        if stripped.startswith(":"):
            return []
        if stripped.startswith("event:"):
            self._event = stripped.removeprefix("event:").strip()
            return []
        if stripped.startswith("data:"):
            self._data_lines.append(stripped.removeprefix("data:").lstrip())
            return []
        return []

    def close(self) -> list[dict[str, Any]]:
        return self._dispatch()

    def _dispatch(self) -> list[dict[str, Any]]:
        if not self._event and not self._data_lines:
            return []
        event = self._event
        data_text = "\n".join(self._data_lines)
        self._event = None
        self._data_lines = []
        if data_text in {"", "[DONE]"}:
            return []
        try:
            decoded = json.loads(data_text)
        except json.JSONDecodeError:
            decoded = {"text": data_text}
        data = decoded if isinstance(decoded, dict) else {"data": decoded}
        event_name = event or data.get("event") or data.get("type")
        if not isinstance(event_name, str) or not event_name:
            return []
        return [{"event": event_name, "data": data}]
