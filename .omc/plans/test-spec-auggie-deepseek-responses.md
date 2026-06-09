---
type: test-spec
project: reverso
slug: auggie-deepseek-responses
status: draft
ported_from: .omx/plans/test-spec-auggie-deepseek-responses-20260608T225050Z.md
ported_at: 20260609
---

# Test Spec: Auggie and DeepSeek Responses Providers

## Test principle
Auggie and DeepSeek must be tested as Reverso provider prefixes on the same local port. Shared Codex Responses fixtures should be reused where possible, with provider-specific suites for Auggie SDK behavior and DeepSeek full modes.

## Shared provider contract tests
1. Same-port routing
   - POST `/auggie/v1/responses` and `/deepseek/v1/responses` through the same ASGI app and configured port.
   - Assert no new listener or server process is required by unit tests.
2. Model refresh
   - GET `/auggie/v1/models` and `/deepseek/v1/models` with Codex-style refresh query metadata.
   - Assert OpenAI model list shape plus Codex-compatible `models` field behavior.
3. Non-streaming Responses
   - Minimal text `input` returns a valid Response object with assistant output text.
4. Streaming Responses
   - `stream: true` returns valid SSE events and a terminal completion event expected by Codex.
5. Conversation continuity
   - `previous_response_id` maps to an in-memory Reverso state store, provider session, explicit unsupported response, or Codex-observed equivalent.
6. Malformed requests
   - Invalid JSON, missing model, missing input, invalid stream type, and unsupported tools produce bounded 4xx or 5xx responses without traceback leaks.
7. No-secret leakage
   - Responses and captured logs must not contain API keys, OAuth tokens, Auggie auth data, DeepSeek key values, or filesystem credentials.

## Auggie-specific tests
1. SDK feasibility tests
   - Mock `auggie_sdk.Auggie` or ACP client to prove Reverso can test without launching real Auggie.
   - Verify auth missing or CLI unavailable yields a bounded provider error.
   - Verify model listing maps `Auggie.get_available_models()` to OpenAI model list shape.
2. Indexing tests
   - If SDK exposes a disable-indexing control, assert Reverso sets it by default.
   - If proof is unavailable, assert docs and `/models` or capability metadata expose a clear caveat and tests capture the best-effort suppression path.
3. Streaming/event tests
   - Map `on_agent_message_chunk`, final messages, tool calls, and thoughts into Responses SSE events or documented provider-native events.
4. Provider-native tool tests
   - Assert Reverso does not secretly execute extra tools beyond provider-native Auggie behavior.
   - Assert Reverso documents and observes tool events where the SDK exposes them.

## DeepSeek-specific tests
1. OpenAI-compatible request forwarding
   - Assert base URL and model aliases are resolved for DeepSeek provider prefix.
   - Verify `/deepseek/v1/responses` does not lose Codex GPT-level profile routing semantics.
2. Full-mode matrix
   - Text response.
   - Streaming response.
   - JSON output or structured output where DeepSeek supports it.
   - Tool calls with provider-native behavior and validation warnings documented.
   - Thinking mode, preserving reasoning/tool-call fields where required.
   - Strict tool-call mode using beta-compatible behavior where feasible.
3. Error and fallback matrix
   - Unsupported DeepSeek modes return explicit compatibility errors rather than silent downgrade.
   - Thinking-mode tool loops preserve required provider fields or reject clearly.

