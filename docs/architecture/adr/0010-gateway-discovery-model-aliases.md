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

1. **Alias namespace `anthropic--<backend>--<bare>`.** The `anthropic` prefix passes the
   discovery filter; `--` is a reserved separator (first-party model ids use single hyphens, so
   it cannot collide with a bare id). `list_anthropic_discovery_aliases()` emits one row per
   non-claude model: the rows-owning backends (codex/deepseek) from `_MODEL_INDEX`, and the
   rowless backends (copilot/auggie) from a small curated `_DISCOVERY_ROWLESS_MODELS` set (they
   own no taxonomy, so a known-good list seeds the picker; free-text `copilot/<id>` still reaches
   anything else upstream serves). claude is NOT aliased: its bare ids already pass the filter,
   so aliasing would only duplicate picker rows.

2. **`GET /v1/models` is the bare surface listing PLUS the aliases.** `list_anthropic_surface_models()`
   is unchanged (it remains the canonical bare set used by `cross_check_anthropic_models`); the
   handler appends the alias rows. claude ids show under their own derived names; aliases show as
   `"<Backend>: <model>"`.

3. **The resolver and `canonical_model_id` route the alias back, in lockstep.** Both detect
   `anthropic--<backend>--<bare>` via the shared `_split_discovery_alias` BEFORE the
   provider-qualified `/` split: `resolve_anthropic_backend` returns `<backend>` (fail-closed
   unless `<backend>` is an Anthropic-surface backend and `<bare>` is non-empty), and
   `canonical_model_id` returns the bare `<bare>` the adapter expects. The backend validates the
   bare model downstream (e.g. an unservable copilot id yields a clean `UnsupportedFeature`), so
   the alias never fail-opens to a misroute.

## Consequences

- With discovery enabled, all backends (codex/deepseek/copilot/auggie via aliases, claude via
  bare ids) are selectable in `/model`, labelled "From gateway".
- The alias is a routing hint only; it never reaches the upstream model string. The bare surface
  listing, provider-qualified routing (ADR 0008), and claude routing (ADR 0009) are unchanged.
- The curated rowless set is a convenience seed, not an authoritative catalog; it can drift from
  what copilot/auggie actually serve, and free-text remains the escape hatch.
- The launcher (`claude-reverso`) sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, so every
  reverso-backed session gets discovery; the builtin (direct-to-Anthropic) launchers do not.
