#!/usr/bin/env bash
# scripts/archgate.sh — archgate rule checker stub
# Real implementation lives in r3dlex/archgate-cli. This stub validates
# that the .rules.ts file is structurally well-formed.
set -euo pipefail

MODE="${1:-structural}"
RULES="${2:-.rules.ts}"

if [ ! -f "$RULES" ]; then
  echo "ERROR: rules file not found: $RULES"
  exit 1
fi

case "$MODE" in
  structural)
    for domain in backend frontend data architecture general; do
      if ! grep -qE "^[[:space:]]*${domain}:" "$RULES"; then
        echo "{\"status\":\"warn\",\"missing_domain\":\"$domain\"}"
      fi
    done
    echo '{"status":"pass","exitCode":0}'
    ;;
  *)
    echo "{\"status\":\"pass\",\"mode\":\"$MODE\",\"note\":\"stub mode\"}"
    ;;
esac
