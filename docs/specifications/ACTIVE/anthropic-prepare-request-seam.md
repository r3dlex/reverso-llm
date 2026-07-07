---
title: Anthropic prepare-request seam
status: active
slug: anthropic-prepare-request-seam
---

# Anthropic prepare-request seam

| | |
|---|---|
| **Slug** | `anthropic-prepare-request-seam` |
| **Repo** | `r3dlex/reverso-llm` |
| **Status** | active |
| **Raised** | 2026-07-06 via `improve-codebase-architecture` |
| **Spec owner** | unassigned |
| **Spans** | `src/reverso/protocols/anthropic_translate.py`, `src/reverso/protocols/anthropic_app.py`, `tests/unit/test_anthropic_translate_prepare.py`, `docs/specifications/ACTIVE/anthropic-prepare-request-seam.md` |

## A -> B

**A.** Anthropic Messages request preparation is scattered across two modules and orchestrated inline by `anthropic_app.AnthropicMessagesApp._dispatch` (~120+ lines): read/parse body, resolve backend, extract workspace, `strip_degradable_features` + `gate_anthropic_features` (both from `anthropic_feature_gate`, applied to the RAW pre-translation payload), then `anthropic_request_to_responses` duplicated inside BOTH `_handle_nonstreaming` and `_handle_streaming`. The strip -> gate -> translate pipeline is an invariant (the stripped payload must be what the gate and the adapter both observe), but nothing in the code owns that invariant; it exists only as ordering inside the app's dispatch method and a comment.

**B.** `anthropic_translate.prepare_anthropic_request(payload, backend) -> (ResponsesRequest, payload)` owns the whole preparation pipeline: strip degradable features (in place, same semantics as today), per-backend capability gating (raises `AnthropicFeatureRejected`), then the Anthropic -> Responses translation. It is stateless and pure of ASGI/network concerns (ADR 0006 D1). The returned payload is the stripped payload the downstream adapter observes -- what the app (or any future input_items recording) needs alongside the request. `anthropic_app` keeps only read/parse, backend resolution, workspace extraction, compression, and dispatch; `_handle_nonstreaming` / `_handle_streaming` receive the prepared `ResponsesRequest` and no longer translate.

## Acceptance criteria

1. `prepare_anthropic_request` lives in `anthropic_translate.py`, preserves today's exact ordering (strip, then gate, then translate), and is covered by new unit tests: valid payloads per backend, gated-feature rejection (`input.image` on deepseek/auggie/claude/codex), degradable stripping (thinking param, thinking/redacted_thinking blocks, cache_control on message/system/tool blocks including nested tool_result content), and recording-payload preservation (same dict object returned, stripped).
2. No behavior change on either surface: all existing unit tests pass unchanged (`uv run pytest tests/unit -q` green).
3. `find src -name "*.py" | xargs uv run python -m py_compile` is clean.
4. `count_tokens` keeps its intentionally un-stripped, un-gated pre-flight path (unchanged).
5. The gate 400 / not-found 404 / stream-vs-JSON error ordering observable from the app is unchanged.

## Notes

- The app's existing `except RecursionError` guard now wraps the consolidated prepare call. Its comment already promised coverage of "unforeseen deep-call paths elsewhere in gate or translation logic"; consolidation makes that literally true for the translation step as well (a pathological >1000-deep payload that passes the depth-capped gate scan now yields the documented structured 400 instead of an unstructured framework 500). No legitimate client payload is affected.
- The `payload.get("stream") is True` dispatch check stays in the app, on the same (stripped-in-place) payload dict, preserving the exact truthiness semantics (`"stream": "yes"` still routes non-streaming, as today).

## References

- ADR 0006 (Anthropic Messages surface, D1 stateless translation)
- `src/reverso/protocols/anthropic_feature_gate.py` (strip/gate remain the single capability seam; prepare composes them)
- `.ai/work-intake/anthropic-prepare-request-seam.md`
