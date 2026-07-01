"""``reverso-codex-sync`` console script.

Synchronizes live per-provider model listings from the local reverso gateway
into Codex's static configuration so the TUI ``/model`` picker can see them
ONLY when the matching profile is selected with ``codex -p <prefix>``.

Per A2 decision (.omc/research/codex-model-picker.md), Codex 0.139.0 has no
native mechanism to feed ``/model`` from a custom provider's ``/v1/models``
endpoint. This script bridges that gap by GET-ing each reverso provider's
``/v1/models`` and idempotently writing one provider-name profile file per
gateway prefix beside ``~/.codex/config.toml``. Each profile pins ``model``,
``model_provider``, and a per-provider ``model_catalog_json`` so the
``/model`` picker is scoped to that provider only when the profile is active.
The DEFAULT config exposes NO reverso models.

The implementation operates on the raw TOML text rather than parsing and
serializing, because round-tripping through ``tomllib`` would drop comments and
formatting outside the managed regions, violating the byte-faithful
preservation contract.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import tomllib
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from reverso.protocols import model_exposure

logger = logging.getLogger(__name__)

GATEWAY_BASE_URL = "http://127.0.0.1:64946"
PROFILE_ARCHIVE_DIR = Path("Archive") / "reverso-codex-sync"
PROFILE_MANAGED_MARKER = "# Managed by reverso-codex-sync."


def _codex_responses_compatible_models(prefix: str, model_ids: list[str]) -> list[str]:
    """Filter live listings to models Codex can call through Responses."""
    return list(
        model_exposure.codex_responses_compatible_model_ids(prefix, tuple(model_ids))
    )


PROFILES_BEGIN = "# BEGIN REVERSO MODELS PROFILES (managed by reverso-codex-sync)"
PROFILES_END = "# END REVERSO MODELS PROFILES (managed by reverso-codex-sync)"
NUX_BEGIN = "# BEGIN REVERSO MODELS NUX (managed by reverso-codex-sync)"
NUX_END = "# END REVERSO MODELS NUX (managed by reverso-codex-sync)"
CATALOG_BEGIN = "# BEGIN REVERSO MODEL CATALOG (managed by reverso-codex-sync)"
CATALOG_END = "# END REVERSO MODEL CATALOG (managed by reverso-codex-sync)"
GATEWAY_PROVIDERS_BEGIN = (
    "# BEGIN REVERSO GATEWAY PROVIDERS (managed by reverso-codex-sync)"
)
GATEWAY_PROVIDERS_END = (
    "# END REVERSO GATEWAY PROVIDERS (managed by reverso-codex-sync)"
)

BACKUPS_KEPT = 5
BACKUP_SUFFIX_PREFIX = ".reverso-sync."

DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
# Per-provider catalog JSON files live under this directory, one per profile
# (e.g. ~/.codex/reverso/copilot.json). The directory is derived from the
# config file's parent so a custom --config relocates the catalogs too.
CATALOG_DIR_NAME = "reverso"

_TABLE_HEADER_LINE_RE = re.compile(r"^[ \t]*\[", re.MULTILINE)
_TOP_LEVEL_MODEL_LINE_RE = re.compile(r"^[ \t]*model[ \t]*=", re.MULTILINE)
# The trailing \r? keeps CRLF-edited configs on the strip path; with MULTILINE,
# $ anchors before \n only, so the \r must be consumed.
_ORPHAN_PROFILE_TABLE_RE = re.compile(
    r"^[ \t]*\[model_providers\.reverso_[^\]\n]+__[^\]\n]+\]" r"[ \t]*(?:#.*)?\r?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ProviderModels:
    """Live model listing for a single reverso prefix."""

    prefix: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class CatalogModelEntry:
    """One selectable model entry in a per-provider Codex model catalog."""

    prefix: str
    slug: str
    model_id: str


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
    *,
    skip_errors: bool = False,
) -> list[ProviderModels]:
    """Fetch model ids for every prefix; preserve order, drop empty results."""
    out: list[ProviderModels] = []
    for prefix in prefixes:
        try:
            fetched_ids = fetcher(prefix)
        except Exception as exc:
            if not skip_errors:
                raise
            logger.warning(
                "Skipping reverso model sync for %s: %s",
                prefix,
                type(exc).__name__,
            )
            continue
        ids = _codex_responses_compatible_models(prefix, fetched_ids)
        deduped: list[str] = []
        seen: set[str] = set()
        for model_id in ids:
            if model_id not in seen:
                seen.add(model_id)
                deduped.append(model_id)
        out.append(ProviderModels(prefix=prefix, models=tuple(deduped)))
    return out


def _live_provider_models(
    provider_models: list[ProviderModels],
) -> list[ProviderModels]:
    """Return only the prefixes that have at least one live model.

    Ordering follows model_exposure's Reverso-routed profile Interface so the
    rendered profile files are deterministic regardless of fetch ordering.
    """
    by_prefix = {pm.prefix: pm for pm in provider_models if pm.models}
    return [
        by_prefix[prefix]
        for prefix in model_exposure.reverso_routed_codex_profile_prefixes()
        if prefix in by_prefix
    ]


def _catalog_path_for(catalog_dir: Path, prefix: str) -> Path:
    """Return the per-provider catalog JSON path for ``prefix``."""
    return catalog_dir / f"{prefix}.json"


def _profile_path_for(config_dir: Path, prefix: str) -> Path:
    """Return the Codex provider profile path for ``prefix``."""
    return config_dir / f"{prefix}.config.toml"


def _render_profile_file(
    *,
    model: str,
    model_provider: str,
    catalog_path: Path | None = None,
    model_context_window: int | None = None,
) -> str:
    """Render one provider-name Codex profile file."""
    lines = [
        PROFILE_MANAGED_MARKER,
        f"model = {_toml_string(model)}",
        f"model_provider = {_toml_string(model_provider)}",
    ]
    if catalog_path is not None:
        lines.append(f"model_catalog_json = {_toml_string(str(catalog_path))}")
    if model_context_window is not None:
        lines.append(f"model_context_window = {model_context_window}")
    return "\n".join(lines) + "\n"


def _reverso_profile_files(
    provider_models: list[ProviderModels],
    config_dir: Path,
    catalog_dir: Path,
) -> dict[Path, str]:
    """Return Reverso-routed provider profile files keyed by path."""
    files: dict[Path, str] = {}
    for entry in _live_provider_models(provider_models):
        spec = model_exposure.reverso_codex_profile_spec(entry.prefix, entry.models)
        catalog_path = (
            _catalog_path_for(catalog_dir, entry.prefix)
            if spec.uses_model_catalog
            else None
        )
        files[_profile_path_for(config_dir, entry.prefix)] = _render_profile_file(
            model=spec.model,
            model_provider=spec.model_provider,
            catalog_path=catalog_path,
        )
    return files


def _direct_profile_files(config_dir: Path) -> dict[Path, str]:
    """Return direct Codex provider profile files keyed by path."""
    files: dict[Path, str] = {}
    for spec in model_exposure.direct_codex_profile_specs():
        files[_profile_path_for(config_dir, spec.prefix)] = _render_profile_file(
            model=spec.model,
            model_provider=spec.model_provider,
            model_context_window=spec.model_context_window,
        )
    return files


def _profile_files(
    provider_models: list[ProviderModels],
    config_dir: Path,
    catalog_dir: Path,
) -> dict[Path, str]:
    """Return every provider-name profile file managed by the sync tool."""
    files = _reverso_profile_files(provider_models, config_dir, catalog_dir)
    files.update(_direct_profile_files(config_dir))
    return files


def _catalog_model_entries(entry: ProviderModels) -> list[CatalogModelEntry]:
    """Return one provider's catalog entries with Codex-visible selector slugs."""
    merged: list[CatalogModelEntry] = []
    seen_slugs: set[str] = set()
    for model_id in entry.models:
        slug = model_exposure.provider_scoped_catalog_slug(entry.prefix, model_id)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        merged.append(
            CatalogModelEntry(prefix=entry.prefix, slug=slug, model_id=model_id)
        )
    return merged


