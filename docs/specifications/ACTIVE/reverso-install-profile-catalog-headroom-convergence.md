---
type: specification
project: reverso
title: Reverso install, profile, catalog, and Headroom convergence
status: active
slug: reverso-install-profile-catalog-headroom-convergence
date: 2026-07-29
---

# Reverso install, profile, catalog, and Headroom convergence

## State A

Reverso has safe lower-level Codex and Claude Code sync commands, embedded
Headroom compression, process-local Headroom counters, and a shipped `coding`
default. Installation guidance and client setup remain distributed, model
catalog refresh is manual, RTK discovery depends on an external symlink that is
not part of the Reverso convergence contract, and embedded metrics expose less
operational detail than the standalone Headroom dashboard.

## State B

A clean install or update has one documented and idempotent convergence path
for the gateway, Codex profiles, Claude Code launchers, model catalogs, RTK
discovery, and a twice-daily catalog refresh. Embedded Headroom remains
in-process and stateless while Reverso exposes useful dashboard-style,
prompt-free aggregates for the current gateway process.

## Goals

1. Make the Reverso agent guide and operator documentation sufficient to
   install, update, configure, diagnose, and verify Reverso on this host.
2. Provide one manifest-driven client convergence workflow for every supported
   Codex profile and Claude Code launcher without overwriting user-owned files.
3. Schedule at least two Reverso-managed model catalog refresh attempts per
   local calendar day through a bounded, short-lived, single-flight
   LaunchAgent.
4. Configure and verify RTK as a host-side optimization prerequisite,
   including the Headroom discovery symlink, without invoking RTK from
   embedded Headroom.
5. Expand embedded Headroom usage telemetry toward standalone dashboard
   usefulness while preserving the no-prompt, no-subprocess, no-persistence,
   loopback-only contract.
6. Preserve `coding` as the embedded Headroom default across source,
   installation, documentation, and generated runtime configuration.

## Selected product contract

### Unified client convergence

Add one top-level client convergence command that composes the existing Codex
and Claude Code sync implementations instead of duplicating them. It reads one
repository-owned supported-surface manifest, plans all changes, validates the
complete candidate set, and then applies each marker-owned provider group
atomically.

The existing `reverso-codex-sync` and `reverso-claude-code-sync` commands remain
supported as lower-level commands. Their write-capable paths and the unified
`apply` and `refresh` modes all use one shared coordination implementation and
one advisory lock. The new console script and public syntax are frozen as:

```text
reverso-client-sync = "reverso.client_sync:main"

reverso-client-sync {dry-run,apply,refresh,verify}
  [--codex-config PATH]
  [--claude-config-dir PATH]
  [--catalog-dir PATH]
  [--launch-agent-dir PATH]
  [--rtk-bin PATH]
  [--json]
```

No implicit mode is permitted. Path options exist for isolated-home tests and
operator overrides; omitted options resolve through the existing lower-level
sync defaults. `--json` writes one JSON object to stdout and sends human
diagnostics to stderr.

The JSON result has exactly these top-level fields: `schema_version`,
`command`, `mode`, `status`, `exit_code`, `started_at`, `finished_at`,
`groups`, `surfaces`, `paths`, `catalog_refresh`, and `errors`. Timestamps are
RFC3339 UTC or null. `groups`, `surfaces`, `paths`, and `errors` are arrays
sorted by stable identifier or path. Each group record contains `id`, `kind`,
`status`, `dependencies`, and `paths`. Each surface record contains `id`,
`kind`, `status`, and `paths`. Each path record contains `path`, `group`,
`owner`, `status`, `before_sha256`, and `after_sha256`. Each error contains a
bounded `code`, `group`, `path`, and operator-safe `message`.

`catalog_refresh` has exactly `last_attempt_at`, `last_success_at`,
`stored_stale`, `stored_stale_observed_at`, `stale`, and `observed_at`.
Timestamps are RFC3339 UTC or null. `stored_stale` and
`stored_stale_observed_at` reproduce the last persisted observation, while
`stale` is recomputed against `observed_at` for every command result. The
object uses null for unavailable persisted state.

