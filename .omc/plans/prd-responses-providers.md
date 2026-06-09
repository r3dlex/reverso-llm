---
type: prd
project: reverso
slug: responses-providers
status: draft
ported_from: .omx/plans/prd-responses-providers-20260608T221431Z.md
ported_at: 20260609
---

# PRD: Responses-Native Reverso Providers

## Source artifacts
- Deep interview spec: `.omc/specs/deep-interview-responses-providers.md`
- Context snapshot: `.omc/context/responses-providers-20260608T214418Z.md`
- Transcript: `.omc/interviews/responses-providers-20260608T215316Z.md`

## Requirements summary
Reverso must move its first-milestone Claude and Copilot provider paths away from LiteLLM as the core request router and toward a Reverso-owned OpenAI Responses API gateway for Codex. Current code proves the opposite starting point: `src/reverso/proxy/app.py:1-32` wraps `litellm.proxy.proxy_server.app`, and `src/reverso/proxy/main.py:1-105` resolves `config/litellm_config.yaml` and starts `reverso.proxy.app:app` as a LiteLLM proxy wrapper.

The first milestone is docs-first: canonical Reverso docs must describe the new Responses-native architecture before source implementation, because `docs/AGENTS.md` treats docs as the design source of truth. Implementation must keep the hard repo constraints from `AGENTS.md`: loopback-only bind, no secrets in version control, `uv` managed Python, markdown frontmatter, and no em or en dash characters in text files.

## Endpoint and port model
All providers are served from one loopback port. Each provider is a path-prefixed Responses endpoint under that single port, so one Codex profile points at one provider endpoint:
- Claude: `http://127.0.0.1:<port>/claude/v1`
- Copilot: `http://127.0.0.1:<port>/copilot/v1`
A Codex profile sets `base_url` to the provider prefix and reaches `/v1/responses`, `/v1/models`, and related routes under that prefix. No per-provider port is introduced.

## User value
- Codex can target Claude Code and GitHub Copilot through Reverso using a Responses API contract.
- Claude Code and GitHub Copilot OAuth-backed local authentication are first-class supported paths, not optional follow-ups.
- Reverso owns provider compatibility instead of depending on LiteLLM behavior for Codex-specific request and stream details.
- Both providers are proven by the same Codex-observed parity suite.

## In scope
- Update canonical docs before code.
- Replace LiteLLM as the core router for new Claude and Copilot provider paths.
- Introduce a Reverso-owned ASGI app and provider adapter boundary.
- Port useful modules or behavior from `../claude-code-openai-wrapper` and `../copilot-openai-api` with Reverso-native tests.
- Produce a written Copilot SDK evaluation matrix in Phase 0 before choosing the Copilot adapter spine. The package, version, license, and API surface are claimed and must be verified with cited evidence; do not treat the SDK API as established fact. Candidate evidence source to verify: `https://github.com/github/copilot-sdk/tree/main/python`.
- Implement `/v1/responses`, `/v1/models`, and Codex-required response/session/stream semantics for both Claude and Copilot.
- Maintain OpenAI-style error responses or compatibility stubs for unsupported fields outside the Codex-observed subset.

## Out of scope for the first milestone
- No Codex CLI provider reimplementation.
- No DeepSeek migration.
- No launchd productionization or LaunchAgent decommissioning.
- No repository-stored secrets.
- No blind vendoring of wrapper repos.

## Acceptance criteria
1. Docs-first gate
   - `docs/03-architecture.md` or a new ADR describes the Responses-native gateway, provider adapter layer, LiteLLM replacement boundary, legacy LiteLLM quarantine rules, and full retirement follow-up criteria.
   - `docs/04-mvp.md` identifies the first milestone non-goals.
   - README and config docs are updated only when implementation behavior is ready or clearly marked as planned.
