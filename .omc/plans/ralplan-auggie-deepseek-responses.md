---
type: ralplan
project: reverso
slug: auggie-deepseek-responses
status: consensus-approved
created_at: 20260608T225050Z
ported_from: .omx/plans/ralplan-auggie-deepseek-responses-20260608T225050Z.md
ported_at: 20260609
reconciled_at: 20260609
consensus_gate:
  complete: true
  approved_at: 20260609
  architect_review:
    pass_1:
      verdict: ITERATE
      report: .omc/state/ralplan/architect-review-auggie-deepseek-responses-20260609.md
      findings: [A-C1 routing-model-drift, A-C2 adapter-surface-mismatch, A-M3 deepseek-non-goal-and-composition-gap]
    pass_2:
      verdict: APPROVE
      report: .omc/state/ralplan/architect-review-auggie-deepseek-responses-20260609-re-review.md
      findings_closed: [A-C1, A-C2, A-M3]
  critic_review:
    pass_1:
      verdict: ITERATE
      report: .omc/state/ralplan/critic-review-auggie-deepseek-responses-20260609.md
      findings: [C1 indexing-unfalsifiable, C2 tool-execution-unfalsifiable, C3 secret-leakage-untested, M1 deepseek-json-drop-params, M2 sdk-claims-asserted, M3 thinking-mode-not-pinned]
    pass_2:
      verdict: APPROVE
      report: .omc/state/ralplan/critic-review-auggie-deepseek-responses-20260609-re-review.md
      findings_closed: [C1, C2, C3, M1, M2, M3]
  note: "Both OMC reviewers returned ITERATE on pass 1 because the ported draft described a ProviderResponsesRouter-before-ProfileRoutingMiddleware topology and an optional capabilities adapter method that do not exist in the merged code. The plan was reconciled against the merged ResponsesGatewayApp/build_app/APP_PROVIDER_PREFIXES gateway and the frozen five-method ProviderAdapter Protocol, and asserted safety/capability claims were converted into falsifiable spike-gated tests. Pass 2 re-review: Architect APPROVE and Critic APPROVE. Gate satisfied; execution may begin docs-first. Two non-blocking executor notes are recorded in the pass_2 reports (LiteLLM guard must bypass the reverso.proxy.app wrapper not only the inner symbol; the Auggie distinguish-execution fixture is the hardest falsifiable test to build)."
---

# RALPLAN: Auggie and DeepSeek Responses Providers (OMC port)

## OMC Consensus Handoff

This plan was produced by OMX ralplan, ported into OMC, then reconciled against the merged Reverso
code after the OMC Architect and Critic passes both returned ITERATE. The consensus evidence is
recorded here honestly.

- planning_artifacts:
  - prd: `.omc/plans/prd-auggie-deepseek-responses.md`
  - test_spec: `.omc/plans/test-spec-auggie-deepseek-responses.md`
  - deep_interview_spec: `.omc/specs/deep-interview-auggie-deepseek-responses.md`
  - context_snapshot: `.omc/context/auggie-deepseek-responses-20260608T223519Z.md`
  - interview_transcript: `.omc/interviews/auggie-deepseek-responses-20260608T224423Z.md`
- ralplan_architect_review:
  - pass_1_verdict: ITERATE
  - pass_1_report: `.omc/state/ralplan/architect-review-auggie-deepseek-responses-20260609.md`
  - pass_1_findings: "A-C1 routing model drift (plan invented ProviderResponsesRouter ordered before ProfileRoutingMiddleware; merged app is ResponsesGatewayApp via build_app + APP_PROVIDER_PREFIXES allow-list + split_provider_path). A-C2 adapter surface mismatch (plan listed optional capabilities; frozen Protocol has exactly five methods). A-M3 DeepSeek touches a non-goal provider and single-port coexistence was unspecified (composition gap: main.py:99 still boots reverso.proxy.app:app)."
  - pass_2_verdict: APPROVE
  - pass_2_report: `.omc/state/ralplan/architect-review-auggie-deepseek-responses-20260609-re-review.md`
  - pass_2_detail: "A-C1, A-C2, A-M3 all CLOSED with file:line evidence. No new critical architectural issue introduced. Composition gap correctly deferred to docs-first Phase 0 with a rollback path."
- ralplan_critic_review:
  - pass_1_verdict: ITERATE
  - pass_1_report: `.omc/state/ralplan/critic-review-auggie-deepseek-responses-20260609.md`
  - pass_1_findings: "C1 Auggie indexing caveat unfalsifiable. C2 no-secret-tool-execution unfalsifiable. C3 no-secret-leakage untested. M1 DeepSeek JSON pass contradicts live config (response_format in _codex_drop_params at config/litellm_config.yaml:23). M2 SDK capability claims asserted as fact. M3 thinking-mode tool-loop field preservation not pinned."
  - pass_2_verdict: APPROVE
  - pass_2_report: `.omc/state/ralplan/critic-review-auggie-deepseek-responses-20260609-re-review.md`
  - pass_2_detail: "C1, C2, C3, M1, M2, M3 all CLOSED; every CRITICAL is now a falsifiable test and both spike-gated MAJORs carry survival tests with explicit promotion gates. Two non-blocking executor notes recorded."
