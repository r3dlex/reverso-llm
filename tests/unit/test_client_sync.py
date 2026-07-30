"""Unified client convergence orchestration tests."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from reverso import client_sync, client_sync_lock, client_sync_mutations
from reverso.client_sync_lock import (
    ClientSyncLockBusy,
    HeldClientSyncLock,
    acquire_client_sync_lock,
    validate_client_sync_lock,
)
from reverso.client_sync_mutations import (
    PreparedApplyFailed,
    PreparedGroup,
    PreparedRollbackFailed,
    PreparedStateChanged,
    apply_prepared_group,
    directory_state,
    file_state,
    prepared_mutation,
    symlink_state,
)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _empty_convergence_plan(tmp_path: Path) -> client_sync._ConvergencePlan:
    manifest = client_sync.load_supported_surface_manifest()
    group_ids = [group["id"] for group in manifest["groups"]]
    rtk = client_sync.RtkPlan(
        executable=tmp_path / "rtk",
        headroom_dir=tmp_path / ".headroom",
        bin_dir=tmp_path / ".headroom/bin",
        link=tmp_path / ".headroom/bin/rtk",
        marker=tmp_path / ".headroom/bin/.reverso-rtk-owner",
        create_directories=(),
        replace_link=False,
        create_marker=False,
        group=PreparedGroup("rtk", ()),
    )
    return client_sync._ConvergencePlan(
        manifest=manifest,
        groups={group: PreparedGroup(group, ()) for group in group_ids},
        rtk=rtk,
        paths={group: [] for group in group_ids},
        provider_errors={},
    )


def _patch_provider_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale: str | None = None,
) -> None:
    def discover(prefix: str) -> Any:
        if prefix == stale:
            raise client_sync.codex_sync.ProviderFreshnessError(prefix)
        return client_sync.codex_sync.ProviderModels(prefix, (f"{prefix}-model",))

    monkeypatch.setattr(
        client_sync.codex_sync,
        "discover_provider_models",
        discover,
    )
    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_provider_sync",
        lambda models, **_: _prepared_provider(models),
    )


def test_supported_surface_manifest_matches_runtime_authorities() -> None:
    manifest = client_sync.load_supported_surface_manifest()
    client_sync.validate_supported_surface_manifest(manifest)
    assert manifest["reverso_routes"] == [
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
    ]
    assert manifest["external_catalogs"]["agy"]["runtime_route"] is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", 2),
        ("reverso_routes", ["claude"]),
        ("feature_gated_routes", {}),
        ("direct_codex_profiles", {}),
        ("claude_launchers", {}),
        ("external_catalogs", {"agy": {"runtime_route": True, "gateway_fetch": True}}),
        ("surfaces", []),
    ],
)
def test_manifest_field_drift_fails_closed(
    field: str,
    replacement: Any,
) -> None:
    manifest = deepcopy(client_sync.load_supported_surface_manifest())
    manifest[field] = replacement
    with pytest.raises(client_sync.ClientSyncError):
        client_sync.validate_supported_surface_manifest(manifest)


@pytest.mark.parametrize(
    ("section", "item_id", "field", "replacement"),
    [
        ("groups", "provider-claude", "kind", "prerequisite"),
        ("groups", "shared-codex-config", "dependencies", []),
        ("surfaces", "codex-reverso-copilot", "selector_template", "<model>"),
        ("surfaces", "codex-reverso-claude", "ownership", "external"),
        ("surfaces", "codex-reverso-kimi", "runtime_authority", "codex"),
        ("surfaces", "codex-reverso-deepseek", "path_template", "<catalog_dir>/x"),
        ("surfaces", "codex-direct", "feature_gate", None),
        ("surfaces", "claude-codex", "default_model_authority", "runtime"),
        ("surfaces", "codex-agy", "group", "provider-claude"),
    ],
)
def test_manifest_item_contract_drift_fails_closed(
    section: str,
    item_id: str,
    field: str,
    replacement: Any,
) -> None:
    manifest = deepcopy(client_sync.load_supported_surface_manifest())
    item = next(item for item in manifest[section] if item["id"] == item_id)
    item[field] = replacement
    with pytest.raises(client_sync.ClientSyncError):
        client_sync.validate_supported_surface_manifest(manifest)


@pytest.mark.parametrize(
    ("launcher", "expected_group"),
    [
        ("claude-claude", "provider-claude"),
        ("claude-copilot", "provider-copilot"),
        ("claude-auggie", "provider-auggie"),
        ("claude-deepseek", "provider-deepseek"),
        ("claude-kimi", "provider-kimi"),
        ("claude-codex", "provider-codex"),
        ("claude-reverso", "shared-reverso-launcher"),
    ],
)
def test_launcher_paths_have_exact_group_ownership(
    tmp_path: Path,
    launcher: str,
    expected_group: str,
) -> None:
    launcher_dir = tmp_path / "bin"
    assert (
        client_sync._claude_mutation_group(
            launcher_dir / launcher,
            settings_path=tmp_path / "claude/settings.json",
            launcher_dir=launcher_dir,
        )
        == expected_group
    )


@pytest.mark.parametrize("suffix", ["", ".reverso.bak.20260730T000000Z"])
def test_claude_settings_and_backups_have_exact_group_ownership(
    tmp_path: Path,
    suffix: str,
) -> None:
    settings = tmp_path / "claude/settings.json"
    assert (
        client_sync._claude_mutation_group(
            settings.with_name(settings.name + suffix),
            settings_path=settings,
            launcher_dir=tmp_path / "bin",
        )
        == "shared-claude-settings"
    )


def test_unknown_claude_launcher_path_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(client_sync.ClientSyncError):
        client_sync._claude_mutation_group(
            tmp_path / "bin/claude-unknown",
            settings_path=tmp_path / "claude/settings.json",
            launcher_dir=tmp_path / "bin",
        )


def test_clean_nested_claude_roots_have_bounded_recursive_ownership(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    settings = home / "claude-root/nested/settings.json"
    launcher_dir = home / "launcher-root/nested/bin"
    prepared = client_sync.claude_code_sync.prepare_sync(
        settings,
        launcher_dir=launcher_dir,
        claude_executable=_executable(tmp_path / "claude"),
    )

    groups = {
        mutation.path: client_sync._claude_mutation_group(
            mutation.path,
            settings_path=settings,
            launcher_dir=launcher_dir,
        )
        for mutation in prepared.group.mutations
    }
    assert groups[home / "launcher-root"] == "claude-roots"
    assert groups[home / "launcher-root/nested"] == "claude-roots"
    with pytest.raises(client_sync.ClientSyncError):
        client_sync._claude_mutation_group(
            home / "unrelated",
            settings_path=settings,
            launcher_dir=launcher_dir,
        )


def test_clean_nested_codex_roots_have_bounded_recursive_ownership(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = home / "config-root/nested/config.toml"
    catalog_dir = home / "catalog-root/nested/catalogs"
    prepared = client_sync.codex_sync.prepare_sync(
        target=target,
        catalog_dir=catalog_dir,
        prefixes=("claude",),
        fetcher=lambda _prefix: ["claude-model"],
    )

    groups = {
        mutation.path: client_sync._codex_mutation_group(
            mutation,
            target=target,
            catalog_dir=catalog_dir,
            active_prefixes=("claude",),
        )
        for mutation in prepared.group.mutations
    }
    assert groups[home / "config-root"] == "codex-roots"
    assert groups[home / "config-root/nested"] == "codex-roots"
    assert groups[home / "catalog-root"] == "codex-roots"
    assert groups[home / "catalog-root/nested"] == "codex-roots"
    with pytest.raises(client_sync.ClientSyncError):
        client_sync._codex_mutation_group(
            prepared_mutation(home / "unrelated", directory_state()),
            target=target,
            catalog_dir=catalog_dir,
            active_prefixes=("claude",),
        )


def test_plan_clean_home_assigns_shared_missing_ancestors_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = home / "managed"
    claude = _executable(tmp_path / "claude")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(claude.parent), os.environ.get("PATH", ""))),
    )

    def discover(prefix: str) -> client_sync.codex_sync.ProviderModels:
        model = "kimi-k3" if prefix == "kimi" else f"{prefix}-model"
        return client_sync.codex_sync.ProviderModels(prefix, (model,))

    monkeypatch.setattr(
        client_sync.codex_sync,
        "discover_provider_models",
        discover,
    )
    plan = client_sync._plan(
        codex_config=root / "codex/nested/config.toml",
        claude_config_dir=root / "claude/nested",
        catalog_dir=root / "catalog/nested",
        launch_agent_dir=root / "launch/nested",
        rtk_bin=_executable(tmp_path / "rtk"),
        home=home,
    )

    all_paths = [
        mutation.path for group in plan.groups.values() for mutation in group.mutations
    ]
    assert len(all_paths) == len(set(all_paths))
    assert root in plan.paths["claude-roots"]
    assert root not in plan.paths["codex-roots"]


def test_shared_lock_reuses_explicit_nested_token(tmp_path: Path) -> None:
    lock_path = tmp_path / "catalog-refresh.lock"
    with (
        acquire_client_sync_lock(path=lock_path) as token,
        acquire_client_sync_lock(path=lock_path, token=token) as nested,
    ):
        assert nested is token
        assert nested.path == lock_path


def test_forged_lock_token_cannot_authorize_nested_use(tmp_path: Path) -> None:
    lock_path = tmp_path / "catalog-refresh.lock"
    with acquire_client_sync_lock(path=lock_path) as token:
        forged = HeldClientSyncLock(
            path=token.path,
            fd=token.fd,
            owner_pid=token.owner_pid,
        )
        with pytest.raises(RuntimeError, match="not issued"):
            validate_client_sync_lock(forged)
        with (
            pytest.raises(RuntimeError, match="not issued"),
            acquire_client_sync_lock(path=lock_path, token=forged),
        ):
            pass
        target = tmp_path / "config.toml"
        target.write_text("before\n", encoding="utf-8")
        prepared = _prepared_codex(
            target=target,
            catalog_dir=tmp_path / "catalogs",
            changed=True,
        )
        with pytest.raises(RuntimeError, match="not issued"):
            client_sync.codex_sync.apply_prepared(prepared, lock_token=forged)

        assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("field", ["path", "fd", "owner_pid", "released"])
def test_mutated_lock_token_fields_cannot_authorize_writes(
    tmp_path: Path,
    field: str,
) -> None:
    lock_path = tmp_path / "catalog-refresh.lock"
    with acquire_client_sync_lock(path=lock_path) as token:
        original = getattr(token, field)
        replacement: object = {
            "path": tmp_path / "other.lock",
            "fd": token.fd + 1,
            "owner_pid": token.owner_pid + 1,
            "released": True,
        }[field]
        setattr(token, field, replacement)
        try:
            with pytest.raises(RuntimeError, match="not active"):
                validate_client_sync_lock(token)
        finally:
            setattr(token, field, original)


def test_released_token_cannot_be_reused_after_fd_recycling(tmp_path: Path) -> None:
    lock_path = tmp_path / "catalog-refresh.lock"
    with acquire_client_sync_lock(path=lock_path) as token:
        old_fd = token.fd
    replacement_fd = os.open(lock_path, os.O_RDONLY)
    if replacement_fd != old_fd:
        os.dup2(replacement_fd, old_fd)
        os.close(replacement_fd)
        replacement_fd = old_fd
    try:
        token.released = False
        token.fd = replacement_fd
        with pytest.raises(RuntimeError, match="not issued"):
            validate_client_sync_lock(token)
        assert replacement_fd == old_fd
    finally:
        os.close(replacement_fd)


def test_lock_parent_swap_during_leaf_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_parent = tmp_path / "locks"
    lock_parent.mkdir()
    lock_path = lock_parent / "catalog-refresh.lock"
    displaced = tmp_path / "displaced"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    original_open = client_sync_lock.os.open
    swapped = False

    def swap_before_leaf_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == lock_path.name and flags & os.O_CREAT and not swapped:
            swapped = True
            lock_parent.rename(displaced)
            lock_parent.symlink_to(attacker, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(client_sync_lock.os, "open", swap_before_leaf_open)

    with pytest.raises(RuntimeError, match="ancestor changed"):
        client_sync_lock._open_lock(lock_path)

    assert not (attacker / lock_path.name).exists()
    assert (displaced / lock_path.name).is_file()


def test_lock_parent_swap_after_open_before_flock_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_parent = tmp_path / "locks"
    lock_parent.mkdir()
    lock_path = lock_parent / "catalog-refresh.lock"
    displaced = tmp_path / "displaced"
    original_flock = client_sync_lock.fcntl.flock
    swapped = False

    def swap_on_first_exclusive_lock(fd: int, operation: int) -> None:
        nonlocal swapped
        if operation & client_sync_lock.fcntl.LOCK_EX and not swapped:
            swapped = True
            lock_parent.rename(displaced)
            lock_parent.mkdir()
        original_flock(fd, operation)

    monkeypatch.setattr(client_sync_lock.fcntl, "flock", swap_on_first_exclusive_lock)

    with (
        pytest.raises(RuntimeError, match="ancestor changed"),
        acquire_client_sync_lock(path=lock_path),
    ):
        pass

    assert (displaced / lock_path.name).is_file()
    assert not lock_path.exists()
    with acquire_client_sync_lock(path=lock_path) as current:
        assert os.path.samestat(os.fstat(current.fd), lock_path.stat())


@pytest.mark.skipif(
    not Path("/var").is_symlink(),
    reason="macOS lexical /var alias is not present",
)
def test_lock_open_accepts_trusted_macos_var_alias(tmp_path: Path) -> None:
    canonical = tmp_path / "catalog-refresh.lock"
    private_var = Path("/private/var")
    relative = canonical.relative_to(private_var)
    lexical = Path("/var") / relative

    fd = client_sync_lock._open_lock(lexical)
    try:
        assert stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def test_rtk_discovery_requires_one_distinct_executable(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    _executable(first_dir / "rtk")
    _executable(second_dir / "rtk")

    with pytest.raises(client_sync.ClientSyncError, match="multiple distinct"):
        client_sync.resolve_rtk_executable(
            None, host_path=os.pathsep.join((str(first_dir), str(second_dir)))
        )

    assert (
        client_sync.resolve_rtk_executable(first_dir / "rtk", host_path=str(second_dir))
        == (first_dir / "rtk").resolve()
    )


def test_rtk_link_creation_is_marker_owned_and_conflicts_are_preserved(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    executable = _executable(tmp_path / "rtk")

    plan = client_sync.plan_rtk_convergence(executable, home=home)
    assert plan.changed is True
    client_sync.apply_rtk_convergence(plan)
    assert (home / ".headroom/bin/rtk").resolve() == executable.resolve()

    conflicting = _executable(tmp_path / "other-rtk")
    (home / ".headroom/bin/rtk").unlink()
    (home / ".headroom/bin/rtk").symlink_to(conflicting)
    with pytest.raises(client_sync.ClientSyncError, match="conflict"):
        client_sync.plan_rtk_convergence(executable, home=home)
    assert (home / ".headroom/bin/rtk").resolve() == conflicting.resolve()


@pytest.mark.parametrize("swapped", ["binary", "headroom", "bin"])
def test_rtk_prepared_source_swap_blocks_every_write(
    tmp_path: Path,
    swapped: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    executable = _executable(tmp_path / "rtk")
    if swapped == "bin":
        (home / ".headroom/bin").mkdir(parents=True)
    elif swapped == "headroom":
        (home / ".headroom").mkdir()
    plan = client_sync.plan_rtk_convergence(executable, home=home)

    changed = {
        "binary": executable,
        "headroom": home / ".headroom",
        "bin": home / ".headroom/bin",
    }[swapped]
    if changed.is_dir():
        changed.rmdir()
        changed.write_text("owner swap\n", encoding="utf-8")
    else:
        changed.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        changed.chmod(0o755)

    with pytest.raises(PreparedStateChanged):
        client_sync.apply_rtk_convergence(plan)
    assert not (home / ".headroom/bin/rtk").is_symlink()


def test_rtk_parent_symlink_swap_after_first_mutation_never_reaches_attacker(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    headroom = home / ".headroom"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    executable = _executable(tmp_path / "rtk")
    plan = client_sync.plan_rtk_convergence(executable, home=home)
    attacker_writes: list[Path] = []

    def swap_parent(mutation: Any, index: int) -> None:
        if index == 1 and mutation.path == headroom / "bin":
            headroom.rmdir()
            headroom.symlink_to(attacker, target_is_directory=True)
        if index > 1:
            attacker_writes.extend(attacker.iterdir())

    with pytest.raises(PreparedRollbackFailed):
        apply_prepared_group(plan.group, observer=swap_parent)

    assert attacker_writes == []
    assert list(attacker.iterdir()) == []
    assert headroom.is_symlink()
    assert headroom.readlink() == attacker


@pytest.mark.parametrize(
    "target_state",
    [
        file_state("managed\n"),
        directory_state(),
        symlink_state("/external/target"),
    ],
    ids=["file", "directory", "symlink"],
)
def test_descriptor_relative_mutation_rejects_parent_swap_for_every_object_kind(
    tmp_path: Path,
    target_state: Any,
) -> None:
    managed_parent = tmp_path / "managed"
    target = managed_parent / "target"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    group = PreparedGroup(
        "ancestor-swap",
        (
            prepared_mutation(managed_parent, directory_state()),
            prepared_mutation(target, target_state),
        ),
    )

    def swap_parent(_mutation: Any, index: int) -> None:
        if index == 1:
            managed_parent.rmdir()
            managed_parent.symlink_to(attacker, target_is_directory=True)

    with pytest.raises(PreparedRollbackFailed):
        apply_prepared_group(group, observer=swap_parent)

    assert list(attacker.iterdir()) == []
    assert managed_parent.is_symlink()
    assert managed_parent.readlink() == attacker


def test_rollback_rejects_parent_swap_after_target_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_parent = tmp_path / "managed"
    managed_parent.mkdir()
    first = managed_parent / "first"
    second = managed_parent / "second"
    first.write_text("before\n", encoding="utf-8")
    group = PreparedGroup(
        "rollback-ancestor-swap",
        (
            prepared_mutation(first, file_state("after\n")),
            prepared_mutation(second, file_state("second\n")),
        ),
    )
    displaced = tmp_path / "displaced"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    original_apply = client_sync_mutations.apply_state

    def swap_after_first(
        path: Path,
        state: Any,
        *,
        expected: Any = None,
    ) -> None:
        original_apply(path, state, expected=expected)
        if path == first and state == group.mutations[0].after:
            managed_parent.rename(displaced)
            managed_parent.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(client_sync_mutations, "apply_state", swap_after_first)

    with pytest.raises(PreparedRollbackFailed):
        apply_prepared_group(group)

    assert list(attacker.iterdir()) == []
    assert (displaced / "first").read_text(encoding="utf-8") == "after\n"
    assert not (displaced / "second").exists()


def test_later_leaf_swap_preserves_conflict_and_rolls_back_prior_mutation(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("before-first\n", encoding="utf-8")
    second.write_text("before-second\n", encoding="utf-8")
    group = PreparedGroup(
        "later-leaf-cas",
        (
            prepared_mutation(first, file_state("after-first\n")),
            prepared_mutation(second, file_state("after-second\n")),
        ),
    )

    def swap_later_leaf(_mutation: Any, index: int) -> None:
        if index == 1:
            second.write_bytes(b"UNMARKED-CONFLICT")

    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group, observer=swap_later_leaf)

    assert first.read_text(encoding="utf-8") == "before-first\n"
    assert second.read_bytes() == b"UNMARKED-CONFLICT"
    assert not tuple(tmp_path.glob(".*.guard"))
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_rollback_cas_preserves_owner_edit_after_prior_mutation(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("before-first\n", encoding="utf-8")
    second.write_text("before-second\n", encoding="utf-8")
    group = PreparedGroup(
        "rollback-leaf-cas",
        (
            prepared_mutation(first, file_state("after-first\n")),
            prepared_mutation(second, file_state("after-second\n")),
        ),
    )

    def edit_applied_leaf_then_break_later_leaf(
        _mutation: Any,
        index: int,
    ) -> None:
        if index == 1:
            first.write_bytes(b"OWNER-EDIT")
            second.write_bytes(b"UNMARKED-CONFLICT")

    with pytest.raises(PreparedRollbackFailed):
        apply_prepared_group(
            group,
            observer=edit_applied_leaf_then_break_later_leaf,
        )

    assert first.read_bytes() == b"OWNER-EDIT"
    assert second.read_bytes() == b"UNMARKED-CONFLICT"
    assert not tuple(tmp_path.glob(".*.guard"))
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_post_transition_state_change_restores_prior_directory_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "managed"
    target.mkdir(mode=0o755)
    group = PreparedGroup(
        "directory-mode-cas",
        (prepared_mutation(target, directory_state(0o700)),),
    )
    original_fchmod = client_sync_mutations.os.fchmod
    injected = False

    def change_mode_after_transition(fd: int, mode: int) -> None:
        nonlocal injected
        original_fchmod(fd, mode)
        if mode == 0o700 and not injected:
            injected = True
            original_fchmod(fd, 0o711)

    monkeypatch.setattr(
        client_sync_mutations.os,
        "fchmod",
        change_mode_after_transition,
    )

    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_guard_cleanup_failure_rolls_back_target_and_owned_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "managed"
    target.write_text("before\n", encoding="utf-8")
    group = PreparedGroup(
        "guard-cleanup",
        (prepared_mutation(target, file_state("after\n")),),
    )
    original_remove = client_sync_mutations._remove_named_state
    failed = False

    def fail_first_guard_remove(parent_fd: int, name: str, state: Any) -> None:
        nonlocal failed
        if name.endswith(".guard") and not failed:
            failed = True
            raise OSError("injected guard cleanup failure")
        original_remove(parent_fd, name, state)

    monkeypatch.setattr(
        client_sync_mutations,
        "_remove_named_state",
        fail_first_guard_remove,
    )

    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group)

    assert target.read_text(encoding="utf-8") == "before\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["managed"]


def test_candidate_create_failure_preserves_exact_before_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "managed"
    target.write_text("before\n", encoding="utf-8")
    group = PreparedGroup(
        "candidate-create-exact-before",
        (prepared_mutation(target, file_state("after\n")),),
    )
    original_create = client_sync_mutations._create_state

    def recreate_before_then_fail(
        parent_fd: int,
        name: str,
        state: Any,
    ) -> None:
        if name == target.name and state == group.mutations[0].after:
            original_create(parent_fd, name, group.mutations[0].before)
            raise OSError("injected candidate creation failure")
        original_create(parent_fd, name, state)

    monkeypatch.setattr(
        client_sync_mutations,
        "_create_state",
        recreate_before_then_fail,
    )

    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group)

    assert target.read_text(encoding="utf-8") == "before\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["managed"]


def test_candidate_create_failure_preserves_differing_concurrent_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "managed"
    target.write_text("before\n", encoding="utf-8")
    group = PreparedGroup(
        "candidate-create-concurrent-leaf",
        (prepared_mutation(target, file_state("after\n")),),
    )
    original_create = client_sync_mutations._create_state
    concurrent = file_state("concurrent\n")

    def recreate_concurrent_then_fail(
        parent_fd: int,
        name: str,
        state: Any,
    ) -> None:
        if name == target.name and state == group.mutations[0].after:
            original_create(parent_fd, name, concurrent)
            raise OSError("injected candidate creation failure")
        original_create(parent_fd, name, state)

    monkeypatch.setattr(
        client_sync_mutations,
        "_create_state",
        recreate_concurrent_then_fail,
    )

    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group)

    assert target.read_text(encoding="utf-8") == "concurrent\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["managed"]


def test_candidate_create_guard_cleanup_failure_requires_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "managed"
    target.write_text("before\n", encoding="utf-8")
    plan = _empty_convergence_plan(tmp_path)
    plan.groups["rtk"] = PreparedGroup(
        "rtk",
        (prepared_mutation(target, file_state("after\n")),),
    )
    plan.paths["rtk"] = [target]
    original_create = client_sync_mutations._create_state
    original_remove = client_sync_mutations._remove_named_state

    def recreate_before_then_fail(
        parent_fd: int,
        name: str,
        state: Any,
    ) -> None:
        if name == target.name and state == plan.groups["rtk"].mutations[0].after:
            original_create(parent_fd, name, plan.groups["rtk"].mutations[0].before)
            raise OSError("injected candidate creation failure")
        original_create(parent_fd, name, state)

    def fail_guard_remove(parent_fd: int, name: str, state: Any) -> None:
        if name.endswith(".guard"):
            raise OSError("injected guard cleanup failure")
        original_remove(parent_fd, name, state)

    monkeypatch.setattr(client_sync, "_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(
        client_sync_mutations,
        "_create_state",
        recreate_before_then_fail,
    )
    monkeypatch.setattr(
        client_sync_mutations,
        "_remove_named_state",
        fail_guard_remove,
    )

    result = client_sync.run(
        "apply",
        lock_path=tmp_path / "catalog-refresh.lock",
    )

    assert result["status"] == "repair_required"
    assert result["exit_code"] == 5
    assert result["errors"][0]["code"] == "rollback_failed"
    assert target.read_text(encoding="utf-8") == "before\n"
    assert len(tuple(tmp_path.glob(".*.guard"))) == 1
    assert not tuple(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("ancestor_index", range(3))
def test_lock_rejects_every_symlinked_ancestor(
    tmp_path: Path,
    ancestor_index: int,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    ancestors = [tmp_path / "one", tmp_path / "one/two", tmp_path / "one/two/three"]
    for index, ancestor in enumerate(ancestors):
        if index == ancestor_index:
            ancestor.parent.mkdir(parents=True, exist_ok=True)
            ancestor.symlink_to(real, target_is_directory=True)
            break
        ancestor.mkdir()
    with (
        pytest.raises(RuntimeError, match="lock ancestor must be a real directory"),
        acquire_client_sync_lock(path=ancestors[-1] / "sync.lock"),
    ):
        pass


def test_lock_sleep_never_exceeds_remaining_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter((10.0, 10.03, 10.05))
    sleeps: list[float] = []
    monkeypatch.setattr(client_sync_lock.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(client_sync_lock.time, "sleep", sleeps.append)

    def busy(*_args: object) -> None:
        raise BlockingIOError(client_sync_lock.errno.EAGAIN, "busy")

    monkeypatch.setattr(client_sync_lock.fcntl, "flock", busy)
    with (
        pytest.raises(ClientSyncLockBusy),
        acquire_client_sync_lock(
            path=tmp_path / "sync.lock",
            timeout_seconds=0.05,
        ),
    ):
        pass
    assert sleeps == pytest.approx([0.02])


def test_dry_run_composes_lower_level_plans_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, bool]] = []
    rtk = _executable(tmp_path / "rtk")
    home = tmp_path / "home"
    home.mkdir()

    def codex_prepare(**kwargs: Any) -> Any:
        calls.append(("codex", True))
        return _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=True,
        )

    def claude_prepare(settings_path: Path, **kwargs: Any) -> Any:
        del settings_path
        calls.append(("claude", True))
        return _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=False,
        )

    monkeypatch.setattr(client_sync.codex_sync, "prepare_sync", codex_prepare)
    _patch_provider_pipeline(monkeypatch)
    monkeypatch.setattr(client_sync.claude_code_sync, "prepare_sync", claude_prepare)

    result = client_sync.run(
        "dry-run",
        codex_config=tmp_path / "config.toml",
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
    )

    assert calls[0] == ("claude", True)
    assert calls.count(("codex", True)) == 1
    assert result["status"] == "planned"
    assert result["exit_code"] == 0
    assert list(result) == client_sync.RESULT_FIELDS
    json.dumps(result)


@pytest.mark.parametrize("mode", ["dry-run", "apply", "refresh"])
def test_external_agy_catalog_is_reported_and_preserved_without_gateway_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    catalog_dir = tmp_path / "catalogs"
    catalog_dir.mkdir()
    agy_catalog = catalog_dir / "agy.json"
    original = b'{"models":[{"slug":"agy/external-model"}]}\n'
    agy_catalog.write_bytes(original)
    (tmp_path / "agy.config.toml").write_text(
        f'model_provider = "agy"\nmodel_catalog_json = "{agy_catalog}"\n',
        encoding="utf-8",
    )
    requested: list[str] = []

    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_sync",
        lambda **kwargs: _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=False,
        ),
    )

    def discover(prefix: str) -> Any:
        requested.append(prefix)
        return client_sync.codex_sync.ProviderModels(prefix, (f"{prefix}-model",))

    monkeypatch.setattr(client_sync.codex_sync, "discover_provider_models", discover)
    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_provider_sync",
        lambda models, **_: _prepared_provider(models),
    )
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "prepare_sync",
        lambda *args, **kwargs: _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=False,
        ),
    )
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])

    result = client_sync.run(
        mode,
        codex_config=tmp_path / "config.toml",
        claude_config_dir=tmp_path / "claude",
        catalog_dir=catalog_dir,
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    external_group = next(
        group for group in result["groups"] if group["id"] == "external-agy"
    )
    external_surface = next(
        surface for surface in result["surfaces"] if surface["id"] == "codex-agy"
    )
    assert external_group["paths"] == [str(agy_catalog)]
    assert external_surface["paths"] == [str(agy_catalog)]
    assert agy_catalog.read_bytes() == original
    assert "agy" not in requested


def test_unconfigured_agy_catalog_is_preserved_but_not_claimed(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "catalogs"
    catalog_dir.mkdir()
    catalog = catalog_dir / "agy.json"
    original = b'{"external":true}\n'
    catalog.write_bytes(original)

    assert (
        client_sync._external_agy_catalog(tmp_path / "config.toml", catalog_dir) is None
    )
    assert catalog.read_bytes() == original


@pytest.mark.parametrize(
    ("profile", "expected_status"),
    [
        (b"not = [valid", "invalid"),
        (
            b'model_provider = "other"\nmodel_catalog_json = "agy.json"\n',
            "ownership_conflict",
        ),
        (b'model_provider = "agy"\n', "invalid"),
    ],
)
def test_external_agy_profile_errors_fail_closed(
    tmp_path: Path,
    profile: bytes,
    expected_status: str,
) -> None:
    profile_path = tmp_path / "agy.config.toml"
    profile_path.write_bytes(profile)
    with pytest.raises(client_sync.ClientSyncError) as exc_info:
        client_sync._external_agy_catalog(
            tmp_path / "config.toml",
            tmp_path / "catalogs",
        )
    assert exc_info.value.status == expected_status


def test_external_agy_profile_symlink_conflict_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "external-profile.toml"
    target.write_text(
        'model_provider = "agy"\nmodel_catalog_json = "agy.json"\n',
        encoding="utf-8",
    )
    (tmp_path / "agy.config.toml").symlink_to(target)
    with pytest.raises(client_sync.ClientSyncError) as exc_info:
        client_sync._external_agy_catalog(
            tmp_path / "config.toml",
            tmp_path / "catalogs",
        )
    assert exc_info.value.status == "ownership_conflict"


@pytest.mark.parametrize(
    "catalog",
    [
        b"{not-json",
        b'{"models":"agy/model"}',
        b'{"models":[]}',
        b'{"models":[{"slug":"external-model"}]}',
        b'{"models":[{"slug":"copilot/model"}]}',
        b'{"models":[{"slug":"agy/"}]}',
        b'{"models":[{"name":"agy/model"}]}',
    ],
)
def test_external_agy_catalog_schema_and_ownership_fail_closed(
    tmp_path: Path,
    catalog: bytes,
) -> None:
    catalog_path = tmp_path / "agy.json"
    catalog_path.write_bytes(catalog)
    (tmp_path / "agy.config.toml").write_text(
        'model_provider = "agy"\nmodel_catalog_json = "agy.json"\n',
        encoding="utf-8",
    )

    with pytest.raises(client_sync.ClientSyncError):
        client_sync._external_agy_catalog(
            tmp_path / "config.toml",
            tmp_path / "catalogs",
        )

    assert catalog_path.read_bytes() == catalog


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_external_agy_catalog_rejects_non_regular_objects(
    tmp_path: Path,
    kind: str,
) -> None:
    catalog_path = tmp_path / "agy.json"
    if kind == "directory":
        catalog_path.mkdir()
    else:
        target = tmp_path / "external.json"
        target.write_text('{"models":[{"slug":"agy/model"}]}', encoding="utf-8")
        catalog_path.symlink_to(target)
    (tmp_path / "agy.config.toml").write_text(
        'model_provider = "agy"\nmodel_catalog_json = "agy.json"\n',
        encoding="utf-8",
    )

    with pytest.raises(client_sync.ClientSyncError) as exc_info:
        client_sync._external_agy_catalog(
            tmp_path / "config.toml",
            tmp_path / "catalogs",
        )

    assert exc_info.value.status == "ownership_conflict"


def test_invalid_agy_catalog_fails_before_any_group_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    catalog = tmp_path / "agy.json"
    catalog.write_text('{"models":[{"slug":"external-model"}]}', encoding="utf-8")
    (tmp_path / "agy.config.toml").write_text(
        'model_provider = "agy"\nmodel_catalog_json = "agy.json"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_sync",
        lambda **kwargs: _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=True,
        ),
    )
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "prepare_sync",
        lambda *args, **kwargs: _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=True,
        ),
    )

    result = client_sync.run(
        "apply",
        codex_config=tmp_path / "config.toml",
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    assert result["status"] == "ownership_conflict"
    assert result["exit_code"] == 3
    assert not (home / ".headroom").exists()
    assert not (tmp_path / "config.toml").exists()
    assert catalog.read_text(encoding="utf-8") == (
        '{"models":[{"slug":"external-model"}]}'
    )


def test_cli_requires_explicit_mode() -> None:
    with pytest.raises(SystemExit) as exc_info:
        client_sync.main([])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "failure",
    [
        client_sync.ClientSyncError("manifest invalid"),
        OSError("filesystem unavailable"),
        ValueError("malformed manifest"),
        RuntimeError("ownership conflict"),
    ],
)
def test_json_mode_emits_exactly_one_object_for_handled_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    def fail_plan(**_kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(client_sync, "_plan", fail_plan)
    assert client_sync.main(["dry-run", "--json"]) == 3
    stdout = capsys.readouterr().out
    payload, end = json.JSONDecoder().raw_decode(stdout)
    assert stdout[end:].strip() == ""
    assert list(payload) == client_sync.RESULT_FIELDS
    assert len(payload["errors"]) == 1


def _codex_result(
    *,
    target: Path,
    catalog_dir: Path,
    changed: bool,
) -> client_sync.codex_sync.SyncResult:
    return client_sync.codex_sync.SyncResult(
        target=target,
        changed=changed,
        backup=None,
        rotated=[],
        provider_models=[],
        catalog_dir=catalog_dir,
        catalogs=[],
        profiles=[],
        profile_backups=[],
        archived_profiles=[],
    )


def _claude_result(
    *,
    launcher_dir: Path,
    changed: bool,
    error: str | None = None,
) -> client_sync.claude_code_sync.ClaudeCodeSyncResult:
    return client_sync.claude_code_sync.ClaudeCodeSyncResult(
        settings_path="settings.json",
        changed=changed,
        dry_run=True,
        backup_path=None,
        removed_env_keys=(),
        removed_settings_keys=(),
        removed_model=None,
        error=error,
        launcher_dir=str(launcher_dir),
        changed_launchers=(),
        conflicting_launchers=(),
    )


def _prepared_provider(
    models: client_sync.codex_sync.ProviderModels,
) -> client_sync.codex_sync.PreparedCodexSync:
    result = _codex_result(
        target=Path("config.toml"),
        catalog_dir=Path("catalogs"),
        changed=False,
    )
    result.provider_models = [models]
    return client_sync.codex_sync.PreparedCodexSync(
        PreparedGroup(f"provider-{models.prefix}", ()),
        result,
        result,
    )


def _prepared_codex(
    *,
    target: Path,
    catalog_dir: Path,
    changed: bool,
) -> Any:
    result = _codex_result(
        target=target,
        catalog_dir=catalog_dir,
        changed=changed,
    )
    mutations = (
        (prepared_mutation(target, file_state("after\n", 0o600)),) if changed else ()
    )
    return client_sync.codex_sync.PreparedCodexSync(
        PreparedGroup("codex", mutations),
        result,
        result,
    )


def _prepared_claude(
    *, launcher_dir: Path, changed: bool, error: str | None = None
) -> Any:
    result = _claude_result(
        launcher_dir=launcher_dir,
        changed=changed,
        error=error,
    )
    path = launcher_dir / "claude-reverso"
    mutations = (
        (prepared_mutation(path, file_state("launcher\n", 0o755)),) if changed else ()
    )
    return client_sync.claude_code_sync.PreparedClaudeCodeSync(
        PreparedGroup("claude", mutations),
        result,
    )


def _readback_plan(
    *,
    manifest: dict[str, Any],
    link: Path,
    executable: Path,
) -> client_sync._ConvergencePlan:
    rtk = client_sync.RtkPlan(
        executable=executable.resolve(),
        headroom_dir=link.parent.parent,
        bin_dir=link.parent,
        link=link,
        marker=link.parent / ".reverso-rtk-owner",
        create_directories=(),
        replace_link=False,
        create_marker=False,
        group=PreparedGroup("rtk", ()),
    )
    return client_sync._ConvergencePlan(
        manifest=manifest,
        groups={},
        rtk=rtk,
        paths={},
        provider_errors={},
    )


def test_global_prevalidation_failure_blocks_every_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    writes: list[str] = []

    def fake_codex(**kwargs: Any) -> Any:
        return _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=True,
        )

    def fake_claude(settings_path: Path, **kwargs: Any) -> Any:
        del settings_path
        return _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=False,
            error="unmanaged launcher conflict",
        )

    monkeypatch.setattr(client_sync.codex_sync, "prepare_sync", fake_codex)
    monkeypatch.setattr(client_sync.claude_code_sync, "prepare_sync", fake_claude)
    monkeypatch.setattr(
        client_sync.codex_sync,
        "apply_prepared",
        lambda *args, **kwargs: writes.append("codex"),
    )
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "apply_prepared",
        lambda *args, **kwargs: writes.append("claude"),
    )
    result = client_sync.run(
        "apply",
        codex_config=tmp_path / "config.toml",
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    assert result["status"] == "ownership_conflict"
    assert result["exit_code"] == 3
    assert writes == []
    assert not (home / ".headroom").exists()


def test_invalid_launcher_candidate_blocks_every_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    rtk = _executable(tmp_path / "rtk")
    rtk_bin = home / ".headroom/bin"
    rtk_bin.mkdir(parents=True)
    rtk_link = rtk_bin / "rtk"
    rtk_link.symlink_to(rtk)
    rtk_marker = rtk_bin / ".reverso-rtk-owner"
    rtk_marker.write_text(client_sync._RTK_MARKER, encoding="utf-8")

    codex_config = tmp_path / "codex/config.toml"
    codex_config.parent.mkdir()
    codex_backup = codex_config.with_name("config.toml.reverso-sync.sentinel")
    codex_archive = (
        codex_config.parent
        / client_sync.codex_sync.PROFILE_ARCHIVE_DIR
        / "reverso-claude.config.toml.reverso-sync.sentinel"
    )
    codex_archive.parent.mkdir(parents=True)
    codex_config.write_bytes(b"codex-config-before\n")
    codex_backup.write_bytes(b"codex-backup-before\n")
    codex_archive.write_bytes(b"codex-archive-before\n")

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    claude_settings = claude_dir / "settings.json"
    claude_backup = claude_dir / "settings.json.reverso.bak.sentinel"
    claude_settings.write_bytes(b'{"settings":"before"}\n')
    claude_backup.write_bytes(b"claude-backup-before\n")
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    launcher_paths = {
        name: launcher_dir / name
        for name, _catalog in client_sync.claude_code_sync.LAUNCHER_CATALOGS
    }
    for name, path in launcher_paths.items():
        path.write_bytes(f"{name}-before\n".encode())
        path.chmod(0o755)

    watched_paths = (
        rtk_link,
        rtk_marker,
        codex_config,
        codex_backup,
        codex_archive,
        claude_settings,
        claude_backup,
        *launcher_paths.values(),
    )
    before = {path: client_sync_mutations.capture_state(path) for path in watched_paths}

    def fake_claude(settings_path: Path, **kwargs: Any) -> Any:
        assert settings_path == claude_settings
        assert kwargs["launcher_dir"] == launcher_dir
        mutations = [
            prepared_mutation(settings_path, file_state('{"settings":"after"}\n')),
            prepared_mutation(claude_backup, file_state("claude-backup-after\n")),
        ]
        for name, path in launcher_paths.items():
            candidate = (
                b"#!/bin/sh\nif\n" if name == "claude-kimi" else b"#!/bin/sh\nexit 0\n"
            )
            mutations.append(prepared_mutation(path, file_state(candidate, 0o755)))
        result = _claude_result(launcher_dir=launcher_dir, changed=True)
        return client_sync.claude_code_sync.PreparedClaudeCodeSync(
            PreparedGroup("claude", tuple(mutations)),
            result,
            claude_backup,
        )

    monkeypatch.setattr(client_sync.claude_code_sync, "prepare_sync", fake_claude)
    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_sync",
        lambda **_kwargs: pytest.fail(
            "Codex preparation must not follow invalid input"
        ),
    )

    result = client_sync.run(
        "apply",
        codex_config=codex_config,
        claude_config_dir=claude_dir,
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=launcher_dir,
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    assert result["status"] == "invalid"
    assert result["exit_code"] == 3
    assert result["errors"][0]["code"] == "launcher_candidate_invalid"
    assert {
        path: client_sync_mutations.capture_state(path) for path in watched_paths
    } == before


def test_complete_set_prevalidation_blocks_first_group_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    first.write_text("before-first\n", encoding="utf-8")
    second.write_text("before-second\n", encoding="utf-8")
    third.write_text("before-third\n", encoding="utf-8")
    plan = _empty_convergence_plan(tmp_path)
    plan.groups["rtk"] = PreparedGroup(
        "rtk",
        (prepared_mutation(first, file_state("after-first\n")),),
    )
    plan.paths["rtk"] = [first]
    plan.groups["direct-openai"] = PreparedGroup(
        "direct-openai",
        (
            prepared_mutation(second, file_state("after-second\n")),
            prepared_mutation(third, file_state("after-third\n")),
        ),
    )
    plan.paths["direct-openai"] = [second, third]

    def stale_second_group(**_kwargs: Any) -> client_sync._ConvergencePlan:
        second.write_bytes(b"UNMARKED-CONFLICT")
        return plan

    monkeypatch.setattr(client_sync, "_plan", stale_second_group)

    result = client_sync.run(
        "apply",
        lock_path=tmp_path / "catalog-refresh.lock",
    )

    assert result["status"] == "drift"
    assert result["exit_code"] == 2
    assert first.read_text(encoding="utf-8") == "before-first\n"
    assert second.read_bytes() == b"UNMARKED-CONFLICT"
    assert third.read_text(encoding="utf-8") == "before-third\n"
    groups = {group["id"]: group["status"] for group in result["groups"]}
    assert groups["rtk"] == "current"
    assert groups["direct-openai"] == "drift"
    surfaces = {surface["id"]: surface["status"] for surface in result["surfaces"]}
    assert surfaces["codex-builtin-openai"] == "drift"
    assert all(
        status == "current"
        for surface, status in surfaces.items()
        if surface != "codex-builtin-openai"
    )
    paths = {path["path"]: path["status"] for path in result["paths"]}
    assert paths[str(first)] == "unchanged"
    assert paths[str(second)] == "drift"
    assert paths[str(third)] == "unchanged"
    assert result["errors"] == [
        {
            "code": "prepared_state_changed",
            "group": "direct-openai",
            "path": str(second),
            "message": "PreparedStateChanged",
        }
    ]


def test_stale_provider_advances_only_independent_rtk_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    writes: list[str] = []

    def fake_codex(**kwargs: Any) -> Any:
        return _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=False,
        )

    def fake_claude(settings_path: Path, **kwargs: Any) -> Any:
        del settings_path
        return _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=True,
        )

    monkeypatch.setattr(client_sync.codex_sync, "prepare_sync", fake_codex)
    _patch_provider_pipeline(monkeypatch, stale="claude")
    monkeypatch.setattr(client_sync.claude_code_sync, "prepare_sync", fake_claude)
    monkeypatch.setattr(
        client_sync.codex_sync,
        "apply_prepared",
        lambda *args, **kwargs: writes.append("codex"),
    )
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "apply_prepared",
        lambda *args, **kwargs: writes.append("claude"),
    )
    result = client_sync.run(
        "apply",
        codex_config=tmp_path / "config.toml",
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    statuses = {group["id"]: group["status"] for group in result["groups"]}
    assert result["status"] == "partial_freshness"
    assert result["exit_code"] == 4
    assert statuses["provider-claude"] == "preserved"
    assert statuses["shared-codex-config"] == "blocked_stale_dependency"
    assert statuses["shared-reverso-launcher"] == "blocked_stale_dependency"
    assert statuses["rtk"] == "changed"
    assert writes == []
    assert (home / ".headroom/bin/rtk").resolve() == rtk.resolve()


@pytest.mark.parametrize(
    "stale_prefix",
    ["claude", "copilot", "auggie", "deepseek", "kimi"],
)
def test_each_stale_provider_is_preserved_while_independent_groups_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_prefix: str,
) -> None:
    monkeypatch.setattr(client_sync, "_post_apply_readback_errors", lambda *a, **k: [])
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    config_dir = tmp_path / "codex"
    catalog_dir = tmp_path / "catalogs"
    launcher_dir = tmp_path / "bin"
    claude_dir = tmp_path / "claude"
    for directory in (config_dir, catalog_dir, launcher_dir, claude_dir):
        directory.mkdir()
    target = config_dir / "config.toml"
    target.write_text("shared-before\n", encoding="utf-8")
    stale_profile = config_dir / f"reverso-{stale_prefix}.config.toml"
    stale_catalog = catalog_dir / f"{stale_prefix}.json"
    stale_launcher = launcher_dir / f"claude-{stale_prefix}"
    stale_profile.write_text("profile-before\n", encoding="utf-8")
    stale_catalog.write_text("catalog-before\n", encoding="utf-8")
    stale_launcher.write_text("launcher-before\n", encoding="utf-8")

    def discover(prefix: str) -> Any:
        if prefix == stale_prefix:
            raise client_sync.codex_sync.ProviderFreshnessError("private detail")
        return client_sync.codex_sync.ProviderModels(prefix, (f"{prefix}-model",))

    def prepare_provider(models: Any, **kwargs: Any) -> Any:
        profile = kwargs["target"].parent / f"reverso-{models.prefix}.config.toml"
        catalog = kwargs["catalog_dir"] / f"{models.prefix}.json"
        group = PreparedGroup(
            f"provider-{models.prefix}",
            (
                prepared_mutation(profile, file_state("profile-after\n")),
                prepared_mutation(catalog, file_state("catalog-after\n")),
            ),
        )
        result = _codex_result(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=True,
        )
        result.provider_models = [models]
        return client_sync.codex_sync.PreparedCodexSync(group, result, result)

    def prepare_base(**kwargs: Any) -> Any:
        return _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=True,
        )

    def prepare_claude(settings_path: Path, **kwargs: Any) -> Any:
        mutations = [
            prepared_mutation(settings_path, file_state("{}\n")),
            prepared_mutation(
                kwargs["launcher_dir"] / "claude-reverso",
                file_state("shared-after\n", 0o755),
            ),
        ]
        mutations.extend(
            prepared_mutation(
                kwargs["launcher_dir"] / launcher,
                file_state(f"{launcher}-after\n", 0o755),
            )
            for launcher in dict(client_sync.claude_code_sync.LAUNCHER_CATALOGS)
            if launcher != "claude-reverso"
        )
        result = _claude_result(
            launcher_dir=kwargs["launcher_dir"],
            changed=True,
        )
        return client_sync.claude_code_sync.PreparedClaudeCodeSync(
            PreparedGroup("claude", tuple(mutations)),
            result,
        )

    monkeypatch.setattr(
        client_sync.codex_sync,
        "discover_provider_models",
        discover,
    )
    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_provider_sync",
        prepare_provider,
    )
    monkeypatch.setattr(client_sync.codex_sync, "prepare_sync", prepare_base)
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "prepare_sync",
        prepare_claude,
    )

    result = client_sync.run(
        "apply",
        codex_config=target,
        claude_config_dir=claude_dir,
        catalog_dir=catalog_dir,
        launch_agent_dir=launcher_dir,
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    statuses = {group["id"]: group["status"] for group in result["groups"]}
    assert result["status"] == "partial_freshness"
    assert result["exit_code"] == 4
    assert statuses[f"provider-{stale_prefix}"] == "preserved"
    assert statuses["shared-codex-config"] == "blocked_stale_dependency"
    assert statuses["shared-codex-cleanup"] == "blocked_stale_dependency"
    assert statuses["shared-reverso-launcher"] == "blocked_stale_dependency"
    assert statuses["shared-claude-settings"] == "changed"
    assert statuses["rtk"] == "changed"
    assert stale_profile.read_text(encoding="utf-8") == "profile-before\n"
    assert stale_catalog.read_text(encoding="utf-8") == "catalog-before\n"
    assert stale_launcher.read_text(encoding="utf-8") == "launcher-before\n"
    assert target.read_text(encoding="utf-8") == "shared-before\n"
    assert not (launcher_dir / "claude-reverso").exists()
    assert any(
        group["status"] == "changed"
        for group in result["groups"]
        if group["id"].startswith("provider-")
        and group["id"] != f"provider-{stale_prefix}"
    )
    assert result["errors"] == [
        {
            "code": "provider_stale",
            "group": f"provider-{stale_prefix}",
            "path": None,
            "message": "ProviderFreshnessError",
        }
    ]


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_code"),
    [("apply", "lock_busy", 2), ("refresh", "lock_skipped", 0)],
)
def test_lock_contention_has_frozen_mode_specific_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_status: str,
    expected_code: int,
) -> None:
    @contextmanager
    def busy_lock(**kwargs: Any) -> Iterator[None]:
        raise ClientSyncLockBusy("busy")
        yield

    monkeypatch.setattr(client_sync, "acquire_client_sync_lock", busy_lock)
    result = client_sync.run(
        mode,
        lock_path=tmp_path / "sync.lock",
        status_path=tmp_path / "status.json",
    )
    assert result["status"] == expected_status
    assert result["exit_code"] == expected_code
    assert result["groups"] == []


def test_caught_group_failure_restores_bytes_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    target = tmp_path / "config.toml"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o751)

    def fake_codex(**kwargs: Any) -> Any:
        return _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=True,
        )

    def fake_claude(settings_path: Path, **kwargs: Any) -> Any:
        del settings_path
        return _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=False,
        )

    monkeypatch.setattr(client_sync.codex_sync, "prepare_sync", fake_codex)
    _patch_provider_pipeline(monkeypatch)
    monkeypatch.setattr(client_sync.claude_code_sync, "prepare_sync", fake_claude)

    original_apply_group = client_sync.apply_prepared_group

    def fail_apply(group: PreparedGroup) -> None:
        if group.id == "shared-codex-config":
            target.write_text("before\n", encoding="utf-8")
            target.chmod(0o751)
            raise PreparedApplyFailed("OSError")
        original_apply_group(group)

    monkeypatch.setattr(client_sync, "apply_prepared_group", fail_apply)
    result = client_sync.run(
        "apply",
        codex_config=target,
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
        lock_path=tmp_path / "sync.lock",
    )

    assert result["status"] == "invalid"
    assert result["exit_code"] == 3
    assert target.read_text(encoding="utf-8") == "before\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    codex = next(
        group for group in result["groups"] if group["id"] == "shared-codex-config"
    )
    assert codex["status"] == "rolled_back"


@pytest.mark.parametrize("failure_index", range(3))
def test_fault_after_every_ledger_mutation_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_index: int,
) -> None:
    paths = [tmp_path / f"path-{index}" for index in range(3)]
    for index, path in enumerate(paths):
        path.write_text(f"before-{index}\n", encoding="utf-8")
    group = PreparedGroup(
        "fault-matrix",
        tuple(
            prepared_mutation(path, file_state(f"after-{index}\n"))
            for index, path in enumerate(paths)
        ),
    )
    original_apply = client_sync_mutations.apply_state

    def apply_then_fail(
        path: Path,
        state: Any,
        *,
        expected: Any = None,
    ) -> None:
        original_apply(path, state, expected=expected)
        mutation = group.mutations[failure_index]
        if path == mutation.path and state == mutation.after:
            raise OSError("injected after mutation")

    monkeypatch.setattr(client_sync_mutations, "apply_state", apply_then_fail)
    with pytest.raises(PreparedApplyFailed):
        apply_prepared_group(group)
    assert [path.read_text() for path in paths] == [
        "before-0\n",
        "before-1\n",
        "before-2\n",
    ]


def test_rollback_failure_is_reported_with_typed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("before-first\n", encoding="utf-8")
    second.write_text("before-second\n", encoding="utf-8")
    group = PreparedGroup(
        "rollback",
        (
            prepared_mutation(first, file_state("after-first\n")),
            prepared_mutation(second, file_state("after-second\n")),
        ),
    )
    original_apply = client_sync_mutations.apply_state

    def fail_apply_and_rollback(
        path: Path,
        state: Any,
        *,
        expected: Any = None,
    ) -> None:
        if path == second and state == group.mutations[1].after:
            raise OSError("apply failed")
        if path == first and state == group.mutations[0].before:
            raise OSError("rollback failed")
        original_apply(path, state, expected=expected)

    monkeypatch.setattr(client_sync_mutations, "apply_state", fail_apply_and_rollback)
    with pytest.raises(PreparedRollbackFailed):
        apply_prepared_group(group)


def test_path_hashes_come_from_exact_prepared_before_and_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text("before\n", encoding="utf-8")
    prepared = _prepared_codex(
        target=target,
        catalog_dir=tmp_path / "catalogs",
        changed=True,
    )
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    monkeypatch.setattr(client_sync.codex_sync, "prepare_sync", lambda **_: prepared)
    _patch_provider_pipeline(monkeypatch)
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "prepare_sync",
        lambda *args, **kwargs: _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=False,
        ),
    )
    result = client_sync.run(
        "dry-run",
        codex_config=target,
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
    )
    record = next(item for item in result["paths"] if item["path"] == str(target))
    assert (
        record["before_sha256"] == client_sync.hashlib.sha256(b"before\n").hexdigest()
    )
    assert record["after_sha256"] == client_sync.hashlib.sha256(b"after\n").hexdigest()
    assert target.read_text() == "before\n"


def test_catalog_refresh_preserves_stored_observation_and_recomputes_stale(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_attempt_at": "2026-07-30T00:00:00+00:00",
                "last_success_at": "2026-07-30T01:00:00+00:00",
                "stale": False,
                "stale_observed_at": "2026-07-30T02:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    refresh = client_sync._catalog_refresh(
        path=status_path,
        observed_at="2026-07-30T16:00:00+00:00",
    )
    assert refresh["stored_stale"] is False
    assert refresh["stored_stale_observed_at"] == "2026-07-30T02:00:00+00:00"
    assert refresh["stale"] is True


@pytest.mark.parametrize(
    ("last_success_at", "expected_status", "expected_exit"),
    [
        (None, "drift", 2),
        ("2026-07-30T02:00:00+00:00", "success", 0),
        ("2026-07-30T01:59:59+00:00", "drift", 2),
    ],
    ids=["missing", "exact-14h", "over-14h"],
)
def test_verify_maps_catalog_refresh_staleness_to_public_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    last_success_at: str | None,
    expected_status: str,
    expected_exit: int,
) -> None:
    status_path = tmp_path / "catalog-refresh-status.json"
    if last_success_at is not None:
        status_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "last_success_at": last_success_at,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(client_sync, "_timestamp", lambda: "2026-07-30T16:00:00+00:00")
    monkeypatch.setattr(
        client_sync,
        "_plan",
        lambda **_kwargs: _empty_convergence_plan(tmp_path),
    )

    result = client_sync.run("verify", status_path=status_path)

    assert result["status"] == expected_status
    assert result["exit_code"] == expected_exit
    stale_errors = [
        error for error in result["errors"] if error["code"] == "catalog_refresh_stale"
    ]
    assert len(stale_errors) == (1 if expected_status == "drift" else 0)
    assert result["catalog_refresh"]["stale"] is (expected_status == "drift")
    if last_success_at is None:
        assert not status_path.exists()
    else:
        assert status_path.exists()


def test_verify_path_drift_takes_precedence_over_catalog_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text("before\n", encoding="utf-8")
    plan = _empty_convergence_plan(tmp_path)
    plan.groups["direct-openai"] = PreparedGroup(
        "direct-openai",
        (prepared_mutation(target, file_state("after\n")),),
    )
    plan.paths["direct-openai"] = [target]
    monkeypatch.setattr(client_sync, "_timestamp", lambda: "2026-07-30T16:00:00+00:00")
    monkeypatch.setattr(client_sync, "_plan", lambda **_kwargs: plan)

    result = client_sync.run(
        "verify",
        status_path=tmp_path / "missing-refresh-status.json",
    )

    assert result["status"] == "drift"
    assert result["exit_code"] == 2
    assert {error["code"] for error in result["errors"]} == {"catalog_refresh_stale"}
    assert target.read_text(encoding="utf-8") == "before\n"


def test_verify_invalid_result_does_not_add_catalog_staleness_noise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_plan(**_kwargs: Any) -> Any:
        raise client_sync.ClientSyncError("manifest invalid")

    monkeypatch.setattr(client_sync, "_plan", fail_plan)

    result = client_sync.run(
        "verify",
        status_path=tmp_path / "missing-refresh-status.json",
    )

    assert result["status"] == "invalid"
    assert result["exit_code"] == 3
    assert result["catalog_refresh"]["stale"] is True
    assert {error["code"] for error in result["errors"]} == {"validation_failed"}


def test_rtk_hook_preservation_fails_closed() -> None:
    before = file_state(
        json.dumps({"hooks": {"PreToolUse": [{"command": "rtk rewrite"}]}})
    )
    after = file_state(json.dumps({"hooks": {"PreToolUse": []}}))
    with pytest.raises(client_sync.ClientSyncError) as caught:
        client_sync._validate_rtk_hook_preservation(
            client_sync.PreparedMutation(Path("settings.json"), before, after)
        )
    assert caught.value.code == "rtk_hook_disabled"


def test_post_apply_readbacks_validate_rtk_launchers_and_host_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _executable(tmp_path / "rtk")
    link = tmp_path / "home/.headroom/bin/rtk"
    link.parent.mkdir(parents=True)
    link.symlink_to(executable)
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    claude = _executable(tmp_path / "claude")
    manifest = client_sync.load_supported_surface_manifest()
    for launcher, catalog in manifest["claude_launchers"].items():
        path = launcher_dir / launcher
        path.write_text(
            client_sync.claude_code_sync._render_launcher(claude, catalog),
            encoding="utf-8",
        )
        path.chmod(0o755)
    plan = _readback_plan(
        manifest=manifest,
        link=link,
        executable=executable,
    )
    observed: list[tuple[list[str], str]] = []

    def capture_run(*args: Any, **kwargs: Any) -> Any:
        observed.append((args[0], kwargs["env"]["PATH"]))
        return client_sync.subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(client_sync.subprocess, "run", capture_run)
    statuses = {group: "current" for group in client_sync.EXPECTED_GROUPS}
    assert (
        client_sync._post_apply_readback_errors(
            plan,
            launcher_dir=launcher_dir,
            host_path="/host/exact/path",
            statuses=statuses,
        )
        == []
    )
    assert observed == [
        *[
            (["/bin/sh", "-n", str(launcher_dir / launcher)], "/host/exact/path")
            for launcher in manifest["claude_launchers"]
        ],
        (["codex", "--version"], "/host/exact/path"),
        (["claude", "--version"], "/host/exact/path"),
    ]


def test_codex_launcher_readback_freezes_localhost_port_and_default_catalog(
    tmp_path: Path,
) -> None:
    executable = _executable(tmp_path / "rtk")
    link = tmp_path / "home/.headroom/bin/rtk"
    link.parent.mkdir(parents=True)
    link.symlink_to(executable)
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    launcher = launcher_dir / "claude-codex"
    launcher.write_text(
        client_sync.claude_code_sync._render_launcher(
            _executable(tmp_path / "claude"),
            "codex",
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    text = launcher.read_text(encoding="utf-8")
    assert "http://127.0.0.1:64946" in text
    assert "x-reverso-model-catalog: codex" in text
    plan = _readback_plan(
        manifest=client_sync.load_supported_surface_manifest(),
        link=link,
        executable=executable,
    )
    statuses = {
        group: ("current" if group == "provider-codex" else "preserved")
        for group in client_sync.EXPECTED_GROUPS
    }
    assert (
        client_sync._post_apply_readback_errors(
            plan,
            launcher_dir=launcher_dir,
            host_path=os.environ.get("PATH", ""),
            statuses=statuses,
        )
        == []
    )


@pytest.mark.parametrize(
    ("damage", "expected_code"),
    [
        ("rtk", "rtk_readback_failed"),
        ("marker", "launcher_readback_failed"),
        ("syntax", "launcher_smoke_failed"),
    ],
)
def test_post_apply_readback_failures_are_bounded(
    tmp_path: Path,
    damage: str,
    expected_code: str,
) -> None:
    executable = _executable(tmp_path / "rtk")
    link = tmp_path / "home/.headroom/bin/rtk"
    link.parent.mkdir(parents=True)
    link.symlink_to(executable)
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    claude = _executable(tmp_path / "claude")
    manifest = client_sync.load_supported_surface_manifest()
    for launcher, catalog in manifest["claude_launchers"].items():
        path = launcher_dir / launcher
        path.write_text(
            client_sync.claude_code_sync._render_launcher(claude, catalog),
            encoding="utf-8",
        )
        path.chmod(0o755)
    if damage == "rtk":
        link.unlink()
    elif damage == "marker":
        target = launcher_dir / "claude-codex"
        target.write_text(
            target.read_text().replace(
                client_sync.claude_code_sync.LAUNCHER_MANAGED_MARKER,
                "# unmanaged",
            ),
            encoding="utf-8",
        )
    else:
        with (launcher_dir / "claude-codex").open("a", encoding="utf-8") as handle:
            handle.write("\n)\n")
    plan = _readback_plan(
        manifest=manifest,
        link=link,
        executable=executable,
    )
    errors = client_sync._post_apply_readback_errors(
        plan,
        launcher_dir=launcher_dir,
        host_path=os.environ.get("PATH", ""),
        statuses={group: "current" for group in client_sync.EXPECTED_GROUPS},
    )
    assert expected_code in {error["code"] for error in errors}
    assert all(error["message"] == error["code"] for error in errors)


@pytest.mark.parametrize(
    ("command", "failure", "expected_code"),
    [
        ("codex", "nonzero", "codex_smoke_failed"),
        ("codex", "missing", "codex_smoke_failed"),
        ("codex", "timeout", "codex_smoke_failed"),
        ("claude", "nonzero", "claude_smoke_failed"),
        ("claude", "missing", "claude_smoke_failed"),
        ("claude", "timeout", "claude_smoke_failed"),
    ],
)
def test_client_smoke_failures_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    failure: str,
    expected_code: str,
) -> None:
    executable = _executable(tmp_path / "rtk")
    link = tmp_path / "home/.headroom/bin/rtk"
    link.parent.mkdir(parents=True)
    link.symlink_to(executable)
    manifest = client_sync.load_supported_surface_manifest()
    plan = _readback_plan(manifest=manifest, link=link, executable=executable)
    plan.paths["shared-codex-config"] = [tmp_path / "config.toml"]
    plan.paths["shared-claude-settings"] = [tmp_path / "settings.json"]

    def fail_client(argv: list[str], **_kwargs: Any) -> Any:
        if argv[0] != command:
            return client_sync.subprocess.CompletedProcess(argv, 0)
        if failure == "nonzero":
            return client_sync.subprocess.CompletedProcess(
                argv,
                1,
                stdout=b"secret stdout",
                stderr=b"secret stderr",
            )
        if failure == "missing":
            raise FileNotFoundError(command)
        raise client_sync.subprocess.TimeoutExpired(
            argv,
            5,
            output=b"secret stdout",
            stderr=b"secret stderr",
        )

    monkeypatch.setattr(client_sync.subprocess, "run", fail_client)
    errors = client_sync._post_apply_readback_errors(
        plan,
        launcher_dir=tmp_path / "missing-launchers",
        host_path="/host/exact/path",
        statuses={
            group: (
                "current"
                if group in {"shared-codex-config", "shared-claude-settings"}
                else "preserved"
            )
            for group in client_sync.EXPECTED_GROUPS
        },
    )

    assert errors == [
        {
            "code": expected_code,
            "group": f"shared-{command}-"
            + ("config" if command == "codex" else "settings"),
            "path": str(
                tmp_path / ("config.toml" if command == "codex" else "settings.json")
            ),
            "message": expected_code,
        }
    ]


def test_refresh_status_record_has_exact_governed_schema() -> None:
    result = {
        "started_at": "2026-07-30T00:00:00+00:00",
        "finished_at": "2026-07-30T00:00:01+00:00",
        "status": "partial_freshness",
        "exit_code": 4,
        "groups": [
            {"id": "provider-claude", "status": "preserved"},
            {"id": "provider-copilot", "status": "changed"},
        ],
        "errors": [
            {"code": "provider_stale"},
            {"code": "provider_stale"},
        ],
    }
    status = client_sync._refresh_status_record(
        result,
        prior={"last_success_at": "2026-07-29T23:00:00+00:00"},
    )
    assert list(status) == [
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
    assert status["status"] == "partial_freshness"
    assert status["duration_ms"] == 1000
    assert status["last_success_at"] == "2026-07-29T23:00:00+00:00"
    assert status["provider_results"] == {
        "claude": "stale",
        "copilot": "changed",
        "auggie": "skipped",
        "deepseek": "skipped",
        "kimi": "skipped",
    }
    assert status["error_codes"] == ["provider_stale"]


def test_refresh_status_write_is_atomic_and_lock_skipped_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "state/catalog-refresh-status.json"
    client_sync._write_refresh_status(
        status_path,
        {
            "schema_version": 1,
            "status": "success",
            "last_success_at": "2026-07-30T00:00:00+00:00",
        },
    )
    persisted = status_path.read_bytes()
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600

    @contextmanager
    def busy_lock(**kwargs: Any) -> Iterator[None]:
        raise ClientSyncLockBusy("busy")
        yield

    monkeypatch.setattr(client_sync, "acquire_client_sync_lock", busy_lock)
    result = client_sync.run(
        "refresh",
        lock_path=tmp_path / "sync.lock",
        status_path=status_path,
    )
    assert result["status"] == "lock_skipped"
    assert status_path.read_bytes() == persisted
    client_sync._catalog_refresh(
        path=status_path,
        observed_at="2026-07-31T00:00:00+00:00",
    )
    assert status_path.read_bytes() == persisted


def test_refresh_status_parent_symlink_swap_never_reaches_attacker(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "state/catalog-refresh-status.json"
    state_dir = status_path.parent
    attacker = tmp_path / "attacker"
    attacker.mkdir()

    def swap_parent(_mutation: Any, index: int) -> None:
        if index == 1:
            state_dir.rmdir()
            state_dir.symlink_to(attacker, target_is_directory=True)

    with pytest.raises(PreparedRollbackFailed):
        client_sync._write_refresh_status(
            status_path,
            {"schema_version": 1, "status": "success"},
            observer=swap_parent,
        )

    assert list(attacker.iterdir()) == []
    assert state_dir.is_symlink()
    assert state_dir.readlink() == attacker


def test_overlapping_refresh_cannot_overwrite_status_out_of_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "status.json"
    lock_active = False
    first_started = threading.Event()
    release_first = threading.Event()

    @contextmanager
    def single_flight_lock(**kwargs: Any) -> Iterator[Any]:
        nonlocal lock_active
        if lock_active:
            raise ClientSyncLockBusy("busy")
        lock_active = True
        try:
            yield object()
        finally:
            lock_active = False

    def apply_result(
        mode: str,
        started_at: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        first_started.set()
        assert release_first.wait(timeout=5)
        return client_sync._result(mode, "success", 0, started_at)

    original_write = client_sync._write_refresh_status

    def write_while_locked(path: Path, status: dict[str, Any]) -> None:
        assert lock_active is True
        original_write(path, status)

    monkeypatch.setattr(client_sync, "acquire_client_sync_lock", single_flight_lock)
    monkeypatch.setattr(client_sync, "_plan", lambda **_: object())
    monkeypatch.setattr(client_sync, "_apply_result", apply_result)
    monkeypatch.setattr(client_sync, "_write_refresh_status", write_while_locked)

    first_result: list[dict[str, Any]] = []
    first = threading.Thread(
        target=lambda: first_result.append(
            client_sync.run("refresh", status_path=status_path)
        )
    )
    first.start()
    assert first_started.wait(timeout=5)

    overlapping = client_sync.run("refresh", status_path=status_path)
    assert overlapping["status"] == "lock_skipped"
    assert not status_path.exists()

    release_first.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert first_result[0]["status"] == "success"
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "success"


def test_lock_skipped_refresh_preserves_last_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_success_at": "2026-07-30T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    @contextmanager
    def busy_lock(**kwargs: Any) -> Iterator[None]:
        raise ClientSyncLockBusy("busy")
        yield

    monkeypatch.setattr(client_sync, "acquire_client_sync_lock", busy_lock)
    client_sync.run(
        "refresh",
        lock_path=tmp_path / "sync.lock",
        status_path=status_path,
    )
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["last_success_at"] == "2026-07-30T00:00:00+00:00"


def test_result_records_have_exact_frozen_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    rtk = _executable(tmp_path / "rtk")
    monkeypatch.setattr(
        client_sync.codex_sync,
        "prepare_sync",
        lambda **kwargs: _prepared_codex(
            target=kwargs["target"],
            catalog_dir=kwargs["catalog_dir"],
            changed=False,
        ),
    )
    _patch_provider_pipeline(monkeypatch)
    monkeypatch.setattr(
        client_sync.claude_code_sync,
        "prepare_sync",
        lambda settings_path, **kwargs: _prepared_claude(
            launcher_dir=kwargs["launcher_dir"],
            changed=False,
        ),
    )

    result = client_sync.run(
        "dry-run",
        codex_config=tmp_path / "config.toml",
        claude_config_dir=tmp_path / "claude",
        catalog_dir=tmp_path / "catalogs",
        launch_agent_dir=tmp_path / "bin",
        rtk_bin=rtk,
        home=home,
    )

    assert list(result) == client_sync.RESULT_FIELDS
    assert all(
        list(group) == ["id", "kind", "status", "dependencies", "paths"]
        for group in result["groups"]
    )
    assert all(
        list(surface) == ["id", "kind", "status", "paths"]
        for surface in result["surfaces"]
    )
    assert all(
        list(path)
        == [
            "path",
            "group",
            "owner",
            "status",
            "before_sha256",
            "after_sha256",
        ]
        for path in result["paths"]
    )
    assert result["surfaces"] == sorted(
        result["surfaces"], key=lambda surface: surface["id"]
    )
