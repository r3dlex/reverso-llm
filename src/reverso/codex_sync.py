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
import re
import shutil
import sys
import tempfile
import tomllib
import typing as t
from dataclasses import dataclass
from pathlib import Path

import httpx

GATEWAY_BASE_URL = "http://127.0.0.1:64946"
GATEWAY_PREFIXES: tuple[str, ...] = ("claude", "copilot", "auggie", "deepseek")


def _has_safe_model_id_chars(model_id: str) -> bool:
    return model_id.isascii() and all(
        not char.isspace() and 32 <= ord(char) < 127 for char in model_id
    )


def _is_codex_copilot_responses_compatible_model(model_id: str) -> bool:
    if not _has_safe_model_id_chars(model_id):
        return False
    return model_id.startswith("gpt-5")


def _codex_responses_compatible_models(prefix: str, model_ids: list[str]) -> list[str]:
    """Filter live listings to models Codex can call through Responses."""
    if prefix != "copilot":
        return model_ids
    return [
        model_id
        for model_id in model_ids
        if _is_codex_copilot_responses_compatible_model(model_id)
    ]


PROFILES_BEGIN = "# BEGIN REVERSO MODELS PROFILES (managed by reverso-codex-sync)"
PROFILES_END = "# END REVERSO MODELS PROFILES (managed by reverso-codex-sync)"
NUX_BEGIN = "# BEGIN REVERSO MODELS NUX (managed by reverso-codex-sync)"
NUX_END = "# END REVERSO MODELS NUX (managed by reverso-codex-sync)"
CATALOG_BEGIN = "# BEGIN REVERSO MODEL CATALOG (managed by reverso-codex-sync)"
CATALOG_END = "# END REVERSO MODEL CATALOG (managed by reverso-codex-sync)"

BACKUPS_KEPT = 5
BACKUP_SUFFIX_PREFIX = ".reverso-sync."

DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
DEFAULT_CATALOG_PATH = Path.home() / ".codex" / "reverso-model-catalog.json"

# The trailing \r? keeps CRLF-edited configs on the header-aware merge path;
# with MULTILINE, $ anchors before \n only, so the \r must be consumed.
_NUX_TABLE_HEADER_RE = re.compile(
    r"^[ \t]*\[tui\.model_availability_nux\][ \t]*(?:#.*)?\r?$",
    re.MULTILINE,
)
_TABLE_HEADER_LINE_RE = re.compile(r"^[ \t]*\[", re.MULTILINE)


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
        ids = _codex_responses_compatible_models(prefix, fetcher(prefix))
        deduped: list[str] = []
        seen: set[str] = set()
        for model_id in ids:
            if model_id not in seen:
                seen.add(model_id)
                deduped.append(model_id)
        out.append(ProviderModels(prefix=prefix, models=tuple(deduped)))
    return out


def _render_profiles_block(
    provider_models: list[ProviderModels],
    catalog_path: Path | None = None,
) -> str:
    """Render the managed profiles block between PROFILES_BEGIN/END sentinels.

    Each entry is a ``[model_providers.reverso_<prefix>__<model_id>]`` table
    whose only purpose is to surface the ``(prefix, model)`` pair to Codex
    via the model_providers map. The base provider entries
    (``model_providers.reverso_<prefix>``) remain hand-managed outside this
    block; this script only adds per-model overlay tables and never touches
    the base entries.
    """
    lines: list[str] = [PROFILES_BEGIN]
    seen_sections: set[str] = set()
    for entry in provider_models:
        for model_id in entry.models:
            section = f"reverso_{entry.prefix}__{_toml_table_key(model_id)}"
            if section in seen_sections:
                continue
            seen_sections.add(section)
            lines.append("")
            lines.append(f"[model_providers.{section}]")
            lines.append(f"name = {_toml_string(f'Reverso {entry.prefix} {model_id}')}")
            lines.append(f'base_url = "{GATEWAY_BASE_URL}/{entry.prefix}/v1"')
            lines.append('wire_api = "responses"')
            lines.append(f"model = {_toml_string(model_id)}")
            if catalog_path:
                lines.append(f"model_catalog_json = {_toml_string(str(catalog_path))}")
    lines.append("")
    lines.append(PROFILES_END)
    return "\n".join(lines)


