---
type: ralplan
project: reverso
slug: responses-providers
status: consensus-complete
created_at: 20260608T221431Z
ported_from: .omx/plans/ralplan-responses-providers-20260608T221431Z.md
ported_at: 20260609
consensus_gate:
  complete: true
  architect_review: APPROVE
  critic_review: APPROVE
  critic_review_iterate_path: .omc/state/ralplan/critic-review-responses-providers-20260609.md
  critic_review_approve_path: .omc/state/ralplan/critic-review-responses-providers-20260609-followup.md
  resolved_blockers: ["C1: closed (falsifiable subscription-OAuth gate + ADR names credential artifact)", "C2: closed (SDK evaluation matrix deliverable + operational fallback trigger + de-asserted SDK claims)", "M1: closed (runtime-scoped LiteLLM guard + import independence)", "M2: closed (drop ported token log + wildcard CORS)", "M3: closed (citation fixed to copilot-openai-api/main.py:39-261)", "m1: closed (robust dash-check form)"]
  note: "OMX recorded Architect APPROVE only. First OMC Critic pass on 20260609 returned ITERATE (C1, C2 CRITICAL; M1-M3 MAJOR). Artifacts were revised the same day to close all findings and add the single-port per-provider endpoint requirement. Follow-up OMC Critic pass on 20260609 returned APPROVE with per-finding CLOSED evidence verified against live code. Consensus gate complete: Architect APPROVE (order 1), Critic ITERATE then APPROVE (order 2). Execution-ready. Amendment 3 still keeps the first deliverable docs+ADR only."
---

# RALPLAN: Responses-Native Reverso Providers (OMC port)

## OMC Consensus Handoff (ported from OMX)

This plan was produced by OMX ralplan and ported into OMC. The consensus evidence is recorded here honestly; it is incomplete.

- planning_artifacts:
  - prd: `.omc/plans/prd-responses-providers.md`
  - test_spec: `.omc/plans/test-spec-responses-providers.md`
  - deep_interview_spec: `.omc/specs/deep-interview-responses-providers.md`
  - context_snapshot: `.omc/context/responses-providers-20260608T214418Z.md`
  - interview_transcript: `.omc/interviews/responses-providers-20260608T215316Z.md`
- ralplan_architect_review:
  - path: `.omc/state/ralplan/architect-review-responses-providers-20260609-re-review.md`
  - verdict: APPROVE
  - order: 1
- ralplan_critic_review:
  - iterate_path: `.omc/state/ralplan/critic-review-responses-providers-20260609.md`
  - approve_path: `.omc/state/ralplan/critic-review-responses-providers-20260609-followup.md`
  - verdict: APPROVE (follow-up OMC Critic pass, 20260609; first pass was ITERATE)
  - order: 2
  - resolved: "C1, C2, M1, M2, M3, m1 all CLOSED with evidence verified against live code; single-port per-provider endpoint requirement CLOSED and propagated across plan/PRD/test-spec."
- ralplan_consensus_gate:
  - complete: true
  - reason: "Architect APPROVE (order 1) then Critic ITERATE -> APPROVE (order 2). Gate satisfied. Execution-ready. Amendment 3 still keeps the first deliverable docs+ADR only."
- recommended_follow_up:
  - default: `$ultragoal` (docs-first; first milestone is docs + ADR, not code)
  - parallel_delivery_option: `$team` later, only after docs/core boundary is fixed (lanes: Claude, Copilot, tests)
  - ralph_fallback: explicit single-owner fallback only if requested

### Accepted user amendments (carried into execution)
1. Claude Code auth must be OAuth-backed or demonstrably backed by Claude Code subscription OAuth login. CLI-auth only counts if proven to preserve that OAuth/subscription behavior.
2. Evaluate the GitHub Copilot Python SDK before choosing the Copilot adapter. Criteria: OAuth/logged-in-user support, Responses SSE mapping, model listing, tool/session support, local credential handling, package maturity/licensing, and a defined fallback trigger if the SDK cannot meet Codex fixture parity.
3. Keep docs-first. Do not start implementation until docs and ADR define the first-milestone boundary.

