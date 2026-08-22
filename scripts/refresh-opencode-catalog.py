#!/usr/bin/env python3
"""Refresh or verify the committed OpenCode Go catalog artifact.

``--check`` compares the committed artifact against the LIVE catalog and exits
non-zero on any difference, so an upstream addition or removal surfaces as a
diff to review rather than as a silent routing change. The default writes.

Network access is required only for these two modes; nothing at import time or
request time reaches out, so a refresh is an explicit operator action.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reverso.opencode_catalog_artifact import (  # noqa: E402
    CATALOG_ARTIFACT_PATH,
    CatalogArtifactError,
    load_catalog_ids,
    render_catalog,
)

MODELS_URL = "https://opencode.ai/zen/go/v1/models"
# A User-Agent is mandatory: the edge rejects a default client fingerprint with
# Cloudflare error 1010 even though this endpoint is public.
USER_AGENT = "reverso-opencode-go/1.0"


def fetch_live_ids() -> tuple[str, ...]:
    """Read the live catalog. Public endpoint; no credential is sent."""
    result = subprocess.run(
        ["curl", "-sS", "--max-time", "60", "-A", USER_AGENT, MODELS_URL],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CatalogArtifactError(
            f"live catalog fetch failed (curl {result.returncode})"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CatalogArtifactError("live catalog returned invalid JSON") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise CatalogArtifactError("live catalog payload has no 'data' list")
    found = {
        row["id"].strip()
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and row["id"].strip()
    }
    if not found:
        raise CatalogArtifactError("live catalog listed no usable model ids")
    return tuple(sorted(found))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh-opencode-catalog")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail closed when the committed catalog differs from live; never write",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent

    try:
        live = fetch_live_ids()
    except CatalogArtifactError as exc:
        print(f"refresh-opencode-catalog: {exc}", file=sys.stderr)
        return 1

    if args.check:
        try:
            committed = load_catalog_ids(root)
        except CatalogArtifactError as exc:
            print(f"refresh-opencode-catalog: {exc}", file=sys.stderr)
            return 1
        added = sorted(set(live) - set(committed))
        removed = sorted(set(committed) - set(live))
        if added or removed:
            print(f"refresh-opencode-catalog: {CATALOG_ARTIFACT_PATH} is stale")
            for model_id in added:
                print(f"  + {model_id}")
            for model_id in removed:
                print(f"  - {model_id}")
            print("Re-run without --check, review the diff, and commit it.")
            return 1
        print(f"refresh-opencode-catalog: current ({len(committed)} models)")
        return 0

    (root / CATALOG_ARTIFACT_PATH).write_text(render_catalog(live), encoding="utf-8")
    print(
        f"refresh-opencode-catalog: wrote {CATALOG_ARTIFACT_PATH} ({len(live)} models)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
