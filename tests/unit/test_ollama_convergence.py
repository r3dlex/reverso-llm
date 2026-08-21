"""G3 Ollama client convergence and inventory freshness contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from reverso import client_sync, codex_sync, ollama_convergence
from reverso.client_sync_mutations import (
    PreparedApplyFailed,
    apply_prepared_group,
)


def _snapshot(path: Path, entries: list[dict[str, object]]) -> None:
    payload = {
        "schema_version": 1,
        "owner": ollama_convergence.INVENTORY_OWNER,
        "observed_at": "2026-08-20T08:00:00+00:00",
        "freshness": "current",
        "auth_status": "current",
        "cloud_status": "current",
        "entries": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_background_auth_required_retains_only_marker_owned_cloud_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ollama-inventory.json"
    _snapshot(
        path,
        [
            {"raw_id": "old-local", "local": True, "cloud": False, "stale": False},
            {"raw_id": "cloud-a", "local": False, "cloud": True, "stale": False},
            {"raw_id": "both", "local": True, "cloud": True, "stale": False},
        ],
    )

    plan = ollama_convergence.plan_inventory_refresh(
        path,
        local_ids=("new-local", "both"),
        cloud_status="auth_required",
        observed_at="2026-08-21T08:00:00+00:00",
    )

    assert plan.model_ids == ("new-local", "both", "cloud-a")
    assert plan.freshness == "partial"
    assert plan.auth_status == "required"
    assert plan.entries == (
        ollama_convergence.InventoryEntry("new-local", True, False, False),
        ollama_convergence.InventoryEntry("both", True, True, True),
        ollama_convergence.InventoryEntry("cloud-a", False, True, True),
    )


@pytest.mark.parametrize("cloud_status", ["timeout", "invalid"])
def test_background_cloud_failure_retains_prior_cloud_as_stale(
    tmp_path: Path,
    cloud_status: str,
) -> None:
    path = tmp_path / "ollama-inventory.json"
    _snapshot(
        path,
        [{"raw_id": "cloud-a", "local": False, "cloud": True, "stale": False}],
    )

    plan = ollama_convergence.plan_inventory_refresh(
        path,
        local_ids=("local-a",),
        cloud_status=cloud_status,
        observed_at="2026-08-21T08:00:00+00:00",
    )

    assert plan.model_ids == ("local-a", "cloud-a")
    assert plan.freshness == "partial"
    assert plan.cloud_status == cloud_status


def test_cloud_opt_out_is_current_local_only_and_never_calls_cloud_or_signin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ollama-inventory.json"
    _snapshot(
        path,
        [{"raw_id": "cloud-a", "local": False, "cloud": True, "stale": False}],
    )

    plan = ollama_convergence.plan_background_refresh(
        path,
        local_discovery=lambda: ("local-a",),
        cloud_discovery=lambda: pytest.fail("cloud discovery must not run"),
        signin=lambda: pytest.fail("signin must not run"),
        cloud_enabled=False,
        observed_at="2026-08-21T08:00:00+00:00",
    )

    assert plan.model_ids == ("local-a",)
    assert plan.freshness == "current"
    assert plan.auth_status == "disabled"
    assert plan.cloud_status == "disabled"


def test_unmarked_inventory_is_never_stale_retention_authority(tmp_path: Path) -> None:
    path = tmp_path / "ollama-inventory.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "user",
                "entries": [{"raw_id": "user-cloud", "cloud": True, "local": False}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ollama_convergence.InventoryOwnershipConflict):
        ollama_convergence.plan_inventory_refresh(
            path,
            local_ids=("local-a",),
            cloud_status="auth_required",
            observed_at="2026-08-21T08:00:00+00:00",
        )


def test_ollama_group_failure_restores_snapshot_profile_catalog_and_launcher(
    tmp_path: Path,
) -> None:
    paths = [
        tmp_path / "ollama-inventory.json",
        tmp_path / "reverso-ollama.config.toml",
        tmp_path / "ollama.json",
        tmp_path / "claude-ollama",
    ]
    for index, path in enumerate(paths):
        path.write_text(f"before-{index}", encoding="utf-8")

    group = ollama_convergence.prepare_ollama_group(
        inventory_path=paths[0],
        inventory_payload=b"after-inventory",
        client_candidates={
            paths[1]: (b"after-profile", 0o600),
            paths[2]: (b"after-catalog", 0o600),
            paths[3]: (b"after-launcher", 0o755),
        },
    )

    def fail_after_second(_mutation: object, index: int) -> None:
        if index == 2:
            raise OSError("injected cross-client failure")

    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group, observer=fail_after_second)

    assert [path.read_text(encoding="utf-8") for path in paths] == [
        "before-0",
        "before-1",
        "before-2",
        "before-3",
    ]


def test_unchanged_inventory_preserves_bytes_and_mtime(tmp_path: Path) -> None:
    path = tmp_path / "ollama-inventory.json"
    first = ollama_convergence.plan_inventory_refresh(
        path,
        local_ids=("local-a",),
        cloud_status="disabled",
        observed_at="2026-08-21T08:00:00+00:00",
    )
    group = ollama_convergence.prepare_ollama_group(
        inventory_path=path,
        inventory_payload=first.mutation.after.data or b"",
        client_candidates={},
    )
    apply_prepared_group(group)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    second = ollama_convergence.plan_inventory_refresh(
        path,
        local_ids=("local-a",),
        cloud_status="disabled",
        observed_at="2026-08-21T09:00:00+00:00",
    )

    assert second.mutation.changed is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_client_sync_apply_twice_uses_one_ollama_snapshot_for_both_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("claude", "codex", "rtk"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("REVERSO_OLLAMA_CLOUD", "1")
    monkeypatch.delenv("OLLAMA_NO_CLOUD", raising=False)

    calls: list[str] = []

    def discover(prefix: str, **_kwargs: Any) -> codex_sync.ProviderModels:
        calls.append(prefix)
        if prefix == "ollama":
            return codex_sync.ProviderModels(
                prefix,
                ("local-a", "cloud-a"),
                ("local-a",),
                ("cloud-a",),
                "current",
            )
        model = (
            "kimi-k3"
            if prefix == "kimi"
            else ("gpt-5.5" if prefix == "copilot" else f"{prefix}-model")
        )
        return codex_sync.ProviderModels(prefix, (model,))

    monkeypatch.setattr(codex_sync, "discover_provider_models", discover)
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])
    kwargs = {
        "codex_config": home / ".codex/config.toml",
        "claude_config_dir": home / ".claude",
        "catalog_dir": home / ".codex/reverso",
        "launch_agent_dir": home / ".local/bin",
        "rtk_bin": fake_bin / "rtk",
        "home": home,
        "lock_path": home / "state/refresh.lock",
        "status_path": home / "state/refresh-status.json",
    }

    first = client_sync.run("apply", **kwargs)
    second = client_sync.run("apply", **kwargs)

    assert first["status"] == "success", first
    assert second["status"] == "no_op"
    assert calls.count("ollama") == 2
    ollama_group = next(
        row for row in first["groups"] if row["id"] == "provider-ollama"
    )
    assert {Path(path).name for path in ollama_group["paths"]} >= {
        "ollama-inventory.json",
        "reverso-ollama.config.toml",
        "ollama.json",
        "claude-ollama",
    }
    inventory = json.loads(
        (home / "Library/Application Support/reverso/ollama-inventory.json").read_text()
    )
    assert [
        (row["raw_id"], row["local"], row["cloud"]) for row in inventory["entries"]
    ] == [
        ("local-a", True, False),
        ("cloud-a", False, True),
    ]
