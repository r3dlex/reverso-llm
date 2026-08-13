"""``reverso-codex-sync`` console script.

Synchronizes live per-provider model listings from the local reverso gateway
into Codex's static configuration so the TUI ``/model`` picker can see them
ONLY when the matching profile is selected with
``codex -p reverso-<prefix>``.

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
    missing_parent_mutations,
)
from reverso.protocols import feature_policy, model_exposure

logger = logging.getLogger(__name__)

GATEWAY_BASE_URL = "http://127.0.0.1:64946"
PROFILE_ARCHIVE_DIR = Path("Archive") / "reverso-codex-sync"
PROFILE_MANAGED_MARKER = "# Managed by reverso-codex-sync."
OPTIONAL_DISCOVERY_PREFIXES = frozenset({"codex-direct"})
MANAGED_REVERSO_PROFILE_PREFIXES = (
    *model_exposure.REVERSO_ROUTED_CODEX_PROFILE_PREFIXES,
    "codex-direct",
    "openai-pass-through",
)
PROVIDER_DISCOVERY_TIMEOUT_SECONDS = 10.0


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


class KimiDiscoveryError(RuntimeError):
    """Kimi sync input was not the canonical live K3-only listing."""


class ModelDiscoveryError(RuntimeError):
    """A provider returned a malformed OpenAI-shaped model listing."""


class ProviderFreshnessError(RuntimeError):
    """One or more required provider catalogs could not be refreshed."""


def _default_fetcher(base_url: str) -> ModelFetcher:
    """Return a fetcher that GETs ``{base_url}/{prefix}/v1/models`` via httpx."""

    def _fetch(prefix: str) -> list[str]:
        url = f"{base_url}/{prefix}/v1/models"
        response = httpx.get(url, timeout=PROVIDER_DISCOVERY_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        model_ids = _require_model_ids(payload)
        if prefix == "kimi" and (
            not isinstance(payload, dict)
            or payload.get("model_discovery_source") != "live"
            or model_ids != ["kimi-k3"]
        ):
            raise KimiDiscoveryError(
                "live Kimi model discovery must contain only kimi-k3"
            )
        return model_ids

    return _fetch


def _require_model_ids(payload: t.Any) -> list[str]:
    """Validate and return ids from an OpenAI-shaped model listing."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ModelDiscoveryError("model discovery response must contain a data list")
    ids: list[str] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            raise ModelDiscoveryError("model discovery data entries must be objects")
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ModelDiscoveryError(
                "model discovery data entries must contain a non-empty id"
            )
        ids.append(model_id)
    return ids


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
        except KimiDiscoveryError:
            raise
        except Exception as exc:
            if not skip_errors:
                raise
            logger.warning(
                "Skipping reverso model sync for %s: %s",
                prefix,
                type(exc).__name__,
            )
            continue
        if prefix == "kimi" and fetched_ids != ["kimi-k3"]:
            raise KimiDiscoveryError(
                "live Kimi model discovery must contain only kimi-k3"
            )
        ids = _codex_responses_compatible_models(prefix, fetched_ids)
        deduped: list[str] = []
        seen: set[str] = set()
        for model_id in ids:
            if model_id not in seen:
                seen.add(model_id)
                deduped.append(model_id)
        out.append(ProviderModels(prefix=prefix, models=tuple(deduped)))
    return out


def discover_provider_models(
    prefix: str,
    *,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
) -> ProviderModels:
    """Discover and validate one provider without preparing other surfaces."""
    fetch = fetcher if fetcher is not None else _default_fetcher(base_url)
    discovered = fetch_all((prefix,), fetch, skip_errors=fetcher is None)
    if not discovered:
        if prefix in OPTIONAL_DISCOVERY_PREFIXES:
            return ProviderModels(prefix=prefix, models=())
        raise ProviderFreshnessError(
            f"required reverso provider model discovery failed for: {prefix}"
        )
    entry = discovered[0]
    if prefix not in OPTIONAL_DISCOVERY_PREFIXES and not entry.models:
        raise ProviderFreshnessError(
            "required reverso provider model discovery returned no compatible "
            f"models for: {prefix}"
        )
    return entry


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
    """Return the bare Codex provider profile path for ``prefix``."""
    return config_dir / f"{prefix}.config.toml"


def _reverso_profile_path_for(config_dir: Path, prefix: str) -> Path:
    """Return the canonical Reverso-routed Codex profile path for ``prefix``."""
    return config_dir / f"reverso-{prefix}.config.toml"


def _provider_supports_feature(prefix: str, feature: str) -> bool:
    """Return whether the governed parity surface supports one feature."""
    return (
        feature_policy.CAPABILITY_TABLES.get(prefix, {}).get(feature)
        != feature_policy.UNSUPPORTED
    )


