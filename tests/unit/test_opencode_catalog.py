"""OCG-G3: OpenCode Go catalog discovery and endpoint selection.

Endpoint selection is DUAL-PROTOCOL BY DEFAULT with a declared deny-list. This
inverts what ``ocgo``'s hand-maintained table implied. Measured against the live
service on 2026-08-22, 22 of 29 catalog ids answered BOTH ``/messages`` and
``/chat/completions``; the only id that refused the Anthropic format was
``grok-4.5`` ("Model grok-4.5 is not supported for format anthropic"). Every
other non-200 had an orthogonal cause (workspace opt-in, upstream outage), not
an endpoint restriction. A per-model table would therefore encode noise, and
``ocgo``'s claim that the minimax and qwen3.7-max ids REQUIRE ``/messages`` is
contradicted: all four answer chat-completions.
"""

from __future__ import annotations

from reverso.protocols.adapters.opencode.catalog import (
    ANTHROPIC_UNSUPPORTED_MODELS,
    CHAT_COMPLETIONS_PATH,
    FALLBACK_MODEL_IDS,
    MESSAGES_PATH,
    OPENCODE_GO_API_BASE,
    USER_AGENT,
    anthropic_endpoint_for,
    parse_model_ids,
    supports_anthropic_format,
)


def test_base_is_the_zen_go_surface() -> None:
    assert OPENCODE_GO_API_BASE == "https://opencode.ai/zen/go/v1"


def test_a_user_agent_is_declared() -> None:
    """Cloudflare 403s a default client fingerprint, so a UA is mandatory."""
    assert USER_AGENT
    assert "reverso" in USER_AGENT.lower()


def test_dual_protocol_is_the_default() -> None:
    for model_id in ("kimi-k3", "minimax-m3", "qwen3.7-max", "glm-5"):
        assert supports_anthropic_format(model_id) is True
        assert anthropic_endpoint_for(model_id) == MESSAGES_PATH


def test_denied_model_routes_to_chat_completions() -> None:
    assert supports_anthropic_format("grok-4.5") is False
    assert anthropic_endpoint_for("grok-4.5") == CHAT_COMPLETIONS_PATH


def test_deny_list_is_narrow_and_measured() -> None:
    """A deny-list that grew would mean the measurement was re-run; keep it honest."""
    assert ANTHROPIC_UNSUPPORTED_MODELS == frozenset({"grok-4.5"})


def test_deny_list_matching_is_case_insensitive() -> None:
    assert supports_anthropic_format("GROK-4.5") is False


def test_ocgo_table_claim_is_contradicted() -> None:
    """ocgo forced these onto /messages; measurement says they are dual."""
    for model_id in ("minimax-m3", "minimax-m2.7", "minimax-m2.5", "qwen3.7-max"):
        assert model_id not in ANTHROPIC_UNSUPPORTED_MODELS


def test_parse_model_ids_reads_the_openai_list_shape() -> None:
    payload = {
        "object": "list",
        "data": [{"id": "glm-5", "object": "model"}, {"id": "kimi-k3"}],
    }
    assert parse_model_ids(payload) == ("glm-5", "kimi-k3")


def test_parse_model_ids_is_sorted_and_deduped() -> None:
    payload = {"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": "zeta"}]}
    assert parse_model_ids(payload) == ("alpha", "zeta")


def test_parse_model_ids_skips_unusable_rows() -> None:
    payload = {"data": [{"id": ""}, {"id": None}, {"no_id": 1}, {"id": "glm-5"}, "x"]}
    assert parse_model_ids(payload) == ("glm-5",)


def test_parse_model_ids_rejects_a_malformed_payload() -> None:
    assert parse_model_ids({}) == ()
    assert parse_model_ids({"data": "not-a-list"}) == ()
    assert parse_model_ids([]) == ()


def test_fallback_is_bounded_and_covers_the_measured_catalog() -> None:
    assert len(FALLBACK_MODEL_IDS) == 29
    assert FALLBACK_MODEL_IDS == tuple(sorted(set(FALLBACK_MODEL_IDS)))
    for model_id in ("kimi-k3", "deepseek-v4-pro", "grok-4.5"):
        assert model_id in FALLBACK_MODEL_IDS


def test_every_denied_model_is_a_known_catalog_id() -> None:
    """A deny-list entry for an id that does not exist is dead configuration."""
    assert ANTHROPIC_UNSUPPORTED_MODELS <= set(FALLBACK_MODEL_IDS)
