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

## Amendment 2026-06-12: the spine also owns the streaming subprocess

The Consequences bullet above that left the claude streaming subprocess
outside the spine is superseded by this amendment. The trigger was not a
second streaming adapter but a safety divergence of exactly the kind this
ADR exists to prevent: the streaming runner had NO wall-clock bound (a
wedged CLI pinned the SSE connection indefinitely), duplicated stderr
redaction inline, and on consumer abandon (client disconnect mid-stream)
waited for the child instead of killing it, leaking a running CLI that
completed its turn unobserved.

`stream_bounded_cli` is the streaming variant of the spine, owning:

1. Wall-clock bound: ONE shared deadline (default 300s) covers the whole
   invocation; every stdout read is capped by the remaining budget.
2. Redaction before logging: nonzero-exit stderr passes through
   `redact_secret`, as in the one-shot path.
3. Redaction-safe failures: every failure mode raises
   `BoundedCliStreamFailure` whose message never carries stderr or argv;
   `returncode` is set only for the nonzero-exit mode so adapters can
   distinguish it without parsing messages.
4. Kill-on-abandon: when the consumer stops iterating, the child is killed
   and reaped, never leaked.

Adapters contribute argv and line parsing only; the claude runner keeps its
preflight-versus-mid-stream policy (B2) on top of the spine, including the
preserved parity that a nonzero exit AFTER emitted text is treated as benign
EOF. The streaming error-mode matrix is pinned in
`tests/unit/test_cli_spine.py` alongside the one-shot matrix.
