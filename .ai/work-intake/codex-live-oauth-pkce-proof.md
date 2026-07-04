# Issue: Plan live Codex OAuth/PKCE backend proof integration

Completion: PRs #70, #71, and #72 merged. Archived as codex-direct reference evidence.
Labels: `needs-triage`, `ready-for-agent`, `northstar`, `spike`, `auth-risk`, `manual-live-proof`
Status: complete

## Traceability

- Spec: `docs/specifications/ARCHIVED/codex-live-oauth-pkce-proof.md`
- Parent: `.ai/work-intake/codex-oauth-provider-reverso.md`
- Version impact: proof harness only; no runtime mount or default behavior change

## What to build

Prepare a local-only, opt-in live proof plan/harness for real Codex OAuth/PKCE / ChatGPT-managed backend access. The official SDK/app-server lane must be evaluated before private direct HTTP. Any live run must be secret-safe and manually gated.

## Acceptance criteria

- [ ] Live proof cannot run by default.
- [ ] Official-supported lane is evaluated first.
- [ ] Private backend lane is explicit opt-in only.
- [ ] No secrets are emitted to logs/artifacts.
- [ ] ADR 0016 records the live proof decision.

## Blocked by

None for planning/harness. Actual live execution requires trusted local machine auth state and explicit opt-in env flags.

## Autobahn Slice 1 implementation evidence

Implemented default-skipped harness:

- `src/reverso/protocols/adapters/codex_live_proof.py`
- `scripts/codex-live-proof.py`
- `tests/unit/test_codex_live_proof.py`

Safety boundary:

- Official lane requires `REVERSO_CODEX_LIVE_PROOF=1` or `REVERSO_CODEX_OFFICIAL_LIVE_PROOF=1`.
- Private direct lane requires `REVERSO_CODEX_DIRECT_LIVE_PROOF=1`.
- Default tests prove auth/subprocess/upstream are not touched without opt-in.
- Manual runner: `scripts/codex-live-proof.py --lane official --json` or `--lane direct --json`.

Verification:

- `scripts/codex-live-proof.py --lane official --json` -> skipped without touching live auth/network.
- `scripts/codex-live-proof.py --lane direct --json` -> skipped without touching live auth/network.
- `uv run pytest tests/unit/test_codex_live_proof.py tests/unit/test_codex_direct_adapter.py -q` -> 17 passed.
