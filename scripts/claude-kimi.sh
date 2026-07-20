#!/bin/sh
set -eu

model=${REVERSO_KIMI_MODEL:-kimi-for-coding}
case "$model" in
    ""|*/*|anthropic-*)
        echo "REVERSO_KIMI_MODEL must be a bare Kimi model id" >&2
        exit 2
        ;;
esac

for arg in "$@"; do
    case "$arg" in
        --model|--model=*|--fallback-model|--fallback-model=*|--settings|--settings=*|--setting-sources|--setting-sources=*)
            echo "launcher-owned options are not accepted; set REVERSO_KIMI_MODEL for model selection" >&2
            exit 2
            ;;
    esac
done

export ANTHROPIC_BASE_URL="http://127.0.0.1:64946/kimi"
export ANTHROPIC_AUTH_TOKEN="reverso-local-loopback"
unset ANTHROPIC_API_KEY
unset CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="1"
export ANTHROPIC_CUSTOM_HEADERS="x-reverso-workspace: $PWD"

# Claude Code's user settings can inject env values after process startup and
# override the launcher exports above. Pin the same loopback contract through
# the highest-precedence --settings source so a stale global gateway cannot
# redirect this provider-specific launcher.
pinned_settings='{"env":{"ANTHROPIC_API_KEY":"","ANTHROPIC_AUTH_TOKEN":"reverso-local-loopback","ANTHROPIC_BASE_URL":"http://127.0.0.1:64946/kimi","CLAUDE_CODE_OAUTH_TOKEN":""}}'

exec "${CLAUDE_BIN:-claude}" --settings "$pinned_settings" --model "$model" "$@"
