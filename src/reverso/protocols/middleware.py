"""App-owned protocol transforms for the first-party Responses gateway.

These are the reusable, provider-agnostic behaviors the first-party app applies
to Codex-observed Responses traffic: request normalization, think-tag stripping,
SSE completion guarding, and Codex models-refresh compatibility. The logic is
extracted from src/reverso/middleware/* (which still serve the legacy LiteLLM
app) but this module imports only pure helpers from reverso.proxy.utils and the
legacy middleware's stateless functions; it MUST NOT import reverso.proxy.app.
"""

from __future__ import annotations

import json
from typing import Any

from reverso.middleware.codex_responses_normalizer import (
    normalize_codex_responses_payload,
)
from reverso.middleware.responses_sse_completion import (
    _append_completion_before_done,
    _completion_event,
    _DONE_EVENT,
    _has_completed_marker,
    _has_done_marker,
)
from reverso.middleware.responses_think_stripper import (
    _rewrite_sse_block,
    _strip_text_fields,
)
from reverso.proxy.utils import StreamingThinkStripper

__all__ = [
    "normalize_request_payload",
    "strip_think_json",
    "strip_think_sse_stream",
    "ensure_sse_completion",
    "models_with_codex_refresh",
]


def normalize_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop Codex-only Responses fields a provider would reject."""
    return normalize_codex_responses_payload(payload)


def strip_think_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip reasoning/<think> content from a non-streaming Responses body."""
    result = _strip_text_fields(payload)
    return result if isinstance(result, dict) else payload


def strip_think_sse_stream(
    sse_blocks: list[bytes],
    *,
    split_visible_deltas: bool = False,
) -> list[bytes]:
    """Strip reasoning content across a sequence of SSE byte blocks.

    State (per-stream strippers, sent-item tracking) is shared across the blocks
    so multi-chunk streams collapse reasoning consistently.
    """
    stream_strippers: dict[tuple[Any, Any, Any], StreamingThinkStripper] = {}
    completed_texts: dict[tuple[Any, Any, Any], str] = {}
    sent_items: set[Any] = set()
    sent_content_parts: set[tuple[Any, Any, Any]] = set()
    rewritten: list[bytes] = []
    for block in sse_blocks:
        rewritten.append(
            _rewrite_sse_block(
                block,
                stream_strippers,
                completed_texts,
                sent_items,
                sent_content_parts,
                split_visible_deltas,
            )
        )
    return rewritten


def ensure_sse_completion(body: bytes, response_id: str) -> bytes:
    """Append response.completed and/or [DONE] to an SSE body when missing.

    Mirrors the legacy completion guard so a first-party adapter that forwards
    upstream SSE still presents Codex with a terminal completion event.
    """
    out = body
    if not _has_completed_marker(out):
        out = _append_completion_before_done(out, _completion_event(response_id))
    if not _has_done_marker(out):
        out = out + _DONE_EVENT
    return out


def models_with_codex_refresh(payload: dict[str, Any]) -> dict[str, Any]:
    """Add an empty Codex-private ``models`` field beside the OpenAI ``data`` list.

    Codex refreshes model metadata and expects a top-level ``models`` field. The
    OpenAI-compatible ``data`` list stays canonical; ``models`` is added only so
    Codex refresh decoding succeeds.
    """
    if "models" in payload or not isinstance(payload.get("data"), list):
        return payload
    enriched = dict(payload)
    enriched["models"] = []
    return enriched


def split_sse_lines(body: bytes) -> list[bytes]:
    """Split an SSE byte body into individual non-empty data blocks."""
    blocks: list[bytes] = []
    for raw in body.split(b"\n\n"):
        line = raw.strip()
        if line:
            blocks.append(line)
    return blocks


def encode_sse_event(event: str, data: dict[str, Any]) -> bytes:
    """Encode a Responses SSE event as wire bytes (event + data + blank line)."""
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return b"event: " + event.encode("utf-8") + b"\ndata: " + payload + b"\n\n"
