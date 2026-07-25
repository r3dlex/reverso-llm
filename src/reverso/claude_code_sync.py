"""Claude Code settings repair for Reverso-managed overrides."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
DEFAULT_LAUNCHER_DIR = Path.home() / ".local" / "bin"
GATEWAY_BASE_URL = "http://127.0.0.1:64946"
PLACEHOLDER_BEARER = "reverso-local-loopback"
LAUNCHER_MANAGED_MARKER = "# Managed by reverso-claude-code-sync."
LAUNCHER_CATALOGS: tuple[tuple[str, str], ...] = (
    ("claude-reverso", "all"),
    ("claude-claude", "claude"),
    ("claude-codex", "codex"),
    ("claude-copilot", "copilot"),
    ("claude-auggie", "auggie"),
    ("claude-deepseek", "deepseek"),
    ("claude-kimi", "kimi"),
)
LEGACY_REVERSO_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_SMALL_FAST_MODEL",
)
LAUNCHER_SCRUB_ENV_KEYS: tuple[str, ...] = (
    *LEGACY_REVERSO_ENV_KEYS,
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
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
    rewritten_env_keys: tuple[str, ...] = ()
    launcher_dir: str | None = None
    claude_executable: str | None = None
    changed_launchers: tuple[str, ...] = ()
    conflicting_launchers: tuple[str, ...] = ()
    error: str | None = None


def _load_settings(settings_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if settings_path.is_symlink():
        return None, f"settings path must not be a symlink: {settings_path}"
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
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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


def _atomic_write_launcher(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        os.fchmod(fd, 0o755)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _is_managed_launcher(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        first_lines = path.read_text(encoding="utf-8").splitlines()[:2]
    except (OSError, UnicodeDecodeError):
        return False
    return LAUNCHER_MANAGED_MARKER in first_lines


def _is_usable_claude(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return False
    return (
        resolved.is_file()
        and os.access(resolved, os.X_OK)
        and not _is_managed_launcher(resolved)
    )


def _resolve_claude_executable(
    claude_executable: Path | None,
) -> tuple[Path | None, str | None]:
    if claude_executable is not None:
        candidate = claude_executable.expanduser()
        if not _is_usable_claude(candidate):
            return None, f"invalid Claude executable: {candidate}"
        return candidate.resolve(), None

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            directory = os.curdir
        candidate = Path(directory).expanduser() / "claude"
        if _is_usable_claude(candidate):
            return candidate.resolve(), None
    return None, "real Claude executable not found"


def _render_launcher(claude_executable: Path, catalog: str) -> str:
    settings = json.dumps(
        {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": PLACEHOLDER_BEARER,
                "ANTHROPIC_BASE_URL": GATEWAY_BASE_URL,
                "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            }
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    scrubbed = " ".join(LAUNCHER_SCRUB_ENV_KEYS)
    return f"""#!/bin/sh
{LAUNCHER_MANAGED_MARKER}

for reverso_arg in "$@"; do
    case "$reverso_arg" in
        --settings|--settings=*|--setting-sources|--setting-sources=*)
            echo "error: --settings and --setting-sources are managed by reverso" >&2
            exit 2
            ;;
    esac
done

unset {scrubbed}
ANTHROPIC_CUSTOM_HEADERS="x-reverso-model-catalog: {catalog}
x-reverso-workspace: $PWD"
export ANTHROPIC_CUSTOM_HEADERS