def _render_nux_block(provider_models: list[ProviderModels]) -> str:
    """Render the managed NUX block including the table header.

    Only used when the user config does NOT already define
    ``[tui.model_availability_nux]``; emitting the header next to an existing
    user-owned table would be a duplicate-table TOML error.
    """
    lines: list[str] = [NUX_BEGIN]
    lines.append("[tui.model_availability_nux]")
    seen: set[str] = set()
    for entry in provider_models:
        for model_id in entry.models:
            # Dedupe on the coerced key so ids that collide after coercion
            # (gpt-5.5 vs gpt-5_5) surface the same single model the profiles
            # block keeps, never a picker entry without a backing profile.
            key = _toml_table_key(model_id)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{_toml_string(model_id)} = 4")
    lines.append(NUX_END)
    return "\n".join(lines)


def _generate_catalog_json(provider_models: list[ProviderModels]) -> str:
    """Generate Codex-compatible model catalog JSON for synced models."""
    models: list[dict[str, t.Any]] = []
    seen: set[str] = set()

    for entry in provider_models:
        for model_id in entry.models:
            if model_id in seen:
                continue
            seen.add(model_id)

            context_window = 128000
            if "500k" in model_id:
                context_window = 500000

            models.append(
                {
                    "slug": model_id,
                    "display_name": f"Reverso {entry.prefix} {model_id}",
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


def _render_catalog_config_block(catalog_path: Path) -> str:
    """Render the top-level Codex catalog pointer block."""
    return "\n".join(
        [
            CATALOG_BEGIN,
            f"model_catalog_json = {_toml_string(str(catalog_path))}",
            CATALOG_END,
        ]
    )


def _merge_catalog_config_block(text: str, catalog_path: Path | None) -> str:
    """Manage the top-level Codex ``model_catalog_json`` pointer.

    Codex loads custom metadata from a top-level key. Provider-local
    ``model_catalog_json`` entries do not feed the active catalog, so the
    managed pointer is inserted before the first TOML table.
    """
    had_catalog_block = _find_sentinel(text, CATALOG_BEGIN) != -1
    stripped = _strip_managed_block(text, CATALOG_BEGIN, CATALOG_END)
    if catalog_path is None:
        return stripped

    block = _render_catalog_config_block(catalog_path)
    if not stripped:
        return block + "\n"

    first_table = _TABLE_HEADER_LINE_RE.search(stripped)
    if first_table is None:
        if stripped and not stripped.endswith("\n"):
            stripped += "\n"
        return stripped + block + "\n"

    insert_at = first_table.start()
    prefix = stripped[:insert_at]
    suffix = stripped[insert_at:]
    if had_catalog_block:
        prefix = prefix.rstrip()
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + block + "\n\n" + suffix


def _render_nux_entries(
    provider_models: list[ProviderModels],
    existing_keys: frozenset[str],
) -> str | None:
    """Render a headerless fenced NUX block for merging into the user table.

    Keys the user already defines are excluded so the merged table never
    carries a duplicate key. Returns ``None`` when nothing new needs to be
    added, in which case no managed block should be present at all.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for entry in provider_models:
        for model_id in entry.models:
            key = _toml_table_key(model_id)
            if key in seen or model_id in existing_keys:
                continue
            seen.add(key)
            lines.append(f"{_toml_string(model_id)} = 4")
    if not lines:
        return None
    return "\n".join([NUX_BEGIN, *lines, NUX_END])


def _strip_managed_block(text: str, begin: str, end: str) -> str:
    """Remove a sentinel-delimited block (and its trailing newline) if present."""
    start_idx = _find_sentinel(text, begin)
    if start_idx == -1:
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


def _existing_nux_keys(text: str) -> frozenset[str]:
    """Return the keys the user already defines under the NUX table."""
    parsed = _parse_toml(text, "target config (outside managed blocks)")
    tui = parsed.get("tui")
    if not isinstance(tui, dict):
        return frozenset()
    nux = tui.get("model_availability_nux")
    if not isinstance(nux, dict):
        return frozenset()
    return frozenset(nux)


def _insert_into_nux_table(text: str, block: str) -> str:
    """Insert ``block`` inside the user's NUX table, before the next table.

    The caller guarantees the table header exists in ``text``. The block is
    placed immediately before the next ``[``-led header line (or at EOF when
    the NUX table is last), so user-owned key lines stay byte-identical.
    """
    header = _NUX_TABLE_HEADER_RE.search(text)
    if header is None:
        raise RuntimeError("NUX table header vanished during merge; refusing to write.")
    line_end = text.find("\n", header.end())
    if line_end == -1:
        return text + "\n" + block + "\n"
    scan_from = line_end + 1
    next_header = _TABLE_HEADER_LINE_RE.search(text, scan_from)
    if next_header is None:
        if not text.endswith("\n"):
            text += "\n"
        return text + block + "\n"
    insert_at = next_header.start()
    return text[:insert_at] + block + "\n" + text[insert_at:]


def _merge_nux_block(
    text: str,
    provider_models: list[ProviderModels],
) -> str:
    """Merge live model NUX entries into ``text`` without duplicating tables.

    When the user config already defines ``[tui.model_availability_nux]``,
    the managed block is rendered headerless, excludes user-owned keys, and
    is relocated inside that table (self-healing any block a previous run
    emitted elsewhere). Otherwise the block keeps its own header and the
    fixed-point replace/append path applies.
    """
    stripped = _strip_managed_block(text, NUX_BEGIN, NUX_END)
    if _NUX_TABLE_HEADER_RE.search(stripped) is None:
        return _replace_managed_block(
            text, NUX_BEGIN, NUX_END, _render_nux_block(provider_models)
        )
    existing_keys = _existing_nux_keys(stripped)
    entries_block = _render_nux_entries(provider_models, existing_keys)
    if entries_block is None:
        return stripped
    return _insert_into_nux_table(stripped, entries_block)


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


@dataclass
class SyncResult:
    """Outcome of one ``sync`` invocation, used by tests and the CLI."""

    target: Path
    changed: bool
    backup: Path | None
    rotated: list[Path]
    provider_models: list[ProviderModels]
    catalog: Path | None = None


def sync(
    target: Path = DEFAULT_CONFIG_PATH,
    *,
    prefixes: t.Iterable[str] = GATEWAY_PREFIXES,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    catalog_target: Path | None = None,
) -> SyncResult:
    """Synchronize ``target`` against live gateway models.

    The function is idempotent: a second call with the same fetcher output
    produces no diff and creates no backup.
    """
    fetch = fetcher if fetcher is not None else _default_fetcher(base_url)
    provider_models = fetch_all(prefixes, fetch)

    old_text = target.read_text(encoding="utf-8") if target.exists() else ""

    profiles_block = _render_profiles_block(provider_models)

    new_text = _merge_catalog_config_block(old_text, catalog_target)
    new_text = _replace_managed_block(
        new_text, PROFILES_BEGIN, PROFILES_END, profiles_block
    )
    new_text = _merge_nux_block(new_text, provider_models)

    if new_text == old_text:
        # The catalog is regenerated even when the config text is unchanged:
        # the config references the catalog path, so a deleted or stale
        # catalog file must come back on every sync, not only on config diffs.
        if catalog_target:
            _atomic_write(catalog_target, _generate_catalog_json(provider_models))
        return SyncResult(
            target=target,
            changed=False,
            backup=None,
            rotated=[],
            provider_models=provider_models,
            catalog=catalog_target,
        )

    # Fail-closed invariant: validation MUST precede backup and write so a
    # render bug can never replace a valid user config with broken TOML.
    _parse_toml(new_text, "rendered config")

    if catalog_target:
        catalog_json = _generate_catalog_json(provider_models)
        _atomic_write(catalog_target, catalog_json)

    backup = _make_backup(target, now=now)
    _atomic_write(target, new_text)
    rotated = _rotate_backups(target, keep=keep_backups)
    return SyncResult(
        target=target,
        changed=True,
        backup=backup,
        rotated=rotated,
        provider_models=provider_models,
        catalog=catalog_target,
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
        "--catalog",
        type=Path,
        default=None,
        help=(
            "Path to the reverso model-catalog.json to write "
            "(default: ~/.codex/reverso-model-catalog.json, env: "
            "REVERSO_CODEX_CATALOG). Inert without --write-catalog."
        ),
    )
    parser.add_argument(
        "--write-catalog",
        action="store_true",
        help="Whether to generate and reference a model catalog JSON.",
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


def _resolve_catalog_path(arg_value: Path | None) -> Path:
    if arg_value is not None:
        return arg_value
    env_value = os.environ.get("REVERSO_CODEX_CATALOG")
    if env_value:
        return Path(env_value)
    return DEFAULT_CATALOG_PATH


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
    catalog_target = _resolve_catalog_path(args.catalog) if args.write_catalog else None

    if args.dry_run:
        fetcher = _default_fetcher(base_url)
        provider_models = fetch_all(GATEWAY_PREFIXES, fetcher)
        report = {
            "target": str(target),
            "catalog_target": str(catalog_target) if catalog_target else None,
            "providers": {pm.prefix: list(pm.models) for pm in provider_models},
        }
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0

    try:
        result = sync(target=target, base_url=base_url, catalog_target=catalog_target)
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
        "catalog": str(result.catalog) if result.catalog else None,
        "providers": {pm.prefix: list(pm.models) for pm in result.provider_models},
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
