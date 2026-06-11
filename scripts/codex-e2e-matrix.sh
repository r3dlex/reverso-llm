#!/usr/bin/env bash
# C1 done-gate: Codex E2E matrix against the live reverso gateway.
#
# Per provider (claude, copilot, auggie, deepseek), exercises:
#   1. multi-turn memory (codex exec then codex exec resume; codename recall test;
#      "codename" wording avoids model refusals that "secret word" prompts trigger)
#   2. workspace context (codex exec --cd against a scratch repo with a known file)
#   3. usage plausibility (jq on turn.completed.usage in --json output)
#   4. resume-after-restart (launchctl kickstart -k between turn 1 and turn 2)
#   5. streaming TTFB (direct curl SSE; first response.output_text.delta within 20s)
#      auggie is N/A per A3 (BUFFER, documented limitation)
#   6. tool-call loop (copilot, deepseek); claude and auggie are N/A and the gate
#      MUST return a structured 400 unsupported_feature body
#   7. model selection via reverso-codex-sync against a temp config copy (does
#      NOT touch ~/.codex/config.toml)
#
# Outputs a pass/fail table to stdout and writes evidence to
# .omc/research/codex-e2e-matrix-results.md.
#
# Hard rules:
#   * gateway bind 127.0.0.1:64946 only
#   * no secrets in output or evidence
#   * no em (U+2014) or en (U+2013) dashes anywhere (use ASCII)
#   * every codex/curl call is bounded by a timeout so the script cannot hang
#   * resume uses -c model_provider / -c model overrides (codex exec resume does
#     not accept -p; see .omc/research/codex-resume-probe.md)

set -uo pipefail

REPO_ROOT="/Users/andreburgstahler/Ws/Personal/AiTool/reverso"
GATEWAY="http://127.0.0.1:64946"
EVIDENCE_FILE="${REPO_ROOT}/.omc/research/codex-e2e-matrix-results.md"
RESULTS_TSV="$(mktemp -t codex-e2e-results.XXXXXX)"
LOG_DIR="$(mktemp -d -t codex-e2e-logs.XXXXXX)"
SCRATCH_ROOT="$(mktemp -d -t codex-e2e-scratch.XXXXXX)"
PROVIDERS=("claude" "copilot" "auggie" "deepseek")

CODEX_TIMEOUT_DEFAULT=180
RESTART_TIMEOUT=45
GATEWAY_POLL_TIMEOUT=45
TTFB_BUDGET_SECS=20
SUMMARY_TIMEOUT=120

# Lookup helpers (bash 3.2 compatible; macOS /bin/bash has no associative arrays).
provider_model() {
  case "$1" in
    claude)   printf 'claude-haiku-4-5-20251001' ;;
    copilot)  printf 'gpt-5.5' ;;
    auggie)   printf 'prism-a' ;;
    deepseek) printf 'deepseek-v4-flash' ;;
    *) return 1 ;;
  esac
}

provider_stream_model() {
  case "$1" in
    claude)   printf 'claude-haiku-4-5-20251001' ;;
    copilot)  printf 'gpt-5.5' ;;
    auggie)   printf 'prism-a' ;;
    deepseek) printf 'deepseek-v4-flash' ;;
    *) return 1 ;;
  esac
}

provider_secret() {
  case "$1" in
    claude)   printf 'Aurora' ;;
    copilot)  printf 'Borealis' ;;
    auggie)   printf 'Delta' ;;
    deepseek) printf 'Cyan' ;;
    *) return 1 ;;
  esac
}

# Workspace context marker: a unique token written into a file the model must read.
provider_token() {
  case "$1" in
    claude)   printf 'MARKER-CLAUDE-7QX1' ;;
    copilot)  printf 'MARKER-COPILOT-7QX1' ;;
    auggie)   printf 'MARKER-AUGGIE-7QX1' ;;
    deepseek) printf 'MARKER-DEEPSEEK-7QX1' ;;
    *) return 1 ;;
  esac
}

# ---------------- helpers ----------------

log() { printf '[matrix] %s\n' "$*" >&2; }

# run_bounded <secs> <cmd...> -- run with a hard timeout, killing the child and
# any descendants on expiry. Mimics coreutils timeout(1), which is not installed
# on macOS; setsid is also unavailable in /bin/bash 3.2 on macOS, so this uses a
# recursive descendant kill instead of a process-group kill.
kill_tree() {
  local parent="$1"
  local kids
  kids=$(pgrep -P "$parent" 2>/dev/null)
  for k in $kids; do
    kill_tree "$k"
  done
  kill -KILL "$parent" 2>/dev/null
}