## Architectural contract tests
- Registration: `split_provider_path` accepts `/auggie/v1/...` and `/deepseek/v1/...` after `APP_PROVIDER_PREFIXES` is extended; `build_app` raises for any adapter prefix not in the allow-list (existing `__init__` guard). No `ProviderResponsesRouter` is created; the merged `ResponsesGatewayApp` owns dispatch.
- LiteLLM quarantine (runtime-scoped, falsifiable): a guard test traces `litellm.proxy.proxy_server.app` and asserts ZERO invocations when driving `/auggie/v1/responses` and `/deepseek/v1/responses`, and asserts the import graph for these paths excludes `reverso.proxy.app`. The test FAILS if either path reaches LiteLLM.
- `/deepseek/v1/responses` uses the first-party DeepSeek adapter (not LiteLLM) for Responses or full-mode behavior; a `resolve_profile_model` regression test covers any path still served by the legacy stack.
- Single-port composition: a test asserts the documented boot wiring serves first-party prefixes on `127.0.0.1:64946` without introducing a new listener or process.
- Unknown provider-prefixed routes are rejected by the allow-list (not silently forwarded); the legacy-fallthrough route list is explicit.
- No new Reverso-managed listener, LaunchAgent, server process, or provider sidecar is introduced.
- Bounded Auggie child process behavior, if required by SDK, has timeout, cleanup, and observability tests (precedent: `adapters/claude.py` subprocess model).
- Auggie capability metadata (via `list_models`/`models_with_codex_refresh`) and docs contain the literal `hard-disable unproven` when indexing hard-disable proof is unavailable; the test FAILS if `disabled` is used. When unproven, the default workspace is no-workspace/sandbox, never the caller workspace.
- The Auggie spike records exact SDK options and CLI flags/config for indexing controls with observed values in `.omc/research/auggie-indexing-spike.md`; "proof unavailable" is valid only when that artifact shows the controls are absent.
- Adapters implement exactly the frozen five-method `ProviderAdapter` Protocol; a test asserts no `capabilities` (or other extra) method is required by the app.
- DeepSeek full modes are tracked in a matrix with status `pass`, `unverified`, `unsupported`, or `blocked`; JSON output and thinking mode start `unverified` and promote only when their survival tests pass.
- No hidden execution (falsifiable): a syscall/subprocess/network spy asserts ZERO execution after a DeepSeek tool call is surfaced; for Auggie the test distinguishes Reverso execution from Auggie's own configured action.
- No secret leakage (falsifiable): `DEEPSEEK_API_KEY` and Auggie auth set to unique sentinels; success and error paths assert sentinels appear in neither response body nor logs.
- Strict tool-call mode tests assert schema constraints only, not authorization guarantees.

## DeepSeek official-doc matrix tests
- Text status `pass`: valid Response object from DeepSeek content.
- Streaming status `pass`: DeepSeek data-only SSE maps to Responses SSE and terminal event.
- JSON output status `unverified` until green: the first-party adapter must NOT inherit `_codex_drop_params` (`config/litellm_config.yaml:23` strips `response_format`); a test proves `response_format` survives end-to-end to the DeepSeek call, then status promotes to `pass`. Unsupported Responses shapes return an explicit compatibility error.
- Tool calls status `pass`: returned tool calls are surfaced; a syscall/subprocess/network spy asserts Reverso does not execute them.
- Thinking mode status `unverified` until green: the first-party adapter must NOT strip `reasoning_content`; a two-turn fixture asserts turn-1 `reasoning_content` is carried into the turn-2 request, OR an explicit rejection is returned before an invalid continuation. Status promotes to `pass`/`blocked` per that fixture.
- Strict tool-call mode status `pass` or `unsupported`: strict mode is tested only as JSON schema conformance, not authorization, sandboxing, or execution safety.

## Static and safety checks
- `rg -nP '[\x{2013}\x{2014}]' docs README.md src tests` for touched text (the `-P`/`\x{...}` form is unambiguous across ripgrep versions).
- No files under `../oh-my-auggie/` are modified.
- No repository secret patterns in diffs.
- Route tests fail if `/auggie/v1/...` or `/deepseek/v1/...` is served from a separate port or process.

## Verification commands for implementation phase
- `uv run pytest tests/unit -q`
- `uv run pytest tests/integration -q`
- `uv run pytest tests/e2e -q` after e2e fixtures exist
- `./scripts/smoke.sh` only after local service startup supports the new app routes
