"""Noninteractive Ollama Cloud eligibility state."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OllamaAuthState:
    cloud_requested: bool
    cloud_status: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OllamaAuthState:
        source = os.environ if env is None else env
        reverso_cloud = source.get("REVERSO_OLLAMA_CLOUD", "1").strip().lower()
        no_cloud = source.get("OLLAMA_NO_CLOUD", "").strip().lower()
        requested = reverso_cloud not in {"0", "false", "no", "off"} and (
            no_cloud not in {"1", "true", "yes", "on"}
        )
        return cls(
            cloud_requested=requested,
            cloud_status="unavailable" if requested else "disabled",
        )
