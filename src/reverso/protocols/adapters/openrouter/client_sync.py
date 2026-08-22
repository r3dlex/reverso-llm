"""OpenRouter managed client exposure (OR-G6, client convergence contracts).

Builds the Codex profile and Claude launcher catalogs from the curated
allowlist. Generates the exact Claude-discoverable alias from the
OR-G0-proven grammar (`anthropic-openrouter-{base64url(author/model)}`) and
the Codex selector as `openrouter/<author>/<model>`. Neither artifact ever
carries the API key.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

__all__ = [
    "ClaudeLauncher",
    "CodexProfile",
    "build_claude_alias",
    "build_claude_launcher",
    "build_codex_profile",
    "decode_claude_alias",
    "encode_claude_alias",
]


@dataclass(frozen=True)
class CodexProfile:
    """A Codex profile that routes `openrouter/<author>/<model>` through Reverso."""

    profile_name: str
    provider: str
    model: str
    base_url: str
    body: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ClaudeLauncher:
    """A Claude launcher that binds the provider-scoped catalog to Reverso."""

    provider: str
    alias: str
    upstream_model: str
    command: str
    body: dict[str, object] = field(default_factory=dict)


def encode_claude_alias(upstream_model: str) -> str:
    """Encode `author/model` into a Claude-discoverable alias."""
    if not upstream_model or "/" not in upstream_model:
        raise ValueError(
            f"upstream_model must be 'author/model', got {upstream_model!r}"
        )
    encoded = (
        base64.urlsafe_b64encode(upstream_model.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"anthropic-openrouter-{encoded}"


def decode_claude_alias(alias: str) -> str:
    """Decode a Claude-discoverable alias back to its upstream identity."""
    if not alias.startswith("anthropic-openrouter-"):
        raise ValueError(
            f"alias must start with 'anthropic-openrouter-', got {alias!r}"
        )
    body = alias[len("anthropic-openrouter-") :]
    if not body:
        raise ValueError(f"alias body is empty: {alias!r}")
    padded = body + "=" * (-len(body) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"alias is not a valid base64url body: {alias!r}") from exc


def build_claude_alias(upstream_model: str) -> str:
    """Build the Claude alias for an upstream model."""
    return encode_claude_alias(upstream_model)


def build_codex_profile(
    *, provider: str, upstream_model: str, gateway_url: str
) -> CodexProfile:
    """Build a Codex profile that points at the local Reverso gateway."""
    body = {
        "name": "reverso-openrouter",
        "provider": provider,
        "model": f"{provider}/{upstream_model}",
        "base_url": f"{gateway_url.rstrip('/')}/v1",
    }
    return CodexProfile(
        profile_name=body["name"],
        provider=provider,
        model=body["model"],
        base_url=body["base_url"],
        body=body,
    )


def build_claude_launcher(
    *, provider: str, upstream_model: str, gateway_url: str
) -> ClaudeLauncher:
    """Build a Claude launcher that binds the provider-scoped catalog."""
    alias = build_claude_alias(upstream_model)
    body = {
        "provider": provider,
        "model": alias,
        "base_url": gateway_url.rstrip("/"),
    }
    command = f"ANTHROPIC_BASE_URL={gateway_url} claude --model {alias}"
    return ClaudeLauncher(
        provider=provider,
        alias=alias,
        upstream_model=upstream_model,
        command=command,
        body=body,
    )
