---
type: prd
project: reverso
slug: auggie-deepseek-responses
status: draft
ported_from: .omx/plans/prd-auggie-deepseek-responses-20260608T225050Z.md
ported_at: 20260609
---

# PRD: Auggie and DeepSeek Responses Providers

## Source artifacts
- Deep interview spec: `.omc/specs/deep-interview-auggie-deepseek-responses.md`
- Context snapshot: `.omc/context/auggie-deepseek-responses-20260608T223519Z.md`
- Transcript: `.omc/interviews/auggie-deepseek-responses-20260608T224423Z.md`
- OpenAI Responses API reference: https://platform.openai.com/docs/api-reference/responses
- Auggie SDK reference: https://pypi.org/project/auggie-sdk/
- DeepSeek OpenAI SDK guide: https://chat-deep.ai/docs/openai-sdk-to-deepseek/
- Official DeepSeek function calling and strict mode: https://api-docs.deepseek.com/guides/function_calling/
- Official DeepSeek JSON output: https://api-docs.deepseek.com/guides/json_mode/
- Official DeepSeek thinking mode: https://api-docs.deepseek.com/guides/thinking_mode
- Official DeepSeek chat completion API: https://api-docs.deepseek.com/api/create-chat-completion

## Requirements summary
Reverso must expose Auggie and DeepSeek as provider-prefixed endpoints on the same local server port used by existing and planned provider routes. The milestone must add `/auggie/v1/responses` and `/deepseek/v1/responses` as Codex-compatible OpenAI Responses API surfaces without adding a new port or separate provider server process.

Current-state provenance (merged code as of 20260609): Reverso is no longer purely LiteLLM-centered. A first-party Responses gateway already exists at `src/reverso/protocols/responses_app.py` (`ResponsesGatewayApp` built via `build_app(adapters)` at line 352, dispatching with `split_provider_path` against `APP_PROVIDER_PREFIXES = frozenset({"claude", "copilot"})` at line 42, and deliberately NOT importing `reverso.proxy.app`). Provider adapters implement the frozen five-method `ProviderAdapter` Protocol at `src/reverso/protocols/adapter.py:125-141`. The legacy LiteLLM stack remains live: `src/reverso/proxy/app.py:1-32` wraps `litellm.proxy.proxy_server.app`, `src/reverso/proxy/main.py:93,99` still boots `reverso.proxy.app:app` on `127.0.0.1:64946` (the single-port composition gap), and `src/reverso/proxy/profile_routing.py:29` recognizes `deepseek` and `claude` prefixes, rewriting them to `/v1/...` via `resolve_profile_model`. This plan extends the first-party gateway with `auggie` and `deepseek` adapters while avoiding source implementation during ralplan.

## User value
- Codex can reach Auggie and DeepSeek through stable Reverso provider endpoints.
- Provider selection is URL-prefix based, not port or process based.
- DeepSeek gets a fuller documented Responses-compatible surface than text-only forwarding.
- Auggie becomes available through `auggie-sdk` while keeping indexing disabled by default in intent and documented if proof is incomplete.

## In scope
- Docs-first Reverso architecture update for same-port provider endpoint topology.
- Reverso provider prefix model for Auggie and DeepSeek.
- `/auggie/v1/responses`, `/auggie/v1/models`, `/deepseek/v1/responses`, and `/deepseek/v1/models` planning.
- Auggie SDK feasibility spike for auth, sessions, streaming/event listeners, model listing, function calling, and indexing suppression controls.
- DeepSeek support planning for text, streaming, JSON output, tool calls, thinking mode, and strict tool-call mode where feasible.
- Provider-native tool behavior with documented Reverso risk boundaries.
- Codex-observed fixture harness reused across provider endpoints where possible.

## Out of scope
- No new port or separate provider server process.
- No edits to `oh-my-auggie/`.
- No Claude Code or Copilot replan, except aligning docs with same-port provider endpoint topology.
- No source implementation during ralplan.
- No repository secrets.
- No blind dependency adoption before an Auggie SDK feasibility spike.

## Functional requirements
1. Same-port provider topology
   - All providers are served under the existing Reverso local port.
   - Provider prefixes are explicit and testable.
   - Existing planned Claude and Copilot topology is aligned in docs only.
