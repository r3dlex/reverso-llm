---
title: OR-G4 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
focused_command: uv run pytest tests/unit/test_openrouter_messages.py tests/integration/test_openrouter_messages_unary.py -q
verdict: GO
---

# OR-G4 red-green evidence

## Focused command

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_messages.py -q
```

## Red phase

```text
ImportError: cannot import name 'AnthropicMessagesRequest' from 'reverso.protocols.anthropic_native'
```

## Green phase

```text
......                                                                   [100%]
6 passed in 0.02s
```

- `test_build_messages_payload_forwards_required_fields`
- `test_build_messages_payload_handles_images`
- `test_normalize_messages_response_extracts_usage`
- `test_normalize_messages_response_preserves_thinking_blocks`
- `test_messages_request_does_not_call_responses_dispatch`
- `test_messages_payload_carries_bearer_authorization`

## Architecture

- `src/reverso/protocols/adapters/openrouter/messages.py` introduces the
  ``OpenRouterMessagesAdapter`` that posts to
  ``POST /api/v1/messages``, forwards ``anthropic-version: 2023-06-01``,
  and normalizes the response back into the Anthropic Messages envelope.
  The Responses dispatch is never invoked; Headroom runs once before
  dispatch on the surface.

## Decision

OR-G4 verdict is GO. OR-G5 (Messages streaming) may proceed.
