"""Frozen regression contracts for client and Headroom convergence slice S1."""

from __future__ import annotations

import ast
import asyncio
import json
import plistlib
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reverso import deployment_drift
from reverso.protocols import model_exposure
from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.headroom_compression import (
    DEFAULT_HEADROOM_METRICS,
    HeadroomCompressionConfig,
    HeadroomCompressionOutcome,
    HeadroomUsageMetrics,
    compress_responses_request,
)

_FIXTURE_DIR = Path("tests/fixtures/client_convergence")


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_contract_fixture_schema_versions() -> None:
    assert _load_fixture("client_sync_contract.json")["schema_version"] == 1
    assert _load_fixture("catalog_refresh_contract.json")["schema_version"] == 1
    assert _load_fixture("headroom_usage_v2_contract.json")["schema_version"] == 2


def test_client_sync_cli_and_exact_result_shapes_are_frozen() -> None:
    contract = _load_fixture("client_sync_contract.json")

    assert contract["entrypoint"] == (
        'reverso-client-sync = "reverso.client_sync:main"'
    )
    assert contract["modes"] == ["dry-run", "apply", "refresh", "verify"]
    assert contract["options"] == [
        "--codex-config PATH",
        "--claude-config-dir PATH",
        "--catalog-dir PATH",
        "--launch-agent-dir PATH",
        "--rtk-bin PATH",
        "--json",
    ]
    assert contract["implicit_mode"] is False
    assert contract["json_stdout_objects"] == 1
    assert contract["human_diagnostics_stream"] == "stderr"
    assert contract["result_fields"] == [
        "schema_version",
        "command",
        "mode",
        "status",
        "exit_code",
        "started_at",
        "finished_at",
        "groups",
        "surfaces",
        "paths",
        "catalog_refresh",
        "errors",
    ]
    assert contract["group_fields"] == [
        "id",
        "kind",
        "status",
        "dependencies",
        "paths",
    ]
    assert contract["surface_fields"] == ["id", "kind", "status", "paths"]
    assert contract["path_fields"] == [
        "path",
        "group",
        "owner",
        "status",
        "before_sha256",
        "after_sha256",
    ]
    assert contract["error_fields"] == ["code", "group", "path", "message"]
    assert contract["catalog_refresh_fields"] == [
        "last_attempt_at",
        "last_success_at",
        "stored_stale",
        "stored_stale_observed_at",
        "stale",
        "observed_at",
    ]


def test_client_sync_exact_status_and_exit_contract_is_frozen() -> None:
    contract = _load_fixture("client_sync_contract.json")

    assert contract["statuses"] == {
        "result": [
            "success",
            "no_op",
            "planned",
            "lock_skipped",
            "lock_busy",
            "drift",
            "stale",
            "invalid",
            "ownership_conflict",
            "partial_freshness",
            "repair_required",
        ],
        "group_and_surface": [
            "current",
            "planned",
            "changed",
            "preserved",
            "blocked_stale_dependency",
            "invalid",
            "drift",
            "rolled_back",
        ],
        "path": [
            "unchanged",
            "planned_create",
            "planned_update",
            "created",
            "updated",
            "preserved_conflict",
            "blocked_stale_dependency",
            "drift",
            "rolled_back",
        ],
    }
    assert contract["exit_codes"] == {
        "0": "success, no-op, dry-run plan, or benign scheduled lock skip",
        "2": "verify drift or refresh staleness, or operator lock timeout",
        "3": "invalid candidate or ownership conflict; no writes",
        "4": "partial provider freshness",
        "5": "rollback or internal inconsistency requires repair",
    }
    assert contract["status_exit_codes"] == {
        "stale": 2,
        "partial_freshness": 4,
    }


