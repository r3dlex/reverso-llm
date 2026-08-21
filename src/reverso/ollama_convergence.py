"""Shared Ollama inventory and atomic cross-client convergence helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from reverso.client_sync_mutations import (
    FileState,
    PreparedGroup,
    PreparedMutation,
    capture_state,
    file_state,
    missing_parent_mutations,
    prepared_mutation,
)

INVENTORY_OWNER = "reverso-client-sync/provider-ollama"
INVENTORY_FILENAME = "ollama-inventory.json"


class InventoryOwnershipConflict(RuntimeError):
    """An existing inventory is not owned by the exact Reverso marker."""


@dataclass(frozen=True)
class InventoryEntry:
    raw_id: str
    local: bool
    cloud: bool
    stale: bool


@dataclass(frozen=True)
class InventoryPlan:
    path: Path
    entries: tuple[InventoryEntry, ...]
    observed_at: str
    freshness: str
    auth_status: str
    cloud_status: str
    mutation: PreparedMutation

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(entry.raw_id for entry in self.entries)

    @property
    def eligible_model_ids(self) -> tuple[str, ...]:
        """Return current routing ids while retaining stale diagnostic rows."""
        return tuple(
            entry.raw_id for entry in self.entries if entry.local or not entry.stale
        )


@dataclass(frozen=True)
class InventorySnapshot:
    entries: tuple[InventoryEntry, ...]
    observed_at: str
    freshness: str
    auth_status: str
    cloud_status: str


def default_inventory_path(home: Path | None = None) -> Path:
    root = home or Path.home()
    return root / "Library" / "Application Support" / "reverso" / INVENTORY_FILENAME


def _validated_ids(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("Ollama inventory ids must be non-empty raw strings")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def load_inventory(path: Path) -> InventorySnapshot:
    """Load only the exact marker-owned shared Ollama inventory."""
    state = capture_state(path)
    if state.kind == "absent":
        raise FileNotFoundError(path)
    if state.kind != "file" or not isinstance(state.data, bytes):
        raise InventoryOwnershipConflict(f"Ollama inventory path is not owned: {path}")
    try:
        payload = json.loads(state.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InventoryOwnershipConflict(
            f"Ollama inventory marker is invalid: {path}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("owner") != INVENTORY_OWNER:
        raise InventoryOwnershipConflict(f"Ollama inventory path is not owned: {path}")
    rows = payload.get("entries")
    if payload.get("schema_version") != 1 or not isinstance(rows, list):
        raise InventoryOwnershipConflict(f"Ollama inventory schema is invalid: {path}")
    entries: list[InventoryEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            raise InventoryOwnershipConflict(
                f"Ollama inventory entry is invalid: {path}"
            )
        raw_id = row.get("raw_id")
        if not isinstance(raw_id, str) or not raw_id or raw_id != raw_id.strip():
            raise InventoryOwnershipConflict(
                f"Ollama inventory entry is invalid: {path}"
            )
        entries.append(
            InventoryEntry(
                raw_id,
                row.get("local") is True,
                row.get("cloud") is True,
                row.get("stale") is True,
            )
        )
    metadata = tuple(
        payload.get(key)
        for key in ("observed_at", "freshness", "auth_status", "cloud_status")
    )
    if not all(isinstance(value, str) and value for value in metadata):
        raise InventoryOwnershipConflict(
            f"Ollama inventory metadata is invalid: {path}"
        )
    return InventorySnapshot(tuple(entries), *metadata)


def _prior_entries(path: Path) -> tuple[InventoryEntry, ...]:
    try:
        return load_inventory(path).entries
    except FileNotFoundError:
        return ()


def _payload(
    *,
    entries: tuple[InventoryEntry, ...],
    observed_at: str,
    freshness: str,
    auth_status: str,
    cloud_status: str,
) -> bytes:
    value = {
        "schema_version": 1,
        "owner": INVENTORY_OWNER,
        "observed_at": observed_at,
        "freshness": freshness,
        "auth_status": auth_status,
        "cloud_status": cloud_status,
        "entries": [
            {
                "raw_id": entry.raw_id,
                "local": entry.local,
                "cloud": entry.cloud,
                "stale": entry.stale,
            }
            for entry in entries
        ],
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _preserve_observation_when_unchanged(path: Path, encoded: bytes) -> bytes:
    state = capture_state(path)
    if state.kind != "file" or not isinstance(state.data, bytes):
        return encoded
    try:
        prior = json.loads(state.data.decode("utf-8"))
        candidate = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return encoded
    if not isinstance(prior, dict) or not isinstance(candidate, dict):
        return encoded
    prior_without_time = {
        key: value for key, value in prior.items() if key != "observed_at"
    }
    candidate_without_time = {
        key: value for key, value in candidate.items() if key != "observed_at"
    }
    return state.data if prior_without_time == candidate_without_time else encoded


def plan_inventory_refresh(
    path: Path,
    *,
    local_ids: Iterable[str],
    cloud_status: str,
    observed_at: str,
    cloud_ids: Iterable[str] = (),
) -> InventoryPlan:
    """Prepare one prompt-free snapshot from current local and bounded Cloud state."""
    local = _validated_ids(local_ids)
    current_cloud = _validated_ids(cloud_ids)
    prior = _prior_entries(path)
    if cloud_status not in {
        "current",
        "auth_required",
        "timeout",
        "invalid",
        "disabled",
        "unavailable",
    }:
        raise ValueError("unsupported Ollama Cloud status")

    if cloud_status == "disabled":
        retained_cloud: tuple[str, ...] = ()
        stale_cloud = False
        freshness = "current"
        auth_status = "disabled"
    elif cloud_status == "current":
        retained_cloud = current_cloud
        stale_cloud = False
        freshness = "current"
        auth_status = "current"
    else:
        retained_cloud = tuple(entry.raw_id for entry in prior if entry.cloud)
        stale_cloud = bool(retained_cloud)
        freshness = "partial" if retained_cloud else "local_only"
        auth_status = "required" if cloud_status == "auth_required" else "failed"

    local_set = set(local)
    cloud_set = set(retained_cloud)
    ordered = (*local, *(item for item in retained_cloud if item not in local_set))
    entries = tuple(
        InventoryEntry(
            raw_id,
            raw_id in local_set,
            raw_id in cloud_set,
            stale_cloud and raw_id in cloud_set,
        )
        for raw_id in ordered
    )
    encoded = _payload(
        entries=entries,
        observed_at=observed_at,
        freshness=freshness,
        auth_status=auth_status,
        cloud_status=cloud_status,
    )
    encoded = _preserve_observation_when_unchanged(path, encoded)
    return InventoryPlan(
        path,
        entries,
        observed_at,
        freshness,
        auth_status,
        cloud_status,
        prepared_mutation(path, file_state(encoded, 0o600)),
    )


def plan_background_refresh(
    path: Path,
    *,
    local_discovery: Callable[[], Iterable[str]],
    cloud_discovery: Callable[[], Iterable[str]],
    signin: Callable[[], object],
    cloud_enabled: bool,
    observed_at: str,
) -> InventoryPlan:
    """Run only noninteractive discovery. The signin callable is never invoked."""
    local_ids = tuple(local_discovery())
    if not cloud_enabled:
        return plan_inventory_refresh(
            path,
            local_ids=local_ids,
            cloud_status="disabled",
            observed_at=observed_at,
        )
    try:
        cloud_ids = tuple(cloud_discovery())
    except PermissionError:
        status = "auth_required"
        cloud_ids = ()
    except TimeoutError:
        status = "timeout"
        cloud_ids = ()
    except (TypeError, ValueError):
        status = "invalid"
        cloud_ids = ()
    else:
        status = "current"
    del signin
    return plan_inventory_refresh(
        path,
        local_ids=local_ids,
        cloud_status=status,
        cloud_ids=cloud_ids,
        observed_at=observed_at,
    )


def prepare_ollama_group(
    *,
    inventory_path: Path,
    inventory_payload: bytes,
    client_candidates: Mapping[Path, tuple[bytes, int]],
) -> PreparedGroup:
    """Prepare all inventory, Codex, and Claude paths as one rollback group."""
    targets = (inventory_path, *client_candidates)
    mutations = [*missing_parent_mutations(targets)]
    mutations.append(
        prepared_mutation(inventory_path, file_state(inventory_payload, 0o600))
    )
    mutations.extend(
        prepared_mutation(path, file_state(payload, mode))
        for path, (payload, mode) in client_candidates.items()
    )
    return PreparedGroup("provider-ollama", tuple(mutations))


def prepare_uninstall_group(
    *,
    inventory_path: Path,
    profile_path: Path,
    catalog_path: Path,
    launcher_path: Path,
    profile_marker: str,
    launcher_marker: str,
    catalog_owned: bool,
) -> PreparedGroup:
    """Remove only exact marker-owned Ollama artifacts as one atomic group."""
    mutations: list[PreparedMutation] = []
    inventory_state = capture_state(inventory_path)
    if inventory_state.kind != "absent":
        try:
            load_inventory(inventory_path)
        except (FileNotFoundError, InventoryOwnershipConflict) as exc:
            raise InventoryOwnershipConflict(
                f"Ollama uninstall ownership conflict: {inventory_path}"
            ) from exc
        mutations.append(
            PreparedMutation(inventory_path, inventory_state, FileState("absent"))
        )

    profile_state = capture_state(profile_path)
    if profile_state.kind != "absent":
        if not (
            profile_state.kind == "file"
            and isinstance(profile_state.data, bytes)
            and profile_state.data.decode("utf-8", errors="replace").startswith(
                profile_marker + "\n"
            )
        ):
            raise InventoryOwnershipConflict(
                f"Ollama uninstall ownership conflict: {profile_path}"
            )
        mutations.append(
            PreparedMutation(profile_path, profile_state, FileState("absent"))
        )

    catalog_state = capture_state(catalog_path)
    if catalog_state.kind != "absent":
        if catalog_state.kind != "file" or not catalog_owned:
            raise InventoryOwnershipConflict(
                f"Ollama uninstall ownership conflict: {catalog_path}"
            )
        mutations.append(
            PreparedMutation(catalog_path, catalog_state, FileState("absent"))
        )

    launcher_state = capture_state(launcher_path)
    if launcher_state.kind != "absent":
        if not (
            launcher_state.kind == "file"
            and isinstance(launcher_state.data, bytes)
            and launcher_marker
            in launcher_state.data.decode("utf-8", errors="replace").splitlines()[:2]
        ):
            raise InventoryOwnershipConflict(
                f"Ollama uninstall ownership conflict: {launcher_path}"
            )
        mutations.append(
            PreparedMutation(launcher_path, launcher_state, FileState("absent"))
        )
    return PreparedGroup("provider-ollama", tuple(mutations))
