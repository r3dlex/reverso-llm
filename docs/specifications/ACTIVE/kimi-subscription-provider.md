---
title: Kimi subscription provider
status: active
slug: kimi-subscription-provider
date: 2026-07-19
related:
  - docs/architecture/adr/0017-kimi-code-oauth-provider.md
  - .ai/handoff/kimi-provider-implementation-handoff.md
---

# Kimi subscription provider

| | |
|---|---|
| **Slug** | `kimi-subscription-provider` |
| **Repo** | `r3dlex/reverso-llm` |
| **Status** | active |
| **Raised** | 2026-07-19 via `$diagnose` |
| **Spec owner** | unassigned |
| **Spans** | Kimi auth and adapter, Responses routing, Anthropic routing, capability data, Headroom verification, provider tests, and ADR 0017 |

## Outcome

Expose a user's Kimi Code subscription through Reverso as a first-party
provider on both supported inbound protocol surfaces:

- OpenAI Responses under `/kimi/v1`.
- Anthropic Messages under `/kimi/v1/messages` and through provider-qualified
  model routing such as `kimi/kimi-k2.5` on `/v1/messages`.

The implementation must use Kimi Code OAuth as the primary credential path,
send the resulting access token as an HTTP bearer token, permit an explicit
bearer-token fallback, and preserve Reverso's provider-agnostic Headroom
compression behavior.

## Current state and problem

Kimi Code CLI already supports browser or device OAuth login and persists its
access and refresh tokens in `~/.kimi-code/credentials/kimi-code.json` by
default. `KIMI_CODE_HOME` overrides the `~/.kimi-code` home. The Kimi
subscription endpoint is OpenAI-compatible at
`https://api.kimi.com/coding/v1`, but Reverso previously had no `kimi` prefix,
adapter, Anthropic backend registration, model routing, or capability column.

The official Python `kimi-sdk` does not close that gap. It provides an agent and
chat-completions abstraction, not an OpenAI Responses or Anthropic Messages
server contract. It also requires Python 3.12 while Reverso supports Python
3.11. Reverso still needs its own translation, storage, replay, routing, error,
and compression seams, so adding the SDK would increase dependency surface
without removing implementation work.

## Scope

### In scope

1. Resolve a usable Kimi bearer token from the Kimi CLI OAuth artifact.
2. Refresh an expiring OAuth token through Kimi's token endpoint and persist the
   rotated artifact atomically with user-only file permissions.
3. Fall back to `KIMI_BEARER_TOKEN` when the OAuth artifact is unavailable.
4. Translate Reverso `ResponsesRequest` objects to Kimi's OpenAI-compatible
   `/chat/completions` request shape for unary and streaming calls.
5. Translate Kimi responses and SSE deltas back to the frozen
   `ProviderAdapter` Responses contract.
6. Expose authenticated live model discovery through `/kimi/v1/models`, with a
   bounded offline fallback model.
7. Register Kimi on the Responses and Anthropic surfaces.
8. Route provider-qualified Anthropic model ids without maintaining a stale
   hard-coded Kimi catalog.
9. Add Kimi to the capability matrix using the existing translated
   OpenAI-compatible ceiling until richer behavior is explicitly proven.
10. Prove that Headroom compression still runs before Kimi adapter dispatch on
    both surfaces.

### Out of scope

- Interactive OAuth login inside Reverso. Users authenticate with `kimi /login`.
- Kimi account creation, subscription purchase, or browser automation.
- Importing or vendoring Kimi CLI internals.
- Adding `kimi-sdk` as a runtime dependency.
- Claiming native OpenAI Responses or Anthropic Messages support from Kimi's
  upstream API.
- Persisting prompts, responses, Headroom content, or provider secrets.
- Broad refactoring of the DeepSeek or generic chat adapter paths.
- Codex picker/profile synchronization unless separately scoped and tested.

## Functional requirements

### FR-1: OAuth-first authentication

The adapter must check the Kimi CLI credential artifact before consulting an
explicit bearer-token environment variable. A valid, non-expiring OAuth access
token is returned without a network call.

### FR-2: Refresh behavior

When the access token is expired or within a five-minute refresh margin, the
auth resolver must exchange the refresh token at
`https://auth.kimi.com/api/oauth/token`. Refresh must be serialized within the
process, use the Kimi OAuth client id, avoid logging secrets, and atomically
replace the credential file with mode `0600`.