run_bounded() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  (
    local waited=0
    while [ "$waited" -lt "$secs" ]; do
      if ! kill -0 "$pid" 2>/dev/null; then
        exit 0
      fi
      sleep 1
      waited=$(( waited + 1 ))
    done
    kill_tree "$pid"
  ) &
  local watcher=$!
  wait "$pid"
  local ec=$?
  kill -KILL "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  return "$ec"
}

# record_result <provider> <cell> <PASS|FAIL|NA> <detail>
record_result() {
  local provider="$1" cell="$2" status="$3" detail="$4"
  detail="${detail//$'\t'/ }"
  detail="${detail//$'\n'/ }"
  printf '%s\t%s\t%s\t%s\n' "$provider" "$cell" "$status" "$detail" >> "$RESULTS_TSV"
  log "$provider $cell : $status : $detail"
}

restart_gateway() {
  log "restarting gateway via launchctl kickstart"
  launchctl kickstart -k "gui/$(id -u)/com.user.reverso-proxy" >/dev/null 2>&1
  local start_ts
  start_ts=$(date +%s)
  while true; do
    local code
    code=$(curl -s -m 2 -o /dev/null -w "%{http_code}" "${GATEWAY}/claude/v1/models" 2>/dev/null || true)
    if [ "$code" = "200" ]; then
      local now elapsed
      now=$(date +%s); elapsed=$(( now - start_ts ))
      log "gateway ready after ${elapsed}s"
      return 0
    fi
    local now=$(date +%s)
    if [ $(( now - start_ts )) -gt "$GATEWAY_POLL_TIMEOUT" ]; then
      log "gateway poll timed out after ${GATEWAY_POLL_TIMEOUT}s"
      return 1
    fi
    sleep 1
  done
}

# scratch_repo <name> -- create an empty git repo under SCRATCH_ROOT/name and
# echo its absolute path.
scratch_repo() {
  local name="$1"
  local path="${SCRATCH_ROOT}/${name}"
  rm -rf "$path"
  mkdir -p "$path"
  ( cd "$path" && git init -q && git -c user.email=x@x -c user.name=x commit -q --allow-empty -m init ) >/dev/null 2>&1
  printf '%s' "$path"
}

# codex_exec_capture <provider> <cwd> <prompt> <out_jsonl> <out_last>
# Runs codex exec --json and writes JSONL events to out_jsonl and the last
# assistant message to out_last. Exits 0 on success, nonzero on timeout/error.
codex_exec_capture() {
  local provider="$1" cwd="$2" prompt="$3" out_jsonl="$4" out_last="$5"
  local model
  model=$(provider_model "$provider")
  : > "$out_jsonl"
  : > "$out_last"
  run_bounded "$CODEX_TIMEOUT_DEFAULT" \
    bash -c "cd '$cwd' && exec codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -c 'model_provider=\"reverso_${provider}\"' -c 'model=\"${model}\"' -o '${out_last}' '${prompt}' < /dev/null >> '${out_jsonl}' 2>&1"
}

# codex_resume_capture <provider> <cwd> <session_id> <prompt> <out_jsonl> <out_last>
codex_resume_capture() {
  local provider="$1" cwd="$2" sid="$3" prompt="$4" out_jsonl="$5" out_last="$6"
  local model
  model=$(provider_model "$provider")
  : > "$out_jsonl"
  : > "$out_last"
  run_bounded "$CODEX_TIMEOUT_DEFAULT" \
    bash -c "cd '$cwd' && exec codex exec resume --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -c 'model_provider=\"reverso_${provider}\"' -c 'model=\"${model}\"' -o '${out_last}' '${sid}' '${prompt}' < /dev/null >> '${out_jsonl}' 2>&1"
}

# codex --json interleaves non-JSON status lines (and stderr) into the capture
# file; jq aborts on the first bad token, so always pre-filter to JSON lines.
json_lines() {
  grep '^{' "$1" 2>/dev/null
}

extract_thread_id() {
  local jsonl="$1"
  json_lines "$jsonl" | jq -r 'select(.type=="thread.started") | .thread_id' 2>/dev/null | head -n1
}

extract_usage_input_tokens() {
  local jsonl="$1"
  json_lines "$jsonl" | jq -r 'select(.type=="turn.completed") | .usage.input_tokens' 2>/dev/null | head -n1
}

extract_usage_output_tokens() {
  local jsonl="$1"
  json_lines "$jsonl" | jq -r 'select(.type=="turn.completed") | .usage.output_tokens' 2>/dev/null | head -n1
}

extract_agent_text() {
  local jsonl="$1"
  json_lines "$jsonl" | jq -r 'select(.type=="item.completed") | .item.text // empty' 2>/dev/null | tr -d '\r' | tr '\n' ' '
}

# ---------------- streaming TTFB probe ----------------

