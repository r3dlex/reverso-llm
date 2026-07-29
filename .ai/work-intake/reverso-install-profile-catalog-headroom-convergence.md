---
title: Work item for Reverso install, profile, catalog, and Headroom convergence
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: reverso-install-profile-catalog-headroom-convergence
owner: unassigned
---

# Work Item: Converge Reverso install, profiles, catalogs, RTK, and Headroom usage

- **Traceability node:**
  `issue:reverso-root:reverso-install-profile-catalog-headroom-convergence`
- **Spec:**
  [`docs/specifications/ACTIVE/reverso-install-profile-catalog-headroom-convergence.md`](../../docs/specifications/ACTIVE/reverso-install-profile-catalog-headroom-convergence.md)
- **State:** `ready-for-agent`
- **Category:** `enhancement`
- **Owner:** unassigned
- **Hosted reconciliation:** local-first; no hosted issue was authorized

## Summary

Create one safe client convergence path for Reverso installation, Codex
profiles, Claude Code launchers, model catalogs, RTK discovery, and scheduled
catalog refresh. Expand the current process-local embedded Headroom metrics
toward dashboard usefulness without adding listeners, subprocess calls, or
persistence. Preserve the shipped `coding` embedded profile default.

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S1 | Lock convergence contracts, regression tests, and the current installation baseline | AFK | ready-for-agent | none |
| S2 | Introduce the supported-surface manifest, unified client command, RTK convergence, and command documentation | AFK | ready-for-agent | S1 |
| S3 | Install a twice-daily short-lived catalog refresh LaunchAgent | AFK | ready-for-agent | S2 |
| S4 | Expand process-local embedded Headroom aggregates and publish the usage schema | AFK | ready-for-agent | S1 |
| S5 | Publish and prove the canonical end-to-end install, refresh, profile, catalog, and usage runbook | AFK | ready-for-agent | S2, S3, S4 |

## Slice briefs

### S1: Lock contracts and current installation baseline

**What to build**

Add contract fixtures and regression tests that lock the current installation
baseline plus the approved CLI, JSON, ownership, rollback, RTK, schedule,
Headroom, port, and `coding` behavior. Do not document future commands before
they exist.

**Acceptance criteria**

- [ ] Existing install and lower-level Codex and Claude sync behavior is
      captured before implementation.
- [ ] The frozen `reverso-client-sync` syntax, statuses, fields, and exit codes
      are expressed as executable contract fixtures.
- [ ] Ownership, rollback, shared-dependency, schedule, and telemetry schemas
      are expressed as executable contract fixtures.
- [ ] Embedded Headroom never invokes RTK.
- [ ] Reverso health targets `64946`, not standalone ports or `8787`.
- [ ] `coding` is the only active default while explicit overrides remain
      supported.

### S2: Unify supported profile and catalog convergence

**What to build**

Add one repository-owned supported-surface manifest and one orchestration
command that composes the existing Codex and Claude sync logic. It must plan,
validate, and apply all marker-owned profiles, launchers, and catalogs without
creating a second provider-routing authority. It also owns safe RTK discovery
and the final documentation for the implemented command.

**Acceptance criteria**

- [ ] One dry run reports all Codex and Claude Code managed changes.
- [ ] The entrypoint is `reverso-client-sync = "reverso.client_sync:main"` and
      implements the exact dry-run, apply, refresh, verify, path-option, JSON,
      status, and exit-code contract in the specification.
- [ ] One apply converges all supported surfaces and is idempotent.
- [ ] Runtime routing and model exposure remain authoritative and are checked
      for drift.
- [ ] Direct OpenAI and MiniMax remain user-preserving.
- [ ] Selector rows preserve bare built-ins and use `copilot/<model>`,
      `auggie/<model>`, and `agy/<model>` exactly; AGY remains an external,
      non-Reverso route.
- [ ] Unmarked conflicts fail closed without modification.
- [ ] Each provider catalog plus dependent profile or launcher is committed as
      one handled-failure rollback group; each writable path has exactly one
      group owner, and stale independent groups produce exit code 4.
- [ ] Unified apply and directly invoked lower-level writers use
      `reverso.client_sync_lock.acquire_client_sync_lock` with the shared
      catalog-refresh lock, wait at most 30 seconds, and write nothing on
      timeout; nested library calls reuse the held lock token.
- [ ] A shared dependency group commits only when every declared provider
      dependency is current; otherwise prior bytes are preserved and the group
      reports `blocked_stale_dependency`.
