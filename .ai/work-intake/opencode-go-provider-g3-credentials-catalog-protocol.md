---
title: OCG-G3: credentials, catalog discovery and measured protocol split
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-go-provider-g3-credentials-catalog-protocol
owner: unassigned
execution_type: AFK
---

# OCG-G3: credentials, catalog discovery and measured protocol split

## Traceability

- BRD: `docs/01-brd.md`
- PRD: `docs/specifications/ACTIVE/opencode-go-provider.md`
- Parent: `.ai/work-intake/opencode-go-provider.md`
- Version impact: additive routed-provider feature

## What to build

Credential handling plus the catalog and protocol facts the adapter needs.

**Credentials.** Keychain item `reverso/OPENCODE_API_KEY`, one entry in
`_KEYCHAIN_KEYS`, read once at proxy startup and injected into the environment,
matching the existing `DEEPSEEK_API_KEY` pattern. A pre-set `OPENCODE_API_KEY`
short-circuits the Keychain read so tests and CI inject without it. `OCGO_API_KEY`
is accepted as a **read-only alias resolved second**, never written. The key is
scrubbed from spawned CLI environments (`LAUNCHER_SCRUB_ENV_KEYS` precedent) since
no child process needs it. The key never appears in a launchd plist: those are
rendered from templates and diffed by `deployment_drift.py`.

**Catalog.** `GET /zen/go/v1/models` (public, no credential) with a bounded
fallback, enriched from `models.dev/api.json` for context windows.

**Measured protocol split.** For each catalog id, send a 1-token request to both
`/messages` and `/chat/completions` and record which accepts it. This replaces
`ocgo`'s hand-maintained 16-entry table, which has no knowledge of the 13 newer ids.

**Bare-exposure artifact.** Emit the computed bare-exposure set as a committed
artifact with a fail-closed `--check`.

## Acceptance criteria

- [ ] Absent key returns 503 and never a partial or silent success.
- [ ] `OPENCODE_API_KEY` wins over `OCGO_API_KEY`; the alias is read-only.
- [ ] The key is absent from the environment of every spawned CLI.
- [ ] Catalog discovery works unauthenticated and degrades to the bounded fallback offline.
- [ ] The protocol table is generated from measurement, with the observed accept/reject per id recorded as evidence.
- [ ] `--check` fails when the bare-exposure artifact does not match a freshly computed set, and is proven falsifiable by reintroducing a collision.

## Blocked by

OCG-G1
