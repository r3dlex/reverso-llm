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


# --- depth-bound recursion safety (Finding 1a) --------------------------------


def _deeply_nested_tool_result(depth: int) -> dict:
    """Build a payload whose tool_result content is nested ``depth`` levels deep.

    Each level wraps the previous in another tool_result so the scanner must
    recurse ``depth`` times to reach the innermost block. At depth > _MAX_BLOCK_DEPTH
    the scan silently stops. A depth of _MAX_BLOCK_DEPTH+1 is enough to prove
    the cap; we keep depth small so Python's own dict construction never blows up.
    """
    inner: list = [{"type": "text", "text": "leaf"}]
    for _ in range(depth):
        inner = [{"type": "tool_result", "tool_use_id": "toolu_x", "content": inner}]
    return {"messages": [{"role": "user", "content": inner}]}


def test_extract_deeply_nested_tool_result_does_not_raise() -> None:
    """A payload nested beyond _MAX_BLOCK_DEPTH must not raise RecursionError.

    extract_anthropic_features must return cleanly regardless of nesting depth
    beyond the cap.
    """
    from reverso.protocols.anthropic_feature_gate import _MAX_BLOCK_DEPTH  # noqa: PLC0415

    # One level beyond the cap is sufficient to exercise the depth guard.
    payload = _deeply_nested_tool_result(_MAX_BLOCK_DEPTH + 1)
    result = extract_anthropic_features(payload)
    assert isinstance(result, set)


def test_gate_deeply_nested_tool_result_does_not_raise() -> None:
    """gate_anthropic_features must not raise RecursionError on a deeply nested payload."""
    from reverso.protocols.anthropic_feature_gate import _MAX_BLOCK_DEPTH  # noqa: PLC0415

    payload = _deeply_nested_tool_result(_MAX_BLOCK_DEPTH + 1)
    # No gated features in a plain text payload: gate passes without raising.
    gate_anthropic_features(payload, "deepseek")


def _tool_result_with_feature_at_depth(depth: int, feature_block: dict) -> dict:
    """Wrap ``feature_block`` inside ``depth`` layers of tool_result nesting.

    The innermost content list contains ``feature_block``; each outer layer
    is a tool_result whose content list wraps the previous.  The resulting
    payload has the feature at tool_result nesting depth ``depth`` as seen by
    ``_scan_block_list``.
    """
    inner: list = [feature_block]
    for _ in range(depth):
        inner = [{"type": "tool_result", "tool_use_id": "toolu_x", "content": inner}]
    return {"messages": [{"role": "user", "content": inner}]}


def test_cap_feature_at_depth_within_cap_is_detected() -> None:
    """A gated feature placed at exactly _MAX_BLOCK_DEPTH MUST be detected.

    This pins the lower boundary of the cap: the scanner must reach depth
    _MAX_BLOCK_DEPTH and find the feature there.
    """
    from reverso.protocols.anthropic_feature_gate import _MAX_BLOCK_DEPTH  # noqa: PLC0415

    image_block = {"type": "image", "source": {"type": "base64", "data": "x"}}
    payload = _tool_result_with_feature_at_depth(_MAX_BLOCK_DEPTH, image_block)
    assert FEATURE_IMAGE in extract_anthropic_features(payload)


def test_cap_feature_at_depth_beyond_cap_is_not_detected() -> None:
    """A gated feature placed at _MAX_BLOCK_DEPTH + 1 must NOT be detected.

    This pins the upper boundary of the cap independently of the RecursionError
    belt-and-suspenders catch: the depth guard in _scan_block_list must stop the
    scan before reaching depth _MAX_BLOCK_DEPTH + 1.  Removing or raising the cap
    would cause this test to fail (the feature would be found).
    """
    from reverso.protocols.anthropic_feature_gate import _MAX_BLOCK_DEPTH  # noqa: PLC0415

    image_block = {"type": "image", "source": {"type": "base64", "data": "x"}}
    payload = _tool_result_with_feature_at_depth(_MAX_BLOCK_DEPTH + 1, image_block)
    assert FEATURE_IMAGE not in extract_anthropic_features(payload)
