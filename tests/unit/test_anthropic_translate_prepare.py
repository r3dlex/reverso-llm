"""Unit tests for the consolidated Anthropic request-preparation seam.

prepare_anthropic_request composes the whole pre-dispatch pipeline (strip
degradable features -> per-backend capability gate -> Anthropic -> Responses
translation) as one pure function. These tests pin the seam's contract: a valid
payload prepares cleanly on every backend; a semantic feature the backend
classifies as unsupported (input.image off copilot) raises
AnthropicFeatureRejected, including when nested inside tool_result inner
content; degradable features (thinking param/blocks, cache_control everywhere)
are stripped IN PLACE before the gate runs (strip-before-gate ordering is
observable: cache_control is unsupported on every backend, so gate-first would
reject payloads that must succeed); the returned recording payload is the SAME
mutated dict object the caller passed in; and the translated request is exactly
what the bare translator produces from the stripped payload.
"""

from __future__ import annotations

import pytest

from reverso.protocols.anthropic_feature_gate import AnthropicFeatureRejected
from reverso.protocols.anthropic_translate import (
    anthropic_request_to_responses,
    prepare_anthropic_request,
)

ALL_BACKENDS = ("claude", "copilot", "auggie", "deepseek", "codex")

IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
}


def _plain_payload(model: str = "deepseek-v4-pro") -> dict:
    return {
        "model": model,
        "system": "be terse",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
    }


# --- valid payloads per backend ----------------------------------------------


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_plain_payload_prepares_on_every_backend(backend: str) -> None:
    request, recording = prepare_anthropic_request(_plain_payload(), backend)
    assert request.model == "deepseek-v4-pro"
    assert request.instructions == "be terse"
    assert request.input == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]
    assert recording["model"] == "deepseek-v4-pro"


def test_image_accepted_on_copilot() -> None:
    payload = _plain_payload()
    payload["messages"] = [{"role": "user", "content": [dict(IMAGE_BLOCK)]}]
    request, _ = prepare_anthropic_request(payload, "copilot")
    # input.image is native on copilot: no rejection, block passes through.
    assert request.input[0]["type"] == "message"


# --- gated-feature rejection --------------------------------------------------


@pytest.mark.parametrize("backend", ("claude", "auggie", "deepseek", "codex"))
def test_image_rejected_on_non_image_backends(backend: str) -> None:
    payload = _plain_payload()
    payload["messages"] = [{"role": "user", "content": [dict(IMAGE_BLOCK)]}]
    with pytest.raises(AnthropicFeatureRejected) as excinfo:
        prepare_anthropic_request(payload, backend)
    assert excinfo.value.feature == "input.image"
    assert excinfo.value.backend == backend
    assert backend in str(excinfo.value)


def test_image_nested_in_tool_result_rejected() -> None:
    payload = _plain_payload()
    payload["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_abc",
                    "content": [dict(IMAGE_BLOCK)],
                }
            ],
        }
    ]
    with pytest.raises(AnthropicFeatureRejected) as excinfo:
        prepare_anthropic_request(payload, "deepseek")
    assert excinfo.value.feature == "input.image"


# --- degradable stripping (and strip-before-gate ordering) --------------------


def _degradable_payload() -> dict:
    return {
        "model": "deepseek-v4-pro",
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "system": [
            {
                "type": "text",
                "text": "be terse",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": [
            {
                "name": "get_weather",
                "description": "w",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "s"},
                    {
                        "type": "text",
                        "text": "checking",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
            {"role": "user", "content": "and now?"},
        ],
    }


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_degradable_features_stripped_not_rejected(backend: str) -> None:
    # thinking and cache_control are unsupported on EVERY backend, so this
    # payload only prepares cleanly if the strip runs BEFORE the gate. A
    # gate-first ordering would raise AnthropicFeatureRejected here.
    payload = _degradable_payload()
    request, recording = prepare_anthropic_request(payload, backend)
    assert "thinking" not in recording
    assert recording["system"][0].get("cache_control") is None
    assert recording["tools"][0].get("cache_control") is None
    assistant_blocks = recording["messages"][0]["content"]
    assert [b["type"] for b in assistant_blocks] == ["text"]
    assert assistant_blocks[0].get("cache_control") is None
    # The assistant text survives the strip and reaches the translation.
    assert request.input[0]["content"][0] == {"type": "output_text", "text": "checking"}


# --- recording-payload preservation -------------------------------------------


def test_recording_payload_is_the_same_mutated_dict() -> None:
    payload = _degradable_payload()
    _, recording = prepare_anthropic_request(payload, "claude")
    # Same object, mutated in place: the caller's handle observes the strip too.
    assert recording is payload
    assert "thinking" not in payload


def test_stream_flag_survives_preparation() -> None:
    # The app dispatches on payload["stream"] AFTER preparation; the strip must
    # never touch it.
    payload = _plain_payload()
    payload["stream"] = True
    _, recording = prepare_anthropic_request(payload, "claude")
    assert recording.get("stream") is True


# --- translation equivalence ---------------------------------------------------


def test_prepared_request_matches_bare_translation_of_stripped_payload() -> None:
    prepared, recording = prepare_anthropic_request(_degradable_payload(), "claude")
    # Translating the (already stripped) recording payload again must yield the
    # identical request: prepare adds ordering, not translation semantics.
    retranslated = anthropic_request_to_responses(recording)
    assert prepared == retranslated
