---
type: open-questions
project: reverso
---

# Open Questions

## Anthropic API Surface (ralplan-anthropic-api-surface) - 2026-06-20

Resolved in consensus iteration 1 (decisions recorded in the plan and ADR 0006, no longer open):

- [x] ping cadence: DECIDED. One ping emitted after message_start, none for buffered-replay backends (claude/auggie/deepseek replay a complete turn with no idle gaps); idle-only pings on the copilot verbatim path. Pins Step 5 emission and the T3 order assertion.
- [x] cache_control on non-caching backends: DECIDED. Hard invalid_request_error on all M1 backends (none calls Anthropic upstream, so none can honor caching; silent acceptance would mislead clients). T6 has a deterministic expected outcome.
- [x] thinking-block support: DECIDED as a capability ceiling, not a pending classification. Structurally impossible in M1 because the Responses intermediate discards/never produces user-visible reasoning deltas (claude.py:596-623); classified unsupported -> gated-error on all three backends.

Still open:

- [ ] GET /v1/models display_name: derive from the OpenAI model id or use a curated map? Cosmetic; does not block implementation.
- [ ] Confirm against recorded Claude Code / Agent SDK traffic that the decided ping cadence (one ping after message_start for buffered replay) is accepted by the client; adjust Step 5 emission if the client demands a different cadence.
