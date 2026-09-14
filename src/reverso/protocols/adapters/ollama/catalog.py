"""Validated Ollama inventory from the supported local API."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from reverso.ollama_convergence import load_inventory

from .auth import (
    CLOUD_ROUTING_SUFFIX,
    OllamaAuthState,
    validate_cloud_authority,
)

CLOUD_DISCOVERY_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class OllamaCatalogEntry:
    raw_id: str
    local: bool
    cloud: bool
    observed_at: float
    stale: bool = False


class OllamaModelEligibilityError(RuntimeError):
    """A requested model is retained diagnostically but is not current."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.status_code = 409
        self.payload = {
            "error": {
                "type": code,
                "code": code,
                "message": code,
            }
        }


def _validated_raw_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("ollama model id must be a non-empty raw string")
    return value


def _authority_routing_ids(payload: object) -> tuple[str, ...]:
    """Map every authority-published Cloud id onto its local routing alias."""
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("ollama Cloud authority payload must contain models")
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("ollama Cloud authority entries must be objects")
        raw_id = _validated_raw_id(row.get("name") or row.get("model"))
        routing_id = (
            raw_id
            if raw_id.endswith(CLOUD_ROUTING_SUFFIX)
            else f"{raw_id}{CLOUD_ROUTING_SUFFIX}"
        )
        if routing_id not in seen:
            seen.add(routing_id)
            ids.append(routing_id)
    return tuple(ids)


class OllamaCatalog:
    """Current local raw-id inventory from Ollama's validated tags response."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        auth: OllamaAuthState,
        inventory_path: Path,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._auth = auth
        self._inventory_path = inventory_path
        self._cloud_status = auth.cloud_status
        self.entries: dict[str, OllamaCatalogEntry] = {}

    @property
    def cloud_status(self) -> str:
        """Live Cloud status from the last refresh, not the requested state."""
        return self._cloud_status

    async def _refresh_local(self) -> tuple[str, ...]:
        response = await self._client.get(f"{self._endpoint}/api/tags")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise TypeError("ollama /api/tags payload must contain models")
        ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("ollama /api/tags model entries must be objects")
            raw_id = _validated_raw_id(row.get("name") or row.get("model"))
            if raw_id not in seen:
                seen.add(raw_id)
                ids.append(raw_id)
        return tuple(ids)

    async def _discover_cloud(self) -> tuple[tuple[str, ...], str]:
        """Probe the documented Cloud authority once, bounded and prompt-free."""
        url = validate_cloud_authority(self._auth.authority_url)
        headers = (
            {"Authorization": f"Bearer {self._auth.api_key}"}
            if self._auth.api_key
            else {}
        )
        try:
            response = await self._client.get(
                url,
                headers=headers,
                timeout=CLOUD_DISCOVERY_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            return (), "timeout"
        except httpx.HTTPError:
            return (), "unavailable"
        if response.status_code in {401, 403}:
            return (), "auth_required"
        if response.status_code >= 400:
            return (), "unavailable"
        try:
            return _authority_routing_ids(response.json()), "current"
        except (TypeError, ValueError):
            return (), "invalid"

    async def refresh(self) -> tuple[OllamaCatalogEntry, ...]:
        local_ids = await self._refresh_local()
        if self._auth.cloud_requested:
            cloud_ids, cloud_status = await self._discover_cloud()
        else:
            cloud_ids, cloud_status = (), "disabled"
        observed_at = time.time()
        entries: dict[str, OllamaCatalogEntry] = {
            raw_id: OllamaCatalogEntry(
                raw_id=raw_id,
                local=True,
                cloud=False,
                observed_at=observed_at,
            )
            for raw_id in local_ids
        }
        for raw_id in cloud_ids:
            prior = entries.get(raw_id)
            entries[raw_id] = OllamaCatalogEntry(
                raw_id=raw_id,
                local=bool(prior and prior.local),
                cloud=True,
                observed_at=observed_at,
            )
        self.entries = entries
        self._cloud_status = cloud_status
        return tuple(entries.values())

    def published(self) -> tuple[tuple[OllamaCatalogEntry, ...], str]:
        """Read the marker-owned snapshot used by scoped Claude discovery."""
        snapshot = load_inventory(self._inventory_path)
        entries = tuple(
            OllamaCatalogEntry(
                entry.raw_id,
                entry.local,
                entry.cloud,
                0.0,
                entry.stale,
            )
            for entry in snapshot.entries
            if entry.local or not entry.stale
        )
        return entries, snapshot.cloud_status

    async def ensure_current(self, raw_id: str) -> None:
        """Confirm request-time eligibility without credentials or side effects."""
        try:
            snapshot = load_inventory(self._inventory_path)
        except FileNotFoundError:
            snapshot = None
        if snapshot is None:
            return
        entry = next(
            (candidate for candidate in snapshot.entries if candidate.raw_id == raw_id),
            None,
        )
        if entry is None or entry.local or not entry.stale:
            return
        try:
            live_entries = await asyncio.wait_for(self.refresh(), timeout=10.0)
        except (TimeoutError, httpx.HTTPError, TypeError, ValueError):
            live_entries = ()
        if any(candidate.raw_id == raw_id for candidate in live_entries):
            return
        code = (
            "auth_required"
            if snapshot.auth_status == "required"
            else "model_not_current"
        )
        raise OllamaModelEligibilityError(code)