exec {shlex.quote(str(claude_executable))} --settings {shlex.quote(settings)} "$@"
"""


def _launcher_plan(
    launcher_dir: Path,
    claude_executable: Path,
) -> tuple[dict[Path, str], tuple[str, ...]]:
    rendered = {
        launcher_dir / name: _render_launcher(claude_executable, catalog)
        for name, catalog in LAUNCHER_CATALOGS
    }
    conflicts = tuple(
        path.name
        for path in rendered
        if (path.exists() or path.is_symlink()) and not _is_managed_launcher(path)
    )
    return rendered, conflicts


def _remove_reverso_overrides(
    settings: dict[str, Any],
) -> tuple[
    dict[str, Any],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str | None,
]:
    cleaned = copy.deepcopy(settings)
    removed_env_keys: list[str] = []
    rewritten_env_keys: list[str] = []
    removed_settings_keys: list[str] = []
    removed_model: str | None = None

    env = cleaned.get("env")
    if isinstance(env, dict):
        has_reverso_base_url = env.get("ANTHROPIC_BASE_URL") == GATEWAY_BASE_URL
        custom_headers = env.get("ANTHROPIC_CUSTOM_HEADERS")
        retained_headers: list[str] = []
        has_reverso_headers = False
        if isinstance(custom_headers, str):
            for line in custom_headers.splitlines():
                header_name = line.partition(":")[0].strip().lower()
                if header_name.startswith("x-reverso-"):
                    has_reverso_headers = True
                else:
                    retained_headers.append(line)
        removable_env_keys = (
            (
                "ANTHROPIC_BASE_URL",
                has_reverso_base_url,
            ),
            (
                "ANTHROPIC_AUTH_TOKEN",
                env.get("ANTHROPIC_AUTH_TOKEN") == PLACEHOLDER_BEARER,
            ),
            (
                "ANTHROPIC_SMALL_FAST_MODEL",
                has_reverso_base_url,
            ),
            (
                "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
                has_reverso_base_url or has_reverso_headers,
            ),
        )
        for key, should_remove in removable_env_keys:
            if should_remove and key in env:
                removed_env_keys.append(key)
                env.pop(key, None)
        if has_reverso_headers:
            retained_custom_headers = "\n".join(retained_headers)
            if retained_custom_headers:
                env["ANTHROPIC_CUSTOM_HEADERS"] = retained_custom_headers
                rewritten_env_keys.append("ANTHROPIC_CUSTOM_HEADERS")
            else:
                env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
                removed_env_keys.append("ANTHROPIC_CUSTOM_HEADERS")
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
        tuple(rewritten_env_keys),
        tuple(removed_settings_keys),
        removed_model,
    )


def sync_claude_code_settings(
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    *,
    launcher_dir: Path = DEFAULT_LAUNCHER_DIR,
    claude_executable: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
    indent: int = 2,
) -> ClaudeCodeSyncResult:
    """Repair global settings and install process-local Reverso launchers."""
    settings_path = settings_path.expanduser()
    launcher_dir = launcher_dir.expanduser()
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
            launcher_dir=str(launcher_dir),
            error=error,
        )
    resolved_claude, executable_error = _resolve_claude_executable(claude_executable)
    if executable_error is not None or resolved_claude is None:
        return ClaudeCodeSyncResult(
            settings_path=str(settings_path),
            changed=False,
            dry_run=dry_run,
            backup_path=None,
            removed_env_keys=(),
            removed_settings_keys=(),
            removed_model=None,
            launcher_dir=str(launcher_dir),
            error=executable_error,
        )

    rendered_launchers, conflicts = _launcher_plan(launcher_dir, resolved_claude)
    if conflicts:
        return ClaudeCodeSyncResult(
            settings_path=str(settings_path),
            changed=False,
            dry_run=dry_run,
            backup_path=None,
            removed_env_keys=(),
            removed_settings_keys=(),
            removed_model=None,
            launcher_dir=str(launcher_dir),
            claude_executable=str(resolved_claude),
            conflicting_launchers=conflicts,
            error=f"unmanaged launcher conflict: {', '.join(conflicts)}",
        )

    (
        cleaned,
        removed_env_keys,
        rewritten_env_keys,
        removed_settings_keys,
        removed_model,
    ) = _remove_reverso_overrides(settings or {})
    settings_changed = settings is not None and cleaned != settings
    changed_launchers = tuple(
        path.name
        for path, text in rendered_launchers.items()
        if not path.exists()
        or path.read_text(encoding="utf-8") != text
        or (path.stat().st_mode & 0o777) != 0o755
    )
    changed = settings_changed or bool(changed_launchers)
    backup_path: Path | None = None
    if changed and not dry_run:
        if settings_changed and backup:
            backup_path = _backup_settings(settings_path)
        if settings_changed:
            _atomic_write_json(settings_path, cleaned, indent)
        for path, text in rendered_launchers.items():
            if path.name in changed_launchers:
                _atomic_write_launcher(path, text)

    return ClaudeCodeSyncResult(
        settings_path=str(settings_path),
        changed=changed,
        dry_run=dry_run,
        backup_path=str(backup_path) if backup_path is not None else None,
        removed_env_keys=removed_env_keys,
        rewritten_env_keys=rewritten_env_keys,
        removed_settings_keys=removed_settings_keys,
        removed_model=removed_model,
        launcher_dir=str(launcher_dir),
        claude_executable=str(resolved_claude),
        changed_launchers=changed_launchers,
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
        "--launcher-dir",
        type=Path,
        default=DEFAULT_LAUNCHER_DIR,
        help="Directory for Reverso-managed Claude launchers.",
    )
    parser.add_argument(
        "--claude-executable",
        type=Path,
        help="Real Claude executable to pin instead of resolving it from PATH.",
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
        launcher_dir=args.launcher_dir,
        claude_executable=args.claude_executable,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        indent=args.indent,
    )
    json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
