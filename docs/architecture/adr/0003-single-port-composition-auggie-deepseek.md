---
type: adr
project: reverso
id: 0003
title: Single-Port Composition and Auggie/DeepSeek Provider Registration
status: Accepted
date: 2026-06-09
supersedes: none
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/03-architecture.md
  - docs/04-mvp.md
  - .omc/plans/ralplan-auggie-deepseek-responses.md
  - .omc/plans/prd-auggie-deepseek-responses.md
  - .omc/plans/test-spec-auggie-deepseek-responses.md
consensus: "ralplan Architect APPROVE + Critic APPROVE (.omc/state/ralplan/architect-review-auggie-deepseek-responses-20260609-re-review.md, .omc/state/ralplan/critic-review-auggie-deepseek-responses-20260609-re-review.md)"
---

# ADR 0003: Single-Port Composition and Auggie/DeepSeek Provider Registration

## Status

Accepted. The ralplan consensus gate for the Auggie and DeepSeek Responses milestone is
complete (Architect APPROVE, Critic APPROVE on pass 2). This ADR began as the docs-first
deliverable that, together with the companion sections in `docs/03-architecture.md` and
`docs/04-mvp.md`, defined the boundary for the increment. Implementation has since landed
under that boundary: the composition root (`src/reverso/proxy/compose.py`) owns port 64946,
`src/reverso/proxy/main.py` boots `reverso.proxy.compose:app`, `APP_PROVIDER_PREFIXES`
includes `auggie` and `deepseek`, and the Auggie and DeepSeek adapters implement the frozen
five-method Protocol. The Auggie spike (`.omc/research/auggie-sdk-spike.md`) resolved the
SDK-versus-subprocess question to the bounded `auggie` CLI subprocess path described in D5.

This ADR extends, and does not supersede, ADR 0002. ADR 0002 established the first-party
Responses gateway for Claude and Copilot but deferred one decision: how the first-party
`ResponsesGatewayApp` actually owns loopback port 64946 when the process entrypoint still
boots the legacy LiteLLM app. This ADR resolves that composition decision and registers two
more providers (Auggie and DeepSeek) on the same gateway.

## Context

### What ADR 0002 left open

ADR 0002 D1 fixed the single-port, path-prefixed endpoint model and D2 quarantined LiteLLM
behind a runtime guard. The first-party app exists as a build-time artifact:

- `src/reverso/protocols/responses_app.py` defines `ResponsesGatewayApp`, built by
  `build_app(adapters: dict[str, ProviderAdapter])` (`responses_app.py:352`).
- It dispatches on `split_provider_path()` (`responses_app.py:57`) against a hard allow-list
  `APP_PROVIDER_PREFIXES = frozenset({"claude", "copilot"})` (`responses_app.py:42`).
- The constructor rejects any adapter prefix not in the allow-list
  (`responses_app.py:277-283`).
- The module does not import `reverso.proxy.app`; the legacy wrapper is named only in a
  docstring (`responses_app.py:7`).

But the process entrypoint is unchanged: `src/reverso/proxy/main.py:98-102` boots
`reverso.proxy.app:app` under uvicorn on `127.0.0.1:64946` (`main.py:93` reads
`REVERSO_PORT`, default 64946). `reverso.proxy.app:app` is a middleware stack wrapping
`litellm.proxy.proxy_server.app` (`app.py:11,22-32`), including
`ProfileRoutingMiddleware`, which rewrites `/<profile>/v1/...` to `/v1/...` for the legacy
prefixes `frozenset({"deepseek", "claude"})` (`profile_routing.py:29,69`).

So `ResponsesGatewayApp` is never reached at runtime today. Nothing wires it to the port.
That is the composition gap.

### What this increment adds

The Auggie and DeepSeek Responses milestone registers two more providers as same-port,
path-prefixed Responses endpoints (`/auggie/v1/...`, `/deepseek/v1/...`). DeepSeek moves to a
first-party adapter that calls the DeepSeek API directly, mirroring the existing subprocess
precedent in `src/reverso/protocols/adapters/claude.py:26`, and is explicitly not served by
LiteLLM fallthrough for this path. Auggie is served through a bounded `auggie` CLI subprocess:
the Phase 1 spike (`.omc/research/auggie-sdk-spike.md`) resolved the SDK-versus-subprocess
question in favor of the CLI subprocess path, following the same precedent. Both additions
need the gateway to actually own the port, which is why this milestone is the one that must
resolve the composition gap.