> Gate status for OMC execution: SATISFIED. Architect APPROVE (order 1) and Critic APPROVE (order 2, after one ITERATE round whose findings were closed) are both recorded. Execution may proceed. Amendment 3 still keeps the first deliverable docs+ADR only.

## Source artifacts
- PRD: `.omc/plans/prd-responses-providers.md`
- Test spec: `.omc/plans/test-spec-responses-providers.md`
- Deep interview spec: `.omc/specs/deep-interview-responses-providers.md`
- Context snapshot: `.omc/context/responses-providers-20260608T214418Z.md`

## RALPLAN-DR summary

### Principles
1. Docs first, then code, because current docs still define a LiteLLM-centered architecture.
2. Keep a stable Reverso-owned Responses boundary so provider internals can evolve independently.
3. Prove Claude and Copilot with the same Codex-observed parity suite.
4. Preserve local safety: loopback bind only, no secrets in repo, OAuth supported for Claude Code and Copilot, and bounded auth failure behavior.
5. Prefer selective porting and deletion over blind vendoring or parallel duplicated routers.

### Decision drivers
1. Codex compatibility through `/v1/responses`, streaming, model refresh, and observed state/tool behavior.
2. Migration safety from current LiteLLM wrapper code to first-party ASGI without breaking unrelated providers.
3. Provider feasibility for both Claude and Copilot in the same first deliverable, including OAuth-backed local authentication.

### Viable options

#### Option A: Big-bang replacement
Replace the existing LiteLLM app entrypoint with a first-party ASGI app in one delivery pass.

Pros:
- Fastest path to the stated end architecture.
- Avoids keeping two router stacks alive.

Cons:
- Highest regression risk for current DeepSeek and middleware behavior.
- Hard to prove both providers while also moving every existing route.
- Conflicts with first-milestone non-goal of no DeepSeek migration.

#### Option B: First-party Responses app with compatibility shell
Introduce a Reverso-owned ASGI app and route Claude and Copilot Responses paths through it first. Keep legacy LiteLLM behavior behind an internal compatibility mount or clearly isolated legacy module until non-goal providers are intentionally migrated later.

Pros:
- Satisfies replacing LiteLLM as core for the new provider paths while avoiding DeepSeek migration.
- Lets existing Responses middleware be moved or adapted behind a stable app boundary.
- Supports docs-first migration and parity tests incrementally.

Cons:
- Requires careful route precedence and docs language so LiteLLM does not remain the effective core for the new milestone.
- Temporarily keeps some legacy code alive.

#### Option C: Wrapper facade
Run `claude-code-openai-wrapper` and `copilot-openai-api` as external services behind Reverso.

Pros:
- Least initial porting work.
- Fast proof of provider reachability.

Cons:
- Violates selected reuse boundary, which was to port useful modules, not facade or vendor wrappers.
- Makes Reverso less self-contained and harder to test with `uv`.
- Does not create a unified provider adapter layer.

### Preferred option
Option B. It is the only option that satisfies the docs-first replacement intent while respecting first-milestone non-goals and reducing migration risk.

## Architecture plan

