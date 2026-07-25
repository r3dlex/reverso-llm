---
type: adr
project: reverso
id: 0010
title: Gateway-discovery model aliases for the /model picker
status: Accepted
date: 2026-06-26
related:
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0008-provider-qualified-model-routing.md
  - docs/architecture/adr/0009-claude-on-anthropic-surface.md
---

# ADR 0010: Gateway-discovery model aliases for the /model picker

## Status

Accepted, 2026-06-26. Personal-use, loopback-only. Additive to the GET /v1/models listing
(ADR 0006 AC8); does not change the bare surface listing or any routing already decided by
ADR 0008 (provider-qualified routing) or ADR 0009 (claude served).

## Context

Claude Code can populate the interactive `/model` picker from a gateway's `GET /v1/models`
when `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` is set (Claude Code v2.1.129+). Per the
gateway protocol, discovery reads each `data[].id` and optional `display_name` but **ignores
any id that does not begin with `claude` or `anthropic`**. reverso serves several non-claude
backends on the Anthropic surface (codex `gpt-*`, deepseek, copilot, auggie). Their bare ids
(`gpt-5.5`, `deepseek-v4-pro`, etc.) are silently dropped by the discovery filter, so only the
claude-family models were ever selectable in the picker; every other backend required free-text
`/model <id>`.

## Decision

Mint a discovery-only alias for every non-claude Anthropic-surface model so it passes the
filter, and route the alias back to its real backend:

1. **Alias namespace `anthropic-<backend>-<bare>`.** The `anthropic` prefix passes the
   discovery filter, and a single dash reads cleanly in the `/model` picker. Real first-party
   model ids use single hyphens AND embed them (`gpt-5.5`, `deepseek-v4-pro`, `claude-sonnet-4.6`),
   so the alias cannot be split naively on `-`. The parser instead keys on the KNOWN backend
   token: after stripping the `anthropic-` prefix, the remainder must begin with `<backend>-`
   where `<backend>` is in `SURFACE_BACKENDS["anthropic"]`;
   the rest is the bare model. The known backend names share no common prefix, so a first match
   over the known set is unambiguous. `list_anthropic_discovery_aliases()` emits one row per
   static non-claude model: the rows-owning backends from `_MODEL_INDEX`, and rowless backends
   from a small curated `_DISCOVERY_ROWLESS_MODELS` fallback set.

2. **`GET /v1/models` is the bare surface listing PLUS complete adapter catalogs.**
   `list_anthropic_surface_models()` is unchanged (it remains the canonical bare set used by
   `cross_check_anthropic_models`). On each listing request, the handler concurrently calls
   `list_models()` on every injected Anthropic-surface adapter and adds a provider-qualified
   discovery alias for every returned model. This includes dynamic claude models because a
   model absent from the static index still needs the explicit backend token to route. A failure
   in one adapter is isolated, each adapter listing has a 10-second deadline, and the static or
   curated rows remain available as fallback. Blocking CLI discovery runs off the shared event
   loop. Kimi discovery uses existing credentials only, so opening the picker cannot start an
   interactive login, and its dynamic rows remain restricted to the canonical `kimi-k3`.
   Duplicate aliases are removed and the result is sorted deterministically.

3. **The resolver and `canonical_model_id` route the alias back, in lockstep.** Both detect
   `anthropic-<backend>-<bare>` via the shared `_split_discovery_alias` BEFORE the
   provider-qualified `/` split. Static aliases must validate through the static taxonomy or
   curated fallback. Dynamic aliases route only when they are present in the application-scoped
   map produced by the most recent successful adapter catalogs. A syntactically valid alias that
   was never listed fails closed with HTTP 404 before adapter dispatch. After resolution,
   `canonical_model_id` returns the bare `<bare>` the adapter expects. Catalog refreshes retain
   each provider's last successful snapshot across transient failures and serialize concurrent
   refreshes so an older slow request cannot overwrite newer state.

4. **Launchers scope discovery with `x-reverso-model-catalog`.** The aggregate
   `claude-reverso` launcher sends `all` and receives every provider catalog. Each
   provider-specific launcher sends its backend name and receives only the bare and
   discovery-alias rows owned by that backend. Request routing remains model-driven;
   the header changes only the `GET /v1/models` listing.

## Consequences

- With discovery enabled, every model returned by every available adapter is selectable in
  `/model`, labelled "From gateway".
- The alias is a routing hint only; it never reaches the upstream model string. The bare surface
  listing, provider-qualified routing (ADR 0008), and claude routing (ADR 0009) are unchanged.
- The curated rowless set is a fallback seed, not the authoritative live catalog. It preserves
  known-good picker entries when a provider listing is unavailable.
- A free-text `anthropic-<backend>-<model>` value cannot bypass provider model validation:
  only static or actually listed aliases route.
- A slow, failed, or unauthenticated provider cannot hold the complete catalog response. Kimi's
  static `kimi-k3` fallback remains selectable without triggering authentication.
- The launcher (`claude-reverso`) sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, so every
  reverso-backed session gets discovery; the builtin (direct-to-Anthropic) launchers do not.
- Provider launchers no longer show unrelated provider models in `/model`; the
  aggregate catalog remains available only through `claude-reverso`.
