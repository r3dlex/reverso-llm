---
title: "A4 Responses surface enumeration across reverso adapters"
status: complete
phase: A
gate: A4
machine_readable: .omc/research/responses-parity-surface.json
adapters_audited: ["src/reverso/protocols/adapters/claude.py", "src/reverso/protocols/adapters/copilot.py", "src/reverso/protocols/adapters/auggie.py", "src/reverso/protocols/adapters/deepseek.py"]
generated: 2026-06-10
---

# A4 Responses surface enumeration

## Decision

A4=done. The machine-readable matrix lives at `.omc/research/responses-parity-surface.json` and is the generation source for B1 capability tables. The matrix below is the draft for the C2 parity doc; B-lane outcomes refine the cell values, the schema does not change.

## Sources

- OpenAI Responses API surface (request/response fields, events, endpoints) as it appears in the four reverso adapter implementations and the shared `ResponsesRequest` type at `src/reverso/protocols/adapter.py`.
- Four adapters, each read in full at audit time:
  - `src/reverso/protocols/adapters/claude.py` (CLI subprocess via the local `claude` binary, OAuth gate, buffered single-delta replay through `replay_turn`).
  - `src/reverso/protocols/adapters/copilot.py` (direct HTTP forward to `api.githubcopilot.com`, SSE pass-through with verbatim event blocks).
  - `src/reverso/protocols/adapters/auggie.py` (CLI subprocess via the local `auggie` binary, buffered single-delta replay through `replay_turn`).
  - `src/reverso/protocols/adapters/deepseek.py` (HTTP translation to DeepSeek chat-completions, buffered single-delta replay through `replay_turn`, `extra` carry-through preserves `response_format`).
- Shared seam: `src/reverso/protocols/replay.py` (the canonical Responses SSE sequence for buffered providers).

## Classification key

- `native`: adapter forwards the feature unchanged or the upstream supports it as-is and the adapter passes it through.
- `translated`: adapter maps the feature from Responses shape to the upstream shape (or vice versa) without loss.
- `partial`: adapter handles a subset; the remainder belongs in the unsupported set.
- `unsupported`: not implemented; B1 must return a structured 400 `unsupported_feature` body and never invoke the adapter runner.

## Matrix

### Inputs

| Feature | claude | copilot | auggie | deepseek | Source citation |
|---------|--------|---------|--------|----------|-----------------|
| input as string | native | native | native | native | `replay.flatten_input` and `copilot.CopilotAdapter._request_body` |
| input as message-list (text content) | translated | native | translated | translated | `replay._input_item_to_text`, copilot forwards verbatim, deepseek `_build_messages` |
| input.image (image_url, input_image) | unsupported | native | unsupported | unsupported | claude/auggie CLI take prompt strings only; deepseek chat-completions translator drops non-text content; copilot forwards to Copilot Responses |
| input.file (file_id / file_url) | unsupported | native | unsupported | unsupported | same reasoning as input.image |
| instructions | translated | native | translated | translated | `replay.build_prompt` for claude/auggie, deepseek `_build_messages` system message, copilot forwards `payload["instructions"]` verbatim |

### Session and chaining

| Feature | claude | copilot | auggie | deepseek | Source citation |
|---------|--------|---------|--------|----------|-----------------|
| previous_response_id | native | native | native | native | all four use `ResponseStore.put_response` / `get_response`; deepseek also re-injects prior assistant message (`_prior_turn`) |
| store (in-memory only) | native | native | native | native | `protocols/store.ResponseStore`; A1 NO-PERSIST keeps in-memory only |
| GET /v1/responses/{id} | native | native | native | native | each adapter implements `get_response` |
| GET /v1/responses/{id}/input_items | native | native | native | native | each adapter implements `list_input_items` |
| POST /v1/responses/{id}/cancel | unsupported | unsupported | unsupported | unsupported | not implemented by any adapter; not routed in `responses_app._dispatch` |

### Streaming

