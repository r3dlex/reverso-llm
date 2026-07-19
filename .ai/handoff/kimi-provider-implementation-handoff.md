---
title: Kimi provider implementation handoff
status: implementation-ready
slug: kimi-subscription-provider
date: 2026-07-19
---

# Kimi provider implementation handoff

## Target result

Ship Kimi Code subscription access through Reverso's OpenAI Responses and
Anthropic Messages surfaces using OAuth-derived bearer authentication, while
retaining the frozen provider contract, LiteLLM quarantine, loopback-only
boundary, and provider-agnostic Headroom compression.

## Sources of truth

- Specification:
  `docs/specifications/ACTIVE/kimi-subscription-provider.md`
- Architecture decision:
  `docs/architecture/adr/0017-kimi-code-oauth-provider.md`
- Provider contract:
  `src/reverso/protocols/adapter.py`
- Responses dispatcher:
  `src/reverso/protocols/responses_app.py`
- Anthropic dispatcher and translation:
  `src/reverso/protocols/anthropic_app.py` and
  `src/reverso/protocols/anthropic_translate.py`
- Headroom seam:
  `src/reverso/protocols/headroom_compression.py`

## Diagnosed design

1. Kimi CLI is the OAuth authority. Reverso consumes and refreshes its persisted
   bearer artifact; it does not implement interactive login.
2. Kimi's subscription model endpoint is OpenAI-compatible chat completions,
   not OpenAI Responses and not Anthropic Messages.
3. One Kimi `ProviderAdapter` therefore serves both inbound surfaces through
   Reverso's existing translations.
4. The official Python Kimi SDK is not selected because it is an agent/chat
   abstraction, does not supply either required inbound wire contract, and
   raises the Python minimum from 3.11 to 3.12.
5. Headroom already executes before adapter dispatch on both surfaces. Adding a
   Kimi-specific compression layer would be incorrect duplication.

## Implementation sequence

### S1: Lock authentication behavior

Add failing tests for:

- OAuth access token priority.
- Explicit bearer fallback.
- Expired-token refresh and rotated-artifact persistence.
- Missing and malformed credentials.
- Secret-free errors.

Implement `KimiOAuthAuth` in
`src/reverso/protocols/adapters/kimi.py`. Default to
`~/.kimi-code/credentials/kimi-code.json` by default, honor `KIMI_CODE_HOME`,
refresh within five minutes of expiry,
write atomically with mode `0600`, and permit `KIMI_BEARER_TOKEN` only when the
OAuth artifact cannot supply a token.

Stop when the auth tests pass without network access outside `MockTransport`.

### S2: Implement the provider adapter

Add unary, streaming, tool-call, usage, model-list, and error-path tests before
the adapter implementation. Use Reverso's existing OpenAI-compatible message,
tool, usage, response-store, and replay seams. Do not alter
`ProviderAdapter`.

Outbound requests target:

- `POST https://api.kimi.com/coding/v1/chat/completions`
- `GET https://api.kimi.com/coding/v1/models`

Every request uses `Authorization: Bearer <resolved-token>`. One unary `401`
may force refresh and retry once. Do not include secrets or response bodies in
errors or logs.

Stop when adapter tests prove valid Responses envelopes and canonical SSE.

### S3: Mount the Responses surface

Add `kimi` to the first-party prefix set and the real composition registry.
Pin these behaviors:

- `/kimi/v1/responses` reaches Kimi.
- `/kimi/v1/models` reaches Kimi.
- Stored response and input-item routes work.
- The legacy LiteLLM app is never invoked for the prefix.

Stop when the deterministic Responses contract probe passes.

### S4: Mount the Anthropic surface

Add Kimi to `SURFACE_BACKENDS["anthropic"]` and
`build_anthropic_adapters`. Treat it as rowless so explicit
`kimi/<model>` routing is authoritative. Add a small discovery seed only for
clients that require an Anthropic-prefixed picker alias.

Pin both path-pinned and model-qualified dispatch, plus canonical model-id
stripping. Do not add Kimi rows to LiteLLM configuration merely to satisfy
first-party routing.

