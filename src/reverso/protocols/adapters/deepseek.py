"""DeepSeek first-party provider adapter (ADR 0002 11.3).

DeepSeek is moved off the legacy LiteLLM stack onto a first-party adapter that
calls the DeepSeek OpenAI-compatible chat-completions API directly. The legacy
config stripped ``response_format`` (and ``reasoning_content`` was lost across
turns); this adapter MUST NOT inherit that stripping. ``response_format`` is
passed THROUGH to the upstream chat request (gating JSON mode) and
``reasoning_content`` is preserved on the envelope and carried forward when a
turn chains via ``previous_response_id`` (gating thinking mode).

DeepSeek is NOT Responses-native, so this adapter translates: Responses
``input``/``instructions`` -> chat ``messages``, calls ``POST /chat/completions``,
and maps the chat response back into Responses ``ResponseEnvelope`` (unary) and
Responses SSE events (stream).

The API key is read from ``DEEPSEEK_API_KEY`` at call time and sent as a bearer
token; it is NEVER logged. Upstream-error diagnostics log only the status code
(never the response headers or body). No repository secret is read or stored.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator

import httpx

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.replay import (
    flatten_input,
    message_item,
    new_message_id,
    new_response_id,
    record_input_items,
    replay_turn,
)
from reverso.protocols.store import ResponseStore
from reverso.proxy.profile_routing import resolve_profile_model

logger = logging.getLogger(__name__)

DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
_FORWARD_TIMEOUT_SECONDS = 300.0

# Models from the live config. list_models() returns these OpenAI-shaped.
_DEEPSEEK_MODELS = (
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-reasoner",
    "deepseek-chat",
)

# Responses request fields the adapter consumes directly, plus the body keys the
# adapter sets itself; everything else in ``extra`` (notably ``response_format``)
# is forwarded UNCHANGED to DeepSeek so the legacy drop_params stripping is not
# reintroduced. The adapter-owned keys are denied so an inbound ``extra`` value
# (e.g. a stray ``messages``) can never clobber the adapter's translated body.
_NON_FORWARDED_EXTRA = frozenset(
    {"input", "instructions", "model", "messages", "stream", "tools", "tool_choice"}
)


class DeepSeekError(RuntimeError):
    """Raised for missing credentials or a failed DeepSeek upstream call.

    Messages are short and secret-free: ``responses_app._safe_error_message``
    surfaces only the class name, but the message itself must also never carry
    the API key or any upstream body that could contain secrets.
    """


class DeepSeekAdapter:
    """ProviderAdapter for DeepSeek over the OpenAI-compatible chat API.

    Unary and streaming Responses calls are translated to DeepSeek
    ``/chat/completions`` and mapped back to Responses shapes. ``response_format``
    survives via the request ``extra`` carry-through and ``reasoning_content`` is
    preserved on the envelope and re-injected when chaining via
    ``previous_response_id``. previous_response_id chaining and ``/input_items``
    are served from the injected in-memory ResponseStore.
    """

    def __init__(
        self,
        store: ResponseStore | None = None,
        *,
        api_base: str = DEEPSEEK_API_BASE,
        client_factory: Any | None = None,
    ) -> None:
        self._store = store or ResponseStore()
        self._api_base = api_base.rstrip("/")
        # Injectable HTTP backend so tests never make a real network call.
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SECONDS)
        )

    def _api_key(self) -> str:
        """Read the API key from the environment at call time (never logged)."""
        key = os.environ.get(DEEPSEEK_API_KEY_ENV)
        if not key:
            raise DeepSeekError("DEEPSEEK_API_KEY is not set")
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _build_messages(self, request: ResponsesRequest) -> list[dict[str, Any]]:
        """Translate Responses input/instructions plus any prior turn into messages.

        When ``previous_response_id`` points at a stored turn that carried
        ``reasoning_content``, that thinking is re-injected as a prior assistant
        message so DeepSeek thinking mode chains correctly (gating thinking).
        """
        messages: list[dict[str, Any]] = []
        if request.instructions:
            messages.append({"role": "system", "content": request.instructions})

        prior = self._prior_turn(request.previous_response_id)
        if prior is not None:
            messages.extend(prior)

        user_text = flatten_input(request.input)
        if user_text:
            messages.append({"role": "user", "content": user_text})
        return messages

    def _prior_turn(
        self, previous_response_id: str | None
    ) -> list[dict[str, Any]] | None:
        """Return prior assistant messages (with reasoning_content) to carry forward."""
        if not previous_response_id:
            return None
        envelope = self._store.get_response(previous_response_id)
        if envelope is None:
            return None
        text = _output_text(envelope.output)
        message: dict[str, Any] = {"role": "assistant", "content": text}
        reasoning = envelope.raw.get("reasoning_content")
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return [message]

    def _build_body(self, request: ResponsesRequest, *, stream: bool) -> dict[str, Any]:
        """Build the outbound DeepSeek chat body, forwarding extra params unchanged.

        ``response_format`` lives in ``request.extra`` (it is not a typed field),
        so the carry-through below passes it to DeepSeek UNCHANGED, defeating the
        legacy drop_params stripping (gating JSON mode).
        """
        # Resolve GPT-level Codex profile names (e.g. gpt-5.5) to concrete
        # DeepSeek model ids, matching the legacy ProfileRoutingMiddleware that
        # the first-party /deepseek path bypasses. Real DeepSeek ids pass through
        # unchanged, so callers may also send them directly.
        body: dict[str, Any] = {
            "model": resolve_profile_model("deepseek", request.model or ""),
            "messages": self._build_messages(request),
            "stream": stream,
        }
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        for key, value in request.extra.items():
            if key in _NON_FORWARDED_EXTRA:
                continue
            body[key] = value
        return body

    def _map_completion(
        self, request: ResponsesRequest, raw: dict[str, Any]
    ) -> ResponseEnvelope:
        """Map a DeepSeek chat-completion body into a Responses ResponseEnvelope.

        DeepSeek is NOT Responses-native, so ``envelope.raw`` is built as a
        Responses object (``object == "response"`` with an ``output`` array), not
        the upstream chat-completions body. Serving the chat body verbatim would
        break the Responses contract (no ``object``/``output``). ``reasoning_content``
        is carried on the Responses body so thinking mode survives both to the
        client and for previous_response_id chaining. The requested model id is
        echoed back (matching the Auggie adapter) so a caller that sent a GPT-level
        profile name does not see the resolved DeepSeek id leak back.
        """
        message = _first_message(raw)
        text = message.get("content") or ""
        response_id = new_response_id()
        output: list[dict[str, Any]] = [message_item(new_message_id(), str(text))]

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                output.append(_tool_call_item(call))

        model = request.model or str(raw.get("model", ""))
        usage = raw.get("usage")
        envelope_raw: dict[str, Any] = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "model": model,
            "output": output,
        }
        if usage is not None:
            envelope_raw["usage"] = usage
        if request.previous_response_id is not None:
            envelope_raw["previous_response_id"] = request.previous_response_id
        reasoning = message.get("reasoning_content")
        if reasoning is not None:
            envelope_raw["reasoning_content"] = reasoning

        return ResponseEnvelope(
            id=response_id,
            model=model,
            output=output,
            status="completed",
            usage=usage,
            previous_response_id=request.previous_response_id,
            raw=envelope_raw,
        )

    async def _call_upstream(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST to DeepSeek /chat/completions, raising bounded DeepSeekError."""
        headers = self._headers()
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{self._api_base}/chat/completions",
                    headers=headers,
                    content=json.dumps(body).encode("utf-8"),
                )
        except httpx.HTTPError as exc:
            logger.warning("deepseek upstream transport error: %s", type(exc).__name__)
            raise DeepSeekError("deepseek upstream request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            # Log only the status code: upstream response headers can carry
            # Set-Cookie / proprietary auth-echo values that redact_mapping does
            # not recognize, so the whole header map is never logged.
            logger.warning("deepseek upstream returned %s", response.status_code)
            raise DeepSeekError(
                f"deepseek upstream returned status {response.status_code}"
            )
        return response.json()

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        """Return a non-streaming Responses object for ``request``."""
        body = self._build_body(request, stream=False)
        raw = await self._call_upstream(body)
        envelope = self._map_completion(request, raw)
        self._store.put_response(envelope, record_input_items(request))
        return envelope

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        """Yield Responses SSE events for ``request`` (stream=True)."""
        return self._stream_response(request)

    async def _stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        # A single upstream chat call backs the stream; the completed turn is
        # then replayed as the canonical Responses SSE sequence.
        body = self._build_body(request, stream=False)
        raw = await self._call_upstream(body)
        envelope = self._map_completion(request, raw)
        async for event in replay_turn(
            envelope, store=self._store, input_items=record_input_items(request)
        ):
            yield event

    async def list_models(self) -> ModelList:
        """Return the DeepSeek model listing for ``/v1/models``."""
        created = int(time.time())
        data = [
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": "deepseek",
            }
            for model_id in _DEEPSEEK_MODELS
        ]
        return ModelList(data=data)

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        """Return a previously created response by id."""
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise DeepSeekError(f"unknown response_id {response_id!r}")
        return envelope

    async def list_input_items(self, response_id: str) -> InputItemList:
        """Return the input items recorded for a prior response id."""
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items


def _first_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the first chat choice's ``message`` dict (empty when absent)."""
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                return message
    return {}


def _output_text(output: list[dict[str, Any]]) -> str:
    """Extract assistant output_text from a stored Responses output list."""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    return text
    return ""


def _tool_call_item(call: dict[str, Any]) -> dict[str, Any]:
    """Surface a DeepSeek tool_call as a Responses function_call output item.

    Tool calls are SURFACED only; the adapter never executes them (no subprocess
    or extra network beyond the single upstream chat call).
    """
    function = call.get("function", {}) if isinstance(call, dict) else {}
    return {
        "id": new_message_id(),
        "type": "function_call",
        "status": "completed",
        "call_id": call.get("id") if isinstance(call, dict) else None,
        "name": function.get("name"),
        "arguments": function.get("arguments"),
    }
