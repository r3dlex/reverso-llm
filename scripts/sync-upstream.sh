#!/usr/bin/env bash
# scripts/sync-upstream.sh — physical-copy sync scaffold
# Reads upstream.lock and produces a sync plan. Does NOT mutate remote state
# without explicit confirmation.
set -euo pipefail

if [ ! -f upstream.lock ]; then
  echo "ERROR: upstream.lock not found at repo root"
  exit 1
fi

UPSTREAM_URL=$(python3 -c "import json; print(json.load(open('upstream.lock'))['source']['url'])")
PINNED_SHA=$(python3 -c "import json; print(json.load(open('upstream.lock'))['pinned_sha'])")

echo "Upstream: $UPSTREAM_URL"
echo "Pinned SHA: $PINNED_SHA"
echo
echo "DRY-RUN: physical-copy sync plan"
echo "  1. git fetch $UPSTREAM_URL"
echo "  2. git checkout $PINNED_SHA -- .ai/ .memory/ docs/architecture/"
echo "  3. Re-run .ai/drift/last-generation.json audit"
echo
echo "To apply, run: SYNC_CONFIRM=ct-$(date -u +%Y-%m-%d)-001 $0 apply"
