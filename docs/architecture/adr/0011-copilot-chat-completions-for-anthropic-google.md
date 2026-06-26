---
type: adr
project: reverso
id: 0011
title: Copilot chat-completions path for Anthropic and Google models
status: Accepted
date: 2026-06-26
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/architecture/adr/0004-deepseek-incremental-streaming.md
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0009-claude-on-anthropic-surface.md
---

# Copilot chat-completions path for Anthropic and Google models

## Status

Accepted

## Context

The Copilot adapter forwarded every turn verbatim to GitHub Copilot's
`/responses` endpoint. That endpoint serves ONLY the gpt-5.x family; Anthropic
(`claude-*`) and Google (`gemini-*`) models that Copilot also exposes are served
on its `/chat/completions` endpoint and return `unsupported_api_for_model` on
`/responses`. The adapter's `_check_model` gate therefore rejected every
non-gpt id with `UnsupportedFeature`, so `copilot/claude-sonnet-4`,
`copilot/opus-4.8`, and `copilot/gemini-*` could not be served on the inbound
Anthropic Messages surface even though routing already resolved them to the
copilot backend (rowless) and stripped the `copilot/` prefix.

The codebase already owns a complete Responses<->/chat/completions translator:
the DeepSeek adapter (ADR 0004). Reproducing that translation inside the Copilot
adapter would duplicate request translation, tool translation, usage renaming,
stream-delta parsing, and the pre-emission priming contract.

## Decision

The Copilot adapter gains a SECOND upstream shape selected by model family:

- gpt-* ids continue to forward verbatim to `/responses` (the existing path is
  untouched, including alias canonicalisation and the blanket `request.extra`
  forwarding the Responses-native gpt surface relies on);
- claude-*/gemini-* ids are translated to `/chat/completions` and mapped back to
  the canonical Responses `ResponseEnvelope` (non-streaming) and Responses SSE
  events (streaming).

Model classification is centralised. `copilot_models.py` adds
`is_copilot_chat_model_id` (true for safe-char ids starting with `claude` or
`gemini`, case-insensitive, behind the same `has_safe_model_id_chars` guard that
still rejects an injection payload like `"gpt-5.5\nmodel:claude-fable-5"`) and a
single `copilot_model_route(model_id) -> "responses" | "chat" | None`. The
adapter's `_route` raises `UnsupportedFeature` only when the route is `None`;
`create_response` and `stream_response` branch on the route so the two upstream
shapes never diverge. The `/v1/models` listing accepts ids that pass the
responses OR the chat classifier.

The pure chat-translation helpers are extracted from the DeepSeek adapter into a
shared module `reverso.protocols.openai_chat` (Option A). DeepSeek and Copilot
both import them; DeepSeek's provider-specific behaviour (reasoning_content
carry-forward, profile model resolution, response_format/max_output_tokens extra
translation) stays in the DeepSeek adapter. The DeepSeek regression suite is the
gate that keeps the extraction behaviour-preserving.

The chat body forwards ONLY the deepseek-vetted set (`model`, `messages`,
`stream`, plus `tools`/`tool_choice` when present, and
`stream_options.include_usage` on the streaming branch). It does NOT
blanket-forward `request.extra`, because `/chat/completions` 400s on
Responses-only keys (include, background, ...). The chat path mints fresh
`resp_`/`msg_` ids via the replay helpers (it cannot echo an upstream `resp_`
id) and stores the envelope through the injected `ResponseStore`. Streaming
checks `status >= 400` at the response headers before draining and primes the
upstream iterator so a pre-emission 4xx raises before any replay event.

Canonical Responses SSE event emission stays owned by
`reverso.protocols.replay.replay_incremental`; the new chat path contributes
only the upstream chunk iterator and a finalize callback, exactly as DeepSeek
does. Capability gating continues to key on the copilot column.

## Consequences

- `copilot/claude-*` and `copilot/gemini-*` are now servable on the Anthropic
  Messages surface for both non-streaming and streaming traffic.
- The gpt-* `/responses` path and its parity are unchanged; the verbatim spine
  and its `request.extra` passthrough are preserved.
- DeepSeek and Copilot now share one translation seam, so a future chat-surface
  fix lands once. The risk is that a DeepSeek-specific change leaks into the
  shared module; the shared module is restricted to provider-neutral helpers and
  the DeepSeek suite guards behaviour.
- The chat path deliberately drops Responses-only extras, so a caller relying on
  those keys against a claude/gemini model will not see them forwarded (they are
  not accepted by the upstream chat surface anyway).
- The chat path is stateless single-shot: it does NOT replay a prior assistant
  turn upstream for a `previous_response_id` conversation (unlike the DeepSeek
  adapter, whose `_prior_turn` re-injects reasoning carry-forward, a DeepSeek-only
  contract). The envelope still records `previous_response_id`, but chaining relies
  on the stored envelope rather than upstream re-injection, matching how the
  verbatim `/responses` path delegated chaining to the upstream.

## Verification

- `tests/unit/test_deepseek_adapter.py` stays green (extraction is
  behaviour-preserving).
- `tests/unit/test_copilot_adapter.py` covers: claude routes to
  `/chat/completions` non-streaming (body has `messages`, envelope has a message
  output item, usage renamed); chat tool_calls surface as function_call items;
  chat streaming yields canonical Responses events with
  `stream_options.include_usage`; pre-emission 4xx raises before any event; the
  chat body forwards only the vetted set; the injection id still raises; and
  gpt-5.5 still routes to `/responses` verbatim.
- `tests/integration/test_copilot_anthropic_surface.py` drives
  `AnthropicMessagesApp` with a fake-auth Copilot adapter for
  `copilot/claude-sonnet-4` non-streaming and streaming.
- `uvx prek run --all-files` and `uv run pytest tests/unit tests/integration
  -p no:randomly -q` both pass.
