---
title: OpenRouter provider through Reverso for Codex and Claude Code
type: active-specification
status: active
created: 2026-08-21
updated: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider
source_context: .omx/context/openrouter-reverso-provider-20260821T173014Z.md
source_plan: .omx/plans/openrouter-reverso-provider.md
---

# OpenRouter provider through Reverso for Codex and Claude Code

## Target result

Add OpenRouter as a first-party Reverso-routed provider on the existing
`127.0.0.1:64946` listener. Codex uses OpenRouter's OpenAI-compatible Responses
API through a dedicated Reverso profile. Claude Code uses OpenRouter's native
Anthropic Messages API through a managed Reverso launcher and catalog. Both
surfaces share one credential, one live model inventory, one routing policy,
and Reverso's existing embedded Headroom pass.

This plan stops before implementation. It does not modify the frozen
`ProviderAdapter`, add a new listener, create an OpenRouter account, generate an
API key, or make a metered inference request.

## Requirements summary

1. Preserve the frozen Responses-shaped `ProviderAdapter` contract in
   `src/reverso/protocols/adapter.py:119-145`.
2. Use OpenRouter's direct HTTP APIs:
   - `POST https://openrouter.ai/api/v1/responses` for Codex.
   - `POST https://openrouter.ai/api/v1/messages` for Claude Code.
   - `GET https://openrouter.ai/api/v1/models` for authenticated catalog
     discovery.
3. Keep OpenRouter routing behind Reverso. Clients must point only at the local
   Reverso gateway, never directly at `openrouter.ai`.
4. Store `OPENROUTER_API_KEY` in the existing Reverso Keychain/install boundary.
   Never write it into generated Codex profiles, Claude launchers, catalogs,
   logs, status JSON, or repository files.
5. Preserve provider-qualified OpenRouter model ids such as
   `stealth/ox-alpha`. Do not strip the author namespace.
6. Keep OpenRouter provider routing explicit and fail-closed for coding agents:
   require requested parameters, deny data collection by default, require Zero
   Data Retention when a compatible endpoint exists, and do not silently change
   models.
7. Expose a model only when live OpenRouter metadata and Reverso's protocol
   compatibility filter agree that the model is usable on the relevant client
   surface.
8. Preserve existing built-in Codex GPT selectors and existing Reverso provider
   catalogs. OpenRouter is additive.
9. Do not add a new long-lived process, SDK dependency, or LiteLLM route.
10. Publish only models in a user-owned curated allowlist. Live discovery
    validates allowlisted models but never expands client catalogs automatically.
11. Permit weaker privacy only through an attended, exact-model grant bound to a
    Reverso-managed client launcher process. Revoke it on launcher exit or
    gateway restart and cap it at 24 hours.
12. Enforce user-configured hard limits for estimated per-request cost and
    cumulative launcher-session cost. No inference is allowed until both limits
    are configured.

## Verified external facts

- OpenRouter documents its OpenAI-compatible base URL as
  `https://openrouter.ai/api/v1` and supports the OpenAI SDK by overriding
  `base_url` and `api_key`.
  Source: <https://openrouter.ai/docs/quickstart#using-the-openai-sdk>
- OpenRouter documents a direct, OpenAI-compatible Responses endpoint at
  `POST /api/v1/responses`. It is stateless: `store: true` and
  `previous_response_id` are not supported. It documents reasoning, tool
  calling, streaming, and web search.
  Source: <https://openrouter.ai/docs/api_reference/responses/overview>
- OpenRouter documents a direct Anthropic Messages endpoint at
  `POST /api/v1/messages`, using bearer authorization, with text, images, PDFs,
  tools, streaming, and extended thinking.
  Source: <https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message>
- OpenRouter's routing API can require parameter support, order or allow
  providers, disable fallbacks, deny data collection, and restrict routing to
  Zero Data Retention endpoints.
  Source: <https://openrouter.ai/docs/features/provider-routing>
- OpenRouter states that endpoint providers have independent logging and data
  retention policies, and that training controls and retention constraints must
  be applied deliberately.
  Source: <https://openrouter.ai/docs/features/privacy-and-logging>
