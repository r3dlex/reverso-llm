---
title: Client convergence command
status: active
---

# Client convergence command

`reverso-client-sync` is the supported operator entrypoint for converging Codex
profiles and catalogs, Claude Code launchers, and the host RTK prerequisite.
It composes `reverso-codex-sync` and `reverso-claude-code-sync`; it does not
define runtime model routing.

## Syntax

```text
reverso-client-sync {dry-run,apply,restore,refresh,verify,uninstall-ollama}
  [--codex-config PATH]
  [--claude-config-dir PATH]
  [--catalog-dir PATH]
  [--launch-agent-dir PATH]
  [--rtk-bin PATH]
  [--json]
```

A mode is required.

- `dry-run` validates and reports the complete candidate without writing or
  acquiring the client sync lock.
- `apply` validates, waits up to 30 seconds for the shared lock, and applies
  valid marker-owned groups.
- `restore` is an idempotent desired-state repair alias for `apply`.
- `refresh` validates and applies only when the shared lock is immediately
  available. Contention is a benign `lock_skipped` result.
- `verify` validates and reports drift without writing or acquiring the lock.
- `uninstall-ollama` atomically removes only the four exactly owned Ollama
  client artifacts. Any ownership conflict fails closed without deleting them.

`--json` writes exactly one result object to stdout. Human diagnostics go to
stderr. The object contains `schema_version`, `command`, `mode`, `status`,
`exit_code`, `started_at`, `finished_at`, `groups`, `surfaces`, `paths`,
`catalog_refresh`, and `errors`.

Exit codes are:

| Code | Meaning |
|---|---|
| 0 | Success, no-op, dry-run plan, or scheduled lock skip |
| 2 | Verify drift, refresh staleness, or operator lock timeout |
| 3 | Invalid candidate or ownership conflict with no writes |
| 4 | Partial provider freshness |
| 5 | Rollback or internal inconsistency requires repair |

## Recommended workflow

1. Ensure Reverso is installed and the gateway is available at
   `http://127.0.0.1:64946`.
2. Ensure one executable `rtk` is on the exact host `PATH`, or pass
   `--rtk-bin PATH`.
3. Review `reverso-client-sync dry-run --json`.
4. Apply with `reverso-client-sync apply --json`.
5. Confirm `reverso-client-sync verify --json` returns exit code 0.

Re-run the same sequence after a Reverso update. Apply is idempotent and
marker-scoped. An unmanaged conflict is preserved and reported instead of
being overwritten.

## Shared lock and partial freshness

All write-capable client sync commands coordinate through:

```text
~/Library/Application Support/reverso/catalog-refresh.lock
```

An operator apply waits at most 30 seconds. A scheduled refresh never waits.
Dry-run and verify never acquire the lock.

If provider discovery is stale, independent prerequisites such as RTK may
advance, but provider and shared dependency groups are preserved. A shared
artifact is never rendered from a mixture of current and prior provider state.
The result is `partial_freshness` with exit code 4.

## Scheduled refresh

`scripts/install-launchagents.sh` installs the short-lived
`com.user.reverso-catalog-refresh` job separately from the proxy and daemon.
It attempts `refresh` at 06:00 and 18:00 local time, with a 10-second bound for
each provider discovery and a 120-second bound for the complete refresh. It
does not keep running and never restarts either long-lived service.
The scheduled runner acquires the shared lock and invokes the same in-process
`client_sync.run("refresh")` convergence path with the held lock capability. It
does not spawn or reimplement a second refresh pipeline.

The latest status is stored at:

```text
~/Library/Application Support/reverso/catalog-refresh-status.json
```

The scheduled stdout and stderr logs are under
`~/Library/Logs/reverso/`. Each rotates at 1 MiB and retains `.1`, `.2`, and
`.3`. A normal uninstall removes the scheduled job but preserves its lock,
status, and logs. Use `scripts/uninstall-launchagents.sh --purge-state` only
when those exact refresh artifacts should also be removed.

## Ollama cross-client convergence

The `provider-ollama` group owns the shared marker-owned inventory snapshot,
the isolated Codex profile and catalog, and the `claude-ollama` launcher. The
complete candidate is prepared before its first write. A handled failure
restores every path in the group, so Codex and Claude Code never observe
different Ollama inventories. Repeating `apply` is a byte-for-byte no-op.

The prompt-free shared snapshot is stored at:

```text
~/Library/Application Support/reverso/ollama-inventory.json
```

Current local ids replace prior local rows. If bounded Cloud discovery reports
`auth_required`, timeout, or invalid data, only Cloud rows from that exact
marker-owned snapshot are retained and marked stale. This is partial freshness,
not current Cloud eligibility. An unmarked or malformed snapshot is an
ownership conflict and is never retention authority. Background refresh never
runs `ollama signin`, reads `~/.ollama/id_ed25519`, sources a shell profile,
manages the Ollama daemon, or pulls a model.

Set `REVERSO_OLLAMA_CLOUD=0` or `OLLAMA_NO_CLOUD=1` for absolute Cloud opt-out.
Opt-out performs no Cloud discovery or sign-in and writes current local-only
state. Restore after an interrupted apply by running `verify`, reviewing the
drift, and running `restore`; group rollback preserves the prior bytes, object
type, mode, and symlink target. Scoped Claude catalog publication reads this
marker-owned snapshot, while Responses `/ollama/v1/models` remains live and
never rereads the snapshot during refresh. `uninstall-ollama` removes the
inventory, exactly referenced Codex profile/catalog pair, and marked Claude
launcher as one group. Any unowned path is preserved and reported as an
ownership conflict. The refresh state remains in place by default.

The hermetic G3 wrapper uses the explicit test-only
`tests/helpers/verify_isolated_convergence.py` entrypoint. It cannot bypass the
production deployment-drift command. Real target acceptance and G4 must still
run `scripts/check-deployment-drift.py` against the governed account home.

## RTK prerequisite

The command resolves an explicit `--rtk-bin` first. Otherwise, the exact host
`PATH` must identify one distinct regular executable. Multiple distinct
candidates fail closed.

The managed discovery link is:

```text
~/.headroom/bin/rtk
```

Missing `.headroom` directories are created as real owner-only directories.
Symlinked parents and unmanaged conflicting files or links are preserved and
reported. Reverso does not invoke RTK from its embedded Headroom request path;
RTK remains a host-side client optimization.
