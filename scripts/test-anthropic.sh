#!/usr/bin/env bash
# Test the Anthropic CLI provider (claude-sonnet-4-6) through the gateway.
#
# Prerequisites:
#   - reverso-proxy running on 127.0.0.1:64946
#   - claude CLI on PATH and authenticated
#
# Usage:
#   scripts/test-anthropic.sh

set -euo pipefail

BASE="http://127.0.0.1:64946"

echo "==> Testing claude-sonnet-4-6 via gateway..."
PAYLOAD='{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"Say hello and identify yourself briefly."}]}'

RESPONSE=$(curl -sf -X POST "${BASE}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}")

echo "${RESPONSE}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('Model:   ', r.get('model'))
print('Content: ', r['choices'][0]['message']['content'][:200])
xgw = r.get('x_gateway') or r.get('_hidden_params', {}).get('x_gateway', {})
print('x_gateway:', json.dumps(xgw, indent=2))
"