### Hard repo constraints (AGENTS.md)

Bind `127.0.0.1:64946` only; no secrets in version control or logs; `uv`-managed Python;
frontmatter on every markdown file; no em or en dash characters; never delete spec content
(augment or deprecate in place). No new port, listener, process, or provider sidecar may be
introduced by this milestone.

## Decision

### D1. Resolve the composition gap with a composition root (front dispatcher)

Introduce a thin composition-root ASGI module that owns the port and dispatches by leading
path segment. The process entrypoint boots the composition root instead of the legacy app
directly:

- New module (for example `src/reverso/proxy/compose.py`) exposes `app`, an ASGI callable
  that inspects the leading path segment of each request.
- If the leading segment is in `APP_PROVIDER_PREFIXES`, the request is dispatched to
  `ResponsesGatewayApp` (built via `build_app(adapters)`).
- Every other path is delegated to the legacy `reverso.proxy.app:app` (the LiteLLM middleware
  stack). This is the named legacy-fallthrough surface.
- `src/reverso/proxy/main.py:99` changes its uvicorn target from `"reverso.proxy.app:app"` to
  the composition root (for example `"reverso.proxy.compose:app"`). Host and port handling
  (`main.py:92-93`, loopback-only, `REVERSO_PORT` default 64946) is unchanged.

This is mount/wrap/replace resolved as replace-the-boot-target with a front dispatcher, not
mounting sub-apps inside the LiteLLM stack and not making the gateway import the legacy app.

Rejected sub-alternatives:

- Mount the first-party gateway inside the legacy app: rejected. First-party requests would
  traverse the LiteLLM middleware stack before reaching the gateway, which weakens the D2
  quarantine and couples the gateway to legacy middleware ordering.
- Make `ResponsesGatewayApp` the outer app and import the legacy app as an inner fallthrough:
  rejected. That would put `reverso.proxy.app` in the gateway's import graph and violate the
  ADR 0002 D2 invariant that `responses_app.py` must not import `reverso.proxy.app`. The
  composition root keeps the gateway pure: the only module that imports both the gateway and
  the legacy app is the composition root, which is not in the gateway's import graph.

Why this satisfies the constraints:

- Single port, single process: the same uvicorn invocation binds `127.0.0.1:64946`; only the
  ASGI callable changes. No new listener, process, or sidecar.
- Quarantine stays falsifiable: first-party prefixes never enter the legacy middleware stack,
  so the runtime guard can assert both zero `litellm.proxy.proxy_server.app` invocations and
  that the `reverso.proxy.app` wrapper is bypassed for first-party prefixes (addresses the
  Critic pass-2 executor note).
- `responses_app.py` keeps its no-import-of-`reverso.proxy.app` property unchanged.

### D2. Register Auggie and DeepSeek by extending the allow-list and the adapter map

No new router type is created. The merged `ResponsesGatewayApp` owns dispatch. Registration
is exactly two changes plus adapters:

- Extend `APP_PROVIDER_PREFIXES` (`responses_app.py:42`) to
  `frozenset({"claude", "copilot", "auggie", "deepseek"})`.
- Pass the `auggie` and `deepseek` adapters into `build_app(adapters)` alongside the existing
  ones. The constructor allow-list guard (`responses_app.py:277-283`) then admits them and
  still rejects any unknown prefix.

`/models` metadata (including any Auggie capability or indexing caveat) flows through
`models_with_codex_refresh()` (`responses_app.py:161`), not through a new adapter method.

### D3. Frozen five-method adapter interface (no `capabilities`)

Auggie and DeepSeek adapters implement exactly the frozen `ProviderAdapter` Protocol
(`adapter.py:125-141`): `create_response`, `stream_response`, `list_models`, `get_response`,
`list_input_items`. There is no `capabilities` method. Any sixth method is gated on a
frozen-interface amendment plus a mini-review, not added ad hoc.

### D4. DeepSeek is first-party, not LiteLLM fallthrough, and must not inherit drop_params