def _catalog_display_name(entry: CatalogModelEntry) -> str:
    """Return a human display name that makes routing ownership explicit."""
    return model_exposure.catalog_display_name(entry.prefix, entry.model_id)


def _generate_catalog_json(provider: ProviderModels) -> str:
    """Generate Codex-compatible catalog JSON for one provider's models."""
    models: list[dict[str, t.Any]] = []

    for entry in _catalog_model_entries(provider):
        context_window = model_exposure.codex_catalog_context_window(entry.model_id)

        models.append(
            {
                "slug": entry.slug,
                "display_name": _catalog_display_name(entry),
                "description": f"Reverso-synced {entry.prefix} model",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {
                        "effort": "low",
                        "description": "Fast responses with lighter reasoning",
                    },
                    {
                        "effort": "medium",
                        "description": "Balances speed and reasoning depth",
                    },
                    {
                        "effort": "high",
                        "description": "Greater reasoning depth for complex tasks",
                    },
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "base_instructions": "",
                "model_messages": {},
                "supports_reasoning_summaries": False,
                "default_reasoning_summary": "none",
                "support_verbosity": True,
                "default_verbosity": "low",
                "apply_patch_tool_type": "freeform",
                "web_search_tool_type": "text_and_image",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_parallel_tool_calls": True,
                "supports_image_detail_original": False,
                "context_window": context_window,
                "max_context_window": context_window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
                "use_responses_lite": False,
            }
        )

    return json.dumps({"models": models}, indent=2)