Stop when both Messages routes exercise the same Kimi adapter.

### S5: Declare the capability ceiling

Add Kimi to the research capability source and packaged mirror. Initially copy
the DeepSeek OpenAI-compatible translation classifications because those
describe Reverso's effective translator, not Kimi's possible upstream feature
set. Keep image/file unsupported until translated and tested.

Stop when table completeness and Anthropic feature-gating tests pass.

### S6: Prove Headroom inheritance

Run the existing parameterized Responses Headroom matrix after registering the
new prefix. Add a Kimi-specific assertion only if the generic matrix cannot
prove that the compressed request reaches the adapter. Do not add provider
branches to `headroom_compression.py`.

Stop when compression, fail-open, and prompt-free metrics behavior are green.

### S7: Validate and smoke

Run:

```bash
uv run pytest tests/unit/test_kimi_adapter.py -q
uv run pytest tests/integration/test_kimi_surfaces.py -q
uv run pytest tests/integration/test_responses_headroom_compression.py -q
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run python -m compileall -q src/reverso
git diff --check
```

Then run the credentialed smoke commands from the specification with a model id
returned by `/kimi/v1/models`.

## Current worktree evidence from the diagnose session

The diagnose session produced an implementation in the current uncommitted
worktree with these results:

- Deterministic routing probe: all Kimi prefix, adapter, and Anthropic-routing
  checks passed.
- Unit suite: 664 passed.
- Kimi and Headroom targeted validation: 45 passed.
- Integration suite excluding the pre-existing Copilot parity file: 221 passed,
  6 skipped.
- Full integration suite: 234 passed, 6 skipped, with two Copilot-only failures
  in `tests/integration/test_responses_real_adapter_parity.py` because the
  existing uncommitted Copilot path omitted `previous_response_id` on follow-up
  responses.
- Python compilation and `git diff --check`: passed.
- No live Kimi call was run because the session found no local Kimi OAuth
  credential artifact.

Treat those two Copilot failures as a separate dirty-worktree baseline. Do not
weaken their assertions or change Kimi code to hide them.

## File ownership and collision notes

The worktree already contained unrelated modifications in Copilot and Codex
sync files before Kimi work began. Preserve them. The Kimi implementation
should primarily own:

- `src/reverso/protocols/adapters/kimi.py`
- `tests/unit/test_kimi_adapter.py`
- `tests/integration/test_kimi_surfaces.py`
- Kimi-specific additions to routing, composition, surface registry, and
  capability data
- ADR 0017 and this specification/handoff

Avoid broad edits in `src/reverso/protocols/adapters/copilot.py`,
`src/reverso/codex_sync.py`, `src/reverso/protocols/model_exposure.py`, their
tests, `pyproject.toml`, or `uv.lock` unless a separately approved scope requires
them.

## Completion gate

The handoff is complete only when:

1. All acceptance criteria in the active specification are satisfied.
2. No Kimi secret appears in logs, test output, diffs, or committed fixtures.
3. Both protocol surfaces pass offline regression tests.
4. Headroom behavior is proven without provider-specific code.
5. A credentialed local smoke passes, or the lack of credentials is explicitly
   recorded as the only remaining validation gap.
6. Any unrelated baseline failures are reported separately with exact test ids.

## Human setup handout

1. Install or update Kimi CLI.
2. Run `kimi /login` and finish OAuth authorization.
3. Do not copy the token into repository configuration.
4. Start or restart Reverso after the Kimi provider code is installed.
5. Query `http://127.0.0.1:64946/kimi/v1/models` and select a returned model id.
6. Configure Responses clients with base URL
   `http://127.0.0.1:64946/kimi/v1`.
7. Configure Anthropic clients with base URL
   `http://127.0.0.1:64946/kimi` or use `kimi/<model>` on the unprefixed
   Messages surface.
8. Use `KIMI_BEARER_TOKEN` only when the OAuth artifact is deliberately
   unavailable.
9. Check `/usage/headroom` to confirm aggregate compression behavior without
   exposing prompt content.