The `/deepseek/v1/responses` path is served by the first-party DeepSeek adapter that calls the
DeepSeek API directly. The legacy LiteLLM DeepSeek configuration is deprecated in place, not
deleted: the four DeepSeek entries at `config/litellm_config.yaml:90-116` carry
`additional_drop_params: *codex_drop_params`, and `config/litellm_config.yaml:23` strips
`response_format`. These are the live bound the first-party adapter must not inherit. The
first-party adapter must not strip `response_format` (gates JSON mode) and must not strip
`reasoning_content` (gates thinking mode). Both modes start `unverified (spike-gated)` in the
mode matrix and promote to `pass` only when their survival tests are green:

- JSON mode: a test proves `response_format` survives end-to-end to the DeepSeek call.
- Thinking mode: a two-turn fixture asserts turn-1 `reasoning_content` is carried into the
  turn-2 request, or an explicit rejection is returned before an invalid continuation.

### D5. Auggie indexing default and falsifiable caveat

If the Phase 1 spike cannot prove a hard indexing-disable control in the SDK or CLI, the
default workspace is no-workspace or sandbox (never the caller workspace), and the docs plus
`/models` capability metadata carry the literal string `hard-disable unproven`. A test fails
if the weaker word `disabled` is used in place of that literal. The spike records the exact
SDK option and CLI flag inspected with observed values in
`.omc/research/auggie-indexing-spike.md`; "proof unavailable" is valid only when that artifact
shows the controls are absent.

### D6. No hidden execution and no secret leakage (falsifiable)

- A syscall, subprocess, and network-egress spy asserts zero execution after a tool call is
  surfaced. For Auggie, a fixture distinguishes Reverso-initiated execution from Auggie's own
  configured action.
- `DEEPSEEK_API_KEY` and Auggie auth are set to unique sentinels; success and error paths
  assert the sentinels appear in neither the response body nor logs.

## Alternatives considered

- Defer the composition decision again: rejected. The Auggie and DeepSeek adapters cannot be
  exercised end-to-end until the gateway owns the port, so deferring blocks the milestone.
- Serve DeepSeek by keeping the LiteLLM fallthrough and only adding Auggie first-party:
  rejected. It would split DeepSeek behavior across two stacks, leave `response_format` and
  `reasoning_content` stripped by the legacy drop_params, and make the parity matrix
  unverifiable for DeepSeek full modes.
- Add a second port or process for the new providers: rejected. Violates the single-port,
  single-process constraint.

## Consequences

- `src/reverso/proxy/main.py` boot target changes once, to the composition root. Rollback
  is a one-line revert back to `reverso.proxy.app:app` (see the Rollback section below for the
  full path and trigger signal).
- The legacy `ProfileRoutingMiddleware` `/deepseek/v1/...` rewrite is bypassed for the
  first-party DeepSeek path. To keep current profile behavior, the DeepSeek adapter resolves
  GPT-level Codex profile names (for example `gpt-5.5`) to concrete DeepSeek model ids by
  reusing `resolve_profile_model("deepseek", model)`, so existing `model = "gpt-5.5"` profiles
  keep working and the two paths resolve identically. The middleware stays in place (deprecate
  in place) for any route still served by the legacy stack; a `resolve_profile_model`
  regression test covers those.
- The LiteLLM quarantine guard gains a second assertion: the `reverso.proxy.app` wrapper is
  bypassed for first-party prefixes, not only that the inner `litellm.proxy.proxy_server.app`
  symbol is uninvoked.
- DeepSeek JSON output and thinking mode remain `unverified` until their survival tests pass.
- One loopback port still serves all providers; Codex profiles point at provider prefixes.

## Rollback

The composition change (how `ResponsesGatewayApp` reaches port 64946) and the DeepSeek
first-party migration are the two highest-risk reversible points in this increment. Both
revert without any schema or data migration.

### Rollback path

1. Repoint the boot target in `src/reverso/proxy/main.py` from `reverso.proxy.compose:app`
   back to `reverso.proxy.app:app`. This is the one-line revert. uvicorn then boots the legacy
   LiteLLM middleware stack directly on `127.0.0.1:64946`, as it did before this increment.
