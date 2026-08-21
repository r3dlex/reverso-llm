#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="$(command -v uv)"
VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/reverso-ollama-g3.XXXXXX")"
VERIFY_ROOT="$(cd "${VERIFY_ROOT}" && pwd -P)"
VERIFY_HOME="${VERIFY_ROOT}/home"
FAKE_BIN="${VERIFY_ROOT}/bin"
GATEWAY_PID=""
cleanup() {
    if [[ -n "${GATEWAY_PID}" ]]; then kill "${GATEWAY_PID}" 2>/dev/null || true; fi
    rm -rf "${VERIFY_ROOT}"
}
trap cleanup EXIT
mkdir -p "${VERIFY_HOME}" "${FAKE_BIN}"

cat >"${FAKE_BIN}/rtk" <<'EOF'
#!/bin/sh
exit 0
EOF
cat >"${FAKE_BIN}/claude" <<'EOF'
#!/bin/sh
exit 0
EOF
cat >"${FAKE_BIN}/codex" <<'EOF'
#!/bin/sh
if [ "${1:-}" = "debug" ]; then
  printf '%s\n' '{"models":[{"slug":"gpt-5.5"},{"slug":"claude-opus-4-8"},{"slug":"copilot/gpt-5.5"},{"slug":"auggie/auggie-model"},{"slug":"deepseek-v4-pro"},{"slug":"kimi-k3"},{"slug":"qwen3:8b"},{"slug":"codex-direct/gpt-5.5"}]}'
fi
exit 0
EOF
chmod 0755 "${FAKE_BIN}/rtk" "${FAKE_BIN}/claude" "${FAKE_BIN}/codex"
ln -s "${UV_BIN}" "${FAKE_BIN}/uv"

export HOME="${VERIFY_HOME}"
export PATH="${FAKE_BIN}:/usr/bin:/bin"
export REVERSO_ACCEPTANCE_RTK_BIN="${FAKE_BIN}/rtk"
export REVERSO_ACCEPTANCE_CODEX_BIN="${FAKE_BIN}/codex"
export REVERSO_OLLAMA_CLOUD=0
export OLLAMA_NO_CLOUD=1

uv run pytest tests/unit/test_ollama_convergence.py tests/unit/test_catalog_refresh.py tests/unit/test_client_convergence_contract.py -q
uv run pytest tests/integration/test_ollama_convergence_runbook.py tests/integration/test_client_convergence_runbook.py -q

FAKE_GATEWAY_PORT="$(uv run python - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"
export REVERSO_FAKE_GATEWAY_PORT="${FAKE_GATEWAY_PORT}"
export REVERSO_CODEX_BASE_URL="http://127.0.0.1:${FAKE_GATEWAY_PORT}"
export REVERSO_HEADROOM_USAGE_URL="${REVERSO_CODEX_BASE_URL}/usage/headroom"
uv run python tests/helpers/fake_convergence_gateway.py &
GATEWAY_PID=$!
sleep 1
kill -0 "${GATEWAY_PID}"

uv run reverso-client-sync dry-run --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync refresh --json
uv run reverso-client-sync verify --json
./scripts/convergence-acceptance.sh
uv run python tests/helpers/verify_isolated_convergence.py --home "${VERIFY_HOME}" --rtk-bin "${FAKE_BIN}/rtk"
uv run pytest tests/ -v --ignore=tests/integration --tb=short