def _merge_catalog_config_block(text: str, catalog_path: Path | None) -> str:
    """Strip the legacy top-level managed catalog block.

    The default config no longer exposes a global ``model_catalog_json``
    pointer; per-provider catalogs are referenced from individual profiles
    instead. This function only ever strips a previously-written managed
    block, so ``catalog_path`` must be ``None``; it is retained so existing
    configs that still carry the block get cleaned up on every sync.
    """
    if catalog_path is not None:
        msg = "global catalog block is no longer written; pass None to strip it"
        raise ValueError(msg)
    return _strip_managed_block(text, CATALOG_BEGIN, CATALOG_END)


def _top_level_has_model_key(text: str) -> bool:
    """Return whether the root TOML document already selects a model."""
    search_end = len(text)
    first_table = _TABLE_HEADER_LINE_RE.search(text)
    if first_table is not None:
        search_end = first_table.start()
    return _TOP_LEVEL_MODEL_LINE_RE.search(text[:search_end]) is not None


def _ensure_default_model(text: str) -> str:
    """Insert Codex's default model unless the user already selected one."""
    if _top_level_has_model_key(text):
        return text
    line = f"model = {_toml_string(model_exposure.CODEX_DEFAULT_MODEL)}\n"
    if not text:
        return line
    first_table = _TABLE_HEADER_LINE_RE.search(text)
    if first_table is None:
        if text.endswith("\n"):
            return text + line
        return text + "\n" + line
    insert_at = first_table.start()
    prefix = text[:insert_at]
    suffix = text[insert_at:]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + line + suffix


def _gateway_provider_table(prefix: str, *, base_url: str = GATEWAY_BASE_URL) -> str:
    """Render one required Reverso Codex provider table."""
    provider = f"reverso_{prefix}"
    display = prefix.capitalize()
    return "\n".join(
        [
            f"[model_providers.{provider}]",
            f"name = {_toml_string(f'Reverso {display} profile')}",
            f"base_url = {_toml_string(f'{base_url}/{prefix}/v1')}",
            'wire_api = "responses"',
        ]
    )


def _ensure_gateway_provider_tables(
    text: str,
    prefixes: t.Iterable[str],
    *,
    base_url: str = GATEWAY_BASE_URL,
) -> str:
    """Append any missing Reverso provider tables required by profile files."""
    parsed = _parse_toml(text, "existing config")
    providers = parsed.get("model_providers")
    if not isinstance(providers, dict):
        providers = {}

    missing = [prefix for prefix in prefixes if f"reverso_{prefix}" not in providers]
    if not missing:
        return text

    block = "\n".join(
        [
            GATEWAY_PROVIDERS_BEGIN,
            *(_gateway_provider_table(prefix, base_url=base_url) for prefix in missing),
            GATEWAY_PROVIDERS_END,
        ]
    )
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        return text + "\n" + block + "\n"
    return block + "\n"


