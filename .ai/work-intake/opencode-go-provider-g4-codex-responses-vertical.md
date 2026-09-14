---
title: OCG-G4: Codex Responses vertical
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-go-provider-g4-codex-responses-vertical
owner: unassigned
execution_type: AFK
---

# OCG-G4: Codex Responses vertical

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

The first usable vertical, and deliberately the Codex one: the adapter contract is
Responses-shaped, so Codex exercises no protocol round-trip and isolates adapter
defects from translation defects.

`OpenCodeAdapter` implementing `ProviderAdapter` with the chat-completions
transport (streaming and unary), registered under prefix `opencode` in
`compose._build_adapters()`, reachable at `/opencode/v1/responses`, plus the
generated isolated Codex profile and model catalog carrying per-model context
windows.

Headroom runs before dispatch at the inherited default. A 429 or quota refusal
surfaces as an error and never falls back to another backend.

## Acceptance criteria

- [ ] A Codex profile reaches `/opencode/v1/responses` and completes a turn against a chat-completions model.
- [ ] Streaming and unary both map to the canonical Responses shapes, including tool calls.
- [ ] Headroom runs exactly once per eligible request and preserves structured content.
- [ ] A 429 surfaces to the client with no provider fallback and no retry against another credential.
- [ ] The generated catalog carries per-model context windows from the enriched metadata.
- [ ] No OpenCode route is reachable on the Anthropic surface in this goal (negative proof).

## Blocked by

OCG-G2, OCG-G3
