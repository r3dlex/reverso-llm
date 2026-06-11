"""``reverso-codex-sync`` console script.

Synchronizes live per-provider model listings from the local reverso gateway
into Codex's static configuration so the TUI ``/model`` picker can see them.

Per A2 decision (.omc/research/codex-model-picker.md), Codex 0.139.0 has no
native mechanism to feed ``/model`` from a custom provider's ``/v1/models``
endpoint. This script bridges that gap by GET-ing each reverso provider's
``/v1/models`` and idempotently writing per-model entries into
``~/.codex/config.toml`` under sentinel-marked managed sections, with full
backup, rotation, atomic replace, and unrelated-key byte-faithful preservation.

The implementation operates on the raw TOML text rather than parsing and
serializing, because round-tripping through ``tomllib`` would drop comments and
formatting outside the managed regions, violating the byte-faithful
preservation contract.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import typing as t
from dataclasses import dataclass
from pathlib import Path

import httpx

GATEWAY_BASE_URL = "http://127.0.0.1:64946"
GATEWAY_PREFIXES: tuple[str, ...] = ("claude", "copilot", "auggie", "deepseek")

PROFILES_BEGIN = "# BEGIN REVERSO MODELS PROFILES (managed by reverso-codex-sync)"
PROFILES_END = "# END REVERSO MODELS PROFILES (managed by reverso-codex-sync)"
NUX_BEGIN = "# BEGIN REVERSO MODELS NUX (managed by reverso-codex-sync)"
NUX_END = "# END REVERSO MODELS NUX (managed by reverso-codex-sync)"

BACKUPS_KEPT = 5
BACKUP_SUFFIX_PREFIX = ".reverso-sync."

DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "config.toml"


@dataclass(frozen=True)
class ProviderModels:
    """Live model listing for a single reverso prefix."""

    prefix: str
    models: tuple[str, ...]


ModelFetcher = t.Callable[[str], list[str]]


def _default_fetcher(base_url: str) -> ModelFetcher:
    """Return a fetcher that GETs ``{base_url}/{prefix}/v1/models`` via httpx."""

    def _fetch(prefix: str) -> list[str]:
        url = f"{base_url}/{prefix}/v1/models"
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        return _extract_model_ids(payload)

    return _fetch


def _extract_model_ids(payload: t.Any) -> list[str]:
    """Pull model id strings from an OpenAI-shaped ``/v1/models`` payload."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            model_id = entry.get("id")
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
    return ids


def fetch_all(
    prefixes: t.Iterable[str],
    fetcher: ModelFetcher,
) -> list[ProviderModels]:
    """Fetch model ids for every prefix; preserve order, drop empty results."""
    out: list[ProviderModels] = []
    for prefix in prefixes:
        ids = fetcher(prefix)
        deduped: list[str] = []
        seen: set[str] = set()
        for model_id in ids:
            if model_id not in seen:
                seen.add(model_id)
                deduped.append(model_id)
        out.append(ProviderModels(prefix=prefix, models=tuple(deduped)))
    return out


def _render_profiles_block(provider_models: list[ProviderModels]) -> str:
    """Render the managed profiles block between PROFILES_BEGIN/END sentinels.

    Each entry is a ``[model_providers.reverso_<prefix>__<model_id>]`` table
    whose only purpose is to surface the ``(prefix, model)`` pair to Codex
    via the model_providers map. The base provider entries
    (``model_providers.reverso_<prefix>``) remain hand-managed outside this
    block; this script only adds per-model overlay tables and never touches
    the base entries.
    """
    lines: list[str] = [PROFILES_BEGIN]
    for entry in provider_models:
        for model_id in entry.models:
            section = f"reverso_{entry.prefix}__{_toml_table_key(model_id)}"
            lines.append("")
            lines.append(f"[model_providers.{section}]")
            lines.append(f'name = "Reverso {entry.prefix} {model_id}"')
            lines.append(f'base_url = "{GATEWAY_BASE_URL}/{entry.prefix}/v1"')
            lines.append('wire_api = "responses"')
            lines.append(f'model = "{model_id}"')
    lines.append("")
    lines.append(PROFILES_END)
    return "\n".join(lines)


def _render_nux_block(provider_models: list[ProviderModels]) -> str:
    """Render the managed [tui.model_availability_nux] additions block.

    The base ``[tui.model_availability_nux]`` table stays hand-managed; this
    block injects per-model entries inside a separate fenced section so
    unrelated user entries are preserved.
    """
    lines: list[str] = [NUX_BEGIN]
    lines.append("[tui.model_availability_nux]")
    seen: set[str] = set()
    for entry in provider_models:
        for model_id in entry.models:
            if model_id in seen:
                continue
            seen.add(model_id)
            lines.append(f'"{model_id}" = 4')
    lines.append(NUX_END)
    return "\n".join(lines)


def _toml_table_key(model_id: str) -> str:
    """Coerce a model id to a TOML bare-key compatible token."""
    out_chars: list[str] = []
    for ch in model_id:
        if ch.isalnum() or ch in ("_", "-"):
            out_chars.append(ch)
        else:
            out_chars.append("_")
    return "".join(out_chars) or "model"


def _replace_managed_block(
    text: str,
    begin: str,
    end: str,
    new_block: str,
) -> str:
    """Replace an existing sentinel-delimited block, or append one if absent.

    Outside the block, the surrounding text is preserved byte-for-byte. The
    block itself is replaced wholesale by ``new_block``. The function is a
    fixed point: calling it twice with the same ``new_block`` produces the
    same output as calling it once.
    """
    start_idx = text.find(begin)
    if start_idx == -1:
        if text and not text.endswith("\n"):
            text = text + "\n"
        if text:
            return text + "\n" + new_block + "\n"
        return new_block + "\n"

    end_idx = text.find(end, start_idx)
    if end_idx == -1:
        msg = (
            f"Found managed begin sentinel without matching end sentinel: "
            f"{begin!r}. Refusing to write to avoid corruption."
        )
        raise RuntimeError(msg)
    tail_start = end_idx + len(end)
    if tail_start < len(text) and text[tail_start] == "\n":
        tail_start += 1
        leading_newline = "\n"
    else:
        leading_newline = ""
    return text[:start_idx] + new_block + leading_newline + text[tail_start:]


