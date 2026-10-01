---
type: specification
project: reverso
title: Reverso convergence participant seam - client-sync planner decomposition
status: active
slug: reverso-convergence-participant-seam
date: 2026-08-23
---

# Reverso convergence participant seam

Source: improve-codebase-architecture survey 2026-08-23 (front: reverso,
candidates #1+#2; highest-impact pick). This spec stages the direction for
sliced implementation; it deliberately does NOT design interfaces yet.

## State A

`client_sync.py` (2193 lines) is one orchestrator whose `_plan()` hand-wires
every harness family's prepare-call shape, group assignment, ownership rules,
and conflict policy inline: Codex profiles/catalogs, Claude Code settings plus
launchers, OpenCode fragments, the RTK prerequisite, the external AGY catalog,
and Ollama inventory. Marker-ownership semantics now live partially in
`client_sync_mutations.py` (head-line markers, backup stamping - PR #145) and
partially per-family (`codex_sync._is_managed_profile_text` strict-prefix
contract, bounded rotation set). Adding a fourth harness means touching the
planner inventory union, validator tables, and a new module - Interface
knowledge of every family lives in one Implementation.

## State B

The Client convergence module is a small planner over a registry of
per-harness participants. Each participant owns its family's discovery,
prepare (exact bytes, no mutation), apply-under-lock, verify, and status
surface behind one narrow contract; the planner knows only ordering, shared
lock, and aggregate reporting. Owned-artifact lifecycle has exactly one home.
Adding a harness adds one participant module plus manifest data - zero planner
edits.

## Goals

1. Per-harness participants behind one contract; planner shrinks to
   composition (ordering, lock, aggregation).
2. One home for owned-artifact lifecycle decisions (marker contracts,
   backup/rotation policy) with per-family differences explicit, not
   accidental.
3. Test surface: participants tested through the contract; planner tested
   with fake participants.

## Non-goals

- Changing any generated artifact bytes or CLI surfaces.
- Touching the frozen ProviderAdapter Protocol (ADR 0002 section 11.3).
- Re-litigating ADR 0020 catalog-authority routing.

## Key decisions (premises to validate during slicing)

- D1: Codex strict-prefix marker contract stays semantically distinct from
  head-line markers; unification happens only as explicit named policies.
- D2: Internal-seam first: extract within `client_sync.py`, prove suite
  green, THEN move modules. No big-bang file surgery.
- D3: `surface_registry` taxonomy/routing split is a separate slice family,
  not part of this seam.

## Slices (shape; finalize at slicing time)

| ID | Slice | Verification |
|----|-------|--------------|
| C1 | Participant contract defined internally; codex+claude prepare orchestration moves behind per-family planner functions inside `client_sync.py` | full unit suite green; no public API change |
| C2 | Participants extracted to `src/reverso/convergence/` modules; client_sync becomes registry + ordering | unit suite green; planner LOC materially reduced |
| C3 | Owned-artifact policy table (marker kinds, rotation modes) single-homed | policy decision tests; drift impossible by construction |
| C4 | Fourth-harness dry run: register opencode fragments as full participant | planner diff is data-only |

## Traceability

- Parent work item: `issue:reverso:reverso-convergence-participant-seam`
- Depends on: PR #145 (owned-artifact primitives) - merged.
- Companion direction: surface_registry split tracked separately.