2. Auggie provider
   - Primary candidate is `auggie-sdk`.
   - The plan must verify required local prerequisites: Augment CLI installed and authenticated with `auggie login`.
   - SDK capabilities needed for Responses (sessions, model listing, event streaming, function calling, mocks) are CLAIMED from the PyPI page and UNVERIFIED until the Phase 1 spike records each as PASS/FAIL/UNKNOWN with cited evidence; a subprocess fallback applies if any record FAIL/UNKNOWN.
   - Adapters implement ONLY the frozen five-method `ProviderAdapter` Protocol; there is no `capabilities` method. Indexing/capability metadata is surfaced via `list_models`/`models_with_codex_refresh` or docs.
   - Indexing must be disabled by default in intent. "Proof unavailable" is valid only when `.omc/research/auggie-indexing-spike.md` records the exact SDK option and CLI flag inspected with observed values showing the controls are absent. When unproven, ship only with best-effort suppression, the literal `hard-disable unproven` caveat in docs and `/auggie/v1/models` metadata (test FAILS if `disabled`), and a no-workspace/sandbox default (never the caller workspace).
3. DeepSeek provider
   - DeepSeek endpoint supports Responses-compatible text and streaming.
   - It plans JSON output, tool calls, thinking mode, and strict mode where feasible from DeepSeek OpenAI-compatible guidance.
   - Provider-native tool behavior is allowed, but Reverso docs and tests must define risk boundaries.
4. Codex compatibility
   - Model refresh responses include Codex-compatible fields currently handled by `CodexModelsCompatMiddleware` for `/models` paths.
   - Responses behavior covers non-streaming, streaming, `previous_response_id` where Codex requires it, malformed input, auth/config failures, and no-secret leakage.
5. Safety and operations
   - Bind remains `127.0.0.1` only.
   - Secrets stay outside the repository.
   - No new LaunchAgent or productionization is required for this milestone.

## Architectural contracts required before implementation
1. Routing via the merged first-party gateway
   - Auggie and DeepSeek are registered by extending `APP_PROVIDER_PREFIXES` (`responses_app.py:42`) and passing adapters into `build_app`. The earlier draft's `ProviderResponsesRouter` ordered before `ProfileRoutingMiddleware` does not exist in the merged code and must NOT be created.
   - `split_provider_path` (line 57) and the `ResponsesGatewayApp.__init__` allow-list guard (lines 278-283) already dispatch and reject unknown prefixes; no middleware-ordering work is involved.
   - `/deepseek/v1/responses` is owned by the first-party DeepSeek adapter (calling the DeepSeek API directly), not LiteLLM fallthrough, when Responses or full-mode behavior is requested.
   - Single-port composition gap: `main.py:99` still boots `reverso.proxy.app:app`. Docs-first must decide how `ResponsesGatewayApp` owns `64946` for first-party prefixes (mount/wrap/replace) without adding a process.
   - Legacy-fallthrough route list (named before implementation): any path NOT prefixed `/claude`, `/copilot`, `/auggie`, `/deepseek` stays on the legacy `reverso.proxy.app:app` stack; unknown provider prefixes are rejected, not silently forwarded.
2. Process boundary
   - "No new process" means no new Reverso-managed server process, listener, LaunchAgent, or provider sidecar.
   - Bounded Auggie CLI or SDK child processes are allowed only if required by `auggie-sdk`, and only with lifecycle, timeout, cleanup, and observability requirements.
3. LiteLLM quarantine
   - First-party adapters must not depend on LiteLLM request mutation for Responses semantics.
   - LiteLLM may remain only as a named legacy fallback.
4. Auggie indexing caveat
   - If hard-disable proof is unavailable, docs and capability metadata must say `hard-disable unproven`, not `disabled`.
   - The spike must inspect both SDK constructor options and Auggie CLI flags/config.
   - Default workspace behavior must be explicit before implementation.
5. DeepSeek evidence
   - Official DeepSeek API docs must be primary evidence for tool calls, thinking mode, and strict mode.
   - Chat-Deep.ai is secondary compatibility guidance.
   - Full documented modes must be represented as a pass, unsupported, or blocked matrix.