def _utc_timestamp(now: datetime.datetime | None = None) -> str:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def _list_existing_backups(target: Path) -> list[Path]:
    parent = target.parent
    if not parent.is_dir():
        return []
    prefix = target.name + BACKUP_SUFFIX_PREFIX
    out = [
        candidate
        for candidate in parent.iterdir()
        if candidate.is_file() and candidate.name.startswith(prefix)
    ]
    out.sort(key=lambda p: p.name)
    return out


def _make_backup(
    target: Path,
    now: datetime.datetime | None = None,
) -> Path | None:
    """Copy ``target`` to a timestamped sibling. Return the backup path.

    Returns ``None`` when ``target`` does not yet exist (first-run case).
    """
    if not target.exists():
        return None
    timestamp = _utc_timestamp(now)
    backup = target.with_name(target.name + BACKUP_SUFFIX_PREFIX + timestamp)
    suffix = 0
    while backup.exists():
        suffix += 1
        backup = target.with_name(
            target.name + BACKUP_SUFFIX_PREFIX + timestamp + f".{suffix}"
        )
    shutil.copy2(target, backup)
    return backup


def _rotate_backups(target: Path, keep: int = BACKUPS_KEPT) -> list[Path]:
    """Prune older backups so only the ``keep`` newest remain."""
    backups = _list_existing_backups(target)
    if len(backups) <= keep:
        return []
    to_remove = backups[: len(backups) - keep]
    removed: list[Path] = []
    for path in to_remove:
        path.unlink()
        removed.append(path)
    return removed


def _atomic_write(target: Path, new_text: str) -> None:
    """Write ``new_text`` to ``target`` via temp-file + ``os.replace``.

    The temp file is created in ``target.parent`` so that ``os.replace`` is an
    atomic same-filesystem rename. The temp file is unlinked on any failure.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        os.replace(tmp_path, target)
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


@dataclass
class SyncResult:
    """Outcome of one ``sync`` invocation, used by tests and the CLI."""

    target: Path
    changed: bool
    backup: Path | None
    rotated: list[Path]
    provider_models: list[ProviderModels]


def sync(
    target: Path = DEFAULT_CONFIG_PATH,
    *,
    prefixes: t.Iterable[str] = GATEWAY_PREFIXES,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
) -> SyncResult:
    """Synchronize ``target`` against live gateway models.

    The function is idempotent: a second call with the same fetcher output
    produces no diff and creates no backup.
    """
    fetch = fetcher if fetcher is not None else _default_fetcher(base_url)
    provider_models = fetch_all(prefixes, fetch)

    old_text = target.read_text(encoding="utf-8") if target.exists() else ""

    profiles_block = _render_profiles_block(provider_models)
    nux_block = _render_nux_block(provider_models)

    new_text = _replace_managed_block(
        old_text, PROFILES_BEGIN, PROFILES_END, profiles_block
    )
    new_text = _replace_managed_block(new_text, NUX_BEGIN, NUX_END, nux_block)

    if new_text == old_text:
        return SyncResult(
            target=target,
            changed=False,
            backup=None,
            rotated=[],
            provider_models=provider_models,
        )

    backup = _make_backup(target, now=now)
    _atomic_write(target, new_text)
    rotated = _rotate_backups(target, keep=keep_backups)
    return SyncResult(
        target=target,
        changed=True,
        backup=backup,
        rotated=rotated,
        provider_models=provider_models,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reverso-codex-sync",
        description=(
            "Sync live reverso gateway /v1/models into ~/.codex/config.toml "
            "so Codex TUI /model can pick reverso models."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to the codex config.toml to update "
            "(default: ~/.codex/config.toml, env: REVERSO_CODEX_CONFIG)."
        ),
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help=(
            "Reverso gateway base URL "
            "(default: http://127.0.0.1:64946, env: REVERSO_CODEX_BASE_URL)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the proposed diff status without writing.",
    )
    return parser


def _resolve_config_path(arg_value: Path | None) -> Path:
    if arg_value is not None:
        return arg_value
    env_value = os.environ.get("REVERSO_CODEX_CONFIG")
    if env_value:
        return Path(env_value)
    return DEFAULT_CONFIG_PATH


def _resolve_base_url(arg_value: str | None) -> str:
    if arg_value:
        return arg_value
    env_value = os.environ.get("REVERSO_CODEX_BASE_URL")
    if env_value:
        return env_value
    return GATEWAY_BASE_URL


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    target = _resolve_config_path(args.config)
    base_url = _resolve_base_url(args.base_url)

    if args.dry_run:
        fetcher = _default_fetcher(base_url)
        provider_models = fetch_all(GATEWAY_PREFIXES, fetcher)
        report = {
            "target": str(target),
            "providers": {pm.prefix: list(pm.models) for pm in provider_models},
        }
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0

    try:
        result = sync(target=target, base_url=base_url)
    except httpx.HTTPError as exc:
        sys.stderr.write(f"reverso-codex-sync: gateway error: {exc}\n")
        return 2

    report = {
        "target": str(result.target),
        "changed": result.changed,
        "backup": str(result.backup) if result.backup else None,
        "rotated": [str(p) for p in result.rotated],
        "providers": {pm.prefix: list(pm.models) for pm in result.provider_models},
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
