---
title: Kimi Code OAuth Provider
status: accepted
date: 2026-07-19
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
---

# ADR 0017: Kimi Code OAuth Provider

## Context

Reverso needs to expose a Kimi Code subscription through both its OpenAI
Responses and Anthropic Messages surfaces. Kimi Code CLI authenticates through
OAuth device login, stores a bearer credential under
`~/.kimi-code/credentials/kimi-code.json` by default, and calls the OpenAI-compatible
`https://api.kimi.com/coding/v1/chat/completions` endpoint.
`KIMI_CODE_HOME` overrides the `~/.kimi-code` home when set.

The official `kimi-sdk` package was evaluated. It is designed for Kimi agent
workflows over chat completions, requires Python 3.12 or newer, and does not
provide OpenAI Responses or Anthropic Messages wire contracts. Adding it would
not remove Reverso's translation requirement and would raise Reverso's Python
floor from 3.11.

## Decision

Add a first-party `KimiAdapter` at the frozen `ProviderAdapter` boundary.

- Mount `/kimi/v1` on the Responses gateway.
- Mount `kimi` on the Anthropic Messages surface and route both bare `kimi-k3`
  and provider-qualified `kimi/kimi-k3`.
- Prefer the OAuth access token written by `kimi login` and refresh it through
  Kimi's OAuth token endpoint when it approaches expiry.
- When a request has no usable local credential or bearer fallback, let the
  gateway supervise exactly one shared `kimi login` process, reload the
  CLI-owned artifact after success, and resume the waiting request.
- Keep the Kimi CLI as the OAuth and browser-interaction authority. Reverso
  bounds and reaps the child process but never handles credential contents or
  implements the OAuth exchange used by interactive login.
- Accept `KIMI_BEARER_TOKEN` only as a fallback when the OAuth artifact is not
  available.
- Use Kimi's OpenAI-compatible chat endpoint through Reverso's existing chat to
  Responses translation and replay behavior rather than adding the Kimi SDK.
- Expose only the public model id `kimi-k3` and translate it to the upstream
  model id `k3` when dispatching requests. Keep
  `anthropic-kimi-kimi-k3` as the Claude Code discovery alias because the
  gateway picker filters out ids that do not begin with `claude` or `anthropic`.
- Fetch Kimi's authenticated `/models` endpoint to establish live discovery.
  Runtime discovery may return only canonical K3 fallback metadata on failure,
  but Codex synchronization must reject fallback or otherwise non-live
  discovery.
- Generate the Kimi Codex profile and catalog with only `kimi-k3`, a context
  window of `1048576`, and a profile-level auto compact token limit of `419430`.
  The explicit limit prevents a lower global Codex limit from surviving profile
  layering and applies the selected 40 percent compaction policy for the Kimi
  window.
- Poll Kimi's authenticated usage endpoint with existing credentials only.
  Cache one single-flight refresh for 60 seconds, retain the last known good
  snapshot across bounded failures, and expose it at `GET /usage/kimi`.
- Map the 300-minute quota to Codex's primary rate-limit headers and the weekly
  quota to its secondary headers on Kimi Responses and Anthropic Messages
  responses. This lets Codex and OMX update both HUD windows without changing
  the frozen adapter protocol.
- Keep Headroom provider-agnostic. Kimi requests pass through the same
  pre-dispatch compression seam as every Responses and Anthropic backend.

## Consequences

Kimi subscription traffic is available on both first-party protocol surfaces
without changing the frozen adapter protocol or adding a dependency. Missing
local authentication can pause the request while the gateway supervises the
official `kimi login` command. The request resumes only after the CLI exits
successfully and a usable CLI-owned artifact can be reloaded. Users may still
run `kimi login` separately or explicitly supply a bearer token.

Kimi initially inherits the existing OpenAI-compatible translated capability
ceiling. Features the upstream may support but Reverso does not yet translate
remain gated consistently with the DeepSeek translation path.

The public model contract is deliberately narrower than Kimi's upstream
catalog. Reverso exposes `kimi-k3`, dispatches `k3`, and prevents stale or
fallback discovery from regenerating Codex metadata.

## Verification

Offline tests cover OAuth priority, bearer fallback, chat translation, live
model discovery, both protocol mounts, bare and provider-qualified Anthropic routing,
Headroom dispatch across all registered prefixes, shared login coordination,
post-login artifact validation, request resume after official CLI success,
K3-only exposure and translation, passive cached usage polling, Codex rate-limit
response headers on both protocol surfaces, and fail-closed Codex synchronization.
