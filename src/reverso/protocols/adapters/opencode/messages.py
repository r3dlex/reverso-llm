"""Anthropic-native request normalization for OpenCode Go (OCG-G5).

This module is deliberately much smaller than ``ocgo``'s
``normalizeAnthropicRequestForUpstream``, and that is the finding rather than an
omission.

``ocgo`` strips seven fields (``thinking``, ``reasoning``, ``reasoning_effort``,
``effort``, ``level``, ``depth``, ``output_config``) and normalizes ``system``,
on the stated grounds that OpenCode's Anthropic endpoint is stricter than
Anthropic's. G5's acceptance criterion requires each strip to be justified by an
observed upstream rejection, so every field was measured against glm-5, kimi-k3
and minimax-m3 on 2026-08-22:

* ``thinking``, ``reasoning``, ``reasoning_effort``, ``effort``, ``level`` and
  ``depth`` were ACCEPTED (200) by all three. Stripping them is speculative, and
  stripping ``thinking`` in particular would silently discard a caller's
  reasoning budget: the request would succeed while doing something other than
  what was asked.
* ``output_config`` was genuinely REJECTED (400 ``invalid_request_error``) by 9
  of the 22 ``/messages``-capable ids: ``kimi-k3``, all three ``minimax`` ids and
  all five ``qwen`` ids. This is the one justified strip.
* ``system`` was accepted in BOTH string and list-of-blocks form, the latter
  being what Claude Code actually sends. Nothing needs normalizing.

``output_config`` is stripped UNCONDITIONALLY rather than per-model. It is an
output-shaping hint that these non-Anthropic upstreams do not implement, so the
11 ids that accept it are almost certainly ignoring it; a per-model table would
add drift risk for no behavioural gain, and this catalog has already produced one
hand-maintained table that turned out to be wrong (see the G3 spec section).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "OUTPUT_CONFIG_REJECTING_MODELS",
    "PROVEN_STRIPPED_FIELDS",
    "SPECULATIVE_FIELDS_KEPT",
    "normalize_messages_payload",
]

# Stripped because an upstream rejection was OBSERVED, not inferred.
PROVEN_STRIPPED_FIELDS: tuple[str, ...] = ("output_config",)

# Stripped by ocgo, measured accepted here, therefore KEPT. Named explicitly so
# a future reader sees the decision was made against evidence rather than missed.
SPECULATIVE_FIELDS_KEPT: tuple[str, ...] = (
    "thinking",
    "reasoning",
    "reasoning_effort",
    "effort",
    "level",
    "depth",
)

# The ids observed rejecting output_config, recorded as evidence. NOT consulted
# at runtime: the strip is unconditional. Two further ids were inconclusive
# (a 503 and a transport failure) and are deliberately not listed as rejectors.
OUTPUT_CONFIG_REJECTING_MODELS: frozenset[str] = frozenset(
    {
        "kimi-k3",
        "minimax-m2.5",
        "minimax-m2.7",
        "minimax-m3",
        "qwen3.5-plus",
        "qwen3.6-plus",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.8-max",
    }
)


def normalize_messages_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` safe to send to OpenCode Go ``/messages``.

    A copy, never an in-place edit: the caller's payload is also used for
    Headroom accounting and request records, and mutating it would make those
    disagree with what was actually sent.
    """
    normalized = dict(payload)
    for field in PROVEN_STRIPPED_FIELDS:
        normalized.pop(field, None)
    return normalized
