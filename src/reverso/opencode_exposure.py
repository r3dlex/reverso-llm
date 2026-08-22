"""The OpenCode Go bare-exposure artifact and its fail-closed check (OCG-G3).

Under ADR 0020 a catalog-owning backend publishes its whole catalog behind its
own prefix, but DEFERS any bare id an incumbent already claims. Which ids are
contested is derived from the live routing index, so it moves whenever any other
backend gains a model: an id reachable bare today can silently become
prefix-only tomorrow, changing what a user's saved model string resolves to
without anything in this repo changing.

That is why the set is committed and policed rather than merely computed. The
artifact records what exposure was true when it was written; ``--check``
recomputes and fails closed on any difference, so the shift shows up as a red
build with a diff instead of a behaviour change nobody noticed.

Measured 2026-08-22 against the 29-id catalog: three ids are contested,
``deepseek-v4-flash`` and ``deepseek-v4-pro`` (deepseek) and ``kimi-k3`` (kimi),
leaving 26 bare-exposable.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "ARTIFACT_RELATIVE_PATH",
    "BACKEND_NAME",
    "ExposureDriftError",
    "compute_exposure",
    "load_catalog_ids",
    "load_model_index",
    "main",
    "render_artifact",
    "verify_artifact",
]

BACKEND_NAME = "opencode"
ARTIFACT_RELATIVE_PATH = Path("docs/reference/opencode-go-exposure.json")


class ExposureDriftError(RuntimeError):
    """The committed artifact does not match a freshly computed exposure set.

    Also raised when the artifact is missing or unparseable: an absent record is
    indistinguishable from a stale one for the purpose it serves, so both fail
    closed rather than being treated as "nothing to compare".
    """


def compute_exposure(
    *,
    catalog_ids: Sequence[str],
    model_index: Mapping[str, str],
) -> dict[str, Any]:
    """Partition ``catalog_ids`` into bare-exposable and prefix-only.

    An id already indexed to a DIFFERENT backend is deferred: incumbency always
    wins, per ADR 0020. An id indexed to this backend is our own seed from a
    previous build and stays bare, so recomputing after seeding is stable rather
    than reclassifying our own ids as contested.
    """
    bare: list[str] = []
    qualified: dict[str, str] = {}
    for model_id in sorted(set(catalog_ids)):
        incumbent = model_index.get(model_id)
        if incumbent is not None and incumbent != BACKEND_NAME:
            qualified[model_id] = incumbent
        else:
            bare.append(model_id)
    return {
        "backend": BACKEND_NAME,
        "catalog_size": len(set(catalog_ids)),
        "bare_exposed": bare,
        "qualified_only": dict(sorted(qualified.items())),
    }


def render_artifact(exposure: Mapping[str, Any]) -> str:
    """Serialize the artifact deterministically, newline-terminated."""
    return json.dumps(exposure, indent=2, sort_keys=True) + "\n"


def verify_artifact(repo_root: Path, exposure: Mapping[str, Any]) -> None:
    """Raise ``ExposureDriftError`` unless the committed artifact matches."""
    path = repo_root / ARTIFACT_RELATIVE_PATH
    try:
        committed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExposureDriftError(
            f"exposure artifact is missing: {ARTIFACT_RELATIVE_PATH}. "
            "Regenerate it with --write and commit the result."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ExposureDriftError(
            f"exposure artifact is unreadable: {ARTIFACT_RELATIVE_PATH} ({exc})"
        ) from exc
    if committed != dict(exposure):
        raise ExposureDriftError(
            "exposure drift: the committed artifact no longer matches the routing "
            "index. Another backend has claimed or released a bare id that "
            f"{BACKEND_NAME} publishes. Re-run with --write, review the diff, and "
            "commit it."
        )


def load_catalog_ids() -> tuple[str, ...]:
    """Return the catalog to reason about: the bounded offline fallback.

    Deliberately NOT a live network read. The artifact must be reproducible in CI
    and offline, and a check whose expected value depends on an upstream that
    changes hourly would fail for reasons unrelated to this repository.
    """
    from reverso.protocols.adapters.opencode.catalog import FALLBACK_MODEL_IDS

    return FALLBACK_MODEL_IDS


def load_model_index() -> Mapping[str, str]:
    """Return the live routing index that decides which ids are contested."""
    from reverso.protocols.surface_registry import _MODEL_INDEX

    return dict(_MODEL_INDEX)


def main(*, repo_root: Path, argv: Sequence[str] | None = None) -> int:
    """CLI entry point. ``--check`` never writes; the default writes."""
    parser = argparse.ArgumentParser(
        prog="check-opencode-exposure",
        description="Verify or regenerate the OpenCode Go bare-exposure artifact.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="fail closed when the committed artifact is stale; never write",
    )
    group.add_argument(
        "--write", action="store_true", help="regenerate the artifact in place"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    exposure = compute_exposure(
        catalog_ids=load_catalog_ids(), model_index=load_model_index()
    )

    if args.check:
        try:
            verify_artifact(repo_root, exposure)
        except ExposureDriftError as exc:
            print(f"opencode-exposure: {exc}")
            return 1
        print(
            "opencode-exposure: artifact matches "
            f"({len(exposure['bare_exposed'])} bare, "
            f"{len(exposure['qualified_only'])} prefix-only)"
        )
        return 0

    path = repo_root / ARTIFACT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_artifact(exposure), encoding="utf-8")
    print(f"opencode-exposure: wrote {ARTIFACT_RELATIVE_PATH}")
    return 0
