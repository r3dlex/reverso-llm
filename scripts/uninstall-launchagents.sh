#!/usr/bin/env bash
# Uninstall Reverso launchd LaunchAgents for the current user.
#
# Usage:
#   scripts/uninstall-launchagents.sh [--purge-state]

set -euo pipefail

if [[ "$#" -gt 1 ]] || [[ "$#" -eq 1 && "$1" != "--purge-state" ]]; then
    echo "Usage: scripts/uninstall-launchagents.sh [--purge-state]" >&2
    exit 2
fi

PURGE_STATE=false
if [[ "$#" -eq 1 && "$1" == "--purge-state" ]]; then
    PURGE_STATE=true
fi

USER_HOME="${HOME}"
LAUNCHD_DIR="${USER_HOME}/Library/LaunchAgents"
STATE_DIR="${USER_HOME}/Library/Application Support/reverso"
LOG_DIR="${USER_HOME}/Library/Logs/reverso"

AGENTS=(
    "com.user.reverso-proxy"
    "com.user.reverso-daemon"
    "com.user.reverso-catalog-refresh"
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

if [[ "${PURGE_STATE}" == "true" ]]; then
    REFRESH_ARTIFACTS=(
        "${STATE_DIR}/catalog-refresh.lock"
        "${STATE_DIR}/catalog-refresh-status.json"
        "${LOG_DIR}/catalog-refresh.stdout.log"
        "${LOG_DIR}/catalog-refresh.stdout.log.1"
        "${LOG_DIR}/catalog-refresh.stdout.log.2"
        "${LOG_DIR}/catalog-refresh.stdout.log.3"
        "${LOG_DIR}/catalog-refresh.stderr.log"
        "${LOG_DIR}/catalog-refresh.stderr.log.1"
        "${LOG_DIR}/catalog-refresh.stderr.log.2"
        "${LOG_DIR}/catalog-refresh.stderr.log.3"
    )
    for artifact in "${REFRESH_ARTIFACTS[@]}"; do
        rm -f "${artifact}"
    done
    echo "Removed scheduled refresh state and logs."
fi

echo "Done. Reverso LaunchAgents uninstalled."
