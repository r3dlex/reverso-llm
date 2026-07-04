# Issue: Spike direct Codex OAuth provider for Reverso

Completion: PRs #70, #71, and #72 merged. Archived as codex-direct reference evidence.
Labels: `needs-triage`, `ready-for-agent`, `northstar`, `spike`, `auth-risk`
Status: complete

## Traceability

- Spec: `docs/specifications/ARCHIVED/codex-oauth-provider-reverso.md`
- Northstar handoff: `.ai/handoff/northstar-codex-oauth-provider-reverso.md`
- Parent: None
- Version impact: experimental provider path, no default behavior change in first slice

## What to build

Determine whether ChatGPT/Codex OAuth/PKCE credentials can safely support a direct Codex provider path in Reverso without relying on Codex CLI for every request. Keep the current CLI-backed Codex provider as fallback.

## Acceptance criteria

- [ ] Feasibility spike proves or rejects direct HTTP/SDK path with evidence.
- [ ] Existing CLI-backed Codex provider is not removed or made secondary by default.
- [ ] Auth handling is local-only and secret-safe.
- [ ] ADR 0007 is updated or superseded.
- [ ] Thin-slice plan is ready for implementation if spike succeeds.

## Blocked by

None - can start immediately as a local spike.

## Autobahn proof evidence (2026-07-03)

Status: ready-for-human (local proof complete; no merge authority supplied).

Implemented proof slice:

- Added `src/reverso/protocols/adapters/codex_direct.py` as an unmounted experimental ProviderAdapter proof.
- Added `tests/unit/test_codex_direct_adapter.py` with fake-upstream, SSE framing, lifecycle-ID, injected-upstream, and not-mounted guards.
- Added `docs/architecture/adr/0016-experimental-codex-direct-oauth-provider.md`.
- Updated `docs/specifications/ARCHIVED/codex-oauth-provider-reverso.md` with proof status and remaining live-spike gates.

Verification:

- `uv run pytest tests/unit/test_codex_direct_adapter.py tests/unit/test_codex_adapter.py tests/unit/test_codex_oauth.py -q` -> 42 passed.
- `uv run pytest -q` -> 827 passed, 4 skipped, 2 warnings.
- Architect review -> APPROVE.
- Code-reviewer review -> APPROVE.

Merge/cascade:

- Not merged and not cascade-closed because Autobahn merge authority is fail-closed without an explicit host-policy-approved merge token.
