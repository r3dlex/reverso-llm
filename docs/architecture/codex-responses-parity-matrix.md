---
title: "Codex Responses parity matrix"
status: shipped
phase: C2
gateway_bind: 127.0.0.1:64946
providers: ["claude", "copilot", "auggie", "deepseek"]
source_research:
  - .omc/research/responses-parity-surface.md
  - .omc/research/responses-parity-surface.json
  - .omc/research/codex-resume-probe.md
  - .omc/research/codex-model-picker.md
  - .omc/research/auggie-streaming.md
  - .omc/research/in-memory-boundary.md
shipped_artifacts:
  - src/reverso/protocols/feature_policy.py
  - src/reverso/protocols/data/responses_parity_surface.json
  - src/reverso/protocols/responses_app.py
  - src/reverso/protocols/adapters/claude.py
  - src/reverso/protocols/adapters/copilot.py
  - src/reverso/protocols/adapters/auggie.py
  - src/reverso/protocols/adapters/deepseek.py
  - src/reverso/codex_sync.py
generated: 2026-06-11
---

# Codex Responses parity matrix

## Scope

This document is the final per-provider Responses-API feature matrix that the reverso gateway (`127.0.0.1:64946`) presents to a Codex client across the four first-party providers (`claude`, `copilot`, `auggie`, `deepseek`). It folds the A4 research surface (`.omc/research/responses-parity-surface.md`, `.omc/research/responses-parity-surface.json`) together with the shipped Phase B outcomes:

1. B1 hybrid feature gate at `src/reverso/protocols/feature_policy.py`, generated from `src/reverso/protocols/data/responses_parity_surface.json` (byte-identical mirror of the A4 JSON). Enforced fast-path-first by `src/reverso/protocols/responses_app.py` before adapter dispatch, with an adapter back-stop via the typed `UnsupportedFeature` exception rendering the same 400 body via `build_unsupported_payload`.
2. B2 incremental claude streaming via the injectable `stream_cli_runner: Callable[[str, str], AsyncIterator[str]]` in `ClaudeAdapter`, default implementation over `asyncio.create_subprocess_exec` calling `claude --print --output-format stream-json --verbose --include-partial-messages ...`. Documented buffered fallback on preflight failure.
3. B3 in-memory `ResponseStore` boundary (no disk persistence). A1 returned NO-PERSIST, so `src/reverso/protocols/store.py` keeps the thread-safe in-memory implementation; the boundary is captured here once for all four providers.
4. B4 per-provider parity translation: deepseek translation for `text.format` and `max_output_tokens` via `_translate_extras` plus `extra` carry-through for sampling and `parallel_tool_calls`; copilot verbatim forwarding verified for the misc surface; claude and auggie remain CLI-buffered for everything outside the message text path.
5. B5 model selection via the `reverso-codex-sync` console script (`src/reverso/codex_sync.py`), which writes provider-name profile files beside `~/.codex/config.toml`, strips legacy managed blocks from the base config, writes provider-scoped catalogs, and preserves unrelated base-config keys byte-for-byte.

The classification cells below are sourced from `src/reverso/protocols/data/responses_parity_surface.json`. The fast-path gate is the source of truth; the table is informational and must match the JSON.

## Classification key

- **native**: the adapter forwards the field unchanged or the upstream supports it as-is.
- **translated**: the adapter rewrites the field between Responses shape and the upstream shape without loss.
- **partial**: only a subset of the field is supported; the gap is covered by an `unsupported_feature` 400 for the rest.
- **unsupported**: the fast-path gate at `feature_policy.check_features` raises `UnsupportedFeature(provider, feature)` and `responses_app._send_unsupported_feature` renders an HTTP 400 with body `{"error":{"type":"invalid_request_error","code":"unsupported_feature","message":"<provider> does not support <feature>"}}` BEFORE the adapter runner is invoked. The adapter back-stop renders the IDENTICAL body if a request slips past the table.

## Cross-cutting properties shared by all four providers

These properties apply to every provider on the matrix and are not repeated per row:

