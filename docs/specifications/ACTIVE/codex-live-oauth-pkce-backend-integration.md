---
title: Codex live OAuth/PKCE backend proof integration
status: active
created: 2026-07-03
slug: codex-live-oauth-pkce-backend-integration
---

# Codex live OAuth/PKCE backend proof integration

## Intent

Convert the current local OAuth/PKCE proof harness into an Autobahn-ready integration plan for a **staged Codex backend proof** in Reverso.

The user-selected success state is **production backend**: after live proof and ADR go/no-go, Reverso should expose a real selectable Codex OAuth/PKCE-backed backend for normal local Reverso use. Proof and promotion remain staged so unsafe live auth/network behavior never runs by default or in CI.

## Current evidence

- `src/reverso/protocols/adapters/codex_direct.py` contains an experimental `ProviderAdapter` with injected upstream support and no default HTTP mount.
- `src/reverso/protocols/adapters/codex_live_proof.py` and `scripts/codex-live-proof.py` provide a secret-free, opt-in proof harness.
- `tests/unit/test_codex_live_proof.py tests/unit/test_codex_direct_adapter.py -q` passes locally with 19 tests.
- Existing docs declare Codex direct OAuth/PKCE HTTP as experimental/private and unsuitable for default production exposure without a go/no-go ADR update.

## Success criteria

1. Live proof remains **local-only and opt-in**; no live auth/network runs in CI or by default.
2. Official-supported lane runs first and emits a redacted JSON proof report for Codex CLI/app-server or equivalent supported ChatGPT-managed path.
3. Direct HTTP lane is explicit-only and fail-closed; 401/403/404/schema mismatches produce secret-free reports rather than fallback behavior.
4. A production-ready backend route/profile is implemented behind explicit configuration, with a kill switch and safe local defaults.
5. `ResponsesGatewayApp` integration tests prove default safety, configured backend registration, error mapping, streaming/non-streaming behavior, and redaction.
6. ADR 0016 or a successor ADR records go/no-go outcomes from live proof evidence before production exposure is enabled.
7. The backend is selectable through the normal Reverso profile/model surfaces only after the live proof gate and ADR gate are satisfied.

## Non-goals

- default-on remains no-go pending ADR approval.
- No public/non-loopback exposure.
- No CI live OAuth, real account, real network, or secret-bearing fixture.
- No storage of ChatGPT/Codex tokens in repo artifacts.
- No broad rewrite of `ProviderAdapter`, `ResponsesGatewayApp`, profile routing, or model catalog behavior.
- No support promise for private ChatGPT/Codex backend endpoints without explicit ADR acceptance of that risk.

## Decision boundaries

- Agents may implement local opt-in proof code, tests, docs, production backend wiring, and model/profile exposure gates.
- Agents may not run live OAuth/network proof unless the environment is explicitly opted in on the local machine.
- Agents may not enable normal model/profile selection until the live proof gate and ADR/go-no-go gate are represented in code/docs.
- If official CLI/app-server proof fails but direct HTTP succeeds, the plan must require an explicit ADR risk acceptance before production selection is enabled.

## Sliced Autobahn plan

### Slice 1: Proof harness hardening

Lock the current proof harness as a testable baseline.

Acceptance criteria:
- Default script execution exits `skipped` without calling auth, subprocess, or network.
- JSON report schema is stable and contains only secret-free fields.
- Targeted tests cover default skip, opt-in gates, redaction, and fake upstream success.

### Slice 2: Official-first live proof lane

Add a local opt-in official lane that calls the supported Codex CLI/app-server path and records evidence.

Acceptance criteria:
- `REVERSO_CODEX_OFFICIAL_LIVE_PROOF=1` is required.
- The report captures status, model, response-shape keys, usage/rate-limit presence when observable, and sanitized failure reason.
- Unit tests mock subprocess/app-server behavior; no live run in CI.

### Slice 3: Direct HTTP live proof lane

Add a local opt-in direct lane using `ProviderAuth` plus explicit `HttpCodexDirectUpstream`.

Acceptance criteria:
- `REVERSO_CODEX_DIRECT_LIVE_PROOF=1` is required.
- 401/403/404, non-object JSON, schema mismatch, and timeout are reported fail-closed and redacted.
- No fallback from direct HTTP to CLI occurs inside the same lane.

### Slice 4: Production backend mount behind gates

Wire a production-shaped Reverso route/profile for the Codex OAuth/PKCE backend, but keep exposure controlled by explicit configuration, proof status, and kill switch.

Acceptance criteria:
- The backend is not reachable by default and never requires live secrets in tests.
- The backend uses existing `ProviderAdapter`, `ProviderAuth`, `ResponsesGatewayApp`, and profile/model exposure seams rather than a parallel server stack.
- Tests prove default absence/safety, configured registration, kill-switch behavior, streaming/non-streaming response behavior, and secret-free errors.

### Slice 5: Model/profile selection and operator UX

Make the backend selectable through normal Reverso local configuration after gates are satisfied.

Acceptance criteria:
- Model/profile catalog exposure is explicit and does not collide with built-in Codex GPT selector defaults.
- Documentation shows how to enable, disable, verify, and rollback the backend locally.
- Usage/rate-limit visibility is exposed when observable and otherwise reported as unavailable without failing open.

### Slice 6: Evidence packet and ADR go/no-go

Produce the operator-facing evidence packet and ADR update for deciding whether production backend exposure is accepted.

Acceptance criteria:
- A manual runbook explains official-first/direct-second execution and where JSON proof artifacts are stored locally.
- ADR 0016 or a successor ADR records live findings, risk acceptance/rejection, and the production exposure decision.
- Production selection is enabled only when the ADR says go; otherwise the backend remains proof-only or is removed.

## Verification

Minimum local validation before Autobahn handoff completion:

```bash
.venv/bin/python -m pytest tests/unit/test_codex_live_proof.py tests/unit/test_codex_direct_adapter.py -q
.venv/bin/python scripts/codex-live-proof.py --lane official --json
.venv/bin/python scripts/codex-live-proof.py --lane direct --json
```

Expected default script behavior: both lanes skip cleanly unless the lane-specific opt-in environment variable is present. Expected default backend behavior: the production backend is not exposed until configured and ADR-gated.

## Implementation evidence (2026-07-03)

Autobahn local implementation adds a guarded `codex-direct` backend mount:

- `/codex-direct/v1/...` is a reserved first-party route.
- The direct backend is not mounted by default.
- `REVERSO_CODEX_DIRECT_BACKEND=1` mounts `CodexDirectAdapter` with `CodexOAuthAuth` and `HttpCodexDirectUpstream`.
- `CodexOAuthAuth.bearer_token()` is available only for gated direct-backend callers; `resolve()` remains secret-free.
- UX keeps GPT built-in bare and exposes `codex-direct/<model>` direct-backend slugs only when `REVERSO_CODEX_DIRECT_BACKEND=1` is enabled.

Verification:

- Unit: `101 passed` for auth, direct adapter, live proof harness, model exposure, and codex sync tests.
- Live direct proof: skipped fail-closed without `REVERSO_CODEX_DIRECT_LIVE_PROOF=1`.
- Live official proof: skipped fail-closed without `REVERSO_CODEX_OFFICIAL_LIVE_PROOF=1` or `REVERSO_CODEX_LIVE_PROOF=1`.

ADR: opt-in; default-off; default-on remains no-go.
