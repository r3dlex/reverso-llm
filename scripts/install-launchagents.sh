#!/usr/bin/env bash
# Install Reverso launchd LaunchAgents for the current user.
#
# Usage:
#   scripts/install-launchagents.sh
#
# What it does:
#   1. Resolves REVERSO_DIR, UV_BIN, USER_HOME.
#   2. Expands the .plist.tmpl templates into ~/Library/LaunchAgents/.
#   3. Creates the log directory ~/Library/Logs/reverso/.
#   4. Loads (or reloads) the agents via launchctl.
#
# Run again to update after changing config; the script unloads before reloading.

set -euo pipefail

REVERSO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_HOME="${HOME}"
LAUNCHD_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="${USER_HOME}/Library/Logs/reverso"

# Locate uv
UV_BIN="$(command -v uv 2>/dev/null || echo "")"
if [[ -z "${UV_BIN}" ]]; then
    echo "ERROR: uv not found on PATH. Install uv first: https://docs.astral.sh/uv/" >&2
    exit 1
fi

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
        -e "s|{{UV_BIN}}|${UV_BIN}|g" \
        -e "s|{{USER_HOME}}|${USER_HOME}|g" \
        "${TMPL}" > "${DEST}"

    echo "Written: ${DEST}"

    # Unload if already loaded (ignore errors - agent may not be loaded yet)
    launchctl unload "${DEST}" 2>/dev/null || true
    launchctl load "${DEST}"
    echo "Loaded:  ${AGENT}"
done

echo ""
echo "Done. Reverso LaunchAgents installed."
echo "Logs: ${LOG_DIR}"
echo ""
echo "To check status:"
echo "  launchctl list | grep reverso"