def _strip_overlay_tables(text: str) -> str:
    """Remove every legacy ``reverso_<prefix>__<id>`` overlay table, table-scoped.

    Each overlay is removed from its own ``[model_providers.reverso_*__*]`` header
    through the line before the NEXT TOML table header (or EOF). The span never
    reaches past a table boundary, so interleaved user tables/keys are preserved
    byte-faithfully even when an overlay sits between them. Overlays anywhere in
    the document are stripped, regardless of any surviving managed sentinel.
    """
    while True:
        match = _ORPHAN_PROFILE_TABLE_RE.search(text)
        if match is None:
            return text
        start = match.start()
        line_end = text.find("\n", match.end())
        scan_from = line_end + 1 if line_end != -1 else len(text)
        next_header = _TABLE_HEADER_LINE_RE.search(text, scan_from)
        end = next_header.start() if next_header is not None else len(text)
        text = text[:start] + text[end:]


def _strip_lone_sentinel_line(text: str, token: str) -> str:
    """Remove a single managed sentinel comment line (and its newline) if present.

    Used to clean up a stray ``PROFILES_END`` whose matching begin sentinel was
    lost; only the one comment line is removed, never surrounding content.
    """
    idx = _find_sentinel(text, token)
    if idx == -1:
        return text
    line_end = text.find("\n", idx)
    if line_end == -1:
        cut = idx - 1 if idx > 0 and text[idx - 1] == "\n" else idx
        return text[:cut]
    return text[:idx] + text[line_end + 1 :]


def _strip_orphan_profiles_block(text: str) -> str:
    """Remove legacy profile overlays whose begin sentinel was lost.

    Strips the orphan overlay tables themselves (table-scoped, never spanning
    arbitrary content) and any stray managed ``PROFILES_END`` comment line, so a
    partially hand-edited config is cleaned without deleting user-owned content.
    """
    return _strip_lone_sentinel_line(_strip_overlay_tables(text), PROFILES_END)


def _strip_managed_block(text: str, begin: str, end: str) -> str:
    """Remove a sentinel-delimited block (and its trailing newline) if present."""
    start_idx = _find_sentinel(text, begin)
    if start_idx == -1:
        if begin == PROFILES_BEGIN:
            return _strip_orphan_profiles_block(text)
        return text
    end_idx = _find_sentinel(text, end, start_idx)
    if end_idx == -1:
        msg = (
            f"Found managed begin sentinel without matching end sentinel: "
            f"{begin!r}. Refusing to write to avoid corruption."
        )
        raise RuntimeError(msg)
    tail_start = end_idx + len(end)
    if tail_start < len(text) and text[tail_start] == "\n":
        tail_start += 1
    return text[:start_idx] + text[tail_start:]