- The live unauthenticated model registry returned `stealth/ox-alpha` on
  2026-08-21 with a 1,048,576-token context window, 131,072 maximum completion
  tokens, text/image/video input, mandatory reasoning, tool calling, and a zero
  listed prompt/completion price. This is discovery evidence, not a durability,
  availability, or future-pricing guarantee.
  Sources: <https://openrouter.ai/api/v1/models> and
  <https://openrouter.ai/stealth/ox-alpha?view=api#providers>

## Current Reverso seams

- `src/reverso/protocols/adapter.py:17-59` already carries unknown Responses
  fields in `ResponsesRequest.extra`, allowing OpenRouter routing and reasoning
  fields to remain adapter-owned without changing the protocol.
- `src/reverso/protocols/adapters/deepseek.py:110-146` demonstrates injectable
  `httpx.AsyncClient` transport and call-time bearer credential resolution.
- `src/reverso/protocols/adapters/deepseek.py:187-219` demonstrates bounded
  forwarding of extra fields and explicit ownership of translated keys.
- `src/reverso/proxy/compose.py:88-106` is the adapter composition point.
- `src/reverso/protocols/surface_registry.py:264-299` is the fail-closed
  Anthropic backend resolution point.
- `src/reverso/protocols/model_exposure.py:114-178` owns Reverso-routed Codex
  profile generation.
- `src/reverso/protocols/model_exposure.py:240-251` owns selector naming for
  Codex and Claude Code.
- `src/reverso/claude_code_sync.py:272-321` owns provider-scoped Claude Code
  launchers and catalog headers.
- `config/supported-client-surfaces.json` is the convergence manifest for
  generated client surfaces.

## Decision and options

### Option A: direct HTTP for both native upstream protocols - chosen

Implement one `OpenRouterProvider` with shared auth, catalog, routing policy,
and transport configuration. Its frozen-contract adapter facet forwards
Responses requests to `/api/v1/responses`. Its internal native-Messages facet
forwards Anthropic requests to `/api/v1/messages`.

Pros:

- Preserves protocol fidelity for both clients.
- Matches Reverso's existing `httpx` and injectable transport patterns.
- Adds no dependency and keeps wire behavior observable in tests.
- Avoids translating Claude tool/thinking blocks through Responses and back.

Cons:

- Requires two carefully bounded request/stream mappers.
- OpenRouter Responses is stateless, so Reverso must own chaining semantics.

### Option B: OpenAI SDK for Responses plus direct HTTP for Messages

Pros:

- Familiar client ergonomics for the Responses branch.

Cons:

- Adds a dependency for only half of the provider.
- Wraps transport behavior already standardized around `httpx` in Reverso.
- Does not remove the need for native Messages implementation or wire-level
  contract tests.

Rejected because it increases dependency and abstraction cost without reducing
the hard protocol work.

### Option C: chat-completions translation for both clients

Pros:

- Could reuse portions of `openai_chat.py` and the DeepSeek adapter.

Cons:

- Discards native Responses and Messages fidelity.
- Creates avoidable risk for tool loops, reasoning, images, usage, and SSE.
- Contradicts the user's goal of using OpenRouter's direct APIs.

Rejected.

### Option D: point Codex and Claude Code directly at OpenRouter

Pros:

- Smallest Reverso code change.

Cons:

- Bypasses Reverso routing, Headroom, convergence, credentials, and telemetry.
- Violates the requested through-Reverso architecture.

Rejected.

## Proposed architecture

```text
Codex
  -> http://127.0.0.1:64946/openrouter/v1/responses
  -> Reverso normalize + Headroom
  -> OpenRouterProvider.responses
  -> https://openrouter.ai/api/v1/responses

Claude Code
  -> http://127.0.0.1:64946/openrouter/v1/messages
  -> Reverso Anthropic validation + Headroom
  -> OpenRouterProvider.native_messages
  -> https://openrouter.ai/api/v1/messages

Shared OpenRouterProvider infrastructure
  -> Keychain-backed OPENROUTER_API_KEY
  -> GET https://openrouter.ai/api/v1/models
  -> model capability and client-surface filtering
  -> routing/privacy policy injection
  -> bounded error and usage telemetry
```

### Naming

- Runtime provider id: `openrouter`.
- Codex model provider: `reverso_openrouter`.
- Codex profile: `reverso-openrouter`.
- Claude launcher: `claude-openrouter`.
- Selector and catalog slugs: `openrouter/<author>/<model>`, for example
  `openrouter/stealth/ox-alpha`.
