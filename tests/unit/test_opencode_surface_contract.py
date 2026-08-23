"""B1 contract: OpenCode launchers are declared, mirrored, and drift-guarded."""

import json
from pathlib import Path

import pytest

from reverso.client_sync import (
    ClientSyncError,
    validate_supported_surface_manifest,
)
from reverso import opencode_sync

MANIFEST = (
    Path(__file__).resolve().parents[2] / "config" / "supported-client-surfaces.json"
)


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_declares_opencode_launchers_matching_catalog() -> None:
    manifest = _load()
    assert manifest["opencode_launchers"] == dict(opencode_sync.LAUNCHER_CATALOGS)


def test_shipped_manifest_passes_validation_with_opencode_surfaces() -> None:
    manifest = _load()
    surface_ids = {surface["id"] for surface in manifest["surfaces"]}
    for name in manifest["opencode_launchers"]:
        assert name in surface_ids


def test_validator_rejects_opencode_launcher_drift() -> None:
    manifest = _load()
    drifted = dict(opencode_sync.LAUNCHER_CATALOGS)
    drifted["opencode-reverso"] = "claude"
    manifest["opencode_launchers"] = drifted
    with pytest.raises(ClientSyncError):
        validate_supported_surface_manifest(manifest)
