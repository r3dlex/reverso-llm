"""Stateless Anthropic Messages <-> Responses translation (ADR 0006 D1, G003).

Pure functions that map an inbound Anthropic Messages request onto the FROZEN
Responses contract (``ResponsesRequest``) and map a ``ResponseEnvelope`` back into
an Anthropic Messages response body. The module is stateless: it holds no
conversation state (that rides the existing in-memory ResponseStore through the
Responses contract) and performs no ASGI or network work, so it is reused by both
the non-streaming handler (G003) and the SSE streaming mapper (G004) and is
exercised directly by the unit tests.

This is the NON-STREAMING translation core plus the consolidated request
PREPARATION seam. ``prepare_anthropic_request`` composes the whole pre-dispatch
pipeline in today's exact order -- strip degradable features, per-backend
capability gating (G005, imported from anthropic_feature_gate, which stays the
single capability seam), then the Anthropic -> Responses translation -- so the
"stripped payload is what the gate and the adapter both observe" invariant is
owned here instead of by inline ordering in the ASGI app. count_tokens (G006)
and /v1/models (G006) are NOT handled here; the bare translation function keeps
passing image blocks through (gating happens in prepare, not in translation).

Request mapping (anthropic_request_to_responses):
  - ``model`` -> ResponsesRequest.model.
  - ``system`` (a STRING or a LIST of content blocks) -> instructions, with the
    text of each block concatenated.
  - ``messages[].content`` blocks -> Responses input items:
      * text -> message item with an input_text/output_text part,
      * tool_use (assistant) -> function_call item preserving call_id (the
        Anthropic tool_use id) / name / arguments,
      * tool_result (user) -> function_call_output item preserving call_id (the
        tool_use_id), with ``is_error`` carried OUT-OF-BAND as a structural
        ``is_error: true`` field on the item (and an in-band hint kept in the
        output text) so a downstream adapter sees the error both structurally and
        in the rendered output,
      * image -> input_image part, passed through (gating is G005).
  - ``tools`` (input_schema -> function ``parameters``) -> Responses function
    tools; ``tool_choice`` (auto/any/none/tool) -> Responses tool_choice, with a
    name-absent ``tool`` choice falling back to ``"required"``.
  - ``max_tokens`` / ``temperature`` / ``stop_sequences`` -> ResponsesRequest.extra.

Response mapping (responses_envelope_to_anthropic):
  - ``id`` -> ``"msg_" + envelope.id.removeprefix("resp_")``.
  - ``type`` "message", ``role`` "assistant".
  - message text output items -> text content blocks (empty ones filtered);
    function_call output items -> tool_use content blocks (id/name/input).
  - ``stop_reason`` mapped (end_turn / tool_use / max_tokens / stop_sequence; an
    unknown reason falls back to end_turn).
  - ``usage`` -> ``{"input_tokens": ..., "output_tokens": ...}``.
"""

from __future__ import annotations

import json
import copy
import hashlib
from dataclasses import dataclass
from typing import Any

from reverso.protocols.adapter import ResponseEnvelope, ResponsesRequest
from reverso.protocols.anthropic_feature_gate import (
    gate_anthropic_features,
    strip_degradable_features,
)

# Responses request fields the translation sets directly; everything else from an
# Anthropic request that the surface still wants to forward rides in ``extra``.
_EXTRA_PASSTHROUGH = ("max_tokens", "temperature", "stop_sequences")

# Anthropic stop_reason values that map straight through; an unknown or absent
# Responses status falls back to "end_turn".
_DEFAULT_STOP_REASON = "end_turn"


@dataclass(frozen=True)
class AnthropicProjectionSource:
    response_address: tuple[int | str, ...]
    native_json_pointer: tuple[int | str, ...]
    native_block_kind: str
    structural_fingerprint: str


@dataclass(frozen=True)
class PreparedAnthropicDispatch:
    request: ResponsesRequest
    payload: dict[str, Any]
    projection_sources: tuple[AnthropicProjectionSource, ...]


