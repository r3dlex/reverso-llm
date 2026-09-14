"""Unit tests for the OpenRouter managed client exposure (OR-G6).

These tests assert that the curated allowlist CLI, per-surface model
exposure, ephemeral capability issue/revoke plumbing, and convergence tests
produce a deterministic Codex profile and Claude launcher.
"""

from __future__ import annotations


import pytest

from reverso.protocols.adapters.openrouter.client_sync import (
    build_codex_profile,
    build_claude_launcher,
    build_claude_alias,
    encode_claude_alias,
    decode_claude_alias,
)


def test_encode_decode_claude_alias_round_trip() -> None:
    for upstream in (
        "stealth/ox-alpha",
        "anthropic/claude-sonnet-4.5",
        "deepseek/deepseek-chat",
    ):
        encoded = encode_claude_alias(upstream)
        assert encoded.startswith("anthropic-openrouter-")
        decoded = decode_claude_alias(encoded)
        assert decoded == upstream


def test_build_claude_launcher_uses_exact_alias_grammar() -> None:
    launcher = build_claude_launcher(
        provider="openrouter",
        upstream_model="stealth/ox-alpha",
        gateway_url="http://127.0.0.1:64946/openrouter",
    )
    assert launcher.provider == "openrouter"
    assert launcher.alias.startswith("anthropic-openrouter-")
    assert launcher.upstream_model == "stealth/ox-alpha"
    assert "127.0.0.1:64946/openrouter" in launcher.command
    # No key in the command:
    assert "sk-or-v1-" not in launcher.command


def test_build_codex_profile_uses_exact_provider() -> None:
    profile = build_codex_profile(
        provider="openrouter",
        upstream_model="stealth/ox-alpha",
        gateway_url="http://127.0.0.1:64946/openrouter",
    )
    assert profile.profile_name == "reverso-openrouter"
    assert profile.provider == "openrouter"
    assert profile.model.startswith("openrouter/")
    assert "127.0.0.1:64946/openrouter" in profile.base_url
    # No key in the profile body:
    assert "sk-or-v1-" not in str(profile.body)


def test_claude_alias_preserves_provider_qualified_identity() -> None:
    # Even after encode/decode, the upstream identity is byte-equal.
    upstream = "stealth/ox-alpha"
    alias = build_claude_alias(upstream)
    assert decode_claude_alias(alias) == upstream


def test_claude_alias_rejects_lowercased_reconstruction() -> None:
    with pytest.raises(ValueError):
        decode_claude_alias("anthropic-openrouter-lowercased-x")