2. With the legacy stack back in front, `/deepseek/v1/...` and `/claude/v1/...` are handled by
   `ProfileRoutingMiddleware`, which rewrites them onto the LiteLLM `/v1/...` routes. The four
   deprecated-in-place DeepSeek entries in `config/litellm_config.yaml` are still present and
   functional, so DeepSeek falls back onto the legacy LiteLLM path with no further change.
3. `/auggie/v1/...` and `/copilot/v1/...` are net-new in this increment and have no legacy
   route. After rollback they return the legacy stack's normal not-found response; callers
   must stop using those prefixes until the composition root is restored.
4. No state migration is required: the in-memory response store is per-process and ephemeral,
   the Keychain-sourced `DEEPSEEK_API_KEY` is read the same way by both stacks, and no
   on-disk schema is introduced by this increment.

The deprecated-in-place LiteLLM DeepSeek config entries and the unchanged
`ProfileRoutingMiddleware` / `resolve_profile_model` behavior exist specifically to keep this
revert one-line. They must not be deleted while the rollback path is supported (see ADR 0002
D2 retirement criteria and the deprecation banner in `config/litellm_config.yaml`).

### Rollback trigger signal

Roll back if any of the following is observed after the cutover:

- The runtime-scoped LiteLLM quarantine guard fails: `litellm.proxy.proxy_server.app` is
  invoked for a first-party prefix, or the import graph regains a static edge to
  `reverso.proxy.app` from the gateway.
- A `resolve_profile_model` regression: a legacy-fallthrough route resolves a GPT-level name
  to the wrong provider model id, or a first-party `/deepseek` profile that worked before the
  cutover (for example `model = "gpt-5.5"`) stops reaching a valid DeepSeek model id.
- A live DeepSeek behavior change attributable to the first-party migration: previously
  working profiles return upstream model errors, or JSON mode / thinking mode regress versus
  the legacy stack.

## Non-goals

- No new port, listener, process, or provider sidecar.
- No edits to `../oh-my-auggie/`.
- No Claude or Copilot replan beyond topology wording (they remain as defined in ADR 0002).
- No full LiteLLM retirement in this increment (retirement criteria remain in ADR 0002 D2).
- No repository-stored secrets.

## Follow-ups

- Phase 1 spike fills `.omc/research/auggie-sdk-spike.md` (SDK PASS/FAIL/UNKNOWN matrix,
  license, pinned version, subprocess-fallback trigger) and
  `.omc/research/auggie-indexing-spike.md` (indexing controls with observed values).
- Later plan for full LiteLLM retirement once every provider path is first-party and the
  parity suite is green (criteria in ADR 0002 D2).

## Evidence and citations

- First-party gateway: `src/reverso/protocols/responses_app.py:7` (docstring, no legacy
  import), `:42` (`APP_PROVIDER_PREFIXES`), `:57` (`split_provider_path`), `:67` (prefix and
  version guard), `:161` (`models_with_codex_refresh`), `:277-283` (`__init__` allow-list
  guard), `:352` (`build_app`).
- Frozen adapter Protocol: `src/reverso/protocols/adapter.py:125-141` (five methods, no
  `capabilities`).
- Composition gap: `src/reverso/proxy/main.py:92-102` (uvicorn boot of `reverso.proxy.app:app`
  on loopback, `REVERSO_PORT` default 64946), `src/reverso/proxy/app.py:11,22-32` (LiteLLM
  middleware stack), `src/reverso/proxy/profile_routing.py:29,69` (legacy
  `PROVIDER_PREFIXES` and `/<profile>/v1/...` rewrite).
- DeepSeek live bound: `config/litellm_config.yaml:23` (`response_format` in
  `_codex_drop_params`), `:90-116` (DeepSeek entries with
  `additional_drop_params: *codex_drop_params`).
- Subprocess precedent: `src/reverso/protocols/adapters/claude.py:26`.
- Planning and consensus artifacts: `.omc/plans/ralplan-auggie-deepseek-responses.md`,
  `.omc/plans/prd-auggie-deepseek-responses.md`,
  `.omc/plans/test-spec-auggie-deepseek-responses.md`,
  `.omc/state/ralplan/architect-review-auggie-deepseek-responses-20260609-re-review.md`
  (APPROVE), `.omc/state/ralplan/critic-review-auggie-deepseek-responses-20260609-re-review.md`
  (APPROVE).