- Upstream request model: strip only the outer Reverso selector prefix and send
  `stealth/ox-alpha` unchanged.

The explicit outer `openrouter/` prefix prevents collisions with Reverso's
built-in GPT, Claude, DeepSeek, Kimi, Copilot, Auggie, and future direct provider
names. The upstream author namespace remains part of model identity.

### Authentication and headers

- Read `OPENROUTER_API_KEY` at call time through the same secret boundary used
  for direct API providers.
- Send `Authorization: Bearer <key>` and `Content-Type: application/json`.
- Configure optional attribution headers centrally:
  `HTTP-Referer` and `X-OpenRouter-Title`. They must contain static public-safe
  product metadata only, never local paths, usernames, workspace names, or
  prompt-derived values.
- Do not proxy inbound client authorization to OpenRouter.
- Redact upstream bodies and headers from errors. Preserve only bounded status,
  request id, provider/model, and safe OpenRouter error code fields.

### Routing and privacy policy

Default policy for coding-agent traffic:

```json
{
  "provider": {
    "require_parameters": true,
    "allow_fallbacks": true,
    "data_collection": "deny",
    "zdr": true
  }
}
```

The implementation spike must verify whether each allowlisted model has at least
one endpoint satisfying the ZDR and parameter requirements. If not, the model is
`policy_incompatible` by default. A user may create an attended, exact-model
privacy override session after Reverso displays the selected endpoint provider,
each relaxed constraint, known retention/training terms, the 24-hour maximum
TTL, and current pricing. Unknown policy or pricing blocks the grant. The grant
is in memory, bound to one managed launcher process, revoked on exit or gateway
restart, and revalidated before every request.

Model fallback is disabled by omission: do not send OpenRouter's `models` array
and do not substitute a different model id. Provider endpoint fallback for the
same model may remain enabled when policy-compatible.

### Allowlist, launcher grants, and spend limits

- `stealth/ox-alpha` is the initial allowlisted model. Future models are added
  explicitly through a Reverso CLI command backed by a user-owned, secret-free
  local configuration file.
- The allow command validates live identity and capabilities before persistence.
  Generated Codex and Claude Code catalogs are derived artifacts.
- Catalog exposure is surface-specific: a model may appear only in Codex or only
  in Claude Code when it passes only that protocol's compatibility checks.
- The weaker-privacy grant is carried by a Reverso-managed Codex or Claude Code
  launcher session and is scoped to one exact upstream model.
- Before the first inference, the user must configure maximum estimated
  per-request cost and cumulative launcher-session cost. Reverso ships no
  arbitrary spending defaults.
- Reverso revalidates privacy and pricing before each request. A material policy
  weakening or price above the approved limit revokes the grant and requires new
  attended consent.
- Pre-request estimation gates new work. An accepted streaming response is not
  terminated for an estimate overrun; actual reported cost is recorded and later
  requests are blocked once the session limit is reached.
- Removing a model from the allowlist permits an in-flight request to finish,
  rejects subsequent requests, and revokes grants for that model.

### Responses state boundary

OpenRouter's Responses API is stateless. Reverso must not forward
`previous_response_id`, `store`, or retrieval assumptions upstream. Instead:

1. Store each completed upstream response in the existing `ResponseStore`.
2. When Codex supplies a Reverso `previous_response_id`, resolve it locally and
   materialize the required prior input/output items into the new stateless
   upstream request.
3. Preserve tool calls, tool outputs, reasoning continuity fields that
   OpenRouter documents as replayable, images, and instructions.
4. Serve `GET /responses/{id}` and `/input_items` from Reverso's local store.
5. Fail with a bounded local unknown-response error when the id is absent; do
   not send an unusable id upstream.

This behavior needs a focused spike because naive text-only replay would break
Codex tool loops.

### Native Messages boundary

Extend the existing optional internal native-Messages dispatch seam proposed
for the Ollama provider rather than changing `ProviderAdapter`. The provider
facet accepts Reverso's validated Anthropic request, adds only OpenRouter-owned
routing fields, forwards native SSE/unary Messages traffic, and maps bounded
errors and usage back to the inbound Anthropic contract.