def _provider_applies_feature(prefix: str, feature: str) -> bool:
    """Return whether the provider actually applies a feature upstream.

    Only native/translated classifications change upstream behavior; partial
    means the gateway accepts the field as a best-effort no-op, so profiles
    and catalogs must not surface it as usable.
    """
    return feature_policy.CAPABILITY_TABLES.get(prefix, {}).get(feature) in (
        feature_policy.NATIVE,
        feature_policy.TRANSLATED,
    )


def _render_profile_file(
    *,
    model: str,
    model_provider: str,
    catalog_path: Path | None = None,
    model_context_window: int | None = None,
    model_auto_compact_token_limit: int | None = None,
    model_reasoning_summary: str | None = None,
    model_reasoning_effort: str | None = None,
) -> str:
    """Render one provider-name Codex profile file."""
    lines = [
        PROFILE_MANAGED_MARKER,
        f"model = {_toml_string(model)}",
        f"model_provider = {_toml_string(model_provider)}",
    ]
    if model_reasoning_effort is not None:
        lines.append(f"model_reasoning_effort = {_toml_string(model_reasoning_effort)}")
    if model_reasoning_summary is not None:
        lines.append(
            f"model_reasoning_summary = {_toml_string(model_reasoning_summary)}"
        )
    if catalog_path is not None:
        lines.append(f"model_catalog_json = {_toml_string(str(catalog_path))}")
    if model_context_window is not None:
        lines.append(f"model_context_window = {model_context_window}")
    if model_auto_compact_token_limit is not None:
        lines.append(
            f"model_auto_compact_token_limit = {model_auto_compact_token_limit}"
        )
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
        files[_reverso_profile_path_for(config_dir, entry.prefix)] = (
            _render_profile_file(
                model=spec.model,
                model_provider=spec.model_provider,
                catalog_path=catalog_path,
                model_context_window=spec.model_context_window,
                model_auto_compact_token_limit=spec.model_auto_compact_token_limit,
                model_reasoning_summary=(
                    None
                    if _provider_applies_feature(entry.prefix, "reasoning.summary")
                    else "none"
                ),
                # Pin the default effort only where the provider forwards it
                # upstream (native/translated). CLI-runner providers (claude,
                # auggie: parity partial) get no key; the gateway strips a
                # CLI-forced effort for them instead.
                model_reasoning_effort=(
                    "medium"
                    if _provider_applies_feature(entry.prefix, "reasoning.effort")
                    else None
                ),
            )
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
            model_auto_compact_token_limit=spec.model_auto_compact_token_limit,
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
    # Advertise reasoning levels only where the provider actually forwards
    # effort upstream (native/translated). CLI-runner providers accept effort
    # as a no-op (partial) and must not advertise levels the model ignores.
    reasoning_supported = _provider_applies_feature(provider.prefix, "reasoning.effort")
    default_reasoning_level = "medium" if reasoning_supported else None
    supported_reasoning_levels = (
        [
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
        ]
        if reasoning_supported
        else []
    )

    for entry in _catalog_model_entries(provider):
        context_window = model_exposure.codex_catalog_context_window(
            entry.prefix, entry.model_id
        )

        models.append(
            {
                "slug": entry.slug,
                "display_name": _catalog_display_name(entry),
                "description": f"Reverso-synced {entry.prefix} model",
                "default_reasoning_level": default_reasoning_level,
                "supported_reasoning_levels": supported_reasoning_levels,
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
    lines = [
        f"[model_providers.{provider}]",
        f"name = {_toml_string(f'Reverso {display} profile')}",
        f"base_url = {_toml_string(f'{base_url}/{prefix}/v1')}",
    ]
    if prefix == "claude":
        lines.append(f"experimental_bearer_token = {_toml_string('local-reverso')}")
    lines.append('wire_api = "responses"')
    return "\n".join(lines)


def _ensure_claude_provider_bearer(text: str) -> str:
    """Use a fixed loopback-only Bearer token in the Claude provider table."""
    header = re.compile(
        r"^[ \t]*\[model_providers\.reverso_claude\][ \t]*(?:#.*)?\r?$",
        re.MULTILINE,
    ).search(text)
    if header is None:
        return text

    line_end = text.find("\n", header.end())
    body_start = line_end + 1 if line_end != -1 else len(text)
    next_header = _TABLE_HEADER_LINE_RE.search(text, body_start)
    body_end = next_header.start() if next_header is not None else len(text)
    body = text[body_start:body_end]
    body = re.sub(
        r"^[ \t]*env_key[ \t]*=[^\r\n]*(?:\r?\n|$)",
        "",
        body,
        flags=re.MULTILINE,
    )
    if re.search(
        r"^[ \t]*experimental_bearer_token[ \t]*=",
        body,
        re.MULTILINE,
    ):
        return text[:body_start] + body + text[body_end:]

    newline = "\r\n" if "\r\n" in text[header.start() : body_end] else "\n"
    bearer_line = (
        f"experimental_bearer_token = {_toml_string('local-reverso')}{newline}"
    )
    wire_api = re.search(r"^[ \t]*wire_api[ \t]*=", body, re.MULTILINE)
    insert_at = wire_api.start() if wire_api is not None else len(body)
    body = body[:insert_at] + bearer_line + body[insert_at:]
    return text[:body_start] + body + text[body_end:]


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
        return _ensure_claude_provider_bearer(text)

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
        text = text + "\n" + block + "\n"
    else:
        text = block + "\n"
    return _ensure_claude_provider_bearer(text)


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
        now = datetime.datetime.now(datetime.UTC)
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
    _reject_symlink(target, "write target")
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
    *,
    dry_run: bool = False,
) -> tuple[list[Path], bool]:
    """Converge live provider catalogs; return their paths and change status.

    Each file contains only that provider's models with Codex-visible slugs.
    Files are written for the same prefixes (and order) the profiles block
    references, so a profile never points at a missing catalog.
    """
    written: list[Path] = []
    changed = False
    for entry in _live_provider_models(provider_models):
        spec = model_exposure.reverso_codex_profile_spec(entry.prefix, entry.models)
        if not spec.uses_model_catalog:
            continue
        path = _catalog_path_for(catalog_dir, entry.prefix)
        new_text = _generate_catalog_json(entry)
        new_bytes = new_text.encode()
        old_bytes = path.read_bytes() if path.exists() else None
        if old_bytes != new_bytes:
            changed = True
            if not dry_run:
                _atomic_write(path, new_text)
        written.append(path)
    return written, changed


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
    _reject_symlink(archive_path, "archive target")
    return archive_path


def _archive_file(
    path: Path,
    archive_dir: Path,
    *,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
) -> Path:
    """Move ``path`` into ``archive_dir`` and return its new location."""
    _reject_symlink(path, "archive source")
    _reject_symlink(archive_dir, "archive directory")
    archive_path = _unique_archive_path(archive_dir, path.name, now=now)
    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), archive_path)
    return archive_path


