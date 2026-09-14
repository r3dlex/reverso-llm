---
title: OR-G1 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g1
goal: .ai/work-intake/openrouter-reverso-provider-or-g1.md
spec: docs/specifications/ACTIVE/openrouter-reverso-provider.md
test_spec: .omx/plans/test-spec-openrouter-reverso-provider.md
focused_command: uv run pytest tests/unit/test_openrouter_runtime.py tests/integration/test_openrouter_admission.py -q
verdict: GO
---

# OR-G1 red-green evidence

## Focused command

```bash
uv run pytest tests/unit/test_openrouter_runtime.py tests/integration/test_openrouter_admission.py -q
```

Equivalent in the worktree:

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_runtime.py -q
```

## Red phase

Before the runtime existed, the test file produced collection errors because
`reverso.protocols.adapters.openrouter.runtime` and its peers were absent:

```text
ImportError while importing test module .../test_openrouter_runtime.py.
ModuleNotFoundError: No module named 'reverso.protocols.adapters.openrouter.runtime'
```

The contract was therefore red: zero runtime, zero admit path, zero deny matrix.

## Green phase

```text
..........................................                               [100%]
42 passed in 0.06s
```

The 28 OR-G1 unit tests cover the U1..U6 contracts:

- `test_adapter_satisfies_provider_adapter_protocol`
- `test_get_or_create_runtime_is_singleton`
- `test_presented_model_strips_only_one_outer_prefix`
- `test_presented_model_rejects_unknown_qualifier`
- `test_runtime_rejects_empty_or_malformed_alias`
- `test_default_policy_injects_required_routing_fields`
- `test_caller_cannot_relax_required_parameters`
- `test_default_attribution_header_is_reverso`
- `test_grant_registry_rejects_unbound_capability`
- `test_grant_registry_revokes_on_launcher_exit`
- `test_grant_registry_enforces_ttl`
- `test_runtime_reconstruct_clears_all_grants`
- `test_budget_rejects_missing_limits`
- `test_budget_reservation_is_atomic`
- `test_budget_releases_on_failure`
- `test_budget_rejects_request_above_request_limit`
- `test_resolve_api_key_reads_only_zsh_exports`
- `test_resolve_api_key_missing_raises`
- `test_outbound_headers_redact_authorization`
- `test_policy_rejects_stale_evidence`
- `test_admission_denies_non_allowlisted_model`
- `test_admission_denies_stale_policy`
- `test_allowlist_membership_required`
- `test_surface_compatibility_required`
- `test_surface_compatibility_denies_unlisted`
- `test_no_secrets_in_serialized_artifacts`
- `test_no_secrets_in_budget_repr`
- `test_no_paths_in_serialized_artifacts`

The 14 OR-G0 fixture-contract tests remain green.

## Architecture

- `src/reverso/protocols/adapters/openrouter/__init__.py` exposes the package
  surface.
- `credentials.py` resolves the key from `$HOME/.zsh_exports` only.
- `catalog.py` parses authenticated `/api/v1/models` entries.
- `policy.py` evaluates freshness, ZDR, and data-denial requirements.
- `grants.py` issues per-launcher, per-model capabilities with TTL and liveness.
- `budget.py` keeps an atomic per-session ledger with `Decimal` arithmetic.
- `runtime.py` owns the composition singleton and applies mandatory routing.
- `adapter.py` implements the frozen `ProviderAdapter` Protocol.
- `responses_app.py` adds `openrouter` to `APP_PROVIDER_PREFIXES`.
- `proxy/compose.py` mounts the adapter under the `openrouter` prefix.

## Decision

OR-G1 verdict is GO. The deny-first runtime, call-time secret resolver,
authenticated catalog, policy evaluator, in-memory grant registry, atomic
budget ledger, and mounted routes are in place. The architecture is admissible
to OR-G2 (Codex native Responses unary).
