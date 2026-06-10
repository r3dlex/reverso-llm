---
type: release-versioning-checklist
project: reverso
host: github
repository: r3dlex/reverso-llm
strategy: hybrid
enforcement: checklist-only
last_updated: 2026-06-10
---

# Release Versioning Checklist - reverso

Per the AI SDLC release-versioning module. Strategy is **Hybrid**: a SemVer-derived base (from `pyproject.toml`, currently `0.1.0`) plus a UTC date and CI trace token. The repository currently has **zero tags**; the manifest at `release.json` is initialized with `tag_creation: "blocked"` until the first guardrail-passed release run.

## Tag format

`v<base>+<utc-date>.<trace-token>` for Hybrid, for example `v0.1.0+2026.06.10.trace-1a2b`. The manifest (`release.json`) is the authoritative audit record; the tag is derived from it.

## How a release happens

1. Run the `Release` workflow (`.github/workflows/release.yml`) via **workflow_dispatch** on `main`.
2. The workflow runs the five guardrails (below) and emits a `release.json` manifest as a run artifact.
3. If and only if every guardrail is `pass` or `skipped` with a documented reason, the workflow creates the tag through the GitHub API using the CI identity. It never pushes tags from a developer machine.
4. Commit the emitted manifest back to the repo root as the audit record for that release.

Deviation from the module default, recorded deliberately: the workflow triggers on `workflow_dispatch` only, NOT on every push to `main`. Enabling on-main release runs is an explicit decision; flip the trigger when release cadence justifies it.

## Tag guardrails (all five gate every tag)

- [ ] **Green CI**: latest CI on the candidate SHA reports success (hosted legs; the self-hosted Pre-commit leg is known-broken and excluded, see `docs/agents/branch-policy-github.md`).
- [ ] **Conventional commits**: commits in the candidate range match `feat:|fix:|docs:|test:|refactor:|perf:|build:|chore:|ci:` grammar (the same nine types the workflow regex enforces). With zero prior tags, the first run validates the HEAD commit only.
- [ ] **Secrets/permissions preflight**: explicit `permissions:` block; preflight logs key names and presence only, never values. No registry publish is configured (private, unpublished package).
- [ ] **No dirty generated state**: clean working tree in the release job.
- [ ] **Protected tag policy**: apply a GitHub ruleset protecting tag pattern `v*` so only CI can create release tags. Until the ruleset is applied this guardrail is `skipped` with that reason recorded in the manifest.

## Tag protection ruleset (admin checklist, not automated)

- [ ] Ruleset target: tags matching `v*`
- [ ] Restrict creation to the GitHub Actions identity
- [ ] Block deletion and non-fast-forward updates (no history rewrites; a bad tag is retired forward with a new tag, never deleted)

## Out of scope

- No production deploys, no database migrations, no cloud provisioning.
- No package publishing until a registry target exists; the workflow stops at tag creation.
- No tag deletion or force-push, ever.

## References

- GitHub rulesets (tag protection): <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets>
