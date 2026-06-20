"""Stateless Anthropic Messages <-> Responses translation (ADR 0006 D1, G003).

Pure functions that map an inbound Anthropic Messages request onto the FROZEN
Responses contract (``ResponsesRequest``) and map a ``ResponseEnvelope`` back into
an Anthropic Messages response body. The module is stateless: it holds no
conversation state (that rides the existing in-memory ResponseStore through the
Responses contract) and performs no ASGI or network work, so it is reused by both
the non-streaming handler (G003) and the SSE streaming mapper (G004) and is
exercised directly by the unit tests.

This is the NON-STREAMING translation core only. Capability gating / feature
rejection (G005), count_tokens (G006), and /v1/models (G006) are NOT handled here;
image blocks are passed through (gating is G005, not a translation concern).

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
from typing import Any

from reverso.protocols.adapter import ResponseEnvelope, ResponsesRequest

# Responses request fields the translation sets directly; everything else from an
# Anthropic request that the surface still wants to forward rides in ``extra``.
_EXTRA_PASSTHROUGH = ("max_tokens", "temperature", "stop_sequences")

# Anthropic stop_reason values that map straight through; an unknown or absent
# Responses status falls back to "end_turn".
_DEFAULT_STOP_REASON = "end_turn"


def anthropic_request_to_responses(payload: dict[str, Any]) -> ResponsesRequest:
    """Map an Anthropic Messages request body into a ResponsesRequest.

    ``payload`` is the decoded JSON Messages body. ``system`` may be a string or a
    list of content blocks; both collapse into ``instructions``. ``messages``
    content blocks become Responses input items. ``tools`` / ``tool_choice`` map to
    the Responses function-tool surface. ``max_tokens`` / ``temperature`` /
    ``stop_sequences`` ride in ``extra``.
    """
    model = payload.get("model")
    instructions = _system_to_instructions(payload.get("system"))
    input_items = _messages_to_input_items(payload.get("messages"))
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


def _messages_to_input_items(messages: Any) -> list[dict[str, Any]]:
    """Translate Anthropic ``messages`` into a Responses input item list.

    Each message's content blocks are translated in order. text blocks for a
    message accumulate into a single message item (so a mixed text + tool_use
    message flushes its text first, before the tool_use), while tool_use and
    tool_result blocks each become their own structural item.
    """
    items: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return items
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        role = role if isinstance(role, str) else "user"
        items.extend(_message_to_items(role, message.get("content")))
    return items


def _message_to_items(role: str, content: Any) -> list[dict[str, Any]]:
    """Translate one Anthropic message's content into Responses input items."""
    if isinstance(content, str):
        if not content:
            return []
        return [_message_item(role, [_text_part(role, content)])]

    if not isinstance(content, list):
        return []

    items: list[dict[str, Any]] = []
    pending_parts: list[dict[str, Any]] = []

    def flush() -> None:
        if pending_parts:
            items.append(_message_item(role, list(pending_parts)))
            pending_parts.clear()

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                pending_parts.append(_text_part(role, text))
        elif block_type == "image":
            pending_parts.append(_image_part(block))
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
