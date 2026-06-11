---
type: adr
project: reverso
id: 0004
title: DeepSeek Incremental Streaming via replay.replay_incremental
status: Accepted
date: 2026-06-11
supersedes: none
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md
  - docs/architecture/codex-responses-parity-matrix.md
  - .omc/plans/ralplan-parity-gap-closure.md
  - src/reverso/protocols/replay.py
  - src/reverso/protocols/adapters/deepseek.py
consensus: "ralplan revision 3 (Architect APPROVE, Critic APPROVE), user approved scope D1+D2+D3 on 2026-06-11"
---

# ADR 0004: DeepSeek Incremental Streaming via replay.replay_incremental

## Status

Accepted. Task D1 of `.omc/plans/ralplan-parity-gap-closure.md` (revision 3, user-approved on 2026-06-11). This ADR records the decision behind the streaming path the deepseek adapter now ships; the matrix row for `stream.incremental_deltas` on deepseek moves from `unsupported` to `translated` in the same commit.

## Decision

DeepSeek's `stream.incremental_deltas` lifts from `unsupported` to `translated`. The `DeepSeekAdapter._stream_response` consumes upstream `POST /chat/completions` with `stream=true` AND `stream_options={"include_usage": true}`, parses upstream SSE lines into a chunk async-iterator, and feeds the chunk stream into a new `replay.replay_incremental` helper that owns canonical envelope event emission, the finalize step (build envelope, capture terminal-chunk usage, write to the store), and terminal envelope events. The store-before-drain invariant currently enforced by `replay.replay_turn` is intentionally RELAXED for the incremental path: the store write moves from "before first yield" to "at finalize, after the last delta and before `response.completed`". The frozen `ProviderAdapter` Protocol is not touched; envelope event shape stays owned by `replay.py`; the deepseek adapter contributes only the upstream streaming call and the SSE-line parser.

## Drivers

1. Reduce time-to-first-token for deepseek on long completions (single biggest user-visible perceived-latency win available without a Protocol change).
2. Close a parity gap the matrix already flagged as "feasible future change."
3. Bring deepseek's streaming behavior into line with claude's B2 incremental path, so the canonical replay seam is the single owner of envelope event shape for every provider where the gateway buffers or streams through `replay.py`.

## Alternatives considered