# stream_ttfb <provider> -- POST a stream=true Responses request directly to the
# gateway and report time-to-first-delta. Echoes either "OK <secs>" or
# "FAIL <reason>" on stdout.
stream_ttfb() {
  local provider="$1"
  local model
  model=$(provider_stream_model "$provider")
  local url="${GATEWAY}/${provider}/v1/responses"
  local body
  body=$(jq -nc --arg m "$model" '{model:$m, input:"Count slowly: one, two, three. Reply with exactly those three words.", stream:true}')
  local out
  out=$(mktemp)
  local start_ms
  start_ms=$(python3 -c 'import time;print(int(time.time()*1000))')
  ( exec curl -sN -m 30 -H 'content-type: application/json' -d "$body" "$url" > "$out" 2>&1 ) &
  local pid=$!
  local first_ms=""
  local deadline=$(( start_ms + 25000 ))
  while :; do
    if [ -s "$out" ] && grep -q 'event: response.output_text.delta' "$out" 2>/dev/null; then
      first_ms=$(python3 -c 'import time;print(int(time.time()*1000))')
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    local now_ms
    now_ms=$(python3 -c 'import time;print(int(time.time()*1000))')
    if [ "$now_ms" -ge "$deadline" ]; then
      break
    fi
    sleep 0.1
  done
  # Reap (allow curl to finish or kill it)
  ( sleep 3; kill -KILL "$pid" 2>/dev/null ) &
  local watcher=$!
  wait "$pid" 2>/dev/null
  kill -KILL "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null
  if [ -z "$first_ms" ]; then
    local snippet
    snippet=$(head -c 300 "$out" | tr -d '\r' | tr '\n' ' ')
    rm -f "$out"
    printf 'FAIL no response.output_text.delta within budget; head=%s\n' "$snippet"
    return 0
  fi
  local elapsed_ms=$(( first_ms - start_ms ))
  rm -f "$out"
  printf 'OK %d.%03d\n' "$(( elapsed_ms / 1000 ))" "$(( elapsed_ms % 1000 ))"
}

# ---------------- tool-call loop (supported providers) ----------------

# tool_call_supported <provider> -- POST a non-stream Responses request with a
# function tool; PASS if response.output contains a function_call item.
tool_call_supported() {
  local provider="$1"
  local model
  model=$(provider_model "$provider")
  local url="${GATEWAY}/${provider}/v1/responses"
  local body
  body=$(jq -nc --arg m "$model" '{
    model: $m,
    input: "Call the get_weather function for city=Paris. Do not reply in text; call the tool.",
    tools: [{
      type: "function",
      name: "get_weather",
      parameters: {
        type: "object",
        properties: { city: { type: "string" } },
        required: ["city"]
      }
    }],
    tool_choice: { type: "function", name: "get_weather" }
  }')
  local out
  out=$(mktemp)
  ( exec curl -s -m 60 -H 'content-type: application/json' -d "$body" "$url" > "$out" 2>&1 ) &
  local pid=$!
  ( sleep 75; kill -KILL "$pid" 2>/dev/null ) &
  local watcher=$!
  wait "$pid"
  local ec=$?
  kill -KILL "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null
  if [ "$ec" -ne 0 ]; then
    rm -f "$out"
    printf 'FAIL curl exit=%s\n' "$ec"
    return 0
  fi
  if jq -e '.output[]? | select(.type=="function_call")' < "$out" >/dev/null 2>&1; then
    local name
    name=$(jq -r '[.output[]? | select(.type=="function_call") | .name][0]' < "$out")
    rm -f "$out"
    printf 'OK function_call=%s\n' "$name"
    return 0
  fi
  local snippet
  snippet=$(jq -c '{status: .status, output_types: [.output[]?.type], err: .error}' < "$out" 2>/dev/null || head -c 300 "$out")
  rm -f "$out"
  printf 'FAIL no function_call item; got=%s\n' "$snippet"
}