def prepare_anthropic_request(
    payload: dict[str, Any], backend: str
) -> tuple[ResponsesRequest, dict[str, Any]]:
    """Prepare an Anthropic Messages payload for dispatch to ``backend``.

    Consolidates the pre-dispatch pipeline the ASGI app previously orchestrated
    inline, in the exact same order:

      1. ``strip_degradable_features(payload)`` -- degrades cache_control and
         extended thinking IN PLACE, so the stripped payload is what the gate
         and the downstream adapter both observe.
      2. ``gate_anthropic_features(payload, backend)`` -- raises
         ``AnthropicFeatureRejected`` for any remaining feature the backend
         classifies as unsupported (the caller maps it to a 400).
      3. ``anthropic_request_to_responses(payload)`` -- the translation onto
         the frozen Responses contract.

    Returns ``(request, payload)``: the translated ``ResponsesRequest`` plus
    the stripped payload (the SAME dict object, mutated in place). The payload
    is returned so the caller keeps an explicit handle on exactly what was
    translated -- e.g. for the stream-flag dispatch check or any input_items
    recording -- without re-deriving it.

    Stateless and pure of ASGI/network concerns (ADR 0006 D1): no I/O, no
    stored state; the only side effect is the documented in-place strip.
    Compression and dispatch remain the app's job.
    """
    prepared = prepare_anthropic_dispatch(payload, backend)
    return prepared.request, prepared.payload


def prepare_anthropic_dispatch(
    payload: dict[str, Any], backend: str
) -> PreparedAnthropicDispatch:
    """Prepare translation and its exact reversible native text addresses."""
    strip_degradable_features(payload)
    gate_anthropic_features(payload, backend)
    sources: list[AnthropicProjectionSource] = []
    request = _anthropic_request_to_responses(payload, sources)
    return PreparedAnthropicDispatch(request, payload, tuple(sources))


def _get_at(root: Any, address: tuple[int | str, ...]) -> Any:
    value = root
    for part in address:
        value = value[part]
    return value


def _set_at(root: Any, address: tuple[int | str, ...], value: Any) -> None:
    target = root
    for part in address[:-1]:
        target = target[part]
    target[address[-1]] = value