- ralplan_consensus_gate:
  - complete: true
  - reason: "Pass 1 returned ITERATE from both reviewers; this revision closed all findings, and pass 2 returned APPROVE from both Architect and Critic. Gate satisfied. Execution may begin docs-first (Phase 0), honoring the standing constraint that no src/ code is written and no commit/PR is opened without user approval."
- recommended_follow_up:
  - default: `$ultragoal` (docs-first; the first milestone slice is docs + ADR, not code)
  - parallel_delivery_option: `$team` later, only after the docs/composition boundary is fixed and the consensus gate is satisfied
  - ralph_fallback: explicit single-owner sequential fallback only if requested

### Accepted user amendments (carried into execution)
1. All providers are served as endpoint prefixes under one local Reverso port. Provider selection is by endpoint prefix (`/auggie/v1`, `/deepseek/v1`, and the planned `/claude/v1`, `/copilot/v1`), not by separate ports or processes.
2. Auggie indexing is disabled by default as a hard intent. If `auggie-sdk` cannot prove hard-disable behavior, Reverso may still ship the Auggie endpoint with best-effort suppression plus an explicit `hard-disable unproven` caveat in docs and capability metadata.
3. DeepSeek targets full documented modes (text, streaming, JSON output, tool calls, thinking mode, strict tool-call mode) where feasible, using official DeepSeek API docs as primary evidence; tool behavior is provider-native with documented risk boundaries and no hidden Reverso execution.
4. Non-goals hold: no new port or separate provider process, no edits to `oh-my-auggie/`, no Claude/Copilot replan except endpoint topology wording.

> Gate status for OMC execution: SATISFIED. Pass 1 returned ITERATE from both reviewers; this revision closed all findings; pass 2 returned APPROVE from both Architect and Critic (reports under `.omc/state/ralplan/`). Execution may begin docs-first (Phase 0). Standing constraint: no src/ code is written and no commit/PR is opened without user approval.

## Source artifacts
- PRD: `.omc/plans/prd-auggie-deepseek-responses.md`
- Test spec: `.omc/plans/test-spec-auggie-deepseek-responses.md`
- Deep interview spec: `.omc/specs/deep-interview-auggie-deepseek-responses.md`
- Context snapshot: `.omc/context/auggie-deepseek-responses-20260608T223519Z.md`
- Transcript: `.omc/interviews/auggie-deepseek-responses-20260608T224423Z.md`

## Evidence used

### Current-state provenance (merged code as of 20260609)
Reverso is no longer purely LiteLLM-centered. The sibling responses-providers milestone landed a
first-party Responses gateway that this plan must extend, not bypass:
- `src/reverso/protocols/responses_app.py:42`: `APP_PROVIDER_PREFIXES = frozenset({"claude", "copilot"})`; the first-party `ResponsesGatewayApp` is built via `build_app(adapters)` at line 352 and dispatches with `split_provider_path()` at line 57. The module header (line 7) states it deliberately does not import `reverso.proxy.app` (LiteLLM quarantine, ADR 0002 D2).
- `src/reverso/protocols/responses_app.py:278-283`: the app rejects any adapter whose prefix is not in `APP_PROVIDER_PREFIXES`, so adding `auggie`/`deepseek` requires extending that frozenset AND passing the adapters into `build_app`.
- `src/reverso/protocols/responses_app.py:161`: `/models` payloads pass through `models_with_codex_refresh()`; capability metadata (including the Auggie indexing caveat) is surfaced here, not via an adapter method.
- `src/reverso/protocols/adapter.py:125-141`: the `ProviderAdapter` Protocol is frozen with EXACTLY five methods: `create_response`, `stream_response`, `list_models`, `get_response`, `list_input_items`. There is no `capabilities` method.
- `src/reverso/protocols/adapters/claude.py:26,100`: precedent for an adapter shelling to a CLI via `subprocess`; this is the concrete in-repo model for the bounded Auggie child-process carve-out.
- `src/reverso/proxy/main.py:93,99`: the server still boots `reverso.proxy.app:app` (the legacy LiteLLM stack) on `127.0.0.1` and `REVERSO_PORT` default `64946`. The first-party `ResponsesGatewayApp` is a build-time artifact NOT yet wired into `main.py`. This is the single-port composition gap that the docs-first phase must resolve.

### Legacy LiteLLM stack (still live, to be quarantined not extended)
- Reverso `AGENTS.md`: docs-first, same local bind, no secrets, no en or em dash, frontmatter, no deletion.
- `src/reverso/proxy/app.py:1-32`: legacy app wraps LiteLLM and the middleware stack.
- `src/reverso/proxy/profile_routing.py:29`: `PROVIDER_PREFIXES = frozenset({"deepseek", "claude"})`; `ProfileRoutingMiddleware` rewrites `/deepseek/v1/...` to `/v1/...` with `resolve_profile_model` (line 39) alias resolution. DeepSeek is currently LiteLLM-backed through this path.
- `config/litellm_config.yaml:23`: `response_format` is listed in `_codex_drop_params`, applied to all DeepSeek models via `additional_drop_params: *codex_drop_params` (lines 95-116) with global `drop_params: true` (line 120). The live pipeline strips `response_format` before it reaches DeepSeek; this directly bounds the JSON-output mode status (see mode matrix).
- `src/reverso/middleware/codex_models_compat.py`: legacy model refresh compatibility adds Codex metadata while preserving OpenAI model data.
- OpenAI Responses API reference: `POST /v1/responses`, streaming, `previous_response_id`, tools, model response objects.
- Auggie SDK PyPI: Python SDK for Augment CLI, released 2026-03-25, requires Python 3.10+, Augment CLI installed and authenticated, supports sessions, event listeners, function calling, model selection, and mock clients.
- DeepSeek OpenAI SDK guide: OpenAI-compatible patterns for base URL, streaming, JSON output, tool calls, thinking mode, and strict tool-call mode warnings.
- Official DeepSeek function calling and strict mode: https://api-docs.deepseek.com/guides/function_calling/
- Official DeepSeek JSON output: https://api-docs.deepseek.com/guides/json_mode/
- Official DeepSeek thinking mode: https://api-docs.deepseek.com/guides/thinking_mode
- Official DeepSeek chat completion API: https://api-docs.deepseek.com/api/create-chat-completion

