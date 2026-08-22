"""OpenCode Go catalog discovery and endpoint selection (OCG-G3).

Endpoint selection is DUAL-PROTOCOL BY DEFAULT with a narrow declared deny-list,
which inverts the design this slice was planned around.

The plan assumed a per-model protocol split needing a measured 29-entry table,
inherited from ``ocgo``'s hand-maintained 16-entry one. Measured against the live
service on 2026-08-22, that split does not exist: 22 of 29 catalog ids answered
BOTH ``/messages`` and ``/chat/completions``. Exactly one id refused the
Anthropic format, ``grok-4.5`` ("Model grok-4.5 is not supported for format
anthropic"). Every other non-200 had a cause orthogonal to protocol: a workspace
opt-in gate (``RegionError`` on the two China-hosted deepseek ids,
``DataPolicyError`` on ``muse-spark-1.2-contributor``) or an upstream outage
("Model is unavailable", "Unsupported model"). Those are transient or
account-scoped and must NOT be frozen into a protocol table.

``ocgo``'s table is not merely stale but wrong in substance: it forces
``minimax-m3``, ``minimax-m2.7``, ``minimax-m2.5`` and ``qwen3.7-max`` onto
``/messages`` as though chat-completions would reject them. All four answer
chat-completions. Encoding its table would have pinned four models to a needless
endpoint.

Two transport facts, both measured rather than assumed:

* ``/messages`` authenticates by ``X-API-Key`` ONLY. An ``Authorization: Bearer``
  header on that path returns ``AuthError: Missing API key``, while
  ``/chat/completions`` wants exactly the bearer form.
* The edge 403s a default client fingerprint with Cloudflare error 1010, so a
  User-Agent is mandatory. ``GET /models`` is otherwise public and needs no
  credential at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "ANTHROPIC_UNSUPPORTED_MODELS",
    "CHAT_COMPLETIONS_PATH",
    "FALLBACK_MODEL_IDS",
    "MESSAGES_PATH",
    "MODELS_PATH",
    "OPENCODE_GO_API_BASE",
    "USER_AGENT",
    "anthropic_endpoint_for",
    "parse_model_ids",
    "supports_anthropic_format",
]

OPENCODE_GO_API_BASE = "https://opencode.ai/zen/go/v1"
MODELS_PATH = "/models"
MESSAGES_PATH = "/messages"
CHAT_COMPLETIONS_PATH = "/chat/completions"

# Cloudflare error 1010 rejects a default client fingerprint outright, so this is
# a functional requirement rather than politeness.
USER_AGENT = "reverso-opencode-go/1.0"

# Measured 2026-08-22: the only catalog id that refuses the Anthropic format.
# Kept as a frozenset of exactly what was observed, not a defensive superset: an
# entry here permanently denies a model the native path, so an unmeasured guess
# would silently downgrade it.
ANTHROPIC_UNSUPPORTED_MODELS = frozenset({"grok-4.5"})

# Bounded offline fallback: the catalog observed on 2026-08-22. Used only when
# live discovery fails, so a network outage degrades to a known-good list rather
# than an empty picker. It WILL drift as OpenCode adds models; live discovery is
# the authority whenever it answers.
FALLBACK_MODEL_IDS: tuple[str, ...] = (
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-pro",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "gpt-5.6-luna",
    "grok-4.5",
    "hy3",
    "hy3-preview",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "mimo-v2-omni",
    "mimo-v2-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "muse-spark-1.2-contributor",
    "ox-alpha-free",
    "qwen3.5-plus",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-max",
)


def supports_anthropic_format(model_id: str) -> bool:
    """Whether ``model_id`` may be dispatched to the native ``/messages`` path."""
    return model_id.strip().lower() not in ANTHROPIC_UNSUPPORTED_MODELS


def anthropic_endpoint_for(model_id: str) -> str:
    """Return the upstream path to use for an Anthropic-shaped request."""
    return (
        MESSAGES_PATH if supports_anthropic_format(model_id) else CHAT_COMPLETIONS_PATH
    )


def parse_model_ids(payload: Any) -> tuple[str, ...]:
    """Extract sorted, deduplicated model ids from an OpenAI list payload.

    Unusable rows are skipped rather than raising: one malformed entry must not
    cost the caller the whole live catalog and send it to the stale fallback.
    A payload that is not a list envelope at all yields ``()``, which the caller
    treats as a failed discovery.
    """
    if not isinstance(payload, dict):
        return ()
    rows = payload.get("data")
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        return ()
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if isinstance(model_id, str) and model_id.strip():
            found.add(model_id.strip())
    return tuple(sorted(found))
