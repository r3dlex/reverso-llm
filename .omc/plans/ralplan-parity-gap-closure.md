---
title: "ralplan: Codex Responses parity gap closure (post-C2)"
status: approved
revision: 3
user_approval: "2026-06-11, scope D1+D2+D3 (D2 still subject to the architect live curl gate)"
phase: D
gateway_bind: 127.0.0.1:64946
providers: ["claude", "copilot", "auggie", "deepseek"]
inputs:
  - docs/architecture/codex-responses-parity-matrix.md
  - src/reverso/protocols/data/responses_parity_surface.json
  - .omc/research/responses-parity-surface.json
  - .omc/research/responses-parity-surface.md
  - .omc/research/auggie-streaming.md
  - .omc/research/codex-resume-probe.md
  - .omc/research/codex-model-picker.md
generated: 2026-06-10
planner_role: ralplan
loop: planner-architect-critic
scope: planning-only-no-code-edits
architect_review:
  verdict: REVISE
  revision_notes:
    - "store-before-drain is RELAXED for the incremental path; document the failure mode honestly"
    - "stream_options.include_usage=true is required to capture usage on the streaming branch"
    - "envelope event emission stays in replay seam via new replay.replay_incremental helper"
    - "add a pre-emission vs post-emission 401 race test on the streaming transport"
    - "sequencing: land team task #10 (parallel_tool_calls/tools partial reclassification) first, rebase D1 surface edits on top"
architect_rereview:
  verdict: APPROVE
  date: 2026-06-10
critic_review:
  verdict: APPROVE
  date: 2026-06-10
  minors_incorporated:
    - "task #10 wording updated from in-progress to landed (revision 3)"
    - "byte-identity test located precisely: tests/unit/test_feature_policy.py"
    - "D2 gained the explicit byte-identity mirror reminder"
    - "D3 verification swapped rg -P for python3 scan (RTK hook rewrites rg on this machine)"
---

# ralplan: Codex Responses parity gap closure (post-C2)

## RALPLAN-DR (Decision Record summary)

### Principles

1. The parity matrix is the contract; every lift moves a cell from `unsupported` to `translated`/`native` in `src/reverso/protocols/data/responses_parity_surface.json` AND the corresponding row of `docs/architecture/codex-responses-parity-matrix.md` in the same commit, never one without the other.
2. The frozen `ProviderAdapter` Protocol in `src/reverso/protocols/adapter.py` MUST NOT be touched. New capabilities live behind existing methods (`create_response`, `stream_response`, ...), behind injectable seams on the adapter classes themselves (mirror of `ClaudeAdapter.stream_cli_runner`), or behind new path branches in `responses_app._dispatch`.
3. Honesty over optics: a gap is only worth closing if the cost/benefit is favorable for the single local user. Recommending DEFER with rationale is a first-class outcome.
4. The mid-stream failure contract (`response.failed` SSE + `[DONE]`, no silent fallback after the first delta) and the structured 400 `unsupported_feature` contract are load-bearing and survive every change. Note: the store-before-drain invariant currently enforced by `replay.replay_turn` (`src/reverso/protocols/replay.py:194` writes to the store BEFORE yielding the first event) is intentionally RELAXED by D1 for the incremental path; the failure-mode trade is documented in the ADR section below and is the chief reason this revision exists.
5. No secrets in code, logs, or process env passed to subprocesses. No bind other than `127.0.0.1:64946`. No em dash (U+2014) or en dash (U+2013) anywhere.

### Decision drivers (top 3)

1. Codex client latency: incremental deltas reduce time-to-first-token (TTFT) for buffered providers. This is the single most user-visible parity gap because every other "unsupported" cell on claude/auggie/deepseek is gated by the adapter back-stop with a 400 the user rarely sees.
2. Cost-of-change vs. blast radius on the frozen Protocol: changes that fit inside one adapter file plus its test file are cheap; changes that touch `responses_app.py` routing or the parity surface JSON are medium; changes that would require a Protocol edit are out of scope.
3. Real-world cancellation demand on this gateway is near zero: codex itself does not POST `/cancel` (it drops the SSE connection), copilot pass-through is the only upstream that even has an authoritative cancel surface, and the local user has not requested it. The route stays unregistered unless a real client need appears.

### Viable options per gap (>= 2 per major gap)

#### Gap 1: deepseek incremental streaming (`stream.incremental_deltas`)

Current: `DeepSeekAdapter._stream_response` issues a single non-streaming chat call and runs the canonical 9-event `replay.replay_turn`. The upstream `POST /chat/completions` supports `stream=true` and emits OpenAI-style chat SSE chunks (one `choices[0].delta.content` segment per event).

- Option D1: Consume upstream `stream=true` and translate chat deltas into incremental Responses `output_text.delta` events through a new `replay.replay_incremental` seam that owns envelope event emission and the finalize step. RECOMMENDED.
  - Pros: lifts `stream.incremental_deltas` from `unsupported` to `translated`; envelope event shape stays owned by the replay seam (no envelope-shape drift between buffered and incremental paths); `reasoning_content` capture for `previous_response_id` chaining survives; no Protocol change; the adapter contributes only an SSE-line parser and the upstream streaming call.
  - Cons: ~150 lines of net new code (SSE line parser, chunk async-iterator, `replay.replay_incremental` helper, finalize/store step, tests); the store-before-drain invariant is RELAXED for the incremental path because the assistant text is only known at end-of-stream, so `store.put_response` runs at finalize-time AFTER the last `output_text.delta` and BEFORE `response.completed`. The failure mode this introduces: a client that disconnects after consuming deltas but before the finalize step completes will not find the envelope in the store via later `previous_response_id` chain or `GET /v1/responses/{id}`. Mitigation: codex client never disconnects mid-stream (it consumes the full SSE), and the matrix already documents the in-memory boundary; the failure mode is bounded to non-codex clients that abort early. Upstream failure mid-stream still surfaces as `response.failed` + `[DONE]` via `responses_app._stream` (the contract once headers are sent).
