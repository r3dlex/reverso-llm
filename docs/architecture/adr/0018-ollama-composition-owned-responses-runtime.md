---
title: Composition-owned Ollama Responses runtime
status: accepted
date: 2026-08-20
---

# ADR 0018: Composition-owned Ollama dual-protocol runtime

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

G2 extends the same runtime with one native Messages client and an internal
`AnthropicNativeAdapter` facet on the existing adapter object. The composition
root injects that identical object into both registries. The frozen
`ProviderAdapter` remains byte-for-byte unchanged.

Ollama Messages routing is available only through the exact
`x-reverso-model-catalog: ollama` authority. The scoped catalog presents opaque
`anthropic-ollama-<raw-id>` aliases and binds each complete alias to its exact
raw model id. Bare ids, generic `ollama/<id>` forms, missing catalog headers,
case variants, duplicate aliases, and casefold collisions do not route. The raw
id from the authority replaces the presented alias before preparation,
Headroom, or upstream dispatch.

The Anthropic translator emits reverse source addresses while it creates each
reversible Responses text leaf. After Headroom runs once, projection changes
only those mapped native text leaves. Missing, reordered, merged, split, or
structurally changed mappings return the complete prepared native payload
unchanged. Tool blocks, tool results, images, ids, ordering, and request controls
remain native Messages structures. Existing non-native adapters continue using
the prior Responses translation and mapping path.

## Consequences

- Ollama becomes a first-party Responses route without changing the frozen
  adapter contract.
- Local inventory changes are visible on the next supported model refresh.
- G2 completes the formerly deferred Claude Messages authority without adding a second
  runtime or widening the public adapter contract.
- Daemon management, model pulls, and unattended sign-in are outside scope.
