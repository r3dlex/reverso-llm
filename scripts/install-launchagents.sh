#!/usr/bin/env bash
# Install Reverso launchd LaunchAgents for the current user.
#
# Usage:
#   scripts/install-launchagents.sh
#
# What it does:
#   1. Verifies the canonical clean checkout and selected Git revision.
#   2. Atomically writes and reads back deployment provenance.
#   3. Expands the .plist.tmpl templates into ~/Library/LaunchAgents/.
#   4. Revalidates rendered provenance before calling launchctl.
#   5. Reads back running LaunchAgent authority before returning.
#
# Run again to update after changing config; the script unloads before reloading.

set -euo pipefail

REVERSO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANONICAL_REVERSO_DIR="/Users/andresilvaburgstahler/.local/share/reverso"
USER_HOME="${HOME}"
LAUNCHD_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="${USER_HOME}/Library/Logs/reverso"

if [[ "${REVERSO_DIR}" != "${CANONICAL_REVERSO_DIR}" ]]; then
    echo "ERROR: installer must run from canonical checkout ${CANONICAL_REVERSO_DIR}" >&2
    exit 1
fi

# Locate uv
UV_BIN="$(command -v uv 2>/dev/null || echo "")"
if [[ -z "${UV_BIN}" ]]; then
    echo "ERROR: uv not found on PATH. Install uv first: https://docs.astral.sh/uv/" >&2
    exit 1
fi
export REVERSO_UV_BIN="${UV_BIN}"

REVERSO_DEPLOYMENT_COMMIT="$(git -C "${REVERSO_DIR}" rev-parse HEAD)"
export REVERSO_DEPLOYMENT_COMMIT

run_deployment_drift() {
    "${UV_BIN}" run --project "${REVERSO_DIR}" python \
        "${REVERSO_DIR}/scripts/check-deployment-drift.py" "$@"
}

run_deployment_drift --phase pre-install
run_deployment_drift \
    --phase pre-install \
    --write-provenance

mkdir -p "${LAUNCHD_DIR}" "${LOG_DIR}"

AGENTS=(
    "com.user.reverso-proxy"
    "com.user.reverso-daemon"
)

for AGENT in "${AGENTS[@]}"; do
    TMPL="${REVERSO_DIR}/launchd/${AGENT}.plist.tmpl"
    DEST="${LAUNCHD_DIR}/${AGENT}.plist"

    if [[ ! -f "${TMPL}" ]]; then
        echo "WARNING: template not found: ${TMPL}" >&2
        continue
    fi

    sed \
        -e "s|{{REVERSO_DIR}}|${REVERSO_DIR}|g" \
        -e "s|{{REVERSO_DEPLOYMENT_COMMIT}}|${REVERSO_DEPLOYMENT_COMMIT}|g" \
        -e "s|{{UV_BIN}}|${UV_BIN}|g" \
        -e "s|{{USER_HOME}}|${USER_HOME}|g" \
        "${TMPL}" > "${DEST}"

    echo "Written: ${DEST}"
done

run_deployment_drift --phase pre-restart

for AGENT in "${AGENTS[@]}"; do
    DEST="${LAUNCHD_DIR}/${AGENT}.plist"
    # Unload if already loaded (ignore errors - agent may not be loaded yet)
    launchctl unload "${DEST}" 2>/dev/null || true
    launchctl load "${DEST}"
    echo "Loaded:  ${AGENT}"
done

run_deployment_drift --phase post-restart

echo ""
echo "Done. Reverso LaunchAgents installed."
echo "Logs: ${LOG_DIR}"
echo ""
echo "To check status:"
echo "  launchctl list | grep reverso"
