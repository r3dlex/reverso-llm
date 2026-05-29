#!/usr/bin/env bash
# Store API keys in macOS Keychain under the reverso/ namespace.
#
# Usage:
#   scripts/keychain-set.sh DEEPSEEK_API_KEY   <key_value>
#   scripts/keychain-set.sh MINIMAX_API_KEY    <key_value>
#
# The service name written to Keychain is "reverso/<KEY_NAME>".
# reverso-proxy reads these at startup via `security find-generic-password`.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <KEY_NAME> <key_value>" >&2
    echo "  KEY_NAME: DEEPSEEK_API_KEY | MINIMAX_API_KEY" >&2
    exit 1
fi

KEY_NAME="$1"
KEY_VALUE="$2"
SERVICE="reverso/${KEY_NAME}"
ACCOUNT="${USER}"

# Delete any existing entry first (add -D generic for type, ignore errors)
security delete-generic-password -s "${SERVICE}" -a "${ACCOUNT}" 2>/dev/null || true

security add-generic-password \
    -s "${SERVICE}" \
    -a "${ACCOUNT}" \
    -w "${KEY_VALUE}"

echo "Stored: ${SERVICE} (account: ${ACCOUNT})"
