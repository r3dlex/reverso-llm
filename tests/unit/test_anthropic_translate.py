"""Unit tests for the stateless Anthropic <-> Responses translation (G003).

Pure round-trips over the translation functions (no ASGI, no network): system
string and block-list both collapse to instructions; tool_use round-trips to a
function_call and back to a tool_use preserving id/name/input; tool_result is_error
is carried both structurally (call_id + is_error field) and is ABSENT when not an
error; stop_reason mapping including the unknown -> end_turn fallback; usage; the
msg_ id derivation; empty content -> []; multiple tool_use blocks (ids preserved);
mixed text + tool_use ordering (text flushed first); multiple function_call outputs
-> multiple tool_use + stop_reason tool_use; tool_result content as a LIST of blocks
joined; tool_choice named / no-name fallback / null-name fallback.
"""

from __future__ import annotations

import json

from reverso.protocols.adapter import ResponseEnvelope
from reverso.protocols.anthropic_translate import (
    anthropic_request_to_responses,
    map_stop_reason,
    responses_envelope_to_anthropic,
)


# --- system -> instructions -------------------------------------------------


def test_system_string_to_instructions() -> None:
    req = anthropic_request_to_responses(
        {"model": "deepseek-v4-pro", "system": "be terse", "messages": []}
    )
    assert req.instructions == "be terse"
    assert req.model == "deepseek-v4-pro"


def test_system_block_list_to_instructions_concatenated() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "system": [
                {"type": "text", "text": "line one"},
                {"type": "text", "text": "line two"},
            ],
            "messages": [],
        }
    )
    assert req.instructions == "line one\nline two"


def test_absent_system_yields_none_instructions() -> None:
    req = anthropic_request_to_responses({"model": "deepseek-v4-pro", "messages": []})
    assert req.instructions is None


# --- text messages ----------------------------------------------------------


def test_user_text_message_to_input_item() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    assert req.input == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


def test_assistant_text_uses_output_text_part() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
            ],
        }
    )
    part = req.input[0]["content"][0]
    assert part == {"type": "output_text", "text": "hi"}


# --- tool_use round-trip ----------------------------------------------------


def test_tool_use_block_to_function_call_item() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "get_weather",
                            "input": {"city": "Berlin"},
                        }
                    ],
                }
            ],
        }
    )
    item = req.input[0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "toolu_abc"
    assert item["name"] == "get_weather"
    assert json.loads(item["arguments"]) == {"city": "Berlin"}


def test_function_call_envelope_to_tool_use_block() -> None:
    envelope = ResponseEnvelope(
        id="resp_xyz",
        model="deepseek-v4-pro",
        output=[
            {
                "type": "function_call",
                "call_id": "toolu_abc",
                "name": "get_weather",
                "arguments": json.dumps({"city": "Berlin"}),
            }
        ],
        usage={"input_tokens": 3, "output_tokens": 5},
    )
    body = responses_envelope_to_anthropic(envelope)
    block = next(b for b in body["content"] if b["type"] == "tool_use")
    assert block["id"] == "toolu_abc"
    assert block["name"] == "get_weather"
    assert block["input"] == {"city": "Berlin"}
    assert body["stop_reason"] == "tool_use"


def test_multiple_tool_use_blocks_preserve_ids() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                        {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
                    ],
                }
            ],
        }
    )
    call_ids = [
        item["call_id"] for item in req.input if item["type"] == "function_call"
    ]
    assert call_ids == ["t1", "t2"]


def test_mixed_text_and_tool_use_flushes_text_first() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "let me check"},
                        {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                    ],
                }
            ],
        }
    )
    assert req.input[0]["type"] == "message"
    assert req.input[0]["content"][0]["text"] == "let me check"
    assert req.input[1]["type"] == "function_call"
    assert req.input[1]["call_id"] == "t1"


def test_multiple_function_call_outputs_to_multiple_tool_use() -> None:
    envelope = ResponseEnvelope(
        id="resp_multi",
        model="deepseek-v4-pro",
        output=[
            {"type": "function_call", "call_id": "t1", "name": "a", "arguments": "{}"},
            {"type": "function_call", "call_id": "t2", "name": "b", "arguments": "{}"},
        ],
    )
    body = responses_envelope_to_anthropic(envelope)
    tool_blocks = [b for b in body["content"] if b["type"] == "tool_use"]
    assert [b["id"] for b in tool_blocks] == ["t1", "t2"]
    assert body["stop_reason"] == "tool_use"


# --- tool_result is_error ---------------------------------------------------


