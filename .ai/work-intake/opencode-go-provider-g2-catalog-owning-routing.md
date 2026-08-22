---
title: OCG-G2: catalog-owning backend kind (ADR)
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-go-provider-g2-catalog-owning-routing
owner: unassigned
execution_type: AFK
---

# OCG-G2: catalog-owning backend kind (ADR)

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

A new ADR and its implementation introducing a third backend kind to the routing
authority. ADR 0008 defines only *rowless* (prefix authoritative for any id) and
*rows-owning* (bare id must be indexed to itself), and membership is derived from
`frozenset(_MODEL_INDEX.values())`. Neither kind can express a backend with a
discoverable catalog that overlaps other backends.

A **catalog-owning** backend:
- is authoritative behind its prefix for any id in its declared catalog, so
  `opencode/kimi-k3` resolves even though bare `kimi-k3` belongs to Kimi;
- receives bare routing only for ids unique to it;
- never takes a bare id already indexed to another backend (incumbency wins).

Exercised against a synthetic catalog-owning fixture backend; no OpenCode adapter
and no credential are involved in this goal.

## Acceptance criteria

- [ ] New ADR recorded, superseding nothing and explicitly extending ADR 0008 with the third kind and the incumbency rule.
- [ ] `opencode/<id>` style qualified resolution succeeds for a fixture id that another backend owns bare.
- [ ] A bare id unique to the fixture backend resolves to it; a bare id owned by an incumbent still resolves to the incumbent.
- [ ] An id absent from the declared catalog fails closed behind the prefix, so the qualifier never becomes a fail-open bypass.
- [ ] `claude` remains fail-closed on every path, before and after the split.
- [ ] Backend-kind membership is declared rather than inferred from index membership.

## Blast radius

`_resolve_qualified` and `_MODEL_INDEX` are shared by claude, codex, deepseek,
kimi, copilot and auggie. Every existing resolution case keeps a regression test.

## Blocked by

OCG-G1
