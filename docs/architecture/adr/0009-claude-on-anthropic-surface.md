---
type: adr
project: reverso
id: 0009
title: Serve claude on the inbound Anthropic Messages surface
status: Accepted
date: 2026-06-22
supersedes:
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md (D2 claude exclusion)
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/architecture/adr/0005-bounded-cli-spine.md
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
---

# ADR 0009: Serve claude on the inbound Anthropic Messages surface

## Status

Accepted, 2026-06-22. Supersedes ADR 0006 decision D2's exclusion of `claude` from the inbound
Anthropic Messages surface. Personal-use, loopback-only.

## Context

ADR 0006 added the inbound Anthropic Messages surface (`/v1/messages` on `127.0.0.1:64946`) so
Claude Code / the Claude Agent SDK, pointed at Reverso via `ANTHROPIC_BASE_URL`, can reach the
non-claude backends (copilot, deepseek, auggie; codex added in ADR 0007). D2 EXCLUDED `claude`
on a circularity argument: the claude backend is the local `claude` CLI, so a claude request
arriving over `ANTHROPIC_BASE_URL=reverso` and dispatched to a claude backend that itself
re-reads `ANTHROPIC_BASE_URL` would loop back into Reverso.

In practice the user wants a single endpoint: `claude-*` ids (and the `opus`/`sonnet`/`haiku`
CLI aliases) served by the local `claude` CLI under the subscription OAuth login, while
non-claude models keep routing to their existing backends. The circularity risk is real but
fully mitigable by controlling the environment handed to the spawned CLI.

## Decision

`claude` is SERVED on the Anthropic surface. `claude` joins `SURFACE_BACKENDS["anthropic"]`;
claude-family model ids resolve through the single `surface_registry` authority to the
`claude` backend; `/claude/v1/messages[/count_tokens]` pins it; and `GET /v1/models` lists the
claude rows. Completions are produced by the local `claude` CLI subprocess (the same
ProviderAdapter the Responses surface already uses), riding the subscription OAuth session.

### Circularity mitigation (two layers)

1. **Server process env.** Reverso runs as a launchd service whose process environment carries
   no `ANTHROPIC_BASE_URL`, so a child inheriting the parent env would not be redirected by
   default.
2. **Adapter env scrub (defence in depth).** `ClaudeAdapter` builds the CLI child env from a
   copy of `os.environ` and then SCRUBS `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and
   `ANTHROPIC_API_KEY` before injecting `CLAUDE_CODE_OAUTH_TOKEN`. This holds even if a caller
   (e.g. Claude Code) propagated those vars into Reverso's environment: the spawned `claude`
   CLI always reaches `api.anthropic.com` under its own subscription OAuth, never Reverso, and
   no caller-provided token can hijack the call. The parent `os.environ` is never mutated (only
   the copied child env), preserving the ADR 0002 falsifiable OAuth gate, which asserts the
   process env is left untouched.

### Auth stays OAuth-subscription-only

The claude backend continues to authenticate solely via the local `claudeAiOauth` artifact
(Keychain or `~/.claude/.credentials.json`), never a metered `ANTHROPIC_API_KEY`. The
falsifiable subscription-OAuth gate (ADR 0002 D3) is unchanged.

### Scope

Personal-use, loopback-only (`127.0.0.1`). This is not a multi-tenant or metered gateway.

## Consequences

- A claude request on the Anthropic surface is served first-party by the claude CLI, never a
  404 and never delegated to the legacy LiteLLM app.
- `GET /v1/models` now includes the claude rows from `litellm_config.yaml`
  (claude-opus-4-8, claude-opus, claude-sonnet-4-6, claude-sonnet, claude, claude-haiku-4-6,
  claude-haiku); the build-time cross-check finds them in config, so no exemption is needed.
- deepseek / gpt (codex) / copilot / auggie routing is unchanged.
- The loop-prevention env scrub is a hard invariant covered by a focused unit test.