| Feature | claude | copilot | auggie | deepseek | Source citation |
|---------|--------|---------|--------|----------|-----------------|
| stream (request flag) | translated | native | translated | translated | claude/auggie/deepseek buffer then replay via `replay_turn`; copilot forwards upstream SSE blocks verbatim |
| stream.incremental_deltas | translated (after B2) | native | unsupported | unsupported | A3 STREAM lights up claude via `stream_cli_runner`; copilot already incremental; auggie BUFFER per spec Round 4; deepseek buffers because the upstream call is unary in `_stream_response` |
| response.refusal.delta | unsupported | native | unsupported | unsupported | only copilot forwards refusal deltas verbatim |
| response.reasoning_summary_text.delta | unsupported | native | unsupported | unsupported | only copilot forwards reasoning summary deltas |
| response.function_call_arguments.delta | unsupported | native | unsupported | unsupported | only copilot forwards function-call argument deltas |

### Tools

| Feature | claude | copilot | auggie | deepseek | Source citation |
|---------|--------|---------|--------|----------|-----------------|
| tools.function | partial | native | partial | translated | claude/auggie accept the field for codex-compat (codex sends 22 built-in function tools by default) but the CLI runners ignore the request `tools` field and produce no `function_call` output items; copilot forwards `payload["tools"]` verbatim; deepseek `_chat_tools` converts to chat-completions function format |
| tools.web_search | partial | native | partial | partial | claude/auggie/deepseek accept the field for codex-compat (codex includes a built-in `web_search` tool entry in every default request) but cannot execute it; CLI runners ignore it and deepseek `_chat_tools` drops it before the upstream chat call; no web-search output items are emitted; copilot supports native |
| tools.file_search | unsupported | native | unsupported | unsupported | codex does not send this by default; same shape as web_search when explicitly requested |
| tools.computer_use | unsupported | unsupported | unsupported | unsupported | no upstream supports computer-use through this gateway |
| tools.code_interpreter | unsupported | unsupported | unsupported | unsupported | no upstream supports code interpreter through this gateway |
| tool_choice.auto | partial | native | partial | translated | codex defaults `tool_choice="auto"`; claude/auggie accept and ignore the field (no client tools can execute); copilot forwards; deepseek `_chat_tool_choice` translates |
| tool_choice.required | unsupported | native | unsupported | translated | codex does not send this by default; claude/auggie cannot honor a required tool call so the field stays unsupported |
| tool_choice.named | unsupported | native | unsupported | translated | codex does not send this by default; claude/auggie cannot honor a named tool call so the field stays unsupported |
| tool_choice.none | unsupported | native | unsupported | translated | codex does not send this by default; copilot/deepseek forward, claude/auggie keep it unsupported |
| parallel_tool_calls | partial | native | partial | translated | codex includes this in every default request; claude/auggie accept and ignore (no client tools can execute); copilot forwards; deepseek passes through via `extra` carry-through |

### Reasoning and sampling

| Feature | claude | copilot | auggie | deepseek | Source citation |
|---------|--------|---------|--------|----------|-----------------|
| reasoning.effort | unsupported | native | unsupported | translated | claude/auggie CLI ignore; copilot forwards verbatim; deepseek passes through `extra` (e.g. to deepseek-reasoner) |
| reasoning.summary | unsupported | native | unsupported | translated | only copilot forwards; deepseek surfaces `reasoning_content` on the envelope and replays it forward (see `_prior_turn`) |
| sampling.temperature | unsupported | native | unsupported | translated | claude/auggie CLI flags do not expose this; copilot forwards; deepseek forwards via `extra` carry-through |
| sampling.top_p | unsupported | native | unsupported | translated | same shape as temperature |
| max_output_tokens | unsupported | native | unsupported | translated | claude CLI has no flag for this; auggie similarly; copilot forwards; deepseek forwards via `extra` |

### Misc Responses request fields

