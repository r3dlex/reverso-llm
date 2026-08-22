"""Unit tests for OpenRouter bounded observability (OR-G7).

These tests assert that the runtime emits bounded counters, never records
secrets or capability handles, and exposes a stable status shape.
"""

from __future__ import annotations


from reverso.protocols.adapters.openrouter.observability import (
    OpenRouterMetrics,
    StatusSnapshot,
    snapshot_status,
)


def test_metrics_counter_increments_on_admission() -> None:
    metrics = OpenRouterMetrics()
    metrics.record_admission(surface="codex", model="stealth/ox-alpha")
    metrics.record_admission(surface="codex", model="stealth/ox-alpha")
    assert metrics.admissions["codex:stealth/ox-alpha"] == 2


def test_metrics_counter_records_denial_reason() -> None:
    metrics = OpenRouterMetrics()
    metrics.record_denial(reason="non_allowlisted_model")
    metrics.record_denial(reason="stale_policy")
    metrics.record_denial(reason="non_allowlisted_model")
    counter = dict(metrics.denials)
    assert counter["non_allowlisted_model"] == 2
    assert counter["stale_policy"] == 1


def test_metrics_never_record_secrets_or_prompts() -> None:
    metrics = OpenRouterMetrics()
    metrics.record_admission(
        surface="codex",
        model="stealth/ox-alpha",
        request_id="req-fixture",
        reported_cost_usd=0.0,
    )
    text = repr(metrics)
    assert "sk-or-v1-" not in text
    assert "cap_" not in text
    assert "ping" not in text  # not a prompt
    assert "secret" not in text.lower()


def test_status_snapshot_includes_safe_fields() -> None:
    metrics = OpenRouterMetrics()
    metrics.record_admission(
        surface="codex", model="stealth/ox-alpha", request_id="req-1"
    )
    snapshot = snapshot_status(metrics, last_request_id="req-1")
    assert isinstance(snapshot, StatusSnapshot)
    assert snapshot.admissions_total == 1
    assert snapshot.last_request_id == "req-1"
    text = repr(snapshot)
    assert "sk-or-v1-" not in text


def test_metrics_export_is_counter_dicts() -> None:
    metrics = OpenRouterMetrics()
    metrics.record_admission(surface="claude", model="stealth/ox-alpha")
    metrics.record_denial(reason="request_above_limit")
    out = metrics.export()
    assert isinstance(out["admissions"], dict)
    assert isinstance(out["denials"], dict)
