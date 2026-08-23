"""OpenCode client-surface sync: managed profile fragments under <opencode_config_dir>.

Sibling of ``claude_code_sync``/``codex_sync``: prepares exact bytes for every
Reverso-managed OpenCode profile fragment, requires the shared client-sync lock
to apply them, and never edits a user-owned ``opencode.json`` / ``opencode.jsonc``
(the operator applies the printed manual step instead). The generated fragments
bind the ``reverso`` provider to the inbound Anthropic Messages surface
(``baseURL`` is the gateway root), so OpenCode traffic traverses
``protocols.headroom_compression`` on ``/v1/messages`` exactly like Claude Code.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from reverso.client_sync_lock import (
    ClientSyncLockBusy,
    HeldClientSyncLock,
    acquire_client_sync_lock,
    validate_client_sync_lock,
)
from reverso.client_sync_mutations import (
    PreparedGroup,
    PreparedMutation,
    apply_prepared_group,
    capture_state,
    file_state,
    is_owned_by_marker,
    missing_parent_mutations,
)
from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    resolve_anthropic_backend,
)

COMMAND = "reverso-opencode-sync"

LAUNCHER_CATALOGS: tuple[tuple[str, str], ...] = (
    ("opencode-reverso", "all"),
    ("opencode-claude", "claude"),
    ("opencode-codex", "codex"),
    ("opencode-copilot", "copilot"),
    ("opencode-auggie", "auggie"),
    ("opencode-deepseek", "deepseek"),
    ("opencode-kimi", "kimi"),
    ("opencode-ollama", "ollama"),
)

GATEWAY_BASE_URL = "http://127.0.0.1:64946"
PLACEHOLDER_BEARER = "reverso-local-loopback"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "opencode"
MANAGED_MARKER = "// Managed by reverso-opencode-sync."
DISCOVERY_TIMEOUT_SECONDS = 10.0
USER_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
KNOWN_ANTHROPIC_BACKENDS: tuple[str, ...] = tuple(
    sorted(SURFACE_BACKENDS["anthropic"], key=len, reverse=True)
)

ModelsFetcher = Callable[[str], Any]


@dataclass(frozen=True)
class OpenCodeSyncResult:
    """Summary of an OpenCode profile fragment sync attempt."""

    config_dir: str
    changed: bool
    dry_run: bool
    changed_profiles: tuple[str, ...]
    conflicting_profiles: tuple[str, ...]
    model_count: int | None
    manual_step: str | None
    error: str | None = None


@dataclass(frozen=True)
class PreparedOpenCodeSync:
    """Immutable OpenCode candidate and its user-facing result."""

    group: PreparedGroup
    result: OpenCodeSyncResult


class ModelDiscoveryError(RuntimeError):
    """The gateway model listing is absent, malformed, or empty."""


def _default_models_fetcher(base_url: str) -> Any:
    response = httpx.get(
        f"{base_url}/v1/models",
        timeout=DISCOVERY_TIMEOUT_SECONDS,
        headers={"accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def _extract_model_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        raise ModelDiscoveryError("models payload must be a JSON object")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ModelDiscoveryError("models payload must carry a data array")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ModelDiscoveryError("models payload rows must be JSON objects")
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelDiscoveryError("models payload rows must carry a non-empty id")
        ids.append(model_id.strip())
    return ids


def _route_for_model_id(model_id: str) -> str | None:
    normalized = model_id.strip().lower()
    if normalized.startswith("anthropic-"):
        remainder = normalized[len("anthropic-") :]
        for backend in KNOWN_ANTHROPIC_BACKENDS:
            prefix = f"{backend}-"
            if remainder.startswith(prefix) and len(remainder) > len(prefix):
                return backend
        return None
    return resolve_anthropic_backend(model_id)


def _manual_step(profile_path: Path) -> str:
    return (
        "Reverso never edits opencode.json/opencode.jsonc. To launch OpenCode "
        f"against this profile run: OPENCODE_CONFIG={profile_path} opencode. "
        "To merge it into your own config instead, copy the provider.reverso "
        "block from the fragment."
    )


def _failed_prepared(
    config_dir: Path,
    message: str,
    conflicts: tuple[str, ...] = (),
) -> PreparedOpenCodeSync:
    return PreparedOpenCodeSync(
        PreparedGroup("opencode", ()),
        OpenCodeSyncResult(
            config_dir=str(config_dir),
            changed=False,
            dry_run=True,
            changed_profiles=(),
            conflicting_profiles=conflicts,
            model_count=None,
            manual_step=None,
            error=message,
        ),
    )


def _is_managed_fragment_state(state: Any) -> bool:
    return is_owned_by_marker(state, MANAGED_MARKER, head_lines=1)


def _render_profile(base_url: str, selectors: list[str]) -> str:
    document = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "reverso": {
                "npm": "@ai-sdk/anthropic",
                "name": "Reverso (Anthropic Messages)",
                "options": {
                    "baseURL": base_url,
                    "apiKey": PLACEHOLDER_BEARER,
                },
                "models": {selector: {"name": selector} for selector in selectors},
            }
        },
    }
    body = json.dumps(document, indent=2, sort_keys=True)
    return f"{MANAGED_MARKER}\n{body}\n"


def prepare_sync(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    *,
    base_url: str = GATEWAY_BASE_URL,
    models_fetcher: ModelsFetcher | None = None,
) -> PreparedOpenCodeSync:
    """Prepare exact OpenCode fragment bytes without mutating the filesystem."""
    config_dir = config_dir.expanduser()
    fetcher = (
        models_fetcher
        if models_fetcher is not None
        else (lambda url: _default_models_fetcher(url))
    )
    try:
        model_ids = _extract_model_ids(fetcher(base_url))
    except (httpx.HTTPError, ModelDiscoveryError, OSError) as exc:
        return _failed_prepared(config_dir, f"model discovery failed: {exc}")
    if not model_ids:
        return _failed_prepared(config_dir, "model discovery returned no models")

    models_by_route: dict[str, list[str]] = {
        backend: [] for backend in SURFACE_BACKENDS["anthropic"]
    }
    for model_id in sorted(set(model_ids)):
        route = _route_for_model_id(model_id)
        if route is None:
            return _failed_prepared(
                config_dir, f"unroutable advertised model id: {model_id}"
            )
        models_by_route[route].append(model_id)

    rendered: dict[Path, str] = {}
    for name, catalog in LAUNCHER_CATALOGS:
        if catalog == "all":
            selectors = [
                model_id
                for backend in sorted(models_by_route)
                for model_id in models_by_route[backend]
            ]
        else:
            selectors = list(models_by_route[catalog])
        rendered[config_dir / f"{name}.jsonc"] = _render_profile(base_url, selectors)

    before = {path: capture_state(path) for path in rendered}
    conflicts = tuple(
        path.name.removesuffix(".jsonc")
        for path, state in before.items()
        if state.kind != "absent" and not _is_managed_fragment_state(state)
    )
    if conflicts:
        return _failed_prepared(
            config_dir,
            f"unmanaged OpenCode fragment conflict: {', '.join(conflicts)}",
            conflicts=conflicts,
        )

    mutations = [
        PreparedMutation(path, state, file_state(rendered[path], 0o600))
        for path, state in before.items()
    ]
    parent_mutations = missing_parent_mutations(path for path in rendered)
    group = PreparedGroup("opencode", (*parent_mutations, *mutations))
    changed_profiles = tuple(
        path.name.removesuffix(".jsonc")
        for path in rendered
        if before[path] != file_state(rendered[path], 0o600)
    )
    return PreparedOpenCodeSync(
        group,
        OpenCodeSyncResult(
            config_dir=str(config_dir),
            changed=group.changed,
            dry_run=True,
            changed_profiles=changed_profiles,
            conflicting_profiles=(),
            model_count=len(model_ids),
            manual_step=_manual_step(config_dir / "opencode-reverso.jsonc"),
        ),
    )


def apply_prepared(
    prepared: PreparedOpenCodeSync,
    *,
    lock_token: HeldClientSyncLock,
) -> OpenCodeSyncResult:
    """Apply the exact prepared OpenCode candidate under a held lock."""
    validate_client_sync_lock(lock_token)
    apply_prepared_group(prepared.group)
    return replace(prepared.result, dry_run=False)


def sync_opencode(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    *,
    base_url: str = GATEWAY_BASE_URL,
    dry_run: bool = False,
    lock_path: Path | None = None,
    lock_token: HeldClientSyncLock | None = None,
) -> OpenCodeSyncResult:
    """Converge managed OpenCode fragments under the shared writer lock."""
    kwargs: dict[str, Any] = {
        "config_dir": config_dir,
        "base_url": base_url,
    }
    if dry_run:
        return prepare_sync(**kwargs).result
    with acquire_client_sync_lock(path=lock_path, token=lock_token) as held:
        prepared = prepare_sync(**kwargs)
        if prepared.result.error is not None:
            return replace(prepared.result, dry_run=False)
        return apply_prepared(prepared, lock_token=held)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Converge Reverso-managed OpenCode profile fragments."
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help="Directory receiving Reverso-managed opencode-*.jsonc fragments.",
    )
    parser.add_argument(
        "--base-url",
        default=GATEWAY_BASE_URL,
        help="Reverso gateway root providing GET /v1/models.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing fragments.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = sync_opencode(
            args.config_dir,
            base_url=args.base_url,
            dry_run=args.dry_run,
        )
    except ClientSyncLockBusy as exc:
        result = OpenCodeSyncResult(
            config_dir=str(args.config_dir),
            changed=False,
            dry_run=False,
            changed_profiles=(),
            conflicting_profiles=(),
            model_count=None,
            manual_step=None,
            error=f"lock_busy: {exc}",
        )
        json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if result.manual_step:
        sys.stdout.write(f"{result.manual_step}\n")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
