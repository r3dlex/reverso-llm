"""Unit tests for the OpenRouter composition-owned runtime (OR-G1, U1..U6)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.openrouter.budget import (
    BudgetLedger,
    InsufficientBudgetError,
    MissingLimitsError,
)
from reverso.protocols.adapters.openrouter.catalog import (
    OpenRouterCatalogEntry,
    OpenRouterCatalogSource,
)
from reverso.protocols.adapters.openrouter.credentials import (
    OpenRouterCredentialError,
    resolve_api_key,
)
from reverso.protocols.adapters.openrouter.grants import (
    GrantRegistry,
    InvalidGrantError,
)
from reverso.protocols.adapters.openrouter.policy import (
    FreshnessBoundError,
    OpenRouterPolicy,
    PolicyEvaluator,
)
from reverso.protocols.adapters.openrouter.runtime import (
    OpenRouterRuntime,
    build_openrouter_runtime,
    get_or_create_runtime,
    reset_openrouter_runtime,
)
from reverso.protocols.adapters.openrouter.adapter import OpenRouterAdapter

CANARY_KEY = "sk-or-v1-CANARYCANARYCANARYCANARY0000"
CANARY_PATH = "/Users/canary-user/.zsh_exports"


# --- Test doubles --------------------------------------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def list_models(self) -> tuple[int, dict[str, Any]]:
        self.calls.append(("GET", "/api/v1/models", None))
        return 200, {"data": []}

    async def create_response(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(("POST", "/api/v1/responses", payload))
        return 200, {
            "id": "gen-fixture",
            "object": "response",
            "status": "completed",
            "model": payload.get("model"),
            "output": [],
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0},
        }


@dataclass
class FakeClock:
    now: float = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeLauncherLiveness:
    alive: bool = True

    def __call__(self) -> bool:
        return self.alive


# --- U1: frozen boundaries and lifecycle --------------------------------------


def test_adapter_satisfies_provider_adapter_protocol() -> None:
    adapter = OpenRouterAdapter(
        transport=FakeTransport(),
        credentials=mock.Mock(resolve_api_key=lambda: "sk-or-v1-test"),
    )
    assert hasattr(adapter, "create_response")
    assert hasattr(adapter, "stream_response")
    assert hasattr(adapter, "list_models")


def test_get_or_create_runtime_is_singleton() -> None:
    reset_openrouter_runtime()
    try:
        kwargs = _runtime_kwargs()
        a = get_or_create_runtime(**kwargs)
        b = get_or_create_runtime(**kwargs)
        assert a is b
    finally:
        reset_openrouter_runtime()


# --- U2: model identity, allowlist, surface eligibility -----------------------


def test_presented_model_strips_only_one_outer_prefix() -> None:
    runtime = _build_test_runtime()
    assert runtime.upstream_model("openrouter/stealth/ox-alpha") == "stealth/ox-alpha"
    with pytest.raises(ValueError):
        runtime.upstream_model("stealth/ox-alpha")


def test_presented_model_rejects_unknown_qualifier() -> None:
    runtime = _build_test_runtime()
    with pytest.raises(ValueError):
        runtime.upstream_model("acme/stealth/ox-alpha")


def test_runtime_rejects_empty_or_malformed_alias() -> None:
    runtime = _build_test_runtime()
    for bad in ("", "openrouter/", "/", "openrouter//x", "openrouter/x/y/z"):
        with pytest.raises(ValueError):
            runtime.upstream_model(bad)


# --- U3: routing, privacy, and attribution ------------------------------------


def test_default_policy_injects_required_routing_fields() -> None:
    runtime = _build_test_runtime()
    request = ResponsesRequest(
        model="openrouter/stealth/ox-alpha",
        input="ping",
        extra={"provider": {"sort": "price"}},
    )
    routed = runtime.apply_mandatory_policy(request)
    assert routed.extra["provider"]["require_parameters"] is True
    assert routed.extra["provider"]["data_collection"] == "deny"
    assert routed.extra["provider"]["zdr"] is True
    assert routed.extra["provider"]["only"] is not None


def test_caller_cannot_relax_required_parameters() -> None:
    runtime = _build_test_runtime()
    request = ResponsesRequest(
        model="openrouter/stealth/ox-alpha",
        input="ping",
        extra={"provider": {"require_parameters": False, "data_collection": "allow"}},
    )
    routed = runtime.apply_mandatory_policy(request)
    assert routed.extra["provider"]["require_parameters"] is True
    assert routed.extra["provider"]["data_collection"] == "deny"


def test_default_attribution_header_is_reverso() -> None:
    runtime = _build_test_runtime()
    request = ResponsesRequest(model="openrouter/stealth/ox-alpha", input="ping")
    routed = runtime.apply_mandatory_policy(request)
    assert routed.extra.get("X-OpenRouter-Title") == "Reverso"
    assert "HTTP-Referer" not in routed.extra


# --- U4: ephemeral attended grants --------------------------------------------


def test_grant_registry_rejects_unbound_capability() -> None:
    registry = GrantRegistry()
    with pytest.raises(InvalidGrantError):
        registry.verify(
            capability="cap_invalid",
            launcher_pid=1234,
            model="stealth/ox-alpha",
            liveness=lambda: True,
        )


def test_grant_registry_revokes_on_launcher_exit() -> None:
    liveness = FakeLauncherLiveness(alive=True)
    registry = GrantRegistry()
    capability = registry.issue(
        launcher_pid=1234, model="stealth/ox-alpha", liveness=liveness
    )
    assert registry.verify(
        capability=capability,
        launcher_pid=1234,
        model="stealth/ox-alpha",
        liveness=liveness,
    )
    liveness.alive = False
    with pytest.raises(InvalidGrantError):
        registry.verify(
            capability=capability,
            launcher_pid=1234,
            model="stealth/ox-alpha",
            liveness=liveness,
        )


def test_grant_registry_enforces_ttl() -> None:
    clock = FakeClock()
    liveness = FakeLauncherLiveness(alive=True)
    registry = GrantRegistry(clock=clock, max_ttl_seconds=10)
    capability = registry.issue(
        launcher_pid=1234, model="stealth/ox-alpha", liveness=liveness
    )
    clock.now += 11
    with pytest.raises(InvalidGrantError):
        registry.verify(
            capability=capability,
            launcher_pid=1234,
            model="stealth/ox-alpha",
            liveness=liveness,
        )


def test_runtime_reconstruct_clears_all_grants() -> None:
    liveness = FakeLauncherLiveness(alive=True)
    runtime = _build_test_runtime()
    capability = runtime.grants.issue(
        launcher_pid=1234, model="stealth/ox-alpha", liveness=liveness
    )
    assert runtime.grants.verify(
        capability=capability,
        launcher_pid=1234,
        model="stealth/ox-alpha",
        liveness=liveness,
    )
    runtime2 = _build_test_runtime()
    assert runtime2.grants is not runtime.grants
    with pytest.raises(InvalidGrantError):
        runtime2.grants.verify(
            capability=capability,
            launcher_pid=1234,
            model="stealth/ox-alpha",
            liveness=liveness,
        )


# --- U5: budget ledger ---------------------------------------------------------


def test_budget_rejects_missing_limits() -> None:
    ledger = BudgetLedger()
    with pytest.raises(MissingLimitsError):
        ledger.reserve(
            request_tokens=10, completion_tokens=10, model="stealth/ox-alpha"
        )


def test_budget_reservation_is_atomic() -> None:
    ledger = BudgetLedger(request_limit_usd="0.05", session_limit_usd="0.10")
    res_a = ledger.reserve(
        request_tokens=100,
        completion_tokens=100,
        model="stealth/ox-alpha",
        pricing_prompt="1",
        pricing_completion="2",
    )
    res_b = ledger.reserve(
        request_tokens=100,
        completion_tokens=100,
        model="stealth/ox-alpha",
        pricing_prompt="1",
        pricing_completion="2",
    )
    assert res_a.id != res_b.id
    assert ledger.remaining_usd() < Decimal("0.10")


def test_budget_releases_on_failure() -> None:
    ledger = BudgetLedger(request_limit_usd="0.05", session_limit_usd="0.10")
    res = ledger.reserve(
        request_tokens=100,
        completion_tokens=100,
        model="stealth/ox-alpha",
        pricing_prompt="1",
        pricing_completion="2",
    )
    ledger.release(res)
    assert ledger.remaining_usd() == Decimal("0.10")


def test_budget_rejects_request_above_request_limit() -> None:
    ledger = BudgetLedger(request_limit_usd="0.001", session_limit_usd="0.10")
    with pytest.raises(InsufficientBudgetError):
        ledger.reserve(
            request_tokens=1000,
            completion_tokens=1000,
            model="stealth/ox-alpha",
            pricing_prompt="1",
            pricing_completion="1",
        )


# --- U6: credentials and bounded errors ---------------------------------------


def test_resolve_api_key_reads_only_zsh_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    fake_path = Path("/tmp/fake-home/.zsh_exports")
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_text(
        'export OPENROUTER_API_KEY="sk-or-v1-fixture"\n', encoding="utf-8"
    )
    assert resolve_api_key(home=Path("/tmp/fake-home")) == "sk-or-v1-fixture"


def test_resolve_api_key_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/empty-home")
    Path("/tmp/empty-home").mkdir(parents=True, exist_ok=True)
    with pytest.raises(OpenRouterCredentialError):
        resolve_api_key(home=Path("/tmp/empty-home"))


def test_outbound_headers_redact_authorization() -> None:
    runtime = _build_test_runtime()
    headers = runtime.outbound_headers()
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer sk-or-v1-")
    assert "sk-or-v1-test" in headers["Authorization"]


# --- Policy evaluator ---------------------------------------------------------


def test_policy_rejects_stale_evidence() -> None:
    clock = FakeClock()
    policy = OpenRouterPolicy(
        revision="rev-1",
        observed_at=clock.now,
        requires_zdr=True,
        denies_data_collection=True,
    )
    clock.now += 700
    evaluator = PolicyEvaluator(
        policy=policy,
        freshness_bound_seconds=600,
        clock=clock,
    )
    with pytest.raises(FreshnessBoundError):
        evaluator.assert_fresh()


# --- Composition root ---------------------------------------------------------


def test_admission_denies_non_allowlisted_model() -> None:
    runtime, transport = _build_runtime_with_transport(scenario="happy_path")
    with pytest.raises(PermissionError):
        runtime.admit_and_dispatch(
            surface="codex",
            presented_model="openrouter/another/model",
            request=ResponsesRequest(model="openrouter/another/model", input="ping"),
        )
    assert not transport.calls


def test_admission_denies_stale_policy() -> None:
    runtime, transport = _build_runtime_with_transport(scenario="stale_policy")
    with pytest.raises(Exception):
        runtime.admit_and_dispatch(
            surface="codex",
            presented_model="openrouter/stealth/ox-alpha",
            request=ResponsesRequest(model="openrouter/stealth/ox-alpha", input="ping"),
        )
    assert not transport.calls


def test_allowlist_membership_required() -> None:
    runtime = _build_test_runtime(allowlist={"openrouter/another/model"})
    with pytest.raises(PermissionError):
        runtime.assert_allowlisted("openrouter/stealth/ox-alpha")


def test_surface_compatibility_required() -> None:
    runtime = _build_test_runtime(
        surface_grants=[]  # explicit empty list still lets all surfaces through
    )
    # With no surface grants registered, all surfaces are eligible by default.
    runtime.assert_surface_eligible("openrouter/stealth/ox-alpha", surface="claude")


def test_surface_compatibility_denies_unlisted() -> None:
    from reverso.protocols.adapters.openrouter.runtime import OpenRouterSurfaceGrant

    runtime = _build_test_runtime(
        surface_grants=[
            OpenRouterSurfaceGrant(
                model="stealth/ox-alpha",
                surface="codex",
                source=OpenRouterCatalogSource.LIVE_AUTHED,
            )
        ]
    )
    with pytest.raises(PermissionError):
        runtime.assert_surface_eligible("openrouter/stealth/ox-alpha", surface="claude")


# --- Leak scan ----------------------------------------------------------------


def test_no_secrets_in_serialized_artifacts() -> None:
    runtime = _build_test_runtime()
    text = repr(runtime.outbound_headers())
    assert "CANARY" not in text
    assert CANARY_KEY not in text


def test_no_secrets_in_budget_repr() -> None:
    ledger = BudgetLedger(request_limit_usd="0.05", session_limit_usd="0.10")
    text = repr(ledger)
    assert CANARY_KEY not in text


def test_no_paths_in_serialized_artifacts() -> None:
    runtime = _build_test_runtime()
    text = repr(runtime)
    assert CANARY_PATH not in text


# --- Helpers ------------------------------------------------------------------


def _build_test_runtime(**overrides: Any) -> OpenRouterRuntime:
    runtime, _ = _build_runtime_with_transport(scenario="happy_path", **overrides)
    return runtime


def _runtime_kwargs(**overrides: Any) -> dict[str, Any]:
    catalog_entry = OpenRouterCatalogEntry(
        id="stealth/ox-alpha",
        context_length=1048576,
        top_provider_max_completion_tokens=131072,
        pricing_prompt="0",
        pricing_completion="0",
        supported_parameters=("include_reasoning", "max_tokens", "reasoning", "tools"),
        source=OpenRouterCatalogSource.LIVE_AUTHED,
    )
    catalog = mock.Mock()
    catalog.freshness_bound_seconds = 600
    catalog.lookup = lambda presented_model: catalog_entry
    catalog.iter_allowlisted = lambda: iter([("stealth/ox-alpha", catalog_entry)])
    clock = FakeClock()
    policy = OpenRouterPolicy(
        revision="rev-1",
        observed_at=clock.now,
        requires_zdr=True,
        denies_data_collection=True,
    )
    transport = FakeTransport()
    credentials = mock.Mock(resolve_api_key=lambda: "sk-or-v1-test")
    kwargs = dict(
        transport=transport,
        credentials=credentials,
        catalog=catalog,
        policy=policy,
        freshness_bound_seconds=600,
        allowlist={"openrouter/stealth/ox-alpha"},
        request_limit_usd="0.05",
        session_limit_usd="0.10",
        compatibility_providers=("zdr_only_endpoint_providers",),
        clock=clock,
    )
    kwargs.update(overrides)
    return kwargs


def _build_runtime_with_transport(
    scenario: str, **overrides: Any
) -> tuple[OpenRouterRuntime, FakeTransport]:
    kwargs = _runtime_kwargs(**overrides)
    if scenario == "stale_policy":
        kwargs["policy"] = OpenRouterPolicy(
            revision="rev-1",
            observed_at=kwargs["clock"].now - 3600,
            requires_zdr=True,
            denies_data_collection=True,
        )
    runtime = build_openrouter_runtime(**kwargs)
    return runtime, kwargs["transport"]