- D2 (KEEP buffered, document as permanent): rejected. Upstream supports streaming natively, so leaving it on the floor is technical debt by omission; the matrix already flagged the cell as "feasible future change."
- D3 (hybrid by model id): rejected. Reasoner streaming is no harder than chat streaming once the parser handles `delta.reasoning_content`, so bifurcating on `deepseek-reasoner` vs `deepseek-chat` is unnecessary complexity.
- Re-emit envelope events inside `DeepSeekAdapter._stream_response` (the planner's revision 1 sketch with a `_translate_stream` helper that mints envelope events itself): rejected by the Architect. Envelope-shape ownership must stay in `replay.py` so a future buffered/incremental shape divergence cannot land in one provider's adapter without showing up in the replay seam tests.

## Why chosen

D1 (with the revision 2 architecture) is the minimum-blast-radius option that delivers the user-visible win, keeps envelope event ownership in `replay.py`, preserves every hard constraint that survives the change (Protocol frozen, mid-stream failure contract, no secrets), surfaces the one invariant it relaxes (store-before-drain) honestly, and lands the usage-on-completed requirement that codex needs for its token-tracking surface.

## Architecture

The deepseek adapter's `_stream_response` path is now:

1. `_build_body(request, stream=True)` sets `stream=true` AND `stream_options={"include_usage": true}` on the outbound chat-completions body. The `stream_options.include_usage=true` flag is REQUIRED: without it the deepseek OpenAI-compat layer leaves the terminal chunk's `usage` object null, and the completed envelope would report zero tokens, breaking the codex token-tracking surface.
2. `_call_upstream_stream(body)` opens `client.stream("POST", "/chat/completions")` and reads bytes line by line. The status code is checked at response headers, BEFORE any body iteration, so a 401 received at headers raises `DeepSeekError` and the gateway can synthesise a structured 502 (pre-emission branch). The line parser feeds `_parse_stream_event` which yields chunk dicts carrying `text` (`delta.content`), `reasoning_text` (`delta.reasoning_content`), `tool_calls` (raw upstream tool_call deltas with their index/function shape preserved), `usage` (translated via `_responses_usage` when the upstream sends the terminal usage chunk), and `done` (set only on the `data: [DONE]` sentinel; `finish_reason` does NOT mark `done` because deepseek's `include_usage` mode emits the usage chunk AFTER the finish_reason chunk and BEFORE `[DONE]`).
3. `_prime_upstream_stream` advances the upstream chunk iterator once so the connect+status check fires BEFORE any canonical envelope event is yielded, then re-injects the first usable chunk back into the iterator that `replay_incremental` consumes. This is the mechanism that keeps the pre-emission branch structurally before any 200 SSE header commit.
4. `replay.replay_incremental` is the single owner of envelope event emission. It yields `response.created` and `response.in_progress` immediately so the gateway commits the 200 header at first byte, then `response.output_item.added` (message shell, status=in_progress) and `response.content_part.added` (empty output_text), then one `response.output_text.delta` per non-empty content chunk while accumulating reasoning chunks and tool_call deltas. On the terminal chunk it calls a `finalize` closure provided by the adapter, persists the resulting envelope (the relaxed store-before-drain step), then yields `response.output_text.done`, `response.content_part.done`, `response.output_item.done`, optional per-item events for any function_call items surfaced by `finalize` (reusing the task #12 per-item helpers `_message_item_events` and `_function_call_item_events`), and `response.completed` with `usage`.
5. The adapter's `_finalize_streaming_envelope` closure synthesises a chat-shaped raw dict from accumulated state and passes it through the existing buffered-path `_map_completion`, so reasoning_content carry-forward and tool-call surfacing both reuse the buffered-path logic unchanged. The response/message ids are threaded through from the adapter so the on-wire `response.created.id` matches the envelope persisted at finalize-time.

## Consequences

- Positive: incremental deltas reach the codex client and any other Responses-API client. The matrix and surface JSON now match implementation.
- Positive: envelope event ownership stays in `replay.py`. A buffered-vs-incremental envelope drift can only land if both replay helpers diverge, which is visible in the replay test file rather than buried inside an adapter.
- Positive: terminal-chunk usage lands on `response.completed` via `stream_options.include_usage=true`, so codex's token-tracking surface and the C1 E2E TTFB-with-usage assertion both keep working.
- Positive: streamed function_call surfaces via canonical per-item events (`response.output_item.added` type=function_call, `response.function_call_arguments.delta`/`.done`, `response.output_item.done`). The replay seam helpers (added by task #12 for `replay_turn`) are reused at `replay_incremental` finalize-time, so the streamed tool-loop contract on deepseek matches the buffered one byte-for-byte at the event level.
- Negative (HONEST DISCLOSURE): store-before-drain is RELAXED on the incremental path. A client that aborts between the last delta and `response.completed` will not find the envelope in the store for later `previous_response_id` chaining or `GET /v1/responses/{id}`. Buffered providers (claude fallback, auggie, deepseek before D1) are unaffected; only the deepseek incremental path carries this relaxed semantic. Codex consumes the full stream, so it does not encounter this window; non-codex clients that abort early do. Pinned by `tests/unit/test_replay.py::test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta` and `tests/unit/test_deepseek_adapter.py::test_stream_store_write_happens_at_finalize_not_before_first_delta`.
- Negative: the deepseek path now has two upstream-call shapes (buffered unary for `create_response`, streaming for `stream_response`). Test surface grows by ~7 tests (replay incremental: 3, deepseek adapter: 5, integration errors: 2, integration function_call streaming: 1; some overlap with existing fixtures).
- Negative (small): a slow upstream first-byte delays the 200 header on the gateway (Responses spec permits this; matches the claude B2 behavior). Documented in the streaming-status section of the matrix.

## Pre-emission vs post-emission failure split

The streaming HTTP transport's failure surface has two structurally different branches; both are pinned by tests.

- Pre-emission: an HTTP non-2xx received at response.headers, OR any transport error before the first SSE byte reaches the gateway. `_call_upstream_stream` raises `DeepSeekError` while `_prime_upstream_stream` is taking its first step, BEFORE `replay_incremental` yields any envelope event. The gateway's `responses_app._stream` sees the exception while `started=False`, so it synthesises a structured 502 with body `{"error":{"message":"upstream provider error (DeepSeekError)","type":"server_error"}}` and zero SSE bytes on the wire. Pinned by `tests/unit/test_deepseek_adapter.py::test_stream_response_401_before_first_delta_returns_structured_error` and `tests/integration/test_responses_app_errors.py::test_deepseek_streaming_401_pre_emission_renders_structured_502`.
- Post-emission: a transport error during body iteration AFTER a `response.output_text.delta` has shipped. The exception propagates unwrapped through `replay_incremental`; `responses_app._stream` sees `started=True` and emits `response.failed` + `[DONE]` on the existing 200 SSE stream. No silent fallback to the buffered path is attempted (the buffered path is not reachable from here; the streamed envelope's content is only known incrementally). Pinned by `tests/unit/test_deepseek_adapter.py::test_stream_response_401_race_after_first_delta_surfaces_response_failed` and `tests/integration/test_responses_app_errors.py::test_deepseek_streaming_401_post_emission_renders_response_failed_done`.

## Follow-ups

- The C1 E2E Codex matrix script's TTFB gate is the natural acceptance check for D1 on a live deepseek run; its usage-on-completed assertion is the natural acceptance check for the `stream_options.include_usage=true` wiring.
- D3 (matrix language refresh) is the documentation pass that follows this ADR; it removes the "deepseek streaming is buffered" carve-out and updates the streaming-status section to point at this ADR.
- No follow-up is planned for the deferred items (auggie streaming, `POST /cancel`, cross-restart durability, codex TUI live polling); their rationale lives in the matrix carve-outs and in `.omc/plans/ralplan-parity-gap-closure.md`.

## Cross-cutting reminders the implementer must respect

- Bind: `127.0.0.1:64946` only. The streaming change does NOT alter the bind.
- Secrets: `DEEPSEEK_API_KEY` is read from env at call time; never logged. `_call_upstream_stream` logs only the status code on upstream errors, matching `_call_upstream`.
- Protocol: `src/reverso/protocols/adapter.py` is FROZEN. The streaming logic lives inside `DeepSeekAdapter` plus the new `replay.replay_incremental` helper in `replay.py`; no Protocol method signature changes.
- `responses_app.py` MUST NOT import `reverso.proxy.app` (runtime guard test enforces this; the change is far from that boundary but worth restating).
- Envelope event ownership: `replay.py` is the single owner of canonical envelope event shape. The deepseek adapter MUST NOT yield `response.created`, `response.in_progress`, `response.output_item.added`, `response.content_part.added`, `response.output_text.done`, `response.content_part.done`, `response.output_item.done`, or `response.completed` directly. Those events come from `replay.replay_incremental`.
- Store-before-drain RELAXATION: the new `replay.replay_incremental` writes to the store at finalize-time, NOT before the first event. This is intentional. The buffered `replay.replay_turn` keeps the pre-drain write. Both behaviors are pinned by replay tests; do not "fix" the relaxed one by tightening it without first revisiting this ADR.
- `stream_options.include_usage=true` is REQUIRED on the streaming branch. Without it the terminal chunk's `usage` object is null and the completed envelope reports zero tokens. The unit test `test_stream_response_terminal_chunk_usage_lands_on_completed_envelope` is the gate.
- Mid-stream failure contract: a failure after the first `response.output_text.delta` MUST surface as `response.failed` + `[DONE]`. The gateway's `_stream` already handles this; the adapter just lets the exception propagate. The pre-emission 401 branch is structurally different: the streaming HTTP transport surfaces a 401 received at response.headers BEFORE iterating the body so `responses_app._stream` synthesises a structured 502. Both branches are pinned by the tests listed above.
- The capability table JSON in both `src/reverso/protocols/data/` and `.omc/research/` must be updated together. The byte-identity test catches any divergence; D1's surface-JSON edit (`stream.incremental_deltas` -> deepseek = translated, and adding deepseek to the `response.function_call_arguments.delta` row) was applied identically to both files in this commit.
- No em dash (U+2014) or en dash (U+2013) in any new or modified file (this ADR, matrix doc, test docstrings, code comments). A python3 regex scan enforces it (RTK hook rewrites `rg` on this machine, so the scan uses `re.search('[\\u2013\\u2014]', ...)` with unicode escapes rather than the literal codepoints).
