"""Claude Code settings repair for Reverso-managed overrides."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reverso.client_sync_lock import (
    ClientSyncLockBusy,
    HeldClientSyncLock,
    acquire_client_sync_lock,
    validate_client_sync_lock,
)
from reverso.client_sync_mutations import (
    FileState,
    PreparedGroup,
    PreparedMutation,
    apply_prepared_group,
    capture_state,
    file_state,
    missing_parent_mutations,
)

DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
DEFAULT_LAUNCHER_DIR = Path.home() / ".local" / "bin"
GATEWAY_BASE_URL = "http://127.0.0.1:64946"
PLACEHOLDER_BEARER = "reverso-local-loopback"
KIMI_MODEL = "kimi-k3"
KIMI_CONTEXT_WINDOW = "1048576"
LAUNCHER_MANAGED_MARKER = "# Managed by reverso-claude-code-sync."
LAUNCHER_CATALOGS: tuple[tuple[str, str], ...] = (
    ("claude-reverso", "all"),
    ("claude-claude", "claude"),
    ("claude-codex", "codex"),
    ("claude-copilot", "copilot"),
    ("claude-auggie", "auggie"),
    ("claude-deepseek", "deepseek"),
    ("claude-kimi", "kimi"),
    ("claude-ollama", "ollama"),
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
    # OpenCode Go (OCG-G3): the gateway holds this credential; no spawned CLI
    # needs it, and a launched agent inherits its parent environment wholesale.
    # Both the canonical name and the read-only alias are scrubbed.
    "OPENCODE_API_KEY",
    "OCGO_API_KEY",
)
KIMI_SCRUB_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
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


@dataclass(frozen=True)
class PreparedClaudeCodeSync:
    """Immutable Claude Code candidate and its user-facing result."""

    group: PreparedGroup
    result: ClaudeCodeSyncResult
    backup_path: Path | None = None


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


def _backup_settings(
    settings_path: Path,
    *,
    now: datetime | None = None,
) -> Path:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(settings_path, source_flags)
    try:
        source_mode = os.fstat(source_fd).st_mode & 0o777
        with os.fdopen(source_fd, "rb") as source:
            source_fd = -1
            contents = source.read()
    finally:
        if source_fd >= 0:
            os.close(source_fd)

    suffix = 0
    while True:
        suffix_text = "" if suffix == 0 else f".{suffix}"
        backup_path = settings_path.with_name(
            f"{settings_path.name}{BACKUP_SUFFIX_PREFIX}{timestamp}{suffix_text}"
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            backup_fd = os.open(backup_path, flags, source_mode)
        except FileExistsError:
            suffix += 1
            continue
        try:
            with os.fdopen(backup_fd, "wb") as backup:
                backup_fd = -1
                backup.write(contents)
            return backup_path
        except BaseException:
            if backup_fd >= 0:
                os.close(backup_fd)
            try:
                backup_path.unlink()
            except FileNotFoundError:
                pass
            raise


def _next_backup_path(
    settings_path: Path,
    *,
    now: datetime | None = None,
) -> Path:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    suffix = 0
    while True:
        suffix_text = "" if suffix == 0 else f".{suffix}"
        candidate = settings_path.with_name(
            f"{settings_path.name}{BACKUP_SUFFIX_PREFIX}{timestamp}{suffix_text}"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        suffix += 1


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


def _is_managed_launcher_state(state: FileState) -> bool:
    if state.kind != "file" or not isinstance(state.data, bytes):
        return False
    try:
        first_lines = state.data.decode("utf-8").splitlines()[:2]
    except UnicodeDecodeError:
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
    settings_env = {
        "ANTHROPIC_AUTH_TOKEN": PLACEHOLDER_BEARER,
        "ANTHROPIC_BASE_URL": GATEWAY_BASE_URL,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    if catalog == "kimi":
        settings_env.update(
            {
                "ANTHROPIC_MODEL": KIMI_MODEL,
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": KIMI_CONTEXT_WINDOW,
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": KIMI_CONTEXT_WINDOW,
            }
        )
    settings = json.dumps(
        {"env": settings_env},
        sort_keys=True,
        separators=(",", ":"),
    )
    scrub_env_keys = LAUNCHER_SCRUB_ENV_KEYS
    if catalog == "kimi":
        scrub_env_keys += KIMI_SCRUB_ENV_KEYS
    scrubbed = " ".join(scrub_env_keys)
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


def _sync_claude_code_settings_unlocked(
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


def prepare_sync(
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    *,
    launcher_dir: Path = DEFAULT_LAUNCHER_DIR,
    claude_executable: Path | None = None,
    backup: bool = True,
    indent: int = 2,
    now: datetime | None = None,
) -> PreparedClaudeCodeSync:
    """Prepare exact Claude Code bytes without mutating the filesystem."""
    settings_path = settings_path.expanduser()
    launcher_dir = launcher_dir.expanduser()
    settings_before = capture_state(settings_path)
    if settings_before.kind == "symlink":
        settings = None
        error = f"settings path must not be a symlink: {settings_path}"
    elif settings_before.kind == "absent":
        settings = None
        error = None
    elif settings_before.kind == "file" and isinstance(settings_before.data, bytes):
        try:
            raw_settings = json.loads(settings_before.data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            settings = None
            error = f"invalid JSON: {exc}"
        else:
            settings = raw_settings if isinstance(raw_settings, dict) else None
            error = (
                None if settings is not None else "settings root must be a JSON object"
            )
    else:
        settings, error = _load_settings(settings_path)
    if error is not None:
        return PreparedClaudeCodeSync(
            PreparedGroup("claude", ()),
            ClaudeCodeSyncResult(
                settings_path=str(settings_path),
                changed=False,
                dry_run=True,
                backup_path=None,
                removed_env_keys=(),
                removed_settings_keys=(),
                removed_model=None,
                launcher_dir=str(launcher_dir),
                error=error,
            ),
        )
    resolved_claude, executable_error = _resolve_claude_executable(claude_executable)
    if executable_error is not None or resolved_claude is None:
        return PreparedClaudeCodeSync(
            PreparedGroup("claude", ()),
            ClaudeCodeSyncResult(
                settings_path=str(settings_path),
                changed=False,
                dry_run=True,
                backup_path=None,
                removed_env_keys=(),
                removed_settings_keys=(),
                removed_model=None,
                launcher_dir=str(launcher_dir),
                error=executable_error,
            ),
        )
    rendered_launchers = {
        launcher_dir / name: _render_launcher(resolved_claude, catalog)
        for name, catalog in LAUNCHER_CATALOGS
    }
    launcher_before = {path: capture_state(path) for path in rendered_launchers}
    conflicts = tuple(
        path.name
        for path, state in launcher_before.items()
        if state.kind != "absent" and not _is_managed_launcher_state(state)
    )
    if conflicts:
        return PreparedClaudeCodeSync(
            PreparedGroup("claude", ()),
            ClaudeCodeSyncResult(
                settings_path=str(settings_path),
                changed=False,
                dry_run=True,
                backup_path=None,
                removed_env_keys=(),
                removed_settings_keys=(),
                removed_model=None,
                launcher_dir=str(launcher_dir),
                claude_executable=str(resolved_claude),
                conflicting_launchers=conflicts,
                error=f"unmanaged launcher conflict: {', '.join(conflicts)}",
            ),
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
        if launcher_before[path] != file_state(text, 0o755)
    )
    mutations: list[PreparedMutation] = []
    backup_path: Path | None = None
    if settings_changed:
        encoded = (json.dumps(cleaned, indent=indent, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if backup:
            backup_path = _next_backup_path(settings_path, now=now)
            backup_before = capture_state(backup_path)
            if backup_before.kind != "absent":
                raise RuntimeError(
                    f"backup path changed while preparing: {backup_path}"
                )
            mutations.append(
                PreparedMutation(
                    backup_path,
                    backup_before,
                    settings_before,
                )
            )
        mutations.append(
            PreparedMutation(settings_path, settings_before, file_state(encoded, 0o600))
        )
    else:
        mutations.append(
            PreparedMutation(settings_path, settings_before, settings_before)
        )
    for path, text in rendered_launchers.items():
        mutations.append(
            PreparedMutation(path, launcher_before[path], file_state(text, 0o755))
        )
    parent_mutations = missing_parent_mutations(
        mutation.path for mutation in mutations if mutation.after.kind != "absent"
    )
    group_mutations = (
        *parent_mutations,
        *mutations,
    )
    changed = any(mutation.changed for mutation in group_mutations)
    return PreparedClaudeCodeSync(
        PreparedGroup("claude", tuple(group_mutations)),
        ClaudeCodeSyncResult(
            settings_path=str(settings_path),
            changed=changed,
            dry_run=True,
            backup_path=None,
            removed_env_keys=removed_env_keys,
            rewritten_env_keys=rewritten_env_keys,
            removed_settings_keys=removed_settings_keys,
            removed_model=removed_model,
            launcher_dir=str(launcher_dir),
            claude_executable=str(resolved_claude),
            changed_launchers=changed_launchers,
        ),
        backup_path,
    )


def apply_prepared(
    prepared: PreparedClaudeCodeSync,
    *,
    lock_token: HeldClientSyncLock,
) -> ClaudeCodeSyncResult:
    """Apply the exact prepared Claude Code candidate under a held lock."""
    validate_client_sync_lock(lock_token)
    apply_prepared_group(prepared.group)
    return replace(
        prepared.result,
        dry_run=False,
        backup_path=(
            str(prepared.backup_path) if prepared.backup_path is not None else None
        ),
    )


def sync_claude_code_settings(
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    *,
    launcher_dir: Path = DEFAULT_LAUNCHER_DIR,
    claude_executable: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
    indent: int = 2,
    lock_path: Path | None = None,
    lock_token: HeldClientSyncLock | None = None,
) -> ClaudeCodeSyncResult:
    """Repair Claude Code client files under the shared writer lock."""
    kwargs = {
        "settings_path": settings_path,
        "launcher_dir": launcher_dir,
        "claude_executable": claude_executable,
        "backup": backup,
        "indent": indent,
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
    try:
        result = sync_claude_code_settings(
            args.settings_path,
            launcher_dir=args.launcher_dir,
            claude_executable=args.claude_executable,
            dry_run=args.dry_run,
            backup=not args.no_backup,
            indent=args.indent,
        )
    except ClientSyncLockBusy as exc:
        result = ClaudeCodeSyncResult(
            settings_path=str(args.settings_path),
            changed=False,
            dry_run=False,
            backup_path=None,
            removed_env_keys=(),
            removed_settings_keys=(),
            removed_model=None,
            launcher_dir=str(args.launcher_dir),
            error=f"lock_busy: {exc}",
        )
        json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