### Phase 0: Docs-first architecture update
All new docs and the ADR must carry the required markdown frontmatter (AGENTS.md hard rule) and stay ASCII-only (no em or en dash characters).
1. Add an ADR under `docs/architecture/adr/` or update `docs/03-architecture.md` to declare the Responses-native gateway decision.
2. Update `docs/04-mvp.md` with the first milestone boundary: Claude and Copilot complete, no Codex provider, no DeepSeek migration, no launchd productionization.
3. Define milestone-one meaning of LiteLLM replacement: first-party ownership of Claude and Copilot Responses routes, legacy LiteLLM quarantine for non-goal providers, and full retirement criteria for a later milestone.
4. Define the single-port endpoint model in docs: one loopback port serves all providers as path-prefixed Responses endpoints (`/claude/v1/...`, `/copilot/v1/...`); one Codex profile sets `base_url` to one provider prefix. No per-provider port.
5. Cite official OpenAI Responses API evidence for Response objects, streaming events, response retrieval, cancellation, input items, tools, and `previous_response_id` before coding.
6. C1 deliverable: the ADR names the exact credential artifact (file path, environment variable, or SDK call) that constitutes Claude subscription OAuth, with cited evidence that it bills the subscription rather than metered API. The ADR must also state whether `claude-agent-sdk`/CLI auth exposes an observable signal distinguishing subscription OAuth from `ANTHROPIC_API_KEY`; if it does not, document the limitation and the chosen mitigation. Until this artifact is named, the Claude OAuth gate is unfalsifiable and stays open.
7. C2 deliverable: produce a written Copilot SDK evaluation matrix scoring the seven amendment-2 criteria (OAuth/logged-in-user support, Responses SSE mapping, model listing, tool/session support, local credential handling, package maturity/licensing, Codex fixture parity) with PASS/FAIL/UNKNOWN and cited evidence (real package, version, license, specific source or README lines). Do not assert the SDK API as fact; verify it. Candidate source to verify: `https://github.com/github/copilot-sdk/tree/main/python`.
8. Update README only for planned architecture notes at this stage, not operational instructions that are not yet true.

### Phase 1: Provider-neutral Responses core
1. Create a first-party ASGI app boundary under `src/reverso/protocols/responses_app.py` (do not add a new top-level `src/reverso/app.py`, since `src/reverso/proxy/app.py` already exists). The new app module must NOT import `reverso.proxy.app`; route both providers under one app on one loopback port with path prefixes `/claude` and `/copilot`.
2. Extract reusable middleware behavior from `src/reverso/middleware/codex_responses_normalizer.py`, `responses_sse_completion.py`, `responses_think_stripper.py`, and `codex_models_compat.py` into app-owned protocol modules.
3. Define provider adapter interfaces:
   - `create_response(request) -> ResponseEnvelope`
   - `stream_response(request) -> AsyncIterator[SSEEvent]`
   - `list_models() -> ModelList`
   - `get_response(response_id)` and `list_input_items(response_id)` where required by Codex-observed fixtures.
4. Define a response/session store abstraction for Codex-observed `previous_response_id` behavior. Keep it in memory for the first milestone unless docs explicitly approve persistence.
5. Define provider auth interfaces for local OAuth or CLI-auth backed credentials with secret redaction, no repository secrets, and deterministic fake-auth tests.

### Phase 2: Shared Codex-observed parity suite
1. Capture safe Codex request/response examples from local logs or synthetic minimized fixtures.
2. Build shared fixtures under `tests/fixtures/responses/`.
3. Build a provider-agnostic test harness under `tests/integration/test_responses_provider_contract.py`.
4. Run the same fixture matrix for `claude` and `copilot` adapters.

### Phase 3: Claude adapter
1. Port selected ideas from `../claude-code-openai-wrapper`: auth diagnostics, message/session adaptation, streaming response generation, parameter validation, and model listing.
2. Decide implementation spine after docs phase:
   - Use `claude-agent-sdk` only if docs and tests prove it still satisfies the subscription-backed Claude Code OAuth or CLI-auth subscription behavior required by Reverso.
   - Otherwise default to preserving the current Claude Code CLI subprocess approach but expose it through the provider adapter interface.
