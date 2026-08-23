---
title: "Branch protection requires a check the workflow marks continue-on-error"
status: ready-for-agent
state: ready-for-agent
category: defect
slug: ci-self-hosted-required-but-tolerated
owner: unassigned
execution_type: AFK
---

# `test (self-hosted) (3.12)` is required by protection but tolerated by the workflow

Structural conflict between two configurations that disagree about whether the
self-hosted runner is allowed to fail.

## The conflict

The workflow marks the self-hosted test job `continue-on-error: true`, so the
workflow treats its failure as tolerated and lets the hosted runner carry the
result. Branch protection lists `test (self-hosted) (3.12)` as a REQUIRED status
check.

Consequence: whenever the self-hosted runner is unhealthy, offline or busy, every
pull request blocks on a check the workflow itself considers optional. The two
settings cannot both be right.

## Why it matters now

Every OpenCode Go slice merged cleanly, so this did not bite during that work, but
it is latent: a single unhealthy runner blocks all merges with no failing test to
point at. The fix is a one-line decision, not an investigation.

## Resolve one way or the other

Either the check is genuinely required, in which case drop
`continue-on-error: true` so a real failure fails loudly and the tolerance stops
being a lie; or it is genuinely optional, in which case remove it from the
required set in branch protection and let the hosted matrix gate merges.

Deciding needs an owner: it is a repository-administration change, not a code
change, and it affects who can merge when.

## Acceptance criteria

- [ ] The workflow's tolerance and the protection requirement agree.
- [ ] An unhealthy self-hosted runner either blocks merges deliberately, or does not block them at all.
- [ ] The chosen answer is recorded, so the next person does not rediscover the conflict.
