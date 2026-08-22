---
title: OR-G3 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g3
focused_command: uv run pytest tests/unit/test_openrouter_responses_stream.py tests/integration/test_openrouter_responses_continuation.py -q
verdict: GO
---

# OR-G3 red-green evidence

## Focused command

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_responses_stream.py -q
```

## Red phase

```text
ModuleNotFoundError: No module named 'reverso.protocols.adapters.openrouter.continuation'
```

## Green phase

```text
......                                                                   [100%]
6 passed in 0.03s
```

- `test_stream_response_emits_canonical_events`
- `test_stream_response_drops_sse_comments`
- `test_continuation_materializes_typed_chain`
- `test_continuation_rejects_unknown_chain`
- `test_continuation_rejects_partial_chain`
- `test_continuation_rejects_chain_with_unknown_role`

## Architecture

- `src/reverso/protocols/adapters/openrouter/transport.py` adds a streaming
  surface that filters SSE comments (``:``) and `[DONE]` markers before
  yielding parsed JSON event payloads.
- `src/reverso/protocols/adapters/openrouter/continuation.py` introduces
  `ReplayChain`, `ReplayItem`, `materialize_continuation`, and
  `build_continuation_request`. Unknown, partial, empty, and unknown-role
  chains are rejected locally. The materialized body never carries
  `previous_response_id` or `store`.
- `src/reverso/protocols/adapters/openrouter/adapter.py` forwards the SSE
  payload through `stream_response` and accepts both dict and `SSEEvent`
  transport payloads.

## Decision

OR-G3 verdict is GO. The Codex streaming and stateless continuation path is
admit-gated, never forwards stateful fields, and rejects unknown / partial
chains. OR-G4 (Claude native Messages unary) may proceed.
