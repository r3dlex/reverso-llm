---
title: Composition-owned Ollama Responses runtime
status: accepted
date: 2026-08-20
---

# ADR 0018: Composition-owned Ollama Responses runtime

## Context

Ollama exposes OpenAI-compatible Responses on a user-owned loopback service and
installed inventory through `/api/tags`. Reverso must keep embedded Headroom,
response retrieval, client ownership, and its single loopback gateway authority.
The frozen `ProviderAdapter` contract cannot be expanded.

## Decision

The composition root owns one Ollama runtime with one validated loopback HTTP
client, one raw-id catalog, one Responses client, one response store, and one
adapter implementing the existing frozen contract. G1 injects that adapter only
into the Responses registry at `/ollama/v1`. The Anthropic registry and Claude
Code launchers remain unchanged until G2.

Installed local model ids come from validated `/api/tags` rows, and every row
is local regardless of suffix. No supported machine-readable Cloud authority is
established in G1, so Cloud publication is unavailable rather than inferred.
Codex receives local ids byte-for-byte in an isolated profile and catalog.
Reverso does not start Ollama, pull models, invoke sign-in, or read device
identity.

`/api/tags` does not provide the model-specific input modalities, parallel-tool
support, or context bound required by the Codex catalog schema. G1 therefore
publishes conservative metadata for every generated Ollama entry: text-only
input, no parallel tool calls, and a 2048-token context bound. This is a picker
metadata limitation, not a gateway feature gate: explicitly submitted image
and function-tool payloads continue to pass through to Ollama Responses.

The composition root closes the runtime once during bounded shutdown. Runtime
close is idempotent only as a defensive safeguard. Embedded Headroom remains at
the Responses dispatch boundary and attributes the request to provider
`ollama` and surface `responses` before adapter dispatch.

## Consequences

- Ollama becomes a first-party Responses route without changing the frozen
  adapter contract.
- Local inventory changes are visible on the next supported model refresh.
- Claude Messages aliases and native Messages dispatch are explicitly deferred.
- Daemon management, model pulls, and unattended sign-in are outside scope.
