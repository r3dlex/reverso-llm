---
title: "A3 Auggie and Claude CLI streaming capability probe"
status: complete
phase: A
gate: A3
decision: STREAM
auggie_version: "0.28.0 (commit 63537d73)"
claude_version: "2.1.153 (Claude Code)"
generated: 2026-06-10
---

# A3 Auggie and Claude CLI streaming capability

## Decision

**STREAM** for the claude CLI; **BUFFER** for the auggie CLI. B2 implements claude incremental streaming via the injectable `stream_cli_runner` async iterator. Auggie remains a buffered single-delta replay (documented limitation per spec Round 4), with the existing `replay_turn` sequence carrying the full assistant text in one `response.output_text.delta`.

The combined decision recorded on the report line is STREAM, because B2 lights up claude incremental streaming; auggie's BUFFER status is the documented carve-out the plan already permits.

## Claude CLI streaming evidence

`claude --help` documents the following streaming flags (verbatim, captured 2026-06-10):

- `--output-format <format>` accepts `"text" | "json" | "stream-json"` (only with `--print`). The `stream-json` mode is "realtime streaming".
- `--input-format <format>` accepts `"text" | "stream-json"`.
- `--verbose` is required to emit the full event stream when using `stream-json`.
- `--include-partial-messages` adds partial message chunks "as they arrive" (only with `--print` and `--output-format=stream-json`).

Live probe:

```
claude -p --output-format stream-json --verbose --model haiku "Say: ok"
```

Emits one JSONL object per stdout line. Observed event progression (representative excerpt, signatures redacted):

1. Multiple `{"type":"system","subtype":"hook_started", ...}` and `{"type":"system","subtype":"hook_response", ...}` lines for hook lifecycle.
2. `{"type":"system","subtype":"init", ...}` once.
3. `{"type":"assistant","message":{...,"content":[{"type":"thinking","thinking":"<text>","signature":"<redacted>"}]}}` for reasoning.
4. `{"type":"assistant","message":{...,"content":[{"type":"text","text":"ok"}]}}` for the assistant text. Each new text-bearing assistant event carries an incremental `content[].text` chunk; concatenating them yields the full response.
5. `{"type":"rate_limit_event", ...}`.
6. `{"type":"result","subtype":"success","is_error":false,"duration_ms":1828,"duration_api_ms":1549,"ttft_ms":1658,"num_turns":1,"result":"ok",...,"total_cost_usd":...}` as the terminal event.

The terminal `result` event carries `ttft_ms` (time to first token from the CLI's own measurement), so B2 can compare the CLI-reported TTFB to the gateway-reported first-delta time during E2E.

The B2 streaming runner contract (per the plan):
- Signature `stream_cli_runner: Callable[[str, str], AsyncIterator[str]]`.
- Default implementation built on `asyncio.create_subprocess_exec` reading stdout line by line.
- Argv: `["claude", "--print", "--output-format", "stream-json", "--verbose", "--model", model, "--", prompt]`.
- Parser yields the text fragment from any `{"type":"assistant","message":{...,"content":[{"type":"text","text":<str>},...]}}` line (ignoring `thinking` parts; ignoring `system`, `rate_limit_event`, and the terminal `result`).
- Fallback (named precisely in the plan): nonzero exit before the first chunk OR first-chunk parse error -> reuse the existing buffered `_run_claude_cli` path for that request.
- Mid-stream failure (exception after first delta) MUST surface through the existing `responses_app` contract: a `response.failed` event followed by `[DONE]`. B2 must preserve this unchanged.

## Auggie CLI streaming evidence

`auggie --help` (captured 2026-06-10) documents the full output and session option surface:

- `--output-format <format>` accepts only `"text"` (default) or `"json"`. There is no `stream-json` mode and no `--include-partial-messages` flag.
- `-p, --print` is one-shot.
- `-q, --quiet` shows only the final assistant message.
- `--mcp` runs auggie as an MCP server (different protocol; not relevant to the reverso adapter's one-shot subprocess shape).
- `--acp` enables ACP mode (Agent Client Protocol). ACP IS a streaming protocol, but it is a JSON-RPC-over-stdio protocol that requires a coordinating ACP client; the existing reverso `auggie` adapter uses the bounded one-shot subprocess shape (`auggie --print --quiet --output-format json --ask -m <model> --workspace-root <sandbox> -- <prompt>`) and would require a fundamental rewrite, not just an additional flag, to consume ACP. That is out of scope for B2 (spec Round 4 explicitly permits buffered auggie).

There is no other documented `auggie` mode that emits incremental tokens to stdout while a `--print` invocation runs. The existing one-shot path with `--output-format json` returns a single JSON object containing the full result (`{"type":"result","result":"<full text>"}`); the reverso adapter then parses `result` and synthesizes a one-delta canonical replay through `replay_turn`.

Decision: BUFFER for auggie. The adapter keeps its current buffered shape and the parity doc records the limitation.

## Implications

- B2 implements claude streaming via `stream_cli_runner` (default `asyncio.create_subprocess_exec` over `claude --print --output-format stream-json --verbose ...`).
- B2 tests: a fake async-generator runner concatenating multiple deltas, a fallback test (nonzero exit before the first chunk), and a mid-stream failure test (exception after first delta -> `response.failed` + `[DONE]`).
- The auggie adapter remains as-is; B4 declares auggie streaming as "buffered single delta" in the parity doc, NOT "unsupported_feature" (the spec's Round 4 carve-out treats this as a documented limitation, not a hard 400).
- C1 E2E matrix's TTFB gate (initial 20 second threshold per plan) compares the first `response.output_text.delta` arrival time on the gateway SSE stream to the CLI-reported `ttft_ms` for claude; auggie is exempt from the gate or set to a buffered-completion threshold.

## Decision line

A3=STREAM (claude STREAM, auggie BUFFER documented per spec Round 4)