## RALPLAN-DR summary

### Principles
1. Same-port provider topology is mandatory for all providers.
2. Docs first, then code, because Reverso docs are the design source of truth.
3. Provider adapters should share a Reverso-owned Responses boundary rather than each owning a server.
4. Preserve local safety: loopback-only bind, no repository secrets, documented tool and indexing risks.
5. Prefer spike-gated adoption for `auggie-sdk` and DeepSeek full modes over optimistic implementation.

### Decision drivers
1. Codex compatibility through provider-prefixed Responses endpoints.
2. Safety around Auggie indexing and provider-native tool behavior.
3. Migration fit with the already-merged first-party Responses gateway (`ResponsesGatewayApp`) and the quarantined legacy LiteLLM stack, plus the already-approved Claude/Copilot same-port topology.

### Viable options

#### Option A: Extend current LiteLLM profile routing only
Add `auggie` to the current prefix middleware and route both providers through existing LiteLLM-compatible mechanisms.

Pros:
- Smallest conceptual diff from current code.
- DeepSeek already has LiteLLM config entries.

Cons:
- Auggie SDK is not a normal OpenAI-compatible HTTP backend.
- Hard to express Auggie event streaming and indexing caveats behind LiteLLM alone.
- Conflicts with the larger Responses-native direction already planned for Claude/Copilot.

#### Option B: Register Auggie and DeepSeek adapters in the merged first-party Responses gateway
Extend the already-merged Reverso-owned `ResponsesGatewayApp` (`src/reverso/protocols/responses_app.py`). Add `auggie` and `deepseek` to `APP_PROVIDER_PREFIXES` and pass their adapters into `build_app(adapters)`, exactly as `claude` and `copilot` already are. Each adapter implements the frozen five-method `ProviderAdapter` Protocol. Legacy LiteLLM behavior remains quarantined for any non-goal paths still served by `reverso.proxy.app:app`.

Pros:
- Matches the user's same-port provider endpoint requirement using the boundary that already exists.
- Reuses the frozen adapter Protocol and `split_provider_path` dispatch; no new routing component is invented.
- Aligns with the approved and merged Claude/Copilot Responses-native gateway.
- Makes Codex fixtures provider-agnostic via the shared adapter contract.

Cons:
- Must resolve the single-port composition gap: `main.py:99` still boots `reverso.proxy.app:app`, so the docs-first phase must decide how `ResponsesGatewayApp` becomes reachable on `64946` (mount, wrap, or replace the boot target) without adding a process.
- Migrating `/deepseek` to first-party touches a non-goal provider; requires a regression contract that preserves legacy `resolve_profile_model` behavior and deprecates-in-place the existing `config/litellm_config.yaml` DeepSeek entries rather than deleting them.
- DeepSeek JSON/thinking modes must escape the legacy `drop_params` stripping that the first-party path must not inherit.

#### Option C: Separate Auggie bridge package or oh-my-auggie integration
Build an Auggie Responses bridge outside Reverso and let Reverso or Codex call it.

Pros:
- Keeps Auggie-specific complexity away from Reverso.
- Could reuse oh-my-auggie domain knowledge.

Cons:
- Violates no new port/process and no oh-my-auggie edits.
- Splits provider topology.
- Makes one-port Codex config harder.

### Preferred option
Option B. It is the only option that satisfies same-port provider endpoints, keeps the work inside Reverso, avoids oh-my-auggie edits, and aligns with the already-merged Claude/Copilot first-party gateway. Critically, Option B now means extending an existing boundary (`ResponsesGatewayApp` + `build_app` + `APP_PROVIDER_PREFIXES`), not designing a new router; the earlier draft's `ProviderResponsesRouter` does not exist and must not be reintroduced.

## Deliberate pre-mortem
1. Auggie indexing guarantee is unprovable.
   - Failure: implementation claims indexing is disabled, but SDK or CLI still indexes a workspace.
   - Prevention: spike SDK and CLI flags first, write tests for any found control, otherwise document caveat and expose capability metadata.
2. DeepSeek full modes create unsafe or incorrect tool loops.
   - Failure: thinking-mode tool calls lose required reasoning fields or Reverso executes unsafe tools.
   - Prevention: provider-native behavior is documented, tests preserve required fields, unsupported loops return explicit errors.
