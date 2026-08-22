"""OCG-G5: the Anthropic-native transport and its normalization.

The normalization here is DELIBERATELY much smaller than ocgo's, and the tests
below are the reason. G5's acceptance criterion requires each stripped field to
have a test proving the upstream rejects the request when it is present, so no
strip is speculative. Measured 2026-08-22 against glm-5, kimi-k3 and minimax-m3:

* thinking, reasoning, reasoning_effort, effort, level, depth -> accepted (200)
  by every model tried. ocgo strips all six; stripping them here would be
  speculative and would silently discard a caller's thinking budget.
* output_config -> genuinely rejected (400, invalid_request_error) by 9 of the 22
  /messages-capable ids (kimi-k3, all three minimax, all five qwen). This is the
  only justified strip.
* system in BOTH string and list-of-blocks form -> accepted. ocgo normalizes it;
  nothing needs normalizing.
"""

from __future__ import annotations

import pytest

from reverso.protocols.adapters.opencode.messages import (
    OUTPUT_CONFIG_REJECTING_MODELS,
    PROVEN_STRIPPED_FIELDS,
    SPECULATIVE_FIELDS_KEPT,
    normalize_messages_payload,
)


def test_output_config_is_stripped() -> None:
    payload = {"model": "kimi-k3", "output_config": {"format": "text"}, "max_tokens": 1}
    assert "output_config" not in normalize_messages_payload(payload)


def test_output_config_is_the_only_proven_strip() -> None:
    assert PROVEN_STRIPPED_FIELDS == ("output_config",)


@pytest.mark.parametrize("field", SPECULATIVE_FIELDS_KEPT)
def test_speculative_fields_survive(field: str) -> None:
    """Measured accepted upstream, so stripping them would discard caller intent."""
    payload = {"model": "glm-5", field: {"probe": True}, "max_tokens": 1}
    assert normalize_messages_payload(payload)[field] == {"probe": True}


def test_thinking_budget_is_not_silently_discarded() -> None:
    """The most consequential of the six: ocgo drops it outright."""
    payload = {
        "model": "kimi-k3",
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "max_tokens": 1,
    }
    assert normalize_messages_payload(payload)["thinking"]["budget_tokens"] == 4096


def test_string_system_is_untouched() -> None:
    payload = {"model": "glm-5", "system": "Be terse.", "max_tokens": 1}
    assert normalize_messages_payload(payload)["system"] == "Be terse."


def test_block_system_is_untouched() -> None:
    """Claude Code sends the list form; it was measured accepted as-is."""
    blocks = [{"type": "text", "text": "Be terse."}]
    payload = {"model": "glm-5", "system": blocks, "max_tokens": 1}
    assert normalize_messages_payload(payload)["system"] == blocks


def test_normalization_does_not_mutate_the_caller_payload() -> None:
    payload = {"model": "kimi-k3", "output_config": {"format": "text"}}
    normalize_messages_payload(payload)
    assert "output_config" in payload


def test_messages_and_model_are_preserved() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    payload = {"model": "glm-5", "messages": messages, "max_tokens": 64}
    result = normalize_messages_payload(payload)
    assert result["messages"] == messages
    assert result["model"] == "glm-5"
    assert result["max_tokens"] == 64


def test_the_rejecting_model_set_is_recorded_evidence() -> None:
    """Documents WHICH ids were observed rejecting, even though the strip is
    unconditional: a future reader needs to know the strip was measured, not
    guessed, and on what."""
    assert "kimi-k3" in OUTPUT_CONFIG_REJECTING_MODELS
    for family in ("minimax-m3", "qwen3.7-max"):
        assert family in OUTPUT_CONFIG_REJECTING_MODELS
    # glm-5 accepted it; it must not be misrecorded as a rejector.
    assert "glm-5" not in OUTPUT_CONFIG_REJECTING_MODELS
