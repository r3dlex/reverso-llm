"""Claude Code settings repair for Reverso-managed overrides."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
REVERSO_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_SMALL_FAST_MODEL",
)
REVERSO_MARKER_KEY = "_reverso_prev_model"
BROKEN_RESTORED_MODELS = frozenset({"haiku"})
BACKUP_SUFFIX_PREFIX = ".reverso.bak."


@dataclass(frozen=True)
class ClaudeCodeSyncResult:
    """Summary of a Claude Code settings sync attempt."""

    settings_path: str
    changed: bool
    dry_run: bool
    backup_path: str | None
    removed_env_keys: tuple[str, ...]
    removed_settings_keys: tuple[str, ...]
    removed_model: str | None
    error: str | None = None


def _load_settings(settings_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not settings_path.exists():
        return None, None
    try:
        raw_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(raw_settings, dict):
        return None, "settings root must be a JSON object"
    return raw_settings, None


def _backup_settings(settings_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = settings_path.with_name(
        f"{settings_path.name}{BACKUP_SUFFIX_PREFIX}{timestamp}"
    )
    shutil.copy2(settings_path, backup_path)
    return backup_path


def _atomic_write_json(
    settings_path: Path, settings: dict[str, Any], indent: int
) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{settings_path.name}.",
        suffix=".tmp",
        dir=settings_path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=indent, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, settings_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _remove_reverso_overrides(
    settings: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...], str | None]:
    cleaned = copy.deepcopy(settings)
    removed_env_keys: list[str] = []
    removed_settings_keys: list[str] = []
    removed_model: str | None = None

    env = cleaned.get("env")
    if isinstance(env, dict):
        for key in REVERSO_ENV_KEYS:
            if key in env:
                removed_env_keys.append(key)
                env.pop(key, None)
        if env:
            cleaned["env"] = env
        else:
            cleaned.pop("env", None)
            removed_settings_keys.append("env")

    had_reverso_marker = REVERSO_MARKER_KEY in cleaned
    if had_reverso_marker:
        cleaned.pop(REVERSO_MARKER_KEY, None)
        removed_settings_keys.append(REVERSO_MARKER_KEY)

    model = cleaned.get("model")
    if (
        had_reverso_marker
        and isinstance(model, str)
        and model in BROKEN_RESTORED_MODELS
    ):
        removed_model = model
        cleaned.pop("model", None)
        removed_settings_keys.append("model")

    return (
        cleaned,
        tuple(removed_env_keys),
        tuple(removed_settings_keys),
        removed_model,
    )


def sync_claude_code_settings(
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    *,
    dry_run: bool = False,
    backup: bool = True,
    indent: int = 2,
) -> ClaudeCodeSyncResult:
    """Remove Reverso-managed global overrides from Claude Code settings."""
    settings_path = settings_path.expanduser()
    settings, error = _load_settings(settings_path)
    if error is not None:
        return ClaudeCodeSyncResult(
            settings_path=str(settings_path),
            changed=False,
            dry_run=dry_run,
            backup_path=None,
            removed_env_keys=(),
            removed_settings_keys=(),
            removed_model=None,
            error=error,
        )
    if settings is None:
        return ClaudeCodeSyncResult(
            settings_path=str(settings_path),
            changed=False,
            dry_run=dry_run,
            backup_path=None,
            removed_env_keys=(),
            removed_settings_keys=(),
            removed_model=None,
        )

    cleaned, removed_env_keys, removed_settings_keys, removed_model = (
        _remove_reverso_overrides(settings)
    )
    changed = cleaned != settings
    backup_path: Path | None = None
    if changed and not dry_run:
        if backup:
            backup_path = _backup_settings(settings_path)
        _atomic_write_json(settings_path, cleaned, indent)

    return ClaudeCodeSyncResult(
        settings_path=str(settings_path),
        changed=changed,
        dry_run=dry_run,
        backup_path=str(backup_path) if backup_path is not None else None,
        removed_env_keys=removed_env_keys,
        removed_settings_keys=removed_settings_keys,
        removed_model=removed_model,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove Reverso-managed overrides from Claude Code settings."
    )
    parser.add_argument(
        "--settings-path",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help="Path to Claude Code settings.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing settings.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip backup before writing changed settings.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation for rewritten settings.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = sync_claude_code_settings(
        args.settings_path,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        indent=args.indent,
    )
    json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