- **Bind**: every provider is reached only via `http://127.0.0.1:64946/<prefix>/v1/...` (loopback). The shared ASGI app at `src/reverso/protocols/responses_app.py` routes by prefix into the per-prefix `ProviderAdapter`.
- **Gate location**: `responses_app._handle_create_response` calls `extract_features` then `check_features` on the raw `ResponsesRequest.from_payload(payload)` BEFORE the Codex-normalizer runs, so Codex-only fields preserved in `extra` (for example `parallel_tool_calls`) are still seen by the gate and rejected when unsupported, even though the normalizer would otherwise drop them.
- **Mid-stream contract**: if an adapter raises after the 200 stream header is committed, `responses_app._emit_mid_stream_failure` emits a `response.failed` SSE event followed by `[DONE]` and closes the body. `UnsupportedFeature` raised mid-stream surfaces through the same path.
- **`ResponseStore`**: in-memory only (per A1 NO-PERSIST). See the dedicated boundary section below.
- **`GET /v1/models`**: each adapter has its own live source with a documented fallback (see the model listing row).
- **No secrets ever written**: token material is never logged, never persisted to disk, never sent to a non-loopback address. The `reverso-codex-sync` tool never touches credentials.

## Matrix

### Inputs

| Feature | claude | copilot | auggie | deepseek | Implementation note |
|---------|--------|---------|--------|----------|---------------------|
| `input.string` | native | native | native | native | Flattened by `replay.flatten_input` for the buffered providers; copilot forwards verbatim. |
| `input.message_list_text` | translated | native | translated | translated | Buffered providers run `replay.flatten_input` over message-list text content; copilot forwards verbatim. The fast-path gate requires `_is_text_only_message_list` (presence of at least one `message` item). |
| `input.image` | unsupported | native | unsupported | unsupported | The fast-path gate triggers when any item contains an `input_image` content part. Only copilot forwards the image to an upstream Responses surface that can handle it. |
| `input.file` | unsupported | native | unsupported | unsupported | The fast-path gate triggers on any `input_file` content part. Only copilot supports it natively. |
| `instructions` | translated | native | translated | translated | claude and auggie prepend instructions to the prompt via `replay.build_prompt`. deepseek emits a `{"role":"system"}` chat message. copilot forwards `payload["instructions"]` verbatim. |

### Session and chaining

| Feature | claude | copilot | auggie | deepseek | Implementation note |
|---------|--------|---------|--------|----------|---------------------|
| `previous_response_id` | native | native | native | native | Stored response envelopes are keyed by id in the in-memory `ResponseStore`. deepseek additionally re-injects the prior assistant `reasoning_content` (`DeepSeekAdapter._prior_turn`) so DeepSeek thinking-mode chains correctly. See the in-memory boundary section for the survival window. |
| `store` | native | native | native | native | Default true (Responses semantics). All four adapters `put_response` on completion regardless. The fast-path gate sees `store` only when the request explicitly sets it; the table marks it native because false is also supported (the gate does not reject either value). |
| `GET /v1/responses/{id}` | native | native | native | native | Served by `adapter.get_response` against the in-memory store. |
| `GET /v1/responses/{id}/input_items` | native | native | native | native | Served by `adapter.list_input_items`. Returns an empty `data` list rather than a 404 when the id is unknown (existing contract preserved). |
| `POST /v1/responses/{id}/cancel` | unsupported | unsupported | unsupported | unsupported | The route is not registered in `ResponsesGatewayApp._dispatch`. A client POSTing to it gets the gateway's standard 404 body. |

### Streaming

| Feature | claude | copilot | auggie | deepseek | Implementation note |
|---------|--------|---------|--------|----------|---------------------|
| `stream` (request flag) | translated | native | translated | translated | claude streams incrementally via the B2 `stream_cli_runner` path or falls back to a buffered canonical replay. deepseek streams incrementally via the D1 `replay.replay_incremental` path consuming upstream `stream=true` chat-completions chunks. auggie replays a single buffered turn through `replay.replay_turn`. copilot forwards upstream SSE blocks verbatim. |
| `stream.incremental_deltas` | translated | native | unsupported | translated | claude: B2 shipped, multi-`response.output_text.delta` from `claude --output-format stream-json`. copilot: native pass-through. auggie: BUFFER per spec Round 4 (auggie CLI has no streaming output mode; the row stays unsupported). deepseek: D1 shipped, multi-`response.output_text.delta` translated from upstream chat-completions `stream=true` chunks via `replay.replay_incremental`; the streamed path also sets `stream_options.include_usage=true` so the terminal usage chunk lands on `response.completed`. |
| `response.refusal.delta` | unsupported | native | unsupported | unsupported | Only copilot forwards upstream refusal deltas. |
| `response.reasoning_summary_text.delta` | unsupported | native | unsupported | unsupported | Only copilot forwards upstream reasoning summary deltas. |
| `response.function_call_arguments.delta` | unsupported | native | unsupported | translated | copilot forwards upstream function-call argument deltas. deepseek surfaces accumulated tool_call deltas at finalize-time through the replay seam's per-item helpers (added at task #12), so a streamed turn with a tool call emits canonical `response.output_item.added` (type=function_call) and `response.function_call_arguments.delta`/`.done` events alongside the message item. |

