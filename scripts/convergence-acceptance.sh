#!/usr/bin/env bash
# Run the complete client convergence acceptance matrix in an isolated home.

set -euo pipefail

REVERSO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "$(git -C "${REVERSO_DIR}" status --porcelain --untracked-files=all)" ]]; then
    echo "convergence-acceptance: checkout must be clean" >&2
    exit 2
fi
if ! git -C "${REVERSO_DIR}" rev-parse --verify \
    refs/remotes/origin/main >/dev/null 2>&1; then
    echo "convergence-acceptance: trusted origin/main is unavailable" >&2
    exit 2
fi
HEAD_COMMIT="$(git -C "${REVERSO_DIR}" rev-parse HEAD)"
BASE_COMMIT="$(
    git -C "${REVERSO_DIR}" merge-base HEAD refs/remotes/origin/main
)"
if [[ ! "${HEAD_COMMIT}" =~ ^[0-9a-f]{40}$ || ! "${BASE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "convergence-acceptance: invalid Git revision" >&2
    exit 2
fi
if ! git -C "${REVERSO_DIR}" diff --quiet \
    "${BASE_COMMIT}" HEAD -- src/reverso/protocols/adapter.py; then
    echo "convergence-acceptance: frozen adapter changed" >&2
    exit 2
fi

UV_BIN="${REVERSO_UV_BIN:-$(command -v uv 2>/dev/null || true)}"
RTK_BIN="${REVERSO_ACCEPTANCE_RTK_BIN:-$(command -v rtk 2>/dev/null || true)}"
CODEX_BIN="$(
    command -v "${REVERSO_ACCEPTANCE_CODEX_BIN:-codex}" 2>/dev/null || true
)"
if [[ -z "${UV_BIN}" || ! -x "${UV_BIN}" ]]; then
    echo "convergence-acceptance: uv is unavailable" >&2
    exit 2
fi
if [[ -z "${RTK_BIN}" || ! -f "${RTK_BIN}" || ! -x "${RTK_BIN}" ]]; then
    echo "convergence-acceptance: one executable RTK is required" >&2
    exit 2
fi
if [[ -z "${CODEX_BIN}" || ! -x "${CODEX_BIN}" ]]; then
    echo "convergence-acceptance: Codex is unavailable" >&2
    exit 2
fi

HOST_HOME="${HOME}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${HOST_HOME}/Library/Caches/uv}"
ACCEPTANCE_ROOT="$(mktemp -d "${HOST_HOME}/.reverso-convergence.XXXXXX")"
trap 'rm -rf "${ACCEPTANCE_ROOT}"' EXIT
ACCEPTANCE_HOME="${ACCEPTANCE_ROOT}/home"
RESULTS_DIR="${ACCEPTANCE_ROOT}/results"
mkdir -p "${ACCEPTANCE_HOME}" "${RESULTS_DIR}"

CODEX_CONFIG="${ACCEPTANCE_HOME}/.codex/config.toml"
CLAUDE_CONFIG_DIR="${ACCEPTANCE_HOME}/.claude"
CATALOG_DIR="${ACCEPTANCE_HOME}/.codex/reverso"
LAUNCHER_DIR="${ACCEPTANCE_HOME}/.local/bin"

emit_sanitized_diagnostic() {
    local diagnostic_file="$1"
    local line
    local lowered
    tail -n 20 "${diagnostic_file}" | while IFS= read -r line; do
        lowered="$(printf '%s' "${line}" | tr '[:upper:]' '[:lower:]')"
        if [[ "${lowered}" =~ (authorization|api[[:space:]_-]*key|password|secret|credential)([[:space:]]|[:=]|$) ]] \
            || [[ "${lowered}" =~ (^|[[:space:]])bearer([[:space:]]|$) ]] \
            || [[ "${lowered}" =~ (^|[^[:alnum:]_])token([[:space:]]*[:=]|[[:space:]]+is([[:space:]]|$)) ]] \
            || [[ "${lowered}" =~ ://[^[:space:]/]+@ ]]; then
            printf '%s\n' "[redacted sensitive child diagnostic]" >&2
            continue
        fi
        line="${line//${ACCEPTANCE_ROOT}/<acceptance-root>}"
        printf '  %.500s\n' "${line}" >&2
    done
}

run_sync() {
    local mode="$1"
    local output="$2"
    local diagnostic="${RESULTS_DIR}/${mode}.stderr"
    if ! HOME="${ACCEPTANCE_HOME}" "${UV_BIN}" run --project "${REVERSO_DIR}" \
        reverso-client-sync "${mode}" \
        --codex-config "${CODEX_CONFIG}" \
        --claude-config-dir "${CLAUDE_CONFIG_DIR}" \
        --catalog-dir "${CATALOG_DIR}" \
        --launch-agent-dir "${LAUNCHER_DIR}" \
        --rtk-bin "${RTK_BIN}" \
        --json >"${output}" 2>"${diagnostic}"; then
        echo "convergence-acceptance: ${mode} failed" >&2
        emit_sanitized_diagnostic "${diagnostic}"
        exit 2
    fi
    rm -f "${diagnostic}"
}

state_digest() {
    HOME="${ACCEPTANCE_HOME}" "${UV_BIN}" run --project "${REVERSO_DIR}" \
        python - "${ACCEPTANCE_HOME}" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
    relative = str(path.relative_to(root)).encode()
    mode = path.lstat().st_mode
    digest.update(relative)
    digest.update(str(stat.S_IMODE(mode)).encode())
    if path.is_symlink():
        digest.update(b"L")
        digest.update(os.readlink(path).encode())
    elif path.is_file():
        digest.update(b"F")
        digest.update(path.read_bytes())
    elif path.is_dir():
        digest.update(b"D")
print(digest.hexdigest())
PY
}

DRY_RUN_JSON="${RESULTS_DIR}/dry-run.json"
FIRST_APPLY_JSON="${RESULTS_DIR}/first-apply.json"
SECOND_APPLY_JSON="${RESULTS_DIR}/second-apply.json"
REFRESH_JSON="${RESULTS_DIR}/refresh.json"
VERIFY_JSON="${RESULTS_DIR}/verify.json"

run_sync dry-run "${DRY_RUN_JSON}"
run_sync apply "${FIRST_APPLY_JSON}"
FIRST_DIGEST="$(state_digest)"
run_sync apply "${SECOND_APPLY_JSON}"
SECOND_DIGEST="$(state_digest)"
if [[ "${FIRST_DIGEST}" != "${SECOND_DIGEST}" ]]; then
    echo "convergence-acceptance: second apply changed isolated state" >&2
    exit 2
fi
run_sync refresh "${REFRESH_JSON}"
run_sync verify "${VERIFY_JSON}"

VALIDATION_JSON="${RESULTS_DIR}/validation.json"
VALIDATION_STDERR="${RESULTS_DIR}/validation.stderr"
if ! HOME="${ACCEPTANCE_HOME}" "${UV_BIN}" run --project "${REVERSO_DIR}" \
    python - \
    "${REVERSO_DIR}" \
    "${ACCEPTANCE_HOME}" \
    "${RESULTS_DIR}" \
    "${RTK_BIN}" \
    "${CODEX_BIN}" \
    "${HEAD_COMMIT}" \
    "${DRY_RUN_JSON}" \
    "${FIRST_APPLY_JSON}" \
    "${SECOND_APPLY_JSON}" \
    "${REFRESH_JSON}" \
    "${VERIFY_JSON}" >"${VALIDATION_JSON}" 2>"${VALIDATION_STDERR}" <<'PY'
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from reverso.deployment_drift import (
    HEADROOM_USAGE_URL,
    SCHEDULED_LAUNCH_AGENT_LABEL,
    SCHEDULED_START_CALENDAR_INTERVAL,
    validate_headroom_usage_payload,
)
from reverso.protocols.headroom_compression import HeadroomCompressionConfig

try:
    root = Path(sys.argv[1])
    home = Path(sys.argv[2])
    results_dir = Path(sys.argv[3])
    rtk = Path(sys.argv[4]).resolve(strict=True)
    codex = Path(sys.argv[5]).resolve(strict=True)
    head_commit = sys.argv[6]
    results = [
        json.loads(Path(path).read_text(encoding="utf-8")) for path in sys.argv[7:]
    ]
    manifest = json.loads(
        (root / "config/supported-client-surfaces.json").read_text(encoding="utf-8")
    )
    expected_surfaces = {item["id"] for item in manifest["surfaces"]}
    if len(expected_surfaces) != 17:
        raise ValueError("surface count")
    for result in results:
        if {item["id"] for item in result["surfaces"]} != expected_surfaces:
            raise ValueError("surface inventory")
    if results[0]["status"] not in {"planned", "no_op"}:
        raise ValueError("dry run")
    if results[1]["status"] not in {"success", "no_op"}:
        raise ValueError("first apply")
    if results[2]["status"] != "no_op":
        raise ValueError("second apply")
    if results[3]["status"] not in {"success", "no_op"}:
        raise ValueError("refresh")
    if results[4]["status"] != "success" or results[4]["catalog_refresh"]["stale"]:
        raise ValueError("verify")
    refresh_fields = {
        "last_attempt_at",
        "last_success_at",
        "stored_stale",
        "stored_stale_observed_at",
        "stale",
        "observed_at",
    }
    for result in results[3:]:
        refresh = result["catalog_refresh"]
        if set(refresh) != refresh_fields:
            raise ValueError("refresh status fields")
        if refresh["last_attempt_at"] is None or refresh["last_success_at"] is None:
            raise ValueError("refresh status timestamps")
    if (home / ".headroom/bin/rtk").resolve(strict=True) != rtk:
        raise ValueError("rtk")

    codex_profile_count = 0
    for surface in manifest["surfaces"]:
        if surface["kind"] not in {"reverso_route", "feature_gated_route"}:
            continue
        profile = Path(
            surface["path_template"].replace(
                "<codex_config_dir>", str(home / ".codex")
            )
        )
        if not profile.exists():
            if surface["kind"] == "reverso_route":
                raise ValueError("codex profile")
            continue
        codex_home = results_dir / "client-smoke" / surface["id"]
        codex_home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(home / ".codex/config.toml", codex_home / "config.toml")
        profile_config = tomllib.loads(profile.read_text(encoding="utf-8"))
        override_keys = (
            "model",
            "model_provider",
            "model_catalog_json",
            "model_context_window",
            "model_auto_compact_token_limit",
            "model_reasoning_summary",
        )
        overrides = [
            argument
            for key in override_keys
            if key in profile_config
            for argument in ("-c", f"{key}={json.dumps(profile_config[key])}")
        ]
        completed = subprocess.run(
            [str(codex), "debug", "models", *overrides],
            cwd=root,
            env={**os.environ, "CODEX_HOME": str(codex_home), "HOME": str(home)},
            capture_output=True,
            check=False,
            timeout=5,
        )
        if completed.returncode != 0:
            raise ValueError("codex profile execution")
        catalog = json.loads(completed.stdout)
        if profile_config["model"] not in {
            model["slug"] for model in catalog["models"]
        }:
            raise ValueError("codex profile catalog")
        codex_profile_count += 1

    claude_launcher_count = 0
    for launcher in manifest["claude_launchers"]:
        completed = subprocess.run(
            [str(home / ".local/bin" / launcher), "--version"],
            cwd=root,
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            check=False,
            timeout=5,
        )
        if completed.returncode != 0:
            raise ValueError("claude launcher execution")
        claude_launcher_count += 1

    scheduled = plistlib.loads(
        (root / f"launchd/{SCHEDULED_LAUNCH_AGENT_LABEL}.plist.tmpl").read_bytes()
    )
    if scheduled.get("StartCalendarInterval") != SCHEDULED_START_CALENDAR_INTERVAL:
        raise ValueError("schedule")
    long_lived = []
    for path in (root / "launchd").glob("*.plist.tmpl"):
        payload = plistlib.loads(path.read_bytes())
        if payload.get("KeepAlive") is True:
            long_lived.append(payload.get("Label"))
    if sorted(long_lived) != [
        "com.user.reverso-daemon",
        "com.user.reverso-proxy",
    ]:
        raise ValueError("service count")

    with urllib.request.urlopen(HEADROOM_USAGE_URL, timeout=5.0) as response:
        expected_profile = HeadroomCompressionConfig.from_env().profile
        validate_headroom_usage_payload(
            json.load(response),
            expected_profile=expected_profile,
        )
except Exception as exc:
    print(
        f"{type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)

print(
    json.dumps(
        {
            "schema_version": 1,
            "status": "passed",
            "commit": head_commit,
            "surface_count": 17,
            "long_lived_service_count": 2,
            "catalog_refresh_schedule": ["06:00", "18:00"],
            "headroom_endpoint": HEADROOM_USAGE_URL,
            "headroom_schema_version": 2,
            "headroom_profile": expected_profile,
            "codex_profiles_executed": codex_profile_count,
            "claude_launchers_executed": claude_launcher_count,
            "rtk_discovered_without_execution": True,
            "second_apply": "no_op",
            "frozen_adapter": "unchanged",
        },
        sort_keys=True,
    )
)
PY
then
    echo "convergence-acceptance: validation failed" >&2
    emit_sanitized_diagnostic "${VALIDATION_STDERR}"
    exit 2
fi
rm -f "${VALIDATION_STDERR}"
cat "${VALIDATION_JSON}"