3. Same-port composition regresses existing providers.
   - Failure: wiring `ResponsesGatewayApp` onto port `64946` breaks current LiteLLM-backed DeepSeek/Claude behavior, or Auggie routes silently fall through to LiteLLM.
   - Prevention: resolve the composition gap explicitly in docs-first (mount/wrap/replace decision), runtime-scoped LiteLLM guard test (trace `litellm.proxy.proxy_server.app` for zero invocations on `/auggie` and `/deepseek` first-party paths), `resolve_profile_model` regression assertion for any path still served by LiteLLM, and a `split_provider_path` allow-list test that unknown prefixes are rejected (not silently routed).
4. First-party DeepSeek inherits legacy request mutation.
   - Failure: the first-party DeepSeek adapter reuses the LiteLLM `drop_params` config and silently strips `response_format`/`reasoning_content`, so JSON and thinking modes never reach the provider.
   - Prevention: the first-party adapter calls the DeepSeek API directly and must not apply `_codex_drop_params`; a test asserts `response_format` survives end-to-end on the first-party path.

## DeepSeek official-doc mode matrix
| Mode | Official source | First milestone status | Reverso behavior | Test expectation |
|---|---|---|---|---|
| Text | Chat Completion API | pass | Map DeepSeek chat completion content into Responses output text. | Valid non-streaming Response object. |
| Streaming | Chat Completion API | pass | Normalize DeepSeek data-only SSE stream into Codex-compatible Responses SSE events, preserving terminal completion. | Deltas arrive and terminal event is emitted. |
| JSON output | JSON Output guide and Chat Completion API `response_format` | unverified (spike-gated) | The live LiteLLM config strips `response_format` (`config/litellm_config.yaml:23`); the first-party DeepSeek adapter must NOT inherit that stripping. Preserve or translate `response_format: {"type":"json_object"}`; require prompt/test fixtures to ask for JSON; return explicit error on unsupported Responses shape. | A test proves `response_format` survives end-to-end to the DeepSeek call on the first-party path; valid JSON content or explicit compatibility error. Status promotes to `pass` only when that test is green. |
| Tool calls | Function Calling guide and Chat Completion API tools fields | pass | Return provider tool-call structures through Responses mapping; Reverso must not execute returned tool calls. | Tool call emitted; a syscall/subprocess/network spy asserts ZERO execution after the tool call is surfaced. |
| Thinking mode | Thinking Mode guide and Chat Completion API `reasoning_content` | unverified (spike-gated) | The first-party adapter must NOT inherit `drop_params` stripping of `reasoning_content`. Preserve `reasoning_content` across tool-call turns; otherwise return explicit blocked/unsupported error BEFORE sending an invalid continuation. | A concrete two-turn fixture asserts turn-1 `reasoning_content` is carried into the turn-2 request, OR an explicit rejection is returned before the invalid continuation. Status promotes to `pass`/`blocked` per that fixture. |
| Strict tool-call mode | Function Calling guide and Chat Completion API `strict` field | pass or unsupported | Treat strict mode as JSON schema conformance only, never authorization, sandboxing, or execution safety. | Schema behavior tested; no auth/safety claim. |

## Architecture plan

## Required architectural contracts (reconciled against merged code)
1. Routing via the merged first-party gateway (NOT a new router)
   - Auggie and DeepSeek are added by extending `APP_PROVIDER_PREFIXES` in `src/reverso/protocols/responses_app.py:42` to include `auggie` and `deepseek`, and by passing their adapters into `build_app(adapters)` (line 352). The earlier draft's `ProviderResponsesRouter` ordered before `ProfileRoutingMiddleware` does not exist and must NOT be created.
   - Dispatch is the existing `split_provider_path()` (line 57); unknown prefixes are already rejected by `ResponsesGatewayApp.__init__` (lines 278-283) and by the `provider not in APP_PROVIDER_PREFIXES` guard (line 67). No middleware ordering work is involved.
   - `/auggie/v1/models` and `/deepseek/v1/models` flow through `models_with_codex_refresh()` (line 161) for Codex compatibility, the same as Claude/Copilot.
   - Single-port composition gap (must be resolved in docs-first Phase 0 before any adapter code): `main.py:99` still boots `reverso.proxy.app:app` (the legacy LiteLLM stack). The plan must decide, in docs/ADR, how `ResponsesGatewayApp` becomes the owner of `64946` for first-party prefixes (mount the first-party app, wrap it, or replace the boot target) WITHOUT adding a process or listener. The first-party app must continue NOT to import `reverso.proxy.app`.
   - `/deepseek/v1/responses` is owned by the first-party DeepSeek adapter (which calls the DeepSeek API directly, mirroring how `adapters/claude.py` shells to its CLI), not by LiteLLM fallthrough.
   - Legacy-fallthrough route list (explicit): any path NOT prefixed `/claude`, `/copilot`, `/auggie`, or `/deepseek` remains served by the legacy `reverso.proxy.app:app` stack (e.g. existing `/v1/...` LiteLLM-rewritten paths via `ProfileRoutingMiddleware`). The first-party app rejects unknown provider prefixes; it does not silently forward them.
2. Process boundary
   - The non-goal "no new process" means no new Reverso-managed server process, listener, LaunchAgent, or provider sidecar.
   - Bounded Auggie CLI or SDK child processes are allowed only if required by `auggie-sdk`, and only with lifecycle, timeout, cleanup, and observability requirements. Precedent: `src/reverso/protocols/adapters/claude.py:26,100` already spawns a CLI via `subprocess` inside an adapter; the Auggie child process follows the same bounded model.
   - If later evidence shows a literal no-child-process interpretation is required, Auggie SDK is not viable and the plan must return to ralplan.
