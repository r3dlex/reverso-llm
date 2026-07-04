# Codex Direct Post-Default CLI Cleanup

Date: 2026-07-04
Status: Northstar planned
Dependency: PR #71, `Default-enable codex direct on loopback`, merged 2026-07-04.

## Intent

After `codex-direct` became the local-loopback default, clean the CLI/profile/operator surfaces and nearby repository hygiene so the default is understandable, safe, and easy to maintain. This is a cleanup stage, not a backend feature stage.

## Current baseline

- `codex-direct` mounts by default only when `REVERSO_HOST` is absent or `127.0.0.1`.
- `REVERSO_CODEX_DIRECT_BACKEND=0`, `false`, `no`, or `off` disables backend and profile exposure.
- Non-loopback `REVERSO_HOST` suppresses backend and profile exposure even with a truthy backend env.
- Built-in Codex GPT model IDs remain bare selectable defaults.
- `codex-direct` selectors remain provider scoped as `codex-direct/<model>` and `codex-direct-<model>`.

## Goals

1. Remove stale default-off wording from CLI-facing instructions, generated profile docs, and operator docs while preserving the kill-switch posture.
2. Keep CLI/profile sync behavior aligned with the merged model selector rule.
3. Reduce hardcoded provider allowlists that drift when first-party response providers are added.
4. Clean nearby repo hygiene only when it is low-risk, behavior-preserving, and covered by tests.
5. Leave live-token proof lanes opt-in and secret-free.

## Non-goals

- No changes to ChatGPT/Codex private API behavior.
- No non-loopback or hosted default enablement.
- No replacement of built-in GPT defaults.
- No new dependency.
- No broad refactor outside CLI/profile/docs/test hygiene.

## Acceptance criteria

- CLI sync and profile generation expose `codex-direct` consistently with the loopback default and kill switch.
- Docs do not imply a backend env flag is required for normal local loopback use.
- Any provider-list tests derive from canonical provider sources or explicitly document why a fixed allowlist is required.
- `REVERSO_HOST=0.0.0.0` remains fail-closed for backend mount and profile exposure.
- Full suite remains green, including integration tests.

## Autobahn slices

1. CLI/profile surface audit: inspect Codex sync, profile rendering, local config instructions, README snippets, and operator UX text.
2. CLI/profile cleanup: update stale default-off wording, preserve bare GPT defaults, preserve provider-scoped `codex-direct`, and adjust regression tests for generated CLI/profile output.
3. Provider-list hygiene: replace hardcoded first-party Responses provider lists with canonical sources where safe; keep `/codex` exclusion distinct from `/codex-direct` provider coverage.
4. Documentation and ADR cleanup: align ADR 0016, active spec, README/operator docs, and local handoff notes; explicitly document kill switch and non-loopback no-go.
5. Repo hygiene pass: remove or ignore local generated artifacts only if they are unintended and not tracked; avoid broad formatting churn.

## Verification gate

- Targeted CLI/profile tests.
- `uvx prek run --all-files`.
- `.venv/bin/python -m pytest -q --tb=short`.
- `scripts/validate-rules.sh`.
- `scripts/archgate.sh .rules.ts`.
- `git diff --check`.
- Architect and code-reviewer clear before merge.

## Risks

- Provider allowlist cleanup can weaken exclusion coverage if `/codex` and `/codex-direct` are conflated.
- CLI profile sync can accidentally shadow built-in GPT models.
- Docs can overstate safety if they omit the loopback-only boundary.

## Stop condition

Stop when CLI/profile/operator surfaces and provider-list tests are aligned with the merged default-on contract, all gates are green, and review clears. Do not expand into backend behavior changes without a new ADR or explicit follow-up scope.
