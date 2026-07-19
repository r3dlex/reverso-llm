#!/bin/sh
set -eu

model=${REVERSO_KIMI_MODEL:-kimi-k2.5}
case "$model" in
    ""|*/*|anthropic-*)
        echo "REVERSO_KIMI_MODEL must be a bare Kimi model id" >&2
        exit 2
        ;;
esac

export ANTHROPIC_BASE_URL="http://127.0.0.1:64946/kimi"
export ANTHROPIC_AUTH_TOKEN="reverso-local-loopback"
unset ANTHROPIC_API_KEY
unset CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="1"
export ANTHROPIC_CUSTOM_HEADERS="x-reverso-workspace: $PWD"

exec "${CLAUDE_BIN:-claude}" --model "$model" "$@"
