---
type: adr
project: reverso
id: 0006
title: Inbound Anthropic Messages API Surface
status: Accepted
date: 2026-06-20
supersedes: none
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md
  - docs/architecture/adr/0004-deepseek-incremental-streaming.md
  - docs/architecture/adr/0005-bounded-cli-spine.md
  - .omc/specs/deep-interview-anthropic-api-surface.md
  - .omc/plans/ralplan-anthropic-api-surface.md
---

# ADR 0006: Inbound Anthropic Messages API Surface

## Status

Accepted, 2026-06-20. Docs-first deliverable for Milestone 1 of the inbound Anthropic
Messages API surface. The ralplan consensus gate is complete (Architect
SOUND-WITH-CHANGES resolved, Critic APPROVED). This ADR plus the companion sections in
`README.md`, `docs/03-architecture.md`, and `docs/04-mvp.md` define the milestone boundary.
Implementation proceeds via per-goal PRs after this ADR is reviewed.

## Context

Reverso already serves an OpenAI Responses surface (ADR 0002, ADR 0003): a first-party ASGI
gateway (`reverso.protocols.responses_app.ResponsesGatewayApp`) on `127.0.0.1:64946`, with one
adapter per backend (claude, copilot, auggie, deepseek) behind a FROZEN `ProviderAdapter`
Protocol, a canonical SSE replay seam (`protocols/replay.py`), and an in-memory
`ResponseStore`.

This milestone adds a second inbound surface: the Anthropic Messages API (`POST /v1/messages`,
`POST /v1/messages/count_tokens`, `GET /v1/models`). The primary client is Claude Code and the
Claude Agent SDK pointed at Reverso via `ANTHROPIC_BASE_URL`. The fidelity target is
Claude-Code-observed parity: the request and stream shapes Claude Code actually sends and
consumes, not the entire theoretical Messages surface.

The surface is inbound only. Reverso does NOT call `api.anthropic.com` upstream; it translates
inbound Anthropic Messages traffic onto the existing Responses backends. A client speaking the
Anthropic dialect therefore reaches copilot, deepseek, or auggie through Reverso, never
Anthropic's hosted models.

Hard repo constraints (`AGENTS.md`): bind `127.0.0.1:64946` only; no secrets in version
control or logs; `uv`-managed Python; frontmatter on every markdown file; no em or en dash
characters; never delete spec content (augment or deprecate in place).

## Decision

Add the Anthropic Messages surface as a translation layer over the frozen Responses contract,
served from the same loopback port, with a single data-driven model-to-backend authority and a
per-feature capability ceiling enforced as honest errors.

### D1. Pure-ASGI AnthropicMessagesApp plus a stateless anthropic_translate module

A new pure-ASGI `AnthropicMessagesApp` and a stateless `anthropic_translate` module are mounted
in the composition root (`reverso.proxy.compose`). The app translates Anthropic Messages
requests and responses to and from the FROZEN `ProviderAdapter` Responses contract
(`ResponsesRequest`, `ResponseEnvelope`, `SSEEvent`) and reuses `protocols/replay.py` for
streaming. The `ProviderAdapter` Protocol is NOT changed: the Anthropic surface is a new
front-of-gateway translation seam, not a sixth adapter method.

`anthropic_translate` is stateless: it maps an Anthropic Messages request into a
`ResponsesRequest`, and maps a `ResponseEnvelope` or an `SSEEvent` stream back into Anthropic
Messages response shapes. All conversation state continues to ride the existing in-memory
`ResponseStore` through the Responses contract; the translation layer holds none of its own.

### D2. surface_registry is the single first-party model-to-backend authority

A new `surface_registry` is the SINGLE first-party authority that maps a requested model to a
backend. The first-party stack routes by path prefix only and has no model map of its own; the
ONLY model map in the system, `config/litellm_config.yaml`, belongs to the quarantined legacy
LiteLLM app. `surface_registry` reads `litellm_config.yaml` via `yaml.safe_load` as DATA only;
it never imports the legacy app, preserving the ADR 0002 D2 quarantine.

