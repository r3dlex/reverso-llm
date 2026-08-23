---
title: "Two load-sensitive flaky test clusters fail about one full run in two"
status: done
state: done
category: defect
slug: flaky-load-sensitive-tests
owner: unassigned
execution_type: AFK
---

# Two load-sensitive flaky test clusters

Observed repeatedly across the OpenCode Go slice series. Roughly one full suite
run in two failed, a DIFFERENT test each time, always in one of two files, and
every affected test passed in isolation and on clean `main`.

## Root cause 1: 0.1s async coordination budgets

`tests/unit/test_kimi_login.py` had 20 `asyncio.wait_for(..., timeout=0.1)` calls
waiting on async coordination events. Those are not latency assertions: each waits
for an event that either fires or does not, so a tight budget buys nothing except
sensitivity to CPU contention. Under a loaded run, whichever test lost the
scheduler race failed, which is exactly why the failing test moved around.

Fixed by one named constant, `_EVENT_WAIT = 10.0`. The `timeout_seconds` values
belonging to the code under test are deliberately untouched.

## Root cause 2: a real dependency plus a success-only assertion

`test_real_headroom_smoke_uses_memory_only_state` invoked the REAL Headroom
compressor with a hard-coded 5s timeout and asserted the outcome was
`compressed` or `unchanged`. Under load the compressor exceeds that and fails
open with `timeout`, so the test failed for a reason unrelated to the property it
guards, which is its final assertion: the real path must write nothing under
`HOME`.

Fixed by accepting fail-open outcomes and raising the timeout to 30s, so a
fail-open now means a genuine hang rather than ordinary contention.

## Evidence

Reproduced deliberately rather than inferred. With the fix stashed, under six
busy-loop CPU hogs:

```
pre-fix full run 1: 1429 passed
pre-fix full run 2: 1429 passed
pre-fix full run 3: 1 failed, 1428 passed
pre-fix full run 4: 2 failed, 1427 passed
PRE-FIX FAILURES: 2/4
```

With the fix applied, three of three loaded runs were clean.

Note that running the two affected files alone under the same external load does
NOT reproduce the failure. The contention that triggers it comes from the full
suite itself, which is why `tests/verify_flaky_load_fix.sh` runs the whole suite
three times rather than the two files.

## Guard against the obvious risk

Loosening a wait can hide a real regression, so the change is paired with a
mutation proof: making the login task never run fails the suite at the new budget
(in 10s) instead of passing. The waits still gate on the event actually firing.

No test was skipped, deselected or marked xfail.