- Option D2: Keep buffered, document as a permanent boundary. REJECTED.
  - Pros: zero code change.
  - Cons: the matrix explicitly calls this out as "feasible future change"; users on `deepseek-reasoner` get noticeably worse perceived latency than copilot for long completions; upstream API supports the feature natively, so leaving it on the floor is technical debt by omission.
- Option D3: Hybrid (incremental on plain `deepseek-chat`, buffered on `deepseek-reasoner` because thinking blocks complicate streaming). REJECTED.
  - Pros: avoids reasoning-content stream framing complexity.
  - Cons: the deepseek streaming API delivers `reasoning_content` as a parallel delta field on `choices[0].delta.reasoning_content`, so streaming reasoner is no harder than streaming chat once the parser exists. Bifurcating by model id is unnecessary complexity.

Invalidation rationale: D2 only wins if upstream streaming did not exist or carried a token cost the buffered path avoids; neither holds. D3 only wins if the reasoner branch were materially harder; it is not.

#### Gap 2: `POST /v1/responses/{id}/cancel`

Current: route not registered in `ResponsesGatewayApp._dispatch`; the catch-all returns the gateway's standard 404 invalid_request envelope. The matrix already documents this as a deliberate non-implementation.

- Option C1: Leave unregistered; document the 404 as the contract in the matrix and add a cancel row to `responses_parity_surface.json` endpoints (already present as `unsupported` for all four). RECOMMENDED.
  - Pros: zero code change; matches the only real client (codex, which drops connections rather than POSTing cancel); avoids the partial-implementation footgun where claude/auggie cancel "kills the CLI subprocess" but deepseek/copilot cancel "best effort, may still bill".
  - Cons: a non-codex Responses-API client that follows the spec strictly receives a 404 instead of a 200 `{"status": "cancelled"}`. For the single-user gateway this is acceptable; the open carve-out in the matrix already documents it.
- Option C2: Implement as a uniform 400 `unsupported_feature` (`POST /v1/responses/{id}/cancel`) by registering the route and immediately raising `UnsupportedFeature(provider, "POST /v1/responses/{id}/cancel")`. SECONDARY.
  - Pros: cleaner contract surface (no 404 ambiguity vs. unknown route); a non-codex client gets a structured error body instead of "not found"; the capability table already encodes `POST /v1/responses/{id}/cancel` as `unsupported`, so this is a one-line wiring.
  - Cons: must extend `feature_policy` to look up `endpoints` in addition to `features` (or use a parallel ENDPOINTS table); slightly increases the surface the gate enforces. Net cost ~30 lines, mostly in tests.
- Option C3: Implement real cancellation per provider (copilot pass-through, CLI subprocess kill for claude/auggie, no-op for deepseek because the single HTTP call is in-flight and not cancellable client-side). REJECTED.
  - Pros: only option that delivers actual cancellation.
  - Cons: requires tracking in-flight requests/subprocesses in a new state structure (`Dict[response_id, asyncio.Task | Process]`), which has to coexist with the `ResponseStore`; demands per-provider semantics that diverge wildly (CLI kill vs. HTTP pass-through vs. no-op); zero observed demand from codex or the local user; high blast radius for a feature nobody asks for. The matrix already documents this as out of scope.

Invalidation rationale: C3 only wins if a real Responses-API client other than codex were the target and that client refused to operate without working cancel. Neither holds. Between C1 and C2, both are correct contracts; C2 trades a small (one-time) code increment for a cleaner client experience. Recommendation: ship C1 in Phase D; revisit C2 only if a non-codex client surfaces.

#### Gap 3: auggie incremental streaming (`stream.incremental_deltas`)

Current: classified `unsupported`. `auggie 0.28.0 --output-format` accepts only `text` or `json` (verified in `.omc/research/auggie-streaming.md`); the CLI has no streaming output mode. `--acp` exists but requires an ACP-client implementation (JSON-RPC stdio framing), not a flag change.

- Option A1: DEFER. Keep `unsupported`. RECOMMENDED.
  - Pros: zero code change; matches Round 4 spec; preserves the auggie adapter's bounded shape (`auggie --print --quiet --output-format json` one-shot).
  - Cons: auggie streaming stays a 400 `unsupported_feature` on the gate. Cost to user is small because auggie is the secondary CLI provider in the codex matrix; the local user routes most agentic traffic to claude.
- Option A2: Implement an ACP client and pipe ACP partial-output events into incremental deltas. REJECTED.
  - Pros: only path to streaming on auggie.
  - Cons: requires a from-scratch ACP client (JSON-RPC over stdio with a defined message taxonomy), a new dependency surface, and a new test harness for the ACP framing. Effort ~500-1000 lines net new code plus harness; benefit is one extra row of incremental streaming on a provider the user uses less often. Cost/benefit is poor.
- Option A3: Fake incremental streaming by polling auggie's CLI output stream (line-buffered stdout reads of `--output-format json` partial frames). REJECTED.
  - Pros: small.
  - Cons: `--output-format json` is documented as terminal-emit (full object at finish). There is no partial JSON envelope to read; attempting to chunk-parse it would be a regression hazard. Verified upstream in the spike.

Invalidation rationale: A2 only wins if a real user need for incremental auggie streaming exists; for the single local user, no such need has surfaced. The matrix carve-out is the right disposition.

#### Gap 4: `previous_response_id` durability across gateway restarts

Current: in-memory `ResponseStore` (`src/reverso/protocols/store.py`); `launchctl kickstart -k` wipes the chain. A1 returned NO-PERSIST after evidence that codex resends client-side transcripts.