def _write_profile_files(
    profile_files: dict[Path, str],
    *,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    dry_run: bool = False,
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
        if dry_run:
            written.append(path)
            continue
        backup = _make_backup(path, now=now)
        if backup is not None:
            backups.append(backup)
        _atomic_write(path, text)
        rotated.extend(_rotate_backups(path, keep=keep_backups))
        written.append(path)
    return written, backups, rotated, changed


def _validate_canonical_reverso_profile_targets(
    profile_files: dict[Path, str],
) -> None:
    """Reject user-owned files at canonical Reverso-routed profile paths."""
    for path in profile_files:
        if not path.name.startswith("reverso-") or not path.exists():
            continue
        if not _is_managed_profile_text(path.read_text(encoding="utf-8")):
            raise RuntimeError(
                f"unmanaged canonical Reverso profile conflicts at {path}"
            )


def _managed_profile_catalog_path(profile_path: Path) -> Path | None:
    """Return the catalog explicitly referenced by a marker-owned profile."""
    if not profile_path.exists() or profile_path.is_symlink():
        return None
    profile_text = profile_path.read_text(encoding="utf-8")
    if not _is_managed_profile_text(profile_text):
        return None
    try:
        parsed = _parse_toml(profile_text, f"existing profile {profile_path.name}")
    except RuntimeError:
        return None
    profile_catalog = parsed.get("model_catalog_json")
    return Path(profile_catalog) if isinstance(profile_catalog, str) else None


def _catalog_is_owned(
    catalog_path: Path,
    *,
    config_dir: Path,
    prefix: str,
) -> bool:
    """Return whether a canonical or legacy managed profile owns this catalog."""
    return any(
        _managed_profile_catalog_path(profile_path) == catalog_path
        for profile_path in (
            _reverso_profile_path_for(config_dir, prefix),
            _profile_path_for(config_dir, prefix),
        )
    )


def _validate_catalog_mutations(
    provider_models: list[ProviderModels],
    *,
    config_dir: Path,
    catalog_dir: Path,
) -> None:
    """Reject changes to catalogs without an exact marker-owned profile reference."""
    for entry in _live_provider_models(provider_models):
        spec = model_exposure.reverso_codex_profile_spec(entry.prefix, entry.models)
        if not spec.uses_model_catalog:
            continue
        catalog_path = _catalog_path_for(catalog_dir, entry.prefix)
        if not catalog_path.exists():
            continue
        if not _catalog_is_owned(
            catalog_path,
            config_dir=config_dir,
            prefix=entry.prefix,
        ):
            raise RuntimeError(
                f"unmanaged per-provider catalog conflicts at {catalog_path}"
            )


def _reject_symlink(path: Path, context: str) -> None:
    """Reject both live and dangling symlinks before filesystem mutation."""
    if path.is_symlink():
        raise RuntimeError(f"{context} must not be a symlink: {path}")


def _validate_sync_paths(
    *,
    target: Path,
    catalog_dir: Path,
    profile_files: dict[Path, str],
    live_prefixes: set[str],
) -> None:
    """Reject symlinks across every path the sync may write or archive."""
    archive_dir = target.parent / PROFILE_ARCHIVE_DIR
    paths = {
        target,
        catalog_dir,
        archive_dir,
        *profile_files,
        *(_profile_path_for(target.parent, prefix) for prefix in live_prefixes),
        *(
            _profile_path_for(target.parent, stem)
            for stem in model_exposure.stale_codex_variant_profile_stems()
        ),
        *(
            _reverso_profile_path_for(target.parent, prefix)
            for prefix in MANAGED_REVERSO_PROFILE_PREFIXES
        ),
        *(
            _catalog_path_for(catalog_dir, prefix)
            for prefix in MANAGED_REVERSO_PROFILE_PREFIXES
        ),
    }
    for prefix in MANAGED_REVERSO_PROFILE_PREFIXES:
        profile_path = _reverso_profile_path_for(target.parent, prefix)
        if not profile_path.exists() or profile_path.is_symlink():
            continue
        profile_text = profile_path.read_text(encoding="utf-8")
        if not _is_managed_profile_text(profile_text):
            continue
        try:
            parsed = _parse_toml(
                profile_text,
                f"existing profile {profile_path.name}",
            )
        except RuntimeError:
            continue
        profile_catalog = parsed.get("model_catalog_json")
        if isinstance(profile_catalog, str):
            candidate = Path(profile_catalog)
            if candidate.parent == catalog_dir:
                paths.add(candidate)
    for path in paths:
        _reject_symlink(path, "sync target")
    if archive_dir.is_dir():
        for path in archive_dir.iterdir():
            _reject_symlink(path, "archive target")
    for path in (target, *profile_files):
        if not path.parent.is_dir():
            continue
        backup_prefix = path.name + BACKUP_SUFFIX_PREFIX
        for candidate in path.parent.iterdir():
            if candidate.name.startswith(backup_prefix):
                _reject_symlink(candidate, "backup target")


def _archive_legacy_managed_reverso_profiles(
    config_dir: Path,
    live_prefixes: set[str],
    *,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Archive marker-owned legacy bare profiles after canonical files exist."""
    archived: list[Path] = []
    archive_dir = config_dir / PROFILE_ARCHIVE_DIR
    for prefix in sorted(live_prefixes):
        path = _profile_path_for(config_dir, prefix)
        if not path.exists():
            continue
        if not _is_managed_profile_text(path.read_text(encoding="utf-8")):
            continue
        canonical = _reverso_profile_path_for(config_dir, prefix)
        if not canonical.exists() and not dry_run:
            raise RuntimeError(
                f"canonical Reverso profile was not written before migration: {canonical}"
            )
        archived.append(_archive_file(path, archive_dir, now=now, dry_run=dry_run))
    return archived


def _archive_stale_variant_profiles(
    config_dir: Path,
    *,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
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
        if not _is_managed_profile_text(path.read_text(encoding="utf-8")):
            continue
        archived.append(_archive_file(path, archive_dir, now=now, dry_run=dry_run))
    return archived


def _archive_stale_managed_reverso_profiles(
    config_dir: Path,
    catalog_dir: Path,
    live_prefixes: set[str],
    *,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Archive managed Reverso profile/catalog files for no-longer-live prefixes.

    Only files carrying this tool's profile marker are moved. Unmarked
    hand-written profile files are preserved even when their prefix is absent
    from the current gateway listing.
    """
    archived: list[Path] = []
    for prefix in MANAGED_REVERSO_PROFILE_PREFIXES:
        if prefix in live_prefixes:
            continue
        archived.extend(
            _archive_stale_managed_reverso_profile(
                config_dir,
                catalog_dir,
                prefix,
                now=now,
                dry_run=dry_run,
            )
        )
    return archived


def _archive_stale_managed_reverso_profile(
    config_dir: Path,
    catalog_dir: Path,
    prefix: str,
    *,
    now: datetime.datetime | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Archive one managed Reverso profile and its in-tree catalog."""
    profile_path = _reverso_profile_path_for(config_dir, prefix)
    if not profile_path.exists():
        return []
    profile_text = profile_path.read_text(encoding="utf-8")
    if not _is_managed_profile_text(profile_text):
        return []

    catalog_path = _managed_profile_catalog_path(profile_path)
    if catalog_path is not None and catalog_path.parent != catalog_dir:
        catalog_path = None

    archive_dir = config_dir / PROFILE_ARCHIVE_DIR
    archived = [
        _archive_file(
            profile_path,
            archive_dir,
            now=now,
            dry_run=dry_run,
        )
    ]
    if catalog_path is not None and catalog_path.exists():
        archived.append(
            _archive_file(
                catalog_path,
                archive_dir,
                now=now,
                dry_run=dry_run,
            )
        )
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


@dataclass(frozen=True)
class PreparedCodexSync:
    """Immutable Codex candidate and dry-run/apply summaries."""

    group: PreparedGroup
    result: SyncResult
    applied_result: SyncResult


def _tree_states(root: Path) -> dict[Path, FileState]:
    states: dict[Path, FileState] = {}
    if not root.is_dir() or root.is_symlink():
        return states
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directory_names, *file_names):
            path = current_path / name
            states[path.relative_to(root)] = capture_state(path)
    return states


def _matching_managed_siblings(parent: Path, prefixes: set[str]) -> set[Path]:
    """Return matching sibling names without inspecting unrelated entries."""
    if not parent.is_dir() or parent.is_symlink():
        return set()
    with os.scandir(parent) as entries:
        return {
            parent / entry.name
            for entry in entries
            if any(entry.name.startswith(prefix) for prefix in prefixes)
        }


def _managed_staging_paths(
    *,
    target: Path,
    catalog_dir: Path,
    prefixes: tuple[str, ...],
) -> set[Path]:
    """Return the bounded paths that Codex sync may read, rotate, or archive."""
    config_dir = target.parent
    managed_prefixes = {*MANAGED_REVERSO_PROFILE_PREFIXES, *prefixes}
    profile_paths = {
        *(_reverso_profile_path_for(config_dir, prefix) for prefix in managed_prefixes),
        *(_profile_path_for(config_dir, prefix) for prefix in managed_prefixes),
        *(
            _profile_path_for(config_dir, stem)
            for stem in model_exposure.stale_codex_variant_profile_stems()
        ),
        *(
            _profile_path_for(config_dir, spec.prefix)
            for spec in model_exposure.direct_codex_profile_specs()
        ),
    }
    catalog_paths = {
        _catalog_path_for(catalog_dir, prefix) for prefix in managed_prefixes
    }
    for profile_path in profile_paths:
        referenced = _managed_profile_catalog_path(profile_path)
        if referenced is not None and referenced.parent == catalog_dir:
            catalog_paths.add(referenced)

    archive_dir = config_dir / PROFILE_ARCHIVE_DIR
    paths = {target, archive_dir, *profile_paths, *catalog_paths}
    if catalog_dir != config_dir and catalog_dir.is_relative_to(config_dir):
        paths.add(catalog_dir)
    backup_prefixes = {
        path.name + BACKUP_SUFFIX_PREFIX for path in {target, *profile_paths}
    }
    paths.update(_matching_managed_siblings(config_dir, backup_prefixes))

    archive_prefixes = {
        path.name + BACKUP_SUFFIX_PREFIX for path in profile_paths | catalog_paths
    }
    paths.update(_matching_managed_siblings(archive_dir, archive_prefixes))
    return paths


def _materialize_state(path: Path, state: FileState) -> None:
    if state.kind == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.kind == "directory":
        if state.mode is None:
            raise RuntimeError(f"staged directory has no mode: {path}")
        path.mkdir(exist_ok=True)
        os.chmod(path, state.mode)
    elif state.kind == "file":
        if state.mode is None:
            raise RuntimeError(f"staged file has no mode: {path}")
        path.write_bytes(t.cast(bytes, state.data))
        os.chmod(path, state.mode)
    elif state.kind == "symlink":
        path.symlink_to(t.cast(str, state.data))
    else:
        raise RuntimeError(f"unsupported staged file state: {state.kind}")


def _copy_managed_paths(
    source_root: Path,
    destination_root: Path,
    paths: set[Path],
) -> dict[Path, FileState]:
    """Copy only explicit managed paths into one controlled staging root."""
    if source_root.is_symlink():
        raise RuntimeError(f"sync root must be a real directory: {source_root}")
    if source_root.exists() and not source_root.is_dir():
        raise RuntimeError(f"sync root must be a real directory: {source_root}")
    destination_root.mkdir(mode=0o700)
    states: dict[Path, FileState] = {}
    for path in sorted(paths):
        if not path.is_relative_to(source_root):
            continue
        relative = path.relative_to(source_root)
        for parent in reversed(relative.parents):
            if parent == Path(".") or parent in states:
                continue
            parent_state = capture_state(source_root / parent)
            states[parent] = parent_state
            _materialize_state(destination_root / parent, parent_state)
        state = capture_state(path)
        states[relative] = state
        _materialize_state(destination_root / relative, state)
    return states


def _translate_file_state(
    state: FileState,
    replacements: list[tuple[Path, Path]],
) -> FileState:
    if state.kind != "file" or not isinstance(state.data, bytes):
        return state
    try:
        text = state.data.decode("utf-8")
    except UnicodeDecodeError:
        return state
    lines = text.splitlines(keepends=True)
    changed = False
    for index, line in enumerate(lines):
        match = re.match(
            r'^(\s*model_catalog_json\s*=\s*)("(?:[^"\\]|\\.)*")(\s*)$', line
        )
        if match is None:
            continue
        value = json.loads(match.group(2))
        candidate = Path(value)
        for source, destination in replacements:
            if candidate == source or candidate.is_relative_to(source):
                mapped = destination / candidate.relative_to(source)
                lines[index] = match.group(1) + json.dumps(str(mapped)) + match.group(3)
                changed = True
                break
    if not changed:
        return state
    return FileState("file", "".join(lines).encode("utf-8"), state.mode)


def _translate_staging_tree(
    root: Path,
    replacements: list[tuple[Path, Path]],
) -> None:
    for relative, state in _tree_states(root).items():
        translated = _translate_file_state(state, replacements)
        if translated != state:
            (root / relative).write_bytes(t.cast(bytes, translated.data))


def _ordered_mutations(
    mutations: list[PreparedMutation],
) -> tuple[PreparedMutation, ...]:
    def key(mutation: PreparedMutation) -> tuple[int, int, str]:
        depth = len(mutation.path.parts)
        if mutation.after.kind == "directory":
            return (0, depth, str(mutation.path))
        if mutation.after.kind != "absent":
            return (1, depth, str(mutation.path))
        if mutation.before.kind != "directory":
            return (2, -depth, str(mutation.path))
        return (3, -depth, str(mutation.path))

    return tuple(sorted(mutations, key=key))


def _remap_result_path(
    path: Path | None, mappings: list[tuple[Path, Path]]
) -> Path | None:
    if path is None:
        return None
    for shadow_root, real_root in mappings:
        if path == shadow_root or path.is_relative_to(shadow_root):
            return real_root / path.relative_to(shadow_root)
    raise RuntimeError(f"prepared result path escaped staging roots: {path}")


def _remap_result(
    result: SyncResult,
    mappings: list[tuple[Path, Path]],
    *,
    dry_run: bool,
) -> SyncResult:
    return SyncResult(
        target=t.cast(Path, _remap_result_path(result.target, mappings)),
        changed=result.changed,
        backup=(None if dry_run else _remap_result_path(result.backup, mappings)),
        rotated=(
            []
            if dry_run
            else [
                t.cast(Path, _remap_result_path(path, mappings))
                for path in result.rotated
            ]
        ),
        provider_models=result.provider_models,
        catalog_dir=_remap_result_path(result.catalog_dir, mappings),
        catalogs=[
            t.cast(Path, _remap_result_path(path, mappings)) for path in result.catalogs
        ],
        profiles=[
            t.cast(Path, _remap_result_path(path, mappings)) for path in result.profiles
        ],
        profile_backups=(
            []
            if dry_run
            else [
                t.cast(Path, _remap_result_path(path, mappings))
                for path in result.profile_backups
            ]
        ),
        archived_profiles=[
            t.cast(Path, _remap_result_path(path, mappings))
            for path in result.archived_profiles
        ],
    )


def prepare_sync(
    target: Path = DEFAULT_CONFIG_PATH,
    *,
    prefixes: t.Iterable[str] | None = None,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    catalog_dir: Path | None = None,
    _provider_only: bool = False,
) -> PreparedCodexSync:
    """Render and validate exact Codex mutations in an isolated staging tree."""
    target = target.expanduser()
    sync_prefixes = (
        tuple(prefixes)
        if prefixes is not None
        else model_exposure.reverso_routed_codex_profile_prefixes()
    )
    real_catalog_dir = (
        catalog_dir.expanduser()
        if catalog_dir is not None
        else _default_catalog_dir(target)
    )
    config_root = target.parent
    real_roots = [config_root]
    if not real_catalog_dir.is_relative_to(config_root):
        real_roots.append(real_catalog_dir)
    managed_paths = _managed_staging_paths(
        target=target,
        catalog_dir=real_catalog_dir,
        prefixes=sync_prefixes,
    )
    with tempfile.TemporaryDirectory(prefix="reverso-codex-prepare-") as temp_name:
        staging = Path(temp_name)
        shadow_config_root = staging / "config"
        roots = [(shadow_config_root, config_root)]
        shadow_target = shadow_config_root / target.relative_to(config_root)
        if real_catalog_dir.is_relative_to(config_root):
            shadow_catalog_dir = shadow_config_root / real_catalog_dir.relative_to(
                config_root
            )
        else:
            shadow_catalog_root = staging / "catalog"
            roots.append((shadow_catalog_root, real_catalog_dir))
            shadow_catalog_dir = shadow_catalog_root
        tree_before = {
            real_root: _copy_managed_paths(
                real_root,
                shadow_root,
                managed_paths,
            )
            for shadow_root, real_root in roots
        }
        for shadow_root, real_root in roots:
            for relative, state in tree_before[real_root].items():
                real_path = real_root / relative
                if capture_state(real_path) != state:
                    raise RuntimeError(
                        f"sync input changed while preparing: {real_path}"
                    )
                if capture_state(shadow_root / relative) != state:
                    raise RuntimeError(
                        f"sync staging copy did not match source: {real_path}"
                    )
        real_to_shadow = [(real_root, shadow_root) for shadow_root, real_root in roots]
        shadow_to_real = [(shadow_root, real_root) for shadow_root, real_root in roots]
        for shadow_root, _real_root in roots:
            _translate_staging_tree(shadow_root, real_to_shadow)
        staged_result = _sync_unlocked(
            target=shadow_target,
            prefixes=sync_prefixes,
            fetcher=fetcher,
            base_url=base_url,
            now=now,
            keep_backups=keep_backups,
            catalog_dir=shadow_catalog_dir,
            dry_run=False,
            _provider_only=_provider_only,
        )
        mutations: list[PreparedMutation] = []
        for shadow_root, real_root in roots:
            old_states = tree_before[real_root]
            new_states = _tree_states(shadow_root)
            for relative in old_states.keys() | new_states.keys():
                old_state = old_states.get(relative, FileState("absent"))
                new_state = _translate_file_state(
                    new_states.get(relative, FileState("absent")),
                    shadow_to_real,
                )
                if old_state == new_state:
                    continue
                real_path = real_root / relative
                if capture_state(real_path) != old_state:
                    raise RuntimeError(
                        f"sync input changed while preparing: {real_path}"
                    )
                mutations.append(PreparedMutation(real_path, old_state, new_state))
        mutation_paths = {mutation.path for mutation in mutations}
        mutations.extend(
            mutation
            for mutation in missing_parent_mutations(
                mutation.path for mutation in mutations
            )
            if mutation.path not in mutation_paths
        )
        mappings = roots
        applied_result = _remap_result(staged_result, mappings, dry_run=False)
        dry_result = _remap_result(staged_result, mappings, dry_run=True)
    return PreparedCodexSync(
        PreparedGroup("codex", _ordered_mutations(mutations)),
        dry_result,
        applied_result,
    )


def prepare_provider_sync(
    provider_models: ProviderModels,
    *,
    target: Path = DEFAULT_CONFIG_PATH,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    catalog_dir: Path | None = None,
) -> PreparedCodexSync:
    """Prepare only one provider profile and catalog as an immutable group."""
    return prepare_sync(
        target=target,
        prefixes=(provider_models.prefix,),
        fetcher=lambda prefix: (
            list(provider_models.models) if prefix == provider_models.prefix else []
        ),
        base_url=base_url,
        now=now,
        keep_backups=keep_backups,
        catalog_dir=catalog_dir,
        _provider_only=True,
    )


def apply_prepared(
    prepared: PreparedCodexSync,
    *,
    lock_token: HeldClientSyncLock,
) -> SyncResult:
    """Apply exact prepared Codex bytes under an active shared lock."""
    validate_client_sync_lock(lock_token)
    apply_prepared_group(prepared.group)
    return prepared.applied_result


def _sync_unlocked(
    target: Path = DEFAULT_CONFIG_PATH,
    *,
    prefixes: t.Iterable[str] | None = None,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    catalog_dir: Path | None = None,
    dry_run: bool = False,
    _provider_only: bool = False,
) -> SyncResult:
    """Synchronize ``target`` against live gateway models.

    Writes one ``reverso-<prefix>.config.toml`` profile file per gateway
    prefix with live models and one per-provider catalog JSON under ``catalog_dir``
    (default ``<target.parent>/reverso``). The default config exposes no
    reverso models; they are only selectable via
    ``codex -p reverso-<prefix>``. Any legacy global catalog, NUX, or profiles
    managed block is stripped.

    The function is idempotent: a second call with the same fetcher output
    produces no diff and creates no backup.
    """
    fetch = fetcher if fetcher is not None else _default_fetcher(base_url)
    sync_prefixes = (
        tuple(prefixes)
        if prefixes is not None
        else model_exposure.reverso_routed_codex_profile_prefixes()
    )
    resolved_catalog_dir = (
        catalog_dir if catalog_dir is not None else _default_catalog_dir(target)
    )
    provider_models = fetch_all(
        sync_prefixes,
        fetch,
        skip_errors=fetcher is None,
    )
    required_prefixes = tuple(
        prefix for prefix in sync_prefixes if prefix not in OPTIONAL_DISCOVERY_PREFIXES
    )
    if not provider_models and required_prefixes:
        raise ProviderFreshnessError(
            "no reverso provider model listings were available"
        )
    if "kimi" in sync_prefixes:
        kimi_models = next(
            (entry.models for entry in provider_models if entry.prefix == "kimi"),
            (),
        )
        if kimi_models != ("kimi-k3",):
            raise KimiDiscoveryError(
                "live Kimi model discovery must contain only kimi-k3"
            )
    discovered_prefixes = {entry.prefix for entry in provider_models}
    missing_prefixes = [
        prefix for prefix in required_prefixes if prefix not in discovered_prefixes
    ]
    if missing_prefixes:
        raise ProviderFreshnessError(
            "required reverso provider model discovery failed for: "
            + ", ".join(missing_prefixes)
        )
    empty_prefixes = [
        entry.prefix
        for entry in provider_models
        if entry.prefix in required_prefixes and not entry.models
    ]
    if empty_prefixes:
        raise ProviderFreshnessError(
            "required reverso provider model discovery returned no compatible models for: "
            + ", ".join(empty_prefixes)
        )
    catalog_dir = resolved_catalog_dir

    _reject_symlink(target, "config target")
    old_text = target.read_text(encoding="utf-8") if target.exists() else ""

    profile_files = (
        _reverso_profile_files(provider_models, target.parent, catalog_dir)
        if _provider_only
        else _profile_files(provider_models, target.parent, catalog_dir)
    )
    live_prefixes = {entry.prefix for entry in _live_provider_models(provider_models)}

    new_text = old_text
    if not _provider_only:
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
    for entry in _live_provider_models(provider_models):
        spec = model_exposure.reverso_codex_profile_spec(entry.prefix, entry.models)
        if spec.uses_model_catalog:
            json.loads(_generate_catalog_json(entry))
    if _provider_only:
        for path in (catalog_dir, *profile_files):
            _reject_symlink(path, "sync target")
    else:
        _validate_sync_paths(
            target=target,
            catalog_dir=catalog_dir,
            profile_files=profile_files,
            live_prefixes=live_prefixes,
        )
    _validate_canonical_reverso_profile_targets(profile_files)
    _validate_catalog_mutations(
        provider_models,
        config_dir=target.parent,
        catalog_dir=catalog_dir,
    )
    if new_text != old_text:
        _parse_toml(new_text, "rendered config")

    catalogs, catalogs_changed = _write_per_provider_catalogs(
        provider_models,
        catalog_dir,
        dry_run=dry_run,
    )
    (
        profiles,
        profile_backups,
        profile_rotated,
        profiles_changed,
    ) = _write_profile_files(
        profile_files,
        now=now,
        keep_backups=keep_backups,
        dry_run=dry_run,
    )
    archived_profiles: list[Path] = []
    if not _provider_only:
        archived_profiles.extend(
            _archive_legacy_managed_reverso_profiles(
                target.parent,
                live_prefixes,
                now=now,
                dry_run=dry_run,
            )
        )
        archived_profiles.extend(
            _archive_stale_variant_profiles(
                target.parent,
                now=now,
                dry_run=dry_run,
            )
        )
        archived_profiles.extend(
            _archive_stale_managed_reverso_profiles(
                target.parent,
                catalog_dir,
                set(sync_prefixes),
                now=now,
                dry_run=dry_run,
            )
        )

    config_changed = new_text != old_text
    changed = (
        config_changed
        or profiles_changed
        or catalogs_changed
        or bool(archived_profiles)
    )
    backup = None
    rotated: list[Path] = []
    if config_changed and not dry_run:
        backup = _make_backup(target, now=now)
        _atomic_write(target, new_text)
        rotated = _rotate_backups(target, keep=keep_backups)
    return SyncResult(
        target=target,
        changed=changed,
        backup=backup,
        rotated=rotated + profile_rotated,
        provider_models=provider_models,
        catalog_dir=catalog_dir,
        catalogs=catalogs,
        profiles=profiles,
        profile_backups=profile_backups,
        archived_profiles=archived_profiles,
    )


def sync(
    target: Path = DEFAULT_CONFIG_PATH,
    *,
    prefixes: t.Iterable[str] | None = None,
    fetcher: ModelFetcher | None = None,
    base_url: str = GATEWAY_BASE_URL,
    now: datetime.datetime | None = None,
    keep_backups: int = BACKUPS_KEPT,
    catalog_dir: Path | None = None,
    dry_run: bool = False,
    lock_path: Path | None = None,
    lock_token: HeldClientSyncLock | None = None,
) -> SyncResult:
    """Synchronize Codex client files under the shared writer lock."""
    kwargs = {
        "target": target,
        "prefixes": prefixes,
        "fetcher": fetcher,
        "base_url": base_url,
        "now": now,
        "keep_backups": keep_backups,
        "catalog_dir": catalog_dir,
    }
    if dry_run:
        return prepare_sync(**kwargs).result
    with acquire_client_sync_lock(path=lock_path, token=lock_token) as held:
        return apply_prepared(prepare_sync(**kwargs), lock_token=held)


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
        try:
            result = sync(
                target=target,
                base_url=base_url,
                catalog_dir=catalog_dir,
                dry_run=True,
            )
        except httpx.HTTPError as exc:
            sys.stderr.write(f"reverso-codex-sync: gateway error: {exc}\n")
            return 2
        except RuntimeError as exc:
            sys.stderr.write(f"reverso-codex-sync: {exc}\n")
            return 3
        report = {
            "target": str(result.target),
            "changed": result.changed,
            "catalog_dir": str(result.catalog_dir),
            "catalogs": [str(p) for p in result.catalogs],
            "profiles": [str(p) for p in result.profiles],
            "archived_profiles": [str(p) for p in result.archived_profiles],
            "providers": {pm.prefix: list(pm.models) for pm in result.provider_models},
        }
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0

    try:
        result = sync(target=target, base_url=base_url, catalog_dir=catalog_dir)
    except ClientSyncLockBusy as exc:
        sys.stderr.write(f"reverso-codex-sync: lock_busy: {exc}\n")
        return 2
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