Top-level `status` is one of `success`, `no_op`, `planned`, `lock_skipped`,
`lock_busy`, `drift`, `stale`, `invalid`, `ownership_conflict`,
`partial_freshness`, or `repair_required`. Group and surface status is one of
`current`, `planned`, `changed`, `preserved`, `blocked_stale_dependency`,
`invalid`, `drift`, or `rolled_back`. Path status is one of `unchanged`,
`planned_create`, `planned_update`, `created`, `updated`, `preserved_conflict`,
`blocked_stale_dependency`, `drift`, or `rolled_back`.

The exit-code contract is:

| Code | Meaning |
|---|---|
| 0 | success, no-op, dry-run plan, or benign lock skip |
| 2 | verify detected drift or refresh staleness, or an operator write timed out waiting for the shared lock |
| 3 | invalid candidate or ownership conflict; no writes |
| 4 | partial provider freshness |
| 5 | rollback or internal inconsistency requires repair |

The unified command reports a per-surface result for:

- Codex Reverso routes: Claude, Copilot, Auggie, DeepSeek, Kimi, and Ollama.
- Feature-gated Codex routes: Codex Direct and OpenAI pass-through.
- Claude Code launchers: aggregate Reverso, Claude, Codex, Copilot, Auggie,
  DeepSeek, Kimi, and Ollama.
- Direct user-preserving Codex profiles: built-in OpenAI and MiniMax.
- External additive selector catalogs: AGY, when an existing configured source
  is present.

The exact selector and ownership rows are:

| Surface | Selector form | Ownership |
|---|---|---|
| Built-in Codex GPT | bare `gpt-*` | Codex-owned and user-preserving |
| MiniMax | bare `MiniMax-*` | direct Codex profile and user-preserving |
| Claude through Claude Code | bare provider model id | Reverso-managed client presentation |
| DeepSeek through Reverso | bare provider model id | Reverso-managed client presentation |
| Kimi through Reverso | bare `kimi-k3` | Reverso-managed client presentation |
| Ollama through Reverso | bare raw Ollama id | Reverso-managed client presentation |
| Copilot through Reverso | `copilot/<model>` | Reverso-managed client presentation |
| Auggie through Reverso | `auggie/<model>` | Reverso-managed client presentation |
| AGY additive catalog | `agy/<model>` | external source and user-preserving unless already marker-owned by its exact sync owner |
| Codex Direct | `codex-direct/<model>` | feature-gated Reverso client presentation |
| OpenAI pass-through | `openai-pass-through/<model>` | feature-gated Reverso client presentation |

### Ollama G1 convergence contract

The manifest owns one `provider-ollama` group and one
`codex-reverso-ollama` surface. Both `shared-codex-config` and
`shared-codex-cleanup` depend on `provider-ollama`; no Claude launcher or
shared Claude dependency includes it in G1. The provider group owns
`~/.codex/reverso-ollama.config.toml` and
`~/.codex/reverso/ollama.json` under the existing exact
`reverso-codex-sync` marker policy. The isolated selector and upstream model
id are the same raw `/api/tags` id. `ollama/<id>` is never generated.

Every validated `/api/tags` row is current local inventory, including an id
whose bytes happen to end in `:cloud` or `-cloud`. A suffix is not Cloud
authority. No supported machine-readable Ollama Cloud eligibility authority is
established in G1, so Cloud publication is blocked. When Cloud is requested its
state is `unavailable`; when explicitly disabled its state is `disabled`. G1
does not publish current Cloud ids and therefore cannot produce partial or
stale Cloud entries. The future refresh contract may report `partial_freshness`
or retain marker-owned Cloud rows as stale only after a supported authority is
specified and implemented; it must never infer, scrape, or ship a static list.

