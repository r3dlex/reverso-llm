#!/usr/bin/env bash
#
# Verification for the load-sensitive flaky-test fix.
#
# A single run proves nothing here: the failure was NONDETERMINISTIC, roughly one
# full suite run in two, and a different test each time. So this runs the full
# suite three times under deliberate CPU contention. Pre-fix that combination
# failed about seven times in eight; post-fix it must be clean three times out of
# three.
#
# The load matters and the FULL suite matters: running the two affected files
# alone, even under external CPU hogs, does not reproduce the failure. The
# contention that triggers it comes from the suite itself.

set -euo pipefail

hogs=()
for _ in 1 2 3 4 5 6; do
  python3 -c "
import time
end = time.time() + 260
while time.time() < end:
    pass
" &
  hogs+=($!)
done

cleanup() {
  for pid in "${hogs[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

sleep 1
for run in 1 2 3; do
  echo "verify-flaky: full suite run ${run}/3 under load"
  uv run pytest tests/ --ignore=tests/integration -q
done

uv run pytest tests/unit/test_kimi_login.py tests/unit/test_headroom_compression.py -q
echo "verify-flaky: three loaded full-suite runs clean"
