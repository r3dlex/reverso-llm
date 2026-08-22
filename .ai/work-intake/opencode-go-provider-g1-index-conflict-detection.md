---
title: OCG-G1: fail closed on model-index collisions
status: complete
state: complete
category: enhancement
slug: opencode-go-provider-g1-index-conflict-detection
owner: unassigned
execution_type: AFK
---

# OCG-G1: fail closed on model-index collisions

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

Make a bare-id collision between two backends detectable before any overlapping
catalog is introduced. `_build_model_index` currently does `index[key] = backend`
with no conflict detection: last writer wins, silently. Kimi is seeded last, so
`kimi-k3 -> kimi` today by ordering accident rather than by decision.

Raise on a duplicate bare id claimed by two different backends, and prove the
current backend set is collision-free.

## Acceptance criteria

- [x] A synthetic duplicate bare id across two backends fails the index build with a message naming the id and both backends.
- [x] The real backend set (claude, codex, deepseek, kimi rows and seeds) builds clean, proving the guard is not vacuous.
- [x] `cross_check_anthropic_models` and its independently rebuilt `fresh_index` observe the same rule, so resolution and the build-time lint cannot diverge.
- [x] No behaviour change for any currently resolvable id.

## Why first

This is the safety net for OCG-G2 and OCG-G3. Introducing a 29-model overlapping
catalog on top of a silently-overwriting index is how bare `kimi-k3` would move
subscription without anyone noticing. Needs no credential.

## Blocked by

None - can start immediately

## Shipped via

Completed in PR #123. Exact-head local and hosted gates passed, and the observed
red-then-green evidence is recorded at `.ai/evidence/OCG-G1.json`.