3. LiteLLM quarantine (runtime-scoped, falsifiable)
   - First-party provider adapters must not depend on LiteLLM request mutation for Responses semantics, and `responses_app.py` must continue NOT to import `reverso.proxy.app`.
   - LiteLLM may remain only as named legacy fallback for the explicit legacy-fallthrough route list above.
   - A runtime-scoped guard test must trace `litellm.proxy.proxy_server.app` and assert ZERO invocations when driving `/auggie/v1/responses` and `/deepseek/v1/responses`, and assert the import graph for these paths excludes `reverso.proxy.app`.
   - Tests must prove `/deepseek/v1/responses` uses the first-party DeepSeek adapter (not LiteLLM) when Responses or full-mode behavior is requested.
4. Auggie indexing caveat (falsifiable, not an escape hatch)
   - The Phase 1 spike must record, in a named artifact (`.omc/research/auggie-indexing-spike.md`), the EXACT `auggie-sdk` constructor option(s) and Auggie CLI flag(s)/config key(s) inspected, each with its observed value. "Proof unavailable" is valid ONLY when that artifact shows the controls are absent; an implementer may not skip the inspection and claim the caveat.
   - When hard-disable is unproven, docs and the `/auggie/v1/models` capability metadata must contain the literal string `hard-disable unproven`; a test FAILS if the word `disabled` is used instead.
   - When unproven, the default workspace MUST be a no-workspace or sandbox workspace, NEVER the caller's workspace; a test asserts this.
   - Capability metadata is surfaced through the `list_models`/`models_with_codex_refresh` payload or docs, NOT a `capabilities` adapter method (no such method exists; see contract 7).
5. DeepSeek evidence and mode matrix
   - Official DeepSeek API docs are primary evidence for tool calls, thinking mode, and strict mode.
   - Chat-Deep.ai remains secondary compatibility guidance.
   - "Full documented modes" must become a matrix with status `pass`, `unsupported`, or `blocked` for text, streaming, JSON output, tool calls, thinking mode, and strict tool-call mode.
6. Provider-native tool safety contract (falsifiable)
   - DeepSeek returned tool calls must not be executed by Reverso unless a separate explicit executor contract exists. A test spies on subprocess, filesystem-write, and network-egress primitives, drives a response that surfaces a tool call, and asserts ZERO such calls occur after the tool call is surfaced.
   - Auggie may perform provider-native actions according to Augment configuration. The Auggie adapter must use a configuration that does not auto-execute, OR the delegation must be documented with a bounded blast radius. The test must distinguish "Auggie acted per its own config" from "Reverso executed," so a failure means Reverso (not Auggie) initiated an action.
   - Strict mode constrains schema shape only and is not authorization, sandboxing, or safety.
7. Frozen adapter interface (no `capabilities`)
   - The `ProviderAdapter` Protocol (`src/reverso/protocols/adapter.py:125-141`) is frozen with EXACTLY five methods: `create_response`, `stream_response`, `list_models`, `get_response`, `list_input_items`. Auggie and DeepSeek adapters implement these and nothing else.
   - There is no `capabilities` method. Any capability/indexing metadata is carried in the `list_models` payload or docs. If a sixth method is later judged necessary, it requires an explicit frozen-interface amendment gated on a mini-review; it must not be assumed available.
8. Secret non-leakage (falsifiable)
   - Tests set `DEEPSEEK_API_KEY` (and the Auggie auth token) to a unique sentinel value, drive both a success path and an error path, and assert the sentinel appears in NEITHER the response body NOR captured logs, for both providers.

### Phase 0: Docs-first topology and evidence update
1. Update canonical Reverso docs to state that all providers live under one local Reverso port and are selected by endpoint prefix.
2. Add an ADR or architecture section for first-party Responses provider routing with Auggie and DeepSeek.
3. Preserve non-goals: no new port/process, no oh-my-auggie edits, no Claude/Copilot replan except topology wording.
4. Cite official OpenAI Responses API behavior for response objects, streaming, tools, `previous_response_id`, and model response storage.
5. Cite Auggie SDK and DeepSeek guide evidence, including Auggie auth prerequisites and DeepSeek tool/thinking caveats.

### Phase 1: Auggie SDK feasibility spike
All `auggie-sdk` capabilities below are CLAIMED from the PyPI page and are UNVERIFIED until this spike
confirms each with cited evidence against the installed package. The spike produces a written
PASS/FAIL/UNKNOWN capability matrix in `.omc/research/auggie-sdk-spike.md`.
1. Record the exact `auggie-sdk` constructor option(s) and Auggie CLI flag(s)/config key(s) governing indexing, each with observed value, in `.omc/research/auggie-indexing-spike.md`. Hard-disable is `proven` only if a control exists and is observed to suppress indexing; otherwise it is `hard-disable unproven`.
2. Decide default workspace behavior before implementation: when unproven it MUST be no-workspace or sandbox workspace, never the caller workspace.
3. Verify (PASS/FAIL/UNKNOWN with evidence) SDK construction, session behavior, event listener streaming, model listing, function calling, and mock ACP client testability. Do not assert any of these as fact in the implementation phases until the matrix records PASS.
4. Define the caveat path if hard-disable proof is unavailable.
5. Decide whether Auggie implementation should use `Auggie`, `AuggieACPClient`, or a thin subprocess fallback. Fallback trigger: if SDK capabilities record FAIL/UNKNOWN, fall back to the bounded subprocess adapter (per the `adapters/claude.py` precedent).
6. Record the `auggie-sdk` license and the pinned version to be added to dependencies; flag any license incompatibility before adoption.

