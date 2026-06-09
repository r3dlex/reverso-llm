---
type: adr
project: reverso
id: 0002
title: Responses-Native Provider Gateway
status: Proposed
date: 2026-06-09
supersedes: none
related: ["docs/03-architecture.md", "docs/04-mvp.md", ".omc/plans/ralplan-responses-providers.md"]
consensus: "ralplan Architect APPROVE + Critic APPROVE (.omc/state/ralplan/critic-review-responses-providers-20260609-followup.md)"
---

# ADR 0002: Responses-Native Provider Gateway

## Status

Proposed. Docs-first deliverable. The ralplan consensus gate is complete (Architect APPROVE,
Critic ITERATE then APPROVE), but no implementation code is written yet. This ADR and the
companion sections added to `docs/03-architecture.md` and `docs/04-mvp.md` define the
first-milestone boundary. Implementation starts only after this ADR is reviewed.

## Context

Reverso today is a LiteLLM-centered gateway: `src/reverso/proxy/app.py` wraps
`litellm.proxy.proxy_server.app`, and `src/reverso/proxy/main.py` boots that app from
`config/litellm_config.yaml` on the loopback bind. Codex talks to Reverso through an OpenAI
Responses surface (`/v1/responses`), and provider routing is path-prefixed today via
`src/reverso/proxy/profile_routing.py` (which currently knows `deepseek` and `claude`).

We want Codex to drive Claude Code and GitHub Copilot through Reverso using a first-party,
Reverso-owned OpenAI Responses contract, instead of depending on LiteLLM behavior for
Codex-specific request and stream details. Two provider auth modes matter and must be
preserved as first-class:

- Claude Code subscription OAuth (Pro/Max/Team "Sign in with Claude"), not metered
  `ANTHROPIC_API_KEY`.
- GitHub Copilot local logged-in-user credentials, not a repository-stored secret.

Hard repo constraints (`AGENTS.md`): bind `127.0.0.1:64946` only, no secrets in version
control or logs, `uv`-managed Python, frontmatter on every markdown file, no em or en dash
characters, and never delete spec content (augment or deprecate in place).

## Decision

Adopt a first-party Reverso ASGI Responses gateway for the Claude and Copilot provider
paths, with a stable provider-adapter interface and a shared Codex-observed parity test
suite. Keep legacy LiteLLM behavior isolated for non-goal providers until a later migration.

### D1. Single-port, path-prefixed endpoint model

All providers are served from one loopback port (`127.0.0.1:64946`). Each provider is a
path-prefixed Responses endpoint under that single port:

- Claude: `http://127.0.0.1:64946/claude/v1`
- Copilot: `http://127.0.0.1:64946/copilot/v1`

A Codex profile sets `base_url` to a provider prefix and reaches `/v1/responses`,
`/v1/models`, and related routes under that prefix. There is no per-provider port. This
matches the existing path-prefix routing already implemented in
`src/reverso/proxy/profile_routing.py` (the `/<profile>/v1/...` to `/v1/...` rewrite), so the
model is grounded in existing prior art. `copilot` is net-new: add its prefix in the
first-party app, not by mutating the legacy `PROVIDER_PREFIXES`.

### D2. First-party app boundary and LiteLLM quarantine

The first-party ASGI app lives at `src/reverso/protocols/responses_app.py` (a new top-level
`src/reverso/app.py` is avoided because `src/reverso/proxy/app.py` already exists). The new
app module must NOT import `reverso.proxy.app` (the legacy LiteLLM wrapper).

LiteLLM is quarantined, not the core router, for the Claude and Copilot `/v1/responses`
paths. The quarantine invariant is enforced at runtime, not by import-shape alone: a test
traces `litellm.proxy.proxy_server.app` and asserts zero invocations during Claude and
Copilot request handling, and asserts the new app's import graph excludes
`reverso.proxy.app`. An import-level-only assertion is insufficient because legacy modules
may coexist in-process.

Full LiteLLM retirement is a later milestone. Retirement criteria: no first-milestone or
later provider path routes through `litellm.proxy.proxy_server`, the non-goal providers
(for example DeepSeek) have a first-party path or a documented replacement, and the parity
suite is green for every migrated provider.