- Option P1: KEEP NO-PERSIST as the documented boundary. RECOMMENDED.
  - Pros: zero code change; the boundary is already documented in the matrix's "In-memory `ResponseStore` boundary" section; codex already survives via client-side resume; no other client of this gateway exists.
  - Cons: a hypothetical non-codex Responses client loses chains on gateway restart.
- Option P2: Add a JSON-lines on-disk mirror at `~/Library/Application Support/reverso/responses-store.ndjson`. REJECTED.
  - Pros: durability for non-codex clients.
  - Cons: pulls token material onto disk (`reasoning_content`, full assistant text, request input items including any user-uploaded text). The AGENTS.md hard rule "No secrets in version control" extends in spirit to never writing user content to disk that we do not have to. Even with a strict permission model, this is a new exfiltration surface for a feature with no real consumer.
- Option P3: Add an in-memory broadcast that emits a `process_generation_token` on each new response so clients can detect a gateway restart and re-issue input items. REJECTED.
  - Pros: notifies clients without persisting content.
  - Cons: invents a non-standard Responses field for a client (codex) that does not consult it; the existing 404 on a stale `previous_response_id` already conveys the same information.

Invalidation rationale: P2 only wins if a real client demands cross-restart chains AND accepts on-disk content. Neither holds. P1 is the right disposition; the matrix already encodes it.

#### Gap 5: Codex TUI `/model` picker live polling

Current: blocked upstream in codex 0.139.0; `reverso-codex-sync` is the documented workaround.

- Option M1: DEFER. Keep `reverso-codex-sync` as the workaround. RECOMMENDED.
  - Pros: zero code change; the tool already idempotently writes the sentinel-fenced blocks.
  - Cons: a manual `reverso-codex-sync` invocation is required when the live models list changes.
- Option M2: Wire `reverso-codex-sync` as a LaunchAgent timer. SECONDARY (optional polish).
  - Pros: removes the manual step.
  - Cons: another LaunchAgent to install; the live models list changes rarely in practice (deepseek model rotation a few times per year, claude/copilot on subscription tier changes). Not justified by frequency.
- Option M3: Patch upstream codex to poll `/v1/models`. REJECTED (not our code).

Invalidation rationale: M3 is out of our control. M2 would be polish; M1 is correct disposition for now.

#### Gap 6 (extra scan): liftable cells beyond the five above

Reading the parity surface JSON row by row for cheap lifts on the deepseek column (the only adapter where translation can extend further without an ACP rewrite or a CLI feature add):

