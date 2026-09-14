---
title: Northstar A to B handoff for opencode-go-provider
status: active
slug: opencode-go-provider
---

# Northstar A→B Handoff: opencode-go-provider

**A (now):** The OpenCode Go subscription is reachable only through `ocgo`, a
standalone Go CLI with its own proxy, credential store, catalog and launchers.
Reverso cannot see it: no Headroom, no `/usage`, no profile sync, no shared
credential handling.

**B (target):** OpenCode Go is the first-class Reverso backend `opencode`, serving
Codex through the Responses gateway and Claude Code through the Anthropic Messages
surface, with all 29 catalog ids reachable qualified and the unique ids reachable
bare, without disturbing the DeepSeek, Kimi or Codex taxonomies.

## Contract

- Spec: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Work item: `.ai/work-intake/opencode-go-provider.md`
- Issue node: `issue:reverso-root:opencode-go-provider`
- Plan node: `plan:reverso-root:northstar-opencode-go-provider`
- Handoff node: `handoff:reverso-root:northstar-opencode-go-provider`
- Handoff path: `.ai/handoff/northstar-opencode-go-provider.md`
- Manifest record: `optional_branches[id=northstar-handoff-opencode-go-provider]` in `.ai/workflows/repo-workflow.json`
- Traceability graph: `.ai/traceability/graph.json` (schema_version 1.1)
- Consensus evidence: none — slicing was authored inline by northstar, not by a `ralplan` consensus run

## Sliced goals

| Goal | Work item | Type | State | Blocked by |
|---|---|---|---|---|
| OCG-G1 | `opencode-go-provider-g1-index-conflict-detection.md` | AFK | ready-for-agent | none |
| OCG-G2 | `opencode-go-provider-g2-catalog-owning-routing.md` | AFK | ready-for-agent | OCG-G1 |
| OCG-G3 | `opencode-go-provider-g3-credentials-catalog-protocol.md` | AFK | ready-for-agent | OCG-G1 |
| OCG-G4 | `opencode-go-provider-g4-codex-responses-vertical.md` | AFK | ready-for-agent | OCG-G2, OCG-G3 |
| OCG-G5 | `opencode-go-provider-g5-claude-messages-vertical.md` | AFK | ready-for-agent | OCG-G4 |
| OCG-G6 | `opencode-go-provider-g6-convergence-refresh-docs.md` | AFK | ready-for-agent | OCG-G5 |
| OCG-G7 | `opencode-go-provider-g7-attended-proof-hardening.md` | HITL | ready-for-human | OCG-G6 |

G2 and G3 are independent of each other and both unblock on G1; everything after
G4 is a chain. All slices serialize on the routing authority and the generated
catalogs, so rebase between merges regardless of the logical graph.

## Locked decisions

D1 native adapter, no `ocgo` at runtime. D2 subscription key available, live
verification in scope. D3 qualified always plus bare where unique, via a new
catalog-owning backend kind (ADR). D4 all 29 ids, no curation. D5 Codex routes
through Reverso. D6 Headroom inherited at the default. D7 quota/429 fails closed,
never falls back. D8 bare-exposure set is a committed artifact with a fail-closed
`--check`. D9 collision transfer fails closed.

## Execution

Autobahn consumes the ready goals in this handoff and ships each through its
governed one-PR loop, in reverso's own checkout. G7 is HITL and stops at
ready-for-human. Regenerating this file with `handoff-write.sh` restores the
contract block but not this table.