The canonical buffered SSE sequence emitted by claude (fallback path) and auggie through `replay.replay_turn` is:

```
response.created
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

The B2 incremental path on claude and the D1 incremental path on deepseek emit the same envelope events with multiple `response.output_text.delta` events (one per CLI or upstream chunk) in place of the single buffered delta. The parity unit suite's `_collapse_repeated_deltas` tolerates the chunking difference.

### Tools

| Feature | claude | copilot | auggie | deepseek | Implementation note |
|---------|--------|---------|--------|----------|---------------------|
| `tools.function` | partial | native | partial | translated | codex 0.139.0 sends a fixed 22-entry default tool surface in every Responses request (exec_command, write_stdin, MCP resource/plugin/goal tools, namespace tools). The fast-path gate accepts the field for claude/auggie so codex turns can complete; the CLI runners ignore the `tools` field and produce no `function_call` output items. copilot forwards `tools` verbatim. deepseek converts the flat Responses `{"type":"function","name":...,"parameters":...}` shape into the chat `{"type":"function","function":{...}}` shape via `_chat_tools`; the response surfaces upstream tool_calls as Responses `function_call` output items through `_tool_call_item`, and the replay seam emits per-item SSE events (`response.output_item.added` with `type=function_call`, `response.function_call_arguments.delta`/`.done`, `response.output_item.done`) for every function_call item on BOTH the buffered (`replay.replay_turn`) and the D1 streamed (`replay.replay_incremental`) paths, so the codex tool loop can drive the call. |
| `tools.web_search` | partial | native | partial | partial | codex 0.139.0 also includes a built-in `{"type":"web_search"}` entry in the default tool surface on every request. claude/auggie accept and ignore it (the CLI runners cannot execute a web_search tool and emit no web-search output items). deepseek `_chat_tools` drops non-`function` tool entries before building the chat body, so the field is accepted at the gate, stripped before the upstream call, and no web-search output items are emitted. |
| `tools.file_search` | unsupported | native | unsupported | unsupported | codex does not send this by default; same shape as web_search when explicitly requested. |
| `tools.computer_use` | unsupported | unsupported | unsupported | unsupported | No upstream supports computer-use through this gateway. |
| `tools.code_interpreter` | unsupported | unsupported | unsupported | unsupported | No upstream supports code interpreter through this gateway. |
| `tool_choice.auto` | partial | native | partial | translated | codex defaults `tool_choice="auto"` on every Responses request; claude/auggie accept the field and ignore it (the CLI runners have no client tools to choose). copilot forwards; deepseek forwards verbatim via `_chat_tool_choice`. |
| `tool_choice.required` | unsupported | native | unsupported | translated | codex does not send this by default; claude/auggie cannot honor a required client tool call. |
| `tool_choice.named` | unsupported | native | unsupported | translated | codex does not send this by default; deepseek rewrites a flat named `tool_choice` into `{"type":"function","function":{"name":...}}` in `_chat_tool_choice`. |
| `tool_choice.none` | unsupported | native | unsupported | translated | codex does not send this by default; same shape as auto on the upstreams that do forward. |
| `parallel_tool_calls` | partial | native | partial | translated | codex includes `parallel_tool_calls` in every default Responses request; claude/auggie accept and ignore it (no client tools to parallelize). copilot forwards; deepseek carries the chat-shape value through verbatim via the `extra` loop in `_build_body`. |

### Reasoning and sampling

| Feature | claude | copilot | auggie | deepseek | Implementation note |
|---------|--------|---------|--------|----------|---------------------|
| `reasoning.effort` | unsupported | native | unsupported | translated | The fast-path gate triggers on `extra["reasoning"]["effort"]`. copilot forwards; deepseek passes the upstream-supported value through (used by `deepseek-reasoner`). claude and auggie CLIs have no equivalent. |
| `reasoning.summary` | unsupported | native | unsupported | translated | copilot forwards. deepseek surfaces `reasoning_content` on the envelope (`_map_completion`) and re-injects it on `previous_response_id` chains (`_prior_turn`). claude/auggie CLIs have no equivalent. |
| `sampling.temperature` | unsupported | native | unsupported | translated | copilot forwards. deepseek carries `temperature` through the `extra` loop in `_build_body`. The claude/auggie CLIs do not expose a temperature flag. |
| `sampling.top_p` | unsupported | native | unsupported | translated | Same shape as temperature. |
| `max_output_tokens` | unsupported | native | unsupported | translated | deepseek translates `max_output_tokens` to chat `max_tokens` in `_translate_extras`; the raw key is denied via `_NON_FORWARDED_EXTRA` so the translation is the only path. claude/auggie CLIs have no max-tokens flag. |

### Misc request fields

| Feature | claude | copilot | auggie | deepseek | Implementation note |
|---------|--------|---------|--------|----------|---------------------|
| `truncation` | unsupported | native | unsupported | unsupported | Only copilot forwards. deepseek chat-completions has no equivalent. |
| `metadata` | unsupported | native | unsupported | unsupported | Only copilot forwards verbatim. |
| `include` | unsupported | native | unsupported | unsupported | Only copilot forwards verbatim. The fast-path gate triggers only when `include` is a non-empty list. |
| `background` | unsupported | native | unsupported | unsupported | Only copilot can suspend an upstream Responses run; CLI subprocesses cannot. |
| `text.format.text` (default) | native | native | native | native | Plain text default path. Returned by all four. |
| `text.format.json_schema` | unsupported | native | unsupported | translated | deepseek translates the Responses `text.format` object into a chat `response_format` wrapper in `_translate_extras` / `_response_format_from_text`. copilot forwards. claude/auggie CLIs have no JSON-schema mode. |
| `text.format.json_object` | unsupported | native | unsupported | translated | Same shape; deepseek emits `{"type":"json_object"}`. |
| `service_tier` | unsupported | native | unsupported | unsupported | Only copilot forwards. |
| `user` | unsupported | native | unsupported | translated | copilot forwards verbatim; deepseek forwards `user` through `_build_body`'s extra carry-through (the upstream chat-completions API accepts it as an end-user identifier and returns a normal completion). claude/auggie CLIs have no equivalent. |
| `safety_identifier` | unsupported | native | unsupported | unsupported | Only copilot forwards. |

### Endpoint surface

| Endpoint | claude | copilot | auggie | deepseek | Implementation note |
|----------|--------|---------|--------|----------|---------------------|
| `POST /v1/responses` (unary) | native | native | native | native | `_handle_create_response` after the fast-path gate. |
| `POST /v1/responses` (stream=true) | native | native | native | native | `_stream` with the mid-stream contract for late failures. |
| `GET /v1/responses/{id}` | native | native | native | native | `_dispatch` falls through to `adapter.get_response`. |
| `GET /v1/responses/{id}/input_items` | native | native | native | native | `_dispatch` falls through to `adapter.list_input_items`. |
| `POST /v1/responses/{id}/cancel` | unsupported | unsupported | unsupported | unsupported | Not registered; standard 404 body returned. |
| `GET /v1/models` | native | native | native | native | claude: live Anthropic listing with subscription OAuth, CLI-alias fallback. copilot: verbatim from `api.githubcopilot.com/models`. auggie: live shell to `auggie model list --json`. deepseek: live `/models` with `_DEEPSEEK_MODELS` static fallback. |

## Streaming status, end-to-end

A consolidated view of what a Codex client actually sees per provider:

- **claude**: incremental deltas during the streaming path (B2). `claude --output-format stream-json --verbose --include-partial-messages` is invoked via `asyncio.create_subprocess_exec`; one assistant text fragment per stream-json event becomes one `response.output_text.delta`. Documented fallback to the buffered single-delta replay on (a) nonzero exit before the first chunk or (b) first-chunk parse failure, signaled internally by `_StreamPreflightError`. Mid-stream failures after the first delta propagate unwrapped through the gateway's `response.failed` + `[DONE]` contract; no silent fallback is performed once a delta has shipped.
- **copilot**: incremental deltas verbatim. The adapter parses upstream SSE blocks (`_parse_sse_block`) and forwards each as the corresponding `SSEEvent`, preserving event types including refusal, reasoning summary, and function-call argument deltas the other providers never produce.
- **auggie**: BUFFER (single-delta canonical replay). Per A3 evidence in `.omc/research/auggie-streaming.md`, the auggie CLI does not expose a streaming output mode (`--output-format` accepts only `"text"` or `"json"`), and `--acp` would require an ACP client rewrite rather than a flag change. The adapter therefore runs `auggie --print --quiet --output-format json --ask -m <model> --workspace-root <sandbox> -- <prompt>` to completion and replays the result through `replay.replay_turn`. This is a spec Round 4 documented limitation, NOT an `unsupported_feature` 400.
- **deepseek**: incremental deltas during the streaming path (D1, ADR 0004). `DeepSeekAdapter._stream_response` opens an upstream `POST /chat/completions` with `stream=true` AND `stream_options={"include_usage": true}` via `_call_upstream_stream`, parses each `data: {...}` line into a chunk dict (text + reasoning_content + tool_calls + usage), and hands the chunk async-iterator to `replay.replay_incremental`. The replay seam owns canonical envelope event emission (`response.created`, `response.in_progress`, `response.output_item.added`, `response.content_part.added`, one `response.output_text.delta` per upstream content chunk, then the finalize step that writes the envelope to the store, then `response.output_text.done`, `response.content_part.done`, `response.output_item.done`, optional per-item events for any function_call surfaced from accumulated tool_call deltas, and `response.completed` with usage). The pre-emission failure branch (401 at response headers, transport error before any chunk) raises `DeepSeekError` BEFORE any envelope event ships, so the gateway synthesises a structured 502. The post-emission failure branch (transport error during body iteration after a delta has shipped) propagates unwrapped through the gateway's `response.failed` + `[DONE]` mid-stream contract. The store-before-drain invariant is RELAXED on this path: the envelope is written at finalize-time, after the last delta and before `response.completed` (see ADR 0004).

## In-memory `ResponseStore` boundary (B3 NO-PERSIST)

A1 (`.omc/research/codex-resume-probe.md`) returned NO-PERSIST: `codex exec resume` succeeds for every provider after a `launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy` that wipes the in-memory store. The persistence branch of B3 therefore does NOT execute; `src/reverso/protocols/store.py` keeps the thread-safe in-memory map. The full boundary doc is `.omc/research/in-memory-boundary.md`; the load-bearing contract for non-codex Responses-API clients is:

1. `previous_response_id` chaining and `/v1/responses/{id}` / `/v1/responses/{id}/input_items` lookups are valid ONLY within the lifetime of the gateway process that issued the id. A gateway restart drops the entire map.
2. codex resume survives a restart because codex persists the full transcript client-side under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and resends the assembled history on the resume request; no `previous_response_id` is consulted. A scan of a representative rollout returned zero occurrences of `previous_response_id` or `response_id` keys.
3. Non-codex Responses-API clients that need durable multi-turn memory across restarts must mirror codex's pattern: persist the conversation client-side and resend the input items. The gateway does not advertise a restart event or a process generation token.
4. The store is a single shared in-memory map (per process). It has no TTL, no size cap, no on-disk mirror, no cross-process sharing, and no token-material on disk by construction.
5. The store-before-drain invariant is RELAXED on the deepseek D1 incremental streaming path (`replay.replay_incremental`). The buffered providers (`replay.replay_turn`) still write BEFORE the first yield; the deepseek incremental path writes at finalize-time, AFTER the last delta and BEFORE `response.completed`. A client that aborts between the last delta and `response.completed` will not find the envelope in the store for later `previous_response_id` chaining or `GET /v1/responses/{id}`. Codex consumes the full stream so it does not encounter this window; non-codex clients that abort early do. See ADR 0004 for the full trade-off; the unit test `test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta` pins the relaxed ordering so a future refactor cannot silently re-tighten or further relax it.

## Model listing and Codex `/model` picker (A2 SYNC-TOOL via B5)

Codex 0.139.0 has no native mechanism to feed its TUI `/model` picker from a custom `model_provider`'s `/v1/models` endpoint (`.omc/research/codex-model-picker.md`). B5 ships the SYNC-TOOL workaround:

- `reverso-codex-sync` console script (`src/reverso/codex_sync.py`).
- GETs `http://127.0.0.1:64946/<prefix>/v1/models` for each of the four Reverso prefixes and writes provider-name profile files beside `~/.codex/config.toml`: `claude.config.toml`, `copilot.config.toml`, `auggie.config.toml`, and `deepseek.config.toml`.
- Each generated Reverso profile pins `model`, `model_provider = "reverso_<prefix>"`, and a provider-scoped `model_catalog_json`. The per-provider catalogs live under `~/.codex/reverso/<prefix>.json` by default and use bare model slugs because collisions cannot occur inside a provider-scoped picker.
- The base `config.toml` is kept clean: the tool strips legacy global catalog, NUX, and managed `[profiles.*]` blocks, and does not generate a root `model_catalog_json` or global model-list exposure.
- Direct `openai.config.toml` and `minimax.config.toml` profiles are direct Codex provider profiles, not Reverso routes. MiniMax remains direct Codex-only.
- Exact known stale generated variant profiles (`deepseek-gpt54`, `deepseek-mini`, `deepseek-spark`, `minimax-gpt54`, `minimax-mini`, `minimax-spark`) are archived under `Archive/reverso-codex-sync/`; other profile files are preserved.
- Idempotent: a second invocation with the same fetcher output produces no config diff and creates no backup. Deleted or stale provider catalog files are regenerated because provider profiles reference them.
- Backup discipline: a UTC-timestamped backup (`config.toml.reverso-sync.YYYYMMDDTHHMMSSZ`) is taken before each base config rewrite, and changed profile files receive their own timestamped sibling backups. `_rotate_backups` keeps the 5 newest per file.
- Atomic write: the new content is composed in a temp file IN THE SAME DIRECTORY as `config.toml` and `os.replace`-d into place. Unrelated keys outside the sentinel-fenced blocks are byte-faithfully preserved (raw text round-trip, no `tomllib` re-emit).
- No secrets: the tool only reads `/v1/models` model ids and never sees provider credentials.