def _parse_toml(text: str, context: str) -> dict[str, t.Any]:
    """Parse TOML text, converting parse errors into fail-closed RuntimeErrors."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{context} is not valid TOML; refusing to write: {exc}"
        raise RuntimeError(msg) from exc


def _toml_table_key(model_id: str) -> str:
    """Coerce a model id to a TOML bare-key compatible token."""
    out_chars: list[str] = []
    for ch in model_id:
        if ch.isalnum() or ch in ("_", "-"):
            out_chars.append(ch)
        else:
            out_chars.append("_")
    return "".join(out_chars) or "model"


def _toml_string(value: str) -> str:
    """Encode ``value`` as a TOML basic string.

    JSON and TOML share basic-string escaping for everything json.dumps can
    emit (quotes, backslashes, control chars, \\uXXXX), so this round-trips
    through tomllib even for hostile model ids.
    """
    return json.dumps(value)


def _find_sentinel(text: str, token: str, start: int = 0) -> int:
    """Find ``token`` at a line start only, skipping mid-line mentions.

    A user comment that merely mentions a sentinel string mid-line must not
    be treated as a managed block boundary.
    """
    idx = text.find(token, start)
    while idx > 0 and text[idx - 1] != "\n":
        idx = text.find(token, idx + 1)
    return idx


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
    start_idx = _find_sentinel(text, begin)
    if start_idx == -1:
        if begin == PROFILES_BEGIN:
            text = _strip_managed_block(text, begin, end)
        if text and not text.endswith("\n"):
            text = text + "\n"
        if text:
            return text + "\n" + new_block + "\n"
        return new_block + "\n"

    end_idx = _find_sentinel(text, end, start_idx)
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


def _default_catalog_dir(target: Path) -> Path:
    """Return the per-provider catalog directory for a config ``target``."""
    return target.parent / CATALOG_DIR_NAME


def _write_per_provider_catalogs(
    provider_models: list[ProviderModels],
    catalog_dir: Path,
) -> list[Path]:
    """Write one catalog JSON per live provider; return the paths written.

    Each file contains only that provider's models with Codex-visible slugs.
    Files are written for the same prefixes (and order) the profiles block
    references, so a profile never points at a missing catalog.
    """
    written: list[Path] = []
    for entry in _live_provider_models(provider_models):
        spec = model_exposure.reverso_codex_profile_spec(entry.prefix, entry.models)
        if not spec.uses_model_catalog:
            continue
        path = _catalog_path_for(catalog_dir, entry.prefix)
        _atomic_write(path, _generate_catalog_json(entry))
        written.append(path)
    return written


def _is_managed_profile_text(text: str) -> bool:
    """Return whether profile text is owned by this sync tool."""
    return (
        text.startswith(PROFILE_MANAGED_MARKER + "\n")
        or text.strip() == PROFILE_MANAGED_MARKER
    )


def _is_direct_profile_path(path: Path) -> bool:
    """Return whether ``path`` is one of the direct Codex profile files."""
    return path.name in {
        f"{spec.prefix}.config.toml"
        for spec in model_exposure.direct_codex_profile_specs()
    }


def _unique_archive_path(
    archive_dir: Path,
    source_name: str,
    *,
    now: datetime.datetime | None = None,
) -> Path:
    """Return a unique archive path under ``archive_dir`` for ``source_name``."""
    stamp = _utc_timestamp(now)
    archive_path = archive_dir / f"{source_name}{BACKUP_SUFFIX_PREFIX}{stamp}"
    suffix = 0
    while archive_path.exists():
        suffix += 1
        archive_path = archive_dir / (
            f"{source_name}{BACKUP_SUFFIX_PREFIX}{stamp}.{suffix}"
        )
    return archive_path


def _archive_file(
    path: Path,
    archive_dir: Path,
    *,
    now: datetime.datetime | None = None,
) -> Path:
    """Move ``path`` into ``archive_dir`` and return its new location."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = _unique_archive_path(archive_dir, path.name, now=now)
    shutil.move(str(path), archive_path)
    return archive_path


def _write_profile_files(
    profile_files: dict[Path, str],
    *,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
) -> tuple[list[Path], list[Path], list[Path], bool]:
    """Write changed profile files and return paths, backups, rotations, changed."""
    written: list[Path] = []
    backups: list[Path] = []
    rotated: list[Path] = []
    changed = False
    for path, text in profile_files.items():
        old_text = path.read_text(encoding="utf-8") if path.exists() else None
        if old_text == text:
            written.append(path)
            continue
        if (
            old_text is not None
            and _is_direct_profile_path(path)
            and not _is_managed_profile_text(old_text)
        ):
            # Direct OpenAI/MiniMax profiles may be user-owned. Create them on
            # first run and keep managing files with our marker, but never
            # overwrite an unmarked direct provider profile.
            continue
        changed = True
        backup = _make_backup(path, now=now)
        if backup is not None:
            backups.append(backup)
        _atomic_write(path, text)
        rotated.extend(_rotate_backups(path, keep=keep_backups))
        written.append(path)
    return written, backups, rotated, changed


def _archive_stale_variant_profiles(
    config_dir: Path,
    *,
    now: datetime.datetime | None = None,
) -> list[Path]:
    """Archive only known generated variant profile files.

    The sync used to leave provider variant profiles behind. The archive path is
    intentionally narrow and exact-match only so user-owned profiles are not
    touched.
    """
    archived: list[Path] = []
    archive_dir = config_dir / PROFILE_ARCHIVE_DIR
    for stem in sorted(model_exposure.stale_codex_variant_profile_stems()):
        path = _profile_path_for(config_dir, stem)
        if not path.exists():
            continue
        archived.append(_archive_file(path, archive_dir, now=now))
    return archived


