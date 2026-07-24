---
title: Work item for Codex encrypted-content include compatibility
status: ready-for-agent
category: bug
slug: codex-encrypted-content-include-compatibility
---

# Work Item: Accept the exact Codex encrypted-content include sentinel

- **Traceability node:** `issue:reverso-root:codex-encrypted-content-include-compatibility`
- **Spec:** [`docs/specifications/ACTIVE/codex-encrypted-content-include-compatibility.md`](../../docs/specifications/ACTIVE/codex-encrypted-content-include-compatibility.md)
- **State:** `ready-for-agent`
- **Category:** `bug`
- **Owner:** unassigned
- **Surface scope:** standalone, Reverso Responses gateway
- **Hosted reconciliation:** local-first; no hosted issue was authorized

## Summary

Codex 0.145.0 unconditionally adds
`include: ["reasoning.encrypted_content"]` to Responses requests. Reverso
currently rejects that request shape for Auggie, Claude, DeepSeek, and Kimi
before normalization or adapter dispatch. Copilot and Codex-direct accept it.

Add a narrowly governed compatibility exception for the exact Codex sentinel.
For translated providers it is a documented no-op removed by the existing
normalizer. Preserve fail-closed handling for every other unsupported include
request.

The governed mechanism is a distinct
`include.reasoning.encrypted_content` feature emitted only for structural
equality with the exact sentinel. Every other non-empty list-valued include
continues to emit generic `include`, which remains unsupported for the four
affected translated providers.

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S0 | Governed Northstar graph and handoff reconciliation command | AFK | ready-for-agent | none |
| S1 | Exact-sentinel compatibility contract across the six-provider matrix | AFK | blocked | S0 |
| S2 | Cross-provider integration proof and parity documentation | AFK | blocked | S1 |
| S3 | Provider-health follow-up classification | HITL or separate issues | deferred | external credentials, quota, and availability |

## Acceptance criteria

1. Exact-sentinel streaming and non-streaming requests pass the feature gate for
   Auggie, Claude, DeepSeek, and Kimi.
2. Copilot gate acceptance remains green and the shared normalizer continues to
   remove the sentinel before adapter dispatch; no upstream forwarding claim is
   made.
3. Codex-direct remains a route-level control separate from the rectangular
   `codex` parity-table column; `/codex/v1/responses` remains excluded.
4. Mixed or unsupported include values retain the canonical structured 400 for
   Auggie, Claude, DeepSeek, and Kimi; Copilot retains its existing generic
   include behavior.
5. Tests distinguish the compatibility gate from independent provider-health
   failures.
6. The parity documentation describes the exact-value no-op and explicitly
   disclaims encrypted-reasoning continuity for translated providers.
7. A repo-owned reconciliation command deterministically creates or repairs the
   issue, plan, and handoff nodes, their edges, the workflow-manifest record,
   and a frontmatter-compliant handoff.

## Out of scope

- Creating or refreshing Claude or Kimi credentials.
- Purchasing or changing Auggie quota.
- Solving DeepSeek availability or latency.
- Changing the frozen `ProviderAdapter` protocol.