def test_tool_result_is_error_preserved_structurally_and_inband() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "boom",
                            "is_error": True,
                        }
                    ],
                }
            ],
        }
    )
    item = req.input[0]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "toolu_abc"
    # Structural out-of-band signal.
    assert item["is_error"] is True
    # In-band hint also present in the rendered output.
    assert "boom" in item["output"]
    assert "tool_error" in item["output"]


def test_tool_result_is_error_absent_when_not_an_error() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "ok",
                        }
                    ],
                }
            ],
        }
    )
    item = req.input[0]
    assert item["type"] == "function_call_output"
    assert "is_error" not in item
    assert item["output"] == "ok"


def test_tool_result_content_list_of_blocks_joined() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": [
                                {"type": "text", "text": "part one"},
                                {"type": "text", "text": "part two"},
                            ],
                        }
                    ],
                }
            ],
        }
    )
    item = req.input[0]
    assert item["output"] == "part one\npart two"


# --- tool_choice ------------------------------------------------------------


def test_tool_choice_named() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
    )
    assert req.tool_choice == {"type": "function", "name": "get_weather"}


def test_tool_choice_tool_without_name_falls_back_to_required() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [],
            "tool_choice": {"type": "tool"},
        }
    )
    assert req.tool_choice == "required"


def test_tool_choice_null_name_falls_back_to_required() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [],
            "tool_choice": {"type": "tool", "name": None},
        }
    )
    assert req.tool_choice == "required"


def test_tool_choice_auto_any_none() -> None:
    def choice(value: dict) -> object:
        req = anthropic_request_to_responses(
            {"model": "deepseek-v4-pro", "messages": [], "tool_choice": value}
        )
        return req.tool_choice

    assert choice({"type": "auto"}) == "auto"
    assert choice({"type": "any"}) == "required"
    assert choice({"type": "none"}) == "none"


def test_tools_input_schema_to_parameters() -> None:
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [],
            "tools": [
                {"name": "get_weather", "description": "w", "input_schema": schema}
            ],
        }
    )
    assert req.tools == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "w",
            "parameters": schema,
        }
    ]


# --- extra passthrough ------------------------------------------------------


def test_max_tokens_temperature_stop_sequences_to_extra() -> None:
    req = anthropic_request_to_responses(
        {
            "model": "deepseek-v4-pro",
            "messages": [],
            "max_tokens": 256,
            "temperature": 0.4,
            "stop_sequences": ["STOP"],
        }
    )
    assert req.extra == {
        "max_tokens": 256,
        "temperature": 0.4,
        "stop_sequences": ["STOP"],
    }


# --- response shape ---------------------------------------------------------


def test_envelope_to_message_id_and_shape() -> None:
    envelope = ResponseEnvelope(
        id="resp_deadbeef",
        model="deepseek-v4-pro",
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello there"}],
            }
        ],
        usage={"input_tokens": 7, "output_tokens": 2},
    )
    body = responses_envelope_to_anthropic(envelope)
    assert body["id"] == "msg_deadbeef"
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "deepseek-v4-pro"
    assert body["content"] == [{"type": "text", "text": "hello there"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 2}


def test_empty_content_yields_empty_list() -> None:
    envelope = ResponseEnvelope(id="resp_empty", model="deepseek-v4-pro", output=[])
    body = responses_envelope_to_anthropic(envelope)
    assert body["content"] == []
    assert body["stop_reason"] == "end_turn"


def test_empty_text_items_filtered() -> None:
    envelope = ResponseEnvelope(
        id="resp_e",
        model="deepseek-v4-pro",
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": ""},
                    {"type": "output_text", "text": "kept"},
                ],
            }
        ],
    )
    body = responses_envelope_to_anthropic(envelope)
    assert body["content"] == [{"type": "text", "text": "kept"}]


def test_usage_absent_defaults_to_zero() -> None:
    envelope = ResponseEnvelope(id="resp_n", model="deepseek-v4-pro", output=[])
    body = responses_envelope_to_anthropic(envelope)
    assert body["usage"] == {"input_tokens": 0, "output_tokens": 0}


# --- stop_reason mapping ----------------------------------------------------


def test_map_stop_reason_known_values() -> None:
    assert map_stop_reason("end_turn") == "end_turn"
    assert map_stop_reason("tool_use") == "tool_use"
    assert map_stop_reason("max_tokens") == "max_tokens"
    assert map_stop_reason("stop_sequence") == "stop_sequence"


def test_map_stop_reason_unknown_falls_back_to_end_turn() -> None:
    assert map_stop_reason("something_else") == "end_turn"
    assert map_stop_reason(None) == "end_turn"