Because `/v1/models` is populated from `/api/tags` and carries no
model-specific capability or context authority, generated Ollama Codex catalog
rows deliberately advertise only `input_modalities = ["text"]`,
`supports_parallel_tool_calls = false`, and a 2048-token context bound. Runtime
Responses routing still passes explicitly invoked image and function-tool
payloads through unchanged; the conservative picker metadata must not be read
as a runtime rejection rule.

The Ollama provider group is prepared and validated before its first write.
Profile and catalog commit atomically as one handled-failure group. An unmarked
conflict preserves bytes and blocks the group. A handled failure restores prior
bytes, absence, object type, mode, and symlink target under the common rollback
contract. Restore and uninstall remove only exact marker-owned Ollama artifacts
and preserve unmarked paths; interrupted work is recovered by verify followed
by an idempotent rerun.

Headroom records provider `ollama` and inbound surface `responses` in G1; the
outbound protocol dimension is `ollama_responses`. Compression runs once before
adapter dispatch and does not rewrite function-tool or image structure. G1
routes only `/ollama/v1` through the Responses registry. Ollama is absent from
the Anthropic registry, and no Ollama alias is valid on `/v1/messages`. G2 may
add catalog-scoped exact Anthropic alias authority and
`anthropic_messages`/`ollama_messages` dimensions without changing the G1 raw
Codex selector authority.

### Ollama G3 refresh and cross-client atomicity contract

The complete `provider-ollama` mutation group owns the shared marker-owned
inventory snapshot, isolated Codex profile and catalog, and Claude launcher. A
single gateway discovery feeds one candidate for both clients. Validation
precedes every write and handled failure restores all paths in the group.
Unrelated provider groups and user-owned files retain their exact hashes.

Background refresh is prompt-free. Current local rows replace prior local rows.
An `auth_required`, timeout, or malformed Cloud result retains only Cloud rows
from the exact prior marker-owned snapshot, marks them stale, and reports
partial freshness. The rows are not current routing eligibility. An unmarked or
invalid snapshot is never retention authority. Cloud opt-out through
`REVERSO_OLLAMA_CLOUD=0` or `OLLAMA_NO_CLOUD=1` performs no Cloud probe or
sign-in and produces current local-only state.

Scoped Claude publication reads the strict marker-owned snapshot. Responses
`/ollama/v1/models` stays live, so refresh does not reread its own publication.
Total Ollama discovery failure preserves the complete existing group and
reports partial freshness. A valid empty HTTP listing remains a current empty
inventory.

No background path invokes `ollama signin`, sources shell profiles, reads device
identity or API keys, manages the Ollama daemon, or pulls models. Verify exposes
snapshot freshness and ownership drift without writing. `restore` is an
explicit idempotent desired-state repair. `uninstall-ollama` removes the four
exactly owned paths as one group; any unmarked, conflicting, or unrelated path
is preserved and reported without a partial uninstall.

### Ollama G2 Claude Messages contract

The manifest adds the marker-owned `claude-ollama` launcher to
`provider-ollama`, and `shared-reverso-launcher` depends on that provider. The
launcher uses the existing loopback base URL and placeholder token, enables
gateway discovery, and sends exactly `x-reverso-model-catalog: ollama` with the
workspace header.

The scoped model listing presents only opaque
`anthropic-ollama-<raw-model-id>` aliases generated from the runtime catalog.
Each complete alias is bound to its exact raw id in request-local Ollama catalog
authority. Bare raw ids and `ollama/<id>` never route on Anthropic. Alias lookup
is exact and case-sensitive; duplicate or casefold-colliding generations publish
no conflicting Ollama rows. The bound raw bytes replace the alias before
Headroom and native `/v1/messages` dispatch.

One composition-owned runtime and adapter serve Responses and Messages. The
Anthropic path prepares and translates once, compresses once, atomically
projects only source-addressed text leaves back into the prepared native
payload, then uses the optional native Messages facet. Projection ambiguity
fails open to the whole prepared payload. Non-native providers retain their
existing Responses translation path.

