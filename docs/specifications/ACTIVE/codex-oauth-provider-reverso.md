---
Completion: codex-direct track completed by PRs #70, #71, and #72; retained in ACTIVE as ADR/reference history.

title: Codex OAuth Direct Provider Feasibility for Reverso
status: active
created: 2026-07-03
source: northstar
slug: codex-oauth-provider-reverso
---

# Codex OAuth Direct Provider Feasibility for Reverso

## Problem

Reverso currently has a Codex provider path oriented around Codex CLI execution and ChatGPT/Codex OAuth validation. The user wants to know whether OAuth PKCE / ChatGPT-managed Codex credentials, as demonstrated by projects such as `7shi/codex-oauth`, can be used to implement Codex as a direct provider for Claude Code over Reverso, potentially avoiding Codex CLI execution.

## Feasibility answer

**Likely technically possible, but not yet production-safe as a default.**

A direct Reverso provider can probably be spiked by reading the same ChatGPT-managed Codex auth cache already validated by `CodexOAuthAuth`, refreshing tokens safely, and calling the Codex/WHAM backend with a `ProviderAdapter` implementation. However, the direct backend path is not documented as a stable public OpenAI API. The official surfaces are Codex CLI/app/IDE, Codex access tokens for trusted Codex local automation, and Codex SDKs that control local Codex agents. Therefore the direct HTTP approach must remain behind an experimental gate until policy, endpoint stability, streaming, tools, and usage/rate-limit behavior are proven.

## Evidence

### Repo-local evidence

- `src/reverso/protocols/adapters/codex.py` already contains `CodexOAuthAuth` and a CLI-backed `CodexAdapter` under the frozen `ProviderAdapter` contract.
- `src/reverso/protocols/adapters/codex_direct.py` now contains an unmounted proof adapter that satisfies `ProviderAdapter` with fake-upstream tests, requires injected upstream transport by default, and does not touch live Codex credentials.
- `docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md` accepted Codex CLI-backed ChatGPT OAuth and explicitly excluded direct OpenAI SDK/direct ChatGPT OAuth HTTP for that milestone.
- `src/reverso/protocols/responses_app.py` and `src/reverso/protocols/anthropic_app.py` route path-prefixed provider adapters, so a direct Codex backend can be added as a separate backend implementation or experimental mode without changing the inbound Claude Code contract.

### External evidence

- OpenAI Codex authentication docs describe ChatGPT sign-in, local auth caching, automatic refresh during use, API-key auth, and enterprise access tokens for trusted Codex local workflows.
- Official Codex access-token docs say Codex access tokens are for Codex local workflows and that Platform API keys remain the right credential for general OpenAI API calls.
- Official Codex SDK docs describe TypeScript/Python SDKs for programmatically controlling local Codex agents; this is official but has agent/thread semantics rather than a simple model-provider API.
- `7shi/codex-oauth` demonstrates browser OAuth/PKCE access to OpenAI's WHAM backend using `openai-python`, but states that the API behavior was reverse-engineered and is not officially documented.
- `openresponses/codex` documents direct access to `https://chatgpt.com/backend-api/codex`, using `~/.codex/auth.json`, token refresh, and required headers/body quirks. This is useful prior art, not an official contract.

## Scope

### In scope

1. Spike a direct Codex OAuth HTTP provider behind an explicit experimental flag or alternate provider slug.
2. Reuse or factor existing `CodexOAuthAuth` token discovery without exposing secrets.
3. Compare three backend choices:
   - existing CLI-backed `CodexAdapter`,
   - official Codex SDK adapter/proxy feasibility,
   - direct private Codex backend adapter feasibility.
4. Validate models, non-streaming response, streaming response, previous response continuity, usage/rate-limit capture, and failure handling.
5. Update ADR 0007 or add a superseding ADR documenting the direct-provider decision.

### Out of scope

- Making direct HTTP the default before spike evidence.
- Removing CLI fallback.
- Multi-tenant hosting or non-loopback exposure.
- Storing ChatGPT/Codex tokens outside local keychain/auth cache without explicit secret-management design.
- Claiming the private Codex backend is officially supported.

## Sliced goals

### Slice 1: Feasibility spike and ADR update

Acceptance criteria:

- [x] A local-only fake-upstream proof shows direct Codex OAuth HTTP can satisfy the `ProviderAdapter` boundary without logging or touching real secrets.
- [ ] A live local-only spike proves whether real direct Codex OAuth HTTP can obtain/refresh credentials without logging secrets.
- [x] A minimal text-only non-streaming fake-upstream request succeeds through the proof adapter.
- [ ] A minimal real text-only non-streaming request succeeds or fails with a typed, documented reason.
- [x] ADR 0016 is added with go/no-go criteria and policy risk for the experimental proof.
- [ ] Existing CLI-backed Codex path remains unchanged and passing tests still pass.

### Slice 2: Experimental ProviderAdapter path

Acceptance criteria:

- [ ] A direct Codex backend implementation satisfies `ProviderAdapter` for `create_response`, `stream_response`, `list_models`, `get_response`, and `list_input_items` at the same level expected of the current Codex adapter.
- [ ] The adapter is disabled by default or exposed under an experimental slug/config flag.
- [ ] Secrets are redacted in logs and errors.
- [ ] Unit tests cover auth resolution, refresh failure, upstream failure, response mapping, and stream finalization.

### Slice 3: Claude Code/Reverso integration validation

Acceptance criteria:

- [ ] Claude Code can call Reverso's Anthropic surface and receive Codex-backed output through the experimental direct provider.
- [ ] Tool-use unsupported/partial behavior is explicitly gated and documented.
- [ ] Usage/rate-limit reporting either matches existing `/usage` expectations or is explicitly unavailable with a safe empty response.
- [ ] Docs show CLI-backed default and direct OAuth experimental path side by side.

## Verification plan

- Run existing unit tests for `reverso.protocols.adapters.codex`, `responses_app`, `anthropic_app`, and auth helpers.
- Add fake-auth/fake-upstream unit tests before any real credential smoke.
- If a real smoke is run, use only local loopback, never record tokens, and capture only status/model/usage metadata.
- Compare output shape against `docs/architecture/codex-responses-parity-matrix.md`.

## Residual risks

- Direct backend may violate or drift from OpenAI's intended supported surfaces.
- Backend headers/body quirks may change without notice.
- Official SDK may be safer but may not fit Reverso's provider protocol without semantic mismatch.
- Token refresh and concurrent auth-cache writes can corrupt or invalidate credentials if not serialized.