Codex-side resume ergonomic caveat (recorded once here; see `.omc/research/codex-resume-probe.md`): `codex exec resume` does NOT accept `-p/--profile`. The supported invocation is `codex exec resume -c 'model_provider="reverso_<provider>"' -c 'model="<id>"' <session_id> <prompt>`. The C1 E2E matrix script uses these overrides on the resume path. The TUI `codex resume` accepts `-p`; only `exec resume` rejects it.

## Feature gate and `unsupported_feature` error contract

The 400 body emitted for any unsupported feature on any provider is:

```json
{
  "error": {
    "type": "invalid_request_error",
    "code": "unsupported_feature",
    "message": "<provider> does not support <feature>"
  }
}
```

- `<provider>` is one of `claude`, `copilot`, `auggie`, `deepseek`.
- `<feature>` is the dotted key from `responses_parity_surface.json` (for example `tools.web_search`, `parallel_tool_calls`, `text.format.json_object`).
- The fast path at `responses_app._handle_create_response` rejects BEFORE the adapter runner is invoked.
- The back-stop (an adapter raising `UnsupportedFeature(provider, feature)` from inside `create_response` or `stream_response`) renders the IDENTICAL body via the same `build_unsupported_payload` builder; the gateway never lets a fast-path 400 and a back-stop 400 diverge.
- Mid-stream `UnsupportedFeature` (raised after the 200 stream header is committed) surfaces through `response.failed` + `[DONE]`; the structured 400 is unreachable once headers are sent.
- An unknown feature key (not in the capability table) is silently allowed at the fast path; the adapter back-stop is the safety net.

