---
type: verification
project: reverso
id: anthropic-surface
related:
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
  - .omc/plans/ralplan-anthropic-api-surface.md
  - .omc/plans/ralplan-codex-anthropic-oauth.md
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

- `copilot`, `deepseek`, `auggie`, `codex`.
- `codex` was added in Milestone 2 (ADR 0007). It serves the five gpt-* models
  (gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1) first-party
  through the local `codex exec` CLI under the ChatGPT/Codex OAuth subscription.
  See the dedicated section below.
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

## Codex backend (Milestone 2, gpt-* via ChatGPT OAuth)

The `codex` backend (ADR 0007:
`docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md`) serves
the five gpt-* models on the Anthropic surface ONLY. It is the symmetric mirror of
the claude exclusion: claude is Responses-surface-only because Claude Code consumes
it, so codex is Anthropic-surface-only because Codex consumes it. gpt on the
Responses surface would be circular and is removed by Milestone 2, not relocated.

- Models: gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1. They
  auto-resolve to the codex backend on the BARE `/v1/messages` path through the
  static `_CODEX_MODELS` seed in `surface_registry`; codex has no per-profile
  prefix (unlike copilot/deepseek/auggie). They are listed by `GET /v1/models`.
- Backend: each gpt-* turn invokes the locally installed `codex exec ... --json`
  CLI through the bounded CLI spine (ADR 0005). The Codex Responses-shaped output
  is mapped onto the internal Responses contract, and the Milestone 1 Anthropic
  translation, streaming, and capability-gating layers convert it to and from
  Anthropic Messages. There is no direct OpenAI Platform call and no
  `openai-python` runtime dependency.
- Authentication: the ChatGPT/Codex OAuth SUBSCRIPTION written by `codex login`,
  never an OpenAI API key. `CodexOAuthAuth` is a VALIDATE-ONLY (design point A3)
  pre-flight gate: it reads and validates the `~/.codex/auth.json` artifact (access
  token present, JWT `exp` not passed) and fails closed with a structured Anthropic
  error when the session is missing or expired. The G002.0 spike confirmed
  `codex exec` exposes NO token-injection env var, so no bearer is injected into the
  child; the CLI authenticates the turn from its own login session. Token material
  is never logged (it routes through the existing redaction).
- Capability ceiling: TEXT-ONLY (the mirror of auggie). `codex exec` emits
  `command_execution` OBSERVATIONS, not Responses function-call output, so a
  `tools` request is accepted but no structured `tool_use` OUTPUT block is ever
  emitted. Image input, streamed thinking deltas, and honored `cache_control` are
  unsupported and gated per feature policy, exactly as on the other backends.

Operational requirement: the manual codex smoke step requires `codex` to be LOGGED
IN (`codex login` completed) and `CODEX_HOME` to point at a WORKING codex home. The
local `~/.codex` directory may carry an oh-my-codex overlay that breaks
`codex exec`; pointing `CODEX_HOME` at a clean codex home avoids this. The automated
suite never spawns `codex exec` (the parity harness substitutes a text-only fixture
under the codex backend key), so this requirement applies only to the live smoke.

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

The script also carries a COMMENTED gpt-5.5 codex step (non-streaming and
streaming). It is commented out because it requires `codex` to be logged in and a
working `CODEX_HOME`; uncomment it to run the live codex path. See the operational
requirement in the Codex backend section above.

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
  copilot, deepseek, auggie, AND codex (codex reuses auggie's text-only fixture
  under the codex backend key, since its real adapter spawns `codex exec` and
  cannot run in CI; routing and gating still key on the resolved codex backend).
- `test_anthropic_claude_exclusion` - the negative claude-exclusion 404 path.
- `test_litellm_quarantine` - the import-graph guard asserting the surface never
  imports the legacy LiteLLM app or any `litellm` module.

Codex backend coverage (Milestone 2, ADR 0007):

- `test_codex_oauth` - the `CodexOAuthAuth` validate-only gate: fail-closed on
  missing artifact, no access token, and expired JWT `exp`; the keychain-then-file
  source layering; the falsifiable assertion that the method is the OAuth path and
  no API-key env var is consumed; and that no token material leaks into logs.
- `test_codex_adapter` - the `CodexAdapter`: non-streaming and streaming turns over
  the bounded spine, the empty-turn vs failure pre-first-chunk branches, the
  no-divergence coupling test (a valid gate with a failing `codex exec` surfaces a
  structured error, never a false-green), the text-only ceiling, and `list_models`.
- `test_codex_responses_exclusion` - the negative suite asserting gpt-* and the
  codex backend are NEVER reachable on the Responses surface (the exact mirror of
  `test_anthropic_claude_exclusion`).
- the codex rows in `test_anthropic_messages_parity` - codex added to `PROVIDERS`
  and to the text-only tool-ceiling group (NOT to the tool-output providers), with
  a gpt-* model id exercising bare-path auto-resolution to the codex backend.
- `test_codex_anthropic_surface` - gpt-* model routing to the codex backend, the
  text-only tool ceiling, the image / thinking / cache_control unsupported gates,
  and the codex streaming event order on the Anthropic surface.
- the codex rows in `test_surface_registry` - `resolve_anthropic_backend` returns
  `codex` for the five gpt ids, the listing includes them sourced from the static
  `_CODEX_MODELS` seed, and the build-time `cross_check_anthropic_models` lint
  exempts the seeded ids from the config-existence check while still enforcing
  backend membership (so codex routing drift is caught at import).