Do not route Claude Code through OpenRouter Responses unless the native Messages
endpoint fails a required compatibility test. That fallback would be a new
architecture decision, not an automatic implementation shortcut.

## Preliminary implementation slices - superseded

The G0-G4 section below is retained only as planning history. Do not execute it. The authoritative delivery contract is the OR-G0 through OR-G7 DAG in `.omx/plans/prd-openrouter-reverso-provider.md` and its eight governed work items under `.ai/work-intake/`.

### G0: attended compatibility and policy spike

Files:

- `docs/spike-notes.md`
- new recorded fixtures under `tests/fixtures/openrouter/`

Work:

1. With user-provided credentials, make explicit attended, low-cost probes for
   `GET /api/v1/models`, unary/streaming Responses, unary/streaming Messages,
   function tools, tool results, images, reasoning, and errors.
2. Probe `stealth/ox-alpha` only after displaying that its zero listed price and
   stealth identity are time-varying metadata, not a guarantee.
3. Verify ZDR plus `require_parameters` compatibility before catalog exposure.
4. Record redacted request/response shapes, headers, status codes, SSE grammar,
   usage, and provider-routing metadata. Never record prompts or credentials.
5. Decide the exact stateless replay mapping for Codex tool loops.
6. Verify the attended privacy disclosure, exact-model launcher grant, 24-hour
   TTL, exit/restart revocation, pricing revalidation, and spend-limit behavior.

Exit criteria:

- Both native upstream endpoints are proven or the plan is revised before
  implementation.
- Privacy policy compatibility is known for the initial model set.
- No metered probe runs without attended approval.

### G1: shared OpenRouter infrastructure and Responses adapter

Files:

- new `src/reverso/protocols/adapters/openrouter.py`
- new `src/reverso/protocols/openrouter_client.py`
- `src/reverso/proxy/compose.py`
- `src/reverso/protocols/responses_app.py`
- `src/reverso/protocols/data/responses_parity_surface.json`
- unit and integration tests

Work:

1. Add call-time credential resolution, safe headers, timeouts, injectable
   transport, retry classification, and bounded errors.
2. Implement authenticated model discovery with a bounded last-known-good cache
   and explicit `live`, `cached`, `unavailable`, and `policy_incompatible`
   provenance.
3. Implement native Responses unary and SSE forwarding while preserving
   Reverso-generated storage and replay semantics.
4. Add OpenRouter routing/privacy policy without allowing inbound payloads to
   overwrite Reverso-owned auth or mandatory policy fields.
5. Compose the `openrouter` adapter without mutating legacy LiteLLM prefixes.
6. Implement user-owned allowlist validation, in-memory privacy grants, and
   per-request/session spend enforcement behind injectable clocks and pricing.

Exit criteria:

- Codex-compatible unary, streaming, tool-loop, reasoning, usage, and error
  fixtures pass.
- Unknown or missing prior response ids fail locally and deterministically.
- No secret or prompt appears in logs or generated state.

### G2: native Messages provider facet

Files:

- `src/reverso/protocols/anthropic_app.py`
- `src/reverso/protocols/surface_registry.py`
- shared OpenRouter provider/client modules from G1
- Anthropic parity tests

Work:

1. Register `openrouter` as an Anthropic-surface backend.
2. Dispatch validated Messages requests to the provider's native facet after
   Headroom and before Responses translation.
3. Preserve content blocks, tool use/results, images/PDFs, thinking, stop
   reasons, usage, request ids, and canonical SSE order.
4. Enforce the same model identity and privacy policy as the Responses surface.

Exit criteria:

- Claude Code-compatible unary, streaming, tool-loop, image, thinking, usage,
  and error fixtures pass.
- Existing Anthropic backends remain byte/behavior compatible.

### G3: catalogs, profiles, launchers, and convergence

Files:

- `src/reverso/protocols/model_exposure.py`
- `src/reverso/codex_sync.py`
- `src/reverso/claude_code_sync.py`
- `src/reverso/client_sync.py`
- `config/supported-client-surfaces.json`
- convergence fixtures and tests

Work:

1. Generate a provider-scoped Codex catalog and `reverso-openrouter` profile.
2. Generate a `claude-openrouter` launcher and exact provider-scoped aliases.
3. Filter the live inventory separately for Responses and Messages capability.
4. Carry exact context and output metadata when OpenRouter supplies it; use
   unknown/null rather than guessed limits.