## Open carve-outs and known limitations

- **auggie streaming**: BUFFER per spec Round 4. Lifting to STREAM would require either an auggie CLI streaming output mode (not present in 0.28.0) or an ACP-client rewrite of the auggie adapter, neither of which is in scope for this matrix.
- **`POST /v1/responses/{id}/cancel`**: not implemented for any provider. Returns the gateway's standard 404 rather than a 400 `unsupported_feature` because the route itself is unregistered; this matches Codex's existing behavior for unknown routes.
- **Codex TUI `/model` picker**: feeds from static config and profile catalog files, not live provider polling. The B5 sync tool is the documented workaround until Codex grows a native polling mechanism.
- **`previous_response_id` across gateway restarts**: drops to a stored-id miss. Codex resume already survives via its client-side transcript; other Responses-API clients must replay input items. The deepseek D1 incremental streaming path additionally relaxes the store-before-drain invariant on its own branch (see ADR 0004 and the in-memory boundary section above); a client that aborts between the last delta and `response.completed` does not find the envelope in the store. This is bounded to non-codex clients.

Phase D removed the previously-recorded deepseek streaming carve-out: the adapter now consumes upstream `stream=true` chunks via `replay.replay_incremental`, so the `stream.incremental_deltas` row is `translated` and the streaming-status section above carries the full description. See ADR 0004 for the architecture and the relaxed store-before-drain trade.
