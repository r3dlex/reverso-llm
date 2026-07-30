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
CANONICAL_USER_HOME="/Users/andresilvaburgstahler"
if [[ -z "${HOME:-}" || "${HOME}" != "${CANONICAL_USER_HOME}" ]]; then
    echo "ERROR: HOME must match canonical account home ${CANONICAL_USER_HOME}" >&2
    exit 1
fi
USER_HOME="${CANONICAL_USER_HOME}"
LAUNCHD_DIR="${USER_HOME}/Library/LaunchAgents"
STATE_DIR="${USER_HOME}/Library/Application Support/reverso"
LOG_DIR="${USER_HOME}/Library/Logs/reverso"
KIMI_CODE_HOME="${USER_HOME}/Library/Application Support/reverso/kimi-code"

if [[ "${REVERSO_DIR}" != "${CANONICAL_REVERSO_DIR}" ]]; then
    echo "ERROR: installer must run from canonical checkout ${CANONICAL_REVERSO_DIR}" >&2
    exit 1
fi

require_real_kimi_home_path() {
    local path_component
    local resolved_user_home
    if [[ ! -d "${USER_HOME}" || -L "${USER_HOME}" ]]; then
        echo "ERROR: canonical account home must be a real directory" >&2
        exit 1
    fi
    resolved_user_home="$(cd "${USER_HOME}" && pwd -P)"
    if [[ "${resolved_user_home}" != "${USER_HOME}" ]]; then
        echo "ERROR: canonical account home must resolve without symbolic links" >&2
        exit 1
    fi
    for path_component in \
        "${USER_HOME}" \
        "${USER_HOME}/Library" \
        "${USER_HOME}/Library/Application Support" \
        "${USER_HOME}/Library/Application Support/reverso" \
        "${KIMI_CODE_HOME}"; do
        if [[ -L "${path_component}" ]]; then
            echo "ERROR: governed KIMI_CODE_HOME path must not contain symbolic links" >&2
            exit 1
        fi
    done
}

prepare_private_directory() {
    local directory="$1"
    if [[ -L "${directory}" || ( -e "${directory}" && ! -d "${directory}" ) ]]; then
        echo "ERROR: refresh directory must be a real directory: ${directory}" >&2
        exit 1
    fi
    mkdir -p "${directory}"
    if [[ -L "${directory}" || ! -d "${directory}" ]]; then
        echo "ERROR: refresh directory must be a real directory: ${directory}" >&2
        exit 1
    fi
    chmod 0700 "${directory}"
}

require_real_kimi_home_path

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

prepare_private_directory "${STATE_DIR}"
prepare_private_directory "${LOG_DIR}"
require_real_kimi_home_path
mkdir -p "${KIMI_CODE_HOME}"
require_real_kimi_home_path
chmod 0700 "${KIMI_CODE_HOME}"
require_real_kimi_home_path

run_deployment_drift \
    --phase pre-install \
    --write-provenance

mkdir -p "${LAUNCHD_DIR}"

LONG_LIVED_AGENTS=(
    "com.user.reverso-proxy"
    "com.user.reverso-daemon"
)
SCHEDULED_AGENT="com.user.reverso-catalog-refresh"
AGENTS=("${LONG_LIVED_AGENTS[@]}" "${SCHEDULED_AGENT}")

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
        -e "s|{{KIMI_CODE_HOME}}|${KIMI_CODE_HOME}|g" \
        "${TMPL}" > "${DEST}"

    echo "Written: ${DEST}"
done

run_deployment_drift --phase pre-restart

for AGENT in "${LONG_LIVED_AGENTS[@]}"; do
    DEST="${LAUNCHD_DIR}/${AGENT}.plist"
    # Unload if already loaded (ignore errors - agent may not be loaded yet)
    launchctl unload "${DEST}" 2>/dev/null || true
    launchctl load "${DEST}"
    echo "Loaded:  ${AGENT}"
done

run_deployment_drift --phase post-restart

SCHEDULED_PLIST="${LAUNCHD_DIR}/${SCHEDULED_AGENT}.plist"
launchctl unload "${SCHEDULED_PLIST}" 2>/dev/null || true
launchctl load "${SCHEDULED_PLIST}"
echo "Loaded:  ${SCHEDULED_AGENT}"

if ! "${UV_BIN}" run --project "${REVERSO_DIR}" reverso-catalog-refresh; then
    echo "WARNING: initial catalog refresh did not complete successfully" >&2
fi

echo ""
echo "Done. Reverso LaunchAgents installed."
echo "Logs: ${LOG_DIR}"
echo ""
echo "To check status:"
echo "  launchctl list | grep reverso"
