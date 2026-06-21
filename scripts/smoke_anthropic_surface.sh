#!/usr/bin/env bash
# Loopback manual smoke test for the inbound Anthropic Messages surface (ADR 0006).
#
# This exercises the Anthropic Messages dialect that Reverso serves on the same
# loopback port as the OpenAI Responses surface. The surface is INBOUND ONLY:
# Reverso never calls api.anthropic.com upstream; it translates Anthropic Messages
# traffic onto the existing Responses backends (copilot, deepseek, auggie). claude
# is excluded from this surface (ADR 0006 D2 circularity), so a /claude/... request
# returns a 404 not_found_error rather than reaching any backend.
#
# Prerequisites:
#   - reverso-proxy running on 127.0.0.1:${REVERSO_PORT:-64946}
#   - curl available
#
# Client configuration note:
#   An Anthropic client (Claude Code, the Claude Agent SDK) reaches this surface
#   by pointing ANTHROPIC_BASE_URL at the loopback gateway and sending an
#   anthropic-version header. A missing anthropic-version is not a 400: the surface
#   defaults it to 2023-06-01 and echoes it back. Concretely:
#       export ANTHROPIC_BASE_URL=http://127.0.0.1:64946
#       (clients send the header:  anthropic-version: 2023-06-01)
#
# This script contains NO secrets. It targets 127.0.0.1 ONLY. Each step echoes
# what it checks; the curl invocations are real and safe to run against a locally
# running proxy.
#
# Usage:
#   scripts/smoke_anthropic_surface.sh

set -euo pipefail

PORT="${REVERSO_PORT:-64946}"
BASE="http://127.0.0.1:${PORT}"
VERSION="2023-06-01"

# A deepseek model id from config/litellm_config.yaml. The surface_registry
# resolves this to the deepseek backend; change it to any Anthropic-surface model
# id returned by GET /v1/models (copilot, deepseek, or auggie family).
MODEL="deepseek-v4-pro"

echo "==> Anthropic surface smoke test against ${BASE}"
echo "    backends on this surface: copilot, deepseek, auggie (claude excluded)"
echo "    clients set ANTHROPIC_BASE_URL=${BASE} and anthropic-version: ${VERSION}"
echo ""

# --- Step 1: POST /v1/messages (non-streaming) ------------------------------
# Checks: a Messages request auto-resolves the model to a backend through the
# single surface_registry authority and returns an Anthropic message body
# (type "message", role "assistant", a content array, stop_reason, usage).
echo "==> Step 1: POST /v1/messages (non-streaming, model=${MODEL})"
echo "    expect: HTTP 200, JSON {\"type\":\"message\",\"role\":\"assistant\",...}"
curl -sS -X POST "${BASE}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "anthropic-version: ${VERSION}" \
    -d "{
        \"model\": \"${MODEL}\",
        \"max_tokens\": 64,
        \"messages\": [
            {\"role\": \"user\", \"content\": \"Say hello in one short sentence.\"}
        ]
    }"
echo ""
echo ""

# --- Step 2: POST /v1/messages with stream:true -----------------------------
# Checks: the Anthropic SSE event grammar is emitted. Expect, in order,
# message_start, ping, content_block_start, content_block_delta(s),
# content_block_stop, message_delta, message_stop, each on the wire as
# "event: <type>\ndata: <json>\n\n". A connect/setup failure before the first
# event returns a non-streaming 502 JSON envelope instead.
echo "==> Step 2: POST /v1/messages (stream:true, Anthropic SSE)"
echo "    expect: text/event-stream with message_start, ping, content_block_*,"
echo "            message_delta, message_stop events"
curl -sS -N -X POST "${BASE}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "anthropic-version: ${VERSION}" \
    -d "{
        \"model\": \"${MODEL}\",
        \"max_tokens\": 64,
        \"stream\": true,
        \"messages\": [
            {\"role\": \"user\", \"content\": \"Count from one to three.\"}
        ]
    }"
echo ""
echo ""

# --- Step 3: POST /v1/messages/count_tokens ---------------------------------
# Checks: the pre-flight sizing endpoint returns {"input_tokens": N}. This is a
# DOCUMENTED word-count APPROXIMATION (ADR 0006), not a real provider tokenizer.
echo "==> Step 3: POST /v1/messages/count_tokens"
echo "    expect: HTTP 200, JSON {\"input_tokens\": N} (word-count approximation)"
curl -sS -X POST "${BASE}/v1/messages/count_tokens" \
    -H "Content-Type: application/json" \
    -H "anthropic-version: ${VERSION}" \
    -d "{
        \"model\": \"${MODEL}\",
        \"messages\": [
            {\"role\": \"user\", \"content\": \"How many tokens is this prompt?\"}
        ]
    }"
echo ""
echo ""

# --- Step 4: GET /v1/models -------------------------------------------------
# Checks: the Anthropic-shaped listing of the surface model set. Every row is a
# {"type":"model","id","display_name","created_at"} entry; NO claude model is
# ever present (the registry index excludes the claude family).
echo "==> Step 4: GET /v1/models"
echo "    expect: HTTP 200, {\"data\":[{\"type\":\"model\",...}],...}; no claude id"
curl -sS "${BASE}/v1/models" \
    -H "anthropic-version: ${VERSION}"
echo ""
echo ""

# --- Step 5: POST /claude/v1/messages (negative, expect 404) ----------------
# Checks: claude is excluded from the Anthropic surface (ADR 0006 D2). A
# claude-pinned request is CLAIMED by this surface (never delegated to legacy) and
# returns an Anthropic-shaped 404 not_found_error envelope.
echo "==> Step 5: POST /claude/v1/messages (negative, expect 404 not_found_error)"
echo "    expect: HTTP 404, {\"type\":\"error\",\"error\":{\"type\":\"not_found_error\",...}}"
curl -sS -o /dev/null -w "    HTTP status: %{http_code}\n" \
    -X POST "${BASE}/claude/v1/messages" \
    -H "Content-Type: application/json" \
    -H "anthropic-version: ${VERSION}" \
    -d "{
        \"model\": \"claude-sonnet-4-6\",
        \"max_tokens\": 64,
        \"messages\": [
            {\"role\": \"user\", \"content\": \"This must be rejected with 404.\"}
        ]
    }"
echo ""
echo "==> Done. Review the output above against each step's expectation."
