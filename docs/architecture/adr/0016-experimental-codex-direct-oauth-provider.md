---
id: 0016
title: Experimental Direct Codex OAuth Provider Spike
status: accepted-for-spike
created: 2026-07-03
supersedes: null
related:
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
  - docs/specifications/ACTIVE/codex-oauth-provider-reverso.md
---

# ADR 0016: Experimental Direct Codex OAuth Provider Spike

## Context

ADR 0007 accepted a first-party `CodexAdapter` that validates ChatGPT/Codex OAuth and invokes Codex CLI. That decision deliberately excluded direct ChatGPT OAuth HTTP / SDK use for the milestone because the supported OpenAI surface was the CLI/app/IDE path and the direct backend was not an official stable API.

The Northstar handoff `codex-oauth-provider-reverso` asked whether Reverso can use OAuth PKCE / ChatGPT-managed Codex auth to use Codex as a provider for Claude Code without depending on Codex CLI for every request. Public prior art such as `7shi/codex-oauth` and `openresponses/codex` suggests a direct backend path can be reached, but those projects rely on reverse-engineered/private backend behavior.

## Decision

Add an isolated, unmounted experimental proof adapter: `reverso.protocols.adapters.codex_direct.CodexDirectAdapter`.

The proof adapter:

- implements the frozen `ProviderAdapter` shape for non-streaming, streaming, model listing, response lookup, and input-item lookup;
- uses an injectable `CodexDirectUpstream` transport so unit tests can prove the Reverso boundary without live credentials or network access;
- includes a small HTTP transport for the likely direct Codex backend endpoint, but only through an explicit factory; the adapter constructor requires an injected upstream and cannot talk to the network by default;
- depends on a `ProviderAuth` that can return a bearer token and supports both synchronous and asynchronous `bearer_token()` implementations;
- keeps the existing CLI-backed `CodexAdapter` unchanged as the only production Codex path, with regression coverage that `codex_direct` is not mounted by default.

## Scope

This ADR authorizes only the spike/proof surface. It does not authorize mounting the direct provider in `SURFACE_BACKENDS`, making it selectable by Claude Code, changing defaults, or removing CLI fallback.

## Consequences

Positive:

- Reverso now has executable proof that a direct Codex backend can satisfy the provider boundary without spawning Codex CLI, using an injected fake upstream by default.
- The proof is secret-safe: tests use synthetic bearer tokens and a fake upstream.
- The production path is unaffected because the new adapter is not registered in the gateway.

Negative / risks:

- The default HTTP transport targets a backend shape inferred from prior art, not an official stable API contract.
- Real auth refresh, endpoint headers, rate-limit metadata, and tool-streaming parity still require a separate live spike.
- Official Codex SDKs may be safer but may impose agent/session semantics that do not map exactly to Reverso's provider abstraction.

## Go / no-go gates before production use

Direct Codex OAuth may become a mounted experimental provider only after a follow-up PR proves:

1. token refresh and concurrent auth-cache access are safe and do not log secrets;
2. non-streaming and streaming responses work against a real local trusted Codex account;
3. model listing, usage, and rate-limit metadata are either mapped or explicitly documented as unavailable;
4. tool-call behavior is gated or mapped to Reverso's Responses/Anthropic surfaces;
5. OpenAI policy/support risk is accepted explicitly in an ADR update.

## Verification evidence

- `uv run pytest tests/unit/test_codex_direct_adapter.py -q` -> 4 passed.
- `uv run pytest tests/unit/test_codex_adapter.py tests/unit/test_codex_oauth.py tests/unit/test_codex_direct_adapter.py -q` -> 38 passed.

## Follow-up decision: local opt-in backend mount (2026-07-03)

Status: opt-in; default-off; default-on remains no-go.

The backend integration may reserve `/codex-direct/v1/...` and may mount `CodexDirectAdapter` only when
`REVERSO_CODEX_DIRECT_BACKEND=1` is present in the operator environment. Without that gate, the default
composition root must not include a `codex-direct` adapter. This keeps the route explicit and fail-closed:
Reverso recognizes the experimental prefix, but no direct Codex network path exists unless an operator opts in.

Additional controls:

- `CodexOAuthAuth.bearer_token()` may expose the local OAuth access token only to the gated direct backend.
  `resolve()` remains secret-free and no token value is serialized into diagnostics.
- UX exposes `codex-direct/gpt-5.5` provider-scoped slugs only when `REVERSO_CODEX_DIRECT_BACKEND=1`; GPT built-in ids stay bare.
- The CLI-backed Codex path remains unchanged and remains the default supported production Codex path.
- Default-on direct backend, hosted use, non-loopback use, and CI live-token execution remain no-go.

Evidence packet:

- `.venv/bin/python -m pytest tests/unit/test_codex_oauth.py tests/unit/test_codex_direct_adapter.py tests/unit/test_codex_live_proof.py tests/unit/test_model_exposure.py tests/unit/test_codex_sync.py -q`
  - Result: 101 passed in 0.43s.
- `.venv/bin/python scripts/codex-live-proof.py --lane direct --json`
  - Result: skipped, with reason to set `REVERSO_CODEX_DIRECT_LIVE_PROOF=1` on a trusted local machine.
- `.venv/bin/python scripts/codex-live-proof.py --lane official --json`
  - Result: skipped, with reason to set `REVERSO_CODEX_OFFICIAL_LIVE_PROOF=1` or `REVERSO_CODEX_LIVE_PROOF=1` on a trusted local machine.

Go / no-go:

- Go: local-only, explicit operator opt-in direct backend mount for proof and controlled manual use.
- No-go: default profile selection, default gateway mounting, remote-hosted use, or production recommendation
  until live official-first and direct HTTP proof evidence is captured on a trusted local account and reviewed
  in a follow-up ADR update.

## Live-token proof evidence - 2026-07-04

Secret-free opt-in proof was run locally with the merged proof harness plus the
streaming-shape fix from this diagnosis pass.

Commands:

```bash
REVERSO_CODEX_DIRECT_LIVE_PROOF=1 .venv/bin/python scripts/codex-live-proof.py --lane direct --json
REVERSO_CODEX_OFFICIAL_LIVE_PROOF=1 .venv/bin/python scripts/codex-live-proof.py --lane official --json
```

Observed direct lane evidence:

- status: passed
- auth_authenticated: true
- auth_method: codex_oauth
- auth_source: credentials_file
- token_present: true
- model: gpt-5.5
- usage_present: true
- error_type: null

Observed official lane evidence:

- status: passed
- response_shape_keys included item, thread_id, type, usage
- usage_present: true
- error_type: null

Diagnosis notes:

- The initial direct lane skipped because the CLI wrapper used validate-only
  `ProviderAuth` instead of `CodexOAuthAuth`.
- After the auth seam was fixed, direct OAuth authenticated but the upstream
  returned HTTP 400 with `Input must be a list`.
- Minimized live probes then showed the direct endpoint requires `store: false`
  and `stream: true`.
- With Responses input-list normalization, `store: false`, and stream-backed
  `create_response`, the direct lane passed against the live backend.

Default-enable recommendation:

- This evidence is sufficient to justify a reviewed default-enable proposal for
  local loopback deployments, provided default-on still fails closed when Codex
  OAuth is missing or expired.
- Do not default-enable for non-loopback or hosted CI environments.
- Before changing the default, merge the streaming-shape fix, run hosted CI, and
  keep ADR evidence attached to the PR.