2. Gateway boundary
   - New app structure separates protocol normalization, provider routing, provider adapters, response store, and compatibility middleware.
   - LiteLLM is not the core router for Claude and Copilot `/v1/responses` paths.
   - One loopback port serves all providers. Each provider is a path-prefixed Responses endpoint (`/claude/v1/...`, `/copilot/v1/...`) so one Codex profile maps to one provider via `base_url`. No per-provider port.
   - The first-party app module does not import `reverso.proxy.app` (the legacy LiteLLM wrapper); a test asserts the import graph excludes it.
3. Claude provider
   - Claude adapter accepts the shared Codex-observed fixture suite.
   - Claude Code OAuth or CLI-auth backed subscription login is supported and proven by a falsifiable test: with no `ANTHROPIC_API_KEY` in the environment and a subscription OAuth credential present, the adapter authenticates and serves a request successfully, AND the resolved auth path is the subscription OAuth credential, not metered API-key auth. The test fails if the adapter falls through to `ANTHROPIC_API_KEY`/API-key auth or if `ANTHROPIC_API_KEY` is consumed.
   - The Phase 0 ADR names the exact credential artifact (file path, environment variable, or SDK call) that constitutes Claude subscription OAuth, with cited evidence that it bills the subscription rather than metered API. Until the ADR names that artifact, this gate is unfalsifiable and stays open. If `claude-agent-sdk`/CLI auth cannot expose an observable signal distinguishing subscription OAuth from `ANTHROPIC_API_KEY`, the ADR must document that limitation and the chosen mitigation.
   - It returns valid non-streaming Responses objects and SSE events.
   - It supports the session behavior Codex sends, including `previous_response_id` when observed or required by fixtures.
4. Copilot provider
   - A Phase 0 Copilot SDK evaluation matrix exists, scoring the SDK against the seven amendment-2 criteria (OAuth/logged-in-user support, Responses SSE mapping, model listing, tool/session support, local credential handling, package maturity/licensing, Codex fixture parity) with PASS/FAIL/UNKNOWN and cited evidence (real package, version, license, specific source or README lines).
   - The fallback trigger is defined operationally: fall back to the ported `copilot-openai-api` token-refresh and direct-forward adapter if ANY of the following holds: (a) the SDK fails the shared Codex parity fixture matrix in a spike, (b) it requires a repository-stored secret, (c) its license is incompatible with personal/local use, or (d) `use_logged_in_user` cannot authenticate without a metered API key.
   - Copilot adapter accepts the same shared fixture suite.
   - Copilot OAuth is supported and tested using local GitHub/Copilot credential sources or, only if the matrix passes, the SDK logged-in-user path.
   - It returns compatible non-streaming and streaming Responses output.
   - Copilot auth/token refresh follows the local token-file pattern, verified SDK logged-in-user behavior, or a Reverso-safe adaptation without repository secrets.
5. Shared parity
   - The same parity test harness runs against both providers.
   - Unsupported fields beyond Codex-observed behavior have explicit OpenAI-style errors or documented stubs.
6. Verification
   - Existing unit tests pass or are intentionally updated after docs are revised.
   - New unit, integration, and adversarial e2e tests cover the parity suite, auth failure paths, malformed payloads, streaming completion, no-secret leakage in responses and logs, and no LiteLLM import/call behavior for Claude and Copilot Responses paths.

## Open questions to resolve during implementation planning
- Exact Codex-observed request fixtures should be harvested from local Codex/Reverso traces where safe, then minimized into fixtures.
- Whether Claude should call `claude-agent-sdk` directly or preserve the current Claude Code CLI subprocess path through an adapter. The plan should choose a phaseable architecture that allows either while keeping the public provider boundary stable. The selected path must preserve Claude Code OAuth or CLI-auth subscription behavior.
- Whether Copilot should use the official Copilot Python SDK, direct `api.githubcopilot.com` forwarding from `copilot-openai-api`, or a hybrid. The SDK must be evaluated first. It is claimed to expose `github_token`, `use_logged_in_user`, runtime connections, streaming events, tools, sessions, and model listing in an async Python API; these claims are unverified and must be confirmed in the Phase 0 evaluation matrix with cited evidence before any adapter choice. If the package or its API surface cannot be verified, the fallback trigger applies.
