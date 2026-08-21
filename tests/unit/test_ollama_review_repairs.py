"""Regression coverage for G3 Ollama review repairs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from reverso import client_sync, codex_sync, ollama_convergence
from reverso.client_sync_mutations import PreparedApplyFailed


def _sync_kwargs(home: Path, fake_bin: Path) -> dict[str, object]:
    return {
        "codex_config": home / ".codex/config.toml",
        "claude_config_dir": home / ".claude",
        "catalog_dir": home / ".codex/reverso",
        "launch_agent_dir": home / ".local/bin",
        "rtk_bin": fake_bin / "rtk",
        "home": home,
        "lock_path": home / "state/refresh.lock",
        "status_path": home / "state/refresh-status.json",
    }


def test_stale_cloud_only_inventory_is_retained_but_not_catalog_eligible(
    tmp_path: Path,
) -> None:
    plan = ollama_convergence.plan_inventory_refresh(
        tmp_path / "ollama-inventory.json",
        local_ids=("local-a",),
        cloud_ids=("current-cloud",),
        cloud_status="current",
        observed_at="2026-08-21T08:00:00+00:00",
    )
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    plan.path.write_bytes(plan.mutation.after.data or b"")

    retained = ollama_convergence.plan_inventory_refresh(
        plan.path,
        local_ids=("local-a",),
        cloud_status="auth_required",
        observed_at="2026-08-21T09:00:00+00:00",
    )

    assert retained.model_ids == ("local-a", "current-cloud")
    assert retained.eligible_model_ids == ("local-a",)


def test_total_ollama_discovery_failure_preserves_group_and_other_provider_hashes(
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
    monkeypatch.setenv("REVERSO_OLLAMA_CLOUD", "0")
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])

    failed = False
    revision = "one"

    def discover(prefix: str, **_kwargs: Any) -> codex_sync.ProviderModels:
        if prefix == "ollama":
            if failed:
                raise codex_sync.ModelDiscoveryError("offline")
            return codex_sync.ProviderModels(prefix, ("local-a",), ("local-a",))
        model = (
            "kimi-k3"
            if prefix == "kimi"
            else (
                "gpt-5.5"
                if prefix == "copilot"
                else (f"auggie-{revision}" if prefix == "auggie" else f"{prefix}-model")
            )
        )
        return codex_sync.ProviderModels(prefix, (model,))

    monkeypatch.setattr(codex_sync, "discover_provider_models", discover)
    kwargs = _sync_kwargs(home, fake_bin)
    assert client_sync.run("apply", **kwargs)["exit_code"] == 0
    tracked = tuple(home.rglob("*"))
    before = {
        path: path.read_bytes()
        for path in tracked
        if path.is_file() and path.name not in {"refresh.lock", "refresh-status.json"}
    }

    failed = True
    revision = "two"
    result = client_sync.run("apply", **kwargs)

    assert result["status"] == "partial_freshness", result
    assert result["exit_code"] == 4
    assert (
        next(row for row in result["groups"] if row["id"] == "provider-ollama")[
            "status"
        ]
        == "preserved"
    )
    assert (
        next(row for row in result["groups"] if row["id"] == "provider-auggie")[
            "status"
        ]
        == "preserved"
    )
    assert {path: path.read_bytes() for path in before} == before


def test_ollama_uninstall_is_marker_safe_idempotent_and_apply_restores(
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
    monkeypatch.setenv("REVERSO_OLLAMA_CLOUD", "0")
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])

    def discover(prefix: str, **_kwargs: Any) -> codex_sync.ProviderModels:
        model = (
            "local-a"
            if prefix == "ollama"
            else (
                "kimi-k3"
                if prefix == "kimi"
                else ("gpt-5.5" if prefix == "copilot" else f"{prefix}-model")
            )
        )
        return codex_sync.ProviderModels(
            prefix,
            (model,),
            (model,) if prefix == "ollama" else (),
        )

    monkeypatch.setattr(codex_sync, "discover_provider_models", discover)
    kwargs = _sync_kwargs(home, fake_bin)
    assert client_sync.run("apply", **kwargs)["exit_code"] == 0
    owned = (
        home / "Library/Application Support/reverso/ollama-inventory.json",
        home / ".codex/reverso-ollama.config.toml",
        home / ".codex/reverso/ollama.json",
        home / ".local/bin/claude-ollama",
    )
    unrelated = home / ".codex/user-owned.txt"
    unrelated.write_text("keep", encoding="utf-8")

    first = client_sync.run("uninstall-ollama", **kwargs)
    second = client_sync.run("uninstall-ollama", **kwargs)

    assert first["status"] == "success"
    assert second["status"] == "no_op"
    assert all(not path.exists() for path in owned)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    restored = client_sync.run("restore", **kwargs)
    assert restored["status"] == "success"
    assert all(path.is_file() for path in owned)
    assert client_sync.run("restore", **kwargs)["status"] == "no_op"


@pytest.mark.parametrize("conflict_index", range(4))
def test_ollama_uninstall_fails_closed_on_each_unowned_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict_index: int,
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
    monkeypatch.setenv("REVERSO_OLLAMA_CLOUD", "0")
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])

    def discover(prefix: str, **_kwargs: Any) -> codex_sync.ProviderModels:
        model = (
            "local-a"
            if prefix == "ollama"
            else (
                "kimi-k3"
                if prefix == "kimi"
                else ("gpt-5.5" if prefix == "copilot" else f"{prefix}-model")
            )
        )
        return codex_sync.ProviderModels(
            prefix,
            (model,),
            (model,) if prefix == "ollama" else (),
        )

    monkeypatch.setattr(codex_sync, "discover_provider_models", discover)
    kwargs = _sync_kwargs(home, fake_bin)
    assert client_sync.run("apply", **kwargs)["exit_code"] == 0
    owned = (
        home / "Library/Application Support/reverso/ollama-inventory.json",
        home / ".codex/reverso-ollama.config.toml",
        home / ".codex/reverso/ollama.json",
        home / ".local/bin/claude-ollama",
    )
    if conflict_index == 2:
        catalog_target = home / ".codex/user-catalog.json"
        catalog_target.write_text("user-owned\n", encoding="utf-8")
        owned[conflict_index].unlink()
        owned[conflict_index].symlink_to(catalog_target)
    else:
        owned[conflict_index].write_text("user-owned\n", encoding="utf-8")
    unrelated = home / ".codex/user-owned.txt"
    unrelated.write_text("keep", encoding="utf-8")
    before = {path: path.read_bytes() for path in (*owned, unrelated)}

    result = client_sync.run("uninstall-ollama", **kwargs)

    assert result["status"] == "invalid"
    assert result["exit_code"] == 3
    assert "ownership conflict" in result["errors"][0]["message"]
    assert {path: path.read_bytes() for path in before} == before
    if conflict_index == 2:
        assert owned[conflict_index].is_symlink()


def test_ollama_apply_failure_precedes_unrelated_provider_mutation(
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
    monkeypatch.setenv("REVERSO_OLLAMA_CLOUD", "0")
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])
    revision = "one"

    def discover(prefix: str, **_kwargs: Any) -> codex_sync.ProviderModels:
        model = (
            f"local-{revision}"
            if prefix == "ollama"
            else (
                "kimi-k3"
                if prefix == "kimi"
                else ("gpt-5.5" if prefix == "copilot" else f"{prefix}-{revision}")
            )
        )
        return codex_sync.ProviderModels(
            prefix,
            (model,),
            (model,) if prefix == "ollama" else (),
        )

    monkeypatch.setattr(codex_sync, "discover_provider_models", discover)
    kwargs = _sync_kwargs(home, fake_bin)
    assert client_sync.run("apply", **kwargs)["exit_code"] == 0
    unrelated = (
        home / ".codex/reverso-auggie.config.toml",
        home / ".codex/reverso/auggie.json",
    )
    before = {path: path.read_bytes() for path in unrelated}
    revision = "two"
    original_apply = client_sync.apply_prepared_group

    def fail_ollama(group: object) -> None:
        if getattr(group, "id", None) == "provider-ollama":
            raise PreparedApplyFailed("injected Ollama failure")
        original_apply(group)  # type: ignore[arg-type]

    monkeypatch.setattr(client_sync, "apply_prepared_group", fail_ollama)

    result = client_sync.run("apply", **kwargs)

    assert result["status"] == "invalid"
    assert (
        next(row for row in result["groups"] if row["id"] == "provider-ollama")[
            "status"
        ]
        == "rolled_back"
    )
    assert {path: path.read_bytes() for path in unrelated} == before
