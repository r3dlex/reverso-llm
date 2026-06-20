---
type: ralplan
project: reverso
slug: anthropic-api-surface
status: pending approval
source_spec: .omc/specs/deep-interview-anthropic-api-surface.md
consensus_gate:
  complete: true
  architect_review: SOUND-WITH-CHANGES (resolved)
  critic_review: APPROVED
---

# RALPLAN: Inbound Anthropic Messages API Surface

Note: this is the design record. Implementation proceeds via per-goal PRs after approval. It is
kept consistent with `docs/architecture/adr/0006-anthropic-messages-api-surface.md`.

## Requirements Summary

Source: `.omc/specs/deep-interview-anthropic-api-surface.md` (deep interview PASSED at 0.18
ambiguity, 14 acceptance criteria).

Add an inbound Anthropic Messages API surface (`POST /v1/messages`, `/v1/messages/count_tokens`,
`GET /v1/models`) that coexists with the OpenAI Responses surface on `127.0.0.1:64946`. Primary
client is Claude Code and the Claude Agent SDK via `ANTHROPIC_BASE_URL`; fidelity target is
Claude-Code-observed parity. Inbound only (no `api.anthropic.com` upstream). Non-claude backends
are model-routed by default through a single authority, with optional per-profile prefixes. The
Messages feature surface is gated per backend by a feature policy. Milestone 1 backends are
copilot, deepseek, and auggie; claude is excluded; codex-cli is Milestone 2.

The 14 acceptance criteria (AC1 docs-first through AC14 unknown-model 404) are restated in the
source spec and mapped to steps and tests below.

## RALPLAN-DR

### Principles

1. **Frozen seams**: the `ProviderAdapter` Protocol (ADR 0002 11.3) is never modified; the
   Anthropic surface is a front-of-gateway translation seam, not a sixth adapter method.
2. **Single authority**: exactly one first-party model-to-backend resolver (`surface_registry`);
   the only model map remains the quarantined `litellm_config.yaml`, read as data.
3. **Honest surface**: capability gaps fail with structured Anthropic errors; no silent stubs for
   semantically meaningful features.
4. **Data over branches**: surface exposure is a `SURFACE_BACKENDS` table; claude exclusion and
   the Milestone 2 codex-cli addition are data rows, not conditionals.
5. **Local-only and secret-safe**: bind `127.0.0.1:64946` only; no secrets in version control or
   logs; the `anthropic` SDK is a contract reference, not a runtime dependency.

### Decision Drivers (top 3)

1. The frozen Protocol forbids scattering native Anthropic methods across four adapters, so the
   translation must live in front of the gateway.
2. There is one model map (the quarantined `litellm_config.yaml`); a second authority would split
   routing brain in two.
3. Claude-Code-observed parity bounds scope, and per-surface exposure must be data so Milestone 2
   is a one-row add.

### Viable Options

**Option A1: Translation app over the frozen Responses contract (chosen).**
A pure-ASGI `AnthropicMessagesApp` plus a stateless `anthropic_translate` module map Messages to
and from `ResponsesRequest`/`ResponseEnvelope`/`SSEEvent` and reuse `protocols/replay.py`.
Pros: keeps the Protocol frozen; one translation point; reuses backends and replay; testable per
cell.
Cons: a second dialect to maintain alongside Responses translation.

**Option A2 (rejected): native Anthropic methods on the `ProviderAdapter` Protocol.**
Rejected: breaks the frozen contract and scatters fidelity loss across all four adapters.

**Option A3 (rejected): SSE byte-rewriting middleware.**
Rejected: byte-level rewriting of a streaming event grammar is a fidelity risk; map through the
typed `SSEEvent` seam instead.

**Option B1: Data-driven single registry (chosen).**
`surface_registry` is the single model-to-backend authority and the single reader of
`litellm_config.yaml` as data; surface exposure is a `SURFACE_BACKENDS` table.
Pros: single brain; data-driven exposure; Milestone 2 is one row.
Cons: registry must stay the only resolver, enforced by review and tests.

**Option B2 (rejected): per-surface conditionals or a hardcoded per-surface registry.**
Rejected: the scattered-conditional anti-pattern; Milestone 2 would touch many call sites.

## Implementation Steps

Grouped by component, ordered so each step is independently reviewable. Steps map to ADR 0006
decisions D1 through D4.

- **Step 1: Docs and ADR (AC1).** ADR 0006 plus companion sections in README,
  `docs/03-architecture.md`, and `docs/04-mvp.md`; frontmatter and dash-clean. Docs-first gate.
- **Step 2: surface_registry, routing, and app skeleton (AC9, D2).** New `surface_registry` that
  reads `litellm_config.yaml` via `yaml.safe_load` as data, holds the `SURFACE_BACKENDS` table
  (Anthropic surface = copilot, deepseek, auggie; claude excluded), and resolves a model to a
  backend. Pure-ASGI `AnthropicMessagesApp` skeleton mounted in `reverso.proxy.compose`; no legacy
  app import.
- **Step 3: non-streaming translation (AC2, AC6, D1).** `anthropic_translate` maps a Messages
  request to a `ResponsesRequest`, calls the resolved adapter's `create_response`, and maps the
  `ResponseEnvelope` back to a non-streaming Messages response.
- **Step 4: routing, error, and version handling (AC11, AC13, AC14, D3).** Default auto-routing
  plus per-profile prefixes; unknown OR claude model -> HTTP 404 `not_found_error`; missing
  `anthropic-version` -> default `"2023-06-01"` and echo; Anthropic error envelope
  `{"type":"error","error":{"type":...,"message":...}}`.