Surface-scoped exposure is data, held in a `SURFACE_BACKENDS` table keyed by surface. The
Anthropic surface exposes a subset of backends, so a backend can be present on the Responses
surface and absent from the Anthropic surface without any code branch. For Milestone 1 the
Anthropic-surface backends are `copilot`, `deepseek`, and `auggie`. `claude` is EXCLUDED:
Claude Code talking to a claude backend through Reverso is circular (the claude backend is the
claude CLI itself), so the Anthropic surface must never route to it. Milestone 2 adds
`codex-cli` as a single one-row addition to `SURFACE_BACKENDS`, no conditionals.

> **Superseded by ADR 0009.** The `claude` exclusion stated here is reversed: `claude` is now
> SERVED on the Anthropic surface via the local `claude` CLI under subscription OAuth. The
> circularity concern is mitigated because Reverso runs as a server whose process env carries no
> `ANTHROPIC_BASE_URL`, and the claude adapter additionally scrubs
> `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` from the spawned CLI's child
> env, so the CLI always reaches `api.anthropic.com`, never Reverso. See ADR 0009.

### D3. Routing, version, and error decisions

- Default routing is automatic model-to-backend resolution through `surface_registry`. A client
  that sets a backend model id reaches that backend; the registry is the single resolver.
- Optional per-profile path-prefix endpoints exist for explicit pinning: `/deepseek/v1/messages`,
  `/copilot/v1/messages`, `/auggie/v1/messages`. These mirror the Responses-surface prefixes and
  bypass model-to-backend auto-resolution for that request.
- An unknown model returns HTTP 404 with a `not_found_error`. (Originally a `claude` model
  also 404'd here as a hard negative case for the D2 circularity reason; reversed by ADR 0009 -
  claude now resolves to the claude backend and is served 200.)
- A missing `anthropic-version` header defaults to `"2023-06-01"` and is echoed back on the
  response. A missing version header is never a 400.
- The error envelope is the Anthropic shape:
  `{"type": "error", "error": {"type": "<error_type>", "message": "<message>"}}`.

### D4. The anthropic SDK is a docs and contract reference only

The `anthropic` Python SDK is consulted as a documentation and contract reference for request
and response shapes, error types, and SSE event grammar. It is NOT a runtime dependency of
Reverso. The translation layer hand-builds Anthropic Messages shapes; it does not import or call
the SDK at runtime.

## Decision Drivers

1. The frozen `ProviderAdapter` Protocol (ADR 0002 11.3) must stay frozen; a second inbound
   dialect cannot be allowed to scatter native methods across four adapters.
2. There is exactly one model map in the system (the quarantined `litellm_config.yaml`); a
   second authority would create a split-brain routing surface.
3. Claude-Code-observed parity bounds scope: implement and translate what Claude Code actually
   sends and consumes, gate the rest honestly.
4. The Anthropic surface must reuse the existing Responses backends and replay seam, not a new
   upstream client, to keep the gateway self-contained and loopback-only.
5. Symmetry with Milestone 2 (`codex-cli` is Anthropic-surface-only) requires surface exposure to
   be data, not code branches.

## Alternatives considered

- **Native Anthropic methods on the `ProviderAdapter` Protocol.** Rejected: it breaks the frozen
  contract (ADR 0002 11.3) and scatters Anthropic-versus-Responses fidelity loss across all four
  adapters, so each adapter would re-derive the same translation independently.
- **SSE byte-rewriting middleware that edits Responses SSE bytes into Anthropic SSE bytes.**
  Rejected: byte-level rewriting of a streaming event grammar is a fidelity risk; event
  boundaries, partial frames, and the canonical replay sequence are far safer mapped through the
  typed `SSEEvent` seam than patched on the wire.
- **Per-surface conditionals or a hardcoded per-surface registry.** Rejected: branching backend
  exposure per surface in code is the scattered-conditional anti-pattern; it makes the
  Milestone 2 `codex-cli` addition a code change in many places instead of one data row.

## Capability ceiling

The Anthropic Messages surface does not promise full Messages fidelity on every backend. Each
(feature x backend) cell falls into one of six buckets:

- **native**: the backend serves the feature directly through its Responses adapter.
- **translated**: the feature is mapped onto an equivalent Responses construct without loss that
  Claude Code observes. A `(approx)` qualifier marks a translation that is a documented
  approximation rather than exact (count_tokens).
- **partial-accepted**: a subset of the feature is accepted and translated while the rest is
  gated-error; the accepted and rejected subsets are named in the matrix footnote.
- **gated-error**: the feature is rejected with a structured Anthropic `invalid_request_error`
  naming the feature and backend; no silent acceptance.
- **accepted-ignored-stub**: the field is accepted to keep the request well-formed but has no
  effect; documented as a stub. No Milestone 1 feature falls in this bucket; it is retained for
  future fields.
- **structurally-impossible-M1**: the Responses contract cannot carry the feature in Milestone 1,
  so the surface raises a hard `invalid_request_error` to the client rather than pretend.

Two features are `structurally-impossible-M1` and BOTH surface to the client as a hard
`invalid_request_error`:

- **Streamed thinking deltas.** The Responses replay seam discards reasoning deltas, so the
  Anthropic `thinking` delta stream cannot be reconstructed in Milestone 1.
- **Honored `cache_control`.** Nothing in the Responses contract honors prompt caching, so a
  request that asks for honored `cache_control` cannot be served truthfully.

### Capability-boundary matrix (Milestone 1)

| Feature | copilot | deepseek | auggie |
|---|---|---|---|
| text in / text out | native | native | native |
| streaming (Anthropic SSE) | translated | translated | translated |
| image input | native | gated-error | gated-error |
| tool_use output | native | translated | gated-error (text-only ceiling) |
| tool_choice | translated | translated | partial-accepted [1] |
| streamed thinking deltas | structurally-impossible-M1 | structurally-impossible-M1 | structurally-impossible-M1 |
| honored cache_control | structurally-impossible-M1 | structurally-impossible-M1 | structurally-impossible-M1 |
| count_tokens | translated (approx) | translated (approx) | translated (approx) |

[1] auggie `tool_choice.auto` is partial-accepted; `tool_choice` of `required`, a named tool, or
`none` is gated-error. auggie has a text-only output ceiling, so `tool_use` output is a
gated-error on auggie while it is native on copilot and translated on deepseek.

## Recorded decisions

- **cache_control**: hard `invalid_request_error` on all Milestone 1 backends. No silent stub: a
  request asking for honored caching is rejected, not quietly accepted with no effect.
- **count_tokens**: a documented word-count approximation, NOT a real tokenizer. The
  `/v1/messages/count_tokens` response is an estimate and is labeled as such in the docs; it is
  not represented as exact provider tokenization.
- **unknown model**: HTTP 404 `not_found_error` (D3). (A `claude` model originally also 404'd
  here for the D2 circularity reason; reversed by ADR 0009 - claude is now served 200 via the
  local claude CLI.)

## Consequences

- A second inbound dialect (Anthropic Messages) coexists with the OpenAI Responses surface on the
  same loopback port, both translating onto the same frozen backends.
- `surface_registry` becomes the single first-party model-to-backend authority and the single
  reader of `litellm_config.yaml` as data; the first-party stack still routes by path prefix and
  gains no model map of its own.
- The capability ceiling is explicit and tested: `structurally-impossible-M1` features fail
  honestly instead of degrading silently.
- The `anthropic` SDK is a contract reference, so dependency surface and supply-chain exposure do
  not grow.
- Backend exposure per surface is data; the claude exclusion and the Milestone 2 addition are
  table edits, not code branches.

## Follow-ups

- **Milestone 2**: add `codex-cli` (gpt models) as an Anthropic-surface-only backend, a single
  `SURFACE_BACKENDS` row, mirroring how the Claude Code CLI is Responses-surface-only. No new
  surface, no Protocol change.
- Revisit `structurally-impossible-M1` features (streamed thinking deltas, honored
  `cache_control`) if and when the Responses contract gains reasoning-delta and prompt-cache
  carriers.
- Replace the count_tokens word-count approximation with a real tokenizer if a loopback-safe one
  becomes available.
