# Work Intake: Codex Direct Post-Default CLI Cleanup

Status: planned
Date: 2026-07-04
Spec: `docs/specifications/ACTIVE/codex-direct-post-default-cli-cleanup.md`
Dependency: PR #71 merged (`b1091cec0373bcd21539627f83e184cafc4f152b`).

## Problem

The backend default has moved from explicit opt-in to local-loopback default-on. CLI/profile/operator surfaces and provider-list tests may still encode the old opt-in mental model or fixed provider sets.

## Desired outcome

A small cleanup PR that makes CLI-facing defaults, docs, and tests match the merged default-on behavior while keeping non-loopback and hosted deployment fail-closed.

## Scope

- Codex CLI sync/profile output and instructions.
- Model selector/profile docs and regression tests.
- Provider-list test hygiene around Responses providers.
- ADR/spec/README/operator text cleanup.
- Low-risk repo hygiene only.

## Out of scope

- Backend behavior changes beyond preserving the merged contract.
- Live-token proof changes.
- Hosted/non-loopback enablement.
- New dependencies.
