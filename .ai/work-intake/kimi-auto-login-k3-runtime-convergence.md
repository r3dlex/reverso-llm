---
title: Work item for Kimi automatic login and K3 runtime convergence
status: ready-for-agent
category: enhancement
slug: kimi-auto-login-k3-runtime-convergence
---

# Work Item: Converge Kimi login, K3, and runtime provenance

- **Traceability node:**
  `issue:reverso-root:kimi-auto-login-k3-runtime-convergence`
- **Spec:**
  [`docs/specifications/ACTIVE/kimi-auto-login-k3-runtime-convergence.md`](../../docs/specifications/ACTIVE/kimi-auto-login-k3-runtime-convergence.md)
- **State:** `ready-for-agent`
- **Category:** `enhancement`
- **Owner:** unassigned
- **Surface scope:** Responses, Anthropic, Codex sync, and LaunchAgent install
- **Hosted reconciliation:** local-first; no hosted issue was authorized

## Summary

Reverso currently fails missing Kimi OAuth with retryable HTTP 502, while its
running LaunchAgent exposes stale K2.5 authority and regenerates the Codex
profile and catalog from that stale runtime. Add a gateway-wide supervisor for
the official `kimi login` command, preserve Kimi CLI ownership of OAuth, and
converge the installed gateway and all Codex-facing metadata on K3.

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S1 | Govern shared login coordinator and first resumed Responses request | AFK | ready-for-agent | none |
| S2 | Prove cross-surface single-flight and bounded lifecycle cleanup | AFK | blocked | S1 |
| S3 | Converge K3 model exposure, profile, catalog, and context metadata | AFK | ready-for-agent | none |
| S4 | Govern canonical LaunchAgent provenance and deployment drift checks | AFK | blocked | S1, S3 |
| S4A | Govern an isolated Kimi home in deployment provenance | AFK | blocked | S4 |
| S5 | Deploy, sync, and perform live OAuth acceptance | HITL | blocked | S2, S4A |

## Acceptance criteria

1. One shared `kimi login` child serves concurrent missing-auth requests across
   both protocol surfaces.
2. The first request waits, reloads the CLI-owned artifact, and resumes.
3. Every failure, cancellation, timeout, and shutdown path is bounded,
   secret-free, and reaps the child.
4. Kimi exposure, upstream translation, profile, and catalog converge on
   `kimi-k3`, `k3`, and context window `1048576`.
5. Deployment provenance tests prevent a stale LaunchAgent checkout from
   restoring K2.5.
6. Governance authorizes official CLI supervision while continuing to prohibit
   OAuth implementation inside Reverso.
7. The generated Kimi profile and its single-entry catalog are explicitly
   tested.
8. Local and hosted verification is green before any merge.
9. Automatic merge occurs only with resolved reviews and valid host-policy
   authority. Otherwise the work stops at `ready-for-human`.
10. The proxy uses a mode-`0700` isolated Reverso Kimi home bound through
    deployment provenance, while the daemon and `~/.kimi-code` remain outside
    that authority.

## Out of scope

- Reimplementing Kimi OAuth endpoints.
- Changing the frozen `ProviderAdapter` protocol.
- Adding a runtime dependency without approval.
- Reading or recording credential contents.
- Reusing archived Codex OAuth handoffs.
- Disturbing unrelated dirty work.
