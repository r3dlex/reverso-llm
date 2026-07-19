---
title: Kimi provider release and rollback runbook
status: active
date: 2026-07-19
related:
  - docs/specifications/ACTIVE/kimi-subscription-provider.md
  - docs/architecture/adr/0017-kimi-code-oauth-provider.md
  - .ai/handoff/northstar-kimi-subscription-provider.md
---

# Kimi Provider Release and Rollback Runbook

Use this runbook for a local Kimi release candidate after the offline suites and
the credentialed proof gate are green. Reverso must remain bound only to
`http://127.0.0.1:64946` throughout the procedure.

## Preconditions

1. Run `kimi /login` and confirm the OAuth artifact exists without printing it.
2. Confirm the candidate checkout is clean and record `git rev-parse HEAD`.
3. Confirm `REVERSO_HOST` is unset or exactly `127.0.0.1`.
4. Back up the two generated LaunchAgent files before installing the candidate:

   ```bash
   backup_dir="$(mktemp -d)"
   for name in com.user.reverso-proxy.plist com.user.reverso-daemon.plist; do
     if [[ -f "$HOME/Library/LaunchAgents/$name" ]]; then
       cp -p "$HOME/Library/LaunchAgents/$name" "$backup_dir/$name"
     fi
   done
   restore_launchagents() {
     for name in com.user.reverso-proxy.plist com.user.reverso-daemon.plist; do
       current="$HOME/Library/LaunchAgents/$name"
       launchctl unload "$current" 2>/dev/null || true
       if [[ -f "$backup_dir/$name" ]]; then
         cp -p "$backup_dir/$name" "$current"
         launchctl load "$current"
       else
         rm -f "$current"
       fi
     done
   }
   trap restore_launchagents EXIT
   ```

The backup contains local paths but no Kimi token. Keep it outside the
repository and remove it after rollback proof.

## Install and restart

Install the candidate LaunchAgents from the candidate checkout:

```bash
uv sync --frozen
./scripts/install-launchagents.sh
launchctl kickstart -k "gui/$(id -u)/com.user.reverso-proxy"
curl -fsS http://127.0.0.1:64946/health/readiness
```

Validate the generated proxy plist before smoke testing:

```bash
plutil -lint "$HOME/Library/LaunchAgents/com.user.reverso-proxy.plist"
/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:REVERSO_HOST' \
  "$HOME/Library/LaunchAgents/com.user.reverso-proxy.plist"
/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:REVERSO_PORT' \
  "$HOME/Library/LaunchAgents/com.user.reverso-proxy.plist"
```

The values must be `127.0.0.1` and `64946`.

## Smoke and release evidence

First preview Codex synchronization so the check does not write operator state:

```bash
uv run reverso-codex-sync --dry-run
```

Generate the Kimi Codex profile in a private temporary Codex home so the proof
does not alter the operator's installed profile. Use the authenticated
`.codex-reverso` home as the source because it is the validated Reverso client
home. Never print or commit the copied authentication file.

```bash
temp_codex_home="$(mktemp -d)"
cleanup_codex_home() { rm -rf "$temp_codex_home"; }
trap cleanup_codex_home EXIT
chmod 700 "$temp_codex_home"
cp -p "$HOME/.codex-reverso/config.toml" "$temp_codex_home/config.toml"
cp -p "$HOME/.codex-reverso/auth.json" "$temp_codex_home/auth.json"
chmod 600 "$temp_codex_home/config.toml" "$temp_codex_home/auth.json"
REVERSO_CODEX_CONFIG="$temp_codex_home/config.toml" \
  REVERSO_CODEX_CATALOG_DIR="$temp_codex_home/reverso" \
  .venv/bin/python -m reverso.codex_sync >/dev/null
```

Then run the fail-closed trusted-machine proof against that temporary profile.
It exercises live model discovery, Responses continuity, Messages, Codex,
`scripts/claude-kimi.sh`, Headroom, redaction, and loopback routing:

```bash
REVERSO_KIMI_LIVE_PROOF=1 \
  REVERSO_CODEX_CONFIG="$temp_codex_home/config.toml" \
  .venv/bin/python scripts/kimi-live-proof.py \
  --manifest .omx/evidence/kimi-live-proof.json
cleanup_codex_home
trap - EXIT
```

A release candidate is eligible for review only when the manifest is mode
`0600`, has `overall` equal to `pass`, has `loopback` equal to `true`, and has
exactly eight passing checks. The manifest must not contain bearer values,
credential fields, prompts, response content, request headers, or raw logs.
Do not commit the local manifest.

## Rollback

Rollback restores the exact LaunchAgent files captured before the candidate was
installed. It does not delete or modify the Kimi CLI OAuth artifact, normal
Claude authentication, or built-in Codex models.

```bash
restore_launchagents
trap - EXIT
rm -rf "$backup_dir"
```

After restoration, prove the prior runtime is healthy and its generated plist
no longer points at the candidate checkout:

```bash
curl -fsS http://127.0.0.1:64946/health/readiness
/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' \
  "$HOME/Library/LaunchAgents/com.user.reverso-proxy.plist"
```

For client-only rollback, stop the Kimi Codex profile and run normal Codex with
a built-in GPT model. Exit the provider-pinned Claude Code process and run
normal `claude`; `scripts/claude-kimi.sh` never writes global Claude settings.
