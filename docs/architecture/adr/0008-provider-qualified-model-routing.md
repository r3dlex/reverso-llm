---
type: adr
project: reverso
id: 0008
title: Provider-qualified model routing on the Anthropic surface
status: Accepted
date: 2026-06-22
supersedes: none
related:
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
---

# ADR 0008: Provider-qualified model routing on the Anthropic surface

## Status

Accepted, 2026-06-22. Extends the inbound Anthropic Messages surface (ADR 0006) so a
client can name the provider up front in a `provider/model` model id to disambiguate
when two backends would otherwise expose the same model name.

## Context

The Anthropic surface resolves a requested model to a backend through the single
authority `surface_registry.resolve_anthropic_backend`. Resolution is by model-name
family: ids beginning `deepseek` route to deepseek, the static gpt-* ids route to codex,
and the claude family fails closed (ADR 0006 D2, ADR 0007). Today the served ids are
globally unique, so a bare name is unambiguous.

That uniqueness is not guaranteed. `copilot` and `auggie` are exposed backends
(`SURFACE_BACKENDS["anthropic"]`) that carry no `litellm_config` rows, so they own no
concrete model ids in the index; and two providers could in future publish the same
model name. When names collide, a bare id cannot say which backend the caller meant.

The client (Claude Code via `claude-reverso`, or the Agent SDK) must therefore be able
to put the provider up front: `codex/gpt-5.5`, `deepseek/deepseek-v4-pro`,
`copilot/<id>`. Before this ADR a qualified id 404'd: `_normalize_model` strips only a
`custom/` prefix, so `codex/gpt-5.5` missed the index and resolved to None.

## Decision

1. **`provider/model` is routed by its explicit prefix, prefix authoritative.**
   `resolve_anthropic_backend` splits a normalized id on the first `/`. When a prefix is
   present it routes through `_resolve_qualified`, which fails closed unless:
   - the provider is a member of `SURFACE_BACKENDS["anthropic"]`, and
   - the bare model is non-empty, and
   - when the bare model is indexed, it is indexed to *this* provider (a
     contradiction such as `deepseek/gpt-5.5` fails closed rather than silently
     honoring either side).
   When the bare model is unknown to the index, the explicit provider is trusted **only
   for a rowless backend**: `copilot`/`auggie` own no index taxonomy, so naming the
   provider up front is the only way to reach them. A backend that *does* own a taxonomy
   (`deepseek`/`codex`) must name a model it actually serves; an unknown bare model behind
   a known-backend prefix (`codex/totally-made-up`, or `codex/ gpt-5.5` with stray
   whitespace) fails closed exactly as the bare-id path would, so the qualifier never
   becomes a fail-open bypass of the index.

2. **claude stays fail-closed.** The claude-family check runs on the whole normalized id
   before the split, so `claude/...` (and any id containing `claude`) resolves to None.
   `claude` is not in the surface backend set either, so even the prefix path rejects it.

3. **The prefix is a routing hint, not part of the upstream model id.** A new
   `canonical_model_id` strips a valid `provider/` qualifier back to the bare model
   (original casing preserved). It makes its provider decision through the **same**
   `_normalize_model` + `_split_provider_qualified` the resolver uses, so the two can
   never diverge: every qualified id the resolver routes (including mixed-case
   `Codex/GPT-5.5` and an upper-case `CUSTOM/codex/gpt-5.5`) is stripped to its bare model
   here. `anthropic_app` canonicalizes `payload["model"]` in place immediately after
   backend resolution, so the downstream adapter (codex `codex exec`, deepseek http, ...)
   receives `gpt-5.5`, never `codex/gpt-5.5`. Non-surface and claude qualifiers are left
   intact (they 404 at resolution anyway).

4. **Bare ids are unchanged.** No `/` means the existing name-family / index resolution
   path runs exactly as before; this ADR is additive and back-compatible.

5. **The `/v1/models` listing is unchanged.** It continues to advertise bare ids; the
   qualified form is an accepted *input* alias, not a second catalog entry, so the listing
   and the codex-side selector catalog (`model_exposure.PREFIXED_SELECTOR_PREFIXES`) stay
   the single sources of truth without duplication.

## Consequences

- Clients can disambiguate conflicting names deterministically, and `copilot`/`auggie`
  (which own no bare index ids) become reachable on the Anthropic surface via their
  explicit prefix.
- Resolution stays fail-closed: unknown provider, claude, empty bare model, and
  provider/model contradictions all 404, with no adapter ever dispatched (verified by
  `tests/integration/test_anthropic_provider_qualified.py`).
- The prefix never leaks to a provider call, so a qualified request is byte-identical to
  the equivalent bare request at the adapter boundary. This holds across normalization
  forms (mixed case, `CUSTOM/`) because `canonical_model_id` and `resolve_anthropic_backend`
  share one normalizer; the coupling is pinned by a parametrized resolve/canonical test and
  an end-to-end assertion that no adapter ever receives a slash-bearing model id.
- A future genuine name collision is handled by the existing mismatch rule without
  further surface changes; only a new shared-name policy (if ever desired) would revisit
  this ADR.
