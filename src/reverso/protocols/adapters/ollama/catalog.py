"""Validated Ollama inventory from the supported local API."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from reverso.ollama_convergence import load_inventory

from .auth import OllamaAuthState


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
        self.entries: dict[str, OllamaCatalogEntry] = {}

    @property
    def cloud_status(self) -> str:
        return self._auth.cloud_status

    async def refresh(self) -> tuple[OllamaCatalogEntry, ...]:
        response = await self._client.get(f"{self._endpoint}/api/tags")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise TypeError("ollama /api/tags payload must contain models")
        observed_at = time.time()
        entries: dict[str, OllamaCatalogEntry] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("ollama /api/tags model entries must be objects")
            raw_id = row.get("name") or row.get("model")
            if not isinstance(raw_id, str) or not raw_id or raw_id != raw_id.strip():
                raise ValueError("ollama model id must be a non-empty raw string")
            prior = entries.get(raw_id)
            entries[raw_id] = OllamaCatalogEntry(
                raw_id=raw_id,
                local=True,
                cloud=bool(prior and prior.cloud),
                observed_at=observed_at,
            )
        self.entries = entries
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