The manifest is the authority for profile names, route prefixes, catalog
ownership, default models, selector naming, and client support. Existing
provider routing registries remain the authority for runtime routing and model
eligibility. AGY is not a Reverso runtime route and must never be fetched from
the Reverso gateway. The convergence command must fail on disagreement rather
than create a second routing authority.

All ownership and syntax checks happen before the first write. Every writable
path belongs to exactly one manifest group. Provider-specific catalogs and
profiles form provider groups. Any aggregate launcher or other artifact that
depends on more than one provider belongs to a separate shared dependency
group and is never duplicated across provider groups.

Each group is staged and validated as a whole. On a caught replacement
failure, compensating rollback restores the prior bytes, existence or absence,
object type, symlink target, and executable mode of every already-replaced
member. This is handled-failure group atomicity, not crash atomicity. A process
interruption may leave detectable drift; verify reports that drift and a
subsequent idempotent rerun repairs it. Independent valid groups may advance
when another provider is stale, but the command reports partial freshness and
exits nonzero.

A shared dependency group commits only when every provider dependency declared
by that group is current in the same run. If any dependency is stale or
invalid, the command preserves the complete previous shared artifact, reports
`blocked_stale_dependency`, returns exit code 4, and never renders a shared
artifact from a mixture of new candidates and prior provider state.

All write-capable entrypoints coordinate through
`reverso.client_sync_lock.acquire_client_sync_lock` and the exact lock path
`~/Library/Application Support/reverso/catalog-refresh.lock`. A scheduled
`refresh` uses non-blocking acquisition and reports `lock_skipped` with exit
code 0 on contention. Unified `apply` and directly invoked lower-level write
commands wait at most 30 seconds, then report `lock_busy`, write nothing, and
exit with code 2. The unified command calls lower-level library functions with
an explicit already-held lock token; nested calls never reacquire or release
the lock. Read-only dry-run and verify operations never acquire it.

### RTK prerequisite

RTK remains a host-side command and output optimization used by supported
client hooks. It is not part of the embedded Headroom request path.

The convergence workflow accepts an explicit RTK path or discovers RTK from
the exact host `PATH` supplied to the convergence command. An explicit path
wins. Without it, discovery must resolve to exactly one distinct regular
executable; multiple distinct candidates fail closed and require an explicit
selection.

The convergence workflow verifies:

1. an executable RTK binary is resolvable;
2. `~/.headroom/bin` is a real directory;
3. `~/.headroom/bin/rtk` resolves to the selected RTK executable;
4. Claude Code's RTK hook remains enabled when the managed environment
   requires it;
5. Codex and Claude launcher smoke checks run with the same host PATH used by
   the LaunchAgents.

If `~/.headroom` or `~/.headroom/bin` is absent, the workflow may create the
missing real directory with owner-only permissions only when every existing
ancestor from the real home directory is a real directory. It never traverses
or replaces a symlinked parent. If `~/.headroom/bin/rtk` is absent, the
workflow may create the symlink. If the path is a regular file, directory, or
points to another target, the workflow fails closed with repair guidance and
preserves the existing object.

### Scheduled catalog refresh

Install a third LaunchAgent named `com.user.reverso-catalog-refresh`. It is a
short-lived scheduled job, not a long-lived Reverso service.

Its filesystem contract is:

| Purpose | Path |
|---|---|
| LaunchAgent | `~/Library/LaunchAgents/com.user.reverso-catalog-refresh.plist` |
| Lock | `~/Library/Application Support/reverso/catalog-refresh.lock` |
| Status | `~/Library/Application Support/reverso/catalog-refresh-status.json` |
| Stdout | `~/Library/Logs/reverso/catalog-refresh.stdout.log` |
| Stderr | `~/Library/Logs/reverso/catalog-refresh.stderr.log` |

