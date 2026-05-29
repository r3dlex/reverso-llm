#!/usr/bin/env bash
# Uninstall Reverso launchd LaunchAgents for the current user.
#
# Usage:
#   scripts/uninstall-launchagents.sh

set -euo pipefail

USER_HOME="${HOME}"
LAUNCHD_DIR="${USER_HOME}/Library/LaunchAgents"

AGENTS=(
    "com.user.reverso-proxy"
    "com.user.reverso-daemon"
)

for AGENT in "${AGENTS[@]}"; do
    PLIST="${LAUNCHD_DIR}/${AGENT}.plist"
    if [[ -f "${PLIST}" ]]; then
        launchctl unload "${PLIST}" 2>/dev/null || true
        rm -f "${PLIST}"
        echo "Removed: ${PLIST}"
    else
        echo "Not found (skipping): ${PLIST}"
    fi
done

echo "Done. Reverso LaunchAgents uninstalled."