def _archive_stale_managed_reverso_profiles(
    config_dir: Path,
    catalog_dir: Path,
    live_prefixes: set[str],
    *,
    now: datetime.datetime | None = None,
) -> list[Path]:
    """Archive managed Reverso profile/catalog files for no-longer-live prefixes.

    Only files carrying this tool's profile marker are moved. Unmarked
    hand-written profile files are preserved even when their prefix is absent
    from the current gateway listing.
    """
    archived: list[Path] = []
    archive_dir = config_dir / PROFILE_ARCHIVE_DIR
    for prefix in model_exposure.reverso_routed_codex_profile_prefixes():
        if prefix in live_prefixes:
            continue
        profile_path = _profile_path_for(config_dir, prefix)
        if not profile_path.exists():
            continue
        profile_text = profile_path.read_text(encoding="utf-8")
        if not _is_managed_profile_text(profile_text):
            continue
        catalog_path = _catalog_path_for(catalog_dir, prefix)
        try:
            parsed = _parse_toml(profile_text, f"existing profile {profile_path.name}")
        except RuntimeError:
            parsed = {}
        profile_catalog = (
            parsed.get("model_catalog_json") if isinstance(parsed, dict) else None
        )
        if isinstance(profile_catalog, str):
            candidate = Path(profile_catalog)
            if candidate.parent == catalog_dir:
                catalog_path = candidate
        archived.append(_archive_file(profile_path, archive_dir, now=now))
        if catalog_path.exists():
            archived.append(_archive_file(catalog_path, archive_dir, now=now))
    return archived


@dataclass
class SyncResult:
    """Outcome of one ``sync`` invocation, used by tests and the CLI."""

    target: Path
    changed: bool
    backup: Path | None
    rotated: list[Path]
    provider_models: list[ProviderModels]
    catalog_dir: Path | None = None
    catalogs: list[Path] = field(default_factory=list)
    profiles: list[Path] = field(default_factory=list)
    profile_backups: list[Path] = field(default_factory=list)
    archived_profiles: list[Path] = field(default_factory=list)


