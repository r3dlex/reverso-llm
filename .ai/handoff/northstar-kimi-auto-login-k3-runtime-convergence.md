---
title: Northstar A to B handoff for kimi-auto-login-k3-runtime-convergence
status: complete
slug: kimi-auto-login-k3-runtime-convergence
---

# Northstar A to B Handoff: kimi-auto-login-k3-runtime-convergence

Completion: Implemented by PRs #97 through #108.

## Contract

- Spec: `docs/specifications/ACTIVE/kimi-auto-login-k3-runtime-convergence.md`
- Work item: `.ai/work-intake/kimi-auto-login-k3-runtime-convergence.md`
- Issue node: `issue:reverso-root:kimi-auto-login-k3-runtime-convergence`
- Plan node: `plan:reverso-root:northstar-kimi-auto-login-k3-runtime-convergence`
- Handoff node: `handoff:reverso-root:northstar-kimi-auto-login-k3-runtime-convergence`
- Handoff path: `.ai/handoff/northstar-kimi-auto-login-k3-runtime-convergence.md`
- Manifest record: `optional_branches[id=northstar-handoff-kimi-auto-login-k3-runtime-convergence]` in `.ai/workflows/repo-workflow.json`
- Traceability graph: `.ai/traceability/graph.json`

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S1 | Govern shared login coordinator and first resumed Responses request | AFK | completed in PR #97 | none |
| S2 | Prove cross-surface single-flight and bounded lifecycle cleanup | AFK | completed in PR #102 | S1 |
| S3 | Converge K3 model exposure, profile, catalog, and context metadata | AFK | completed in PR #98 | none |
| S4 | Govern canonical LaunchAgent provenance and deployment drift checks | AFK | completed in PR #100 | S1, S3 |
| S4A | Govern an isolated Kimi home in deployment provenance | AFK | completed in PR #104 | S4 |
| S5 | Deploy, sync, and perform live OAuth acceptance | HITL | completed in PR #105 | S2, S4A |

## Execution

Autobahn consumes the ready goals in this handoff and ships each goal through its governed one-PR loop. Completed and deferred goals are not implementation inputs.
