# Work Item: reverso-convergence-participant-seam - client-sync planner decomposition

- **Traceability node:** `issue:reverso:reverso-convergence-participant-seam`
- **Spec:** [`docs/specifications/ACTIVE/reverso-convergence-participant-seam.md`](../../docs/specifications/ACTIVE/reverso-convergence-participant-seam.md)
- **State:** `planned` (improve-codebase-architecture 2026-08-23; autonomous directive)
- **Owner:** northstar -> autobahn (slicing + implementation pending operator go)
- **Spans repos:** `r3dlex/reverso` only

## Summary

Decompose the client-sync orchestrator (`client_sync.py`, 2193 lines) into a
small planner over per-harness participants (codex / claude-code / opencode /
rtk / external-agy / ollama), with owned-artifact lifecycle single-homed.
First slice is internal seams only; module extraction follows once the suite
proves the shape. Source: architecture survey candidates #1+#2.

## Acceptance criteria (target state)

- [ ] Planner knows ordering, shared lock, aggregation only.
- [ ] Each harness family exposes prepare/apply/verify behind one contract.
- [ ] Marker-contract and rotation-policy differences are named policies, not inline branches.
- [ ] Fourth-harness registration is data-only (C4 dry run).
- [ ] Full unit suite green at every slice boundary; no artifact bytes change.

## Slicing

Rough slice shape C1-C4 in the spec; finalize via ralpan/northstar before
autobahn. No interactive interview per standing autonomous directive;
adversarial grill pass to be run at slicing time, not skipped silently.
