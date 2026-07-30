---
title: Northstar A to B handoff for reverso-install-profile-catalog-headroom-convergence
status: active
slug: reverso-install-profile-catalog-headroom-convergence
---

# Northstar A to B Handoff: reverso-install-profile-catalog-headroom-convergence

## Contract

- Spec: `docs/specifications/ACTIVE/reverso-install-profile-catalog-headroom-convergence.md`
- Work item: `.ai/work-intake/reverso-install-profile-catalog-headroom-convergence.md`
- Issue node: `issue:reverso-root:reverso-install-profile-catalog-headroom-convergence`
- Plan node: `plan:reverso-root:northstar-reverso-install-profile-catalog-headroom-convergence`
- Handoff node: `handoff:reverso-root:northstar-reverso-install-profile-catalog-headroom-convergence`
- Handoff path: `.ai/handoff/northstar-reverso-install-profile-catalog-headroom-convergence.md`
- Manifest record: `optional_branches[id=northstar-handoff-reverso-install-profile-catalog-headroom-convergence]` in `.ai/workflows/repo-workflow.json`
- Traceability graph: `.ai/traceability/graph.json`

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S1 | Lock convergence contracts, regression tests, and the current installation baseline | AFK | ready-for-agent | none |
| S2 | Introduce the supported-surface manifest, unified client command, RTK convergence, and command documentation | AFK | ready-for-agent | S1 |
| S3 | Install a twice-daily short-lived catalog refresh LaunchAgent | AFK | ready-for-agent | S2 |
| S4 | Expand process-local embedded Headroom aggregates and publish the usage schema | AFK | ready-for-agent | S1 |
| S5 | Publish and prove the canonical end-to-end install, refresh, profile, catalog, and usage runbook | AFK | ready-for-agent | S2, S3, S4 |

## Execution

Autobahn consumes the ready goals in this handoff and ships each goal through its governed one-PR loop. Completed and deferred goals are not implementation inputs.
