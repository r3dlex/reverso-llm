---
title: OpenCode Go subscription as a Reverso provider
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-go-provider
owner: unassigned
---

# Work Item: OpenCode Go subscription as a Reverso provider

## Traceability

- Issue node: `issue:reverso-root:opencode-go-provider`
- Consensus evidence: none — slicing was authored inline by northstar, not by a `ralplan` consensus run
- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: None
- Version impact: additive routed-provider feature plus one routing-authority extension (ADR)
- Hosted reconciliation: local-first; no hosted tracker is configured for this repo (`.ai/host-policy/` absent), so no hosted issue was authorized

## What to build

Serve the OpenCode Go subscription as the Reverso backend `opencode`: Codex through
the Responses gateway, Claude Code through the Anthropic Messages surface,
credentials in the Keychain, Headroom applied before dispatch, quota failures
fail-closed. All 29 catalog ids are reachable qualified (`opencode/<id>`) and the
ids unique to OpenCode are additionally reachable bare, which requires a new
catalog-owning backend kind in the routing authority.

`ocgo` is not run at runtime; it is used as a contract oracle for the per-model
protocol split and the strict-upstream normalization.

## Sliced goals

| Goal | Work item | Type | Status | Blocked by |
|---|---|---|---|---|
| OCG-G1 | `opencode-go-provider-g1-index-conflict-detection.md` | AFK | completed in PR #123 | none |
| OCG-G2 | `opencode-go-provider-g2-catalog-owning-routing.md` | AFK | ready-for-agent | OCG-G1 |
| OCG-G3 | `opencode-go-provider-g3-credentials-catalog-protocol.md` | AFK | ready-for-agent | OCG-G1 |
| OCG-G4 | `opencode-go-provider-g4-codex-responses-vertical.md` | AFK | ready-for-agent | OCG-G2, OCG-G3 |
| OCG-G5 | `opencode-go-provider-g5-claude-messages-vertical.md` | AFK | ready-for-agent | OCG-G4 |
| OCG-G6 | `opencode-go-provider-g6-convergence-refresh-docs.md` | AFK | ready-for-agent | OCG-G5 |
| OCG-G7 | `opencode-go-provider-g7-attended-proof-hardening.md` | HITL | ready-for-human | OCG-G6 |

## Acceptance criteria

- [ ] All seven goal records complete in dependency order with recorded red-green evidence.
- [ ] `opencode/<id>` reaches OpenCode for every one of the 29 catalog ids, including the ids DeepSeek and Kimi already claim bare.
- [ ] Bare routing is granted only for ids unique to OpenCode, and no existing bare id changes backend.
- [x] A collision between two backends over one bare id fails closed rather than resolving silently.
- [ ] The per-model protocol split is measured against both upstreams, not copied from `ocgo`.
- [ ] Headroom runs exactly once per eligible request; a 429 surfaces as an error and never falls back to another provider.
- [ ] Local and hosted exact-head gates are green before each merge.
