---
title: Northstar A to B handoff for Kimi automatic login K3 runtime convergence
slug: kimi-auto-login-k3-runtime-convergence
status: ready-for-autobahn
issue: .ai/work-intake/kimi-auto-login-k3-runtime-convergence.md
specification: docs/specifications/ACTIVE/kimi-auto-login-k3-runtime-convergence.md
prd: .omx/plans/prd-kimi-auto-login-k3-runtime-convergence.md
test_specification: .omx/plans/test-spec-kimi-auto-login-k3-runtime-convergence.md
consensus: .omx/state/kimi-auto-login-k3-runtime-convergence-ralplan-consensus.json
---

# Northstar A to B handoff: Kimi automatic login K3 runtime convergence

## Authority

This handoff is the only Northstar authority for this work. The archived
`codex-oauth-provider-reverso` handoff is unrelated and must not be consumed.

- Spec: `docs/specifications/ACTIVE/kimi-auto-login-k3-runtime-convergence.md`
- Work item: `.ai/work-intake/kimi-auto-login-k3-runtime-convergence.md`
- PRD: `.omx/plans/prd-kimi-auto-login-k3-runtime-convergence.md`
- Test specification: `.omx/plans/test-spec-kimi-auto-login-k3-runtime-convergence.md`
- Architect verdict: iteration 6 `APPROVE`
- Critic verdict: iteration 2 `APPROVE`

## A state

- The Kimi credential artifact is absent.
- The running LaunchAgent uses the stale deployed checkout and exposes K2.5.
- The development checkout contains preserved dirty K3 work plus unrelated user
  changes.
- Missing authentication currently fails the original request with a retryable
  HTTP 502.
- Existing governance requires separate `kimi login` and must be updated before
  automatic local login is implemented.

## B state

- One gateway-owned single-flight coordinator supervises official `kimi login`
  across Responses and Anthropic surfaces.
- The official CLI owns interactive authorization and initial artifact creation.
  Existing `KimiOAuthAuth` retains refresh locking, refresh rotation, one retry
  on HTTP 401, and atomic persistence of refreshed credentials.
- Cancellation, timeout, shutdown, child cleanup, and secret-safe output draining
  are bounded and tested without changing the frozen `ProviderAdapter`.
- Codex exposes only `kimi-k3`, sends upstream `k3`, and keeps profile/catalog
  context metadata at `1048576`.
- Canonical deployment provenance prevents a stale checkout from restoring
  K2.5.
- The installed proxy uses the governed mode-`0700` Reverso Kimi home at
  `~/Library/Application Support/reverso/kimi-code`; provenance and drift
  checks bind that path without exposing it to the daemon or touching
  `~/.kimi-code`.
- Automatic merge occurs only with local tests, hosted CI, resolved reviews,
  repository policy, and valid host-policy merge authority all green.

## Sliced goals

| Goal | Result | Dependency | Future PR |
|---|---|---|---|
| S1 | Shared login coordinator, trigger boundary, and governance authorization | none | one isolated PR |
| S2 | Cross-surface lifecycle, cancellation, shutdown, and ASGI acceptance | S1 | one isolated PR |
| S3 | K3 alias, upstream mapping, model exposure, profile, catalog, and context convergence | none | one isolated PR |
| S4 | Canonical LaunchAgent provenance and deployment drift enforcement | S1, S3 | one isolated PR |
| S4A | Isolated Reverso Kimi home provenance and drift enforcement | S4 | one isolated PR |
| S5 | Governed deployment, live OAuth acceptance, and sanitized evidence handoff | S2, S4A | one isolated PR |

S1 and S3 may run in parallel in separate worktrees. All other dependency edges
are sequential. Preserve every unrelated dirty path and never reset, clean,
destructively stash, overwrite, or accidentally commit it.

## Execution gates

Autobahn must consume the PRD and test specification exactly. Each goal requires
red-green proof, an independent Architect plus code-reviewer loop, local and
hosted CI, resolved review comments, and fail-closed host-policy merge authority.
Green CI alone never authorizes merge.

S5 first uses the isolated credential-free `reverso_kimi` plus `kimi-k3`
bootstrap configuration against the proxy's governed isolated Kimi home. It
must not read, copy, delete, or mutate `~/.kimi-code`. Sync and normal
generated-profile acceptance occur only after the original request resumes.
If browser authorization cannot be completed, S5 records `blocked-external`
in the named sanitized evidence artifact without treating live assertions as
passed.

## Traceability

- Manifest record:
  `optional_branches[id=northstar-handoff-kimi-auto-login-k3-runtime-convergence]`
- Issue node: `issue:reverso-root:kimi-auto-login-k3-runtime-convergence`
- Plan node:
  `plan:reverso-root:northstar-kimi-auto-login-k3-runtime-convergence`
- Handoff node:
  `handoff:reverso-root:northstar-kimi-auto-login-k3-runtime-convergence`
- Required edges: issue to plan `planned-by`, plan to handoff `summarized-by`
