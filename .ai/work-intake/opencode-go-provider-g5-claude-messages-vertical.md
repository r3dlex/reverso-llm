---
title: OCG-G5: Claude Code Messages vertical
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-go-provider-g5-claude-messages-vertical
owner: unassigned
execution_type: AFK
---

# OCG-G5: Claude Code Messages vertical

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

The Anthropic-native transport and the Claude Code surface.

Add the `/messages` transport for the model families that require it, carrying the
strict-upstream normalization ported from `ocgo`: strip `thinking`, `reasoning`,
`reasoning_effort`, `effort`, `level`, `depth`, `output_config`, and normalize
`system`. Select the transport per model from the measured table (OCG-G3).

Expose the backend on the Anthropic surface: `SURFACE_BACKENDS["anthropic"]`,
`anthropic-opencode-<id>` discovery aliases per ADR 0010, a catalog-driven
discovery listing replacing the curated tuple pattern, a `claude-opencode`
launcher catalog row, and per-model context windows for the auto-compact window.

## Acceptance criteria

- [ ] Each stripped field has a test proving the upstream rejects the request when it is present, so no strip is speculative.
- [ ] A `/messages`-only model completes a Claude Code turn; a chat-completions model still routes correctly on the same surface.
- [ ] All 29 ids are selectable in the picker via discovery aliases, generated from the catalog rather than a hand-maintained tuple.
- [ ] Bare routing is available exactly for the unique ids, and no pre-existing bare id changed backend.
- [ ] The launcher sets the context and auto-compact window per selected model.
- [ ] Tool-heavy turn fidelity is recorded, quantifying the Anthropic to Responses to Anthropic round-trip cost.

## Blocked by

OCG-G4
