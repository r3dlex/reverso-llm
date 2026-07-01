"""In-process latest-snapshot store for Codex usage telemetry (Slice 2 / Slice 5).

This module is the sole in-process owner of the latest Codex turn snapshot.
It is a plain module-level singleton (no thread-local or session key) because
5h/weekly quota is account-level - the most recent turn's snapshot is the
correct display value regardless of which session produced it.

The store is updated at finalize-time by the CodexAdapter (token counts from
the stream, rate_limits from the session rollout file) and read-only by the
``GET /usage`` route in ``reverso.proxy.compose``.  The route MUST NOT spawn
codex (INV-2); it reads only what the store already holds.

Snapshot shape (matches ``docs/specifications/ACTIVE/reverso-usage-contract.md``
schema_version 1):

    {
        "schema_version": 1,
        "model_id": "gpt-5.5",
        "provider": "codex",
        "tokens": {
            "input_tokens": int,
            "cached_input_tokens": int,
            "output_tokens": int,
            "reasoning_output_tokens": int,
            "total_tokens": int,          # derived: input + output + reasoning
        },
        "context": {
            "used_tokens": int,           # = input_tokens
            "window_tokens": int | None,  # from codex_usage_context_window()
            "used_percent": float | None, # used_tokens / window_tokens * 100
        },
        "rate_limits": {                  # null until first rollout read
            "five_hour": {"used_percent": float, "resets_at": str},
            "weekly":    {"used_percent": float, "resets_at": str},
            "plan_type": str,
        } | None,
        "updated_at": str,                # ISO-8601 UTC
    }
"""

from __future__ import annotations

import datetime
from typing import Any


# The single latest Codex usage snapshot.  None until the first codex turn
# completes.  Written by CodexAdapter.finalize(); read by GET /usage.
_latest: dict[str, Any] | None = None


def update(snapshot: dict[str, Any]) -> None:
    """Replace the stored snapshot with ``snapshot``.

    Called at finalize-time by the CodexAdapter.  The snapshot must already
    conform to the contract shape; this module does no validation.
    """
    global _latest
    _latest = snapshot


def get() -> dict[str, Any] | None:
    """Return the latest snapshot, or None if no codex turn has completed yet."""
    return _latest


def get_rate_limits() -> dict[str, Any] | None:
    """Return just the rate_limits portion of the latest snapshot, or None."""
    if _latest is None:
        return None
    return _latest.get("rate_limits")


def empty_response(model_id: str | None = None) -> dict[str, Any]:
    """Return a schema_version 1 body with null rate_limits for the empty-store case.

    Used by GET /usage when no codex turn has completed yet.  Context values
    are zeroed; rate_limits is null per the contract.
    """
    return {
        "schema_version": 1,
        "model_id": model_id or "",
        "provider": "codex",
        "tokens": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        },
        "context": {
            "used_tokens": 0,
            "window_tokens": 0,
            "used_percent": 0.0,
        },
        "rate_limits": None,
        "updated_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    }


def build_snapshot(
    *,
    model_id: str,
    stream_usage: dict[str, Any],
    window_tokens: int | None,
    rate_limits: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a contract-shaped snapshot from the finalize-time data.

    ``stream_usage``  - the 4-key dict from ``turn.completed.usage``
    ``window_tokens`` - from ``codex_usage_context_window(model_id)``; ``None``
                        for an unmapped model id, in which case ``used_percent``
                        is ``None`` (HUD renders ``n/a``) rather than a guess.
    ``rate_limits``   - the mapped rate_limits block (or None)

    ``total_tokens`` = input_tokens + output_tokens + reasoning_output_tokens
    (reasoning is NOT double-counted with output; it is additional compute).
    """
    input_tokens = int(stream_usage.get("input_tokens") or 0)
    cached_input_tokens = int(stream_usage.get("cached_input_tokens") or 0)
    output_tokens = int(stream_usage.get("output_tokens") or 0)
    reasoning_output_tokens = int(stream_usage.get("reasoning_output_tokens") or 0)
    total_tokens = input_tokens + output_tokens + reasoning_output_tokens

    used_tokens = input_tokens
    used_percent = (
        round(used_tokens / window_tokens * 100, 1) if window_tokens else None
    )

    return {
        "schema_version": 1,
        "model_id": model_id,
        "provider": "codex",
        "tokens": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_tokens": total_tokens,
        },
        "context": {
            "used_tokens": used_tokens,
            "window_tokens": window_tokens,
            "used_percent": used_percent,
        },
        "rate_limits": rate_limits,
        "updated_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    }
