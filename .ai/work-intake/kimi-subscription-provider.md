---
title: Kimi subscription provider for Codex and Claude Code
status: ready-for-human
category: enhancement
state: ready-for-human
traceability_node: issue:reverso-root:kimi-subscription-provider
---

# Work Item: Kimi Subscription Provider for Codex and Claude Code

## Traceability

- BRD: `docs/01-brd.md`
- PRD/spec: `docs/specifications/ACTIVE/kimi-subscription-provider.md`
- Architecture: `docs/architecture/adr/0017-kimi-code-oauth-provider.md`
- Deep interview: `.omx/specs/deep-interview-kimi-subscription-provider.md`
- Adversarial review: `.omx/plans/grill-kimi-subscription-provider.md`
- Parent: None
- Version impact: new first-party subscription provider and client profiles
- Hosted reconciliation: local-first; no hosted tracker authorization established

## What to build

Harden the existing Kimi Code OAuth prototype and ship it as a first-party
Reverso provider for OpenAI Responses and Anthropic Messages. Make it selectable
from Codex and usable from Claude Code, preserve provider-agnostic Headroom, and
block merge until a credentialed local proof passes through both protocol and
both client paths.

## Tracer-bullet slices

| Slice | Title | Type | State | Blocked by |
|---|---|---|---|---|
| S1 | Harden prototype auth, transport, translation, and secret safety | AFK | complete | None |
| S2 | Complete Responses, Messages, capability, quarantine, and Headroom parity | AFK | complete | S1 |
| S3 | Add Kimi Codex profile and provider-scoped catalog synchronization | AFK | complete | S1 |
| S4 | Add reversible Claude Code configuration and Kimi model discovery proof | AFK | complete | S2 |
| S5 | Run credentialed end-to-end subscription proof and enforce merge gate | HITL | complete | S2, S3, S4 |
| S6 | Close operator docs, restart, rollback, and release evidence | AFK | ready-for-human | S5 |

## Acceptance criteria

- [x] Kimi OAuth is primary and explicit bearer auth is fallback only.
- [x] Responses and Messages dispatch through the same frozen provider adapter.
- [x] Kimi never falls through to LiteLLM and Reverso remains loopback-only.
- [x] Codex generates a Kimi provider/profile/catalog without replacing bare
      built-in GPT defaults or leaking other provider catalogs.
- [x] Claude Code discovers and routes Kimi through the existing Messages
      surface without destructive global settings mutation.
- [x] Headroom compression is proven before Kimi dispatch on unary and streaming
      requests for both inbound protocols.
- [x] Offline unit, integration, compilation, and diff gates pass; unrelated
      Copilot failures remain separately reported.
- [x] Credentialed local Responses, Messages, Codex, and Claude Code smoke passes
      without tokens, prompts, or response content in logs or evidence.
- [x] Merge remains blocked until the credentialed proof is green.

## Version impact

This adds a user-selectable subscription provider and local client profiles. It
does not change built-in Codex provider names, the frozen adapter contract,
loopback binding, or the existing Claude OAuth backend.

## Blocked by

Implementation and trusted-machine proof are complete. Final merge remains
blocked on independent final review, exact-head hosted CI, and host-policy merge
authority.

## Release evidence

- Offline: 770 unit tests passed; 252 integration tests passed with 6 skips.
- Live: all eight allowlisted Kimi proof rows passed on `127.0.0.1:64946`.
- Install: generated plists validated and the installed candidate passed smoke.
- Rollback: both original plist hashes and working directories were restored;
  final readiness returned HTTP 200.
- Evidence: ignored manifests are mode `0600` and contain no credential,
  prompt, response-content, header, or raw-log fields.
- Delivery: release closure is draft PR #86 and remains unmerged.