3. Treat Claude Code OAuth or CLI-auth support as a hard acceptance gate with a falsifiable test: with no `ANTHROPIC_API_KEY` set and a subscription OAuth credential present, the adapter authenticates and serves a request, AND the resolved auth path is the subscription OAuth credential. The test fails if `auth_method` resolves to `anthropic`/API-key auth or if `ANTHROPIC_API_KEY` is consumed. Note: the only available CLI-auth reference, `../claude-code-openai-wrapper/src/auth.py:172-181`, "assumes valid and lets the SDK handle auth" and `auth.py:34-71` auto-selects API-key auth when `ANTHROPIC_API_KEY` is present, so a naive "auth succeeded" check would pass for metered API keys and defeat amendment 1. Also test missing auth, expired auth where observable, and secret redaction. If no observable signal distinguishes subscription OAuth from `ANTHROPIC_API_KEY`, the test is skipped against the documented ADR limitation rather than passed.
4. Map Claude output into Responses object and SSE events.
5. Enforce no secret printing and bounded auth failure responses.

### Phase 4: Copilot adapter
1. Evaluate the candidate Copilot Python SDK as the preferred adapter spine using the Phase 0 evaluation matrix. It is claimed to expose an async `CopilotClient`, session lifecycle, streaming events, tool hooks, `github_token`, `use_logged_in_user`, runtime connections, and model listing; these claims are unverified until the matrix confirms them with cited evidence. Candidate source to verify: `https://github.com/github/copilot-sdk/tree/main/python`. Apply the operational fallback trigger: fall back to the ported `copilot-openai-api` token-refresh and direct-forward adapter if ANY of (a) the SDK fails the shared Codex parity fixture matrix in a spike, (b) it requires a repository-stored secret, (c) its license is incompatible with personal/local use, or (d) `use_logged_in_user` cannot authenticate without a metered API key.
2. Port selected auth/token refresh behavior from the full `CopilotAuth` class at `../copilot-openai-api/main.py:39-261` (init, OAuth token read, refresh, lock/stale-lock handling, file watcher) and proxy behavior from `../copilot-openai-api/main.py:295-374` only where the SDK does not cover Reverso needs. When porting, DO NOT carry over the access-token log line at `../copilot-openai-api/main.py:276` and DO NOT carry over wildcard CORS (`allow_origins=["*"]` with `allow_credentials=True`, `main.py:284-292`); loopback-only bind makes broad CORS unnecessary and AGENTS.md forbids secret printing.
3. Rework Copilot OAuth into Reverso modules with safe token file reads or verified SDK logged-in-user support, lock handling, timeout handling, and no repository secrets.
4. Forward, adapt, or SDK-drive Copilot responses into Reverso Responses objects and SSE events.
5. Normalize model list output using the existing Codex model compatibility requirements.

### Phase 5: Cutover for first milestone paths
1. Wire `/claude/v1/responses` and `/copilot/v1/responses` to the first-party app, both served on the single loopback port with their path prefixes. `copilot` is net-new: the legacy `src/reverso/proxy/profile_routing.py:29` `PROVIDER_PREFIXES` lists only `{deepseek, claude}`; add the `copilot` prefix in the first-party app, not by mutating the legacy middleware.
2. Keep non-goal paths, including DeepSeek and legacy behavior, isolated and documented as legacy or out of scope.
3. Ensure route precedence proves LiteLLM is not the core for Claude and Copilot Responses paths.
4. Add a runtime-scoped guard test: monkeypatch or trace `litellm.proxy.proxy_server.app` and assert zero invocations during Claude and Copilot `/v1/responses` handling, AND assert the first-party app module's import graph excludes `reverso.proxy.app`. An import-level-only assertion is insufficient because legacy modules may coexist in-process.
5. Rollback: if the parity suite fails at cutover, revert the route wiring while the first-party app is fixed. Claude reverts to its prior LiteLLM-backed path; Copilot has no prior path, so rollback for Copilot means disabling its route and returning a bounded unavailable error rather than falling back. Blast radius is low because the first milestone ships docs+ADR before any cutover and non-goal providers are untouched.