def _native_fingerprint(
    payload: dict[str, Any], pointer: tuple[int | str, ...], kind: str
) -> str:
    if pointer[:1] == ("system",):
        siblings = (
            [block.get("type") for block in payload["system"]]
            if isinstance(payload.get("system"), list)
            else ["string"]
        )
        role = "system"
        stable_id = None
    else:
        message_index = int(pointer[1])
        message = payload["messages"][message_index]
        content = message.get("content")
        siblings = (
            [block.get("type") for block in content if isinstance(block, dict)]
            if isinstance(content, list)
            else ["string"]
        )
        role = message.get("role")
        block = content[pointer[3]] if isinstance(content, list) else message
        stable_id = block.get("id") or block.get("tool_use_id")
    structural = json.dumps(
        [role, kind, stable_id, siblings],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(structural).hexdigest()


def _record_source(
    sources: list[AnthropicProjectionSource],
    payload: dict[str, Any],
    response: tuple[int | str, ...],
    native: tuple[int | str, ...],
    kind: str,
) -> None:
    sources.append(
        AnthropicProjectionSource(
            response,
            native,
            kind,
            _native_fingerprint(payload, native, kind),
        )
    )


def project_compressed_request_to_anthropic_payload(
    prepared: PreparedAnthropicDispatch,
    compressed_request: ResponsesRequest,
) -> dict[str, Any]:
    """Atomically project reversible compressed text, otherwise fail open."""
    original = prepared.request
    sources = prepared.projection_sources
    response_addresses = [source.response_address for source in sources]
    if len(response_addresses) != len(set(response_addresses)):
        return copy.deepcopy(prepared.payload)
    original_shape = {
        "instructions": original.instructions,
        "input": copy.deepcopy(original.input),
    }
    compressed_shape = {
        "instructions": compressed_request.instructions,
        "input": copy.deepcopy(compressed_request.input),
    }
    try:
        for address in response_addresses:
            if not isinstance(_get_at(original_shape, address), str) or not isinstance(
                _get_at(compressed_shape, address), str
            ):
                return copy.deepcopy(prepared.payload)
            _set_at(original_shape, address, "<reverso-text>")
            _set_at(compressed_shape, address, "<reverso-text>")
    except (IndexError, KeyError, TypeError):
        return copy.deepcopy(prepared.payload)
    if original_shape != compressed_shape:
        return copy.deepcopy(prepared.payload)

    projected = copy.deepcopy(prepared.payload)
    try:
        for source in sources:
            if (
                _native_fingerprint(
                    projected, source.native_json_pointer, source.native_block_kind
                )
                != source.structural_fingerprint
            ):
                return copy.deepcopy(prepared.payload)
            compressed_text = _get_at(
                {
                    "instructions": compressed_request.instructions,
                    "input": compressed_request.input,
                },
                source.response_address,
            )
            _set_at(projected, source.native_json_pointer, compressed_text)
    except (IndexError, KeyError, TypeError):
        return copy.deepcopy(prepared.payload)
    return projected


def anthropic_request_to_responses(payload: dict[str, Any]) -> ResponsesRequest:
    """Map an Anthropic Messages request body into a ResponsesRequest.

    ``payload`` is the decoded JSON Messages body. ``system`` may be a string or a
    list of content blocks; both collapse into ``instructions``. ``messages``
    content blocks become Responses input items. ``tools`` / ``tool_choice`` map to
    the Responses function-tool surface. ``max_tokens`` / ``temperature`` /
    ``stop_sequences`` ride in ``extra``.
    """
    return _anthropic_request_to_responses(payload, None)


def _anthropic_request_to_responses(
    payload: dict[str, Any],
    sources: list[AnthropicProjectionSource] | None,
) -> ResponsesRequest:
    """Translate while optionally emitting reverse sources at leaf creation."""
    model = payload.get("model")
    instructions = _system_to_instructions(payload.get("system"))
    if sources is not None and instructions is not None:
        system = payload.get("system")
        if isinstance(system, str):
            _record_source(
                sources, payload, ("instructions",), ("system",), "system_text"
            )
        elif (
            isinstance(system, list)
            and len(system) == 1
            and isinstance(system[0], dict)
            and system[0].get("type") == "text"
            and isinstance(system[0].get("text"), str)
        ):
            _record_source(
                sources,
                payload,
                ("instructions",),
                ("system", 0, "text"),
                "system_text",
            )
    input_items = _messages_to_input_items(
        payload.get("messages"), payload=payload, sources=sources
    )
    tools = _tools_to_responses(payload.get("tools"))
    tool_choice = _tool_choice_to_responses(payload.get("tool_choice"))

    extra: dict[str, Any] = {}
    for key in _EXTRA_PASSTHROUGH:
        if key in payload and payload[key] is not None:
            extra[key] = payload[key]

    return ResponsesRequest(
        model=model if isinstance(model, str) else "",
        input=input_items,
        stream=bool(payload.get("stream", False)),
        tools=tools,
        instructions=instructions,
        tool_choice=tool_choice,
        extra=extra,
    )


def responses_envelope_to_anthropic(envelope: ResponseEnvelope) -> dict[str, Any]:
    """Map a Responses ResponseEnvelope into an Anthropic Messages response body.

    Emits the Anthropic message shape: a ``msg_``-prefixed id derived from the
    Responses id, ``type`` "message", ``role`` "assistant", a content array of
    text and tool_use blocks (empty text blocks filtered), a mapped
    ``stop_reason``, and a ``usage`` block with input/output token counts.
    """
    content = _output_to_content_blocks(envelope.output)
    stop_reason = _stop_reason_from_output(content)
    return {
        "id": _anthropic_message_id(envelope.id),
        "type": "message",
        "role": "assistant",
        "model": envelope.model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _usage_to_anthropic(envelope.usage),
    }


# --- request helpers --------------------------------------------------------


def _system_to_instructions(system: Any) -> str | None:
    """Collapse an Anthropic ``system`` (string or block list) into instructions.

    A string passes through; a list concatenates the ``text`` of each text block
    with newlines. Returns None when there is no usable system text.
    """
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        texts = [
            block["text"]
            for block in system
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        joined = "\n".join(texts)
        return joined or None
    return None


def _messages_to_input_items(
    messages: Any,
    *,
    payload: dict[str, Any] | None = None,
    sources: list[AnthropicProjectionSource] | None = None,
) -> list[dict[str, Any]]:
    """Translate Anthropic ``messages`` into a Responses input item list.

    Each message's content blocks are translated in order. text blocks for a
    message accumulate into a single message item (so a mixed text + tool_use
    message flushes its text first, before the tool_use), while tool_use and
    tool_result blocks each become their own structural item.
    """
    items: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return items
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        role = role if isinstance(role, str) else "user"
        items.extend(
            _message_to_items(
                role,
                message.get("content"),
                item_offset=len(items),
                message_index=message_index,
                payload=payload,
                sources=sources,
            )
        )
    return items


def _message_to_items(
    role: str,
    content: Any,
    *,
    item_offset: int = 0,
    message_index: int = 0,
    payload: dict[str, Any] | None = None,
    sources: list[AnthropicProjectionSource] | None = None,
) -> list[dict[str, Any]]:
    """Translate one Anthropic message's content into Responses input items."""
    if isinstance(content, str):
        if not content:
            return []
        item = _message_item(role, [_text_part(role, content)])
        if sources is not None and payload is not None:
            _record_source(
                sources,
                payload,
                ("input", item_offset, "content", 0, "text"),
                ("messages", message_index, "content"),
                "message_text",
            )
        return [item]

    if not isinstance(content, list):
        return []

    items: list[dict[str, Any]] = []
    pending_parts: list[dict[str, Any]] = []
    pending_native_pointers: list[tuple[int | str, ...]] = []

    def flush() -> None:
        if pending_parts:
            item_index = item_offset + len(items)
            items.append(_message_item(role, list(pending_parts)))
            if sources is not None and payload is not None:
                for part_index, (part, pointer) in enumerate(
                    zip(pending_parts, pending_native_pointers, strict=False)
                ):
                    if part.get("type") in {"input_text", "output_text"}:
                        _record_source(
                            sources,
                            payload,
                            ("input", item_index, "content", part_index, "text"),
                            pointer,
                            "message_text",
                        )
            pending_parts.clear()
            pending_native_pointers.clear()

    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                pending_parts.append(_text_part(role, text))
                pending_native_pointers.append(
                    ("messages", message_index, "content", block_index, "text")
                )
        elif block_type == "image":
            pending_parts.append(_image_part(block))
            pending_native_pointers.append(())
        elif block_type == "tool_use":
            flush()
            items.append(_function_call_item(block))
        elif block_type == "tool_result":
            flush()
            items.append(_function_call_output_item(block))
    flush()
    return items


def _text_part(role: str, text: str) -> dict[str, Any]:
    """Build a Responses content part for text, role-appropriate type."""
    part_type = "output_text" if role == "assistant" else "input_text"
    return {"type": part_type, "text": text}


def _image_part(block: dict[str, Any]) -> dict[str, Any]:
    """Pass an Anthropic image block through as a Responses input_image part.

    Gating of image input per backend is G005, not a translation concern, so the
    source payload is preserved here rather than rejected.
    """
    part: dict[str, Any] = {"type": "input_image"}
    source = block.get("source")
    if source is not None:
        part["source"] = source
    return part


def _message_item(role: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Responses message input item from translated content parts."""
    return {"type": "message", "role": role, "content": parts}


def _function_call_item(block: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic tool_use block into a Responses function_call item.

    The Anthropic ``id`` is preserved as the Responses ``call_id`` so a later
    tool_result can be matched back to its call. The ``input`` object is serialized
    to the Responses ``arguments`` JSON string.
    """
    arguments = block.get("input")
    return {
        "type": "function_call",
        "call_id": block.get("id"),
        "name": block.get("name"),
        "arguments": _dump_arguments(arguments),
    }


def _function_call_output_item(block: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic tool_result block into a function_call_output item.

    ``tool_use_id`` is preserved as ``call_id``. ``is_error`` is carried
    OUT-OF-BAND as a structural ``is_error: true`` field on the item AND, when
    true, kept in-band as a hint prefixed on the rendered output text so an
    adapter that only reads ``output`` still sees the failure signal.
    """
    text = _tool_result_content_text(block.get("content"))
    is_error = block.get("is_error") is True
    output = f"[tool_error] {text}" if is_error else text
    item: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": block.get("tool_use_id"),
        "output": output,
    }
    if is_error:
        item["is_error"] = True
    return item


def _tool_result_content_text(content: Any) -> str:
    """Render an Anthropic tool_result ``content`` field into output text.

    ``content`` may be a plain string or a LIST of content blocks; a list joins
    the ``text`` of each text block with newlines. Non-text list entries are
    skipped. A non-string, non-list value falls back to an empty string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "\n".join(texts)
    return ""


def _dump_arguments(value: Any) -> str:
    """Serialize a tool_use ``input`` object into a Responses arguments string."""
    if value is None:
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _tools_to_responses(tools: Any) -> list[dict[str, Any]] | None:
    """Map Anthropic ``tools`` to Responses function tools, or None when absent.

    Each Anthropic tool's ``input_schema`` becomes the Responses ``parameters``
    object, matching the flat ``{"type":"function","name":...,"parameters":...}``
    shape the Responses adapters consume.
    """
    if not isinstance(tools, list):
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        function: dict[str, Any] = {"type": "function", "name": name}
        description = tool.get("description")
        if isinstance(description, str):
            function["description"] = description
        input_schema = tool.get("input_schema")
        if input_schema is not None:
            function["parameters"] = input_schema
        converted.append(function)
    return converted or None


def _tool_choice_to_responses(tool_choice: Any) -> Any | None:
    """Map an Anthropic ``tool_choice`` to a Responses tool_choice, or None.

    Mapping: ``auto`` -> "auto", ``any`` -> "required", ``none`` -> "none",
    ``tool`` -> a function choice naming the tool. A ``tool`` choice that omits a
    usable ``name`` falls back to "required" (force a tool call without pinning a
    specific tool) rather than emitting a nameless choice.
    """
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        name = tool_choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "name": name}
        return "required"
    return None


# --- response helpers -------------------------------------------------------


def _output_to_content_blocks(output: Any) -> list[dict[str, Any]]:
    """Map Responses output items into Anthropic content blocks.

    message items contribute one text block per non-empty output_text part;
    function_call items become tool_use blocks carrying the call id, name, and the
    parsed arguments object. Empty text blocks are filtered out.
    """
    blocks: list[dict[str, Any]] = []
    if not isinstance(output, list):
        return blocks
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content", []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        blocks.append({"type": "text", "text": text})
        elif item_type == "function_call":
            blocks.append(_tool_use_block(item))
    return blocks


def _tool_use_block(item: dict[str, Any]) -> dict[str, Any]:
    """Build an Anthropic tool_use block from a Responses function_call item."""
    return {
        "type": "tool_use",
        "id": item.get("call_id"),
        "name": item.get("name"),
        "input": _parse_arguments(item.get("arguments")),
    }


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """Parse a Responses function_call ``arguments`` JSON string into an object."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _stop_reason_from_output(content: list[dict[str, Any]]) -> str:
    """Derive the Anthropic stop_reason from the translated content blocks.

    A turn that produced any tool_use block stops with "tool_use"; otherwise the
    turn ends normally with the default "end_turn". max_tokens / stop_sequence
    reasons are not derivable from a buffered Responses envelope in G003, so the
    common end_turn / tool_use cases are mapped here and unknown states fall back
    to end_turn.
    """
    for block in content:
        if block.get("type") == "tool_use":
            return "tool_use"
    return _DEFAULT_STOP_REASON


def map_stop_reason(reason: Any) -> str:
    """Map a Responses-level stop reason string to an Anthropic stop_reason.

    Recognizes end_turn / tool_use / max_tokens / stop_sequence; any unknown or
    non-string reason falls back to end_turn. Exposed for the streaming mapper
    (G004) and the unit tests that pin the unknown-reason fallback.
    """
    known = {"end_turn", "tool_use", "max_tokens", "stop_sequence"}
    if isinstance(reason, str) and reason in known:
        return reason
    return _DEFAULT_STOP_REASON


def _anthropic_message_id(response_id: Any) -> str:
    """Build the Anthropic ``msg_`` id from a Responses ``resp_`` id."""
    if isinstance(response_id, str):
        return "msg_" + response_id.removeprefix("resp_")
    return "msg_"


def _usage_to_anthropic(usage: Any) -> dict[str, int]:
    """Map Responses usage into the Anthropic input/output token shape."""
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": _int_or_zero(usage.get("input_tokens")),
        "output_tokens": _int_or_zero(usage.get("output_tokens")),
    }


def _int_or_zero(value: Any) -> int:
    """Coerce a usage count to int, defaulting to 0."""
    return value if isinstance(value, int) else 0