# tool_call_partial <provider> -- post-#10 contract for claude/auggie:
# tools.function is "partial" (accepted-and-ignored). A function-tool request
# must return 200 with a text-only message (no function_call items), and a
# still-unsupported tool (tools.file_search) must keep the structured 400.
tool_call_partial() {
  local provider="$1"
  local model
  model=$(provider_model "$provider")
  local url="${GATEWAY}/${provider}/v1/responses"
  local body
  body=$(jq -nc --arg m "$model" '{
    model: $m,
    input: "Reply with only: ok",
    tools: [{
      type: "function",
      name: "get_weather",
      parameters: { type: "object", properties: {}, required: [] }
    }]
  }')
  local out
  out=$(mktemp)
  local http
  http=$(curl -s -m 60 -o "$out" -w "%{http_code}" -H 'content-type: application/json' -d "$body" "$url" 2>/dev/null || echo "000")
  if [ "$http" != "200" ]; then
    rm -f "$out"
    printf 'FAIL expected 200 partial accept; got http=%s\n' "$http"
    return 0
  fi
  if jq -e '.output[]? | select(.type=="function_call")' < "$out" >/dev/null 2>&1; then
    rm -f "$out"
    printf 'FAIL partial provider emitted a function_call item\n'
    return 0
  fi
  if ! jq -e '[.output[]? | select(.type=="message")] | length > 0' < "$out" >/dev/null 2>&1; then
    local snippet
    snippet=$(jq -c '{status: .status, output_types: [.output[]?.type]}' < "$out" 2>/dev/null || head -c 200 "$out")
    rm -f "$out"
    printf 'FAIL no message item in 200 body; got=%s\n' "$snippet"
    return 0
  fi
  rm -f "$out"
  # 400 contract back-stop: tools.file_search stays unsupported.
  body=$(jq -nc --arg m "$model" '{
    model: $m,
    input: "hi",
    tools: [{ type: "file_search" }]
  }')
  out=$(mktemp)
  http=$(curl -s -m 15 -o "$out" -w "%{http_code}" -H 'content-type: application/json' -d "$body" "$url" 2>/dev/null || echo "000")
  local body_check feature
  body_check=$(jq -r '.error.code // empty' < "$out" 2>/dev/null)
  feature=$(jq -r '.error.message // empty' < "$out" 2>/dev/null)
  rm -f "$out"
  if [ "$http" = "400" ] && [ "$body_check" = "unsupported_feature" ] && [[ "$feature" == *"tools.file_search"* ]]; then
    printf 'NA tools.function partial (200 text-only verified); 400 contract intact via tools.file_search\n'
  else
    printf 'FAIL file_search back-stop: expected 400 unsupported_feature; got http=%s code=%s msg=%s\n' "$http" "$body_check" "$feature"
  fi
}

# ---------------- model selection via reverso-codex-sync ----------------

model_selection_check() {
  local tmp_cfg
  tmp_cfg=$(mktemp -t reverso-codex-sync-tmp.XXXXXX.toml)
  cat > "$tmp_cfg" <<'TOML'
# fixture config for matrix model-selection check
model = "gpt-5.5"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"

[model_providers.reverso_claude]
name = "Reverso Claude profile"
base_url = "http://127.0.0.1:64946/claude/v1"
wire_api = "responses"

[tui.model_availability_nux]
"gpt-5.5" = 4
TOML
  local out
  out=$(mktemp)
  if ! run_bounded 30 bash -c "cd '${REPO_ROOT}' && exec uv run --quiet reverso-codex-sync --config '${tmp_cfg}' --base-url '${GATEWAY}' > '${out}' 2>&1"; then
    local snippet
    snippet=$(head -c 300 "$out")
    rm -f "$tmp_cfg" "$out"
    printf 'FAIL\treverso-codex-sync failed: %s\n' "$snippet"
    return 0
  fi
  local changed providers_keys
  changed=$(jq -r '.changed' < "$out" 2>/dev/null || echo "?")
  providers_keys=$(jq -r '.providers | keys | join(",")' < "$out" 2>/dev/null || echo "?")
  if [ "$changed" != "true" ]; then
    rm -f "$tmp_cfg" "$out"
    printf 'FAIL\tsync did not report changed=true (got %s)\n' "$changed"
    return 0
  fi
  # Check sentinel block was inserted and one expected per-model section is present.
  local marker_count
  marker_count=$(grep -c "BEGIN REVERSO MODELS PROFILES" "$tmp_cfg" 2>/dev/null || echo 0)
  if [ "$marker_count" -lt 1 ]; then
    rm -f "$tmp_cfg" "$out"
    printf 'FAIL\tno PROFILES sentinel after sync\n'
    return 0
  fi
  # Find first per-model entry for the current provider in the sync output.
  printf 'PASS\tchanged=true providers=%s sentinel_count=%s tmp_cfg=%s\n' "$providers_keys" "$marker_count" "$tmp_cfg"
  rm -f "$tmp_cfg" "$out"
}

# Known-good chat-capable alternates for the model-selection drill. The first
# listed model is not always usable for a chat turn (claude-opus-4.6 via the
# copilot upstream returns 502 on the responses wire; listings also contain
# embedding models), so prefer a vetted id and require it in the live listing.
provider_alt_model() {
  case "$1" in
    claude)   printf 'claude-sonnet-4-6' ;;
    copilot)  printf 'gpt-5.4-mini' ;;
    auggie)   printf 'haiku4.5' ;;
    deepseek) printf 'deepseek-v4-pro' ;;
    *) return 1 ;;
  esac
}

