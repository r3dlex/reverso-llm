# Codex live OAuth/PKCE backend proof integration

Completion: PRs #70, #71, and #72 merged. Archived as codex-direct reference evidence.
Labels: `needs-triage`, `ready-for-agent`, `northstar`, `auth-risk`, `manual-live-proof`
Status: complete
Spec: `docs/specifications/ARCHIVED/codex-live-oauth-pkce-backend-integration.md`
Northstar handoff: `.ai/handoff/northstar-codex-live-oauth-pkce-backend-integration.md`

## Traceability

- BRD: None provided
- PRD/spec: `docs/specifications/ARCHIVED/codex-live-oauth-pkce-backend-integration.md`
- Parent: `.ai/work-intake/codex-live-oauth-pkce-proof.md`
- Version impact: production backend path after live proof and ADR go/no-go; default behavior remains safe

## What to build

Plan and execute a production-oriented Reverso integration for live Codex OAuth/PKCE. The implementation must prove official-supported ChatGPT-managed Codex access first, direct HTTP second, then expose a real selectable backend through normal Reverso profile/model surfaces only after proof and ADR gates are satisfied.

## Acceptance criteria

- [ ] Live proof is local-only, opt-in, and skipped by default.
- [ ] Proof reports are JSON and secret-free.
- [ ] Official-supported lane is attempted before direct HTTP in operator docs.
- [ ] Direct HTTP failures are fail-closed and never silently fall back.
- [ ] Production backend route/profile is implemented behind explicit config, proof gate, ADR gate, and kill switch.
- [ ] Tests cover default safety, configured mount, model/profile selection, redaction, error mapping, streaming/non-streaming behavior, and proof report schema.
- [ ] ADR 0016 or successor records go/no-go before production exposure is enabled.

## Suggested slices

1. Proof harness hardening.
2. Official-first live proof lane.
3. Direct HTTP live proof lane.
4. Production backend mount behind gates.
5. Model/profile selection and operator UX.
6. Evidence packet and ADR go/no-go.

## Blocked by

None for code/tests/docs. Actual live OAuth/network execution is blocked until a local operator explicitly opts in with the documented environment variables. Production exposure is blocked until live proof and ADR go/no-go pass.
