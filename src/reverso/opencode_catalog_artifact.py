"""The OpenCode Go catalog as committed data (OCG-G6).

G6 requires that a model added upstream becomes reachable after a refresh with no
code change. Before this slice the declared catalog was a Python constant, so a
new upstream model was *listed* by live discovery but not *routable*: it fell
outside the declared catalog and ADR 0020's prefix branch correctly failed closed
on it. Refreshing therefore meant editing code.

Holding the catalog as a committed artifact makes a refresh a data change while
keeping the routing index deterministic at import time. Runtime discovery stays
the authority for LISTING; this artifact is the authority for ROUTING, and
``scripts/refresh-opencode-catalog.py`` reconciles the two.

Parsing fails closed. An unreadable or empty artifact raises rather than yielding
an empty catalog, because an empty declared catalog makes every qualified id fail
closed, which presents as a routing bug rather than as a corrupt file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "CATALOG_ARTIFACT_PATH",
    "CatalogArtifactError",
    "load_catalog_ids",
    "parse_catalog",
    "render_catalog",
    "repo_root",
]

CATALOG_ARTIFACT_PATH = Path("docs/reference/opencode-go-catalog.json")


class CatalogArtifactError(RuntimeError):
    """The committed catalog artifact is missing, unreadable or empty."""


def repo_root() -> Path:
    # src/reverso/opencode_catalog_artifact.py -> ../../ = repo root
    return Path(__file__).resolve().parent.parent.parent


def parse_catalog(payload: Any) -> tuple[str, ...]:
    """Extract the sorted, deduplicated model ids from an artifact payload."""
    if not isinstance(payload, dict):
        raise CatalogArtifactError("catalog artifact is not a JSON object")
    rows = payload.get("models")
    if not isinstance(rows, list):
        raise CatalogArtifactError("catalog artifact has no 'models' list")
    found = {row.strip() for row in rows if isinstance(row, str) and row.strip()}
    if not found:
        raise CatalogArtifactError("catalog artifact lists no usable model ids")
    return tuple(sorted(found))


def render_catalog(model_ids: tuple[str, ...]) -> str:
    """Serialize the artifact deterministically, newline-terminated."""
    return (
        json.dumps(
            {
                "backend": "opencode",
                "source": "https://opencode.ai/zen/go/v1/models",
                "model_count": len(model_ids),
                "models": sorted(model_ids),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def load_catalog_ids(root: Path | None = None) -> tuple[str, ...]:
    """Read the committed catalog. Raises rather than degrading to empty."""
    path = (root or repo_root()) / CATALOG_ARTIFACT_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogArtifactError(
            f"catalog artifact is missing: {CATALOG_ARTIFACT_PATH}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogArtifactError(
            f"catalog artifact is unreadable: {CATALOG_ARTIFACT_PATH} ({exc})"
        ) from exc
    return parse_catalog(payload)
