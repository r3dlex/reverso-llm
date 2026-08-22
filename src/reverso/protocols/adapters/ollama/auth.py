"""Noninteractive Ollama Cloud eligibility state."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

CLOUD_AUTHORITY_URL = "https://ollama.com/api/tags"
"""Documented machine-readable Ollama Cloud model authority.

See https://docs.ollama.com/cloud ("Listing models"). This is the only source
permitted for Cloud ids; suffix inference over local rows, HTML scraping of
https://ollama.com/search?c=cloud, and shipped static lists all remain barred.
"""

CLOUD_AUTHORITY_HOST = "ollama.com"
CLOUD_ROUTING_SUFFIX = "-cloud"
"""Documented local routing alias for an authority-published Cloud model.

The authority publishes bare ids (``gpt-oss:120b``). The user-owned local
Ollama service routes the same model only under ``<id>-cloud`` (see
https://docs.ollama.com/cloud, "Running Cloud models"); the bare id is rejected
with ``model 'gpt-oss:120b' not found``.
"""


def validate_cloud_authority(url: str) -> str:
    """Accept only a plain credential-free HTTPS Ollama Cloud authority URL."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != CLOUD_AUTHORITY_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama Cloud authority must be a plain ollama.com HTTPS URL")
    return url


@dataclass(frozen=True)
class OllamaAuthState:
    cloud_requested: bool
    cloud_status: str
    authority_url: str = CLOUD_AUTHORITY_URL
    api_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OllamaAuthState:
        source = os.environ if env is None else env
        reverso_cloud = source.get("REVERSO_OLLAMA_CLOUD", "1").strip().lower()
        no_cloud = source.get("OLLAMA_NO_CLOUD", "").strip().lower()
        requested = reverso_cloud not in {"0", "false", "no", "off"} and (
            no_cloud not in {"1", "true", "yes", "on"}
        )
        authority = (
            source.get("REVERSO_OLLAMA_CLOUD_AUTHORITY", "").strip()
            or CLOUD_AUTHORITY_URL
        )
        return cls(
            cloud_requested=requested,
            cloud_status="unavailable" if requested else "disabled",
            authority_url=validate_cloud_authority(authority),
            api_key=source.get("OLLAMA_API_KEY", "").strip() or None,
        )