def test_exact_eleven_selector_and_ownership_rows_are_frozen() -> None:
    rows = _load_fixture("client_sync_contract.json")["selector_rows"]

    assert [(row["surface"], row["selector"], row["ownership"]) for row in rows] == [
        (
            "Built-in Codex GPT",
            "bare gpt-*",
            "Codex-owned and user-preserving",
        ),
        (
            "MiniMax",
            "bare MiniMax-*",
            "direct Codex profile and user-preserving",
        ),
        (
            "Claude through Claude Code",
            "bare provider model id",
            "Reverso-managed client presentation",
        ),
        (
            "DeepSeek through Reverso",
            "bare provider model id",
            "Reverso-managed client presentation",
        ),
        (
            "Kimi through Reverso",
            "bare kimi-k3",
            "Reverso-managed client presentation",
        ),
        (
            "Ollama through Reverso",
            "bare raw Ollama id",
            "Reverso-managed client presentation",
        ),
        (
            "Copilot through Reverso",
            "copilot/<model>",
            "Reverso-managed client presentation",
        ),
        (
            "Auggie through Reverso",
            "auggie/<model>",
            "Reverso-managed client presentation",
        ),
        (
            "AGY additive catalog",
            "agy/<model>",
            "external source and user-preserving unless marker-owned by its exact sync owner",
        ),
        (
            "Codex Direct",
            "codex-direct/<model>",
            "feature-gated Reverso client presentation",
        ),
        (
            "OpenAI pass-through",
            "openai-pass-through/<model>",
            "feature-gated Reverso client presentation",
        ),
    ]
    minimax = rows[1]
    assert minimax["runtime_authority"] == "codex"
    assert minimax["model_provider"] == "minimax"
    assert minimax["reverso_runtime_route"] is False

    minimax_spec = next(
        spec
        for spec in model_exposure.DIRECT_CODEX_PROFILE_SPECS
        if spec.prefix == "minimax"
    )
    assert minimax_spec.model_provider == "minimax"
    assert minimax_spec.prefix != model_exposure._CODEX_DIRECT_PROFILE_PREFIX
    assert (
        minimax_spec.prefix not in model_exposure.REVERSO_ROUTED_CODEX_PROFILE_PREFIXES
    )

    agy = rows[8]
    assert agy["runtime_authority"] == "external"
    assert agy["reverso_runtime_route"] is False
    assert agy["reverso_gateway_fetch"] is False


def test_ownership_rollback_and_shared_dependency_contract_is_frozen() -> None:
    contract = _load_fixture("client_sync_contract.json")

    assert contract["ownership"] == {
        "unique_writable_path_owner": True,
        "validate_all_before_first_write": True,
        "unmarked_conflicts": "preserve_and_fail_closed",
        "provider_group": (
            "provider-specific catalog and dependent provider-specific profiles "
            "or launchers"
        ),
        "shared_dependency_group": (
            "multi-provider artifacts have one separate owner and commit only "
            "when every dependency is current"
        ),
    }
    assert contract["rollback"] == {
        "scope": "complete handled-failure group",
        "restores": [
            "bytes",
            "existence_or_absence",
            "object_type",
            "symlink_target",
            "executable_mode",
        ],
        "crash_atomicity": False,
        "interruption_recovery": ("verify exact drifted group then idempotent rerun"),
        "shared_stale_status": "blocked_stale_dependency",
        "shared_stale_exit_code": 4,
        "mixed_current_and_prior_inputs": False,
    }


def test_shared_lock_and_nested_token_contract_is_frozen() -> None:
    lock = _load_fixture("client_sync_contract.json")["lock"]

    assert lock["function"] == ("reverso.client_sync_lock.acquire_client_sync_lock")
    assert lock["path"] == (
        "~/Library/Application Support/reverso/catalog-refresh.lock"
    )
    assert lock["scheduled_refresh"] == {
        "acquisition": "non_blocking",
        "contention_status": "lock_skipped",
        "exit_code": 0,
    }
    assert lock["operator_writes"] == {
        "max_wait_seconds": 30,
        "contention_status": "lock_busy",
        "exit_code": 2,
        "writes": False,
    }
    assert lock["read_only_modes"] == ["dry-run", "verify"]
    assert lock["nested_calls"] == (
        "explicit held-lock token; never reacquire or release"
    )