- [ ] Verify detects interruption drift and an idempotent rerun repairs it.
- [ ] Existing lower-level sync commands remain supported.
- [ ] `~/.headroom/bin/rtk` is verified or safely created only when absent;
      parent symlinks and conflicting targets are preserved and fail closed.
- [ ] Command documentation is published with the working command.

### S3: Schedule two bounded refreshes each day

**What to build**

Install `com.user.reverso-catalog-refresh` as a short-lived LaunchAgent with
refresh attempts scheduled at 06:00 and 18:00 local time. It invokes the
unified refresh mode with single-flight locking, atomic provider-group writes,
bounded logs, and failure isolation.

**Acceptance criteria**

- [ ] The scheduled agent has no `KeepAlive`.
- [ ] Two `StartCalendarInterval` entries are installed and read back.
- [ ] Overlapping invocations produce one writer.
- [ ] Unchanged catalogs are not rewritten.
- [ ] Invalid discovery cannot replace a valid catalog or matching profile.
- [ ] The job never restarts or blocks the proxy or daemon.
- [ ] Install, uninstall, and deployment drift checks govern the new agent.
- [ ] Last attempt, last success, and stale status distinguish configured
      cadence from successful completion.
- [ ] Scheduled refresh shares the same writer lock as manual and lower-level
      sync, uses non-blocking acquisition, and skips safely on contention.
- [ ] Lock, status, stdout, and stderr paths; `0700` directories; `0600` files;
      10-second provider timeout; 120-second overall bound; 14-hour stale
      formula; 1 MiB logs with three rotations; and the status schema match the
      specification.
- [ ] Persisted stale state records its observation time, while verify
      recomputes current staleness without another refresh or status rewrite.
- [ ] Uninstall preserves status and logs by default;
      `scripts/uninstall-launchagents.sh --purge-state` removes them.
- [ ] Deployment drift classifies the job separately and still reports exactly
      two long-lived Reverso processes.

### S4: Expand embedded Headroom aggregate intelligence

**What to build**

Extend the existing in-memory Headroom metrics and usage schema with bounded
configuration, outcome, surface, success-rate, average-savings, and
measurement-window fields. Keep both usage routes on one prompt-free snapshot
and publish the exact version 2 inner schema with the implementation.

**Acceptance criteria**

- [ ] The schema answers frequency, savings, failure, surface, and active
      configuration questions for the current process lifetime.
- [ ] The existing inner fields remain present and the exact version 2 fields,
      enums, formulas, zero behavior, timestamps, and reset reasons match the
      specification.
- [ ] Cardinality is bounded by governed enums.
- [ ] Responses and Anthropic dispatch boundaries pass explicit governed
      provider and surface values; compression code never derives attribution
      from raw model strings.
- [ ] No prompt, response, tool, workspace, session, request, or unbounded
      error data is stored or returned.
- [ ] Usage reads do not spawn a subprocess or trigger compression.
- [ ] Restart reset is explicit and tested.
- [ ] No standalone Headroom file is read or merged.
- [ ] Recording, snapshot, and usage paths perform no persistence writes.
- [ ] No Kimi quota fields or obsolete 40 percent threshold are introduced.

### S5: Prove end-to-end convergence

**What to build**

Publish the complete canonical clean-install, update, convergence, scheduled
refresh, RTK, port, and usage runbook only after S2 through S4 exist. Add a safe
acceptance matrix that installs or renders into isolated temporary homes,
verifies the scheduled agent, performs client sync twice, checks every
supported profile and launcher, reads catalog freshness, and validates
Headroom usage on `64946`.

**Acceptance criteria**

- [ ] The matrix covers every supported Codex and Claude Code surface.
- [ ] A second run produces no unintended diff.
- [ ] RTK discovery is proven without running it from embedded Headroom.
- [ ] Scheduled refresh status and catalog freshness are machine-readable.
- [ ] `coding` is observed as the embedded default.
- [ ] Agent and operator instructions use one final command sequence and do not
      document nonexistent or pre-implementation behavior.
- [ ] A changed-file guard proves
      `src/reverso/protocols/adapter.py` remains untouched.
- [ ] Full local and hosted verification is green before merge.

## Version impact

Behavioral enhancement to installation, generated client configuration,
scheduled local maintenance, and the additive usage schema. No listener,
provider route, frozen adapter contract, or persistent data model changes.

## Out of scope

- Persistent embedded telemetry.
- A new dashboard server.
- A third long-lived Reverso process.
- Embedded invocation of RTK.
- Standalone Headroom data ingestion.
- Provider routing changes.
- Unmanaged file replacement.
- Kimi quota telemetry or obsolete compaction thresholds.
