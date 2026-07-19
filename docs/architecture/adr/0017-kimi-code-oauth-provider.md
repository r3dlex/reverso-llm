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
- Mount `kimi` on the Anthropic Messages surface and route provider-qualified
  model ids such as `kimi/kimi-k2.5`.
- Prefer the OAuth access token written by `kimi /login` and refresh it through
  Kimi's OAuth token endpoint when it approaches expiry.
- Accept `KIMI_BEARER_TOKEN` only as a fallback when the OAuth artifact is not
  available.
- Use Kimi's OpenAI-compatible chat endpoint through Reverso's existing chat to
  Responses translation and replay behavior rather than adding the Kimi SDK.
- Fetch the available model catalog from Kimi's authenticated `/models`
  endpoint, with a small offline fallback.
- Keep Headroom provider-agnostic. Kimi requests pass through the same
  pre-dispatch compression seam as every Responses and Anthropic backend.

## Consequences

Kimi subscription traffic is available on both first-party protocol surfaces
without changing the frozen adapter protocol or adding a dependency. Users must
complete Kimi Code login separately with `kimi /login`, or explicitly supply a
bearer token. Reverso does not implement or collect interactive OAuth login.

Kimi initially inherits the existing OpenAI-compatible translated capability
ceiling. Features the upstream may support but Reverso does not yet translate
remain gated consistently with the DeepSeek translation path.

## Verification

Offline tests cover OAuth priority, bearer fallback, chat translation, live
model discovery, both protocol mounts, provider-qualified Anthropic routing,
and Headroom dispatch across all registered prefixes.
