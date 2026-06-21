---
type: adr
project: reverso
id: 0007
title: Codex GPT models on the Anthropic surface via ChatGPT OAuth
status: Accepted
date: 2026-06-21
supersedes: none
related:
  - docs/architecture/adr/0005-bounded-cli-spine.md
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - .omc/plans/prd-codex-anthropic-oauth.md
---

# ADR 0007: Codex GPT models on the Anthropic surface via ChatGPT OAuth

## Status

Accepted, 2026-06-21. Docs-first deliverable for Milestone 2: a first-party Codex
backend that exposes gpt-* models on the inbound Anthropic Messages surface (ADR 0006),
authenticated by the ChatGPT/Codex OAuth subscription through the local Codex CLI. This
ADR plus the companion sections in `README.md` and `docs/03-architecture.md` define the
milestone boundary. Implementation proceeds via per-goal PRs after this ADR is reviewed.

## Context

Milestone 1 (ADR 0006) added the inbound Anthropic Messages API surface so that Claude
Code and the Claude Agent SDK, pointed at Reverso via `ANTHROPIC_BASE_URL`, can reach the
non-claude backends (copilot, deepseek, auggie). They still cannot reach OpenAI gpt-*
models. Milestone 2 adds a first-party Codex backend that exposes gpt-* models on the
Anthropic surface ONLY, with the Codex Responses-shaped output converted to the Anthropic
Messages shape by reusing the Milestone 1 translation layer.

This is the symmetric mirror of the Milestone 1 design. The Claude Code CLI backend is
Responses-surface-only (it is consumed by Codex), so the Codex CLI backend is
Anthropic-surface-only (it is consumed by Claude Code). gpt-on-the-Responses-surface is
circular (Codex talking to Codex) and is removed by this milestone, not relocated.

Today gpt models are served ONLY through the legacy LiteLLM custom provider
`openai_cli_provider.py` (`src/reverso/proxy/openai_cli_provider.py`), which runs
`codex exec` as a subprocess and parses newline-delimited Responses-style JSON events.
The config rows in `config/litellm_config.yaml` use `custom_llm_provider: openai_cli`.
There is no first-party Codex adapter; `build_adapters` in `src/reverso/proxy/compose.py`
returns claude, copilot, auggie, and deepseek only. Reverso does not manage Codex OAuth
today; the Codex CLI is expected to be pre-authenticated.

The claude backend (`src/reverso/protocols/adapters/claude.py`) is the template:
`ClaudeOAuthAuth` reads and validates the OAuth artifact (Keychain or local credentials
file), gates on token presence and expiry, and injects the token into the CLI child via
`stream_bounded_cli` (`cli_spine.py`, ADR 0005).

Hard repo constraints (`AGENTS.md`): bind `127.0.0.1:64946` only; no secrets in version
control or logs; `uv`-managed Python; frontmatter on every markdown file; no em dash, en
dash, or smart-quote characters; never delete spec content (augment or deprecate in
place). OAuth token material is never logged (use the existing redaction). The chosen
auth path is the ChatGPT OAuth subscription only; there is no OpenAI API key billing path.

## Decision

Add a first-party Codex backend that produces the internal Responses contract from the
Codex CLI under ChatGPT OAuth, exposed Anthropic-surface-only as a single data row,
replacing the legacy `openai_cli` path with a clean cut.

### D1. First-party CodexAdapter over the bounded CLI spine

A first-party `CodexAdapter` implements the FROZEN `ProviderAdapter` Protocol
(`create_response`, `stream_response`, `list_models`, `get_response`,
`list_input_items`). It invokes the Codex CLI (`codex exec`) via the bounded `cli_spine`
(ADR 0005) for both non-streaming (`run_bounded_cli`) and streaming
(`stream_bounded_cli`), inheriting the wall-clock bound, redaction-before-logging,
cause suppression, and kill-on-abandon contract. The adapter contributes only its argv,
an optional child environment, and the Codex event line parsing. Codex Responses-style
events are parsed into the internal Responses contract (`ResponsesRequest`,
`ResponseEnvelope`, `SSEEvent`); the existing Milestone 1 Anthropic translation,
streaming, and capability-gating layers then convert that internal contract to and from
Anthropic Messages.