6. Tool safety (falsifiable)
   - DeepSeek returned tool calls must not be executed by Reverso unless a separate explicit executor contract exists. A test spies on subprocess, filesystem-write, and network-egress primitives and asserts ZERO such calls after a tool call is surfaced.
   - Auggie may perform provider-native actions according to Augment configuration; the adapter must use a non-auto-execute config OR document the delegation with a bounded blast radius. The test distinguishes "Auggie acted per its own config" from "Reverso executed."
   - Strict mode constrains schema shape only and is not authorization, sandboxing, or safety.
7. Secret non-leakage (falsifiable)
   - Tests set `DEEPSEEK_API_KEY` and the Auggie auth token to unique sentinels, drive success and error paths, and assert the sentinels appear in neither response bodies nor captured logs.
8. LiteLLM quarantine (falsifiable)
   - A runtime-scoped guard test asserts zero `litellm.proxy.proxy_server.app` invocations on `/auggie` and `/deepseek` first-party paths, and that their import graph excludes `reverso.proxy.app`.

## DeepSeek official-doc mode matrix
| Mode | Official source | First milestone status | Reverso behavior | Test expectation |
|---|---|---|---|---|
| Text | Chat Completion API | pass | Map DeepSeek chat completion content into Responses output text. | Valid non-streaming Response object. |
| Streaming | Chat Completion API | pass | Normalize DeepSeek data-only SSE stream into Codex-compatible Responses SSE events, preserving terminal completion. | Deltas arrive and terminal event is emitted. |
| JSON output | JSON Output guide and Chat Completion API `response_format` | unverified (spike-gated) | The live LiteLLM config strips `response_format` (`config/litellm_config.yaml:23`); the first-party DeepSeek adapter must NOT inherit that stripping. Preserve or translate `response_format: {"type":"json_object"}`; require prompt/test fixtures to ask for JSON; return explicit error on unsupported Responses shape. | A test proves `response_format` survives end-to-end to the DeepSeek call on the first-party path; status promotes to `pass` only when green. |
| Tool calls | Function Calling guide and Chat Completion API tools fields | pass | Return provider tool-call structures through Responses mapping; Reverso must not execute returned tool calls. | Tool call emitted; syscall/subprocess/network spy asserts ZERO execution after the tool call is surfaced. |
| Thinking mode | Thinking Mode guide and Chat Completion API `reasoning_content` | unverified (spike-gated) | The first-party adapter must NOT inherit `drop_params` stripping of `reasoning_content`. Preserve `reasoning_content` across tool-call turns; otherwise return explicit blocked/unsupported error BEFORE an invalid continuation. | Two-turn fixture carries turn-1 `reasoning_content` into turn-2, OR explicit rejection before invalid continuation. |
| Strict tool-call mode | Function Calling guide and Chat Completion API `strict` field | pass or unsupported | Treat strict mode as JSON schema conformance only, never authorization, sandboxing, or execution safety. | Schema behavior tested; no auth/safety claim. |

## Acceptance criteria
- Canonical docs define same-port provider endpoint topology for Auggie, DeepSeek, Claude, and Copilot, and resolve the single-port composition gap (`main.py:99` boot target) explicitly.
- `/auggie/v1/responses` and `/deepseek/v1/responses` are first-class Reverso provider endpoints registered via `APP_PROVIDER_PREFIXES` + `build_app`, not a new router.
- Adapters implement exactly the frozen five-method `ProviderAdapter` Protocol; no `capabilities` method is introduced without an explicit interface amendment and mini-review.
- Auggie SDK feasibility is a required first execution story, producing a PASS/FAIL/UNKNOWN capability matrix and a recorded indexing-control inspection artifact.
- Auggie indexing behavior has the falsifiable caveat test (literal `hard-disable unproven`, FAIL on `disabled`) and the no-workspace/sandbox default test when hard-disable proof is unavailable.
- DeepSeek JSON output and thinking mode are spike-gated (status promotes to `pass` only when the `response_format` survival and two-turn `reasoning_content` tests are green), reflecting the live `drop_params` config.
- The no-hidden-execution, no-secret-leakage, and LiteLLM-quarantine tests are present and falsifiable.
- No plan step edits `oh-my-auggie/`.
- No plan step adds a new port or provider process.
- Tests guard against accidentally routing Auggie or DeepSeek through the wrong provider prefix and against unknown prefixes being silently forwarded.
