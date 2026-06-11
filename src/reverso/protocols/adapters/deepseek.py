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
    replay_incremental,
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
# ``text`` and ``max_output_tokens`` are Responses-shape and are translated by
# _translate_extras into their chat equivalents (response_format/max_tokens)
# before forwarding, so they MUST NOT be forwarded raw alongside their
# translation.
_NON_FORWARDED_EXTRA = frozenset(
    {
        "input",
        "instructions",
        "model",
        "messages",
        "stream",
        "tools",
        "tool_choice",
        "text",
        "max_output_tokens",
    }
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

        When ``request.input`` is an item list that carries any function_call
        or function_call_output item (the shape codex resends on the second
        turn of a tool loop), a structural walk is used instead of the bare
        flatten_input collapse so the upstream chat API can see the prior
        assistant tool_calls message and the role=tool result message. Without
        this the upstream never sees the tool result and keeps re-issuing the
        same call indefinitely (the E2E run3 deepseek loop). Text-only input
        lists still go through flatten_input so all existing message-only
        fixtures stay byte-for-byte unchanged.
        """
        messages: list[dict[str, Any]] = []
        if request.instructions:
            messages.append({"role": "system", "content": request.instructions})

        prior = self._prior_turn(request.previous_response_id)
        if prior is not None:
            messages.extend(prior)

        if _has_tool_items(request.input):
            messages.extend(_walk_input_items(request.input))
            return messages

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
        if stream:
            # stream_options.include_usage=true is REQUIRED on the streaming
            # branch (ADR 0004): without it the deepseek OpenAI-compat layer
            # leaves the terminal chunk's usage object null, so the completed
            # envelope reports zero tokens and Codex's token-tracking surface
            # breaks. Pinned by test_stream_response_terminal_chunk_usage_lands_on_completed_envelope.
            body["stream_options"] = {"include_usage": True}
        if request.tools is not None:
            body["tools"] = _chat_tools(request.tools)
        if request.tool_choice is not None:
            body["tool_choice"] = _chat_tool_choice(request.tool_choice)
        body.update(_translate_extras(request.extra))
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
        usage = _responses_usage(raw.get("usage"))
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

    async def _call_upstream_stream(
        self, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """POST a streaming /chat/completions call; yield parsed chunk dicts.

        Each yielded chunk dict has keys ``text`` (delta.content), ``reasoning_text``
        (delta.reasoning_content), ``tool_calls`` (list of upstream tool_call
        deltas with their ``index``/``function`` shape preserved), ``usage`` (the
        terminal chunk's translated Responses usage object, or None for non-
        terminal chunks), and ``done`` (True on the chunk that carries a
        finish_reason or on ``data: [DONE]``).

        401/non-2xx received at response.headers BEFORE any SSE byte is read
        raises ``DeepSeekError`` so the gateway can synthesise a structured
        502 (pre-emission branch). Transport errors during body iteration
        raise ``DeepSeekError("deepseek streaming transport error")``. As with
        ``_call_upstream``, only the status code is logged, never headers or
        body, to avoid leaking the API key or any upstream secret.
        """
        headers = self._headers()
        try:
            async with self._client_factory() as client:
                async with client.stream(
                    "POST",
                    f"{self._api_base}/chat/completions",
                    headers=headers,
                    content=json.dumps(body).encode("utf-8"),
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        logger.warning(
                            "deepseek upstream returned %s", response.status_code
                        )
                        raise DeepSeekError(
                            f"deepseek upstream returned status {response.status_code}"
                        )
                    pending = b""
                    async for raw in response.aiter_bytes():
                        if not raw:
                            continue
                        pending += raw
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if not line.startswith(b"data:"):
                                continue
                            payload = line[len(b"data:") :].strip()
                            if not payload:
                                continue
                            if payload == b"[DONE]":
                                yield {
                                    "text": "",
                                    "reasoning_text": "",
                                    "tool_calls": [],
                                    "usage": None,
                                    "done": True,
                                }
                                return
                            try:
                                event = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            parsed = _parse_stream_event(event)
                            if parsed is not None:
                                yield parsed
                                if parsed.get("done"):
                                    return
        except DeepSeekError:
            raise
        except httpx.HTTPError as exc:
            logger.warning("deepseek streaming transport error: %s", type(exc).__name__)
            raise DeepSeekError("deepseek streaming transport error") from exc

    def _finalize_streaming_envelope(
        self,
        request: ResponsesRequest,
        *,
        response_id: str,
        message_id: str,
        full_text: str,
        full_reasoning: str | None,
        usage: dict[str, Any] | None,
        tool_calls: list[dict[str, Any]],
    ) -> ResponseEnvelope:
        """Build the streamed envelope from accumulated chunk state.

        Synthesises a chat-shaped raw dict so the buffered _map_completion
        path's reasoning_content and tool-call surfacing logic runs unchanged,
        then overrides the resulting response_id and message_id so they match
        the ids replay_incremental already announced on the wire. Returning
        the SAME envelope shape the buffered path produces is what lets a
        streamed function_call surface through the per-item events
        replay_incremental adds at finalize-time.
        """
        chat_message: dict[str, Any] = {"role": "assistant", "content": full_text}
        if full_reasoning:
            chat_message["reasoning_content"] = full_reasoning
        if tool_calls:
            chat_message["tool_calls"] = tool_calls
        finish_reason = "tool_calls" if tool_calls else "stop"
        raw = {
            "id": response_id,
            "model": resolve_profile_model("deepseek", request.model or ""),
            "choices": [
                {"index": 0, "message": chat_message, "finish_reason": finish_reason}
            ],
        }
        if usage is not None:
            raw["usage"] = _usage_to_chat(usage)
        envelope = self._map_completion(request, raw)
        # _map_completion mints fresh response_id and message_id; rewrite to
        # the ones replay_incremental already emitted so previous_response_id
        # chaining and the on-wire event ids stay consistent.
        envelope.id = response_id
        envelope.raw["id"] = response_id
        if envelope.output and envelope.output[0].get("type") == "message":
            envelope.output[0]["id"] = message_id
        return envelope

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
        # Incremental streaming (ADR 0004): the adapter opens the upstream
        # stream, parses SSE chunk lines into a chunk async-iterator, and hands
        # it to replay_incremental which owns canonical envelope event
        # emission and the finalize step. Envelope ids are minted by the
        # adapter and threaded through so the on-wire response.created id
        # matches the one persisted in the store at finalize-time.
        #
        # Pre-emission contract: the upstream stream is OPENED (and its
        # response status checked) BEFORE the first replay event yields, so
        # a 401 received at response.headers raises DeepSeekError and the
        # gateway synthesises a structured 502 instead of a truncated 200
        # stream. _call_upstream_stream's first iteration step performs the
        # connect+status check, so priming it here makes that check fire
        # before any prelude event is emitted.
        body = self._build_body(request, stream=True)
        response_id = new_response_id()
        message_id = new_message_id()

        raw_chunks = self._call_upstream_stream(body)
        primed = await _prime_upstream_stream(raw_chunks)

        def finalize(
            *,
            full_text: str,
            full_reasoning: str | None,
            usage: dict[str, Any] | None,
            tool_calls: list[dict[str, Any]],
        ) -> ResponseEnvelope:
            return self._finalize_streaming_envelope(
                request,
                response_id=response_id,
                message_id=message_id,
                full_text=full_text,
                full_reasoning=full_reasoning,
                usage=usage,
                tool_calls=tool_calls,
            )

        async for event in replay_incremental(
            primed,
            response_id=response_id,
            message_id=message_id,
            model=request.model,
            store=self._store,
            input_items=record_input_items(request),
            finalize=finalize,
        ):
            yield event

    async def list_models(self) -> ModelList:
        """Return the live DeepSeek model listing for ``/v1/models``.

        Fetched from the upstream ``/models`` endpoint so the list reflects what
        the account can actually invoke. Falls back to the static
        ``_DEEPSEEK_MODELS`` snapshot when the key is missing or upstream is
        unreachable, so the endpoint never 502s for a listing.
        """
        created = int(time.time())
        try:
            headers = self._headers()
            async with self._client_factory() as client:
                response = await client.get(f"{self._api_base}/models", headers=headers)
            if 200 <= response.status_code < 300:
                payload = response.json()
                data = [
                    {
                        "id": model["id"],
                        "object": "model",
                        "created": created,
                        "owned_by": model.get("owned_by", "deepseek"),
                    }
                    for model in payload.get("data", [])
                    if isinstance(model, dict) and model.get("id")
                ]
                if data:
                    return ModelList(data=data)
            logger.warning(
                "deepseek model listing returned %s; serving static fallback",
                response.status_code,
            )
        except (DeepSeekError, httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "deepseek model listing unavailable (%s); serving static fallback",
                type(exc).__name__,
            )
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


def _has_tool_items(value: Any) -> bool:
    """True when the Responses input list contains a function_call(_output) item."""
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") in {"function_call", "function_call_output"}
        for item in value
    )


def _walk_input_items(items: Any) -> list[dict[str, Any]]:
    """Translate codex's tool-loop input list into chat-completion messages.

    Codex resends the prior turn's transcript on the second leg of a tool
    loop: the original user message, the assistant's function_call echo (with
    call_id/name/arguments), then a function_call_output item (with call_id
    and the executed tool's output). The chat API expects this as an assistant
    message carrying ``tool_calls`` followed by one ``role="tool"`` message per
    call result. Without this translation deepseek never sees the tool result
    and keeps re-issuing the same function_call indefinitely (the E2E run3
    deepseek loop). Consecutive function_call items collapse into ONE assistant
    message so a parallel-tool turn surfaces as a single tool_calls array.
    Reasoning items and any unknown types are skipped.
    """
    out: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    def flush_pending() -> None:
        if pending_tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": list(pending_tool_calls),
                }
            )
            pending_tool_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            pending_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else "",
                    },
                }
            )
            continue
        flush_pending()
        if item_type == "function_call_output":
            call_id = item.get("call_id")
            output = item.get("output")
            if not isinstance(call_id, str):
                continue
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_output_content(output),
                }
            )
            continue
        if item_type == "message" or item.get("role") is not None:
            role = item.get("role")
            if not isinstance(role, str):
                continue
            text = _input_content_text(item)
            out.append({"role": role, "content": text})
            continue
        # reasoning or unknown item types: skip
    flush_pending()
    return out


def _tool_output_content(output: Any) -> str:
    """Coerce a function_call_output ``output`` field into a chat ``content`` string.

    Codex sends a plain string for shell tool outputs, but other tools return
    structured payloads (dict/list); the chat API expects a string ``content``
    on the role=tool message, so structured outputs are JSON-serialised here
    instead of silently dropped. None and non-serialisable values fall back
    to the empty string (the conservative default that preserved behaviour
    before this change).
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _input_content_text(item: dict[str, Any]) -> str:
    """Extract text content from a Responses input item (string or list-of-parts)."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    text = item.get("text")
    return text if isinstance(text, str) else ""


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


def _responses_usage(usage: Any) -> dict[str, Any] | None:
    """Translate chat-completions usage into Responses usage field names.

    Codex parses the terminal response.completed event strictly and fails on
    chat-style ``prompt_tokens``/``completion_tokens`` (missing field
    `input_tokens`), so the chat names must not leak into the envelope.
    """
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _translate_extras(extra: dict[str, Any]) -> dict[str, Any]:
    """Map Responses-shape extras (text.format, max_output_tokens) to chat keys.

    DeepSeek speaks the OpenAI chat-completions surface, so Responses-shape
    fields the gateway forwards need to be rewritten into the chat names
    (response_format and max_tokens). Sampling params (temperature, top_p),
    parallel_tool_calls and response_format already use chat names and are
    forwarded by _build_body unchanged. An explicit response_format in extra
    wins over a translated text.format so a caller can still override the
    derived shape.
    """
    translated: dict[str, Any] = {}
    text = extra.get("text")
    if isinstance(text, dict):
        fmt = text.get("format")
        if isinstance(fmt, dict):
            response_format = _response_format_from_text(fmt)
            if response_format is not None:
                translated["response_format"] = response_format
    max_output_tokens = extra.get("max_output_tokens")
    if isinstance(max_output_tokens, int):
        translated["max_tokens"] = max_output_tokens
    return translated


def _response_format_from_text(fmt: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a Responses text.format object into a chat response_format.

    Responses encodes the JSON-schema body alongside the format type; the chat
    API wraps the schema in a {"type": "json_schema", "json_schema": {...}}
    object, so the wrapper is reconstructed here from the Responses fields.
    """
    fmt_type = fmt.get("type")
    if fmt_type == "text":
        return {"type": "text"}
    if fmt_type == "json_object":
        return {"type": "json_object"}
    if fmt_type == "json_schema":
        schema_body: dict[str, Any] = {}
        for key in ("name", "schema", "strict", "description"):
            value = fmt.get(key)
            if value is not None:
                schema_body[key] = value
        return {"type": "json_schema", "json_schema": schema_body}
    return None


def _chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Responses-format function tools to chat-completions format.

    Codex sends flat ``{"type":"function","name":...,"parameters":...}`` tool
    declarations; DeepSeek's chat API requires the nested ``function`` object
    and returns 400 (missing field `function`) otherwise. Tools already in
    chat format pass through unchanged; non-function tool types are dropped
    because DeepSeek only supports function tools.
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "function" in tool:
            converted.append(tool)
            continue
        if tool.get("type") != "function":
            continue
        function = {
            key: tool[key]
            for key in ("name", "description", "parameters")
            if tool.get(key) is not None
        }
        converted.append({"type": "function", "function": function})
    return converted


def _chat_tool_choice(tool_choice: Any) -> Any:
    """Convert a Responses-format tool_choice to chat-completions format."""
    if (
        isinstance(tool_choice, dict)
        and tool_choice.get("type") == "function"
        and "function" not in tool_choice
    ):
        return {"type": "function", "function": {"name": tool_choice.get("name")}}
    return tool_choice


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


def _parse_stream_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one upstream stream-json event into the replay chunk shape.

    Returns ``None`` for events that should be skipped (no choices payload AND
    no usage block, or a non-null choices entry that yields no content of any
    kind). Note: ``finish_reason`` does NOT mark ``done`` here; deepseek with
    ``stream_options.include_usage`` always emits the terminal usage block
    AFTER the finish_reason chunk and BEFORE the ``[DONE]`` sentinel, so the
    iterator must keep consuming until ``[DONE]`` (the ``[DONE]`` sentinel in
    ``_call_upstream_stream`` is the only thing that yields a chunk with
    ``done=True``).
    """
    usage = event.get("usage")
    choices = event.get("choices")
    text = ""
    reasoning_text = ""
    tool_calls: list[dict[str, Any]] = []
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                text = content
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                reasoning_text = reasoning
            raw_tool_calls = delta.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for call in raw_tool_calls:
                    if isinstance(call, dict):
                        tool_calls.append(call)
    translated_usage = _responses_usage(usage) if isinstance(usage, dict) else None
    if not text and not reasoning_text and not tool_calls and translated_usage is None:
        return None
    return {
        "text": text,
        "reasoning_text": reasoning_text,
        "tool_calls": tool_calls,
        "usage": translated_usage,
        "done": False,
    }


def _usage_to_chat(usage: dict[str, Any]) -> dict[str, Any]:
    """Reverse _responses_usage so the synthesised chat body looks upstream-shaped.

    _map_completion expects chat-style ``prompt_tokens``/``completion_tokens``
    keys when translating usage; the streaming chunk parser already produced
    a Responses-shaped usage object, so we round-trip it here.
    """
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


async def _prime_upstream_stream(
    chunks: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Advance ``chunks`` once so upstream connect+status errors raise eagerly.

    The deepseek streaming runner is an async generator whose `async with
    client.stream(...)` block only enters when iteration starts. Priming the
    first step here ensures any pre-emission failure (401 at response.headers,
    transport error before any byte) raises DeepSeekError BEFORE the caller
    yields the first canonical envelope event, so the gateway can synthesise
    a structured 502. The first usable chunk (if any) is re-injected ahead of
    the remaining iterator so replay_incremental sees the whole sequence.
    """
    try:
        first = await chunks.__anext__()
    except StopAsyncIteration:

        async def empty() -> AsyncIterator[dict[str, Any]]:
            return
            yield  # pragma: no cover - keeps this an async generator

        return empty()

    async def replay() -> AsyncIterator[dict[str, Any]]:
        yield first
        async for chunk in chunks:
            yield chunk

    return replay()
