# Copilot picker completeness

| | |
|---|---|
| **Slug** | `copilot-picker-completeness` |
| **Repo** | `r3dlex/reverso-llm` |
| **Status** | active |
| **Raised** | 2026-07-05 via `$northstar` |
| **Spec owner** | unassigned |
| **Spans** | `src/reverso/protocols/model_exposure.py`, `src/reverso/codex_sync.py`, `tests/unit/test_model_exposure.py`, `tests/unit/test_codex_sync.py`, `docs/specifications/ACTIVE/copilot-picker-completeness.md` |

## A → B

**A.** The Codex `/model` picker for the `reverso_copilot` provider shows 19 of 30 live Copilot models. The `codex_sync` filter (`model_exposure.codex_responses_compatible_model_ids`) was written before commit `4507019` and ADR 0011 added Claude/Gemini serving on Copilot's `/chat/completions` route, and was never updated. Concretely, `claude-fable-5` (the model in the user's current session) and 10 other chat-route models are dropped from the picker.

A second-order symptom appeared as a hand-patch: an OMX session in a different repo (`rib-workspace`) produced a manual `copilot.json` that leaked three Claude-surface model IDs (`claude-sonnet-5`, `claude-opus-4-8`, `claude-haiku-4-5-20251001`) into the Copilot catalog with the wrong slug format (no `copilot/` prefix). The IDs are real -- they live on the `/claude` surface -- but the OMX session had no surface-separation invariant to keep them apart.

**B.** The Copilot picker reflects all 30 live Copilot models with correctly-prefixed `copilot/<model>` slugs (including `claude-fable-5`). The filter uses `copilot_model_route` (the single source of truth for which IDs Copilot serves on either route) as the eligibility gate. A live-pinning integration test fails loudly if a chat-route model is dropped from the picker again. The per-provider catalog files (`copilot.json`, `claude.json`, `auggie.json`, `deepseek.json`) are understood as **surface-scoped**; cross-surface ID leakage is a documented footgun, recovered by re-running `reverso-codex-sync`.

## Acceptance criteria

1. `copilot.json` (under `~/.codex-reverso/reverso/`) contains all models returned by the live gateway at `/copilot/v1/models` that pass `copilot_model_route`, each with a `copilot/` slug prefix.
2. `claude-fable-5` is selectable in the Codex picker under the `reverso_copilot` provider.
3. `tests/unit/test_model_exposure.py::test_model_exposure_owns_codex_responses_model_eligibility` covers the broadened semantic (gpt-* + claude-* + gemini-*) and the unsafe-id rejection cases.
4. `tests/unit/test_codex_sync.py::test_fetch_all_keeps_copilot_served_models_via_either_route` covers the equivalent at the sync layer.
5. An integration test (`tests/integration/test_copilot_picker_live.py`) fetches the live `/copilot/v1/models`, asserts the kept set contains every chat-route model in the live response, and runs as a local-only test (gated by `RUN_LIVE_PICKER=1` or similar) so it does not require a live gateway in CI.
6. The hand-patch antipattern is documented in a short note (`docs/learning/notes/copilot-picker-surface-separation.md` or inline in the spec) with the symptom and the recovery.
7. No regression: all 631 unit tests pass.

## Sliced goals (one PR each)

### S1 -- `fix(codex-sync): include copilot chat-route models in picker`
- **Files:** `src/reverso/protocols/model_exposure.py`, `tests/unit/test_model_exposure.py`, `tests/unit/test_codex_sync.py`.
- **Behavior:** swap the copilot filter from `is_copilot_responses_model_id` to `copilot_model_route is not None`. Update the two unit tests to cover the broadened semantic and the unsafe-id rejection cases.
- **Verify:** `uv run pytest tests/unit -p no:randomly -q` shows 631/631 passing; the kept set for `copilot` grows from 19 to 30.
- **After merge:** run `reverso-codex-sync --config ~/.codex-reverso/config.toml` once to regenerate `~/.codex-reverso/reverso/copilot.json` and `claude.json`.

### S2 -- `test(codex-sync): live-pinning test for copilot picker completeness`
- **Files:** `tests/integration/test_copilot_picker_live.py` (new), `pyproject.toml` marker update if needed.
- **Behavior:** when the live gateway is reachable, fetch `/copilot/v1/models`, run the filter, and assert the kept set contains `claude-fable-5` and at least one model per known chat-route family (claude-*, gemini-*). When the gateway is unreachable, skip.
- **Verify:** `RUN_LIVE_PICKER=1 uv run pytest tests/integration/test_copilot_picker_live.py -p no:randomly` passes against the live gateway; without the env var, the test is collected-but-skipped.

### S3 -- `docs: surface-separation invariant for per-provider catalogs`
- **Files:** `docs/learning/notes/copilot-picker-surface-separation.md` (new), or inline in the active spec.
- **Behavior:** short note that explains (a) the per-provider catalog files are surface-scoped (one per gateway prefix), (b) Claude-surface IDs in a Copilot catalog is the OMX hand-patch antipattern, (c) the recovery is `reverso-codex-sync`.
- **Verify:** the note is linked from the spec and from `AGENTS.md`'s "Specification documents" table.

## Architectural notes (for follow-up, not in scope)

- The function name `codex_responses_compatible_model_ids` is now slightly misleading; a future rename to `codex_picker_visible_model_ids` would prevent the same misunderstanding. Not done without explicit owner sign-off.
- `/copilot/v1/chat/completions` is **not** a public route on the gateway (only `/copilot/v1/responses` is exposed; the adapter dispatches internally). The picker's entry point is correct; the runtime routing is owned by `copilot_models.copilot_model_route`.
- The known `reverso_codex-direct` 502 (litellm `Unmapped LLM provider` for `custom/claude-*` in `config/litellm_config.yaml`) is a separate concern; the sync's `skip_errors=True` keeps it out of the regenerated catalog.

## References

- ADR 0011: `docs/architecture/adr/0011-copilot-chat-completions-for-anthropic-google.md`
- Commit `4507019` -- `feat(copilot): serve claude/gemini via Copilot /chat/completions`
- `src/reverso/protocols/copilot_models.py` -- `copilot_model_route` (single routing authority)
- `src/reverso/protocols/model_exposure.py:138-145` -- the filter that was too narrow
- `src/reverso/codex_sync.py:49-52` -- the sync-time caller
- `tests/unit/test_codex_sync.py:136-170` -- the test that pinned the old (wrong) semantic