An upstream unary `401` must trigger one forced refresh and one retry. Further
failure must stop with a bounded, secret-free error. Retrying indefinitely or
falling back silently after an explicitly rejected OAuth token is not allowed.

### FR-3: Explicit bearer fallback

If no usable OAuth artifact exists, `KIMI_BEARER_TOKEN` may provide the bearer
token. Missing credentials must fail with an actionable message that directs
the user to `kimi /login` or the fallback environment variable.

### FR-4: Responses API

The first-party gateway must own these Kimi-prefixed paths without LiteLLM:

- `POST /kimi/v1/responses`
- `GET /kimi/v1/models`
- `GET /kimi/v1/responses/{id}`
- `GET /kimi/v1/responses/{id}/input_items`

Unary responses must map to an OpenAI Responses object. Streaming responses
must emit canonical Reverso Responses SSE events, preserve tool-call deltas and
usage where available, store the finalized response before drain, and support
`previous_response_id` through the existing in-memory store.

### FR-5: Anthropic Messages API

Kimi must be a member of the Anthropic surface backend set and the real adapter
factory. The surface must accept both:

- Path-pinned requests under `/kimi/v1/messages`.
- Provider-qualified model routing such as `kimi/<upstream-model>` on
  `/v1/messages`.

The provider qualifier is a routing hint and must be stripped before the model
id reaches Kimi.

### FR-6: Model discovery

`KimiAdapter.list_models` must query the subscription endpoint's `/models`
resource with the resolved bearer token. A `401` may force one refresh and one
retry. Network, auth, or payload failure must return a deterministic offline
fallback instead of making model discovery crash the gateway.

Kimi is rowless in the Anthropic registry: an explicit `kimi/<model>` qualifier
is authoritative, while discovery aliases may use a small curated seed for
clients that require an Anthropic-prefixed picker id.

### FR-7: Capability gating

Every capability-matrix feature and endpoint must include a Kimi
classification. Initial classifications mirror the DeepSeek translation path,
not Kimi's theoretical upstream maximum, because Reverso's translator is the
effective capability ceiling. Image or file inputs remain unsupported until
the adapter translates and regression-tests them.

### FR-8: Headroom

No Kimi-specific Headroom implementation is allowed. The existing Responses
and Anthropic pre-dispatch compression seams must compress the request before
the Kimi adapter sees it, retain fail-open behavior, and keep all metrics
prompt-free.

## Security and reliability requirements

1. Never log bearer tokens, refresh tokens, credential-file payloads, upstream
   response bodies, or response headers that may echo authentication state.
2. Keep the gateway bound to `127.0.0.1:64946`.
3. Keep the frozen `ProviderAdapter` protocol unchanged.
4. Do not import LiteLLM from the Kimi provider path.
5. Bound transport timeouts and retries.
6. Treat malformed credential JSON as unavailable, not as a crash.
7. Ensure temporary credential files are removed after atomic replacement.
8. Keep error messages provider-specific but secret-free.
9. Do not persist Headroom request content or Kimi conversation content beyond
   Reverso's existing in-memory response store.

## Implementation map

| Concern | Primary seam |
|---|---|
| OAuth and bearer resolution | `src/reverso/protocols/adapters/kimi.py` |
| Chat and SSE translation | `src/reverso/protocols/adapters/kimi.py` plus existing OpenAI-compatible helpers/replay |
| Responses prefix | `src/reverso/protocols/responses_app.py` |
| Responses adapter composition | `src/reverso/proxy/compose.py` |
| Anthropic adapter composition | `src/reverso/protocols/anthropic_app.py` |
| Anthropic model authority | `src/reverso/protocols/surface_registry.py` |
| Capability source | `.omc/research/responses-parity-surface.json` and packaged mirror |
| Adapter regression tests | `tests/unit/test_kimi_adapter.py` |
| Cross-surface tests | `tests/integration/test_kimi_surfaces.py` |
| Headroom proof | Existing parameterized Responses Headroom integration tests |
| Architecture decision | `docs/architecture/adr/0017-kimi-code-oauth-provider.md` |

## Acceptance criteria

1. `/kimi/v1/responses` dispatches to `KimiAdapter` and never reaches the
   legacy LiteLLM app.
2. `/kimi/v1/messages` dispatches to `KimiAdapter` through the Anthropic app.
3. `kimi/<model>` resolves to the Kimi Anthropic backend and canonicalizes to
   the bare upstream model id.
4. A fresh OAuth artifact wins over `KIMI_BEARER_TOKEN`.
5. An expired OAuth artifact refreshes successfully and persists the rotated
   token with a future expiry.