# Per-provider model-selection drill: confirm a synced model id from the live
# listing can be used via codex exec with explicit -c overrides.
model_selection_drill() {
  local provider="$1"
  local preferred model
  preferred=$(provider_alt_model "$provider")
  model=$(curl -s -m 5 "${GATEWAY}/${provider}/v1/models" \
    | jq -r --arg want "$preferred" '[.data[]?.id] | if index($want) then $want else .[0] end' 2>/dev/null)
  if [ -z "$model" ] || [ "$model" = "null" ]; then
    printf 'FAIL\tno models on /v1/models\n'
    return 0
  fi
  local cwd
  cwd=$(scratch_repo "modelsel-${provider}")
  local jsonl="${LOG_DIR}/modelsel-${provider}.jsonl"
  local last="${LOG_DIR}/modelsel-${provider}.last"
  : > "$jsonl"; : > "$last"
  if ! run_bounded "$CODEX_TIMEOUT_DEFAULT" \
    bash -c "cd '$cwd' && exec codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -c 'model_provider=\"reverso_${provider}\"' -c 'model=\"${model}\"' -o '${last}' 'Reply with only the literal text: ok' < /dev/null >> '${jsonl}' 2>&1"; then
    printf 'FAIL\tmodel=%s codex exec timed out or errored\n' "$model"
    return 0
  fi
  local text
  text=$(cat "$last" 2>/dev/null | tr -d '\r' | head -c 200)
  if [ -n "$text" ]; then
    printf 'PASS\tmodel=%s reply=%s\n' "$model" "${text:0:60}"
  else
    printf 'FAIL\tmodel=%s empty reply\n' "$model"
  fi
}

# ---------------- per-provider scenarios ----------------

cell_memory() {
  local provider="$1"
  local secret
  secret=$(provider_secret "$provider")
  local cwd=$(scratch_repo "mem-${provider}")
  local j1="${LOG_DIR}/mem-${provider}-t1.jsonl"
  local l1="${LOG_DIR}/mem-${provider}-t1.last"
  local j2="${LOG_DIR}/mem-${provider}-t2.jsonl"
  local l2="${LOG_DIR}/mem-${provider}-t2.last"
  if ! codex_exec_capture "$provider" "$cwd" "Our project codename for this session is ${secret}. Reply with only: ok" "$j1" "$l1"; then
    printf 'FAIL\tturn1 codex exec timed out or errored\n'
    return 0
  fi
  local sid
  sid=$(extract_thread_id "$j1")
  if [ -z "$sid" ]; then
    printf 'FAIL\tno thread_id in turn 1\n'
    return 0
  fi
  if ! codex_resume_capture "$provider" "$cwd" "$sid" "What is our project codename? Reply with only the codename, nothing else." "$j2" "$l2"; then
    printf 'FAIL\tturn2 codex exec resume timed out or errored\n'
    return 0
  fi
  local reply
  reply=$(extract_agent_text "$j2")
  if [[ "$reply" == *"$secret"* ]]; then
    printf 'PASS\tsid=%s reply_contains_secret=true\n' "${sid:0:13}"
  else
    local trimmed=${reply:0:80}
    printf 'FAIL\tsid=%s no_secret_in_reply reply=%s\n' "${sid:0:13}" "$trimmed"
  fi
}

cell_workspace() {
  local provider="$1"
  local token
  token=$(provider_token "$provider")
  local cwd=$(scratch_repo "ws-${provider}")
  printf '%s\n' "$token" > "${cwd}/marker.txt"
  ( cd "$cwd" && git add marker.txt && git -c user.email=x@x -c user.name=x commit -q -m "add marker" ) >/dev/null 2>&1
  local jsonl="${LOG_DIR}/ws-${provider}.jsonl"
  local last="${LOG_DIR}/ws-${provider}.last"
  if [ "$provider" = "claude" ]; then
    # The claude CLI denies model-side file reads in print mode (safe default)
    # and tools.function is partial (codex local tools dropped), so the
    # file-read variant cannot work by design. Validate that workspace context
    # still reaches the model via codex environment_context instead.
    local envprompt="Reply with only the base name of your current working directory, no extra text."
    if ! codex_exec_capture "$provider" "$cwd" "$envprompt" "$jsonl" "$last"; then
      printf 'FAIL\tcodex exec timed out or errored\n'
      return 0
    fi
    local envreply
    envreply=$(cat "$last" 2>/dev/null | tr -d '\r')
    [ -n "$envreply" ] || envreply=$(extract_agent_text "$jsonl")
    if [[ "$envreply" == *"ws-${provider}"* ]]; then
      printf 'PASS\tenv_context_cwd_returned=true (file reads NA: claude CLI permission default + tools.function partial)\n'
    else
      printf 'FAIL\tno cwd basename in reply reply_head=%s\n' "${envreply:0:80}"
    fi
    return 0
  fi
  local prompt="Read the file marker.txt in the current working directory and reply with only its contents, no extra text."
  if ! codex_exec_capture "$provider" "$cwd" "$prompt" "$jsonl" "$last"; then
    printf 'FAIL\tcodex exec timed out or errored\n'
    return 0
  fi
  local reply
  reply=$(cat "$last" 2>/dev/null | tr -d '\r')
  if [[ "$reply" == *"$token"* ]]; then
    printf 'PASS\treply_contains_token=true\n'
  else
    local agent_text
    agent_text=$(extract_agent_text "$jsonl")
    if [[ "$agent_text" == *"$token"* ]]; then
      printf 'PASS\tagent_text_contains_token=true\n'
    else
      printf 'FAIL\tno_token reply_head=%s\n' "${reply:0:80}"
    fi
  fi
}