There is NO direct OpenAI Platform Responses API call over HTTP and NO `openai-python`
runtime dependency. Those references were considered and informed the decision (see
Alternatives), but were not adopted: the chosen path rides the same subscription-backed
Codex CLI surface that the user already logs in with.

### D2. CodexOAuthAuth: managed gate plus token injection on the ChatGPT subscription

Authentication is the ChatGPT/Codex OAuth SUBSCRIPTION (the credential written by
`codex login`), NOT an OpenAI API key. A new `CodexOAuthAuth` resolver mirrors
`ClaudeOAuthAuth`: it reads and validates the ChatGPT OAuth artifact directly, asserts
the access token is present and (when observable) not expired, and fails closed with a
structured Anthropic error when the session is missing or expired. The gate is
falsifiable in the same sense as the claude gate: it asserts the subscription OAuth path
and never resolves to a metered API key or consumes an API key from the environment. The
resolved OAuth token is injected into the Codex CLI child environment only and is never
logged.

The exact Codex OAuth artifact location and format are an explicit discovery spike before
the resolver is implemented (see Open spike). It is unknown today whether the artifact is
a Keychain entry, a file such as `~/.codex/auth.json`, or both, what the token field names
and expiry field are, and whether the Codex CLI exposes a token injection environment
variable analogous to `CLAUDE_CODE_OAUTH_TOKEN` or instead reads its own stored session
while Reverso only validates the artifact.

### D3. gpt is Anthropic-surface-only, as a single SURFACE_BACKENDS data row

`codex` is added to `SURFACE_BACKENDS["anthropic"]` as ONE data row, and `CodexAdapter`
is registered in `build_adapters`. The five gpt model ids (gpt-5.5, gpt-5.4,
gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1) resolve to the codex backend on the Anthropic
surface through the existing `surface_registry` authority. The codex backend must NEVER be
reachable on the Responses surface; this is the exact mirror of the Milestone 1
claude-exclusion (Claude Code talking to a claude backend is circular; Codex talking to a
codex backend is circular). The exclusion is enforced as data plus a negative test that
asserts gpt-* models and the codex backend are not reachable on the Responses surface.

### D4. Clean-cut removal of the legacy LiteLLM openai_cli path

The legacy LiteLLM gpt path is removed in this milestone, not kept as a coexisting
fallback. `openai_cli_provider.py` and the `openai_cli` gpt rows in
`config/litellm_config.yaml` are removed; `codex_sync.py` is reconciled with the removal
(it writes Reverso provider listings into the Codex config and references the gpt rows).
After the clean cut the first-party `CodexAdapter` is the sole gpt path. The contrarian
probe in the interview confirmed the clean cut over a staged removal; the product owner
accepted the rollback tradeoff (no legacy fallback) on the strength of the parity suite,
the loopback smoke test, and a fast `git` revert.

## Decision Drivers

1. The ChatGPT subscription is the intended billing relationship; an OpenAI API key path
   would meter usage against a different account and contradicts the subscription-backed
   premise of Reverso.
2. The frozen `ProviderAdapter` Protocol (ADR 0002 11.3) must stay frozen; the Codex
   backend is a fifth adapter producing the internal Responses contract, not a new
   Protocol method or a new inbound surface.
3. ADR 0005 already owns the subprocess safety contract (bound, redaction, kill-on-abandon);
   the Codex backend must reuse it rather than introduce a parallel runner.
4. Milestone 1 (ADR 0006) made surface exposure data, so the Anthropic-surface-only codex
   addition is a single `SURFACE_BACKENDS` row, not a code branch.
5. A coexisting `openai_cli` path would keep a circular gpt-on-the-Responses-surface route
   alive; the clean cut removes the circularity rather than relocating it.

## Alternatives considered

- **Direct OpenAI Platform Responses API via `openai-python` plus an API key.** Rejected:
  this is not the ChatGPT subscription; it bills against a metered API key, which
  contradicts the subscription-backed premise and would add `openai-python` as a runtime
  dependency. The OpenAI Apps SDK auth, `openai-python`, and the API quickstart were
  consulted as references and informed this decision; they were not adopted.