5. Archive only exact stale generated artifacts owned by Reverso.
6. Prove dry-run/apply/apply/refresh/verify idempotency and user-file
   preservation.
7. Add the OpenRouter allowlist CLI and managed client launchers that issue and
   revoke session grants without persisting grant tokens.

Exit criteria:

- `openrouter/stealth/ox-alpha` is selectable only when live and
  policy-compatible for the target surface.
- Built-in GPT ids remain bare defaults and all existing provider catalogs are
  unchanged.

### G4: documentation, observability, and attended end-to-end proof

Files:

- `README.md`
- `docs/03-architecture.md`
- `docs/04-mvp.md`
- deployment drift and convergence acceptance checks

Work:

1. Document Keychain setup, optional attribution, privacy defaults, refresh,
   revocation, and uninstall behavior.
2. Add safe metrics for upstream endpoint/provider, selected model, latency,
   status, token usage, cost when reported, catalog provenance, and routing
   policy outcome.
3. Run attended Codex and Claude Code smoke sessions through Reverso with tool
   use and a follow-up turn.
4. Verify Keychain removal produces a bounded auth error and no fallback to a
   different provider.

Exit criteria:

- Both client banners/config diagnostics prove local Reverso routing and the
  `openrouter` provider.
- The OpenRouter activity/usage view correlates with the two attended requests.
- Full unit, integration, convergence, drift, and smoke suites pass.

## Acceptance criteria

1. A Codex request using `openrouter/stealth/ox-alpha` reaches Reverso's
   `/openrouter/v1/responses`, then OpenRouter `/api/v1/responses`, and completes
   unary and streaming tool loops without direct client credentials.
2. A Claude Code request using the same upstream model reaches Reverso's
   `/openrouter/v1/messages`, then OpenRouter `/api/v1/messages`, and completes
   unary and streaming tool loops with valid Anthropic event order.
3. Headroom runs once before upstream dispatch on both surfaces and preserves
   tools, tool results, images, instructions, and model identity.
4. `OPENROUTER_API_KEY` is absent from git, generated profiles, generated
   launchers, catalogs, logs, diagnostics, and error bodies.
5. `previous_response_id` is resolved locally; it is never forwarded to the
   stateless OpenRouter endpoint.
6. Catalog refresh never fabricates availability and reports its provenance.
7. The default routing policy does not select a data-collecting or non-ZDR
   endpoint. Weaker privacy requires an attended exact-model launcher grant that
   expires on exit/restart or after 24 hours.
8. OpenRouter model fallback does not occur. Same-model provider fallback is
   permitted only within the configured privacy/parameter policy.
9. Existing provider tests and all client convergence fixtures remain green.
10. A second sync apply is a no-op and user-owned files are unchanged.
11. Only curated allowlisted models are published, and each appears only on
    protocol-compatible client surfaces.
12. No inference runs until user-defined request/session spend limits exist;
    pricing is checked before each request and accepted streams finish before
    later work is blocked.

## Verification commands

