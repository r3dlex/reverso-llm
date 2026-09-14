# Work Item: reverso-opencode-client-surface — OpenCode profiles, aliases, Headroom, Anthropic-preference

- **Traceability node:** `issue:reverso:reverso-opencode-client-surface`
- **Spec:** [`docs/specifications/ACTIVE/reverso-opencode-client-surface.md`](../../docs/specifications/ACTIVE/reverso-opencode-client-surface.md)
- **State:** `complete` (shipped as PR #143, B1-B5 one atomic PR)
- **Owner:** autobahn (northstar → slice → autobahn loop)
- **Spans repos:** `r3dlex/reverso` only
- **Hosted reconciliation:** GitHub Issues mirror only when configured AND authorized (fail-closed; not done).

## Summary

Declare OpenCode as a Reverso client surface: an `opencode` group in
`config/supported-client-surfaces.json`, a default `opencode-reverso` profile
plus optional per-route `opencode-<route>` variants mirroring the `claude-*`
launcher naming, and a new `opencode_sync.py` sibling of `codex_sync.py` using
existing prepare/stage/lock/fail-closed semantics. The generated OpenCode-side
config binds the `anthropic` provider to the inbound Anthropic Messages surface
(`baseURL: http://127.0.0.1:64946`) — operator rule prefers the Anthropic API
when both surfaces serve the backend; Responses stays reachable but secondary.
Headroom-on-path is asserted by contract test on the Messages route; model
catalog exposure follows existing prefix rules (no new namespaces);
`scripts/opencode-live-proof.py` mirrors `codex-live-proof.py`.

## Decisions

1. Anthropic Messages surface is the default binding for OpenCode.
2. Sync never edits user-owned `opencode.jsonc`; it writes managed fragments +
   prints the manual step (mirrors Codex/Claude convergence contract).
3. Routing authority remains with `surface_registry`; frozen `ProviderAdapter`
   Protocol untouched (ADR 0002 §11.3).
4. No new model-prefix namespaces; MiniMax/DeepSeek/GPT(Codex)/Claude stay bare.

## Acceptance criteria

- [x] Surface registry exposes the `opencode` client group (unit test).
- [x] `opencode_sync.py` honors lock token, staged copies, provider-only mutations, fail-closed discovery (tests mirroring `test_codex_sync.py` invariants).
- [x] Profiles/aliases generated prefix-correct; managed artifacts preserved when discovery fails or is empty.
- [x] Headroom-on-path contract test red-then-green for the OpenCode route.
- [x] Live proof script green against a running gateway (transport-injected unit contract; opt-in live lane via `REVERSO_OPENCODE_CLIENT_LIVE_PROOF=1`).

Evidence: `.ai/evidence/reverso-opencode-client-surface-tdd.md`.

## Slicing

5 sliced goals B1–B5; graph `B1→B2→B3; B4 after B3; all→B5`. Sliced inline
under explicit operator autonomous directive (no interactive interview);
adversarial grill pass skipped by same directive.
