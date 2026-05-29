#!/usr/bin/env bash
# Start the Reverso proxy in the foreground (development / debugging).
#
# Usage:
#   scripts/start-fg-proxy.sh
#
# Ctrl-C to stop.

set -euo pipefail

REVERSO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec uv run --project "${REVERSO_DIR}" reverso-proxy