### Phase 2: Register Auggie and DeepSeek in the merged gateway, resolve composition gap
1. Extend `APP_PROVIDER_PREFIXES` (`responses_app.py:42`) to add `auggie` and `deepseek`; pass their adapters into `build_app(adapters)`. Do NOT create a new router; `split_provider_path` and `ResponsesGatewayApp` already dispatch and reject unknown prefixes.
2. Each adapter implements ONLY the frozen five-method `ProviderAdapter` Protocol (`create_response`, `stream_response`, `list_models`, `get_response`, `list_input_items`). No `capabilities` method.
3. Resolve the single-port composition gap in docs/ADR before adapter code: decide how `ResponsesGatewayApp` owns `64946` for first-party prefixes (mount/wrap/replace `main.py:99`'s boot target) without adding a process, while the legacy LiteLLM app keeps serving the explicit legacy-fallthrough route list.
4. Add the runtime-scoped LiteLLM guard test (zero `litellm.proxy.proxy_server.app` invocations on `/auggie` and `/deepseek` first-party paths; import graph excludes `reverso.proxy.app`) and the `resolve_profile_model` regression assertion for any path still served by LiteLLM.

### Phase 3: Shared Codex Responses contract suite
1. Build shared fixtures for non-streaming text, streaming text, model refresh, previous response continuity, malformed input, and no-secret logging.
2. Run the same fixtures for Auggie and DeepSeek where meaningful.
3. Add provider-specific test matrices for Auggie SDK behavior and DeepSeek full modes.

### Phase 4: Auggie provider implementation plan
1. Implement `/auggie/v1/models` from SDK model listing or a documented static fallback.
2. Implement `/auggie/v1/responses` through the selected SDK or adapter spine.
3. Map Auggie event listener output to Responses SSE where possible.
4. Represent provider-native tool behavior and thoughts as Responses-compatible events or documented metadata.
5. Implement indexing suppression attempt and visible caveat when proof is unavailable.
6. Handle missing Auggie CLI, missing auth, timeout, and SDK parse failures as bounded provider errors.

### Phase 5: DeepSeek provider implementation plan
1. Implement the DeepSeek adapter against the frozen Protocol and register it in the merged `ResponsesGatewayApp` via `build_app`. The adapter calls the DeepSeek API directly and must NOT inherit the legacy `_codex_drop_params`/`drop_params` mutation from `config/litellm_config.yaml`.
2. Map GPT-level Codex models to DeepSeek provider model IDs without breaking current profile behavior; keep the existing `config/litellm_config.yaml` DeepSeek entries (deprecate-in-place, do not delete) for any legacy-fallthrough path.
3. Support text and streaming (status `pass`); JSON output and thinking mode are `unverified` until their survival tests are green (see mode matrix); tool calls and strict mode per the matrix. Official DeepSeek docs are primary evidence, Chat-Deep.ai secondary.
4. Preserve `reasoning_content` and other provider-native fields required for thinking-mode tool loops; reject invalid continuations explicitly before sending them.
5. Return explicit compatibility errors for unsupported DeepSeek mode combinations (no silent downgrade).
6. Read the DeepSeek key from Keychain or environment; the secret-non-leakage sentinel test (contract 8) must pass for both success and error paths.

### Phase 6: Cutover, verification, rollback, and cleanup
1. Update Codex profile docs for Auggie and DeepSeek provider prefixes.
2. Run unit, integration, and e2e suites.
3. Run static safety scans for forbidden dashes and secret patterns.
4. Verify no files under `../oh-my-auggie/` changed.
5. Verify no new port or separate provider process was introduced.
6. Rollback analysis: the composition change (how `ResponsesGatewayApp` reaches `64946`) and the DeepSeek first-party migration are the two highest-risk reversible points. Record a concrete revert path: restore `main.py` to boot `reverso.proxy.app:app` for all prefixes, and keep `/deepseek` on the legacy LiteLLM path (the deprecated-in-place config entries remain functional). The revert must require no schema or data migration. Define the regression signal that triggers rollback (LiteLLM guard test failure, `resolve_profile_model` regression, or live DeepSeek behavior change).

## Expanded test plan

### Unit
- `split_provider_path` accepts `auggie`/`deepseek` after the `APP_PROVIDER_PREFIXES` extension and rejects unknown prefixes.
- `build_app` raises for an adapter whose prefix is not in the allow-list (existing `__init__` guard, lines 278-283).
- Model alias resolution for Auggie and DeepSeek; `resolve_profile_model` regression for legacy paths.
- Responses object normalization.
- SSE event normalization.
- Auggie SDK mock client behavior (only for capabilities the spike matrix records as PASS).
- DeepSeek mode request builders and error translation; assert the first-party builder does not apply `_codex_drop_params`.

### Falsifiable safety tests (close Critic C1/C2/C3)
- Indexing caveat: `/auggie/v1/models` metadata and docs contain the literal `hard-disable unproven` when unproven; test FAILS if `disabled` is used. Default workspace is no-workspace/sandbox, never caller workspace.
- No hidden execution: spy on subprocess, filesystem-write, and network-egress primitives; after a tool call is surfaced, assert ZERO such calls (DeepSeek). For Auggie, assert non-auto-execute config or documented bounded delegation, distinguishing Reverso execution from Auggie's own action.
- No secret leakage: set `DEEPSEEK_API_KEY` and Auggie auth token to unique sentinels; drive success and error paths; assert sentinels appear in neither response body nor captured logs.
- LiteLLM quarantine: runtime trace asserts zero `litellm.proxy.proxy_server.app` invocations on `/auggie` and `/deepseek` first-party paths; import graph excludes `reverso.proxy.app`.
- DeepSeek JSON survival: `response_format` reaches the DeepSeek call end-to-end on the first-party path (promotes JSON status to `pass`).
- Thinking-mode continuity: two-turn fixture carries turn-1 `reasoning_content` into turn-2, OR explicit rejection before invalid continuation.

### Integration
- Same ASGI app handles `/auggie/v1/responses` and `/deepseek/v1/responses`.
- Codex model refresh for `/auggie/v1/models` and `/deepseek/v1/models`.
- Missing auth and missing CLI behavior for Auggie.
- DeepSeek API key missing behavior.
- Previous response/session behavior where Codex fixtures require it.

### E2E
- Codex profile smoke for Auggie if local Auggie auth exists.
- Codex profile smoke for DeepSeek if Keychain/env key exists.
- Streaming smoke for both providers.
- DeepSeek JSON/tool/thinking smoke where safe and available.

### Observability and safety
- Logs redact secrets.
- Tool behavior is documented and observable.
- Auggie indexing capability or caveat appears in docs and tests.
- No new port/process.
- No oh-my-auggie changes.

## Risks and mitigations
| Risk | Mitigation |
|---|---|
| Auggie indexing cannot be truly disabled | Run spike first; if proof is missing, ship only with explicit caveat and best-effort suppression tests. |
| Provider-native tools are unsafe or ambiguous | Document provider-native boundary, avoid hidden Reverso execution, require explicit errors for unsupported tool loops. |
| DeepSeek full modes exceed Codex needs | Keep mode support test-driven and return explicit unsupported errors where full support is not feasible. |
| Single-port composition gap (main.py boots LiteLLM stack) conflicts with first-party gateway | Resolve mount/wrap/replace in docs-first; runtime-scoped LiteLLM guard test; `resolve_profile_model` regression; unknown-prefix rejection test. |
| Existing Claude/Copilot plan is reopened accidentally | Limit changes to topology wording only unless a later execution handoff explicitly expands scope. |

## ADR

### Decision
Plan Auggie and DeepSeek as first-party Reverso provider endpoints under the same local port by extending the already-merged `ResponsesGatewayApp` (`APP_PROVIDER_PREFIXES` + `build_app`) with adapters that implement the frozen five-method `ProviderAdapter` Protocol. Use `auggie-sdk` as the Auggie candidate after a feasibility spike, and target DeepSeek full documented modes where feasible. The single-port composition gap (`main.py:99` boots the legacy LiteLLM stack) is resolved in docs-first before adapter code.

### Drivers
- User requires all providers as endpoint prefixes on one port.
- User selected Auggie and DeepSeek in the first milestone.
- Auggie indexing must be disabled by default in intent.
- DeepSeek should consider the OpenAI SDK compatibility guide and support full modes.
- Reverso already has provider-prefix and Codex model-refresh concepts to preserve.

### Alternatives considered
- Extend current LiteLLM profile routing only: rejected because Auggie SDK is not a simple OpenAI HTTP backend and full Responses semantics need a first-party boundary.
- Separate Auggie bridge or oh-my-auggie integration: rejected by no new port/process and no oh-my-auggie edits.
- DeepSeek text-only parity: rejected because user selected full DeepSeek modes.

### Why chosen
Option B provides a unified provider topology, preserves local safety, and aligns Auggie/DeepSeek with the already-merged Claude/Copilot first-party gateway by reusing its boundary rather than inventing a new router.

### Consequences
- A first execution story must be an Auggie SDK/indexing spike.
- DeepSeek full-mode support must be test-gated and may include explicit compatibility errors.
- Existing LiteLLM code may remain temporarily but must not obscure new provider route ownership.

### Follow-ups
- Decide final Auggie adapter spine after the spike.
- Align Claude/Copilot docs with same-port topology only.
- Later plan full legacy LiteLLM retirement if desired.

## Available agent-types roster
- `planner`: refine milestones and docs-first slices.
- `researcher`: gather official Auggie, DeepSeek, and OpenAI behavior before implementation.
- `dependency-expert`: evaluate `auggie-sdk` adoption risk, version, license, and alternatives.
- `architect`: guard the `ResponsesGatewayApp` composition and adapter boundaries.
- `critic`: enforce risk and testability.
- `executor`: implement docs and code slices after approval.
- `test-engineer`: build shared fixture and provider-specific tests.
- `debugger`: diagnose SDK, auth, streaming, and DeepSeek mode failures.
- `verifier`: validate final evidence against PRD and test spec.
- `code-reviewer`: review final diff before PR.

## Follow-up staffing guidance
- Use `$ultragoal` as the default durable follow-up. Suggested goals: docs/ADR topology and composition-gap resolution, Auggie SDK/indexing spike, gateway adapter registration, shared fixture suite, Auggie adapter, DeepSeek adapter, verification.
- Use `$team` after docs and router design are settled:
  - Lane 1 `researcher` plus `dependency-expert`: Auggie SDK and indexing evidence.
  - Lane 2 `executor`: docs and gateway adapter-registration skeleton (extend `APP_PROVIDER_PREFIXES` + `build_app`).
  - Lane 3 `test-engineer`: shared Responses fixtures and route guards.
  - Lane 4 `executor`: DeepSeek full-mode adapter.
  - Lane 5 `executor`: Auggie adapter after spike decision.
  - Lane 6 `verifier` or `code-reviewer`: final evidence and review.
- Suggested reasoning: architect, critic, verifier, dependency-expert high; executor and test-engineer medium; researcher high for official docs.
- `$ralph` is only an explicit fallback for single-owner sequential completion if requested later.

## Team launch hints (OMC)
These are post-consensus suggestions only. Do not start execution until the OMC consensus gate is satisfied (run an Architect pass and a Critic pass against this ported plan and record their verdicts in the frontmatter).

- After Ultragoal settles the docs/router boundary: `/oh-my-claudecode:team` against `.omc/plans/ralplan-auggie-deepseek-responses.md`.
- Preserve the docs-first gate, same-port topology, Auggie indexing caveat, and provider-native tool safety constraints when launching parallel lanes for Auggie, DeepSeek, and tests.

## Team verification path
Team must return:
- Docs and ADR evidence.
- Auggie SDK/indexing spike evidence.
- Route precedence and same-port test output.
- Shared Codex fixture results for Auggie and DeepSeek.
- DeepSeek full-mode test matrix evidence.
- No-secret, no forbidden dash, and no oh-my-auggie-change checks.

## Goal-Mode Follow-up Suggestions
- Recommended: `/oh-my-claudecode:ultragoal` using this plan (`.omc/plans/ralplan-auggie-deepseek-responses.md`) as the brief, with docs-first as the first goal. Satisfy the consensus gate before any execution.
- Use Team with Ultragoal after the docs/router boundary is fixed if parallel lanes are desired.
- Do not use `$autoresearch-goal`; this is implementation planning with bounded research tasks, not a standalone research deliverable.
- Do not use `$performance-goal`; no performance target is primary.

## Planner changelog
- Initial deliberate consensus draft created from deep-interview spec, Reverso repo inspection, OpenAI Responses docs, Auggie SDK PyPI page, and DeepSeek compatibility guide.
- Applied Architect iteration 1: explicit route precedence, narrowed no-new-process semantics, LiteLLM quarantine tests, fail-safe Auggie indexing caveat, official DeepSeek evidence requirement, and provider-native tool safety contract.
- Applied Architect iteration 2: added official DeepSeek doc URLs and concrete mode matrix covering text, streaming, JSON output, tool calls, thinking mode, and strict tool-call mode.
- Ported OMX -> OMC on 20260609: paths rewritten from `.omx` to `.omc`, OMX CLI/team hints translated to OMC skill invocations (`$ultragoal`, `$team`, `/oh-my-claudecode:team`), and the consensus gate recorded honestly as Architect-iterations-applied / Critic-missing (INCOMPLETE). This port did not add or assert any review verdict that OMX did not produce; an OMC Architect pass and Critic pass are still required before execution.
- OMC consensus pass 1 (20260609): Architect and Critic both returned ITERATE (reports under `.omc/state/ralplan/`). Reconciled the plan against the merged code: replaced the non-existent `ProviderResponsesRouter`/`ProfileRoutingMiddleware`-ordering model with the merged `ResponsesGatewayApp` + `build_app` + `APP_PROVIDER_PREFIXES` reality (A-C1); dropped the non-existent optional `capabilities` adapter method and pinned the frozen five-method `ProviderAdapter` Protocol (A-C2); made `/deepseek` fully first-party, named the legacy-fallthrough route list, surfaced the single-port composition gap at `main.py:99`, and added `resolve_profile_model` + config deprecate-in-place handling (A-M3); converted the Auggie indexing caveat, no-hidden-execution, and no-secret-leakage claims into falsifiable tests (C1/C2/C3); changed DeepSeek JSON and thinking-mode status to spike-gated against the live `drop_params` config and pinned a two-turn thinking-mode fixture (M1/M3); de-asserted SDK capability claims to PASS/FAIL/UNKNOWN spike matrix with subprocess fallback and license/version check (M2); fixed doubled braces; added rollback analysis.
- OMC consensus pass 2 (20260609): Architect APPROVE and Critic APPROVE (re-review reports under `.omc/state/ralplan/`). All pass-1 findings verified CLOSED with file:line evidence. Consensus gate marked complete. Two non-blocking executor notes carried forward: (1) the LiteLLM guard test must assert the `reverso.proxy.app` wrapper is bypassed for first-party prefixes, not only the inner `litellm.proxy.proxy_server.app` symbol; (2) the Auggie distinguish-Reverso-execution-from-Auggie-action fixture is the hardest falsifiable test and must produce an observably distinct Reverso-initiated action.