- **ChatGPT OAuth but direct HTTP to the ChatGPT/Codex Responses backend without the
  CLI.** Rejected: this requires reverse-engineering an unsupported backend protocol and
  carries breakage risk; the Codex CLI is the supported, stable surface for the ChatGPT
  subscription and is already what `codex login` authenticates.
- **Coexist: keep `openai_cli` alongside the new CodexAdapter.** Rejected: it keeps the
  circular gpt-on-the-Responses-surface path alive and creates a split gpt path. After a
  contrarian probe the product owner chose the clean cut.

## Scope

- All five gpt models: gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1.
- Text non-streaming Anthropic Messages bodies and Anthropic-native SSE streaming.
- tool_use and tool_result round-trip for gpt-* through the codex backend.
- Exposure on the Anthropic surface ONLY (a single `SURFACE_BACKENDS` row plus the
  model-to-backend mapping for the five ids); never on the Responses surface.
- Reuse of the Milestone 1 translation, streaming mapper, capability gate, and parity
  harness; the CodexAdapter only produces the internal Responses contract.
- Clean-cut removal of `openai_cli_provider.py` and the `openai_cli` gpt rows, with
  `codex_sync.py` and `litellm_config.yaml` reconciled.

Out of scope: any direct OpenAI Platform Responses API call or `openai-python` runtime
dependency; any gpt exposure on the Responses surface; any new inbound endpoint (Milestone
2 reuses the Milestone 1 Anthropic surface unchanged); any change to the frozen
`ProviderAdapter` Protocol, `replay.py`, or the other adapters; image input for the codex
backend unless the Codex CLI is shown to support it (gate per feature policy otherwise).

## Consequences

- A fifth first-party adapter (codex) joins claude, copilot, auggie, and deepseek, but is
  exposed only on the Anthropic surface, mirroring how claude is Responses-surface-only.
- The Anthropic surface gains gpt-* coverage under the ChatGPT subscription with no new
  inbound endpoint and no Protocol change; the capability ceiling and parity harness from
  ADR 0006 extend to cover the codex backend over its supported feature subset.
- Removing `openai_cli` leaves no legacy fallback for gpt; a regression in the OAuth
  adapter has no in-tree backstop. The accepted mitigation is the parity suite plus the
  loopback smoke test and a fast `git` revert.
- The `openai-python` SDK and the OpenAI Platform Responses API stay out of the dependency
  and supply-chain surface; the OpenAI references remain docs-only.
- `codex_sync.py` and `litellm_config.yaml` are reconciled with the removal, so the Codex
  config sync no longer references the removed gpt rows.

## Follow-ups

- Resolve the Codex OAuth artifact spike (see below) before `CodexOAuthAuth` implementation
  and model the resolver on `ClaudeOAuthAuth` once the format is known.
- Extend the Anthropic parity suite to cover the codex backend over its supported feature
  subset, mirroring copilot, deepseek, and auggie.
- Add the negative test that asserts gpt-* models and the codex backend are NOT reachable
  on the Responses surface (the mirror of the Milestone 1 claude-exclusion test).
- Confirm whether image input is feasible through the Codex CLI; gate per feature policy
  until proven.

## Open spike: Codex OAuth artifact format and location

The exact location and format of the Codex ChatGPT OAuth artifact are NOT handled in
Reverso today and are an explicit, time-boxed discovery spike before `CodexOAuthAuth` is
written. The spike must determine: the artifact storage (a Keychain entry name, or a file
such as `~/.codex/auth.json`, or both); the token field names and the expiry field; the
refresh behavior; and whether the Codex CLI exposes a token injection environment variable
analogous to `CLAUDE_CODE_OAUTH_TOKEN`, or whether Reverso must rely on the CLI reading its
own stored session while Reverso only validates the artifact. The precise reconciliation of
`codex_sync.py` after the `openai_cli` rows are removed is resolved as part of the same
clean-cut work.
