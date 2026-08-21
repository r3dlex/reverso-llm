"""Manifest-driven Codex, Claude Code, and RTK client convergence command."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reverso import claude_code_sync, codex_sync, ollama_convergence
from reverso.client_sync_lock import (
    ClientSyncLockBusy,
    HeldClientSyncLock,
    acquire_client_sync_lock,
    validate_client_sync_lock,
)
from reverso.client_sync_mutations import (
    FileState,
    MutationObserver,
    PreparedApplyFailed,
    PreparedGroup,
    PreparedMutation,
    PreparedRollbackFailed,
    PreparedStateChanged,
    apply_prepared_group,
    capture_state,
    directory_state,
    file_state,
    missing_parent_mutations,
    prepared_mutation,
    symlink_state,
    validate_prepared_group,
)
from reverso.protocols import model_exposure
from reverso.protocols.adapters.ollama.auth import OllamaAuthState

COMMAND = "reverso-client-sync"
SUPPORTED_SURFACE_MANIFEST = (
    Path(__file__).resolve().parents[2] / "config" / "supported-client-surfaces.json"
)
DEFAULT_CATALOG_REFRESH_STATUS_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "reverso"
    / "catalog-refresh-status.json"
)
RESULT_FIELDS = [
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
EXPECTED_GROUPS = {
    "claude-roots": ("prerequisite", ()),
    "codex-roots": ("prerequisite", ()),
    "direct-minimax": ("provider", ()),
    "direct-openai": ("provider", ()),
    "external-agy": ("provider", ()),
    "provider-auggie": ("provider", ()),
    "provider-claude": ("provider", ()),
    "provider-codex": ("provider", ()),
    "provider-codex-direct": ("provider", ()),
    "provider-copilot": ("provider", ()),
    "provider-deepseek": ("provider", ()),
    "provider-kimi": ("provider", ()),
    "provider-ollama": ("provider", ()),
    "provider-openai-pass-through": ("provider", ()),
    "rtk": ("prerequisite", ()),
    "shared-claude-settings": ("shared_dependency", ()),
    "shared-codex-config": (
        "shared_dependency",
        (
            "provider-auggie",
            "provider-claude",
            "provider-codex-direct",
            "provider-copilot",
            "provider-deepseek",
            "provider-kimi",
            "provider-ollama",
            "provider-openai-pass-through",
        ),
    ),
    "shared-codex-cleanup": (
        "shared_dependency",
        (
            "provider-auggie",
            "provider-claude",
            "provider-codex-direct",
            "provider-copilot",
            "provider-deepseek",
            "provider-kimi",
            "provider-ollama",
            "provider-openai-pass-through",
        ),
    ),
    "shared-reverso-launcher": (
        "shared_dependency",
        (
            "provider-auggie",
            "provider-claude",
            "provider-codex",
            "provider-copilot",
            "provider-deepseek",
            "provider-kimi",
            "provider-ollama",
        ),
    ),
}
EXPECTED_SURFACES = {
    "claude-auggie": (
        "provider_launcher",
        "provider-auggie",
        "auggie-<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-auggie",
        None,
    ),
    "claude-claude": (
        "provider_launcher",
        "provider-claude",
        "<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-claude",
        None,
    ),
    "claude-codex": (
        "provider_launcher",
        "provider-codex",
        "gpt-*",
        "codex",
        "reverso_managed",
        "codex",
        "<launch_agent_dir>/claude-codex",
        None,
    ),
    "claude-copilot": (
        "provider_launcher",
        "provider-copilot",
        "copilot-<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-copilot",
        None,
    ),
    "claude-deepseek": (
        "provider_launcher",
        "provider-deepseek",
        "<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-deepseek",
        None,
    ),
    "claude-kimi": (
        "provider_launcher",
        "provider-kimi",
        "kimi-k3",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-kimi",
        None,
    ),
    "claude-ollama": (
        "provider_launcher",
        "provider-ollama",
        "anthropic-ollama-<raw-model-id>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-ollama",
        None,
    ),
    "claude-reverso": (
        "shared_launcher",
        "shared-reverso-launcher",
        "<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<launch_agent_dir>/claude-reverso",
        None,
    ),
    "codex-agy": (
        "external_catalog",
        "external-agy",
        "agy/<model>",
        "external",
        "external_user_preserving",
        "external",
        "<catalog_dir>/agy.json",
        None,
    ),
    "codex-builtin-openai": (
        "direct_profile",
        "direct-openai",
        "gpt-*",
        "codex",
        "codex_user_preserving",
        "codex",
        "<codex_config_dir>/openai.config.toml",
        None,
    ),
    "codex-direct": (
        "feature_gated_route",
        "provider-codex-direct",
        "codex-direct/<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-codex-direct.config.toml",
        "REVERSO_CODEX_DIRECT_BACKEND",
    ),
    "codex-minimax": (
        "direct_profile",
        "direct-minimax",
        "MiniMax-*",
        "codex",
        "codex_user_preserving",
        "codex",
        "<codex_config_dir>/minimax.config.toml",
        None,
    ),
    "codex-openai-pass-through": (
        "feature_gated_route",
        "provider-openai-pass-through",
        "openai-pass-through/<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-openai-pass-through.config.toml",
        "REVERSO_OPENAI_BACKEND",
    ),
    "codex-reverso-auggie": (
        "reverso_route",
        "provider-auggie",
        "auggie/<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-auggie.config.toml",
        None,
    ),
    "codex-reverso-claude": (
        "reverso_route",
        "provider-claude",
        "<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-claude.config.toml",
        None,
    ),
    "codex-reverso-copilot": (
        "reverso_route",
        "provider-copilot",
        "copilot/<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-copilot.config.toml",
        None,
    ),
    "codex-reverso-deepseek": (
        "reverso_route",
        "provider-deepseek",
        "<model>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-deepseek.config.toml",
        None,
    ),
    "codex-reverso-kimi": (
        "reverso_route",
        "provider-kimi",
        "kimi-k3",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-kimi.config.toml",
        None,
    ),
    "codex-reverso-ollama": (
        "reverso_route",
        "provider-ollama",
        "<raw-model-id>",
        "runtime",
        "reverso_managed",
        "reverso",
        "<codex_config_dir>/reverso-ollama.config.toml",
        None,
    ),
}
_RTK_MARKER = "Managed by reverso-client-sync.\n"


class ClientSyncError(RuntimeError):
    """A bounded, operator-actionable client convergence failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "validation_failed",
        status: str = "invalid",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class RtkPlan:
    """Validated RTK link convergence plan."""

    executable: Path
    headroom_dir: Path
    bin_dir: Path
    link: Path
    marker: Path
    create_directories: tuple[Path, ...]
    replace_link: bool
    create_marker: bool
    group: PreparedGroup

    @property
    def changed(self) -> bool:
        return bool(self.create_directories or self.replace_link or self.create_marker)