| Feature | claude | copilot | auggie | deepseek | Source citation |
|---------|--------|---------|--------|----------|-----------------|
| truncation | unsupported | native | unsupported | unsupported | only copilot forwards; deepseek chat-completions API has no equivalent in the current translator |
| metadata | unsupported | native | unsupported | unsupported | only copilot forwards verbatim |
| include | unsupported | native | unsupported | unsupported | only copilot forwards verbatim |
| background | unsupported | native | unsupported | unsupported | only copilot supports background mode; the three buffered providers cannot suspend the CLI subprocess |
| text.format.text (default) | native | native | native | native | the default path; all adapters return plain text |
| text.format.json_schema | unsupported | native | unsupported | translated | copilot forwards; deepseek forwards `response_format` via `extra` (explicit non-stripping behavior per the deepseek adapter docstring) |
| text.format.json_object | unsupported | native | unsupported | translated | same shape as json_schema |
| service_tier | unsupported | native | unsupported | unsupported | only copilot forwards |
| user | unsupported | native | unsupported | translated | copilot forwards; deepseek forwards `user` via the `extra` carry-through in `_build_body` (upstream accepts as end-user identifier); claude/auggie CLIs have no equivalent |
| safety_identifier | unsupported | native | unsupported | unsupported | only copilot forwards |

### Endpoint surface

| Endpoint | claude | copilot | auggie | deepseek | Notes |
|----------|--------|---------|--------|----------|-------|
| POST /v1/responses (unary) | native | native | native | native | all four adapters implement `create_response` |
| POST /v1/responses (stream=true) | native | native | native | native | all four implement `stream_response`; incremental only for claude (after B2) and copilot |
| GET /v1/responses/{id} | native | native | native | native | served from the in-memory `ResponseStore` (claude, auggie, deepseek); copilot stores and serves the upstream envelope |
| GET /v1/responses/{id}/input_items | native | native | native | native | same as above |
| POST /v1/responses/{id}/cancel | unsupported | unsupported | unsupported | unsupported | not routed in `responses_app._dispatch`; B1 returns the structured 400 if a client posts to this path |
| GET /v1/models | native | native | native | native | all four implement `list_models`; copilot is verbatim, claude calls Anthropic with the OAuth bearer + oauth beta header, auggie shells `auggie model list --json`, deepseek queries upstream `/models` |

### SSE event emission

The buffered providers (claude, auggie, deepseek) emit the canonical nine-event sequence from `replay.py::CANONICAL_EVENT_SEQUENCE` exactly once per turn. Copilot forwards upstream SSE blocks verbatim, so it can emit any event the upstream sends; the "native"-only rows in the events table above reflect the additional events copilot is observed to forward but the other three never produce.

## Implementation notes for B1

The B1 fast-path table is generated from `responses-parity-surface.json`. Generation rules:

- `native`, `translated`, and `partial` cells: PASS through to the adapter.
- `unsupported` cells: B1 raises `UnsupportedFeature(provider=<p>, feature=<f>)` before adapter dispatch.
- Default for any feature not in the table: classify as `unsupported` (safe-by-default, surfaces a structured 400 rather than silent drop).
- The back-stop: an adapter MAY raise `UnsupportedFeature` from inside `create_response` / `stream_response` if it hits a gap the table forgot. The shared 400 builder MUST render the identical body whichever path raises.

## Implementation notes for B4

- copilot: native forward for the entire row; B4 adds fixture tests verifying pass-through for the misc fields (`include`, `background`, `metadata`, `text.format`) that the existing implementation already supports but the unit suite does not yet pin down.
- deepseek: B4 extends the chat-completions translation for `response_format`/`text.format`, `parallel_tool_calls`, `max_output_tokens`, and the sampling params. `service_tier`, `user`, `safety_identifier`, and `truncation` go into the unsupported set.
- claude and auggie: B4 confirms instructions and multi-item text input flatten through `replay.build_prompt`; everything else in the columns lands in the unsupported set.

## Decision line

A4=done
