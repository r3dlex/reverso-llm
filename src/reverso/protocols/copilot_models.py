"""Shared Copilot model-id validation for Responses forwarding."""

from __future__ import annotations


def has_safe_model_id_chars(model_id: str) -> bool:
    """Return whether a model id is safe to send as a JSON model string."""
    return model_id.isascii() and all(
        not char.isspace() and 32 <= ord(char) < 127 for char in model_id
    )


def canonical_copilot_responses_model(model_id: str) -> str | None:
    """Return the Copilot Responses model id to put on the wire."""
    if not has_safe_model_id_chars(model_id):
        return None
    if model_id.startswith("gpt5"):
        suffix = model_id[len("gpt5") :]
        if suffix.startswith((".", "-")):
            return f"gpt-5{suffix}"
        return None
    return model_id


def is_copilot_responses_model_id(model_id: str) -> bool:
    """Return whether Reverso can safely forward a Copilot Responses model id."""
    return canonical_copilot_responses_model(model_id) is not None
