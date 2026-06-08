#!/usr/bin/env bash
# scripts/validate-rules.sh — validate .rules.ts against the v3 schema
set -euo pipefail

if [ ! -f .rules.ts ]; then
  echo "ERROR: .rules.ts not found"
  exit 1
fi

if ! grep -q "defineRules" .rules.ts; then
  echo "ERROR: .rules.ts must export a defineRules() call"
  exit 1
fi

for domain in backend frontend data architecture general; do
  if ! grep -qE "^[[:space:]]*${domain}:" .rules.ts; then
    echo "WARN: .rules.ts is missing the '${domain}' domain"
  fi
done

echo "OK: .rules.ts structural validation passed"