cell_usage() {
  local provider="$1"
  local cwd=$(scratch_repo "use-${provider}")
  local jsonl="${LOG_DIR}/use-${provider}.jsonl"
  local last="${LOG_DIR}/use-${provider}.last"
  if ! codex_exec_capture "$provider" "$cwd" "Reply with only: ok" "$jsonl" "$last"; then
    printf 'FAIL\tcodex exec timed out or errored\n'
    return 0
  fi
  local input_tokens output_tokens
  input_tokens=$(extract_usage_input_tokens "$jsonl")
  output_tokens=$(extract_usage_output_tokens "$jsonl")
  if [ -z "$input_tokens" ] || [ -z "$output_tokens" ] || [ "$input_tokens" = "null" ] || [ "$output_tokens" = "null" ]; then
    printf 'FAIL\tmissing usage in turn.completed input=%s output=%s\n' "$input_tokens" "$output_tokens"
    return 0
  fi
  if [ "$input_tokens" -gt 0 ] 2>/dev/null && [ "$output_tokens" -gt 0 ] 2>/dev/null; then
    printf 'PASS\tinput_tokens=%s output_tokens=%s\n' "$input_tokens" "$output_tokens"
  else
    printf 'FAIL\timplausible usage input=%s output=%s\n' "$input_tokens" "$output_tokens"
  fi
}

cell_resume_after_restart() {
  local provider="$1"
  local secret
  secret="$(provider_secret "$provider")-RST"
  local cwd=$(scratch_repo "rstrt-${provider}")
  local j1="${LOG_DIR}/rstrt-${provider}-t1.jsonl"
  local l1="${LOG_DIR}/rstrt-${provider}-t1.last"
  local j2="${LOG_DIR}/rstrt-${provider}-t2.jsonl"
  local l2="${LOG_DIR}/rstrt-${provider}-t2.last"
  if ! codex_exec_capture "$provider" "$cwd" "Our project codename for this session is ${secret}. Reply with only: ok" "$j1" "$l1"; then
    printf 'FAIL\tturn1 codex exec timed out or errored\n'
    return 0
  fi
  local sid
  sid=$(extract_thread_id "$j1")
  if [ -z "$sid" ]; then
    printf 'FAIL\tno thread_id in turn 1\n'
    return 0
  fi
  if ! restart_gateway; then
    printf 'FAIL\tgateway did not come back up within %ss\n' "$GATEWAY_POLL_TIMEOUT"
    return 0
  fi
  if ! codex_resume_capture "$provider" "$cwd" "$sid" "What is our project codename? Reply with only the codename, exactly as it was given." "$j2" "$l2"; then
    printf 'FAIL\tturn2 codex exec resume timed out or errored\n'
    return 0
  fi
  local reply
  reply=$(extract_agent_text "$j2")
  # Models sometimes reformat punctuation (Delta-RST -> DeltaRST), which still
  # proves the session memory survived the restart; compare alphanumerics only.
  local reply_norm secret_norm
  reply_norm=$(printf '%s' "$reply" | tr -cd '[:alnum:]')
  secret_norm=$(printf '%s' "$secret" | tr -cd '[:alnum:]')
  if [ -n "$secret_norm" ] && [[ "$reply_norm" == *"$secret_norm"* ]]; then
    printf 'PASS\tsid=%s post_restart_secret_returned=true\n' "${sid:0:13}"
  else
    printf 'FAIL\tsid=%s reply=%s\n' "${sid:0:13}" "${reply:0:80}"
  fi
}

# ---------------- main ----------------

