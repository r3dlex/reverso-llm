---
type: verification
project: reverso
id: anthropic-surface
related:
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - .omc/plans/ralplan-anthropic-api-surface.md
  - scripts/smoke_anthropic_surface.sh
---

# Anthropic Messages Surface Verification

This is the verification summary for the inbound Anthropic Messages API surface
(ADR 0006, Milestone 1). It records what the surface provides, where the
authoritative capability matrix lives, how to run the manual smoke test, and the
automated test inventory that covers the surface.

## What the surface provides

Reverso serves a second inbound dialect, the Anthropic Messages API, on the same
loopback port as the OpenAI Responses surface (127.0.0.1:64946). The surface is
INBOUND ONLY: Reverso never calls api.anthropic.com upstream. It translates
inbound Anthropic Messages traffic onto the existing Responses backends through a
front-of-gateway translation seam, so the frozen `ProviderAdapter` Protocol is
unchanged.

Endpoints:

- `POST /v1/messages` (non-streaming and `stream: true`), plus the optional
  per-profile prefixes `/copilot/v1/messages`, `/deepseek/v1/messages`,
  `/auggie/v1/messages` that pin a backend and bypass model auto-resolution.
- `POST /v1/messages/count_tokens` (a pre-flight sizing call).
- `GET /v1/models` (the bare listing of the Anthropic-surface model set).

Backends exposed on this surface (data-driven via `SURFACE_BACKENDS`):

- `copilot`, `deepseek`, `auggie`.
- `claude` is EXCLUDED. Claude Code talking to a claude backend through Reverso is
  circular (the claude backend is the claude CLI itself), so a `/claude/v1/...`
  request is claimed by this surface and returns a 404 `not_found_error` rather
  than reaching any backend. An unknown non-claude model is also a 404
  `not_found_error`.

Routing, version, and error rules:

- Default routing auto-resolves the requested model to a backend through the
  single `surface_registry` authority, which reads `config/litellm_config.yaml`
  as data and is the only first-party model map.
- A missing `anthropic-version` header defaults to `2023-06-01` and is echoed on
  the response; it is never a 400.
- Errors use the Anthropic envelope
  `{"type": "error", "error": {"type": <error_type>, "message": <message>}}`.

## Capability matrix

The per-(feature x backend) capability ceiling is authoritative in ADR 0006, in
the "Capability-boundary matrix (Milestone 1)" section:
`docs/architecture/adr/0006-anthropic-messages-api-surface.md`. In summary, text
in/out and Anthropic SSE streaming are supported on all three backends; image
input and `tool_use` output are gated per backend; streamed thinking deltas and
honored `cache_control` are `structurally-impossible-M1` and raise a hard
`invalid_request_error`; `count_tokens` is a documented word-count approximation,
not a real tokenizer.

## How to run the smoke test

The manual loopback smoke test is `scripts/smoke_anthropic_surface.sh`. With a
reverso proxy running on 127.0.0.1:64946:

```sh
scripts/smoke_anthropic_surface.sh
```

It exercises, against 127.0.0.1 only and with no secrets: a non-streaming
`POST /v1/messages`, a streaming `POST /v1/messages` (showing the Anthropic SSE
events), `POST /v1/messages/count_tokens`, `GET /v1/models`, and the negative
`POST /claude/v1/messages` that must return a 404 `not_found_error`. Each step
echoes what it checks. Set `REVERSO_PORT` to target a non-default port.

## Test inventory

The surface is covered by the following automated tests (under `tests/unit` and
`tests/integration`):

- `test_anthropic_routing` - path split, profile prefixes, auto-resolution, the
  bare `/v1/models` claim, and the claimed claude prefix.
- `test_surface_registry` - model-to-backend resolution, the `SURFACE_BACKENDS`
  data, claude fail-closed, and the listing.
- `test_anthropic_translate` - Messages to/from the frozen Responses contract
  (system, messages, tools, tool_choice, usage, stop_reason).
- `test_anthropic_stream` - the pure Responses-SSE to Anthropic-SSE mapper,
  including peek-first, empty/truncated upstream, mid-stream failure, and the
  copilot superset tolerance.
- `test_anthropic_feature_gate` and `test_anthropic_feature_gating` - per-backend
  capability gating and the gated/structurally-impossible buckets.
- `test_anthropic_messages_nonstreaming` - the end-to-end non-streaming handler.
- `test_anthropic_messages_streaming` - the end-to-end streaming handler over the
  ASGI app.
- `test_anthropic_aux` - `count_tokens` approximation and the `/v1/models`
  listing shape.
- `test_anthropic_messages_parity` - the Claude-Code-observed parity harness over
  copilot, deepseek, and auggie.
- `test_anthropic_claude_exclusion` - the negative claude-exclusion 404 path.
- `test_litellm_quarantine` - the import-graph guard asserting the surface never
  imports the legacy LiteLLM app or any `litellm` module.
