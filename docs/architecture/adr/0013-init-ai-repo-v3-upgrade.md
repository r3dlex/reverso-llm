---
type: adr
project: reverso
id: 0013
title: init-ai-repo v3 governance upgrade
status: Accepted
date: 2026-06-28
related:
  - docs/architecture/adr/0001-init.md
  - docs/architecture/adr/0014-agents-md-single-source-codex-parity.md
  - docs/architecture/adr/0015-mcp-a2a-and-observability-harness.md
---

# ADR 0013: init-ai-repo v3 governance upgrade

## Status

Accepted.

## Context

`reverso` adopted the v3 AI-SDLC scaffold (ADR-0001) with a minimal `.ai/` layer
(rules, skills, system-prompts, drift) and a `.memory/`/`docs/` tree. The current
init-ai-repo v3 standard adds governance layers that were missing: workflow
manifests, a traceability graph, an eval-coverage scaffold, an MCP/A2A surface, a
provider-neutral model-routing policy, observability conventions, an
AI-failure-mode review checklist, command surfaces, and phased status files.

## Decision

Additively generate the missing v3 governance layers, adapted to a standalone
python ASGI/asyncio gateway (`topology_type: standalone`, depth 0). Refresh
`AGENTS.md` as the single source of truth (with a Harness Map and workflow links)
and make `CLAUDE.md` and `GEMINI.md` thin pointers to `AGENTS.md` (ADR-0014).

Skills-catalog-specific artifacts are intentionally excluded - this repo is not a
skills catalog. Cascade is a no-op for standalone topology. No example evalsets
are shipped; the eval-coverage gate remains offline-structural. The repo CI gate
(`uvx prek run --all-files`, `scripts/validate-rules.sh`, `scripts/archgate.sh`,
`uv run pytest`) is unchanged.

## Consequences

- The repo now exposes the full v3 surface map: `Instructions`, `Knowledge`,
  `Memory`, `Examples`, `Tools`, and `Guardrails`.
- Existing governance content (`.ai/rules/`, `.ai/skills/`, `.ai/system-prompts/`,
  `.ai/drift/`, `.memory/`, prior ADRs 0001 through 0012) is preserved unchanged.
- The upgrade is documentation/governance only: no application source code,
  package version, or runtime behavior changed.

## Verification

- `python3 -m json.tool` parses every generated JSON artifact.
- `uvx prek run --all-files` passes (file hygiene plus ruff).
- `uv run pytest tests/ --ignore=tests/integration` passes.
- Traceability validation reports zero dangling edges and zero dangling backlinks.