The state and log directories are mode `0700`; created lock, status, and log
files are mode `0600`. Each provider discovery has a 10-second network timeout
and the complete refresh has a 120-second execution bound. The command rotates
each log before writing when it would exceed 1 MiB and retains three rotations.

The job:

- has no `KeepAlive`;
- runs at 06:00 and 18:00 local time through two
  `StartCalendarInterval` entries;
- may also run once at installation to prove the configuration;
- acquires the shared non-blocking writer lock before discovery or writes;
- calls the unified convergence command in a catalog and generated-profile
  refresh mode;
- validates every fetched model listing and every rendered artifact before
  replacement;
- uses atomic writes and skips byte-identical files;
- never edits unmarked files;
- never restarts the proxy or daemon;
- records a prompt-free bounded status summary;
- enforces the exact log rotation contract above;
- exits successfully when another refresh already owns the lock;
- isolates one failed provider discovery without corrupting previously valid
  catalogs;
- returns nonzero when the full refresh is not current so monitoring can
  distinguish stale from healthy.

The existing proxy and daemon remain the only long-lived Reverso processes.
The schedule guarantees two configured attempts, not two successful
completions. Verification reports the last attempt, last success, and stale
state so a missed or failed run is visible without adding a catch-up daemon.

The status file has exactly these fields: `schema_version`, `status`,
`last_attempt_at`, `last_success_at`, `duration_ms`, `exit_code`, `stale`,
`stale_observed_at`, `provider_results`, and `error_codes`. `schema_version` is
1. Its status is one of `never_run`, `success`, `lock_skipped`,
`partial_freshness`, or `failed`.
Timestamps are RFC3339 UTC or null, `duration_ms` is a non-negative integer or
null, `provider_results` uses only manifest provider ids with values
`current`, `changed`, `stale`, `invalid`, or `skipped`, and `error_codes` is a
sorted list of at most 16 governed codes. No raw response or exception text is
stored.

Persisted `stale` is an as-of snapshot computed when the status file is
written; `stale_observed_at` records that RFC3339 UTC observation time.
`verify` and every unified JSON result recompute current `stale` from
`last_success_at` and their own `observed_at`, without rewriting the status
file. Staleness is true when `last_success_at` is null or more than 14 hours
before the observation time. A lock skip does not update `last_success_at`.
Uninstall removes the plist and bootstrapped job but preserves status and logs
by default;
`scripts/uninstall-launchagents.sh --purge-state` removes the lock, status,
logs, and rotations. Deployment drift classifies this plist as a scheduled job
and does not add it to the long-lived executable map.

### Embedded Headroom metrics

`GET /usage/headroom` keeps its outer `schema_version: 1` and `provider:
"headroom"` envelope. `GET /usage` continues to embed the exact same Headroom
snapshot object under `headroom`. The inner snapshot becomes the additive
schema authority with `schema_version: 2`.

The snapshot preserves the existing fields `enabled`, `profile`,
`requests_seen`, `requests_compressed`, `tokens_before`, `tokens_after`,
`tokens_saved`, `compression_ratio`, `fail_open_count`, `failure_reasons`,
`error_types`, and `updated_at`. It adds `process_started_at`,
`measurement_started_at`, `requests_passed_through`,
`compression_success_rate`, `average_tokens_saved`,
`outcome_counts`, `provider_counts`, `surface_counts`, `timeout_seconds`,
`model_limit`, `last_success_at`, `last_failure_at`, and `reset_reason`.

All counters are non-negative integers. Ratios are finite numbers between zero
and one. `compression_ratio` is `tokens_saved / tokens_before`, or zero when
`tokens_before` is zero. `compression_success_rate` is
`requests_compressed / requests_seen`, or zero when `requests_seen` is zero.
`average_tokens_saved` is `tokens_saved / requests_compressed`, or zero when
`requests_compressed` is zero. `requests_passed_through` is
`requests_seen - requests_compressed - fail_open_count`, clamped at zero.
Timestamp fields are RFC3339 UTC or null. `reset_reason` is
`process_start` for a new metrics object and `manual_test_reset` only for an
explicit test reset.