- `metadata` on deepseek: upstream chat-completions accepts an optional `metadata` object on some accounts but is not part of the public OpenAI compat surface deepseek ships. KEEP `unsupported`; not worth a translation that may 400 on some plans.
- `user` on deepseek: chat-completions accepts `user` (deepseek's compat layer mirrors the OpenAI field). Liftable to `translated` by forwarding via the `extra` carry-through (already partially supported by `_build_body`); the gate currently denies it because the surface JSON marks it `unsupported`. SECONDARY recommendation: lift `user` to `translated` for deepseek as a single-line surface-JSON change plus a verifying test. Cost: ~10 lines.
- `safety_identifier` on deepseek: not documented upstream. KEEP `unsupported`.
- `truncation` on deepseek: no upstream equivalent. KEEP `unsupported`.
- `include` on deepseek: Responses-only; chat-completions has no analog. KEEP `unsupported`.
- `background` on deepseek: requires async job model the chat API does not have. KEEP `unsupported`.
- `service_tier` on deepseek: not exposed by deepseek's compat layer. KEEP `unsupported`.

Only `user` is plausibly cheap; flagged as a secondary deepseek-side cleanup. The Architect should sanity-check whether the deepseek compat layer actually accepts `user` at the time of implementation (the matrix was generated 2026-06-10; verify on first PR).

### Headline recommendation per gap

| Gap | Option | Phase |
|---|---|---|
| deepseek incremental streaming | D1 (implement) | D1 |
| `POST /cancel` | C1 (document 404 as contract) | D1 |
| auggie incremental streaming | A1 (DEFER) | n/a |
| `previous_response_id` durability | P1 (KEEP NO-PERSIST) | n/a |
| Codex TUI live polling | M1 (DEFER, keep sync tool) | n/a |
| deepseek `user` field | lift `user` to `translated` (deepseek only) | D2 (optional) |

## Phase D plan

Phase D is the parity-gap-closure phase that follows shipped Phases A-C. It is scoped to two concrete code increments (D1: deepseek incremental streaming; D2 optional: deepseek `user` lift) plus three matrix/docs adjustments (cancel, durability, picker carve-outs already documented; the change is a once-over to make sure the language is precise).

### Phase D, Task D1 - deepseek incremental streaming

**Sequencing prerequisite (HARD GATE)**: team task #10 (landed in the working tree as of 2026-06-11, verified complete with a live deepseek codex probe reaching turn.completed) reclassified `parallel_tool_calls`, `tools.function`, and `tools.web_search` from `unsupported` to `partial` for claude/auggie (web_search also for deepseek) inside `src/reverso/protocols/data/responses_parity_surface.json` and its byte-identical mirror at `.omc/research/responses-parity-surface.json`. D1 also edits one cell in the same file (`stream.incremental_deltas` -> `deepseek`). Confirm #10 is merged before D1 ships; rebase D1's surface-JSON edit on top of the post-#10 file. The byte-identity test that compares the two copies of `responses_parity_surface.json` (under `src/reverso/protocols/data/` and `.omc/research/`) is the gate; D1 must not merge until that test is green over the combined diff.

**Goal**: lift `stream.incremental_deltas` for deepseek from `unsupported` to `translated`. The Codex client and other Responses clients see one `response.output_text.delta` per upstream chat chunk instead of one buffered delta at the end. Usage tokens captured from the terminal upstream chunk land on `response.completed` via `stream_options.include_usage`.

**Architecture rule (enforces Architect REVISE point 3)**: envelope event emission stays in the replay seam. The adapter contributes ONLY (a) the upstream streaming HTTP call and (b) the SSE-line parser that converts upstream bytes into a chunk async-iterator. A new `replay.replay_incremental` helper owns `response.created`, `response.in_progress`, `response.output_item.added`, `response.content_part.added`, the per-chunk `response.output_text.delta` emission, the finalize step (build envelope, capture usage, `store.put_response`), and the terminal `response.output_text.done` / `response.content_part.done` / `response.output_item.done` / `response.completed` events. The deepseek adapter MUST NOT re-emit envelope events itself; this is the same separation `replay.replay_turn` already enforces for buffered providers.

**File-level targets**:

- `src/reverso/protocols/replay.py`
  - Add `replay_incremental(upstream_chunks, *, response_id, message_id, model, store, input_items, finalize)` (async generator). Parameters: an async iterator yielding upstream chunk dicts with keys `text`, `reasoning_text`, `done`, `usage`, and (optionally) `tool_calls`; the response/message ids generated by the caller; the model echoed back; the store and input items for the finalize write; a `finalize` callable that takes the accumulated `(full_text, full_reasoning, usage, tool_calls)` and returns a completed `ResponseEnvelope` (the adapter passes a closure over its existing `_map_completion`-shaped logic so reasoning_content and tool calls land correctly).
  - Sequence: yield `response.created` and `response.in_progress` immediately so `responses_app._stream` commits the 200 header at TTFB; yield `response.output_item.added` with status `in_progress`; yield `response.content_part.added` with empty `output_text`; for each upstream chunk with `text`, yield `response.output_text.delta` with `delta=chunk.text` and accumulate into `full_text` (reasoning chunks accumulate into `full_reasoning` without emitting an event; tool-call deltas accumulate into a per-call argument buffer); on the terminal chunk (`done=True`), call `finalize(full_text, full_reasoning, usage_from_terminal_chunk, accumulated_tool_calls)` to build the envelope, then `store.put_response(envelope, input_items)`, then yield `response.output_text.done`, `response.content_part.done`, `response.output_item.done`, `response.completed` using the SAME data shapes `replay.replay_turn` emits.
  - Document the relaxed store-before-drain invariant for the incremental path in the docstring: the buffered `replay_turn` writes BEFORE the first yield; `replay_incremental` writes at finalize-time AFTER the last delta and BEFORE `response.completed`. A client that disconnects between the last delta and `response.completed` will not find the envelope in the store. This is the documented trade.
- `src/reverso/protocols/adapters/deepseek.py`
  - `DeepSeekAdapter._build_body(request, stream=True)`: forward `"stream": True` AND set `"stream_options": {"include_usage": True}`. This is REQUIRED to surface upstream usage on the streaming branch (deepseek's OpenAI-compat layer emits the usage object ONLY on the terminal chunk and ONLY when `stream_options.include_usage` is true; without it usage is null and the envelope reports zero tokens, which breaks the codex token-tracking surface).
  - New `_call_upstream_stream(body)`: async iterator yielding upstream chunk dicts. Uses `client.stream("POST", f"{self._api_base}/chat/completions", headers=self._headers(), content=json.dumps(body).encode("utf-8"))`. On HTTP non-2xx (read at response.status_code BEFORE iterating the body), raises `DeepSeekError(f"deepseek upstream returned status {status}")` (status only, never headers or body, matches the existing `_call_upstream` rule). On a transport error during body iteration, raises `DeepSeekError("deepseek streaming transport error")`. Per-line parsing: accumulate bytes until `\n\n`, split off lines, ignore non-`data:` lines (comment lines, blank keepalives), JSON-parse the `data: ...` payload, watch for `data: [DONE]` as the terminal sentinel. Each yielded chunk dict carries: `text` (`choices[0].delta.content` or empty), `reasoning_text` (`choices[0].delta.reasoning_content` or empty), `tool_calls` (`choices[0].delta.tool_calls` or empty list), `done` (True on the chunk that carries `finish_reason` non-null or on `[DONE]`), `usage` (the terminal chunk's `usage` dict translated via `_responses_usage`, or None on non-terminal chunks).
  - New `_finalize_streaming_envelope(request, full_text, full_reasoning, usage, tool_calls) -> ResponseEnvelope`: synthesises a chat-shaped raw dict and calls the existing `_map_completion` so reasoning_content carry-forward and tool-call surfacing both reuse the buffered-path logic. This is the closure passed as `finalize` into `replay.replay_incremental`.
  - `_stream_response` becomes a thin wrapper: builds the body with `stream=True` plus `stream_options.include_usage=true`, opens the upstream stream via `_call_upstream_stream`, generates response/message ids, and delegates to `replay.replay_incremental` with `_finalize_streaming_envelope` as the finalize callable.
- `src/reverso/protocols/data/responses_parity_surface.json`
  - Flip `stream.incremental_deltas` -> `deepseek` from `unsupported` to `translated`. Apply this on top of the post-#10 file (see Sequencing prerequisite).
- `.omc/research/responses-parity-surface.json`
  - Byte-identical mirror update (the byte-identity test enforces this).
- `docs/architecture/codex-responses-parity-matrix.md`
  - Update the `stream.incremental_deltas` row table cell for deepseek from `unsupported` to `translated`.
  - Update the "Streaming status, end-to-end" deepseek paragraph and remove the deepseek line from the "Open carve-outs" section.
  - Add one sentence in the "In-memory `ResponseStore` boundary" subsection documenting the relaxed store-before-drain invariant on the incremental path.
- `tests/unit/test_replay.py`
  - Add `test_replay_incremental_emits_canonical_envelope_around_per_chunk_deltas`: drive `replay.replay_incremental` with a fake async iterator yielding three chunks and a terminal chunk. Assert: nine envelope events with three `response.output_text.delta` events in order; final `response.completed` carries the concatenated text; `store.put_response` is called exactly once at finalize-time (use a stub store that records call ordering relative to event emission).
  - Add `test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta`: explicitly assert that the store is empty after the first delta is yielded and non-empty after `response.completed` is yielded. This pins the relaxed invariant so a future regression that re-tightens it (or relaxes it further) will fail the test.
- `tests/unit/test_deepseek_adapter.py`
  - Add `test_stream_response_consumes_upstream_streaming_and_emits_incremental_deltas`: mock transport returns an SSE body of three chat chunks (`"He"`, `"llo "`, `"world"`) plus a terminal chunk with `finish_reason="stop"` and a `usage` block (`prompt_tokens=4`, `completion_tokens=2`, `total_tokens=6`) followed by `data: [DONE]`. Assert: the emitted event sequence contains exactly three `response.output_text.delta` events with those delta strings in order; the final `response.completed` carries `"Hello world"`; the captured outbound body has `stream=true` AND `stream_options={"include_usage": true}`.
  - Add `test_stream_response_terminal_chunk_usage_lands_on_completed_envelope`: same fixture as above; assert `events[-1].data["response"]["usage"]["input_tokens"] > 0` AND `events[-1].data["response"]["usage"]["output_tokens"] > 0`. This is the load-bearing test for Architect REVISE point 2.
  - Add `test_stream_response_preserves_reasoning_content_on_stream`: mock transport interleaves `delta.reasoning_content` chunks with `delta.content` chunks across four chunks, then a terminal chunk. Assert: the stored envelope's `raw["reasoning_content"]` equals the concatenation of the reasoning chunks; `previous_response_id` chaining through `_prior_turn` returns a message that includes that reasoning_content.
  - Add `test_stream_response_401_before_first_delta_returns_structured_error`: mock transport returns HTTP 401 on the first read (before any SSE byte is emitted). Assert: the adapter raises `DeepSeekError` BEFORE yielding any event; `responses_app._stream` (covered by the integration test below) renders this as a structured 502 server_error body with no SSE bytes ever sent and no 200 header committed. This is the load-bearing test for Architect REVISE point 4 (pre-emission branch).
  - Add `test_stream_response_401_race_after_first_delta_surfaces_response_failed`: mock transport emits one `data: {"choices": [{"delta": {"content": "Hel"}}]}` chunk then closes the connection with a 401 (`httpx.RemoteProtocolError` after the first SSE block). Assert: the adapter yields one `response.output_text.delta` then raises. The post-emission branch surfaces as `response.failed` + `[DONE]` via `responses_app._stream`; the integration test below pins the wire-format side.
- `tests/integration/test_responses_app_errors.py`
  - Add `test_deepseek_streaming_401_pre_emission_renders_structured_502`: drive the gateway with a mock client_factory returning HTTP 401 on the first read of the upstream stream. Assert: response is `502` with body `{"error": {"message": "upstream provider error (DeepSeekError)", "type": "server_error"}}` and the body contains zero SSE bytes. This pins the pre-emission branch end-to-end.
  - Add `test_deepseek_streaming_401_post_emission_renders_response_failed_done`: drive with a mock that emits one chunk then errors. Assert: response is `200` with `content-type: text/event-stream`; body contains exactly one `event: response.output_text.delta`, then `event: response.failed`, then `data: [DONE]`. This pins the post-emission branch end-to-end and is the load-bearing wire-format test for Architect REVISE point 4.
- `tests/unit/test_feature_policy.py`
  - Update the table-driven assertion that `("deepseek", "stream.incremental_deltas")` is `unsupported` to assert `translated`.
- `tests/integration/test_responses_provider_contract.py`
  - Update the buffered-vs-incremental contract section to allow deepseek on the incremental path. If the suite uses a shared `_collapse_repeated_deltas` helper (mirroring the claude B2 work), the only change is moving deepseek from the "single-delta" set to the "may-emit-many-deltas" set.

**Acceptance criteria (testable)**:

- `uv run pytest tests/unit/test_replay.py -q` is green, including the two new `replay_incremental` tests (canonical envelope sequence + store-write-at-finalize ordering).
- `uv run pytest tests/unit/test_deepseek_adapter.py -q` is green, including the five new tests above (incremental deltas + usage on completed + reasoning_content on stream + 401-pre-emission + 401-post-emission).
- `uv run pytest tests/unit/test_feature_policy.py -q` is green with the flipped classification.
- `uv run pytest tests/integration/test_responses_provider_contract.py -q` is green; the deepseek streaming case now sees more than one `response.output_text.delta` event in at least one fixture (the test must accept either single-delta or multi-delta because not every mock fixture chunks the output).
- `uv run pytest tests/integration/test_responses_app_errors.py -q` is green, including the two new end-to-end tests pinning the pre-emission vs post-emission branches for the streaming transport.
- `uv run pytest tests/unit/test_responses_sse_completion.py -q` remains green (the canonical envelope events around the deltas are unchanged because emission still lives in the replay seam).
- The `responses_parity_surface.json` byte-identity test (in `tests/unit/test_feature_policy.py`; it compares the `src/reverso/protocols/data/` copy against the `.omc/research/` copy) passes after both copies are updated. This is the hard gate that catches a #10-versus-D1 merge race; D1 does not merge until this is green over the combined diff.
- `python3 -c "import re,sys; sys.exit(1 if re.search('[\u2013\u2014]', open('.omc/plans/ralplan-parity-gap-closure.md').read()) else 0)"` exits 0 (the plan itself contains no em or en dashes; the regex uses unicode escapes so the plan body never carries the literal codepoints).

**Verification steps (exact commands)**:

```
uv run pytest tests/unit/test_replay.py -q
uv run pytest tests/unit/test_deepseek_adapter.py -q
uv run pytest tests/unit/test_feature_policy.py -q
uv run pytest tests/integration/test_responses_app_errors.py -q
uv run pytest tests/integration/test_responses_provider_contract.py -q
uv run pytest -q
python3 -c "import re,sys; sys.exit(1 if re.search('[\u2013\u2014]', open('.omc/plans/ralplan-parity-gap-closure.md').read()) else 0)"
python3 -c "import re,sys; sys.exit(1 if re.search('[\u2013\u2014]', open('docs/architecture/codex-responses-parity-matrix.md').read()) else 0)"
```

Live integration verification (no automation; the maintainer runs once):

```
export DEEPSEEK_API_KEY=$(security find-generic-password -s reverso/DEEPSEEK_API_KEY -w)
launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy
curl -sN -X POST http://127.0.0.1:64946/deepseek/v1/responses \
  -H 'content-type: application/json' \
  -d '{"model":"deepseek-chat","input":"Count from 1 to 10 slowly.","stream":true}' \
  | head -c 4096
```

Expected: more than one `event: response.output_text.delta` block in the body before `event: response.completed`.

**Risks**:

- Upstream chunking is provider-dependent. Some prompts may produce one chunk for short outputs; the test must accept "one or more" deltas, not "exactly N".
- The `reasoning_content` parallel-channel parsing is the most fragile piece; if upstream changes the delta shape (e.g., wraps reasoning in a `reasoning` object), the parser silently drops it. Mitigation: the chaining test asserts non-empty reasoning round-trips, and the existing buffered path is untouched (we still pass a synthesised raw object to `_map_completion`).
- Store-before-drain is RELAXED on the incremental path. The store write moves from "before first yield" (`replay.replay_turn:194` today) to "at finalize, after the last delta and before `response.completed`" (the new `replay.replay_incremental`). The failure mode this introduces: a client that aborts between the last delta and `response.completed` will not find the envelope in the store for later `previous_response_id` chaining or `GET /v1/responses/{id}`. Mitigation: codex consumes the full stream; non-codex clients are bounded to in-process lifetime per the existing in-memory boundary; the new `test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta` pins the ordering so a future refactor cannot silently tighten or further relax it without a test flip.
- `stream_options.include_usage=true` is REQUIRED to surface upstream usage on the streaming branch. Without it the terminal chunk's `usage` object is null and the completed envelope reports zero tokens, which breaks codex's token-tracking surface and the C1 E2E matrix's TTFB-with-usage assertion. The new `test_stream_response_terminal_chunk_usage_lands_on_completed_envelope` pins this.
- `responses_app._stream` reads the first event before committing the 200 header. The new incremental path must yield `response.created` immediately (the moment the upstream stream opens), so the gateway commits headers before the first content chunk. If the upstream is slow to send any byte, the gateway holds the 200 header until then. This is acceptable and matches the claude B2 behavior.
- Sequencing race against team task #10: both #10 and D1 edit the same parity surface JSON files. The byte-identity test is the gate; D1 lands after #10 and rebases the `stream.incremental_deltas` cell flip on top of #10's `parallel_tool_calls`/`tools.function`/`tools.web_search` reclassifications. If #10 ships partial classifications for cells D1 does not touch, D1 inherits those without further changes; D1's only surface-JSON edit is the one `stream.incremental_deltas` cell.
- The pre-emission vs post-emission failure split on the streaming transport (`client.stream("POST", ...)`) is subtle: a 401 received at the response headers stage MUST raise BEFORE any SSE byte reaches the gateway, so `responses_app._stream` can synthesise a structured 502 server_error. A 401 received DURING body iteration (after an SSE chunk has shipped) MUST propagate as a normal exception so the gateway emits `response.failed` + `[DONE]`. The two new 401 tests (`test_stream_response_401_before_first_delta_returns_structured_error` and `test_stream_response_401_race_after_first_delta_surfaces_response_failed`) plus the two integration tests pin both branches.

### Phase D, Task D2 (OPTIONAL) - deepseek `user` field lift

**Goal**: lift `user` for deepseek from `unsupported` to `translated`. Defers to the Architect to verify upstream compat first.

**File-level targets**:

- `src/reverso/protocols/adapters/deepseek.py`: no code change; `user` already flows through `_build_body`'s `extra` carry-through. The lift is gate-only.
- `src/reverso/protocols/data/responses_parity_surface.json` and `.omc/research/responses-parity-surface.json`: flip `user` -> `deepseek` from `unsupported` to `translated`. Update BOTH copies in the same commit; the byte-identity test in `tests/unit/test_feature_policy.py` enforces the mirror (same discipline as D1).
- `docs/architecture/codex-responses-parity-matrix.md`: update the `user` row.
- `tests/unit/test_deepseek_adapter.py`: add `test_user_field_forwarded_via_extra_carry_through`: send a request with `user="abc-123"` and assert the captured outbound body contains `"user": "abc-123"`.
- `tests/unit/test_feature_policy.py`: flip the assertion for `("deepseek", "user")` to `translated`.

**Acceptance criteria**: the same `uv run pytest` block above is green with the two surface JSON files updated.

**Architect gate**: do not ship D2 if a live curl to `https://api.deepseek.com/v1/chat/completions` with a `user` field returns 400. Architect runs the curl once before approving D2.

### Phase D, Task D3 - matrix language refresh (no code)

- `docs/architecture/codex-responses-parity-matrix.md`: small wording pass to make the "Open carve-outs" section reflect the post-D1 state. The auggie/cancel/durability/picker carve-outs stay; the deepseek streaming carve-out is removed (replaced by a sentence in the streaming-status section noting incremental deltas now ship).
- Bump the matrix `generated:` date in frontmatter; do NOT bump `status: shipped` (the matrix is informational and the source of truth is the JSON).

**Acceptance criteria**: the suite-enforced no-em-dash and no-en-dash checks (referenced by C3 in the existing task list) remain green; markdown lint is green.

**Verification**:

```
uv run pytest tests/unit/test_docs_installation.py -q
python3 -c "import re,sys; bad=[f for f in ['docs/architecture/codex-responses-parity-matrix.md', '.omc/plans/ralplan-parity-gap-closure.md'] if re.search('[\\u2013\\u2014]', open(f, encoding='utf-8').read())]; sys.exit(1 if bad else 0)"
```

The python3 scan must exit 0 (do NOT use rg -P on this machine; the RTK hook rewrites rg to grep, which breaks the -P codepoint syntax).

## Out-of-scope (DEFER) items, with rationale

These are explicitly NOT in Phase D. Each is documented so a future planner does not re-litigate.

- **auggie incremental streaming**: requires an ACP-client rewrite (`auggie --acp` JSON-RPC stdio). Effort 500-1000 lines net new code plus a new test harness. For a single-user gateway where auggie is the secondary provider, cost/benefit is poor. Revisit if (a) the local user reports latency complaints on auggie OR (b) upstream auggie ships a streaming `--output-format`.
- **`POST /v1/responses/{id}/cancel` real implementation**: requires a per-response in-flight tracker, per-provider semantics (CLI subprocess kill vs. HTTP pass-through vs. no-op), and surface area that no current client uses. The matrix already documents the 404 as the contract. Revisit if a non-codex Responses-API client surfaces with a hard cancel dependency.
- **`previous_response_id` cross-restart durability**: A1 already decided NO-PERSIST. The trade-off (writing user content to disk for a client that does not exist) does not move. Revisit only if a non-codex client surfaces AND on-disk persistence is acceptable for it.
- **Codex TUI live polling**: blocked upstream. The `reverso-codex-sync` tool is the documented workaround. Revisit when codex grows a native polling mechanism.

## ADR (Phase D)

Proposed ADR file: `docs/architecture/adr/0004-deepseek-incremental-streaming.md` (created when Phase D ships; not in this planning artifact). Sketch below.

### Decision

DeepSeek's `stream.incremental_deltas` lifts from `unsupported` to `translated`. The `DeepSeekAdapter._stream_response` consumes upstream `POST /chat/completions` with `stream=true` AND `stream_options={"include_usage": true}`, parses upstream SSE chunks line by line, and feeds the chunk stream into a new `replay.replay_incremental` helper that owns canonical envelope event emission, the finalize step (build envelope, capture terminal-chunk usage, write to the store), and terminal envelope events. The store-before-drain invariant currently enforced by `replay.replay_turn` is intentionally RELAXED for the incremental path: the store write moves from "before first yield" to "at finalize, after the last delta and before `response.completed`". The frozen `ProviderAdapter` Protocol is not touched; envelope event shape stays owned by `replay.py`; the deepseek adapter contributes only the upstream streaming call and the SSE-line parser.

### Drivers

1. Reduce time-to-first-token for deepseek on long completions (single biggest user-visible perceived-latency win available without a Protocol change).
2. Close a parity gap the matrix already flagged as "feasible future change."
3. Bring deepseek's streaming behavior into line with claude's B2 incremental path, so the canonical replay seam is the single owner of envelope event shape for every provider where the gateway buffers or streams through `replay.py`.

### Alternatives considered

- D2 (KEEP buffered, document as permanent): rejected; upstream supports streaming and the boundary is debt not contract.
- D3 (hybrid by model id): rejected; reasoner streaming is no harder than chat streaming once the parser exists, so bifurcation is unnecessary complexity.
- Re-emit envelope events inside `DeepSeekAdapter._stream_response` (planner revision 1 sketch with a `_translate_stream` helper): rejected by the Architect. Envelope-shape ownership must stay in `replay.py` so a future buffered/incremental shape divergence cannot land in one provider's adapter without showing up in the replay seam tests.

### Why chosen

D1 (with the revision 2 architecture) is the minimum-blast-radius option that delivers the user-visible win, keeps envelope event ownership in `replay.py`, preserves every hard constraint that survives the change (Protocol frozen, mid-stream failure contract, no secrets), surfaces the one invariant it relaxes (store-before-drain) honestly, and lands the usage-on-completed requirement that codex needs for its token-tracking surface.

### Consequences

- Positive: incremental deltas reach the codex client and any other Responses-API client. The matrix and surface JSON now match implementation.
- Positive: envelope event ownership stays in `replay.py`. A buffered-vs-incremental envelope drift can only land if both replay helpers diverge, which is visible in the replay test file rather than buried inside an adapter.
- Positive: terminal-chunk usage lands on `response.completed` via `stream_options.include_usage=true`, so codex's token-tracking surface and the C1 E2E TTFB-with-usage assertion both keep working.
- Negative (REVISION 2 HONEST DISCLOSURE): store-before-drain is RELAXED on the incremental path. A client that aborts between the last delta and `response.completed` will not find the envelope in the store for later `previous_response_id` chaining or `GET /v1/responses/{id}`. Buffered providers (claude fallback, auggie, deepseek before D1) are unaffected; only the deepseek incremental path carries this relaxed semantic. Codex consumes the full stream, so it does not encounter this window; non-codex clients that abort early do. Pinned by `test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta`.
- Negative: the deepseek path now has two upstream-call shapes (buffered for unary, streaming for `stream=true`). Test surface grows by ~7 tests (replay incremental: 2, deepseek adapter: 5, integration errors: 2; some overlap with existing fixtures).
- Negative (small): a slow upstream first-byte delays the 200 header on the gateway (Responses spec already permits this; matches claude B2 behavior). Documented in the streaming-status section of the matrix.

### Follow-ups

- D2: lift `user` for deepseek to `translated` if upstream accepts it (architect-gated).
- Re-run the C1 E2E Codex matrix script (referenced as in-progress in the existing task list) against the new streaming path. The script's TTFB gate is the natural acceptance check for D1; its usage assertion is the natural acceptance check for the `stream_options.include_usage` wiring.
- No follow-up needed for the Architect-endorsed deferred items (auggie streaming, cancel 404 contract, NO-PERSIST durability, TUI picker); their rationale lives here and in the matrix carve-outs.
- Sequencing follow-up: confirm team task #10 has merged and `responses_parity_surface.json` byte-identity is green BEFORE opening the D1 PR; the surface-JSON edit must rebase on top of #10's reclassifications.

## Cross-cutting reminders the implementer must respect

- Bind: `127.0.0.1:64946` only. The streaming change does NOT alter the bind.
- Secrets: `DEEPSEEK_API_KEY` is read from env at call time; never log it. The new streaming code path must continue to log only the status code on upstream errors (current pattern in `_call_upstream`). The new `_call_upstream_stream` follows the same rule.
- Protocol: `src/reverso/protocols/adapter.py` is FROZEN. The streaming logic lives inside `DeepSeekAdapter` plus the new `replay.replay_incremental` helper in `replay.py`; no Protocol method signature changes.
- `responses_app.py` MUST NOT import `reverso.proxy.app` (runtime guard test enforces this; the change is far from that boundary, but call it out).
- Envelope event ownership: `replay.py` is the single owner of canonical envelope event shape. The deepseek adapter MUST NOT yield `response.created`, `response.in_progress`, `response.output_item.added`, `response.content_part.added`, `response.output_text.done`, `response.content_part.done`, `response.output_item.done`, or `response.completed` directly. Those events come from `replay.replay_incremental`. The adapter only contributes the upstream streaming call and the SSE-line parser that produces the chunk async-iterator.
- Store-before-drain RELAXATION (post-revision-2): the new `replay.replay_incremental` writes to the store at finalize-time, NOT before the first event. This is intentional. The buffered `replay.replay_turn` keeps the pre-drain write. Both behaviors are pinned by replay tests; do not "fix" the relaxed one by tightening it without first revisiting the ADR.
- `stream_options.include_usage=true` is REQUIRED on the streaming branch. Without it the terminal chunk's `usage` object is null and the completed envelope reports zero tokens. The unit test `test_stream_response_terminal_chunk_usage_lands_on_completed_envelope` is the gate.
- Mid-stream failure contract: a failure after the first `response.output_text.delta` MUST surface as `response.failed` + `[DONE]`. The gateway's `_stream` already handles this; the adapter must just let the exception propagate. The pre-emission 401 branch is structurally different: the streaming HTTP transport must surface a 401 received at response.headers BEFORE iterating the body, so `responses_app._stream` synthesises a structured 502. Both branches pinned by tests.
- The structured 400 `unsupported_feature` contract (`build_unsupported_payload`) and the capability table JSON in both `src/reverso/protocols/data/` and `.omc/research/` must be updated together. The byte-identity test will catch a divergence. Team task #10 is editing the same files in parallel; land #10 first and rebase D1's surface-JSON edit on top.
- No em dash (U+2014) or en dash (U+2013) in any new or modified file (this plan file, ADR draft, matrix doc, test docstrings, code comments). The python3 regex scan in the acceptance criteria is the enforcement.
- YAML frontmatter on every new `.md` (this file, the future ADR, the matrix doc already has it).
- All tests run via `uv run pytest` from the repo root.

## Uncertainty log

- The exact wire shape of deepseek's upstream chat streaming `reasoning_content` channel is inferred from the buffered `_map_completion` path's handling of `message.reasoning_content`. Architect should verify on first PR by capturing a real upstream SSE response for `deepseek-reasoner` and confirming the `delta.reasoning_content` field name.
- The integration suite's deepseek mock fixtures may chunk or not chunk output depending on how they are written; the new test must accept "one or more" deltas, not a fixed count.
- Whether `user` on deepseek is actually accepted upstream is unverified at planning time; D2 is therefore optional and architect-gated.
- The post-revision-2 store-before-drain relaxation is a real semantic change. Non-codex Responses clients that rely on `previous_response_id` chaining AND abort mid-stream lose the chain. We have no evidence such a client exists today, but if one surfaces, the only path back is reintroducing a pre-stream "envelope skeleton" store write (id-only) with a finalize-time update; that is a future change and not in scope here.

## Revision 2 changelog (relative to revision 1)

1. Frontmatter: added `revision: 2` and `architect_review:` block with the five required changes recorded inline.
2. Principle 4: declared the store-before-drain relaxation honestly instead of claiming preservation. Updated D1 option pros/cons accordingly. Every false-preservation claim about that invariant has been removed from the plan body.
3. D1 architecture: removed the `_translate_stream` helper from the adapter. Added a new `replay.replay_incremental` helper in `replay.py` that owns envelope event emission and the finalize step. Deepseek adapter now contributes only the upstream streaming call and the SSE-line parser.
4. D1 outbound body: added `stream_options.include_usage=true` on the streaming branch with a load-bearing acceptance criterion that `usage.input_tokens > 0` on `response.completed`.
5. D1 tests: added `test_stream_response_401_before_first_delta_returns_structured_error` (pre-emission branch) and `test_stream_response_401_race_after_first_delta_surfaces_response_failed` (post-emission branch) plus two integration tests pinning both branches at the wire-format level on the `client.stream("POST", ...)` racing the first delta.
6. D1 sequencing: added a HARD GATE noting team task #10 is editing the same `responses_parity_surface.json` files; #10 lands first, D1 rebases on top, byte-identity test is the gate.
7. ADR Consequences: rewrote the negatives section to surface the relaxed invariant and its bounded failure mode (client aborts between last delta and `response.completed`) explicitly.
8. Cross-cutting reminders: added an envelope-event ownership rule and an explicit "do not tighten the relaxed invariant without revisiting the ADR" reminder.
9. Endorsed unchanged: C1 (cancel KEEP 404), auggie DEFER, P1 (NO-PERSIST KEEP), M1 (picker DEFER), D2 (architect-gated `user` lift). Architect's endorsement on those is recorded; their sections are unchanged.
