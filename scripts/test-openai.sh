#!/usr/bin/env bash
# Test the OpenAI/Codex CLI provider (gpt-4.1) through the gateway.
#
# Prerequisites:
#   - reverso-proxy running on 127.0.0.1:4000
#   - codex CLI on PATH and authenticated
#
# Usage:
#   scripts/test-openai.sh

set -euo pipefail

BASE="http://127.0.0.1:4000"

echo "==> Testing gpt-4.1 via gateway..."
PAYLOAD='{"model":"gpt-4.1","messages":[{"role":"user","content":"Say hello and identify yourself briefly."}]}'

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
