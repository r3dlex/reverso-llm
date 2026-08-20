"""Validated Ollama inventory from the supported local API."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .auth import OllamaAuthState


@dataclass(frozen=True)
class OllamaCatalogEntry:
    raw_id: str
    local: bool
    cloud: bool
    observed_at: float


class OllamaCatalog:
    """Current local raw-id inventory from Ollama's validated tags response."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        auth: OllamaAuthState,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._auth = auth
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