@dataclass(frozen=True)
class _ConvergencePlan:
    manifest: dict[str, Any]
    groups: dict[str, PreparedGroup]
    rtk: RtkPlan
    paths: dict[str, list[Path]]
    provider_errors: dict[str, str]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def load_supported_surface_manifest(
    path: Path = SUPPORTED_SURFACE_MANIFEST,
) -> dict[str, Any]:
    """Load the repository-owned client support authority."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ClientSyncError("supported surface manifest must be a JSON object")
    return value


def validate_supported_surface_manifest(manifest: dict[str, Any]) -> None:
    """Fail closed when client support data drifts from runtime authorities."""
    if manifest.get("schema_version") != 1:
        raise ClientSyncError("unsupported supported-surface manifest schema")
    if manifest.get("reverso_routes") != list(
        model_exposure.REVERSO_ROUTED_CODEX_PROFILE_PREFIXES
    ):
        raise ClientSyncError("manifest Reverso routes drift from model exposure")
    direct = {
        spec.prefix: spec.model_provider
        for spec in model_exposure.DIRECT_CODEX_PROFILE_SPECS
    }
    if manifest.get("direct_codex_profiles") != direct:
        raise ClientSyncError("manifest direct profiles drift from model exposure")
    feature_gated = {
        "codex-direct": model_exposure.CODEX_DIRECT_BACKEND_ENV,
        "openai-pass-through": model_exposure.OPENAI_BACKEND_ENV,
    }
    if manifest.get("feature_gated_routes") != feature_gated:
        raise ClientSyncError("manifest feature-gated routes drift from model exposure")
    launchers = dict(claude_code_sync.LAUNCHER_CATALOGS)
    if manifest.get("claude_launchers") != launchers:
        raise ClientSyncError("manifest Claude launchers drift from launcher catalog")
    external = manifest.get("external_catalogs", {}).get("agy")
    if not isinstance(external, dict) or (
        external.get("runtime_route") is not False
        or external.get("gateway_fetch") is not False
        or external.get("selector_template") != "agy/<model>"
        or external.get("ownership") != "external_user_preserving"
        or external.get("runtime_authority") != "external"
    ):
        raise ClientSyncError(
            "AGY must remain external and must not be gateway-fetched"
        )
    groups = manifest.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ClientSyncError("supported-surface manifest requires groups")
    group_ids = [group.get("id") for group in groups if isinstance(group, dict)]
    if len(group_ids) != len(groups) or len(group_ids) != len(set(group_ids)):
        raise ClientSyncError("supported-surface manifest has invalid groups")
    group_id_set = set(group_ids)
    for group in groups:
        if group.get("kind") not in {"provider", "shared_dependency", "prerequisite"}:
            raise ClientSyncError(
                "supported-surface manifest has an invalid group kind"
            )
        dependencies = group.get("dependencies")
        if (
            not isinstance(dependencies, list)
            or dependencies != sorted(set(dependencies))
            or any(dependency not in group_id_set for dependency in dependencies)
        ):
            raise ClientSyncError("supported-surface manifest has invalid dependencies")
    actual_groups = {
        group["id"]: (group["kind"], tuple(group["dependencies"])) for group in groups
    }
    if actual_groups != EXPECTED_GROUPS:
        raise ClientSyncError("supported-surface manifest group contract drift")
    surfaces = manifest.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise ClientSyncError("supported-surface manifest requires surfaces")
    surface_ids = [surface.get("id") for surface in surfaces]
    if len(surface_ids) != len(set(surface_ids)):
        raise ClientSyncError("supported-surface manifest has duplicate surface ids")
    required_surface_fields = {
        "id",
        "kind",
        "group",
        "selector_template",
        "default_model_authority",
        "ownership",
        "runtime_authority",
        "path_template",
        "feature_gate",
    }
    if any(
        not isinstance(surface, dict)
        or set(surface) != required_surface_fields
        or surface.get("group") not in group_id_set
        for surface in surfaces
    ):
        raise ClientSyncError("supported-surface manifest has an invalid group")
    expected_surface_ids = {
        "codex-builtin-openai",
        "codex-minimax",
        "codex-agy",
        *(f"codex-reverso-{prefix}" for prefix in manifest["reverso_routes"]),
        "codex-direct",
        "codex-openai-pass-through",
        *manifest["claude_launchers"],
    }
    if set(surface_ids) != expected_surface_ids:
        raise ClientSyncError("supported-surface manifest surface inventory drift")
    actual_surfaces = {
        surface["id"]: (
            surface["kind"],
            surface["group"],
            surface["selector_template"],
            surface["default_model_authority"],
            surface["ownership"],
            surface["runtime_authority"],
            surface["path_template"],
            surface["feature_gate"],
        )
        for surface in surfaces
    }
    if actual_surfaces != EXPECTED_SURFACES:
        raise ClientSyncError("supported-surface manifest surface contract drift")


def _usable_executable(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return False
    return resolved.is_file() and os.access(resolved, os.X_OK)


def resolve_rtk_executable(
    explicit: Path | None,
    *,
    host_path: str | None = None,
) -> Path:
    """Resolve one exact regular RTK executable, with explicit-path precedence."""
    if explicit is not None:
        if not _usable_executable(explicit):
            raise ClientSyncError(f"invalid RTK executable: {explicit.expanduser()}")
        return explicit.expanduser().resolve()

    candidates: set[Path] = set()
    for raw_directory in (
        host_path if host_path is not None else os.environ.get("PATH", "")
    ).split(os.pathsep):
        directory = Path(raw_directory or os.curdir).expanduser()
        candidate = directory / "rtk"
        if _usable_executable(candidate):
            candidates.add(candidate.resolve())
    if not candidates:
        raise ClientSyncError("RTK executable not found on the exact host PATH")
    if len(candidates) != 1:
        raise ClientSyncError(
            "multiple distinct RTK executables found; use --rtk-bin explicitly"
        )
    return next(iter(candidates))


def _real_directory_or_absent(path: Path) -> bool:
    return (
        not path.exists()
        and not path.is_symlink()
        or (path.is_dir() and not path.is_symlink())
    )


def plan_rtk_convergence(executable: Path, *, home: Path | None = None) -> RtkPlan:
    """Validate parents and plan the marker-owned Headroom RTK symlink."""
    resolved = resolve_rtk_executable(executable)
    real_home = (home or Path.home()).expanduser()
    if not real_home.is_dir() or real_home.is_symlink():
        raise ClientSyncError(f"home must be a real directory: {real_home}")
    headroom_dir = real_home / ".headroom"
    bin_dir = headroom_dir / "bin"
    for parent in (headroom_dir, bin_dir):
        if not _real_directory_or_absent(parent):
            raise ClientSyncError(
                f"RTK parent must be a real directory: {parent}",
                code="ownership_conflict",
                status="ownership_conflict",
            )
    create_directories = tuple(
        parent for parent in (headroom_dir, bin_dir) if not parent.exists()
    )
    link = bin_dir / "rtk"
    marker = bin_dir / ".reverso-rtk-owner"
    marker_owned = (
        marker.is_file()
        and not marker.is_symlink()
        and marker.read_text(encoding="utf-8") == _RTK_MARKER
    )
    resolved_before = capture_state(resolved)
    home_before = capture_state(real_home)
    preconditions = [
        PreparedMutation(resolved, resolved_before, resolved_before),
        PreparedMutation(real_home, home_before, home_before),
    ]
    for parent in (headroom_dir, bin_dir):
        before = capture_state(parent)
        after = directory_state() if before.kind == "absent" else before
        preconditions.append(PreparedMutation(parent, before, after))
    if link.is_symlink():
        try:
            current_target = link.resolve(strict=True)
        except OSError as exc:
            raise ClientSyncError(
                f"RTK symlink conflict at {link}",
                code="ownership_conflict",
                status="ownership_conflict",
            ) from exc
        if current_target == resolved:
            link_before = capture_state(link)
            if marker_owned:
                marker_before = capture_state(marker)
                preconditions.append(
                    PreparedMutation(marker, marker_before, marker_before)
                )
            group = PreparedGroup(
                "rtk",
                (
                    *preconditions,
                    PreparedMutation(link, link_before, link_before),
                ),
            )
            return RtkPlan(
                resolved,
                headroom_dir,
                bin_dir,
                link,
                marker,
                create_directories,
                False,
                False,
                group,
            )
        raise ClientSyncError(
            f"RTK symlink conflict at {link}",
            code="ownership_conflict",
            status="ownership_conflict",
        )
    elif link.exists():
        raise ClientSyncError(
            f"RTK path conflict at {link}",
            code="ownership_conflict",
            status="ownership_conflict",
        )
    if (marker.exists() or marker.is_symlink()) and not marker_owned:
        raise ClientSyncError(
            f"RTK ownership marker conflict at {marker}",
            code="ownership_conflict",
            status="ownership_conflict",
        )
    mutations: list[PreparedMutation] = preconditions
    mutations.extend(
        (
            prepared_mutation(link, symlink_state(resolved)),
            prepared_mutation(marker, file_state(_RTK_MARKER)),
        )
    )
    return RtkPlan(
        resolved,
        headroom_dir,
        bin_dir,
        link,
        marker,
        create_directories,
        True,
        True,
        PreparedGroup("rtk", tuple(mutations)),
    )


def apply_rtk_convergence(plan: RtkPlan) -> None:
    """Apply a previously validated RTK plan without traversing symlink parents."""
    apply_prepared_group(plan.group)


def _state_sha256(state: FileState) -> str | None:
    if state.kind == "symlink" and isinstance(state.data, str):
        return hashlib.sha256(state.data.encode()).hexdigest()
    if state.kind == "file" and isinstance(state.data, bytes):
        return hashlib.sha256(state.data).hexdigest()
    return None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _catalog_refresh(
    *,
    path: Path = DEFAULT_CATALOG_REFRESH_STATUS_PATH,
    observed_at: str | None = None,
) -> dict[str, Any]:
    observed_text = observed_at or _timestamp()
    observed = _parse_timestamp(observed_text)
    persisted: dict[str, Any] = {}
    try:
        loaded = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    else:
        if isinstance(loaded, dict) and loaded.get("schema_version") == 1:
            persisted = loaded
    last_success_at = persisted.get("last_success_at")
    last_success = _parse_timestamp(last_success_at)
    stale = (
        observed is None
        or last_success is None
        or observed - last_success > timedelta(hours=14)
    )
    return {
        "last_attempt_at": persisted.get("last_attempt_at"),
        "last_success_at": last_success_at,
        "stored_stale": persisted.get("stale"),
        "stored_stale_observed_at": persisted.get("stale_observed_at"),
        "stale": stale,
        "observed_at": observed_text,
    }


def _refresh_status_record(
    result: dict[str, Any],
    *,
    prior: dict[str, Any],
) -> dict[str, Any]:
    observed_at = result["finished_at"]
    started = _parse_timestamp(result["started_at"])
    finished = _parse_timestamp(observed_at)
    duration_ms = (
        max(0, round((finished - started).total_seconds() * 1000))
        if started is not None and finished is not None
        else None
    )
    if result["status"] in {"success", "no_op"}:
        status = "success"
    elif result["status"] == "lock_skipped":
        status = "lock_skipped"
    elif result["status"] == "partial_freshness":
        status = "partial_freshness"
    else:
        status = "failed"
    last_success_at = (
        observed_at if status == "success" else prior.get("last_success_at")
    )
    last_success = _parse_timestamp(last_success_at)
    observed = _parse_timestamp(observed_at)
    stale = (
        observed is None
        or last_success is None
        or observed - last_success > timedelta(hours=14)
    )
    group_statuses = {group["id"]: group["status"] for group in result["groups"]}
    provider_results: dict[str, str] = {}
    for prefix in model_exposure.REVERSO_ROUTED_CODEX_PROFILE_PREFIXES:
        if not isinstance(prefix, str):
            raise ClientSyncError("runtime provider prefix must be a string")
        group_status = group_statuses.get(f"provider-{prefix}")
        provider_status = group_status if isinstance(group_status, str) else ""
        provider_results[prefix] = {
            "current": "current",
            "changed": "changed",
            "preserved": "stale",
            "invalid": "invalid",
            "rolled_back": "invalid",
        }.get(provider_status, "skipped")
    return {
        "schema_version": 1,
        "status": status,
        "last_attempt_at": result["started_at"],
        "last_success_at": last_success_at,
        "duration_ms": duration_ms,
        "exit_code": result["exit_code"],
        "stale": stale,
        "stale_observed_at": observed_at,
        "provider_results": provider_results,
        "error_codes": sorted({error["code"] for error in result["errors"]})[:16],
    }


def _write_refresh_status(
    path: Path,
    status: dict[str, Any],
    *,
    observer: MutationObserver | None = None,
) -> None:
    target = path.expanduser()
    before = capture_state(target)
    if before.kind not in {"absent", "file"}:
        raise OSError(f"refresh status path must be a real file: {target}")
    encoded = (json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    target_mutation = PreparedMutation(
        target,
        before,
        file_state(encoded, 0o600),
    )
    parents = missing_parent_mutations((target,))
    apply_prepared_group(
        PreparedGroup("refresh-status", (*parents, target_mutation)),
        observer=observer,
    )


def _load_refresh_status(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return (
        loaded if isinstance(loaded, dict) and loaded.get("schema_version") == 1 else {}
    )


def _result(
    mode: str,
    status: str,
    exit_code: int,
    started_at: str,
    *,
    groups: list[dict[str, Any]] | None = None,
    surfaces: list[dict[str, Any]] | None = None,
    paths: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return dict(
        zip(
            RESULT_FIELDS,
            (
                1,
                COMMAND,
                mode,
                status,
                exit_code,
                started_at,
                _timestamp(),
                sorted(groups or [], key=lambda item: item["id"]),
                sorted(surfaces or [], key=lambda item: item["id"]),
                sorted(paths or [], key=lambda item: item["path"]),
                _catalog_refresh(),
                sorted(
                    errors or [],
                    key=lambda item: (
                        item.get("code", ""),
                        item.get("group") or "",
                        item.get("path") or "",
                    ),
                ),
            ),
            strict=True,
        )
    )


def _path_record(
    path: Path,
    group: str,
    owner: str,
    status: str,
    *,
    before_sha256: str | None = None,
    after_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "group": group,
        "owner": owner,
        "status": status,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
    }


def _manifest_group_metadata(
    manifest: dict[str, Any],
) -> dict[str, tuple[str, list[str]]]:
    return {
        group["id"]: (group["kind"], group["dependencies"])
        for group in manifest["groups"]
    }


def _codex_mutation_group(
    mutation: PreparedMutation,
    *,
    target: Path,
    catalog_dir: Path,
    active_prefixes: tuple[str, ...],
) -> str:
    path = mutation.path
    if path == target or path.name.startswith(f"{target.name}.reverso-sync."):
        return "shared-codex-config"
    if any(
        path == root or root.is_relative_to(path)
        for root in (target.parent, catalog_dir)
    ):
        return "codex-roots"
    archive_dir = target.parent / codex_sync.PROFILE_ARCHIVE_DIR
    if archive_dir.is_relative_to(path):
        return "codex-roots"
    if path == archive_dir or path.is_relative_to(archive_dir):
        return "shared-codex-cleanup"
    for direct in ("openai", "minimax"):
        if path.name.startswith(f"{direct}.config.toml"):
            return f"direct-{direct}"
    for prefix in active_prefixes:
        profile_name = f"reverso-{prefix}.config.toml"
        if path.name == profile_name or path.name.startswith(
            f"{profile_name}{codex_sync.BACKUP_SUFFIX_PREFIX}"
        ):
            return (
                "shared-codex-cleanup"
                if mutation.after.kind == "absent"
                else f"provider-{prefix}"
            )
        if path.parent == catalog_dir and path.name == f"{prefix}.json":
            return (
                "shared-codex-cleanup"
                if mutation.after.kind == "absent"
                else f"provider-{prefix}"
            )
    if (
        path.parent == target.parent
        and path.name.endswith(".config.toml")
        and mutation.after.kind == "absent"
    ):
        return "shared-codex-cleanup"
    raise ClientSyncError(f"unmapped Codex prepared path: {path}")


def _claude_mutation_group(
    path: Path,
    *,
    settings_path: Path,
    launcher_dir: Path,
) -> str:
    if path == settings_path or path.name.startswith(
        f"{settings_path.name}{claude_code_sync.BACKUP_SUFFIX_PREFIX}"
    ):
        return "shared-claude-settings"
    if any(
        path == root or root.is_relative_to(path)
        for root in (launcher_dir, settings_path.parent)
    ):
        return "claude-roots"
    if path.parent == launcher_dir and path.name == "claude-reverso":
        return "shared-reverso-launcher"
    if path.parent == launcher_dir and path.name.startswith("claude-"):
        catalog = dict(claude_code_sync.LAUNCHER_CATALOGS).get(path.name)
        if catalog is None:
            raise ClientSyncError(f"unmapped Claude prepared path: {path}")
        return f"provider-{catalog}"
    raise ClientSyncError(f"unmapped Claude prepared path: {path}")


def _append_group_mutations(
    destination: dict[str, list[PreparedMutation]],
    group: str,
    mutations: list[PreparedMutation],
) -> None:
    existing = {mutation.path: mutation for mutation in destination[group]}
    for mutation in mutations:
        prior = existing.get(mutation.path)
        if prior is not None and prior != mutation:
            raise ClientSyncError(
                f"conflicting prepared candidates for {mutation.path}"
            )
        if prior is None:
            destination[group].append(mutation)
            existing[mutation.path] = mutation


def _contains_rtk_hook(settings: object) -> bool:
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks")

    def contains(value: object) -> bool:
        if isinstance(value, str):
            return "rtk" in value.lower()
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False

    return contains(hooks)


def _validate_rtk_hook_preservation(
    mutation: PreparedMutation,
) -> None:
    if mutation.before.kind != "file" or mutation.after.kind != "file":
        return
    before_data = mutation.before.data
    after_data = mutation.after.data
    if not isinstance(before_data, bytes) or not isinstance(after_data, bytes):
        return
    try:
        before = json.loads(before_data.decode())
        after = json.loads(after_data.decode())
    except (UnicodeDecodeError, ValueError):
        return
    if _contains_rtk_hook(before) and not _contains_rtk_hook(after):
        raise ClientSyncError(
            "Claude RTK hook would be disabled",
            code="rtk_hook_disabled",
        )


def _validate_claude_launcher_candidates(
    prepared: claude_code_sync.PreparedClaudeCodeSync,
    *,
    launcher_dir: Path,
) -> None:
    launcher_paths = {
        launcher_dir / name for name, _catalog in claude_code_sync.LAUNCHER_CATALOGS
    }
    for mutation in prepared.group.mutations:
        if mutation.path not in launcher_paths:
            continue
        data = mutation.after.data
        if mutation.after.kind != "file" or not isinstance(data, bytes):
            raise ClientSyncError(
                f"invalid Claude launcher candidate: {mutation.path}",
                code="launcher_candidate_invalid",
            )
        try:
            syntax = subprocess.run(
                ["/bin/sh", "-n"],
                input=data,
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientSyncError(
                f"invalid Claude launcher candidate: {mutation.path}",
                code="launcher_candidate_invalid",
            ) from exc
        if syntax.returncode != 0:
            raise ClientSyncError(
                f"invalid Claude launcher candidate: {mutation.path}",
                code="launcher_candidate_invalid",
            )


def _external_agy_catalog(codex_config: Path, catalog_dir: Path) -> Path | None:
    """Discover an existing AGY catalog without fetching or claiming ownership."""
    del catalog_dir
    config_path = codex_config.parent / "agy.config.toml"
    config_state = capture_state(config_path)
    if config_state.kind == "absent":
        return None
    if config_state.kind != "file" or not isinstance(config_state.data, bytes):
        raise ClientSyncError(
            f"AGY profile must be a real file: {config_path}",
            code="external_catalog_conflict",
            status="ownership_conflict",
        )
    try:
        parsed = tomllib.loads(config_state.data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ClientSyncError(
            f"invalid AGY profile: {config_path}",
            code="external_catalog_invalid",
        ) from exc
    if parsed.get("model_provider") != "agy":
        raise ClientSyncError(
            f"AGY profile has unexpected owner: {config_path}",
            code="external_catalog_conflict",
            status="ownership_conflict",
        )
    configured = parsed.get("model_catalog_json")
    if not isinstance(configured, str) or not configured:
        raise ClientSyncError(
            f"AGY profile has no catalog source: {config_path}",
            code="external_catalog_invalid",
        )
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    catalog_state = capture_state(candidate)
    if catalog_state.kind == "absent":
        raise ClientSyncError(
            f"configured AGY catalog is missing: {candidate}",
            code="external_catalog_invalid",
        )
    if catalog_state.kind != "file" or not isinstance(catalog_state.data, bytes):
        raise ClientSyncError(
            f"AGY catalog must be a real file: {candidate}",
            code="external_catalog_invalid",
            status="ownership_conflict",
        )
    try:
        catalog = json.loads(catalog_state.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClientSyncError(
            f"invalid AGY catalog JSON: {candidate}",
            code="external_catalog_invalid",
        ) from exc
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list) or not models:
        raise ClientSyncError(
            f"invalid AGY catalog schema: {candidate}",
            code="external_catalog_invalid",
        )
    for model in models:
        slug = model.get("slug") if isinstance(model, dict) else None
        if (
            not isinstance(slug, str)
            or not slug.startswith("agy/")
            or not slug.removeprefix("agy/")
        ):
            raise ClientSyncError(
                f"AGY catalog contains an unowned model slug: {candidate}",
                code="external_catalog_invalid",
                status="ownership_conflict",
            )
    return candidate


def _plan(
    *,
    codex_config: Path,
    claude_config_dir: Path,
    catalog_dir: Path,
    launch_agent_dir: Path,
    rtk_bin: Path | None,
    home: Path | None,
) -> _ConvergencePlan:
    manifest = load_supported_surface_manifest()
    validate_supported_surface_manifest(manifest)
    rtk = resolve_rtk_executable(rtk_bin)
    rtk_plan = plan_rtk_convergence(rtk, home=home)
    claude_settings = claude_config_dir / "settings.json"
    claude_prepared = claude_code_sync.prepare_sync(
        claude_settings,
        launcher_dir=launch_agent_dir,
    )
    claude_result = claude_prepared.result
    if claude_result.error:
        status = (
            "ownership_conflict"
            if "conflict" in claude_result.error.lower()
            else "invalid"
        )
        raise ClientSyncError(
            claude_result.error,
            code=status,
            status=status,
        )
    _validate_claude_launcher_candidates(
        claude_prepared,
        launcher_dir=launch_agent_dir,
    )
    metadata = _manifest_group_metadata(manifest)
    metadata.update(
        {
            "codex-roots": ("prerequisite", []),
            "claude-roots": ("prerequisite", []),
        }
    )
    group_mutations = {group: [] for group in metadata}
    agy_catalog = _external_agy_catalog(codex_config, catalog_dir)
    if agy_catalog is not None:
        state = capture_state(agy_catalog)
        group_mutations["external-agy"].append(
            PreparedMutation(agy_catalog, state, state)
        )
    provider_errors: dict[str, str] = {}
    active_prefixes = model_exposure.reverso_routed_codex_profile_prefixes()
    discovered: dict[str, codex_sync.ProviderModels] = {}
    provider_prepared: dict[str, codex_sync.PreparedCodexSync] = {}
    ollama_inventory: ollama_convergence.InventoryPlan | None = None
    for prefix in active_prefixes:
        try:
            models = codex_sync.discover_provider_models(prefix)
            if prefix == "ollama":
                auth = OllamaAuthState.from_env()
                ollama_inventory = ollama_convergence.plan_inventory_refresh(
                    ollama_convergence.default_inventory_path(home),
                    local_ids=models.local_models or models.models,
                    cloud_ids=models.cloud_models,
                    cloud_status=(
                        "disabled" if not auth.cloud_requested else models.cloud_status
                    ),
                    observed_at=_timestamp(),
                )
                models = codex_sync.ProviderModels(
                    prefix, ollama_inventory.eligible_model_ids
                )
            prepared = codex_sync.prepare_provider_sync(
                models,
                target=codex_config,
                catalog_dir=catalog_dir,
            )
        except (
            codex_sync.ProviderFreshnessError,
            codex_sync.KimiDiscoveryError,
            codex_sync.ModelDiscoveryError,
        ) as exc:
            provider_errors[f"provider-{prefix}"] = type(exc).__name__
            continue
        discovered[prefix] = models
        provider_prepared[prefix] = prepared

    if ollama_inventory is not None:
        _append_group_mutations(
            group_mutations,
            "provider-ollama",
            [
                *missing_parent_mutations((ollama_inventory.path,)),
                ollama_inventory.mutation,
            ],
        )

    if provider_errors:
        prepared_candidates = [
            *provider_prepared.values(),
            codex_sync.prepare_sync(
                target=codex_config,
                catalog_dir=catalog_dir,
                prefixes=(),
                fetcher=lambda _prefix: [],
            ),
        ]
        allowed_groups = {
            "codex-roots",
            "direct-openai",
            "direct-minimax",
            *(f"provider-{prefix}" for prefix in discovered),
        }
    else:
        prepared_candidates = [
            codex_sync.prepare_sync(
                target=codex_config,
                catalog_dir=catalog_dir,
                prefixes=active_prefixes,
                fetcher=lambda prefix: list(discovered[prefix].models),
            )
        ]
        allowed_groups = set(group_mutations)

    for prepared in prepared_candidates:
        for mutation in prepared.group.mutations:
            group = _codex_mutation_group(
                mutation,
                target=codex_config,
                catalog_dir=catalog_dir,
                active_prefixes=active_prefixes,
            )
            if group in allowed_groups:
                _append_group_mutations(group_mutations, group, [mutation])

    for mutation in claude_prepared.group.mutations:
        if mutation.path == claude_settings:
            _validate_rtk_hook_preservation(mutation)
        group = _claude_mutation_group(
            mutation.path,
            settings_path=claude_settings,
            launcher_dir=launch_agent_dir,
        )
        _append_group_mutations(group_mutations, group, [mutation])
    claude_root_mutations = {
        mutation.path: mutation for mutation in group_mutations["claude-roots"]
    }
    shared_root_paths = set(claude_root_mutations) & {
        mutation.path for mutation in group_mutations["codex-roots"]
    }
    for path in shared_root_paths:
        codex_mutation = next(
            mutation
            for mutation in group_mutations["codex-roots"]
            if mutation.path == path
        )
        if codex_mutation != claude_root_mutations[path]:
            raise ClientSyncError(f"conflicting prepared candidates for {path}")
    group_mutations["codex-roots"] = [
        mutation
        for mutation in group_mutations["codex-roots"]
        if mutation.path not in shared_root_paths
    ]
    group_mutations["rtk"].extend(rtk_plan.group.mutations)
    groups = {
        group: PreparedGroup(group, tuple(mutations))
        for group, mutations in group_mutations.items()
    }
    paths = {
        group: sorted({mutation.path for mutation in prepared.mutations})
        for group, prepared in groups.items()
    }
    all_paths = [path for values in paths.values() for path in values]
    if len(all_paths) != len(set(all_paths)):
        raise ClientSyncError("each writable path must belong to exactly one group")
    return _ConvergencePlan(
        manifest=manifest,
        groups=groups,
        rtk=rtk_plan,
        paths=paths,
        provider_errors=provider_errors,
    )


def _group_records(
    manifest: dict[str, Any],
    paths: dict[str, list[Path]],
    statuses: dict[str, str],
) -> list[dict[str, Any]]:
    metadata = _manifest_group_metadata(manifest)
    metadata.update(
        {
            "codex-roots": ("prerequisite", []),
            "claude-roots": ("prerequisite", []),
        }
    )
    return [
        {
            "id": group,
            "kind": metadata[group][0],
            "status": statuses[group],
            "dependencies": metadata[group][1],
            "paths": [str(path) for path in values],
        }
        for group, values in paths.items()
    ]


def _surface_records(
    manifest: dict[str, Any],
    statuses: dict[str, str],
    paths: dict[str, list[Path]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": surface["id"],
            "kind": surface["kind"],
            "status": statuses[surface["group"]],
            "paths": [str(path) for path in paths[surface["group"]]],
        }
        for surface in manifest["surfaces"]
    ]


def _path_records(
    paths: dict[str, list[Path]],
    group_statuses: dict[str, str],
    *,
    mode: str,
    mutations: dict[Path, PreparedMutation],
    drift_paths: set[Path] | None = None,
) -> list[dict[str, Any]]:
    def status_for(path: Path, group: str) -> str:
        group_status = group_statuses[group]
        if group_status == "blocked_stale_dependency":
            return "blocked_stale_dependency"
        if group_status == "rolled_back":
            return "rolled_back"
        if group_status == "drift":
            return (
                "drift" if drift_paths is None or path in drift_paths else "unchanged"
            )
        if group_status in {"current", "preserved"}:
            return "unchanged"
        if mode == "verify":
            return "drift"
        existed = mutations[path].before.kind != "absent"
        if mode == "dry-run":
            return "planned_update" if existed else "planned_create"
        return "updated" if existed else "created"

    records = []
    for group, values in paths.items():
        for path in values:
            mutation = mutations[path]
            records.append(
                _path_record(
                    path,
                    group,
                    (
                        COMMAND
                        if group == "rtk"
                        else (
                            "reverso-claude-code-sync"
                            if group.startswith("shared-claude")
                            or group == "shared-reverso-launcher"
                            or group == "claude-roots"
                            or path.name.startswith("claude-")
                            else "reverso-codex-sync"
                        )
                    ),
                    status_for(path, group),
                    before_sha256=_state_sha256(mutation.before),
                    after_sha256=_state_sha256(mutation.after),
                )
            )
    return records


def _mutations_by_path(plan: _ConvergencePlan) -> dict[Path, PreparedMutation]:
    return {
        mutation.path: mutation
        for group in plan.groups.values()
        for mutation in group.mutations
    }


def _changed_by_group(plan: _ConvergencePlan) -> dict[str, bool]:
    return {group: prepared.changed for group, prepared in plan.groups.items()}


def _post_apply_readback_errors(
    plan: _ConvergencePlan,
    *,
    launcher_dir: Path,
    host_path: str,
    statuses: dict[str, str],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    def add(code: str, group: str, path: Path) -> None:
        errors.append(
            {
                "code": code,
                "group": group,
                "path": str(path),
                "message": code,
            }
        )

    try:
        if (
            not plan.rtk.link.is_symlink()
            or plan.rtk.link.resolve(strict=True) != plan.rtk.executable
        ):
            add("rtk_readback_failed", "rtk", plan.rtk.link)
    except OSError:
        add("rtk_readback_failed", "rtk", plan.rtk.link)

    launcher_catalogs = dict(plan.manifest["claude_launchers"])
    for launcher, catalog in launcher_catalogs.items():
        group = (
            "shared-reverso-launcher"
            if launcher == "claude-reverso"
            else f"provider-{catalog}"
        )
        if statuses.get(group) in {"preserved", "blocked_stale_dependency"}:
            continue
        path = launcher_dir / launcher
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            add("launcher_readback_failed", group, path)
            continue
        if (
            claude_code_sync.LAUNCHER_MANAGED_MARKER not in text.splitlines()[:3]
            or not os.access(path, os.X_OK)
            or claude_code_sync.GATEWAY_BASE_URL not in text
            or f"x-reverso-model-catalog: {catalog}" not in text
        ):
            add("launcher_readback_failed", group, path)
            continue
        try:
            syntax = subprocess.run(
                ["/bin/sh", "-n", str(path)],
                check=False,
                capture_output=True,
                env={**os.environ, "PATH": host_path},
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            add("launcher_smoke_failed", group, path)
        else:
            if syntax.returncode != 0:
                add("launcher_smoke_failed", group, path)

    client_smokes = (
        ("codex", "shared-codex-config", "codex_smoke_failed"),
        ("claude", "shared-claude-settings", "claude_smoke_failed"),
    )
    for executable, group, code in client_smokes:
        if statuses.get(group) in {"preserved", "blocked_stale_dependency"}:
            continue
        paths = plan.paths.get(group, [])
        path = paths[0] if paths else Path(executable)
        try:
            smoke = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                env={**os.environ, "PATH": host_path},
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            add(code, group, path)
        else:
            if smoke.returncode != 0:
                add(code, group, path)
    return errors


def _validation_result(
    mode: str,
    started_at: str,
    exc: Exception,
) -> dict[str, Any]:
    status = getattr(exc, "status", "invalid")
    code = getattr(exc, "code", "validation_failed")
    return _result(
        mode,
        status,
        3,
        started_at,
        errors=[
            {
                "code": code,
                "group": None,
                "path": None,
                "message": str(exc),
            }
        ],
    )


def _read_only_result(
    mode: str,
    started_at: str,
    plan: _ConvergencePlan,
) -> dict[str, Any]:
    changed = _changed_by_group(plan)
    metadata = _manifest_group_metadata(plan.manifest)
    statuses = {
        group: (
            ("planned" if mode == "dry-run" else "drift")
            if group_changed
            else "current"
        )
        for group, group_changed in changed.items()
    }
    for group in plan.provider_errors:
        statuses[group] = "preserved"
    for group, (_kind, dependencies) in metadata.items():
        if any(dependency in plan.provider_errors for dependency in dependencies):
            statuses[group] = "blocked_stale_dependency"
    if plan.provider_errors:
        status, exit_code = "partial_freshness", 4
        errors = [
            {
                "code": "provider_stale",
                "group": group,
                "path": None,
                "message": message,
            }
            for group, message in plan.provider_errors.items()
        ]
    else:
        any_changed = any(changed.values())
        status = (
            ("planned" if any_changed else "no_op")
            if mode == "dry-run"
            else ("drift" if any_changed else "success")
        )
        exit_code = 2 if mode == "verify" and any_changed else 0
        errors = []
    mutations = _mutations_by_path(plan)
    return _result(
        mode,
        status,
        exit_code,
        started_at,
        groups=_group_records(plan.manifest, plan.paths, statuses),
        surfaces=_surface_records(plan.manifest, statuses, plan.paths),
        paths=_path_records(plan.paths, statuses, mode=mode, mutations=mutations),
        errors=errors,
    )


def _apply_result(
    mode: str,
    started_at: str,
    plan: _ConvergencePlan,
    *,
    lock_token: HeldClientSyncLock,
    launcher_dir: Path,
    host_path: str,
) -> dict[str, Any]:
    changed = _changed_by_group(plan)
    mutations = _mutations_by_path(plan)
    statuses = {group: "current" for group in plan.paths}
    metadata = _manifest_group_metadata(plan.manifest)
    for group in plan.provider_errors:
        statuses[group] = "preserved"
    if "provider-ollama" in plan.provider_errors:
        for group, group_changed in changed.items():
            if group_changed:
                statuses[group] = "preserved"
        return _result(
            mode,
            "partial_freshness",
            4,
            started_at,
            groups=_group_records(plan.manifest, plan.paths, statuses),
            surfaces=_surface_records(plan.manifest, statuses, plan.paths),
            paths=_path_records(
                plan.paths,
                statuses,
                mode=mode,
                mutations=mutations,
            ),
            errors=[
                {
                    "code": "provider_stale",
                    "group": "provider-ollama",
                    "path": None,
                    "message": plan.provider_errors["provider-ollama"],
                }
            ],
        )
    for group, (_kind, dependencies) in metadata.items():
        if any(dependency in plan.provider_errors for dependency in dependencies):
            statuses[group] = "blocked_stale_dependency"
    kind_order = {"prerequisite": 0, "provider": 1, "shared_dependency": 2}
    groups_to_apply = sorted(
        plan.groups,
        key=lambda group: (
            kind_order[metadata.get(group, ("prerequisite", []))[0]],
            0 if group == "provider-ollama" else 1,
            group,
        ),
    )
    eligible_groups = tuple(
        group
        for group in groups_to_apply
        if statuses[group] not in {"blocked_stale_dependency", "preserved"}
        and changed[group]
    )
    eligible_group_by_path = {
        mutation.path: group
        for group in eligible_groups
        for mutation in plan.groups[group].mutations
    }
    try:
        validate_prepared_group(
            PreparedGroup(
                "client-sync",
                tuple(
                    mutation
                    for group in eligible_groups
                    for mutation in plan.groups[group].mutations
                ),
            )
        )
    except PreparedStateChanged as exc:
        failed_path = exc.path
        failed_group = (
            eligible_group_by_path.get(failed_path) if failed_path is not None else None
        )
        if failed_path is None or failed_group is None:
            return _result(
                mode,
                "repair_required",
                5,
                started_at,
                groups=_group_records(plan.manifest, plan.paths, statuses),
                surfaces=_surface_records(plan.manifest, statuses, plan.paths),
                paths=_path_records(
                    plan.paths,
                    statuses,
                    mode=mode,
                    mutations=mutations,
                ),
                errors=[
                    {
                        "code": "prepared_state_changed",
                        "group": None,
                        "path": None,
                        "message": type(exc).__name__,
                    }
                ],
            )
        statuses[failed_group] = "drift"
        return _result(
            mode,
            "drift",
            2,
            started_at,
            groups=_group_records(plan.manifest, plan.paths, statuses),
            surfaces=_surface_records(plan.manifest, statuses, plan.paths),
            paths=_path_records(
                plan.paths,
                statuses,
                mode=mode,
                mutations=mutations,
                drift_paths={failed_path},
            ),
            errors=[
                {
                    "code": "prepared_state_changed",
                    "group": failed_group,
                    "path": str(failed_path),
                    "message": type(exc).__name__,
                }
            ],
        )
    except (OSError, RuntimeError) as exc:
        return _result(
            mode,
            "repair_required",
            5,
            started_at,
            groups=_group_records(plan.manifest, plan.paths, statuses),
            surfaces=_surface_records(plan.manifest, statuses, plan.paths),
            paths=_path_records(
                plan.paths,
                statuses,
                mode=mode,
                mutations=mutations,
            ),
            errors=[
                {
                    "code": "prepared_state_changed",
                    "group": None,
                    "path": None,
                    "message": type(exc).__name__,
                }
            ],
        )
    for group in eligible_groups:
        try:
            validate_client_sync_lock(lock_token)
            apply_prepared_group(plan.groups[group])
            statuses[group] = "changed"
        except PreparedRollbackFailed as exc:
            return _result(
                mode,
                "repair_required",
                5,
                started_at,
                groups=_group_records(plan.manifest, plan.paths, statuses),
                surfaces=_surface_records(plan.manifest, statuses, plan.paths),
                paths=_path_records(
                    plan.paths,
                    statuses,
                    mode=mode,
                    mutations=mutations,
                ),
                errors=[
                    {
                        "code": "rollback_failed",
                        "group": group,
                        "path": None,
                        "message": type(exc).__name__,
                    }
                ],
            )
        except PreparedStateChanged as exc:
            statuses[group] = "drift"
            return _result(
                mode,
                "drift",
                2,
                started_at,
                groups=_group_records(plan.manifest, plan.paths, statuses),
                surfaces=_surface_records(plan.manifest, statuses, plan.paths),
                paths=_path_records(
                    plan.paths,
                    statuses,
                    mode=mode,
                    mutations=mutations,
                ),
                errors=[
                    {
                        "code": "prepared_state_changed",
                        "group": group,
                        "path": None,
                        "message": type(exc).__name__,
                    }
                ],
            )
        except (PreparedApplyFailed, OSError, RuntimeError) as exc:
            statuses[group] = "rolled_back"
            return _result(
                mode,
                "invalid",
                3,
                started_at,
                groups=_group_records(plan.manifest, plan.paths, statuses),
                surfaces=_surface_records(plan.manifest, statuses, plan.paths),
                paths=_path_records(
                    plan.paths,
                    statuses,
                    mode=mode,
                    mutations=mutations,
                ),
                errors=[
                    {
                        "code": "apply_failed",
                        "group": group,
                        "path": None,
                        "message": type(exc).__name__,
                    }
                ],
            )

    readback_errors = _post_apply_readback_errors(
        plan,
        launcher_dir=launcher_dir,
        host_path=host_path,
        statuses=statuses,
    )
    if readback_errors:
        return _result(
            mode,
            "repair_required",
            5,
            started_at,
            groups=_group_records(plan.manifest, plan.paths, statuses),
            surfaces=_surface_records(plan.manifest, statuses, plan.paths),
            paths=_path_records(
                plan.paths,
                statuses,
                mode=mode,
                mutations=mutations,
            ),
            errors=readback_errors,
        )
    if plan.provider_errors:
        result_status, exit_code = "partial_freshness", 4
        errors = [
            {
                "code": "provider_stale",
                "group": group,
                "path": None,
                "message": message,
            }
            for group, message in plan.provider_errors.items()
        ]
    else:
        result_status = (
            "success"
            if any(value == "changed" for value in statuses.values())
            else "no_op"
        )
        exit_code = 0
        errors = []
    return _result(
        mode,
        result_status,
        exit_code,
        started_at,
        groups=_group_records(plan.manifest, plan.paths, statuses),
        surfaces=_surface_records(plan.manifest, statuses, plan.paths),
        paths=_path_records(
            plan.paths,
            statuses,
            mode=mode,
            mutations=mutations,
        ),
        errors=errors,
    )


def _run_once(
    mode: str,
    *,
    codex_config: Path | None = None,
    claude_config_dir: Path | None = None,
    catalog_dir: Path | None = None,
    launch_agent_dir: Path | None = None,
    rtk_bin: Path | None = None,
    home: Path | None = None,
    lock_path: Path | None = None,
    lock_token: HeldClientSyncLock | None = None,
    status_path: Path | None = None,
) -> dict[str, Any]:
    """Plan, verify, or apply all client convergence groups."""
    if mode not in {
        "dry-run",
        "apply",
        "restore",
        "refresh",
        "verify",
        "uninstall-ollama",
    }:
        raise ValueError(f"unsupported client sync mode: {mode}")

    started_at = _timestamp()
    resolved_codex = (codex_config or codex_sync.DEFAULT_CONFIG_PATH).expanduser()
    resolved_claude_dir = (
        claude_config_dir or claude_code_sync.DEFAULT_SETTINGS_PATH.parent
    ).expanduser()
    resolved_catalog = (
        catalog_dir or codex_sync._default_catalog_dir(resolved_codex)
    ).expanduser()
    resolved_launchers = (
        launch_agent_dir or claude_code_sync.DEFAULT_LAUNCHER_DIR
    ).expanduser()

    if mode == "uninstall-ollama":
        try:
            group = ollama_convergence.prepare_uninstall_group(
                inventory_path=ollama_convergence.default_inventory_path(home),
                profile_path=resolved_codex.parent / "reverso-ollama.config.toml",
                catalog_path=resolved_catalog / "ollama.json",
                launcher_path=resolved_launchers / "claude-ollama",
                profile_marker=codex_sync.PROFILE_MANAGED_MARKER,
                launcher_marker=claude_code_sync.LAUNCHER_MANAGED_MARKER,
                catalog_owned=codex_sync._catalog_is_owned(
                    resolved_catalog / "ollama.json",
                    config_dir=resolved_codex.parent,
                    prefix="ollama",
                ),
            )
            with acquire_client_sync_lock(
                path=lock_path,
                token=lock_token,
                blocking=True,
                timeout_seconds=30.0,
            ) as held_lock:
                validate_client_sync_lock(held_lock)
                apply_prepared_group(group)
        except (OSError, RuntimeError) as exc:
            return _validation_result(mode, started_at, exc)
        changed = group.changed
        return _result(
            mode,
            "success" if changed else "no_op",
            0,
            started_at,
            groups=[
                {
                    "id": "provider-ollama",
                    "kind": "provider",
                    "status": "changed" if changed else "current",
                    "dependencies": [],
                    "paths": [str(mutation.path) for mutation in group.mutations],
                }
            ],
        )

    def planned() -> _ConvergencePlan:
        return _plan(
            codex_config=resolved_codex,
            claude_config_dir=resolved_claude_dir,
            catalog_dir=resolved_catalog,
            launch_agent_dir=resolved_launchers,
            rtk_bin=rtk_bin,
            home=home,
        )

    if mode in {"dry-run", "verify"}:
        try:
            return _read_only_result(mode, started_at, planned())
        except (ClientSyncError, OSError, ValueError, RuntimeError) as exc:
            return _validation_result(mode, started_at, exc)

    try:
        with acquire_client_sync_lock(
            path=lock_path,
            token=lock_token,
            blocking=mode in {"apply", "restore"},
            timeout_seconds=30.0,
        ) as held_lock:
            try:
                plan = planned()
            except (ClientSyncError, OSError, ValueError, RuntimeError) as exc:
                return _validation_result(mode, started_at, exc)
            result = _apply_result(
                mode,
                started_at,
                plan,
                lock_token=held_lock,
                launcher_dir=resolved_launchers,
                host_path=os.environ.get("PATH", ""),
            )
            if mode == "refresh" and status_path is not None:
                prior = _load_refresh_status(status_path)
                try:
                    _write_refresh_status(
                        status_path,
                        _refresh_status_record(result, prior=prior),
                    )
                except (OSError, RuntimeError) as exc:
                    result["status"] = "repair_required"
                    result["exit_code"] = 5
                    result["errors"] = sorted(
                        [
                            *result["errors"],
                            {
                                "code": "status_persist_failed",
                                "group": None,
                                "path": str(status_path),
                                "message": type(exc).__name__,
                            },
                        ],
                        key=lambda item: (
                            item.get("code", ""),
                            item.get("group") or "",
                            item.get("path") or "",
                        ),
                    )
            return result
    except ClientSyncLockBusy as exc:
        return _result(
            mode,
            "lock_skipped" if mode == "refresh" else "lock_busy",
            0 if mode == "refresh" else 2,
            started_at,
            errors=[]
            if mode == "refresh"
            else [
                {
                    "code": "lock_busy",
                    "group": None,
                    "path": str(lock_path) if lock_path else None,
                    "message": str(exc),
                }
            ],
        )
    except (OSError, RuntimeError) as exc:
        return _validation_result(mode, started_at, exc)


def run(
    mode: str,
    *,
    codex_config: Path | None = None,
    claude_config_dir: Path | None = None,
    catalog_dir: Path | None = None,
    launch_agent_dir: Path | None = None,
    rtk_bin: Path | None = None,
    home: Path | None = None,
    lock_path: Path | None = None,
    lock_token: HeldClientSyncLock | None = None,
    status_path: Path | None = None,
) -> dict[str, Any]:
    """Plan, verify, or apply all client convergence groups."""
    refresh_path = status_path or DEFAULT_CATALOG_REFRESH_STATUS_PATH
    result = _run_once(
        mode,
        codex_config=codex_config,
        claude_config_dir=claude_config_dir,
        catalog_dir=catalog_dir,
        launch_agent_dir=launch_agent_dir,
        rtk_bin=rtk_bin,
        home=home,
        lock_path=lock_path,
        lock_token=lock_token,
        status_path=refresh_path,
    )
    catalog_refresh = _catalog_refresh(path=refresh_path)
    result["catalog_refresh"] = catalog_refresh
    if mode == "verify" and catalog_refresh["stale"] and result["exit_code"] in {0, 2}:
        if result["exit_code"] == 0:
            result["status"] = "drift"
            result["exit_code"] = 2
        result["errors"].append(
            {
                "code": "catalog_refresh_stale",
                "group": None,
                "path": str(refresh_path),
                "message": "catalog refresh status is stale",
            }
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    parser.add_argument(
        "mode",
        choices=(
            "dry-run",
            "apply",
            "restore",
            "refresh",
            "verify",
            "uninstall-ollama",
        ),
    )
    parser.add_argument("--codex-config", type=Path)
    parser.add_argument("--claude-config-dir", type=Path)
    parser.add_argument("--catalog-dir", type=Path)
    parser.add_argument("--launch-agent-dir", type=Path)
    parser.add_argument("--rtk-bin", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run(
        args.mode,
        codex_config=args.codex_config,
        claude_config_dir=args.claude_config_dir,
        catalog_dir=args.catalog_dir,
        launch_agent_dir=args.launch_agent_dir,
        rtk_bin=args.rtk_bin,
    )
    if args.json:
        json.dump(result, sys.stdout, sort_keys=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(
            f"{COMMAND}: {result['mode']} {result['status']} "
            f"(exit {result['exit_code']})\n"
        )
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