6. `KIMI_BEARER_TOKEN` works when the OAuth artifact is absent.
7. Unary Kimi calls send `Authorization: Bearer <token>` to
   `/coding/v1/chat/completions` and return a valid Responses envelope.
8. Streaming calls emit canonical events and store a final response usable by
   `previous_response_id`.
9. Model discovery uses bearer auth and returns live account models when the
   endpoint succeeds.
10. Every Kimi capability row is present and validated by the shared table
    tests.
11. Existing parameterized Headroom tests include `kimi` and pass without any
    Kimi-specific compression branch.
12. `uv run pytest tests/unit -q` passes.
13. `uv run pytest tests/integration -q` passes, or unrelated pre-existing
    failures are captured with exact test ids and evidence.
14. `uv run python -m compileall -q src/reverso` and `git diff --check` pass.
15. A credentialed local smoke verifies one Responses request and one
    Anthropic Messages request without exposing the token in logs or output.

## Test strategy

### Offline unit tests

- OAuth priority over explicit bearer fallback.
- Bearer fallback with no OAuth artifact.
- Expired-token refresh, rotation, expiry, and persistence.
- Missing, malformed, and incomplete credential artifacts.
- Auth and upstream errors do not expose sentinels.
- Unary request translation, bearer header, response mapping, tool calls, usage,
  and previous-response storage.
- SSE parsing, terminal completion, tool-call deltas, and usage.
- Live-model response mapping and fallback behavior.

### Offline integration tests

- Responses prefix recognition and composition registry membership.
- Anthropic registry membership.
- Provider-qualified Anthropic resolution and canonicalization.
- LiteLLM quarantine for `/kimi/v1`.
- Headroom compression reaches Kimi only after compression and does not mutate
  other adapters.
- Capability matrix completeness.

### Credentialed local smoke

Prerequisites:

1. Install a current Kimi CLI.
2. Run `kimi /login` and confirm the credential artifact exists without printing
   its contents.
3. Start Reverso on loopback.

Then verify:

```bash
curl -sS http://127.0.0.1:64946/kimi/v1/models

curl -sS http://127.0.0.1:64946/kimi/v1/responses \
  -H 'content-type: application/json' \
  -d '{"model":"kimi-k2.5","input":"Reply with exactly: kimi-ok"}'

curl -sS http://127.0.0.1:64946/kimi/v1/messages \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"kimi-k2.5","max_tokens":32,"messages":[{"role":"user","content":"Reply with exactly: kimi-ok"}]}'
```

The live account may expose a different current model id. Use an id returned by
the first command instead of relying on the offline fallback.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Kimi changes the credential-file schema | Keep parsing defensive and pin behavior with artifact fixtures; verify against a current Kimi CLI before release |
| Refresh-token rotation races across processes | Use atomic writes and process-local serialization initially; add cross-process file locking if concurrent Reverso/Kimi refresh proves problematic |
| Kimi changes subscription endpoint behavior | Keep base URLs injectable in tests and fail with bounded provider errors |
| Capability table overstates Kimi support | Mirror the proven translator ceiling, not upstream marketing claims |
| SDK adoption creates redundant abstractions | Keep the direct HTTP adapter until an official SDK exposes real Responses or Messages transports and supports Reverso's Python floor |
| Default fallback model becomes stale | Prefer live `/models`; treat the fallback only as offline discovery metadata |
| Existing dirty provider work obscures regressions | Record baseline failures before editing and report exact failing test ids separately |

## Rollout and rollback

1. Land auth and adapter tests before routing changes.
2. Add the adapter and both surface registrations.
3. Add capability data and cross-surface tests.
4. Run offline unit and integration suites.
5. Run credentialed local smoke with Headroom enabled.
6. Restart the local Reverso LaunchAgent after verification.

Rollback is additive and local: remove Kimi from both composition registries and
the surface tables while leaving the credential artifact owned by Kimi CLI. No
data migration or secret deletion is required.

## References

- `docs/architecture/adr/0017-kimi-code-oauth-provider.md`
- `docs/architecture/adr/0002-responses-native-provider-gateway.md`
- `docs/architecture/adr/0006-anthropic-messages-api-surface.md`
- Kimi CLI: `https://github.com/MoonshotAI/kimi-cli`
- Kimi CLI provider documentation:
  `https://moonshotai.github.io/kimi-cli/en/configuration/providers.html`