### D3. Claude subscription-OAuth credential artifact (amendment 1)

The credential artifact that constitutes Claude subscription OAuth is the `claudeAiOauth`
object produced by "Sign in with Claude":

- macOS: macOS Keychain generic-password item, service name `Claude Code-credentials`.
  The stored JSON has top-level key `claudeAiOauth` with sub-keys `accessToken`,
  `refreshToken`, `expiresAt`, `scopes`, `subscriptionType`, and `rateLimitTier`.
- Linux or headless fallback: `~/.claude/.credentials.json` (mode 0600), same top-level
  `claudeAiOauth` shape.

The metered alternative is the `ANTHROPIC_API_KEY` environment variable (or the long-lived
`CLAUDE_CODE_OAUTH_TOKEN`). A runtime-observable signal exists IF the adapter reads the
artifact directly: assert `claudeAiOauth.accessToken` is present AND `ANTHROPIC_API_KEY`
(and `CLAUDE_CODE_OAUTH_TOKEN`) is absent from the process environment. Subscription tier is
observable via `claudeAiOauth.subscriptionType`.

Limitation: the only available CLI-auth reference,
`../claude-code-openai-wrapper/src/auth.py`, does NOT read this artifact. It infers
`claude_cli` by elimination (it selects `anthropic` when `ANTHROPIC_API_KEY` is set, lines
34-71, and otherwise "assumes valid and lets the SDK handle auth", lines 172-183). A naive
"auth succeeded" check therefore passes identically for subscription OAuth and metered API
key, which would defeat amendment 1. Mitigation: Reverso's Claude adapter MUST add the
explicit Keychain or `.credentials.json` check above and assert on the `claudeAiOauth`
artifact, not on auth-by-elimination. Headless note: Keychain access needs a GUI desktop
session, which is why the `.credentials.json` file fallback exists.

This makes the Claude OAuth gate falsifiable: with no `ANTHROPIC_API_KEY` set and a
subscription OAuth credential present, the adapter authenticates and the resolved auth path
is the OAuth credential; the test fails if the resolved path is API-key auth or if
`ANTHROPIC_API_KEY` is consumed. If a future environment exposes no observable signal, the
adapter documents the limitation and the test is skipped against that documented limitation
rather than passed.

### D4. Copilot adapter spine: ported direct-forward adapter, not the SDK (amendment 2)

The GitHub Copilot Python SDK was evaluated before choosing the Copilot adapter spine. It is
real but does not fit a Responses-native gateway.

- Package: `github-copilot-sdk` (import name `copilot`), version 1.0.0, license
  `MIT AND LicenseRef-Copilot-CLI`, Python >= 3.11. Repo:
  `https://github.com/github/copilot-sdk/tree/main/python`.
- Architecture: a JSON-RPC wrapper that programmatically controls the GitHub Copilot CLI
  agent. It is NOT an HTTP client for an OpenAI Responses endpoint.

Evaluation matrix (seven amendment-2 criteria):

| Criterion | Result | Cited evidence |
|---|---|---|
| OAuth / logged-in-user | PASS | README constructor: `use_logged_in_user` (default True) and `github_token` priority. |
| Responses-style SSE mapping | FAIL | README exposes `AssistantMessageDeltaData` JSON-RPC deltas, not OpenAI Responses SSE events. |
| Model listing | PASS | README `on_list_models` handler for `list_models()`. |
| Tool / session support | PASS | README `create_session()` with custom tools and sessions. |
| Local credential handling | PASS | Requires the Copilot CLI authenticated via local GitHub login; no repo-stored secret. |
| Maturity / licensing | PASS | v1.0.0 stable, MIT plus CLI license ref; compatible with personal/local use. |
| Codex Responses fixture parity | FAIL | JSON-RPC-over-CLI surface, no OpenAI Responses contract; would need a heavy translation shim. |

