---
title: OCG-G7: attended live proof and hardening
status: ready-for-human
state: ready-for-human
category: enhancement
slug: opencode-go-provider-g7-attended-proof-hardening
owner: unassigned
execution_type: HITL
---

# OCG-G7: attended live proof and hardening

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

The bounded attended proof that cannot be automated, because it needs a real
subscription, a real Codex session and a real Claude Code session.

Run one tool-heavy Codex turn and one tool-heavy Claude Code turn against a
`/messages` model and a `/chat/completions` model, compare tool_use fidelity
between the two surfaces, and observe quota behaviour at first-hand.

## Acceptance criteria

- [ ] A real Codex turn and a real Claude Code turn both complete against OpenCode, with transcripts recorded as evidence.
- [ ] The double-translation cost is quantified rather than asserted; if it is material, a passthrough seam is filed as follow-up work rather than built here.
- [ ] Observed quota or rate-limit behaviour is recorded, including whatever headers the upstream actually returns.
- [ ] Any defect found is filed with its reproduction, not fixed inside this goal.

## Blocked by

OCG-G6
