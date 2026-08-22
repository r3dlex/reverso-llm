---
title: OR-G0 attended wire, policy, pricing, and replay spike
type: spike-decision-record
status: active
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g0
parent_work_item: .ai/work-intake/openrouter-reverso-provider-or-g0.md
parent_handoff: .ai/handoff/northstar-openrouter-reverso-provider.md
authoritative_spec: docs/specifications/ACTIVE/openrouter-reverso-provider.md
test_spec: .omx/plans/test-spec-openrouter-reverso-provider.md
fixtures_manifest: tests/fixtures/openrouter/manifest.json
fixtures_captures: scripts/spike/captures/
---

# OR-G0 attended wire, policy, pricing, and replay spike

verdict: GO

## Provenance

- Attended probe run on 2026-08-22 from `.worktrees/reverso-openrouter-g0`.
- `OPENROUTER_API_KEY` sourced from `$HOME/.zsh_exports` only; never passed as a
  command argument, never persisted, never echoed. Length recorded as 73 chars;
  prefix redacted everywhere in this record.
- Five redacted wire captures live under `scripts/spike/captures/`.
- Five schema-valid, redacted fixtures and the canonical manifest live under
  `tests/fixtures/openrouter/`.

## Probe budget

- Approved ceiling: US$0.10.
- Cumulative reported cost: US$0.00.
- Reported prompt and completion prices for `stealth/ox-alpha` are both `0`.
- All five captures returned HTTP 200 with `stealth/ox-alpha` echoed in the
  response body and a stable `gen-...` response id.
- Repricing is mandatory before every dispatch in OR-G1 and later goals.

## Endpoint-binding proof

- Mandatory routing is bound to a ZDR-compatible endpoint set.
- The catalog entry for `stealth/ox-alpha` carries `context_length=1048576`,
  `max_completion_tokens=131072`, and a `top_provider` block that limits to a
  single endpoint provider.
- Freshness bound: 600 seconds (10 minutes). OR-G1 must revalidate before
  every request; staleness revokes future admission and preserves already
  accepted streams.

## Wire captures (redacted)

| Capture | Method | Path | Status | Notes |
|---|---|---|---|---|
| `list_models_authenticated` | GET | `/api/v1/models` | 200 | Catalog entry redacted; pricing fields retained as `0` |
| `responses_unary` | POST | `/api/v1/responses` | 200 | Single JSON `response` object with a `reasoning` output item |
| `responses_streaming` | POST | `/api/v1/responses` | 200 | SSE via `data:`-only transport; ends in `response.incomplete` because `max_output_tokens=32` is consumed by reasoning |
| `messages_unary` | POST | `/api/v1/messages` | 200 | Single JSON `message` object with a `thinking` content block; `stop_reason=max_tokens` |
| `messages_streaming` | POST | `/api/v1/messages` | 200 | Canonical Anthropic SSE order: `message_start`, `content_block_start`, multiple `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop` |

All fixtures use the canonical envelope:

- `fixture`, `name`, `method`, `path`, `request`, `expected`
- `request.headers.Authorization` is `[REDACTED]`
- `request.body.input` and `request.body.messages` are `[REDACTED]`
- No prompt, body, capability handle, local path, or username appears in any
  fixture or capture

## Capability carriers (one per managed client)

- Codex: feasible. Carries `x-reverso-capability` via Codex CLI provider
  configuration. OR-G1 must strip the header before adapter dispatch.
- Claude Code: feasible. Carries `x-reverso-capability` via
  `ANTHROPIC_CUSTOM_HEADERS` on the managed launcher. OR-G1 must strip the
  header before adapter dispatch.
- If a future client cannot carry the header, weaker-privacy grants remain
  unavailable for that client and default-policy traffic still requires budgets
  and all other admission gates.

## Claude alias grammar

- Template: `anthropic-openrouter-{encoded_author_model}`.
- Encoding: `base64url` with padding stripped (avoids Anthropic's `claude-*`
  namespace collisions).
- Catalog authority is exact-match: the launcher catalog maps each alias to the
  unchanged upstream `<author>/<model>` bytes.
- Tail parsing and lowercased reconstruction are forbidden.

## Responses replay grammar

- Chain source: local Reverso storage.
- Forbidden upstream fields: `previous_response_id`, `store`.
- Unknown, incomplete, or partial chains fail locally. Text-only replay is
  forbidden.
- Reverso materializes the complete typed input and output items, including
  instructions, reasoning continuity data, tools, tool calls and results,
  images, ordering, and identifiers before issuing the next stateless upstream
  request.

## Risks and follow-ups carried into OR-G1

1. `stealth/ox-alpha` consumes the full `max_output_tokens` budget on internal
   reasoning. OR-G2 must size `max_output_tokens` accordingly and surface
   visible reasoning in the Codex profile before OR-G7 attended proof.
2. Streaming latency exceeded 60 seconds for a 32-token request during OR-G0.
   OR-G1 must measure streaming latency under realistic `max_output_tokens`
   budgets and decide whether the user-configured session timeout needs an
   attended override.
3. The catalog reports `pricing_prompt=0` and `pricing_completion=0`. OR-G1
   must still revalidate these values before every dispatch and treat a
   non-zero change as a fresh evidence event that blocks future admission
   unless explicitly approved.
4. The catalog returned `top_provider.is_moderated=false`. OR-G1 must surface
   this fact in the runtime config and revalidate on every catalog refresh.
5. The Responses streaming transport uses `data:`-only SSE (no separate
   `event:` lines). OR-G2 and OR-G3 must parse the embedded `type` field
   rather than relying on SSE event names.

## Decision

- All five redacted fixtures are present, schema-valid, and free of secrets,
  prompts, bodies, capabilities, paths, and usernames.
- The endpoint-binding proof, freshness bound, capability carriers, Claude
  alias grammar, and Responses replay grammar are all decided and recorded in
  `tests/fixtures/openrouter/manifest.json`.
- Probe budget remains under the US$0.10 ceiling with zero reported spend.
- The DAG proceeds to OR-G1.
