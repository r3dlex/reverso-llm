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
  model routing such as `kimi/kimi-k3` on `/v1/messages`.

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
6. Expose only `kimi-k3` through `/kimi/v1/models`, translate request dispatch
   to upstream `k3`, and retain only canonical K3 metadata in the bounded
   runtime fallback.
7. Register Kimi on the Responses and Anthropic surfaces.
8. Route only the provider-qualified Anthropic model id `kimi/kimi-k3`.
9. Add Kimi to the capability matrix using the existing translated
   OpenAI-compatible ceiling until richer behavior is explicitly proven.
10. Prove that Headroom compression still runs before Kimi adapter dispatch on
    both surfaces.
11. When a request has neither a usable CLI-owned artifact nor a bearer
    fallback, supervise one shared official `kimi login` process, reload the
    artifact after success, and resume the waiting request.
12. Generate the Kimi Codex profile and catalog with only `kimi-k3`, context
    window `1048576`, and live discovery as a mandatory synchronization input.

### Out of scope

- Implementing OAuth login, browser automation, or credential collection inside
  Reverso. The gateway may only supervise the official `kimi login` command and
  consume the CLI-owned artifact after the child exits.
- Kimi account creation, subscription purchase, or browser automation.
- Importing or vendoring Kimi CLI internals.
- Adding `kimi-sdk` as a runtime dependency.
- Claiming native OpenAI Responses or Anthropic Messages support from Kimi's
  upstream API.
- Persisting prompts, responses, Headroom content, or provider secrets.
- Broad refactoring of the DeepSeek or generic chat adapter paths.
- Codex picker/profile synchronization for providers other than Kimi.

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
the user to `kimi login` or the fallback environment variable.

If both sources are unavailable, the gateway must start or join one shared
`kimi login` attempt for the local credential surface. The original request
must remain pending without upstream dispatch, reload the CLI-owned artifact
after successful process exit, and resume only when that artifact is usable.
Missing executables, nonzero exits, and absent, malformed, or unusable
post-login artifacts must fail with bounded, secret-free errors.

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
- Provider-qualified model routing with `kimi/kimi-k3` on
  `/v1/messages`.

The provider qualifier is a routing hint and must be stripped before the model
id reaches Kimi. The public id `kimi-k3` must be translated to upstream `k3`
when the adapter dispatches the request.

### FR-6: Model discovery

`KimiAdapter.list_models` must query the subscription endpoint's `/models`
resource with the resolved bearer token. A `401` may force one refresh and one
retry. Successful discovery must expose exactly one public model, `kimi-k3`.
Network, auth, or payload failure must return deterministic canonical K3
fallback metadata instead of making runtime model discovery crash the gateway.

Kimi is rowless in the Anthropic registry. The explicit `kimi/kimi-k3`
qualifier and its `anthropic-kimi-kimi-k3` discovery alias are the only
Kimi model ids authorized on that surface.

Runtime fallback metadata is not valid evidence for Codex synchronization.
`reverso-codex-sync` must accept Kimi discovery only when the payload declares
`model_discovery_source: "live"` and contains exactly `kimi-k3`; stale,
fallback, mixed, empty, or otherwise noncanonical discovery must fail closed
without replacing the existing profile or catalog.

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

### FR-9: Codex metadata convergence

The generated Kimi profile must select `model = "kimi-k3"` and set
`model_context_window = 1048576` and
`model_auto_compact_token_limit = 419430` (40 percent). The profile-level
compact limit is required because Codex profile layering otherwise retains any
lower value from the base configuration. Its provider-specific catalog must
contain exactly one model with slug `kimi-k3`, `context_window = 1048576`, and
`max_context_window = 1048576`.

### FR-10: Usage telemetry

`GET /usage/kimi` must expose Kimi's five-hour and weekly subscription windows
with context and refresh metadata. Polling must use existing OAuth credentials
only and must never trigger interactive login. Refreshes must be asynchronous,
single-flight, cached for 60 seconds, retried with bounded backoff after failure,
and retain a last-known-good stale snapshot.

