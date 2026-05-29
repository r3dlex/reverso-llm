---
type: agent-guide
project: reverso
area: docs
last_updated: 2026-05-27
---

<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-27 | Updated: 2026-05-27 -->

# docs/

Specification documents for the Reverso gateway. Read before writing code.

## Key files

| File | Purpose |
|---|---|
| `01-brd.md` | Business requirements, rationale, Q1-Q18 locked decisions |
| `02-prd.md` | All functional requirements by area (F-INB, F-ROUTE, F-CLI, F-SESS, F-ENV, F-CFG, F-OPS) |
| `03-architecture.md` | Component architecture, runtime topology, request flow diagrams, failure modes |
| `04-mvp.md` | Phased plan: Phase 0 spike through Phase 4 hardening with exit criteria |
| `spike-notes.md` | (Created during Phase 0) Answers to Q-Spike-1 through Q-Spike-6 |

## For agents

These files are specification, not code. Do not modify them to match the implementation. If the implementation diverges from spec, update the implementation or open a discussion. Spec docs are the source of truth for design decisions.

The locked decisions in `01-brd.md` Section 6 (Q1-Q18) are not open for re-litigation. They reflect deliberate trade-offs documented with rationale.

<!-- MANUAL: -->