def sync(
    target: Path = DEFAULT_CONFIG_PATH,
    *,
    prefixes: t.Iterable[str] | None = None,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    catalog_dir: Path | None = None,
) -> SyncResult:
    """Synchronize ``target`` against live gateway models.

    Writes one ``<prefix>.config.toml`` profile file per gateway prefix with
    live models and one per-provider catalog JSON under ``catalog_dir``
    (default ``<target.parent>/reverso``). The default config exposes no
    reverso models; they are only selectable via ``codex -p <prefix>``. Any
    legacy global catalog, NUX, or profiles managed block is stripped.

    The function is idempotent: a second call with the same fetcher output
    produces no diff and creates no backup.
    """
    fetch = fetcher if fetcher is not None else _default_fetcher(base_url)
    sync_prefixes = (
        tuple(prefixes)
        if prefixes is not None
        else model_exposure.reverso_routed_codex_profile_prefixes()
    )
    provider_models = fetch_all(
        sync_prefixes,
        fetch,
        skip_errors=fetcher is None,
    )
    if not provider_models:
        raise RuntimeError("no reverso provider model listings were available")

    catalog_dir = (
        catalog_dir if catalog_dir is not None else _default_catalog_dir(target)
    )

    old_text = target.read_text(encoding="utf-8") if target.exists() else ""

    profile_files = _profile_files(provider_models, target.parent, catalog_dir)
    live_prefixes = {entry.prefix for entry in _live_provider_models(provider_models)}

    new_text = _ensure_default_model(old_text)
    # Strip the legacy global catalog and NUX managed blocks; neither is
    # written any more. Profile files carry per-provider catalog pointers
    # instead.
    new_text = _merge_catalog_config_block(new_text, None)
    new_text = _strip_managed_block(new_text, NUX_BEGIN, NUX_END)
    new_text = _strip_managed_block(new_text, PROFILES_BEGIN, PROFILES_END)
    new_text = _ensure_gateway_provider_tables(
        new_text,
        model_exposure.reverso_routed_codex_profile_prefixes(),
        base_url=base_url,
    )

    for path, text in profile_files.items():
        _parse_toml(text, f"rendered profile {path.name}")

    if new_text == old_text:
        # The catalogs are regenerated even when the config text is unchanged:
        # the profiles reference these paths, so deleted or stale catalog files
        # must come back on every sync, not only on config diffs.
        catalogs = _write_per_provider_catalogs(provider_models, catalog_dir)
        (
            profiles,
            profile_backups,
            profile_rotated,
            profiles_changed,
        ) = _write_profile_files(
            profile_files,
            now=now,
            keep_backups=keep_backups,
        )
        archived_profiles = _archive_stale_variant_profiles(target.parent, now=now)
        archived_profiles.extend(
            _archive_stale_managed_reverso_profiles(
                target.parent,
                catalog_dir,
                live_prefixes,
                now=now,
            )
        )
        changed = profiles_changed or bool(archived_profiles)
        return SyncResult(
            target=target,
            changed=changed,
            backup=None,
            rotated=profile_rotated,
            provider_models=provider_models,
            catalog_dir=catalog_dir,
            catalogs=catalogs,
            profiles=profiles,
            profile_backups=profile_backups,
            archived_profiles=archived_profiles,
        )

    # Fail-closed invariant: validation MUST precede backup and write so a
    # render bug can never replace a valid user config with broken TOML.
    _parse_toml(new_text, "rendered config")

    catalogs = _write_per_provider_catalogs(provider_models, catalog_dir)
    (
        profiles,
        profile_backups,
        profile_rotated,
        _profiles_changed,
    ) = _write_profile_files(
        profile_files,
        now=now,
        keep_backups=keep_backups,
    )
    archived_profiles = _archive_stale_variant_profiles(target.parent, now=now)
    archived_profiles.extend(
        _archive_stale_managed_reverso_profiles(
            target.parent,
            catalog_dir,
            live_prefixes,
            now=now,
        )
    )

    backup = _make_backup(target, now=now)
    _atomic_write(target, new_text)
    rotated = _rotate_backups(target, keep=keep_backups)
    return SyncResult(
        target=target,
        changed=True,
        backup=backup,
        rotated=rotated + profile_rotated,
        provider_models=provider_models,
        catalog_dir=catalog_dir,
        catalogs=catalogs,
        profiles=profiles,
        profile_backups=profile_backups,
        archived_profiles=archived_profiles,
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
        "--catalog-dir",
        type=Path,
        default=None,
        help=(
            "Directory for per-provider catalog JSON files "
            "(default: <config dir>/reverso, env: REVERSO_CODEX_CATALOG_DIR)."
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


def _resolve_catalog_dir(arg_value: Path | None, config: Path) -> Path:
    if arg_value is not None:
        return arg_value
    env_value = os.environ.get("REVERSO_CODEX_CATALOG_DIR")
    if env_value:
        return Path(env_value)
    return _default_catalog_dir(config)


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
    catalog_dir = _resolve_catalog_dir(args.catalog_dir, target)

    if args.dry_run:
        fetcher = _default_fetcher(base_url)
        provider_models = fetch_all(
            model_exposure.reverso_routed_codex_profile_prefixes(),
            fetcher,
            skip_errors=True,
        )
        report = {
            "target": str(target),
            "catalog_dir": str(catalog_dir),
            "providers": {pm.prefix: list(pm.models) for pm in provider_models},
        }
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0

    try:
        result = sync(target=target, base_url=base_url, catalog_dir=catalog_dir)
    except httpx.HTTPError as exc:
        sys.stderr.write(f"reverso-codex-sync: gateway error: {exc}\n")
        return 2
    except RuntimeError as exc:
        sys.stderr.write(f"reverso-codex-sync: {exc}\n")
        return 3

    report = {
        "target": str(result.target),
        "changed": result.changed,
        "backup": str(result.backup) if result.backup else None,
        "rotated": [str(p) for p in result.rotated],
        "catalog_dir": str(result.catalog_dir) if result.catalog_dir else None,
        "catalogs": [str(p) for p in result.catalogs],
        "profiles": [str(p) for p in result.profiles],
        "profile_backups": [str(p) for p in result.profile_backups],
        "archived_profiles": [str(p) for p in result.archived_profiles],
        "providers": {pm.prefix: list(pm.models) for pm in result.provider_models},
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
