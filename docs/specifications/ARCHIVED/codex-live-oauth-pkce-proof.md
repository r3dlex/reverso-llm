---
Status: complete. Archived after PRs #70, #71, and #72 merged. Retained as ADR/reference evidence for codex-direct.
Completion: codex-direct track completed by PRs #70, #71, and #72; archived as ADR/reference history.

title: Live Codex OAuth/PKCE Backend Proof Integration
status: active
created: 2026-07-03
source: northstar
slug: codex-live-oauth-pkce-proof
---

# Live Codex OAuth/PKCE Backend Proof Integration

## Problem

The fake-upstream `CodexDirectAdapter` proof shows Reverso can satisfy its `ProviderAdapter` boundary without Codex CLI. It does **not** prove that real ChatGPT/Codex OAuth or PKCE-backed services can be used safely and reliably as a live Reverso provider backend.

This plan defines the next proof: live, opt-in, secret-safe validation of real Codex authentication and backend behavior.

## Official evidence baseline

Refreshed from official OpenAI Codex docs on 2026-07-03:

- Codex supports ChatGPT sign-in for subscription access and API-key auth for usage-based access. CLI/IDE auth is cached locally in `~/.codex/auth.json` or OS credential storage.
- For trusted automation, Codex access tokens are intended for Codex local workflows; API keys remain the recommended credential for general API automation.
- The Codex CI/CD auth guide recommends using Codex's built-in refresh flow for ChatGPT-managed auth instead of calling OAuth token endpoints directly.
- The Codex App Server supports device-code login and experimental externally managed ChatGPT tokens, with explicit experimental API capability gates.
- The Codex SDK controls a local Codex app-server over JSON-RPC and includes a pinned Codex CLI runtime.

## Scope

### In scope

1. Add an opt-in live proof harness that can be run only on a trusted local machine.
2. Prove auth readiness without printing secrets:
   - detect auth mode/status,
   - verify token presence only as booleans / redacted metadata,
   - confirm refresh strategy or fail closed.
3. Try the official-supported path first:
   - Codex CLI/App Server/SDK local server with ChatGPT-managed auth,
   - device-code or existing `auth.json` flow where appropriate,
   - map the result back to Reverso proof evidence.
4. Try private direct backend HTTP only as an explicit second lane:
   - must require `REVERSO_CODEX_DIRECT_LIVE_PROOF=1`,
   - must use local-only trusted credentials,
   - must log only status/model/shape metadata.
5. Update ADR 0016 with live proof evidence and a go/no-go decision.

### Out of scope

- Mounting `codex_direct` in `ResponsesGatewayApp` or Anthropic surface.
- Making direct Codex OAuth the default.
- Persisting auth artifacts outside Codex's own cache/keychain or approved local secret source.
- Running against public CI, forked PRs, shared machines, or untrusted runners.
- Claiming private backend HTTP is an official supported API.

## Sliced goals

### Slice 1: Live-proof safety harness

Acceptance criteria:

- [x] Add a local-only script or pytest marker for live Codex proof that is skipped unless explicit env opt-in is present.
- [x] Harness refuses to run when `REVERSO_CODEX_DIRECT_LIVE_PROOF=1` is absent.
- [x] Harness records only redacted/shape metadata.
- [x] Unit tests prove no live proof can run by default.

### Slice 2: Official-supported live proof lane

Acceptance criteria:

- [ ] Determine whether Codex App Server or SDK can run a minimal prompt with ChatGPT-managed auth on the local machine.
- [ ] Capture response shape, model, streaming support, usage/rate-limit metadata availability, and failure mode without secrets.
- [ ] Decide whether SDK/app-server can be adapted as a Reverso backend without unacceptable agent/session semantic mismatch.

### Slice 3: Private direct backend HTTP proof lane

Acceptance criteria:

- [ ] With explicit opt-in only, call the private direct Codex backend using the existing explicit factory/upstream path.
- [ ] Capture only response envelope shape and status metadata.
- [ ] Verify auth refresh is not manually reimplemented unless official docs or observed Codex behavior support it safely.
- [ ] Fail closed on 401/403/404/schema drift with typed, secret-free errors.

### Slice 4: ADR decision and next integration plan

Acceptance criteria:

- [ ] ADR 0016 is updated with live proof results.
- [ ] One of these decisions is recorded:
  - official SDK/app-server backend viable,
  - private direct HTTP viable but unsupported/risky,
  - live backend not viable; keep CLI/fake-upstream only.
- [ ] If viable, create a separate implementation plan for a still-unmounted experimental provider slug.

## Verification plan

- Default test suite must pass without live network or auth.
- Live proof tests must be skipped unless explicit opt-in env vars are set.
- Secret scan over generated artifacts must find no token material.
- Any live run report must include only:
  - auth mode/status booleans,
  - endpoint lane name,
  - HTTP/status category,
  - response shape keys,
  - model id if returned,
  - usage/rate-limit metadata presence, not raw token values.

## Residual risks

- Official Codex SDK/app-server may not fit Reverso's provider semantics because it controls an agent runtime rather than a simple model API.
- Private backend HTTP may be unstable or unsupported.
- Real auth refresh race conditions require serialization around `auth.json`.
- Account policy or workspace admin settings may block access tokens or ChatGPT-managed local automation.
