---
type: fixtures-readme
project: reverso
slug: responses-parity-fixtures
status: draft
created: 2026-06-09
related:
  - ".omc/plans/test-spec-responses-providers.md"
  - "docs/architecture/adr/0002-responses-native-provider-gateway.md"
---

# Codex-observed Responses parity fixtures

Shared, synthetic OpenAI Responses fixtures used by the provider-agnostic parity
harness. The same matrix runs against the `claude` and `copilot` adapters through
`reverso.protocols.responses_app.build_app`. Each provider is reached at its
path prefix (`/claude/v1`, `/copilot/v1`), so a fixture `path` of `/v1/responses`
is exercised as `/{provider}/v1/responses`.

## Safety

All fixtures are synthetic. They contain no real secrets, tokens, captured
credentials, or private endpoint data. Identifiers such as `resp_fixture_0001`
and `call_fixture_0001` are placeholders. Do not paste real Codex captures here
without scrubbing every secret first.

## Files

- `manifest.json` - machine-readable index the harness iterates.
- `create_response_nonstreaming.json` - non-streaming create_response.
- `create_response_streaming.json` - streaming SSE event sequence.
- `list_models.json` - model refresh / list_models.
- `get_response.json` - get_response retrieval.
- `list_input_items.json` - list_input_items for a stored response.
- `tools_function_call.json` - function tools plus the function_call_output follow-up.
- `previous_response_id_chain.json` - previous_response_id conversation continuity.
- `README.md` - this file.

## Fixture envelope

Each fixture is JSON with a stable envelope so the harness can stay
provider-agnostic:

- `fixture` / `name` - identifier.
- `method`, `path` - the provider-relative request line (prefix is added per provider).
- `request` - `headers`, optional `query`, optional `path_params`, and `body`.
- `expected` - `status`, `content_type`, and either `body` (JSON) or `events`
  plus `terminal_done` (SSE).
- `assertions` - the minimal provider-agnostic checks both adapters must satisfy.
- Multi-step fixtures use `turns` (ordered) or `followup` (a second request that
  depends on the first).

## OpenAI Responses shapes encoded

These fixtures encode the public OpenAI Responses API contract surface:

- Response object: `POST /v1/responses` reply with `object: "response"`,
  `status`, `model`, `output[]` items, and `usage`. Assistant text is an
  `output[].type: "message"` item carrying `content[].type: "output_text"`.
  (`create_response_nonstreaming.json`)
- Streaming events: the ordered server-sent event sequence
  `response.created`, `response.in_progress`, `response.output_item.added`,
  `response.content_part.added`, `response.output_text.delta` (one per chunk),
  `response.output_text.done`, `response.content_part.done`,
  `response.output_item.done`, and the terminal `response.completed`, followed by
  the `[DONE]` marker that Codex consumes. (`create_response_streaming.json`)
- Model listing: `GET /v1/models` returns `object: "list"` with `data[]` items of
  `object: "model"`. The Codex refresh path also expects the top-level `models`
  field added by `CodexModelsCompatMiddleware`. (`list_models.json`)
- Response retrieval: `GET /v1/responses/{response_id}` returns the stored
  Response object. (`get_response.json`)
- Input items: `GET /v1/responses/{response_id}/input_items` returns
  `object: "list"` with the original request input items. (`list_input_items.json`)
- Tools: `tools[].type: "function"` declaration, a `function_call` output item
  with `name`, `call_id`, and JSON-string `arguments`, and the
  `function_call_output` input item that returns the tool result.
  (`tools_function_call.json`)
- previous_response_id: chaining a follow-up request to a prior response, with the
  reply echoing `previous_response_id`. (`previous_response_id_chain.json` and the
  `tools_function_call.json` follow-up)

## Conventions

- ASCII only. No em dash or en dash characters.
- JSON files are stable and pretty-printed for review.
- `expected` bodies are reference shapes. The harness asserts on the
  provider-agnostic `assertions`, not on exact byte equality, so adapters may add
  extra fields without breaking parity.
