---
title: Northstar A to B handoff for ollama-reverso-provider
status: complete
slug: ollama-reverso-provider
---

# Northstar A to B Handoff: ollama-reverso-provider

Completion: Implemented by PRs #116 through #120.

## Contract

- Spec: `docs/specifications/ACTIVE/ollama-reverso-provider.md`
- Work item: `.ai/work-intake/ollama-reverso-provider.md`
- Issue node: `issue:reverso-root:ollama-reverso-provider`
- Plan node: `plan:reverso-root:northstar-ollama-reverso-provider`
- Handoff node: `handoff:reverso-root:northstar-ollama-reverso-provider`
- Handoff path: `.ai/handoff/northstar-ollama-reverso-provider.md`
- Manifest record: `optional_branches[id=northstar-handoff-ollama-reverso-provider]` in `.ai/workflows/repo-workflow.json`
- Traceability graph: `.ai/traceability/graph.json`

## Sliced goals

| Goal | Work item | Type | Status | Blocked by |
|---|---|---|---|---|
| OLLAMA-RP-G1 | `ollama-reverso-provider-g1-codex-responses.md` | AFK | completed in PR #116 | none |
| OLLAMA-RP-G2 | `ollama-reverso-provider-g2-claude-messages.md` | AFK | completed in PR #118 | OLLAMA-RP-G1 |
| OLLAMA-RP-G3 | `ollama-reverso-provider-g3-convergence-refresh-docs.md` | AFK | completed in PR #119 | OLLAMA-RP-G2 |
| OLLAMA-RP-G4 | `ollama-reverso-provider-g4-attended-proof-hardening.md` | HITL | completed in PR #120 | OLLAMA-RP-G3 |

## Execution

Autobahn consumes the ready goals in this handoff and ships each goal through its governed one-PR loop. Completed and deferred goals are not implementation inputs.
