#!/usr/bin/env bash
# Smoke tests for the Reverso gateway.
#
# Prerequisites:
#   - reverso-proxy running on 127.0.0.1:64946
#   - curl available
#
# Usage:
#   scripts/smoke.sh
#
# Exit code 0 = all tests passed, non-zero = at least one failure.

set -euo pipefail

BASE="http://127.0.0.1:64946"
PASS=0
FAIL=0

check() {
    local name="$1"
    local result="$2"
    local expected="$3"
    if echo "${result}" | grep -q "${expected}"; then
        echo "  PASS: ${name}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${name}"
        echo "        expected to find: ${expected}"
        echo "        got: ${result}" | head -5
        FAIL=$((FAIL + 1))
    fi
}

echo "==> Smoke: /healthz"
R=$(curl -sf "${BASE}/healthz" 2>&1 || echo "CONNECTION_ERROR")
check "healthz" "${R}" "."

echo "==> Smoke: /v1/models"
R=$(curl -sf "${BASE}/v1/models" 2>&1 || echo "CONNECTION_ERROR")
check "models endpoint" "${R}" "data"

echo "==> Smoke: deepseek profile GPT-level alias"
PAYLOAD='{"model":"gpt-5.5","messages":[{"role":"user","content":"Reply with exactly: SMOKE_OK"}]}'
R=$(curl -sf -X POST "${BASE}/deepseek/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}" 2>&1 || echo "CONNECTION_ERROR")
check "deepseek profile response" "${R}" "choices"

echo "==> Smoke: minimax profile GPT-level alias"
PAYLOAD='{"model":"gpt-5.5","messages":[{"role":"user","content":"Reply with exactly: SMOKE_OK"}]}'
R=$(curl -sf -X POST "${BASE}/minimax/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}" 2>&1 || echo "CONNECTION_ERROR")
check "minimax profile response" "${R}" "choices"

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[[ ${FAIL} -eq 0 ]]
