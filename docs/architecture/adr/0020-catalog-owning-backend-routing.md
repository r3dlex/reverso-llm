---
type: adr
project: reverso
id: 0020
title: Catalog-owning backends on the Anthropic surface
status: Accepted
date: 2026-08-22
supersedes: none
related:
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0008-provider-qualified-model-routing.md
  - docs/architecture/adr/0009-claude-on-anthropic-surface.md
---

# ADR 0020: Catalog-owning backends on the Anthropic surface

## Status

Accepted, 2026-08-22. Extends ADR 0008 with a third backend kind. ADR 0008 stays
correct for the two kinds it defined; nothing in it is reversed.

## Context

ADR 0008 gave `_resolve_qualified` two kinds of backend:

- **rowless** (`copilot`, `auggie`): owns no taxonomy the index can see, so its
  prefix is authoritative for ANY bare id, including one indexed to another
  backend. This is what makes `copilot/gpt-5.5` reach Copilot's gpt-5.5 while bare
  `gpt-5.5` reaches codex.
- **rows-owning** (`codex`, `deepseek`, `claude`, `kimi`): owns concrete ids in the
  index, so a qualified id MUST name a model indexed to itself. `deepseek/gpt-5.5`
  is a conflict and fails closed.

Membership is not declared. It is derived: `_BACKENDS_WITH_ROWS` is
`frozenset(_MODEL_INDEX.values())`, so appearing in the index at all makes a
backend rows-owning.

A provider arrived that fits neither. The OpenCode Go subscription publishes a
**discoverable** catalog (a public `/v1/models` listing, 29 ids when probed on
2026-08-11) which **overlaps** several incumbents: it serves its own `kimi-k3`,
`deepseek-v4-pro` and `deepseek-v4-flash`, ids already indexed to the kimi and
deepseek backends and billed to different subscriptions.

Expressing that provider with the existing two kinds forces a choice between two
unacceptable outcomes:

1. **Seed its catalog** so bare ids route. It then appears in `_MODEL_INDEX`, so it
   is rows-owning by derivation, so `opencode/kimi-k3` resolves to None. The
   overlapping half of the catalog becomes unreachable even when the caller names
   the provider explicitly, which is the one thing the qualifier exists for.
2. **Do not seed it**, leaving it rowless. Every id is then reachable qualified, but
   no bare id routes at all, and the prefix fails open for ids the provider does
   not serve.

Seeding was also unsafe until recently: `_build_model_index` assigned
`index[key] = backend` with no conflict detection, so a duplicate was resolved by
insertion order and the loser vanished silently. `kimi-k3` resolved to kimi only
because the kimi seed loop ran last. OCG-G1 made a cross-backend claim fatal
(`ModelIndexConflictError`), which is the precondition for this ADR: without it,
adding an overlapping catalog would silently move a bare id to a different
subscription, credential and bill, with nothing observable in the request.

## Decision

1. **A third kind, declared rather than derived.** `_CATALOG_OWNING_BACKENDS` maps
   a backend to the frozen set of bare ids it serves. Being declared, it cannot be
   changed by an accident of index insertion order, which is precisely how the
   existing kinds can be misclassified.

2. **Behind its prefix, the declared catalog is authoritative, and only it.** A
   qualified id whose bare model is in the catalog resolves to that backend even
   when the bare id is indexed to an incumbent, so `<owner>/kimi-k3` reaches the
   owner. A bare model outside the catalog **fails closed**. This is deliberately
   narrower than rowless: a rowless backend trusts any id because the index cannot
   know what it serves, whereas a catalog owner has told us, so trusting an unknown
   id would be a fail-open bypass with no justification.

3. **The catalog branch is evaluated BEFORE the rowless and rows-owning branches.**
   Once a catalog owner's unique ids are seeded it also appears in
   `_BACKENDS_WITH_ROWS`, and the rows branch would then reject exactly the
   contested ids the prefix exists to reach. Ordering is load-bearing, not
   stylistic, and there is a test that fails when the branch is moved.

4. **Incumbency always wins for bare ids.** Seeding runs LAST and DEFERS: an id an
   incumbent already claims is skipped, not claimed. Bare routing for a catalog
   owner is therefore a convenience layered on top of the qualified id and never an
   override. Moving an established bare id would silently change which upstream
   subscription served a request and who was billed for it, while the request
   itself looked identical.

5. **Deference is not a conflict.** Skipping a contested id must not raise the
   `ModelIndexConflictError` that OCG-G1 introduced. That error means two backends
   both assert ownership; a catalog owner standing aside is a recorded decision.

6. **The claude family is untouched.** No new branch grants a route to it. ADR 0009
   already replaced the whole-id claude veto with index resolution, and ADR 0011
   makes a prefix naming a claude model legitimate for a provider that genuinely
   serves them. A claude id absent from a catalog owner's catalog is refused for the
   ordinary reason: it is outside the catalog.

## Consequences

- A provider whose catalog overlaps an incumbent is now expressible without
  reversing ADR 0008 and without disturbing any existing resolution path.
- Which ids a catalog owner reaches bare is a function of what the incumbents
  claim, so it changes when either side changes. That set must therefore be treated
  as an observable artifact with a fail-closed drift check rather than an
  implementation detail, otherwise a newly added incumbent id could quietly take
  over a bare id a catalog owner was serving. Committing that artifact is OCG-G3.
- `_CATALOG_OWNING_BACKENDS` ships EMPTY in this slice. The mechanism is proven
  against a synthetic fixture backend, so no live provider or credential is
  involved, and registering the first real catalog owner is a separate, reviewable
  change.
- A future provider that publishes no catalog stays rowless; one whose ids never
  overlap can remain rows-owning. The third kind is for overlap, not a replacement.
