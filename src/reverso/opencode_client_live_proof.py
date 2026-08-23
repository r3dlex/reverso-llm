"""Opt-in live proof for the Reverso OpenCode client surface.

Mirrors ``codex_live_proof``/``ollama_live_proof``: deterministic report,
secret-free output, injectable transport for unit tests. The proof drives the
exact request shape a generated OpenCode fragment produces - POST to the
gateway root Anthropic Messages surface with a fragment selector as ``model`` -
so a green run demonstrates routing plus embedded Headroom traversal end to end.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

GATEWAY_BASE_URL = "http://127.0.0.1:64946"
DEFAULT_PROOF_MODEL = "claude-sonnet-4-6"
PROOF_MARKER = "REVERSO_OK"
ANTHROPIC_VERSION = "2023-06-01"

Poster = Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any] | None]]


@dataclass(frozen=True)
class OpencodeLiveProofInputs:
    base_url: str = GATEWAY_BASE_URL
    model: str = DEFAULT_PROOF_MODEL
    prompt: str = f"Reply with exactly: {PROOF_MARKER}"
    max_tokens: int = 64
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class OpencodeLiveProofReport:
    """Secret-free live proof outcome."""

    status: str
    model: str
    endpoint: str
    latency_ms: int | None = None
    detail: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_poster(
    url: str, payload: dict[str, Any], timeout: float
) -> tuple[int, dict[str, Any] | None]:
    response = httpx.post(
        url,
        json=payload,
        timeout=timeout,
        headers={"anthropic-version": ANTHROPIC_VERSION},
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


def _message_text(body: dict[str, Any]) -> str:
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def run_opencode_live_proof(
    inputs: OpencodeLiveProofInputs | None = None,
    *,
    poster: Poster | None = None,
) -> OpencodeLiveProofReport:
    """Drive one Messages request shaped like a generated OpenCode fragment."""
    values = inputs or OpencodeLiveProofInputs()
    post = poster if poster is not None else _default_poster
    endpoint = f"{values.base_url}/v1/messages"
    payload = {
        "model": values.model,
        "max_tokens": values.max_tokens,
        "messages": [{"role": "user", "content": values.prompt}],
    }
    started = time.monotonic()
    try:
        status_code, body = post(endpoint, payload, values.timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a failed proof
        return OpencodeLiveProofReport(
            status="failed",
            model=values.model,
            endpoint=endpoint,
            detail=f"{type(exc).__name__}",
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    if status_code != 200:
        return OpencodeLiveProofReport(
            status="failed",
            model=values.model,
            endpoint=endpoint,
            latency_ms=latency_ms,
            detail=f"http_{status_code}",
        )
    if not isinstance(body, dict):
        return OpencodeLiveProofReport(
            status="failed",
            model=values.model,
            endpoint=endpoint,
            latency_ms=latency_ms,
            detail="non_json_body",
        )
    if PROOF_MARKER not in _message_text(body):
        return OpencodeLiveProofReport(
            status="failed",
            model=values.model,
            endpoint=endpoint,
            latency_ms=latency_ms,
            detail="marker_missing",
        )
    return OpencodeLiveProofReport(
        status="passed",
        model=values.model,
        endpoint=endpoint,
        latency_ms=latency_ms,
    )