`outcome_counts` has exactly `compressed`, `passed_through`, `fail_open`, and
`other`. `failure_reasons` has exactly `worker_busy`, `timeout`, `exception`,
`inflation_guard`, `retrieval_marker`, `unsafe_output`, and `other`.
`error_types` has exactly `timeout`, `worker_busy`,
`dependency_exception`, `inflation_guard`, `retrieval_marker`,
`unsafe_output`, and `other`. `provider_counts` has exactly `claude`,
`copilot`, `auggie`, `deepseek`, `kimi`, `codex-direct`,
`ollama`, `openai-pass-through`, and `other`. `surface_counts` has exactly `responses`,
`anthropic_messages`, and `other`. Every governed key is present with zero
when unused; unknown input is accumulated only in `other`.

Provider and surface attribution is supplied explicitly at the request
dispatch boundary. `ResponsesGatewayApp` maps its selected governed provider
route to the provider enum and passes `surface="responses"`;
`AnthropicMessagesApp` maps its selected governed backend to the same provider
enum and passes `surface="anthropic_messages"`. The compression recorder
accepts these governed provider and surface values as explicit inputs and
never infers either dimension from a model name, prompt, error, or response.
Unknown dispatch values are mapped to `other` before the recorder is called.

No request body, prompt text, response text, tool content, workspace path,
session id, request id, model prompt fragment, or unbounded error string may
appear. The route remains subprocess-free and reads one in-memory snapshot.

Metrics reset on gateway restart. This scope does not read, merge, or claim
parity with standalone `~/.headroom/proxy_savings.json`. Lifetime aggregates
require a separate ADR that explicitly narrows the BRD no-persistence rule.

### `coding` default

The shipped `coding` profile remains the embedded default when
`REVERSO_HEADROOM_PROFILE` is absent or blank. Installation templates and
documentation either omit the variable and rely on that default or set it
explicitly to `coding`.

The future implementation must distinguish an explicit alternative profile
override from a default. Tests may use `agent-90` only to prove override
behavior. No generated default, installer example, or active operator
instruction may select `agent-90`.

Kimi retains the compaction behavior already governed by
`docs/specifications/ACTIVE/kimi-subscription-provider.md`. This initiative
must not replace it with the rejected 40 percent threshold.

### Port-aware verification

Every operational check names its target:

- Reverso: `http://127.0.0.1:64946`.
- Local standalone Headroom, when independently installed: operator-assigned
  ports such as `58787`.
- Upstream Headroom's default `8787`: never treated as Reverso health.

The install and agent guides must not use an unqualified `headroom doctor`
result as proof that embedded compression is healthy.

## Non-goals

- No product implementation in this Northstar run.
- No new Reverso listener or dashboard server.
- No third long-lived Reverso process.
- No persistent embedded telemetry database or history file.
- No reading or merging of standalone Headroom savings files.
- No invocation of RTK from embedded Headroom or usage routes.
- No overwrite of unmarked Codex, Claude, RTK, or Headroom files.
- No change to the frozen `ProviderAdapter` protocol.
- No modification of `src/reverso/protocols/adapter.py`.
- No provider routing changes.
- No Kimi quota telemetry or 40 percent compaction threshold.
- No change to direct OpenAI or MiniMax ownership.

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S1 | Lock convergence contracts, regression tests, and the current installation baseline | AFK | ready-for-agent | none |
| S2 | Introduce the supported-surface manifest, unified client command, RTK convergence, and command documentation | AFK | ready-for-agent | S1 |
| S3 | Install a twice-daily short-lived catalog refresh LaunchAgent | AFK | ready-for-agent | S2 |
| S4 | Expand process-local embedded Headroom aggregates and publish the usage schema | AFK | ready-for-agent | S1 |
| S5 | Publish and prove the canonical end-to-end install, refresh, profile, catalog, and usage runbook | AFK | ready-for-agent | S2, S3, S4 |

