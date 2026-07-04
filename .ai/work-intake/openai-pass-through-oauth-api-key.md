# OpenAI pass-through OAuth/API-key provider

Completion: PR #73 merged 2026-07-04. GitHub #47 closed after local-loopback opt-in OpenAI pass-through shipped.
Date: 2026-07-04
Status: complete
Hosted tracker: GitHub issue #47, https://github.com/r3dlex/reverso-llm/issues/47
Spec: `docs/specifications/ARCHIVED/openai-pass-through-oauth-api-key.md`

## Problem

Reverso has completed the `codex-direct` local-loopback default track. The next
provider track is OpenAI pass-through: expose a Reverso-routed `/openai/v1/...`
surface for Responses API and model listing, authenticated by OAuth when possible
and API-key fallback when explicitly configured.

## Requested outcome

Create an Autobahn-ready plan that implements OpenAI pass-through as
local-loopback opt-in first, preserves all `codex-direct` boundaries, and includes
post-PR repo hygiene for completed codex-direct artifacts.

## Slices

1. OpenAI pass-through skeleton and local-loopback gates.
2. OAuth/API-key auth resolution plus secret-safe diagnostics.
3. Responses/models direct HTTP pass-through with streaming and non-streaming tests.
4. Codex profile/model UX without overriding built-in GPT defaults.
5. Operator docs, traceability updates, and codex-direct spec hygiene.

## Constraints

- No non-loopback or hosted default-on exposure.
- No `codex-direct` behavior changes.
- No secret-bearing live-token CI requirement.
- Use official OpenAI API shape: upstream `POST /responses`, upstream
  `GET /models`; Reverso-prefixed paths are `/openai/v1/responses` and
  `/openai/v1/models`.