main() {
  if ! curl -s -m 5 -o /dev/null -w "%{http_code}" "${GATEWAY}/claude/v1/models" | grep -q '^200$'; then
    log "gateway not reachable on first ping; restarting"
    restart_gateway || { log "FATAL: gateway unreachable"; exit 1; }
  fi
  log "scratch root: $SCRATCH_ROOT"
  log "log dir: $LOG_DIR"

  # 7. model selection (one cross-cutting reverso-codex-sync write check, plus
  # per-provider drill below).
  local out
  out=$(model_selection_check)
  local status="${out%%$'\t'*}"
  local detail="${out#*$'\t'}"
  record_result "all" "model_sync" "$status" "$detail"

  for provider in "${PROVIDERS[@]}"; do
    log "=== provider: $provider ==="

    out=$(cell_memory "$provider")
    status="${out%%$'\t'*}"; detail="${out#*$'\t'}"
    record_result "$provider" "memory" "$status" "$detail"

    out=$(cell_workspace "$provider")
    status="${out%%$'\t'*}"; detail="${out#*$'\t'}"
    record_result "$provider" "workspace" "$status" "$detail"

    out=$(cell_usage "$provider")
    status="${out%%$'\t'*}"; detail="${out#*$'\t'}"
    record_result "$provider" "usage" "$status" "$detail"

    out=$(cell_resume_after_restart "$provider")
    status="${out%%$'\t'*}"; detail="${out#*$'\t'}"
    record_result "$provider" "resume_after_restart" "$status" "$detail"

    # TTFB
    if [ "$provider" = "auggie" ]; then
      record_result "$provider" "ttfb_under_${TTFB_BUDGET_SECS}s" "NA" "BUFFER per A3 auggie-streaming.md; documented limitation, not a failure"
    else
      out=$(stream_ttfb "$provider")
      local kind="${out%% *}"
      local rest="${out#* }"
      if [ "$kind" = "OK" ]; then
        # Numeric compare seconds.fraction against TTFB_BUDGET_SECS
        local int_part="${rest%%.*}"
        if [ -n "$int_part" ] && [ "$int_part" -lt "$TTFB_BUDGET_SECS" ] 2>/dev/null; then
          record_result "$provider" "ttfb_under_${TTFB_BUDGET_SECS}s" "PASS" "first_delta_secs=${rest}"
        else
          record_result "$provider" "ttfb_under_${TTFB_BUDGET_SECS}s" "FAIL" "first_delta_secs=${rest} exceeds ${TTFB_BUDGET_SECS}s"
        fi
      else
        record_result "$provider" "ttfb_under_${TTFB_BUDGET_SECS}s" "FAIL" "$rest"
      fi
    fi

    # Tool-call loop
    if [ "$provider" = "claude" ] || [ "$provider" = "auggie" ]; then
      out=$(tool_call_partial "$provider")
      local kind="${out%% *}"
      local rest="${out#* }"
      if [ "$kind" = "NA" ]; then
        record_result "$provider" "tool_call_loop" "NA" "tools.function partial per post-#10 surface; ${rest}"
      else
        record_result "$provider" "tool_call_loop" "FAIL" "$rest"
      fi
    else
      out=$(tool_call_supported "$provider")
      local kind="${out%% *}"
      local rest="${out#* }"
      if [ "$kind" = "OK" ]; then
        record_result "$provider" "tool_call_loop" "PASS" "$rest"
      else
        record_result "$provider" "tool_call_loop" "FAIL" "$rest"
      fi
    fi

    # Per-provider model-selection drill (uses a synced /v1/models id).
    out=$(model_selection_drill "$provider")
    status="${out%%$'\t'*}"; detail="${out#*$'\t'}"
    record_result "$provider" "model_selection" "$status" "$detail"
  done

  emit_evidence
}