Decision: use the ported direct-forward adapter as the Copilot spine, NOT the SDK. The
operational fallback trigger fired on condition (a): the SDK fails the shared Codex Responses
parity requirement (SSE mapping FAIL, parity FAIL). This is an evidence-driven primary
choice, not an ad-hoc workaround: the ported adapter in `../copilot-openai-api/main.py`
already speaks the native OpenAI `/responses` SSE contract by forwarding to
`api.githubcopilot.com`, using local `~/.config/github-copilot/{apps,hosts}.json`
credentials with auto-refresh and no repository secret (`CopilotAuth` at
`../copilot-openai-api/main.py:39-261`, proxy at `:295-374`). The SDK would require a
substantial event-translation shim purely to fake Responses parity, which is more code and
more risk than the direct forward.

Must-fix when porting: drop the access-token log line at `../copilot-openai-api/main.py:276`
and do NOT carry over the wildcard CORS at `../copilot-openai-api/main.py:284-292`
(`allow_origins=["*"]` with `allow_credentials=True`). Loopback-only bind makes broad CORS
unnecessary, and AGENTS.md forbids secret printing.

If a future SDK release adds a genuine Responses-compatible surface, re-evaluate against this
same matrix.

## Alternatives considered

- Big-bang LiteLLM replacement: rejected. Highest regression risk for DeepSeek and existing
  middleware, and conflicts with the first-milestone non-goal of no DeepSeek migration.
- External wrapper facade (run `claude-code-openai-wrapper` and `copilot-openai-api` as
  separate services behind Reverso): rejected. Violates the selected reuse boundary (port
  useful modules, do not facade or vendor), and makes Reverso less self-contained and harder
  to test with `uv`.
- Keep LiteLLM as core and add custom providers: rejected. Conflicts with the LiteLLM
  replacement intent for the new provider paths.
- Copilot via the official SDK: rejected for this milestone on cited evidence (D4): the SDK
  is a JSON-RPC CLI controller and fails Responses SSE mapping and Codex parity.

## Consequences

- Docs change before code; this ADR is the first deliverable.
- Some legacy LiteLLM code remains temporarily, but it does not own the new Claude and
  Copilot Responses paths, and a runtime guard proves it.
- The shared Codex-observed parity fixture suite becomes the milestone gate for both
  providers.
- Claude OAuth and Copilot OAuth success and failure behavior are part of the milestone gate.
- One loopback port serves all providers; Codex profiles point at provider prefixes.

## First-milestone non-goals

- No Codex CLI provider reimplementation.
- No DeepSeek migration.
- No launchd productionization or LaunchAgent decommissioning.
- No repository-stored secrets.
- No blind vendoring of the wrapper repos.

## Follow-ups

- Later plan for DeepSeek migration and full LiteLLM retirement (criteria in D2).
- Later plan for launchd productionization.
- Later plan for a Codex CLI provider if still desired.
- Re-evaluate the Copilot SDK if a Responses-compatible surface ships.

## Evidence and citations

- Reverso current state: `src/reverso/proxy/app.py`, `src/reverso/proxy/main.py`,
  `src/reverso/proxy/profile_routing.py`.
- Claude OAuth artifact: `claude_agent_sdk/_internal/session_resume.py:48` (Keychain service
  `Claude Code-credentials`), `:330-335` (`~/.claude/.credentials.json` path), `:383-385`
  (redaction confirming the `claudeAiOauth` shape); auth-by-elimination limitation at
  `../claude-code-openai-wrapper/src/auth.py:34-71` and `:172-183`. Corroborating public
  docs: Anthropic Claude Code authentication docs; anthropics/claude-code issue 29816.
- Copilot SDK: `https://github.com/github/copilot-sdk/tree/main/python`,
  `https://pypi.org/project/github-copilot-sdk/`,
  `https://docs.github.com/en/copilot/how-tos/copilot-sdk/getting-started`.
- Copilot fallback adapter: `../copilot-openai-api/main.py:39-261` (CopilotAuth),
  `:295-374` (proxy), `:276` (token-log must-fix), `:284-292` (wildcard CORS must-fix).
- Planning artifacts: `.omc/plans/ralplan-responses-providers.md`,
  `.omc/plans/prd-responses-providers.md`,
  `.omc/plans/test-spec-responses-providers.md`,
  `.omc/state/ralplan/critic-review-responses-providers-20260609.md` (ITERATE),
  `.omc/state/ralplan/critic-review-responses-providers-20260609-followup.md` (APPROVE).