Kimi Responses and Kimi-routed Anthropic Messages responses must map the
five-hour window to the Codex primary rate-limit headers and the weekly window
to the secondary headers. Percent, window-minute, and Unix reset-time headers
must be present when the corresponding upstream window is valid. Telemetry
failure must not fail or delay the model response beyond the bounded poll.

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
| Codex profile and catalog sync | `src/reverso/codex_sync.py` and `src/reverso/protocols/model_exposure.py` |
| Capability source | `.omc/research/responses-parity-surface.json` and packaged mirror |
| Adapter regression tests | `tests/unit/test_kimi_adapter.py` |
| Cross-surface tests | `tests/integration/test_kimi_surfaces.py` |
| Headroom proof | Existing parameterized Responses Headroom integration tests |
| Architecture decision | `docs/architecture/adr/0017-kimi-code-oauth-provider.md` |

## Acceptance criteria

1. `/kimi/v1/responses` dispatches to `KimiAdapter` and never reaches the
   legacy LiteLLM app.
2. `/kimi/v1/messages` dispatches to `KimiAdapter` through the Anthropic app.
3. `kimi/kimi-k3` resolves to the Kimi Anthropic backend and dispatches the bare
   upstream model id `k3`.
4. A fresh OAuth artifact wins over `KIMI_BEARER_TOKEN`.
5. An expired OAuth artifact refreshes successfully and persists the rotated
   token with a future expiry.
6. `KIMI_BEARER_TOKEN` works when the OAuth artifact is absent.
7. Unary Kimi calls send `Authorization: Bearer <token>` to
   `/coding/v1/chat/completions` and return a valid Responses envelope.
8. Streaming calls emit canonical events and store a final response usable by
   `previous_response_id`.
9. Model discovery uses bearer auth and exposes exactly `kimi-k3` when the
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
16. Kimi Codex synchronization accepts only canonical live discovery and writes
    one `kimi-k3` profile/catalog entry with context window `1048576`.
17. `GET /usage/kimi` returns cached five-hour and weekly quota data without
    starting login, and malformed or unavailable telemetry returns a safe stale
    or unavailable snapshot.
18. Kimi Responses and Anthropic Messages responses expose the six Codex quota
    headers while repeated requests inside the cache TTL perform one upstream
    usage poll.

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
- Live K3 response mapping and canonical runtime fallback behavior.
- Public `kimi-k3` to upstream `k3` request translation.
- Five-hour and weekly usage parsing, malformed payload fallback, cache TTL,
  single-flight refresh, forced credential refresh after 401, and stale fallback.

### Offline integration tests

- Responses prefix recognition and composition registry membership.
- Anthropic registry membership.
- Provider-qualified Anthropic resolution and canonicalization.
- LiteLLM quarantine for `/kimi/v1`.
- Headroom compression reaches Kimi only after compression and does not mutate
  other adapters.
- Capability matrix completeness.
- Codex sync rejection for fallback, stale, mixed, empty, and noncanonical Kimi
  discovery.
- Generated Kimi profile and catalog context metadata.
- Codex quota response headers on Kimi Responses and Anthropic Messages with
  one cached upstream usage poll across repeated requests.

### Credentialed local smoke

Prerequisites:

1. Install a current Kimi CLI.
2. Run `kimi login` and confirm the credential artifact exists without printing
   its contents.
3. Start Reverso on loopback.

Then verify:

```bash
curl -sS http://127.0.0.1:64946/kimi/v1/models

curl -sS http://127.0.0.1:64946/kimi/v1/responses \
  -H 'content-type: application/json' \
  -d '{"model":"kimi-k3","input":"Reply with exactly: kimi-ok"}'

curl -sS http://127.0.0.1:64946/kimi/v1/messages \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"kimi-k3","max_tokens":32,"messages":[{"role":"user","content":"Reply with exactly: kimi-ok"}]}'
```

The first command must expose only `kimi-k3`. A fallback discovery response is
valid for bounded runtime inspection but is not valid input to Codex sync.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Kimi changes the credential-file schema | Keep parsing defensive and pin behavior with artifact fixtures; verify against a current Kimi CLI before release |
| Refresh-token rotation races across processes | Use atomic writes and process-local serialization initially; add cross-process file locking if concurrent Reverso/Kimi refresh proves problematic |
| Kimi changes subscription endpoint behavior | Keep base URLs injectable in tests and fail with bounded provider errors |
| Capability table overstates Kimi support | Mirror the proven translator ceiling, not upstream marketing claims |
| SDK adoption creates redundant abstractions | Keep the direct HTTP adapter until an official SDK exposes real Responses or Messages transports and supports Reverso's Python floor |
| Runtime fallback obscures stale deployment | Keep fallback metadata canonical to K3 and reject every non-live discovery source during Codex sync |
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
