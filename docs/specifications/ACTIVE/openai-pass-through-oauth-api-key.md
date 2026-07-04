# OpenAI pass-through OAuth/API-key provider

Date: 2026-07-04
Status: Northstar ready for Autobahn
Tracker: GitHub issue #47, https://github.com/r3dlex/reverso-llm/issues/47
Slug: openai-pass-through-oauth-api-key

## Intent

Implement a local-loopback, opt-in OpenAI pass-through provider for Reverso so a
Codex/OpenAI-compatible client can call Reverso at `/openai/v1/...` while Reverso
forwards to the official OpenAI API using OAuth when available and API-key auth as
an explicit fallback.

Official API target shape verified for this plan:

- OpenAI create response: upstream `POST /responses`; Reverso route
  `/openai/v1/responses`.
- OpenAI list models: upstream `GET /models`; Reverso route
  `/openai/v1/models`.

## Scope

1. Add an OpenAI pass-through provider surface behind a local-loopback opt-in
   gate.
2. Implement `POST /openai/v1/responses` for non-streaming and streaming
   Responses API payloads with secret-safe diagnostics.
3. Implement `GET /openai/v1/models` and expose provider-scoped model catalog
   entries without overriding built-in GPT/Codex selector defaults.
4. Add auth resolution with OAuth first when configured/valid, API key fallback
   when explicitly configured, and fail-closed diagnostics for missing/expired
   credentials.
5. Add Codex profile/model generation support for the OpenAI pass-through surface.
6. Keep `codex-direct` unchanged: local-loopback default-on only, kill-switch
   preserved, non-loopback/hosted default-on remains no-go per ADR 0016.
7. After the implementation PR, perform repo hygiene: archive or mark completed
   codex-direct ACTIVE specs, update traceability for PRs #70-#72, and add
   operator docs stating that codex-direct is done and OpenAI pass-through is the
   next provider track.

## Non-goals

- No non-loopback or hosted default-on exposure.
- No change to `codex-direct` behavior, ADR 0016 boundaries, or kill-switches.
- No replacement of built-in Codex/OpenAI GPT selector defaults such as
  `gpt-5.5`.
- No long-lived secret logging, fixture capture, or CI live-token requirement.
- No broad SDK dependency adoption unless an Autobahn implementation slice proves
  direct HTTP is insufficient.

## Constraints and invariants

- Local-loopback gate: the provider may mount only when Reverso is bound to a
  loopback host and an explicit opt-in flag is enabled.
- Auth is fail-closed. Missing, expired, or ambiguous credentials return a
  diagnostic response without leaking token material.
- API-key fallback must be explicit and documented; OAuth remains preferred when
  available.
- Generated profile/catalog state must be additive. OpenAI pass-through models
  must not supersede built-in bare GPT model ids.
- Provider-list integration must not accidentally re-enable legacy `/codex`.
- Streaming tests must verify event shape and close behavior without relying on
  real OpenAI network calls.

## Sliced goals for Autobahn

### Slice 1: OpenAI pass-through skeleton and gates

- Add the provider route/mount shape for `/openai/v1/models` and
  `/openai/v1/responses` behind an explicit local-loopback opt-in flag.
- Add tests proving the provider is absent by default when not opted in, absent
  or fail-closed on non-loopback binds, and does not alter `codex-direct`.
- Candidate files: `src/reverso/proxy/compose.py`,
  `src/reverso/protocols/responses_app.py`, provider registry tests.

### Slice 2: Auth resolution and diagnostics

- Add OpenAI pass-through auth resolution: OAuth bearer where available, explicit
  API-key fallback, and secret-redacted diagnostics.
- Add tests for precedence, missing credentials, expired OAuth, API-key fallback,
  and log/response redaction.
- Candidate files: `src/reverso/protocols/auth.py`, new OpenAI adapter/auth
  module, `tests/unit/test_codex_oauth.py` or a new OpenAI auth test file.

### Slice 3: Responses and models pass-through

- Implement direct HTTP upstream calls for `POST /responses` and `GET /models`.
- Cover non-streaming, streaming SSE, upstream 401/403/429/5xx, invalid JSON,
  timeout, and cancellation behavior.
- Candidate files: new `src/reverso/protocols/adapters/openai.py`, existing
  `ProviderAdapter` contracts, adapter tests, integration-style route tests.

### Slice 4: Codex profile/model UX

- Generate/update `openai-pass-through.config.toml` and
  `~/.codex/reverso/openai-pass-through.json` for the opt-in pass-through
  provider without overriding top-level bare GPT defaults. The operator HTTP
  route remains `/openai/v1/...`; the profile name is separate to preserve the
  built-in `openai.config.toml` GPT profile.
- Add model exposure tests for provider-scoped catalog entries and built-in GPT
  preservation.
- Candidate files: `src/reverso/protocols/model_exposure.py`, Codex sync logic,
  `tests/unit/test_codex_sync.py`, `tests/unit/test_model_exposure.py`.

### Slice 5: Operator docs, traceability, and codex-direct hygiene

- Document OpenAI pass-through as the next provider track and codex-direct as
  completed local-loopback default-on work.
- Archive completed codex-direct ACTIVE specs or mark them complete according to
  repo convention.
- Update traceability for PRs #70, #71, and #72, and link issue #47 to this
  Northstar handoff.
- Candidate files: `README.md`, `docs/architecture/codex-responses-parity-matrix.md`,
  `docs/specifications/ACTIVE/`, `.ai/traceability/graph.json`.

## Acceptance criteria

- `POST /openai/v1/responses` and `GET /openai/v1/models` are available only in
  explicitly enabled local-loopback mode.
- Non-streaming and streaming request paths are covered by unit or integration
  tests using fake upstreams.
- OAuth/API-key auth precedence and redaction are test-covered.
- Codex sync/profile generation is test-covered and preserves built-in GPT ids.
- `codex-direct` tests still pass and ADR 0016 boundaries remain documented.
- Full gate passes: `uvx prek run --all-files`, pytest, `scripts/validate-rules.sh`,
  `scripts/archgate.sh .rules.ts`, and `git diff --check`.

## Stop condition

Stop after one or more Autobahn PRs land the five slices with green local and
hosted CI, with issue #47 updated and codex-direct hygiene recorded.

`~/.codex/reverso/openai-pass-through.json` opt-in pass-through
profile uses `/openai-pass-through/v1/...` as its canonical Codex-sync route;
operator HTTP uses `/openai/v1/...`. The split avoids collisions with built-in
`openai.config.toml` GPT provider defaults.
