"""Pure feature-extraction + gate-mapping tests for the Anthropic gate (G005).

These exercise anthropic_feature_gate directly (no ASGI app, no adapter): they
pin (feature x backend -> allow/reject) against the data-driven capability table,
including the tool-definition cache_control case and the nested tool_result inner
cache_control / image cases that the integration suite drives end to end.
"""

from __future__ import annotations

import pytest

from reverso.protocols.anthropic_feature_gate import (
    FEATURE_CACHE_CONTROL,
    FEATURE_IMAGE,
    FEATURE_THINKING,
    FEATURE_TOOLS,
    AnthropicFeatureRejected,
    extract_anthropic_features,
    gate_anthropic_features,
)

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie"]


# --- extraction -------------------------------------------------------------


def test_extract_image_block_top_level() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image", "source": {"type": "base64", "data": "x"}},
                ],
            }
        ]
    }
    assert FEATURE_IMAGE in extract_anthropic_features(payload)


def test_extract_image_block_nested_in_tool_result() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "data": "x"}}
                        ],
                    }
                ],
            }
        ]
    }
    assert FEATURE_IMAGE in extract_anthropic_features(payload)


def test_extract_thinking_param() -> None:
    payload = {
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert FEATURE_THINKING in extract_anthropic_features(payload)


def test_extract_thinking_content_block() -> None:
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "hmm"}],
            }
        ]
    }
    assert FEATURE_THINKING in extract_anthropic_features(payload)


def test_extract_redacted_thinking_content_block() -> None:
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "redacted_thinking", "data": "x"}],
            }
        ]
    }
    assert FEATURE_THINKING in extract_anthropic_features(payload)


def test_extract_cache_control_on_message_block() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ]
    }
    assert FEATURE_CACHE_CONTROL in extract_anthropic_features(payload)


def test_extract_cache_control_on_system_block() -> None:
    payload = {
        "system": [
            {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert FEATURE_CACHE_CONTROL in extract_anthropic_features(payload)


def test_extract_cache_control_on_tool_definition() -> None:
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "get_weather",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    found = extract_anthropic_features(payload)
    assert FEATURE_CACHE_CONTROL in found
    assert FEATURE_TOOLS in found


def test_extract_cache_control_nested_in_tool_result() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {
                                "type": "text",
                                "text": "result",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assert FEATURE_CACHE_CONTROL in extract_anthropic_features(payload)


def test_extract_tools_emits_tools_feature() -> None:
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
    }
    assert FEATURE_TOOLS in extract_anthropic_features(payload)


def test_extract_plain_text_has_no_gated_features() -> None:
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    assert extract_anthropic_features(payload) == set()


# --- gate mapping (feature x backend) ---------------------------------------


def _image_payload() -> dict:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": "x"}}
                ],
            }
        ]
    }


def test_gate_image_allowed_on_copilot() -> None:
    gate_anthropic_features(_image_payload(), "copilot")


@pytest.mark.parametrize("backend", ["deepseek", "auggie"])
def test_gate_image_rejected_on_deepseek_and_auggie(backend: str) -> None:
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(_image_payload(), backend)
    assert exc.value.feature == FEATURE_IMAGE
    assert exc.value.backend == backend


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_thinking_param_rejected_on_all_backends(backend: str) -> None:
    payload = {
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(payload, backend)
    assert exc.value.feature == FEATURE_THINKING


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_thinking_block_rejected_on_all_backends(backend: str) -> None:
    payload = {
        "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}
        ]
    }
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(payload, backend)
    assert exc.value.feature == FEATURE_THINKING


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_cache_control_message_block_rejected_all(backend: str) -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ]
    }
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(payload, backend)
    assert exc.value.feature == FEATURE_CACHE_CONTROL


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_cache_control_tool_definition_rejected_all(backend: str) -> None:
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "get_weather",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(payload, backend)
    assert exc.value.feature == FEATURE_CACHE_CONTROL


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_cache_control_nested_tool_result_rejected_all(backend: str) -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {
                                "type": "text",
                                "text": "r",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(payload, backend)
    assert exc.value.feature == FEATURE_CACHE_CONTROL


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_tools_accepted_on_all_backends(backend: str) -> None:
    # auggie classifies tools.function as partial (text-only ceiling), not
    # unsupported: tools must be ACCEPTED and degrade, never hard-rejected.
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
    }
    gate_anthropic_features(payload, backend)


@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
def test_gate_plain_text_accepted_on_all_backends(backend: str) -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    gate_anthropic_features(payload, backend)


def test_rejected_error_message_secret_free_names_feature_and_backend() -> None:
    with pytest.raises(AnthropicFeatureRejected) as exc:
        gate_anthropic_features(_image_payload(), "deepseek")
    message = str(exc.value)
    assert FEATURE_IMAGE in message
    assert "deepseek" in message
