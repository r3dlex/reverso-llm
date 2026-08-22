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

from reverso.opencode_catalog_artifact import load_catalog_ids as _load_catalog_ids

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

# Bounded offline fallback: the committed catalog artifact
# (docs/reference/opencode-go-catalog.json). Used only when live discovery fails,
# so a network outage degrades to a known-good list rather than an empty picker.
#
# It lives in DATA rather than in this module (OCG-G6) so refreshing the catalog
# is not a code change: the same artifact is also the declared catalog that ADR
# 0020 routes on, so one file governs both listing fallback and routability.
# Reading fails closed: an empty declared catalog would make every qualified id
# fail closed, presenting as a routing bug rather than a corrupt file.
FALLBACK_MODEL_IDS: tuple[str, ...] = _load_catalog_ids()


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