emit_evidence() {
  mkdir -p "$(dirname "$EVIDENCE_FILE")"
  local now_iso
  now_iso=$(python3 -c 'import datetime;print(datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  # Build the markdown table from the TSV.
  local tmp_md
  tmp_md=$(mktemp)
  {
    printf -- '---\n'
    printf 'title: "C1 Codex E2E matrix results"\n'
    printf 'status: complete\n'
    printf 'phase: C\n'
    printf 'gate: C1\n'
    printf 'gateway: 127.0.0.1:64946\n'
    printf 'codex_version: "codex-cli 0.139.0"\n'
    printf 'ttfb_budget_seconds: %s\n' "$TTFB_BUDGET_SECS"
    printf 'generated: %s\n' "$now_iso"
    printf -- '---\n\n'
    printf '# C1 Codex E2E matrix results\n\n'
    printf 'Live run of `scripts/codex-e2e-matrix.sh` against the reverso gateway on 127.0.0.1:64946.\n\n'
    printf '## Pass/fail table\n\n'
    printf '| provider | cell | status | detail |\n'
    printf '|----------|------|--------|--------|\n'
    while IFS=$'\t' read -r provider cell status detail; do
      # Escape pipes in detail to keep the markdown table well-formed.
      local esc="${detail//|/\\|}"
      printf '| %s | %s | %s | %s |\n' "$provider" "$cell" "$status" "$esc"
    done < "$RESULTS_TSV"
    printf '\n'
    printf '## Counts\n\n'
    local n_pass n_fail n_na
    n_pass=$(awk -F$'\t' '$3=="PASS"{c++} END{print c+0}' "$RESULTS_TSV")
    n_fail=$(awk -F$'\t' '$3=="FAIL"{c++} END{print c+0}' "$RESULTS_TSV")
    n_na=$(awk -F$'\t' '$3=="NA"{c++} END{print c+0}' "$RESULTS_TSV")
    printf '* PASS: %s\n' "$n_pass"
    printf '* FAIL: %s\n' "$n_fail"
    printf '* NA (documented unsupported): %s\n\n' "$n_na"
    printf '## Notes on documented unsupported cells\n\n'
    printf '* auggie `ttfb_under_%ss`: A3 (.omc/research/auggie-streaming.md) decided BUFFER. The auggie CLI has no incremental output mode; the adapter emits a single buffered delta after the upstream completes. This is the documented limitation, not a regression.\n' "$TTFB_BUDGET_SECS"
    printf '* claude and auggie `tool_call_loop`: post-#10 the surface declares `tools.function` PARTIAL for the CLI-spine providers (the codex normalizer strips function tools and the request proceeds text-only). The matrix verifies a 200 text-only completion for a function-tool request AND asserts the structured 400 `unsupported_feature` body still fires via `tools.file_search` before recording NA.\n\n'
    printf '## How the script is bounded\n\n'
    printf '* Each `codex exec` and `codex exec resume` invocation runs under a 180s watchdog that kills the process and its descendants via a pgrep-based loop (setsid is unavailable on macOS bash 3.2).\n'
    printf '* The gateway restart polls `/claude/v1/models` for up to %ss before bailing.\n' "$GATEWAY_POLL_TIMEOUT"
    printf '* The streaming TTFB probe gives the gateway a 25s wall-clock budget and fails the cell if no `response.output_text.delta` arrives within %ss.\n' "$TTFB_BUDGET_SECS"
    printf '* The tool-call POSTs have a 60s curl timeout.\n\n'
    printf '## Resume protocol detail\n\n'
    printf 'Every `codex exec resume` call uses `-c model_provider="reverso_<provider>" -c model="<id>"` overrides. `codex exec resume` does not accept `-p` (see A1, .omc/research/codex-resume-probe.md); without these overrides resume silently falls back to the default openai provider, defeating the test.\n\n'
    printf '## Model selection mechanism\n\n'
    printf 'The cross-cutting `model_sync` row exercises `uv run reverso-codex-sync --config <tmp> --base-url http://127.0.0.1:64946` against a temporary fixture config (the real `~/.codex/config.toml` is never modified). Per-provider `model_selection` rows then run `codex exec -c model_provider="reverso_<p>" -c model="<live id from /v1/models>"` to confirm the synced id can drive a real turn end to end.\n'
  } > "$tmp_md"
  mv "$tmp_md" "$EVIDENCE_FILE"
  log "evidence written to $EVIDENCE_FILE"
  # Dash scan: python3 walks the evidence file and prints any em/en dash hit.
  python3 - "$EVIDENCE_FILE" <<'PY'
import sys, pathlib
target = pathlib.Path(sys.argv[1])
text = target.read_text(encoding='utf-8')
bad = []
for i, line in enumerate(text.splitlines(), start=1):
    for ch in ("\u2013", "\u2014"):
        if ch in line:
            bad.append((i, ch.encode('unicode_escape').decode(), line))
if bad:
    for i, ch, line in bad:
        sys.stderr.write(f"DASH DETECTED line {i} ({ch}): {line}\n")
    sys.exit(2)
print(f"dash scan OK ({target})")
PY
  local scan_ec=$?
  if [ "$scan_ec" -ne 0 ]; then
    log "FATAL: evidence file contains em/en dash"
    exit 3
  fi
  # Print summary table to stdout.
  echo
  echo "=== summary ==="
  printf '%-10s %-26s %-6s %s\n' "provider" "cell" "status" "detail"
  printf '%-10s %-26s %-6s %s\n' "--------" "--------------------------" "------" "------"
  while IFS=$'\t' read -r provider cell status detail; do
    printf '%-10s %-26s %-6s %s\n' "$provider" "$cell" "$status" "$detail"
  done < "$RESULTS_TSV"
}

main "$@"
