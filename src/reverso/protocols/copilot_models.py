"""Shared Copilot model-id validation for Responses forwarding."""

from __future__ import annotations


def has_safe_model_id_chars(model_id: str) -> bool:
    """Return whether a model id is safe to send as a JSON model string."""
    return model_id.isascii() and all(
        not char.isspace() and 32 <= ord(char) < 127 for char in model_id
    )


def canonical_copilot_responses_model(model_id: str) -> str | None:
    """Return the Copilot Responses model id to put on the wire.

    GitHub Copilot lists Anthropic and Google models on ``/models``, but its
    ``/responses`` endpoint currently rejects them with
    ``unsupported_api_for_model``. Keep the Codex Responses profile on the
    OpenAI-family models that the upstream Responses endpoint accepts.
    """
    if not has_safe_model_id_chars(model_id):
        return None
    if model_id.startswith("gpt5"):
        suffix = model_id[len("gpt5") :]
        if suffix.startswith((".", "-")):
            model_id = f"gpt-5{suffix}"
        else:
            return None
    if not model_id.startswith("gpt-"):
        return None
    return model_id


def is_copilot_responses_model_id(model_id: str) -> bool:
    """Return whether Copilot upstream accepts this id on /responses."""
    return canonical_copilot_responses_model(model_id) is not None
