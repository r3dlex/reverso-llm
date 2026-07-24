---
title: Northstar A to B handoff for codex-encrypted-content-include-compatibility
status: complete
slug: codex-encrypted-content-include-compatibility
---

# Northstar A to B Handoff: codex-encrypted-content-include-compatibility

Completion: Implemented by PRs #90, #91, and #92.

## Contract

- Spec: `docs/specifications/ACTIVE/codex-encrypted-content-include-compatibility.md`
- Work item: `.ai/work-intake/codex-encrypted-content-include-compatibility.md`
- Issue node: `issue:reverso-root:codex-encrypted-content-include-compatibility`
- Plan node: `plan:reverso-root:northstar-codex-encrypted-content-include-compatibility`
- Handoff node: `handoff:reverso-root:northstar-codex-encrypted-content-include-compatibility`
- Handoff path: `.ai/handoff/northstar-codex-encrypted-content-include-compatibility.md`
- Manifest record: `optional_branches[id=northstar-handoff-codex-encrypted-content-include-compatibility]` in `.ai/workflows/repo-workflow.json`
- Traceability graph: `.ai/traceability/graph.json`

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S0 | Governed Northstar graph and handoff reconciliation command | AFK | completed in PR #90 | none |
| S1 | Exact-sentinel compatibility contract across the six-provider matrix | AFK | completed in PR #91 | none |
| S2 | Cross-provider integration proof and parity documentation | AFK | completed in PR #92 | none |
| S3 | Provider-health follow-up classification | HITL or separate issues | deferred | external credentials, quota, and availability |

## Execution

Autobahn consumes the ready goals in this handoff and ships each goal through its governed one-PR loop. Completed and deferred goals are not implementation inputs.
