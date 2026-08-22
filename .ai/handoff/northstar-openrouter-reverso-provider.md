---
title: Northstar A to B handoff for openrouter-reverso-provider
status: active
slug: openrouter-reverso-provider
---

# Northstar A to B Handoff: openrouter-reverso-provider

## Contract

- Spec: `docs/specifications/ACTIVE/openrouter-reverso-provider.md`
- Work item: `.ai/work-intake/openrouter-reverso-provider.md`
- Issue node: `issue:reverso-root:openrouter-reverso-provider`
- Plan node: `plan:reverso-root:northstar-openrouter-reverso-provider`
- Handoff node: `handoff:reverso-root:northstar-openrouter-reverso-provider`
- Handoff path: `.ai/handoff/northstar-openrouter-reverso-provider.md`
- Manifest record: `optional_branches[id=northstar-handoff-openrouter-reverso-provider]` in `.ai/workflows/repo-workflow.json`
- Traceability graph: `.ai/traceability/graph.json`

## Sliced goals

| Goal | Work item | State | Blocked by |
|---|---|---|---|
| OR-G0 | `.ai/work-intake/openrouter-reverso-provider-or-g0.md` | ready-for-human | None |
| OR-G1 | `.ai/work-intake/openrouter-reverso-provider-or-g1.md` | ready-for-agent | OR-G0 GO |
| OR-G2 | `.ai/work-intake/openrouter-reverso-provider-or-g2.md` | ready-for-agent | OR-G1 |
| OR-G3 | `.ai/work-intake/openrouter-reverso-provider-or-g3.md` | ready-for-agent | OR-G2 |
| OR-G4 | `.ai/work-intake/openrouter-reverso-provider-or-g4.md` | ready-for-agent | OR-G1 |
| OR-G5 | `.ai/work-intake/openrouter-reverso-provider-or-g5.md` | ready-for-agent | OR-G4 |
| OR-G6 | `.ai/work-intake/openrouter-reverso-provider-or-g6.md` | ready-for-agent | OR-G3 and OR-G5 |
| OR-G7 | `.ai/work-intake/openrouter-reverso-provider-or-g7.md` | ready-for-human | OR-G6 |

## Execution

Autobahn consumes the ready goals in this handoff and ships each goal through its governed one-PR loop. Completed and deferred goals are not implementation inputs.
