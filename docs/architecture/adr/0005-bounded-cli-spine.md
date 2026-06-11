---
type: adr
project: reverso
id: 0005
title: Bounded CLI spine owns the one-shot subprocess safety contract
status: Accepted
date: 2026-06-11
supersedes: none
related:
  - docs/architecture/adr/0002-responses-native-provider-gateway.md
  - docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md
---

# ADR 0005: Bounded CLI spine owns the one-shot subprocess safety contract

## Status

Accepted, 2026-06-11.

## Decision

All one-shot CLI invocations made by CLI-backed provider adapters run through
a single module, `src/reverso/protocols/adapters/cli_spine.py`
(`run_bounded_cli`). The spine owns three non-negotiable properties:

1. Wall-clock bound: every invocation carries a timeout (default 300s). No
   caller may run a provider CLI unbounded.
2. Redaction before logging: child stderr passes through `redact_secret`
   before any log line is emitted.
3. Cause suppression on nonzero exit: `subprocess.CalledProcessError` carries
   raw stderr and argv, so the provider-typed error is raised `from None`.
   The cause must never ride a traceback into logs or error bodies.

Adapters contribute only their argv (plus an optional child environment) and
parse the returned stdout themselves.

## Drivers

ADR 0003 established the claude adapter as the "precedent subprocess spine"
for auggie. The spine therefore existed only as a copy-paste convention, and
the copies diverged on safety-relevant behavior: the auggie runner gained a
300s bound and cause suppression during review hardening, while the claude
runner kept no timeout at all (a hung CLI pinned a gateway worker thread
indefinitely) and re-raised with its `CalledProcessError` cause attached
(raw stderr traveled with any traceback). A fix shipped to one adapter did
not propagate to the other.

## Alternatives considered

1. Keep the copy-paste precedent and add the missing bound and suppression to
   the claude runner by hand. Rejected: restores parity once but leaves the
   contract spread across call sites, so the next hardening diverges again.
2. A common adapter base class. Rejected: the adapters share only the
   subprocess invocation, not auth, parsing, or streaming; a base class would
   couple far more surface than actually varies.

## Why chosen

A single function with a small interface concentrates the safety contract
where it can be reviewed, hardened, and tested once. The error-mode matrix
(missing binary, timeout, nonzero exit with secret-bearing stderr) is pinned
by `tests/unit/test_cli_spine.py` at the spine seam; adapter tests verify
only argv construction and stdout parsing.

## Consequences

- The claude one-shot runner is now bounded at the shared 300s default. A
  legitimately longer turn surfaces `claude CLI timed out` instead of
  hanging; raise the per-call `timeout` if a provider needs more headroom.
- Cause suppression makes the original `CalledProcessError` unavailable in
  tracebacks BY DESIGN. Do not "fix" this by reattaching the cause; the
  redacted warning log is the supported diagnostic.
- The claude streaming subprocess (`asyncio.create_subprocess_exec`) stays
  outside the spine: only one adapter streams via subprocess, and its
  lifecycle (incremental reads, preflight fallback) does not fit a one-shot
  interface. Revisit if a second streaming CLI adapter appears.
- The frozen ProviderAdapter Protocol (ADR 0002 11.3) is untouched: the spine
  is an internal seam behind the adapter interface.

## Cross-cutting reminders the implementer must respect

- Token material is never logged; `env` values passed to the spine are
  handed to the child process only.
- The gateway binds 127.0.0.1:64946 only; the spine introduces no listener.
