---
title: Northstar Kimi subscription provider handoff
status: ready-for-human
slug: kimi-subscription-provider
date: 2026-07-19
---

# Northstar A-to-B Handoff: kimi-subscription-provider

- Spec: `docs/specifications/ACTIVE/kimi-subscription-provider.md`
- Sliced goals: `.omx/plans/ralplan-kimi-subscription-provider.md`.
- PRD: `.omx/plans/prd-kimi-subscription-provider.md`.
- Test spec: `.omx/plans/test-spec-kimi-subscription-provider.md`.
- Consensus evidence:
  `.omx/state/ralplan/kimi-subscription-provider-handoff.json`.
- Issue: local-first markdown under `.ai/work-intake/` (reconcile before merge).
- Manifest record: `optional_branches[id=northstar-handoff-kimi-subscription-provider]` in `.ai/workflows/repo-workflow.json`.
- Traceability: `plan:*:northstar-kimi-subscription-provider` and `handoff:*:northstar-kimi-subscription-provider` (schema_version 1.1).

Autobahn consumes this handoff to ship each sliced goal one PR at a time. The
credentialed live-proof slice is a fail-closed merge gate for all selectable
Kimi provider changes.

## Delivery status

All six implementation slices have local evidence. The credentialed gate and
the release install, smoke, and rollback proof are green. The stacked pull
requests remain draft and unmerged. Release closure is draft PR #86, pending
final independent review, hosted CI on its exact head, and host-policy merge
authority.