Run from `reverso/` after implementation:

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run ruff check src tests
uv run reverso-client-sync dry-run --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync refresh --json
uv run reverso-client-sync verify --json
./scripts/convergence-acceptance.sh
uv run python scripts/check-deployment-drift.py --phase acceptance
```

Attended smoke commands must be added only after G0 proves the exact safe
payloads. They must report selected provider/model, local gateway route, and
OpenRouter request id while redacting credentials and prompt content.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Stealth model identity, price, availability, or policy changes | Treat `stealth/ox-alpha` as a live-catalog candidate, not a static guaranteed default; refresh and fail closed. |
| Responses statelessness breaks Codex continuation/tool loops | Implement local full-item replay and contract-test multi-turn tool loops before exposure. |
| Provider fallback weakens privacy | Inject mandatory data/ZDR/parameter policy after parsing inbound payloads; do not allow client override. |
| OpenRouter model ids collide with existing selectors | Use outer `openrouter/` selector namespace and retain upstream author/model identity. |
| Native Messages behavior differs from Anthropic | Record G0 wire fixtures and gate Claude exposure on parity tests. |
| SDK hides transport or adds dependency churn | Use direct `httpx` transport; revisit an SDK only if a measured protocol gap cannot be handled safely. |
| Catalog size overwhelms client pickers | Filter by target-surface capability and configurable allowlist; ship a small initial allowlist with live discovery metadata. |
| Cost surprises | Require user-configured hard request/session limits, preflight estimates, actual-cost accounting, and attended approval for unknown pricing. |
| Secrets leak in errors or telemetry | Reuse bounded error patterns, redact headers/bodies, and add negative leak tests. |

## Resolved intake decisions

1. Start with `stealth/ox-alpha`; future models require explicit allowlist
   expansion through the Reverso CLI.
2. Default to ZDR and denied data collection. A weaker policy requires an
   attended, exact-model launcher-session grant with a 24-hour maximum TTL.
3. Expose allowlisted models independently per compatible client surface.
4. Ship no arbitrary spend defaults. Require explicit request/session limits
   before first inference and explicit session approval for unknown pricing.
5. Set only `X-OpenRouter-Title: Reverso` initially. Omit `HTTP-Referer` unless a
   stable public project URL is intentionally configured later.

## ADR summary

### Decision

Integrate OpenRouter as a first-party, dual-protocol Reverso provider using
direct `httpx` calls to native Responses and native Anthropic Messages APIs.

### Drivers

- Protocol fidelity for Codex and Claude Code.
- Preservation of Reverso Headroom, convergence, secrets, and observability.
- Minimal dependencies and reuse of existing injectable transport patterns.

### Alternatives considered

- OpenAI SDK for Responses plus direct Messages.
- Chat-completions translation.
- Direct client-to-OpenRouter configuration.

### Why chosen

OpenRouter already provides the two required native endpoints. Direct HTTP
keeps the wire contracts explicit, avoids a one-surface SDK dependency, and
fits Reverso's frozen adapter plus internal native-Messages design.

### Consequences

- Reverso must own stateless Responses continuation and two native wire mappers.
- Provider privacy/routing policy becomes a first-class configuration and test
  boundary.
- Live catalog filtering is required before client exposure.

### Follow-ups

1. Convert this crystallized plan into an active specification and governed
   handoff.
2. Record G0 attended evidence without secrets or prompt content.
3. Use the governed OR-G0 through OR-G7 work items before any source edit.

## Preliminary stop condition - superseded

Planning is complete when this artifact is reviewed against the active Ollama
provider specification, the user selects the G0 privacy/cost boundaries, and a
separate implementation workflow is explicitly invoked. No provider code is
implemented as part of this plan.

## Ralplan admission-authority amendment

This amendment supersedes conflicting preliminary language above.

1. The managed launcher obtains one unguessable, exact-launcher, exact-model capability through an attended local issuance handshake. It carries the capability in a Reverso-owned request header whose exact name and real-client feasibility OR-G0 must prove. The gateway consumes and strips it before adapter dispatch. PID and liveness may trigger revocation but never authorize.
2. If either real client cannot safely carry a per-launch capability, weaker-privacy grants remain unavailable for that client. Default-policy traffic does not need a relaxation grant but still needs both hard budgets and all other admission evidence.
3. Cached and last-known-good metadata is discovery-only. Every admission requires fresh authenticated endpoint-specific policy and pricing evidence, an immutable revision, a proven freshness bound, and mandatory routing that limits dispatch to evidence-compatible endpoints. Failure to prove this stops the architecture at OR-G0.
4. The cumulative spend limit is scoped to one exact launcher capability and exact model. Concurrent requests from that launcher share one atomic ledger. Codex and Claude launchers have separate isolated budgets.
5. Codex presents `openrouter/<author>/<model>`. Claude presents an OR-G0-proven Claude-discoverable `anthropic-openrouter-...` alias that exact catalog authority maps to unchanged upstream `author/model` bytes.
6. Normal post-admission policy, consent, allowlist, or budget drift never kills accepted work. Shutdown stops admission, drains for a bound, cancels what remains, reconciles reported cost or retains conservative reservations, revokes grants, and closes the shared client once.
7. The authoritative delivery DAG is `OR-G0` through `OR-G7` in `.omx/plans/prd-openrouter-reverso-provider.md`. The preliminary G0-G4 slices above are retained only as superseded planning history.
