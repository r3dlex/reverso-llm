---
title: "C1 Codex E2E matrix results"
status: complete
phase: C
gate: C1
gateway: 127.0.0.1:64946
codex_version: "codex-cli 0.139.0"
ttfb_budget_seconds: 20
generated: 2026-06-11T06:32:30Z
---

# C1 Codex E2E matrix results

Live run of `scripts/codex-e2e-matrix.sh` against the reverso gateway on 127.0.0.1:64946.

## Pass/fail table

| provider | cell | status | detail |
|----------|------|--------|--------|
| all | model_sync | PASS | changed=true providers=auggie,claude,copilot,deepseek sentinel_count=1 tmp_cfg=/var/folders/r6/yb6jhjqs3_xf1l3zwhdcnr6m0000gn/T/reverso-codex-sync-tmp.XXXXXX.toml.5MOZS3KIzq |
| claude | memory | PASS | sid=019eb55a-27e0 reply_contains_secret=true |
| claude | workspace | PASS | env_context_cwd_returned=true (file reads NA: claude CLI permission default + tools.function partial) |
| claude | usage | PASS | input_tokens=9938 output_tokens=1 |
| claude | resume_after_restart | PASS | sid=019eb55a-989a post_restart_secret_returned=true |
| claude | ttfb_under_20s | PASS | first_delta_secs=2.049 |
| claude | tool_call_loop | NA | tools.function partial per post-#10 surface; tools.function partial (200 text-only verified); 400 contract intact via tools.file_search |
| claude | model_selection | PASS | model=claude-sonnet-4-6 reply=ok |
| copilot | memory | PASS | sid=019eb55b-1e56 reply_contains_secret=true |
| copilot | workspace | PASS | reply_contains_token=true |
| copilot | usage | PASS | input_tokens=18327 output_tokens=17 |
| copilot | resume_after_restart | PASS | sid=019eb55b-8806 post_restart_secret_returned=true |
| copilot | ttfb_under_20s | PASS | first_delta_secs=3.848 |
| copilot | tool_call_loop | PASS | function_call=get_weather |
| copilot | model_selection | PASS | model=gpt-5.4-mini reply=ok |
| auggie | memory | PASS | sid=019eb55d-1333 reply_contains_secret=true |
| auggie | workspace | PASS | reply_contains_token=true |
| auggie | usage | PASS | input_tokens=9938 output_tokens=1 |
| auggie | resume_after_restart | PASS | sid=019eb55f-6993 post_restart_secret_returned=true |
| auggie | ttfb_under_20s | NA | BUFFER per A3 auggie-streaming.md; documented limitation, not a failure |
| auggie | tool_call_loop | NA | tools.function partial per post-#10 surface; tools.function partial (200 text-only verified); 400 contract intact via tools.file_search |
| auggie | model_selection | PASS | model=haiku4.5 reply=ok |
| deepseek | memory | PASS | sid=019eb560-0acc reply_contains_secret=true |
| deepseek | workspace | PASS | reply_contains_token=true |
| deepseek | usage | PASS | input_tokens=19951 output_tokens=13 |
| deepseek | resume_after_restart | PASS | sid=019eb560-6bf8 post_restart_secret_returned=true |
| deepseek | ttfb_under_20s | PASS | first_delta_secs=1.925 |
| deepseek | tool_call_loop | PASS | function_call=get_weather |
| deepseek | model_selection | PASS | model=deepseek-v4-pro reply=ok |

## Counts

* PASS: 26
* FAIL: 0
* NA (documented unsupported): 3

## Notes on documented unsupported cells

* auggie `ttfb_under_20s`: A3 (.omc/research/auggie-streaming.md) decided BUFFER. The auggie CLI has no incremental output mode; the adapter emits a single buffered delta after the upstream completes. This is the documented limitation, not a regression.
* claude and auggie `tool_call_loop`: post-#10 the surface declares `tools.function` PARTIAL for the CLI-spine providers (the codex normalizer strips function tools and the request proceeds text-only). The matrix verifies a 200 text-only completion for a function-tool request AND asserts the structured 400 `unsupported_feature` body still fires via `tools.file_search` before recording NA.

## How the script is bounded

* Each `codex exec` and `codex exec resume` invocation runs under a 180s watchdog that kills the process and its descendants via a pgrep-based loop (setsid is unavailable on macOS bash 3.2).
* The gateway restart polls `/claude/v1/models` for up to 45s before bailing.
* The streaming TTFB probe gives the gateway a 25s wall-clock budget and fails the cell if no `response.output_text.delta` arrives within 20s.
* The tool-call POSTs have a 60s curl timeout.

## Resume protocol detail

Every `codex exec resume` call uses `-c model_provider="reverso_<provider>" -c model="<id>"` overrides. `codex exec resume` does not accept `-p` (see A1, .omc/research/codex-resume-probe.md); without these overrides resume silently falls back to the default openai provider, defeating the test.

## Model selection mechanism

The cross-cutting `model_sync` row exercises `uv run reverso-codex-sync --config <tmp> --base-url http://127.0.0.1:64946` against a temporary fixture config (the real `~/.codex/config.toml` is never modified). Per-provider `model_selection` rows then run `codex exec -c model_provider="reverso_<p>" -c model="<live id from /v1/models>"` to confirm the synced id can drive a real turn end to end.