## Acceptance criteria

1. `AGENTS.md` and the operator documentation contain one consistent clean
   install, update, configuration, diagnosis, and verification path.
2. The documented RTK prerequisite includes executable discovery and the
   `~/.headroom/bin/rtk` symlink, preserves conflicts, and explains that RTK is
   not called by embedded Headroom.
3. The unified dry run reports the complete Codex and Claude Code change set
   without writing any file.
4. The `reverso-client-sync` script, arguments, JSON schema, statuses, and exit
   codes match the frozen public contract.
5. The unified apply assigns every writable path to one group, provides
   handled-failure group atomicity, detects interruption drift, repairs it on
   rerun, is idempotent and marker-scoped, and preserves all unmanaged content
   and direct profiles.
6. Unified apply, scheduled refresh, and both directly invoked lower-level
   writers use one advisory lock and one coordination implementation; operator
   contention times out without writes and scheduled contention skips safely.
7. A shared group never commits from mixed fresh and stale dependencies; it
   preserves prior bytes and reports `blocked_stale_dependency`.
8. The supported-surface matrix is generated or validated from one manifest
   and fails on drift from runtime routing authorities.
9. The scheduled agent is non-KeepAlive, has attempts scheduled at 06:00 and
   18:00 local time, is single-flight, keeps bounded logs, skips unchanged
   artifacts, reports last attempt and success, and cannot restart or block
   either long-lived service.
10. The scheduled paths, permissions, 10-second provider timeout, 120-second
   execution bound, 14-hour stale formula, status schema, 1 MiB by three log
   rotation, uninstall preservation, explicit purge, and scheduled-job drift
   classification match the frozen contract.
11. Persisted stale state is explicitly an as-of snapshot and verify recomputes
    current staleness across the 14-hour boundary without requiring another
    scheduled run.
12. A failed or malformed provider listing cannot replace a previously valid
   catalog or partially update its matching profile.
13. `GET /usage` and `GET /usage/headroom` return schema-valid, prompt-free,
   bounded-cardinality aggregates from one in-process snapshot and invoke no
   subprocess.
14. The additive Headroom inner schema preserves every existing field and
    exactly implements version 2 fields, enums, formulas, zero behavior, and
    timestamp semantics.
15. Provider and surface counts come from explicit governed dispatch metadata,
    never raw model inference.
16. Embedded metrics expose enough counters and timestamps to answer how often
   compression ran, how many tokens it saved, how often it failed open, which
   governed surfaces were involved, and which configuration was active during
   the current process lifetime.
17. Restart resets embedded metrics and the documentation states that reset is
    expected.
18. `coding` is the default in source behavior, generated configuration,
    installer evidence, and documentation. An explicit alternative override
    remains supported.
19. Reverso checks always target `64946`; standalone Headroom ports are
    documented separately and never treated as embedded health.
20. Targeted unit and integration tests, full pytest, Ruff, Prek, compileall,
    architecture checks, secret scans, debug-marker scans, and documentation
    contract tests pass before merge.
21. Each slice is delivered as one reviewable PR with resolved review findings
    and green hosted checks.

## Traceability

- Context:
  `.omx/context/reverso-install-profile-catalog-headroom-convergence-20260729T214459Z.md`
- Work item:
  `.ai/work-intake/reverso-install-profile-catalog-headroom-convergence.md`
- PRD:
  `.omx/plans/prd-reverso-install-profile-catalog-headroom-convergence.md`
- Test specification:
  `.omx/plans/test-spec-reverso-install-profile-catalog-headroom-convergence.md`
- Issue node:
  `issue:reverso-root:reverso-install-profile-catalog-headroom-convergence`