### Phase 6: Verification and cleanup
1. Run unit, integration, and e2e parity tests.
2. Run smoke tests after service startup exists.
3. Scan touched text for em and en dash characters.
4. Remove dead LiteLLM-only code only when it is outside non-goal providers or explicitly documented as replaced.

## Acceptance criteria
See `.omc/plans/prd-responses-providers.md` and `.omc/plans/test-spec-responses-providers.md`. The short form is:
- Docs updated first.
- First-party app owns Claude and Copilot `/v1/responses` paths.
- Both providers pass the same Codex-observed parity suite.
- No first-milestone DeepSeek, Codex provider, or launchd productionization work.
- No secrets in repo or logs.

## Risks and mitigations
| Risk | Mitigation |
|---|---|
| Codex-observed parity is under-specified | Capture fixtures before adapter implementation and make fixture updates explicit. |
| Copilot auth is brittle | Prefer the SDK only if the Phase 0 matrix passes; otherwise isolate token refresh into a small tested module with fake token fixtures and timeout tests. |
| Copilot SDK is unreal or its API differs from claims | Phase 0 evaluation matrix verifies package, version, license, and each capability with cited evidence before any adapter choice; operational fallback trigger routes to the ported direct-forward adapter if verification fails. |
| API key masquerades as subscription OAuth | Falsifiable Claude auth test: with no `ANTHROPIC_API_KEY` and an OAuth credential, assert the resolved auth path is OAuth, not API-key; ADR names the credential artifact and any signal limitation. |
| OAuth support is treated as a later enhancement | Make Claude Code OAuth or CLI-auth and Copilot OAuth success paths hard acceptance criteria in docs, unit tests, and integration tests. |
| Claude SDK versus CLI choice causes churn | Keep provider adapter interface stable and defer internal spine choice to docs-first ADR. |
| LiteLLM remains hidden core | Runtime-scoped guard: trace `litellm.proxy.proxy_server.app` for zero invocations during Claude/Copilot handling AND assert the new app's import graph excludes `reverso.proxy.app`. |
| Ported Copilot code leaks the bearer token or opens wildcard CORS | Drop the `main.py:276` token log and the `main.py:284-292` wildcard CORS when porting; test-spec item 7 asserts no token substrings in logs and no wildcard CORS with credentials. |
| Response state leaks sensitive content | Use in-memory response store, bounded retention, no logs of full prompts by default. |

## ADR

### Decision
Use a first-party Reverso ASGI Responses gateway for Claude and Copilot provider paths, with a provider adapter interface and shared Codex-observed parity tests. Serve all providers from one loopback port as path-prefixed Responses endpoints (`/claude/v1/...`, `/copilot/v1/...`), one Codex profile per provider prefix. Keep legacy LiteLLM behavior isolated for non-goal providers until a later migration. The ADR must name the Claude subscription-OAuth credential artifact and record the Copilot SDK evaluation outcome before adapter implementation.

### Drivers
- Codex needs a reliable `/v1/responses` surface.
- The user selected LiteLLM core replacement.
- The user clarified that OAuth support is very important for Claude Code and Copilot.
- DeepSeek and launchd migration are explicitly out of scope.
- Both Claude and Copilot must complete together.
- The official Copilot Python SDK is a current candidate for the Copilot adapter and must be evaluated before direct-forwarding code is ported.

### Alternatives considered
- Big-bang replacement: rejected for migration risk and DeepSeek non-goal conflict.
- External wrapper facade: rejected because the selected reuse boundary is port useful modules, not facade wrappers.
- Keep LiteLLM core and add custom providers: rejected because it conflicts with the replacement intent.

### Why chosen
Option B provides the clean target architecture while preserving migration safety and giving both provider adapters a shared contract.

### Consequences
- Docs must be updated before code.
- Some legacy LiteLLM code may remain temporarily, but it must not own the new Claude and Copilot Responses paths.
- The parity fixture suite becomes the milestone gate.
- OAuth-backed success and failure behavior becomes part of the milestone gate for both providers.