- **Step 5: SSE streaming mapper (AC3, D1).** Map the canonical Responses `SSEEvent` replay into
  the Anthropic SSE grammar (message_start, content_block_start/delta/stop, message_delta,
  message_stop). Tolerate the copilot superset (copilot may emit a richer Responses event set than
  deepseek/auggie; the mapper must accept the superset and still emit a valid Anthropic sequence).
  Emit the Anthropic one-ping cadence (`ping` event) consistent with what Claude Code consumes.
- **Step 6: capability gating (AC4, AC5, D3 capability ceiling).** `feature_policy` enforces the
  per-(feature x backend) matrix: image native on copilot, gated-error on deepseek and auggie;
  tool_use output native/translated on copilot+deepseek, text-only gated-error on auggie
  (tool_choice.auto partial-accepted, required/named/none gated-error); streamed thinking deltas
  and honored cache_control are structurally-impossible-M1 and both raise a hard
  `invalid_request_error`.
- **Step 7: count_tokens (AC7).** `POST /v1/messages/count_tokens` returns a documented word-count
  approximation labeled as an estimate, not a real tokenizer.
- **Step 8: /v1/models and compose mounting (AC8).** `GET /v1/models` lists exactly the Anthropic
  surface backends' models (claude excluded); finalize the compose mounting so the Anthropic and
  Responses surfaces share the one port without Responses regression.
- **Step 9: parity harness (AC12).** A parity suite runs the Claude-Code-observed subset over
  copilot, deepseek, and auggie, recording each cell pass/fail/gated, plus the negative
  claude-exclusion check.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| A second model authority creeps in beside surface_registry | Single-authority principle; a test asserts the first-party stack has no model map other than the registry, which reads litellm_config as data |
| SSE grammar drift from what Claude Code consumes | Map through the typed SSEEvent seam; copilot-superset tolerance tested; one-ping cadence fixture-pinned |
| Silent degradation of gated features | feature_policy returns structured invalid_request_error; structurally-impossible-M1 features raise hard, never stub |
| count_tokens mistaken for exact tokenization | Documented as a word-count approximation and labeled an estimate in responses and docs |
| Anthropic SDK pulled in as a runtime dep | SDK is contract reference only; not added to pyproject runtime deps |
| Responses surface regression from shared port | compose mounting test asserts existing Responses routes unchanged |
| claude reachable on the Anthropic surface | SURFACE_BACKENDS excludes claude; negative test asserts 404 not_found_error |

## Test Plan

- **T2 (AC2)**: non-streaming `/v1/messages` text turn per backend.
- **T3 (AC3)**: streaming Anthropic SSE sequence well-formed per backend.
- **T4 (AC4)**: tool round-trip on copilot and deepseek; auggie text-only gated.
- **T5 (AC5)**: image, streamed thinking, honored cache_control gated per matrix.
- **T6 (AC6)**: translation layer round-trips Messages to/from the frozen Responses contract.
- **T7 (AC7)**: count_tokens returns a labeled word-count approximation.
- **T8 (AC8)**: `/v1/models` lists Anthropic-surface backends, excludes claude.
- **T9 (AC9)**: default auto-routing resolves a model through the single authority.
- **T10 (AC10)**: per-profile prefixes pin the backend and bypass auto-resolution.
- **T11 (AC11)**: missing anthropic-version defaults to "2023-06-01" and echoes; never 400.
- **T12 (AC12)**: parity harness records cells over copilot/deepseek/auggie.
- **T13 (AC13)**: negative claude-exclusion -> 404 not_found_error.
- **T14 (AC14)**: unknown non-claude model -> 404 not_found_error with the Anthropic envelope.
- **Unit**: surface_registry resolution and SURFACE_BACKENDS data; feature_policy bucket
  classification; error-envelope builder; version default-and-echo.
- **T-superset (PURE unit test)**: the SSE mapper accepts a copilot superset event stream and
  emits a valid Anthropic sequence; no network, no subprocess, fixed event fixtures.

## ADR

- Decision: Option A1 (translation app over the frozen Responses contract) plus Option B1
  (data-driven single registry). The authoritative record is
  `docs/architecture/adr/0006-anthropic-messages-api-surface.md`.
- Drivers: (1) keep the ProviderAdapter Protocol frozen; (2) one model authority, not two; (3)
  Claude-Code-observed parity bounds scope and per-surface exposure is data.
- Alternatives considered: native Anthropic methods on the Protocol (rejected, breaks the frozen
  contract and scatters fidelity loss); SSE byte-rewriting middleware (rejected, fidelity risk);
  per-surface conditionals or hardcoded per-surface registry (rejected, scattered-conditional
  anti-pattern).
- Consequences: a second inbound dialect coexists on the one port; surface_registry is the single
  resolver and the single reader of litellm_config as data; the capability ceiling fails honestly;
  the anthropic SDK stays a contract reference.

## Milestone 2 hooks

- Add codex-cli (gpt models) as an Anthropic-surface-only backend: one `SURFACE_BACKENDS` row,
  mirroring how the Claude Code CLI is Responses-surface-only. No new surface, no Protocol change,
  no per-surface conditionals.
- Revisit structurally-impossible-M1 features (streamed thinking deltas, honored cache_control) if
  the Responses contract later gains reasoning-delta and prompt-cache carriers.
