---
type: test-spec
project: reverso
slug: responses-providers
status: draft
ported_from: .omx/plans/test-spec-responses-providers-20260608T221431Z.md
ported_at: 20260609
---

# Test Spec: Responses-Native Reverso Providers

## Test principle
The same Codex-observed Responses fixture suite must run against Claude and Copilot. Provider-specific tests may exist underneath, but milestone completion requires a shared contract suite.

## Fixture classes
1. Minimal text response
   - POST `/claude/v1/responses` and `/copilot/v1/responses` with `input` text.
   - Expect a valid Response object with assistant output text.
2. Streaming text response
   - Same payload with `stream: true`.
   - Expect SSE events with text deltas, a terminal `response.completed`, and `[DONE]` if Codex requires it.
3. Model refresh
   - GET `/claude/v1/models` and `/copilot/v1/models` with Codex-style query metadata.
   - Expect OpenAI model list plus Codex-compatible fields currently handled by `CodexModelsCompatMiddleware`.
4. Conversation continuity
   - Send a second request with `previous_response_id` or the Codex-observed state field.
   - Expect the adapter to map state to provider capabilities or return an explicit compatibility error where Codex does not require it.
5. Function/tool surface
   - Send Codex-observed function tool declarations and tool outputs.
   - Expect pass-through, supported translation, or explicit OpenAI-style unsupported-field errors.
6. Malformed input
   - Invalid JSON, missing model, missing input, invalid stream type, oversized strings.
   - Expect bounded 4xx error shapes, no traceback leak.
7. OAuth, auth, and secrets
   - Missing Copilot OAuth file, expired Copilot token, missing Claude Code OAuth or CLI auth.
   - Expect 401 or 503 style provider errors without printing token values in responses or logs.
   - Assert captured logs do not contain token-like substrings (backstop for ported Copilot code that previously logged the bearer token).
   - Assert successful Claude Code OAuth or CLI-auth backed requests work without repository-stored secrets.
   - Assert successful Copilot OAuth requests work through the selected SDK, token-file, or hybrid adapter path without repository-stored secrets.
   - Claude subscription-OAuth falsifiable test: with no `ANTHROPIC_API_KEY` set and a subscription OAuth credential present, the Claude adapter authenticates and serves a request, AND the resolved auth path is the subscription OAuth credential. The test fails if `auth_method` resolves to `anthropic`/API-key auth or if `ANTHROPIC_API_KEY` is consumed. If no observable signal distinguishes subscription OAuth from `ANTHROPIC_API_KEY`, this test is marked skipped with a reference to the ADR limitation, not silently passed.
   - CORS backstop: assert the served app does not set wildcard `allow_origins=["*"]` together with `allow_credentials=True`. Loopback-only bind makes broad CORS unnecessary.
8. Streaming failure
   - Upstream timeout or interrupted stream.
   - Expect bounded timeout and final error event or HTTP error without hanging.
9. Dirty worktree and safety
   - Provider prompts must not cause Reverso tests to modify unrelated workspace files.
10. Regression
   - Existing middleware tests remain green or are replaced by equivalent first-party app tests.
11. LiteLLM quarantine (runtime-scoped)
   - Monkeypatch or trace `litellm.proxy.proxy_server.app` and assert zero invocations during Claude and Copilot `/v1/responses` request handling. An import-level assertion is insufficient because the first-party app may share a process with legacy modules.
   - Assert the first-party app module's import graph excludes `reverso.proxy.app` (the legacy LiteLLM wrapper). The new app must not import the legacy proxy app.
12. Single-port per-provider endpoints
   - Start one app on one loopback port and assert both `/claude/v1/responses` and `/copilot/v1/responses` are reachable on that same port.
   - Assert a Codex profile `base_url` of `http://127.0.0.1:<port>/claude/v1` (and `/copilot/v1`) resolves to the correct provider adapter, and that the prefix routes `/v1/responses` and `/v1/models` to that provider only.

## Official Responses API and SDK evidence to cite before coding
- OpenAI Responses create endpoint: `POST /v1/responses`.
- OpenAI Responses streaming events for SSE behavior.
- OpenAI response retrieval, cancellation, input items, tool calling, and `previous_response_id` semantics.
- Copilot Python SDK: do not cite the SDK API surface as fact. The Phase 0 evaluation matrix must verify the package, version, license, and each claimed capability (`CopilotClient`, async sessions, streaming events, `github_token`, `use_logged_in_user`, tools, model listing) with cited evidence before any test depends on it. Candidate source to verify: `https://github.com/github/copilot-sdk/tree/main/python`. If unverifiable, the fallback trigger applies and tests target the ported direct-forward adapter instead.

## Verification commands
- `uv run pytest tests/unit -q`
- `uv run pytest tests/integration -q`
- `uv run pytest tests/e2e -q` after e2e fixtures exist
- `./scripts/smoke.sh` only after local service startup is implemented for the new app
- `rg -nP '[\x{2013}\x{2014}]' docs README.md src tests` to enforce no en or em dashes in touched text files (the `-P`/`\x{...}` form is unambiguous across ripgrep versions)