### Follow-ups
- Later plan for DeepSeek migration.
- Later plan for launchd productionization.
- Later plan for Codex CLI provider if still desired.

## Available agent-types roster
- `planner`: refine docs and phased implementation tasks.
- `architect`: guard adapter boundaries and migration safety.
- `critic`: enforce option consistency, risks, and testability.
- `executor`: implement docs and provider slices after approval.
- `test-engineer`: build shared parity fixtures and e2e harnesses.
- `debugger`: diagnose provider-specific auth, streaming, and state failures.
- `verifier`: validate final evidence against PRD/test spec.
- `code-reviewer`: review final diff before PR.

## Follow-up staffing guidance
- Use `$ultragoal` as the default durable follow-up. Suggested goals: docs-first update, Responses core, shared parity suite, Claude adapter, Copilot adapter, cutover verification.
- Use `$team` inside Ultragoal after docs/core are settled if parallel work is desired:
  - Lane 1 `executor`: docs and app skeleton.
  - Lane 2 `test-engineer`: parity fixtures and harness.
  - Lane 3 `executor`: Claude adapter.
  - Lane 4 `executor`: Copilot adapter.
  - Lane 5 `verifier` or `code-reviewer`: integration evidence and review.
- Suggested reasoning: architect, critic, verifier high; executor and test-engineer medium; explore/debugger as needed.
- `$ralph` is only an explicit fallback for single-owner sequential completion if requested later.

## Team launch hints (OMC)
- After Ultragoal settles docs/core boundary: `/oh-my-claudecode:team` against `.omc/plans/ralplan-responses-providers.md`.
- Preserve the docs-first gate and provider parity constraints when launching parallel lanes for Claude, Copilot, and tests.

## Team verification path
Team must return:
- Docs update evidence.
- Parity fixture matrix results for Claude and Copilot.
- Provider-specific unit and integration test output.
- Smoke output for local service when available.
- Cleanup evidence proving no temporary harness debris and no unrelated worktree changes.

## Goal-Mode Follow-up Suggestions
- Recommended: `/oh-my-claudecode:ultragoal` using this plan (`.omc/plans/ralplan-responses-providers.md`) as the brief, with docs-first as the first goal.
- Use Team with Ultragoal if parallel lanes are desired after docs/core boundary is fixed.
- Do not use `$autoresearch-goal`; this is an implementation architecture project, not a research deliverable.
- Do not use `$performance-goal`; no performance target is primary.

## Planner changelog
- Initial consensus draft created from deep-interview spec and repo inspection.
- Applied Architect refinements: legacy quarantine definition, official Responses evidence step, no LiteLLM import/call tests, Claude subscription-backed constraint, concrete model endpoints, and no-secret log assertions.
- Applied user clarification: OAuth is a hard requirement for Claude Code and Copilot, and the official GitHub Copilot Python SDK must be evaluated for the Copilot adapter.
- Ported OMX -> OMC on 20260609: paths rewritten to `.omc`, OMX CLI hints translated to OMC skill invocations, consensus gate recorded as Architect-APPROVE / Critic-MISSING (incomplete).
- Ran first OMC Critic pass on 20260609 (verdict ITERATE) and iterated artifacts the same day to close C1 (falsifiable Claude subscription-OAuth gate + ADR names credential artifact), close C2 (Copilot SDK evaluation matrix deliverable + operational fallback trigger + de-asserted SDK claims), fold M1 (runtime-scoped LiteLLM guard + import independence), M2 (drop ported token log + wildcard CORS), M3 (citation fixed to `copilot-openai-api/main.py:39-261`), and m1 (dash-check uses `rg -nP '[\x{2013}\x{2014}]'`). Added the user single-port per-provider endpoint requirement.
- Follow-up OMC Critic pass on 20260609 returned APPROVE with per-finding CLOSED evidence verified against live code. Consensus gate marked complete; plan is execution-ready under the docs-first amendment.
