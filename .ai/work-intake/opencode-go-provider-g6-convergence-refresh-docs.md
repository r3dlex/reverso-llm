---
title: OCG-G6: convergence, refresh and operator contracts
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-go-provider-g6-convergence-refresh-docs
owner: unassigned
execution_type: AFK
---

# OCG-G6: convergence, refresh and operator contracts

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

Make the provider survive refresh, restore, uninstall and drift.

Wire the bare-exposure `--check` into CI, wire catalog refresh into the existing
`catalog_refresh` job so a new OpenCode model appears without a code change, and
extend `cross_check_anthropic_models` to fail on a newly detected collision rather
than resolving it. Record the domain terms in `CONTEXT.md` and document the
operator surface: provisioning the key, what a 429 looks like, and how to tell
which subscription served a request.

## Acceptance criteria

- [ ] A model added upstream appears after refresh with no code change; a model removed disappears without breaking a saved selection.
- [ ] A newly introduced collision fails CI with a message naming both backends.
- [ ] `CONTEXT.md` gains the catalog-owning backend and bare-exposure terms.
- [ ] Uninstall and restore leave no OpenCode artifact and no user-owned file modified.
- [ ] Operator documentation covers key provisioning, quota failure and per-request provider attribution.

## Blocked by

OCG-G5
