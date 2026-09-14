---
type: specification
project: reverso
title: Reverso OpenCode client surface - profiles, aliases, Headroom, and Anthropic-preference
status: active
slug: reverso-opencode-client-surface
date: 2026-08-22
---

# Reverso OpenCode client surface

> Scope note (2026-08-23): the OpenCode Go *subscription backend* (`provider-opencode`,
> OCG goal series, PRs #133 through #141) is a separate, already-shipped layer.
> This spec covers the complementary client-side layer: configuring the OpenCode
> harness to consume Reverso. No `OPENCODE_CONFIG`-driven fragment sync exists on
> main prior to this work.

## State A

Reverso serves two inbound surfaces on `127.0.0.1:64946`: the native OpenAI
Responses gateway (`reverso.protocols.responses_app`) and the inbound Anthropic
Messages API (`reverso.protocols.anthropic_app`, `/v1/messages`) used by Claude
Code and the Claude Agent SDK via `ANTHROPIC_BASE_URL`. Client convergence is
manifest-driven for Codex profiles and Claude Code launchers
(`config/supported-client-surfaces.json`, `codex_sync.py`,
`claude_code_sync.py`, `client_sync*` prepare/lock/stage semantics). Embedded
Headroom compression is wired at protocol level on both surfaces
(`protocols/headroom_compression.py` referenced by `responses_app.py` and
`anthropic_app.py`). Model catalog exposure applies selector prefixes for
collision-prone providers (`copilot/<model>`, `auggie/<model>`,
`agy/<model>`) and keeps MiniMax, DeepSeek, GPT (Codex), and Claude ids bare.
OpenCode is not a declared client surface: no profile, no launcher set, no sync
module, no live proof.

## State B

OpenCode is a declared Reverso client surface with one idempotent convergence
path:

1. `supported-client-surfaces.json` declares an `opencode` client group with a
   default profile `opencode-reverso` routing all Reverso-managed routes.
2. A new `opencode_sync.py` (sibling of `codex_sync.py`) prepares and writes
   the OpenCode-side configuration using existing client-sync semantics:
   explicit lock token, staged copies, provider-only mutations,
   fail-closed on failed discovery, managed artifacts preserved.
3. **Surface preference: Anthropic Messages.** The generated OpenCode config
   points the `anthropic` provider at the inbound Messages surface
   (`baseURL: http://127.0.0.1:64946`), because (a) the operator contract
   prefers the Anthropic API whenever both surfaces serve the needed backend,
   (b) both surfaces already embed Headroom, and (c) the Messages surface is
   the subscription-backed first-party path. The Responses surface remains
   reachable but is not the default for OpenCode.
4. Optional per-route variants mirror the `claude-*` launcher naming:
   `opencode-reverso` (all routes) plus `opencode-<route>` single-route
   profiles, following the same ownership rules (never overwrite user-owned
   files).
5. Model exposure for the OpenCode catalog follows the existing prefix rules;
   no new prefix namespaces are introduced.
6. Headroom-on-path is asserted by contract test: requests proxied for the
   OpenCode profile traverse `headroom_compression` on the Messages surface.
7. `scripts/opencode-live-proof.py` mirrors `codex-live-proof.py`.

## Goals

1. Declare and route the `opencode` client surface through `surface_registry`
   without touching the frozen `ProviderAdapter` Protocol.
2. Provide manifest-driven, fail-closed profile sync that writes only
   Reverso-managed OpenCode config fragments.
3. Keep embedded Headroom active on the OpenCode path and prove it by test.
4. Expose models to OpenCode under the established prefix rules.
5. Verify end-to-end with a live proof script (red-then-green evidence).

## Non-goals

- Modifying `ProviderAdapter` (frozen, ADR 0002 §11.3).
- Serving OpenCode from the legacy LiteLLM fallthrough.
- ai-catapult / skills payload work (umbrella spec
  `opencode-omo-harness-convergence`).

## Key decisions

- D1: Anthropic Messages surface is the default binding for the OpenCode
  profile; Responses stays secondary (operator preference rule).
- D2: Config delivery mirrors Codex/Claude convergence: generated fragments +
  printed manual step for anything user-owned in `opencode.jsonc`; the sync
  never edits `opencode.jsonc` directly (strict-schema, user-owned).
- D3: Profile/alias naming reuses the established `<harness>-<route>`
  convention; `opencode-reverso` is the alias for all-routes.

## Slices (one PR each)

| ID | Slice | Verification |
|----|-------|--------------|
| B1 | Surface declaration: `opencode` group + registry wiring | unit test on surface registry |
| B2 | `opencode_sync.py`: prepare/stage/write with lock semantics | unit tests mirroring `test_codex_sync.py` invariants |
| B3 | Profile generation: `opencode-reverso` + per-route variants, prefix-correct catalog | model-exposure contract tests |
| B4 | Headroom-on-path contract test for OpenCode route | red-then-green protocol test |
| B5 | `scripts/opencode-live-proof.py` + docs update | live proof green against running gateway |

## Traceability

- Parent work item: `issue:reverso:reverso-opencode-client-surface`
- Depends on nothing in the umbrella spec; independent repo, own PRs.
