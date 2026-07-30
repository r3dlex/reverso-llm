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
reverso-client-sync {dry-run,apply,refresh,verify}
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
- `refresh` validates and applies only when the shared lock is immediately
  available. Contention is a benign `lock_skipped` result.
- `verify` validates and reports drift without writing or acquiring the lock.

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