def test_rtk_precedence_parent_safety_and_separation_are_frozen() -> None:
    rtk = _load_fixture("client_sync_contract.json")["rtk"]

    assert rtk == {
        "explicit_path_precedence": True,
        "discovery": (
            "exact host PATH must resolve to one distinct regular executable"
        ),
        "multiple_distinct_candidates": ("fail_closed_require_explicit_selection"),
        "link": "~/.headroom/bin/rtk",
        "create_missing_real_parents_mode": "0700",
        "traverse_symlink_parent": False,
        "conflict_policy": "preserve_and_fail_closed",
        "embedded_headroom_invokes_rtk": False,
    }
    source = Path("src/reverso/protocols/headroom_compression.py").read_text()
    tree = ast.parse(source)
    imported_or_called_names = {
        node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    imported_or_called_names.update(
        alias.name.lower()
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    assert not any("rtk" in name for name in imported_or_called_names)


def test_catalog_refresh_schedule_paths_permissions_and_bounds_are_frozen() -> None:
    contract = _load_fixture("catalog_refresh_contract.json")

    assert contract["launch_agent"] == {
        "label": "com.user.reverso-catalog-refresh",
        "path": "~/Library/LaunchAgents/com.user.reverso-catalog-refresh.plist",
        "start_calendar_interval": [
            {"Hour": 6, "Minute": 0},
            {"Hour": 18, "Minute": 0},
        ],
        "keep_alive": False,
        "listener": False,
        "short_lived": True,
    }
    assert contract["paths"] == {
        "lock": "~/Library/Application Support/reverso/catalog-refresh.lock",
        "status": ("~/Library/Application Support/reverso/catalog-refresh-status.json"),
        "stdout": "~/Library/Logs/reverso/catalog-refresh.stdout.log",
        "stderr": "~/Library/Logs/reverso/catalog-refresh.stderr.log",
    }
    assert contract["permissions"] == {
        "state_directory": "0700",
        "log_directory": "0700",
        "created_lock": "0600",
        "created_status": "0600",
        "created_logs": "0600",
    }
    assert contract["bounds"] == {
        "provider_timeout_seconds": 10,
        "overall_timeout_seconds": 120,
        "log_max_bytes": 1_048_576,
        "log_rotations": 3,
        "error_code_limit": 16,
        "stale_after_hours": 14,
    }
    assert contract["stale_formula"] == (
        "last_success_at is null or more than 14 hours before observed_at"
    )
    assert contract["stored_stale_semantics"] == ("as-of snapshot at stale_observed_at")
    assert contract["verify_semantics"] == (
        "recompute current stale without rewriting status"
    )
    assert contract["lock_skip_updates_last_success"] is False


def test_catalog_refresh_exact_status_and_uninstall_contract_is_frozen() -> None:
    contract = _load_fixture("catalog_refresh_contract.json")

    assert contract["status_fields"] == [
        "schema_version",
        "status",
        "last_attempt_at",
        "last_success_at",
        "duration_ms",
        "exit_code",
        "stale",
        "stale_observed_at",
        "provider_results",
        "error_codes",
    ]
    assert contract["status_values"] == [
        "never_run",
        "success",
        "lock_skipped",
        "partial_freshness",
        "failed",
    ]
    assert contract["provider_result_values"] == [
        "current",
        "changed",
        "stale",
        "invalid",
        "skipped",
    ]
    assert contract["error_codes"] == {
        "sorted": True,
        "governed": True,
        "max_items": 16,
        "raw_text": False,
    }
    assert contract["uninstall"] == {
        "default_removes": ["plist", "bootstrapped_job"],
        "default_preserves": ["lock", "status", "logs", "rotations"],
        "purge_flag": "--purge-state",
        "purge_removes": ["lock", "status", "logs", "rotations"],
    }


def test_exact_two_long_lived_launch_agents_remain_the_current_baseline() -> None:
    expected = {
        "com.user.reverso-proxy": "reverso-proxy",
        "com.user.reverso-daemon": "reverso-daemon",
    }
    assert deployment_drift.LAUNCH_AGENT_EXECUTABLES == expected

    labels = set()
    for path in Path("launchd").glob("*.plist.tmpl"):
        parsed = plistlib.loads(path.read_bytes())
        if parsed.get("KeepAlive", False):
            labels.add(parsed["Label"])
    assert labels == set(expected)

    topology = _load_fixture("catalog_refresh_contract.json")["service_topology"]
    assert topology["long_lived_launch_agents"] == [
        "com.user.reverso-proxy",
        "com.user.reverso-daemon",
    ]
    assert topology["scheduled_launch_agents"] == ["com.user.reverso-catalog-refresh"]
    assert topology["refresh_restarts_services"] is False


def test_headroom_v2_preserved_and_additive_fields_are_exact() -> None:
    contract = _load_fixture("headroom_usage_v2_contract.json")

    assert contract["outer_usage_headroom_schema_version"] == 1
    assert contract["outer_provider"] == "headroom"
    assert contract["same_inner_snapshot_on_usage_routes"] is True
    assert contract["preserved_fields"] == [
        "enabled",
        "profile",
        "requests_seen",
        "requests_compressed",
        "tokens_before",
        "tokens_after",
        "tokens_saved",
        "compression_ratio",
        "fail_open_count",
        "failure_reasons",
        "error_types",
        "updated_at",
    ]
    assert contract["additive_fields"] == [
        "process_started_at",
        "measurement_started_at",
        "requests_passed_through",
        "compression_success_rate",
        "average_tokens_saved",
        "outcome_counts",
        "provider_counts",
        "surface_counts",
        "timeout_seconds",
        "model_limit",
        "last_success_at",
        "last_failure_at",
        "reset_reason",
    ]


def test_headroom_v2_governed_maps_are_exact() -> None:
    maps = _load_fixture("headroom_usage_v2_contract.json")["maps"]

    assert maps == {
        "outcome_counts": [
            "compressed",
            "passed_through",
            "fail_open",
            "other",
        ],
        "failure_reasons": [
            "worker_busy",
            "timeout",
            "exception",
            "inflation_guard",
            "retrieval_marker",
            "unsafe_output",
            "other",
        ],
        "error_types": [
            "timeout",
            "worker_busy",
            "dependency_exception",
            "inflation_guard",
            "retrieval_marker",
            "unsafe_output",
            "other",
        ],
        "provider_counts": [
            "claude",
            "copilot",
            "auggie",
            "deepseek",
            "kimi",
            "ollama",
            "codex-direct",
            "openai-pass-through",
            "other",
        ],
        "surface_counts": ["responses", "anthropic_messages", "other"],
    }


def test_headroom_formulas_reset_and_side_effect_contract_is_frozen() -> None:
    contract = _load_fixture("headroom_usage_v2_contract.json")

    assert contract["formulas"] == {
        "compression_ratio": (
            "tokens_saved / tokens_before; zero when tokens_before is zero"
        ),
        "compression_success_rate": (
            "requests_compressed / requests_seen; zero when requests_seen is zero"
        ),
        "average_tokens_saved": (
            "tokens_saved / requests_compressed; zero when requests_compressed is zero"
        ),
        "requests_passed_through": (
            "max(requests_seen - requests_compressed - fail_open_count, 0)"
        ),
    }
    assert contract["reset_reasons"] == {
        "new_process": "process_start",
        "explicit_test_reset": "manual_test_reset",
    }
    assert contract["persistence"] is False
    assert contract["standalone_savings_file_read"] is False
    assert contract["subprocess"] is False
    assert contract["rtk"] is False
    assert contract["explicit_attribution"] == {
        "provider": True,
        "surface": True,
        "infer_from_raw_model": False,
        "responses_surface": "responses",
        "anthropic_surface": "anthropic_messages",
        "unknown": "other",
    }


def test_current_headroom_profile_defaults_and_override_survive() -> None:
    assert HeadroomCompressionConfig.from_env({}).profile == "coding"
    assert (
        HeadroomCompressionConfig.from_env({"REVERSO_HEADROOM_PROFILE": "   "}).profile
        == "coding"
    )
    assert (
        HeadroomCompressionConfig.from_env(
            {"REVERSO_HEADROOM_PROFILE": "agent-90"}
        ).profile
        == "agent-90"
    )


def test_current_embedded_compression_and_snapshot_do_not_spawn_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("embedded Headroom must not spawn a subprocess")

    async def forbidden_async(*_args: Any, **_kwargs: Any) -> None:
        forbidden()

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "call", forbidden)
    monkeypatch.setattr(subprocess, "check_call", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)

    request = ResponsesRequest(model="raw-model-must-not-drive-attribution", input="x")
    metrics = HeadroomUsageMetrics()

    def compressor(messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        assert messages == [{"role": "user", "content": "x"}]
        return SimpleNamespace(
            messages=[{"role": "user", "content": "compressed"}],
            tokens_before=10,
            tokens_after=4,
            tokens_saved=6,
            compression_ratio=0.6,
        )

    outcome = asyncio.run(
        compress_responses_request(
            request,
            compressor=compressor,
            metrics=metrics,
        )
    )
    snapshot = metrics.snapshot()

    assert outcome.compressed is True
    assert snapshot["requests_seen"] == 1
    assert snapshot["requests_compressed"] == 1
    assert snapshot["tokens_saved"] == 6


@pytest.mark.asyncio
async def test_usage_routes_share_in_memory_headroom_without_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy.compose import CompositionRoot

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("usage routes must not spawn subprocesses")

    async def forbidden_async(*_args: Any, **_kwargs: Any) -> None:
        forbidden()

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_call", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)

    reached = {"gateway": False, "anthropic": False, "legacy": False}

    def tripwire(name: str) -> Any:
        async def app(_scope: Any, _receive: Any, _send: Any) -> None:
            reached[name] = True
            raise AssertionError(f"usage route reached injected {name} app")

        return app

    async def get(path: str) -> dict[str, Any]:
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await root(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "query_string": b"",
                "headers": [],
            },
            receive,
            send,
        )
        body = next(
            message["body"]
            for message in sent
            if message["type"] == "http.response.body"
        )
        return json.loads(body)

    DEFAULT_HEADROOM_METRICS.reset()
    DEFAULT_HEADROOM_METRICS.record(
        HeadroomCompressionOutcome(
            request=ResponsesRequest(model="test-model", input="secret"),
            compressed=True,
            reason="compressed",
            tokens_before=100,
            tokens_after=40,
            tokens_saved=60,
            compression_ratio=0.6,
        )
    )
    expected = DEFAULT_HEADROOM_METRICS.snapshot(HeadroomCompressionConfig.from_env())
    root = CompositionRoot(
        gateway=tripwire("gateway"),
        anthropic_app=tripwire("anthropic"),
        legacy_app=tripwire("legacy"),
    )

    try:
        usage = await get("/usage")
        headroom_usage = await get("/usage/headroom")
    finally:
        DEFAULT_HEADROOM_METRICS.reset()

    assert usage["headroom"] == headroom_usage["headroom"]
    assert usage["headroom"] == expected
    assert reached == {"gateway": False, "anthropic": False, "legacy": False}


def test_current_installation_keeps_lower_level_sync_entrypoints() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["reverso-codex-sync"] == (
        "reverso.codex_sync:main"
    )
    assert project["project"]["scripts"]["reverso-claude-code-sync"] == (
        "reverso.claude_code_sync:main"
    )


def test_readiness_uses_64946_and_never_8787() -> None:
    health = _load_fixture("client_sync_contract.json")["health"]

    assert health == {
        "reverso_readiness": "http://127.0.0.1:64946/health/readiness",
        "base_port": 64946,
        "standalone_headroom_example_port": 58787,
        "reverso_health_port_forbidden": 8787,
    }
    readme = Path("README.md").read_text(encoding="utf-8")
    smoke = Path("scripts/smoke.sh").read_text(encoding="utf-8")
    assert "http://127.0.0.1:64946/health/readiness" in readme
    assert 'BASE="http://127.0.0.1:64946"' in smoke
    assert "127.0.0.1:8787/health" not in readme
    assert "127.0.0.1:8787/health" not in smoke
