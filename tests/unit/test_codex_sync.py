"""Unit tests for ``reverso.codex_sync`` (B5).

No live network. The fetcher is always injected so calls never hit the
gateway. The target ``config.toml`` path is always under ``tmp_path`` so
``~/.codex/config.toml`` is never touched, and per-provider catalog files
are always written under ``tmp_path`` too.
"""

from __future__ import annotations

import datetime
import json
import tomllib
from pathlib import Path

import pytest

from reverso import codex_sync
from reverso.protocols import model_exposure


def _fixture_payload() -> dict[str, list[str]]:
    """Stable fixture model id payload, frozen here so changes are deliberate."""
    return {
        "claude": ["claude-fable-5", "claude-sonnet-4-6"],
        "copilot": ["claude-fable-5", "gpt-4o", "gpt-5.5", "claude-opus-4.8"],
        "auggie": ["prism-a"],
        "deepseek": ["deepseek-v3", "deepseek-r1"],
    }


def _make_fetcher(
    payload: dict[str, list[str]] | None = None,
) -> codex_sync.ModelFetcher:
    payload = payload if payload is not None else _fixture_payload()

    def _fetch(prefix: str) -> list[str]:
        if prefix not in payload:
            return []
        return list(payload[prefix])

    return _fetch


def _baseline_config_text() -> str:
    """Realistic existing config.toml with hand-managed unrelated content."""
    return (
        "# user comment header that must survive\n"
        'model_reasoning_effort = "medium"\n'
        "\n"
        "[model_providers.minimax]\n"
        'name = "MiniMax"\n'
        'base_url = "https://api.minimax.io/v1"\n'
        'env_key = "MINIMAX_ANTHROPIC_API_KEY"\n'
        'wire_api = "responses"\n'
        "\n"
        "# BEGIN REVERSO GATEWAY PROFILES (127.0.0.1:64946)\n"
        "[model_providers.reverso_claude]\n"
        'name = "Reverso Claude profile"\n'
        'base_url = "http://127.0.0.1:64946/claude/v1"\n'
        'wire_api = "responses"\n'
        "[model_providers.reverso_copilot]\n"
        'name = "Reverso Copilot profile"\n'
        'base_url = "http://127.0.0.1:64946/copilot/v1"\n'
        'wire_api = "responses"\n'
        "[model_providers.reverso_auggie]\n"
        'name = "Reverso Auggie profile"\n'
        'base_url = "http://127.0.0.1:64946/auggie/v1"\n'
        'wire_api = "responses"\n'
        "[model_providers.reverso_deepseek]\n"
        'name = "Reverso DeepSeek profile"\n'
        'base_url = "http://127.0.0.1:64946/deepseek/v1"\n'
        'wire_api = "responses"\n'
        "# END REVERSO GATEWAY PROFILES\n"
        "\n"
        "[tui]\n"
        'status_line = ["model-with-reasoning", "git-branch"]\n'
        "\n"
        '[projects."/Users/example/repo"]\n'
        'trust_level = "trusted"\n'
    )


def _prior_clutter_config_text() -> str:
    """Config carrying every legacy managed block + orphan overlay tables.

    Models the real ~/.codex/config.toml after the old global-exposure sync
    ran: a top-level managed catalog block, a managed NUX block, and per-model
    ``reverso_*__*`` overlay tables inside the managed PROFILES block. A new
    sync must strip all three.
    """
    return (
        "# user comment header that must survive\n"
        'model_reasoning_effort = "medium"\n'
        "\n"
        + codex_sync.CATALOG_BEGIN
        + "\n"
        + 'model_catalog_json = "/old/reverso-model-catalog.json"\n'
        + codex_sync.CATALOG_END
        + "\n"
        "\n"
        "[model_providers.minimax]\n"
        'name = "MiniMax"\n'
        'base_url = "https://api.minimax.io/v1"\n'
        'wire_api = "responses"\n'
        "\n"
        "[model_providers.reverso_copilot]\n"
        'name = "Reverso Copilot profile"\n'
        'base_url = "http://127.0.0.1:64946/copilot/v1"\n'
        'wire_api = "responses"\n'
        "\n" + codex_sync.PROFILES_BEGIN + "\n"
        "[model_providers.reverso_copilot__gpt-5_5]\n"
        'name = "Reverso copilot gpt-5.5"\n'
        'base_url = "http://127.0.0.1:64946/copilot/v1"\n'
        'wire_api = "responses"\n'
        'model = "gpt-5.5"\n'
        "[model_providers.reverso_claude__claude-fable-5]\n"
        'name = "Reverso claude claude-fable-5"\n'
        'base_url = "http://127.0.0.1:64946/claude/v1"\n'
        'wire_api = "responses"\n'
        'model = "claude-fable-5"\n' + codex_sync.PROFILES_END + "\n"
        "\n"
        "[tui]\n"
        'status_line = ["model-with-reasoning", "git-branch"]\n'
        "\n" + codex_sync.NUX_BEGIN + "\n"
        "[tui.model_availability_nux]\n"
        '"gpt-5.5" = 4\n'
        '"claude-fable-5" = 4\n' + codex_sync.NUX_END + "\n"
        "\n"
        '[projects."/Users/example/repo"]\n'
        'trust_level = "trusted"\n'
    )


def test_fetch_all_keeps_only_upstream_accepted_copilot_responses_models() -> None:
    payload = {
        "copilot": [
            "claude-fable-5",
            "gpt-4o",
            "gpt-5.5",
            "claude-opus-4.8",
            "claude-opus-4.7",
            "claude-sonnet-4.6",
            "gemini-2.5-pro",
            "gpt-5.5\nmodel:claude-fable-5",
            "gpt５.５",
            "gpt-5.4-mini",
            "gpt-5-mini",
        ]
    }

    result = codex_sync.fetch_all(["copilot"], _make_fetcher(payload))

    assert result == [
        codex_sync.ProviderModels(
            "copilot",
            (
                "gpt-4o",
                "gpt-5.5",
                "gpt-5.4-mini",
                "gpt-5-mini",
            ),
        )
    ]


def test_fetch_all_can_skip_unavailable_provider() -> None:
    def _fetch(prefix: str) -> list[str]:
        if prefix == "copilot":
            raise RuntimeError("copilot unavailable")
        return [f"{prefix}-model"]

    result = codex_sync.fetch_all(
        ["claude", "copilot", "deepseek"],
        _fetch,
        skip_errors=True,
    )

    assert result == [
        codex_sync.ProviderModels("claude", ("claude-model",)),
        codex_sync.ProviderModels("deepseek", ("deepseek-model",)),
    ]


def test_sync_fails_closed_when_all_default_provider_fetches_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    baseline = _baseline_config_text()
    target.write_text(baseline, encoding="utf-8")

    def _default_fetcher(_base_url: str) -> codex_sync.ModelFetcher:
        def _fetch(_prefix: str) -> list[str]:
            raise RuntimeError("gateway unavailable")

        return _fetch

    monkeypatch.setattr(codex_sync, "_default_fetcher", _default_fetcher)

    with pytest.raises(RuntimeError, match="no reverso provider model listings"):
        codex_sync.sync(target=target)

    assert target.read_text(encoding="utf-8") == baseline


def test_default_model_for_prefers_deepseek_v4_pro() -> None:
    assert (
        model_exposure.codex_profile_default_model(
            "deepseek", ("deepseek-v3", "deepseek-v4-pro")
        )
        == "deepseek-v4-pro"
    )
    # Without the preferred id, first listed wins.
    assert (
        model_exposure.codex_profile_default_model(
            "deepseek", ("deepseek-v3", "deepseek-r1")
        )
        == "deepseek-v3"
    )
    # Non-deepseek providers always use the first model.
    assert (
        model_exposure.codex_profile_default_model("copilot", ("gpt-4o", "gpt-5.5"))
        == "gpt-4o"
    )


def test_profile_files_emit_one_file_per_live_prefix(
    tmp_path: Path,
) -> None:
    pm = [
        codex_sync.ProviderModels("claude", ("claude-fable-5",)),
        codex_sync.ProviderModels("copilot", ("gpt-5.5", "gpt-4o")),
        codex_sync.ProviderModels("auggie", ("prism-a",)),
        codex_sync.ProviderModels("deepseek", ("deepseek-v3", "deepseek-v4-pro")),
    ]
    catalog_dir = tmp_path / "reverso"
    files = codex_sync._reverso_profile_files(pm, tmp_path, catalog_dir)

    assert [path.name for path in files] == [
        "claude.config.toml",
        "copilot.config.toml",
        "auggie.config.toml",
        "deepseek.config.toml",
    ]
    parsed = {
        path.stem.removesuffix(".config"): tomllib.loads(text)
        for path, text in files.items()
    }
    assert parsed["claude"]["model_provider"] == "reverso_claude"
    assert parsed["copilot"]["model_provider"] == "reverso_copilot"
    assert parsed["deepseek"]["model_provider"] == "reverso_deepseek"
    assert parsed["claude"]["model"] == "claude-fable-5"
    assert parsed["copilot"]["model"] == "gpt-5.5"
    assert parsed["deepseek"]["model"] == "deepseek-v4-pro"
    assert parsed["copilot"]["model_catalog_json"] == str(catalog_dir / "copilot.json")


def test_reverso_profile_files_skip_prefixes_without_models(
    tmp_path: Path,
) -> None:
    pm = [
        codex_sync.ProviderModels("claude", ()),
        codex_sync.ProviderModels("copilot", ("gpt-5.5",)),
        codex_sync.ProviderModels("auggie", ()),
        codex_sync.ProviderModels("deepseek", ()),
    ]
    files = codex_sync._reverso_profile_files(pm, tmp_path, tmp_path)
    assert {path.name for path in files} == {"copilot.config.toml"}


def test_sync_writes_profiles_for_each_live_prefix(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog_dir = tmp_path / "reverso"

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=catalog_dir
    )

    assert result.changed is True
    for prefix in ("claude", "copilot", "auggie", "deepseek"):
        profile = tomllib.loads((tmp_path / f"{prefix}.config.toml").read_text())
        assert profile["model_provider"] == f"reverso_{prefix}"
        assert profile["model_catalog_json"] == str(catalog_dir / f"{prefix}.json")
        assert profile["model"]
    openai = tomllib.loads((tmp_path / "openai.config.toml").read_text())
    minimax = tomllib.loads((tmp_path / "minimax.config.toml").read_text())
    assert openai == {"model": "gpt-5.5", "model_provider": "openai"}
    assert minimax["model_provider"] == "minimax"
    assert minimax["model"] == "MiniMax-M3"


def test_sync_uses_model_exposure_profile_prefix_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    seen: list[str] = []

    monkeypatch.setattr(
        codex_sync.model_exposure,
        "reverso_routed_codex_profile_prefixes",
        lambda: ("copilot",),
    )

    def fetch(prefix: str) -> list[str]:
        seen.append(prefix)
        return ["gpt-5.5"]

    result = codex_sync.sync(target=target, fetcher=fetch, catalog_dir=tmp_path / "rev")

    assert seen == ["copilot"]
    assert {path.name for path in result.profiles} == {
        "copilot.config.toml",
        "openai.config.toml",
        "minimax.config.toml",
    }
    assert not (tmp_path / "claude.config.toml").exists()


def test_sync_honors_model_exposure_catalog_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    def without_catalog(
        prefix: str, models: tuple[str, ...]
    ) -> model_exposure.CodexProfileSpec:
        return model_exposure.CodexProfileSpec(
            prefix=prefix,
            model=models[0],
            model_provider=f"reverso_{prefix}",
            uses_model_catalog=False,
        )

    monkeypatch.setattr(
        codex_sync.model_exposure,
        "reverso_codex_profile_spec",
        without_catalog,
    )

    result = codex_sync.sync(
        target=target,
        fetcher=_make_fetcher({"copilot": ["gpt-5.5"]}),
        prefixes=("copilot",),
        catalog_dir=tmp_path / "rev",
    )

    profile = tomllib.loads((tmp_path / "copilot.config.toml").read_text())
    assert "model_catalog_json" not in profile
    assert result.catalogs == []
    assert not (tmp_path / "rev" / "copilot.json").exists()


def test_sync_archives_only_known_generated_variant_profiles(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    stale = [
        tmp_path / "deepseek-gpt54.config.toml",
        tmp_path / "deepseek-mini.config.toml",
        tmp_path / "deepseek-spark.config.toml",
        tmp_path / "minimax-gpt54.config.toml",
        tmp_path / "minimax-mini.config.toml",
        tmp_path / "minimax-spark.config.toml",
    ]
    for path in stale:
        path.write_text('model = "old"\n', encoding="utf-8")
    user_owned = tmp_path / "deepseek-custom.config.toml"
    user_owned.write_text('model = "keep-me"\n', encoding="utf-8")

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    assert {
        path.name.split(codex_sync.BACKUP_SUFFIX_PREFIX)[0]
        for path in result.archived_profiles
    } == {path.name for path in stale}
    assert all(not path.exists() for path in stale)
    assert user_owned.exists()
    archive_dir = tmp_path / codex_sync.PROFILE_ARCHIVE_DIR
    assert archive_dir.is_dir()


def test_sync_preserves_unmarked_direct_provider_profiles(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    openai_profile = tmp_path / "openai.config.toml"
    minimax_profile = tmp_path / "minimax.config.toml"
    openai_text = 'model = "custom-openai"\nmodel_provider = "openai"\napproval_policy = "never"\n'
    minimax_text = 'model = "custom-minimax"\nmodel_provider = "minimax"\nmodel_context_window = 123456\n'
    openai_profile.write_text(openai_text, encoding="utf-8")
    minimax_profile.write_text(minimax_text, encoding="utf-8")

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    assert result.changed is True
    assert openai_profile.read_text(encoding="utf-8") == openai_text
    assert minimax_profile.read_text(encoding="utf-8") == minimax_text
    assert openai_profile not in result.profiles
    assert minimax_profile not in result.profiles
    assert not any(
        path.name.startswith("openai.config.toml") for path in result.profile_backups
    )
    assert not any(
        path.name.startswith("minimax.config.toml") for path in result.profile_backups
    )


def test_sync_updates_managed_direct_provider_profiles(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    openai_profile = tmp_path / "openai.config.toml"
    minimax_profile = tmp_path / "minimax.config.toml"
    openai_profile.write_text(
        codex_sync.PROFILE_MANAGED_MARKER
        + '\nmodel = "old"\nmodel_provider = "openai"\n',
        encoding="utf-8",
    )
    minimax_profile.write_text(
        codex_sync.PROFILE_MANAGED_MARKER
        + '\nmodel = "old"\nmodel_provider = "minimax"\n',
        encoding="utf-8",
    )

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    openai = tomllib.loads(openai_profile.read_text(encoding="utf-8"))
    minimax = tomllib.loads(minimax_profile.read_text(encoding="utf-8"))
    assert openai == {"model": "gpt-5.5", "model_provider": "openai"}
    assert minimax["model"] == "MiniMax-M3"
    assert minimax["model_provider"] == "minimax"
    assert result.profile_backups


def test_sync_archives_stale_managed_reverso_profile_and_catalog(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog_dir = tmp_path / "reverso"
    catalog_dir.mkdir()
    stale_profile = tmp_path / "claude.config.toml"
    stale_catalog = catalog_dir / "claude.json"
    stale_profile.write_text(
        codex_sync._render_profile_file(
            model="claude-old",
            model_provider="reverso_claude",
            catalog_path=stale_catalog,
        ),
        encoding="utf-8",
    )
    stale_catalog.write_text('{"models": []}\n', encoding="utf-8")
    user_profile = tmp_path / "auggie.config.toml"
    user_profile.write_text(
        'model = "custom-auggie"\nmodel_provider = "reverso_auggie"\n',
        encoding="utf-8",
    )

    result = codex_sync.sync(
        target=target,
        fetcher=_make_fetcher(
            {"copilot": ["gpt-5.5"], "deepseek": ["deepseek-v4-pro"]}
        ),
        catalog_dir=catalog_dir,
    )

    assert not stale_profile.exists()
    assert not stale_catalog.exists()
    assert user_profile.exists()
    archived_names = {
        path.name.split(codex_sync.BACKUP_SUFFIX_PREFIX)[0]
        for path in result.archived_profiles
    }
    assert "claude.config.toml" in archived_names
    assert "claude.json" in archived_names
    assert "auggie.config.toml" not in archived_names


def test_sync_default_config_exposes_no_reverso_models_globally(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    text = target.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    # No global NUX block, no top-level managed catalog pointer.
    assert codex_sync.NUX_BEGIN not in text
    assert codex_sync.CATALOG_BEGIN not in text
    assert "[tui.model_availability_nux]" not in text
    assert "model_catalog_json" not in text
    assert "[profiles." not in text
    assert codex_sync.PROFILES_BEGIN not in text
    # The default codex model stays plain.
    assert parsed["model"] == "gpt-5.5"


def test_sync_writes_per_provider_catalog_files_with_profile_safe_slugs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog_dir = tmp_path / "reverso"

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=catalog_dir
    )

    assert result.catalog_dir == catalog_dir
    written = {p.name for p in result.catalogs}
    assert written == {"claude.json", "copilot.json", "auggie.json", "deepseek.json"}

    copilot = json.loads((catalog_dir / "copilot.json").read_text(encoding="utf-8"))
    copilot_slugs = [m["slug"] for m in copilot["models"]]
    # Collision-prone providers are prefixed so they cannot shadow built-in GPT
    # model ids in Codex's picker. The profile's default model remains bare.
    assert "copilot/gpt-5.5" in copilot_slugs
    assert "copilot/gpt-4o" in copilot_slugs
    assert "gpt-5.5" not in copilot_slugs

    claude = json.loads((catalog_dir / "claude.json").read_text(encoding="utf-8"))
    claude_slugs = {m["slug"] for m in claude["models"]}
    # Each provider catalog only carries its own models.
    assert claude_slugs == {"claude-fable-5", "claude-sonnet-4-6"}
    assert "gpt-5.5" not in claude_slugs

    auggie = json.loads((catalog_dir / "auggie.json").read_text(encoding="utf-8"))
    assert [m["slug"] for m in auggie["models"]] == ["auggie/prism-a"]

    deepseek = json.loads((catalog_dir / "deepseek.json").read_text(encoding="utf-8"))
    assert [m["slug"] for m in deepseek["models"]] == [
        "deepseek-v3",
        "deepseek-r1",
    ]


def test_sync_strips_legacy_clutter_blocks(tmp_path: Path) -> None:
    """Regression: prior global catalog + NUX + overlay tables all removed."""
    target = tmp_path / "config.toml"
    prior = _prior_clutter_config_text()
    target.write_text(prior, encoding="utf-8")

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    text = target.read_text(encoding="utf-8")
    # Legacy managed catalog block gone.
    assert codex_sync.CATALOG_BEGIN not in text
    assert "/old/reverso-model-catalog.json" not in text
    # Legacy NUX block gone.
    assert codex_sync.NUX_BEGIN not in text
    assert "[tui.model_availability_nux]" not in text
    # Legacy per-model overlay tables gone.
    assert "[model_providers.reverso_copilot__gpt-5_5]" not in text
    assert "[model_providers.reverso_claude__claude-fable-5]" not in text
    parsed = tomllib.loads(text)
    assert "[profiles." not in text
    profile = tomllib.loads((tmp_path / "copilot.config.toml").read_text())
    assert profile["model_provider"] == "reverso_copilot"
    # Hand-managed base provider table preserved.
    assert "[model_providers.reverso_copilot]" in text
    assert parsed["model_providers"]["reverso_copilot"]["base_url"].endswith(
        "/copilot/v1"
    )
    # Unrelated user content preserved.
    assert "# user comment header that must survive" in text
    assert parsed["projects"]["/Users/example/repo"]["trust_level"] == "trusted"


def test_sync_inserts_default_model_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    text = target.read_text(encoding="utf-8")
    assert text.index('model = "gpt-5.5"') < text.index("[model_providers.minimax]")
    assert tomllib.loads(text)["model"] == "gpt-5.5"


def test_sync_preserves_user_selected_model(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        'model = "custom-user-model"\n' + _baseline_config_text(),
        encoding="utf-8",
    )

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    text = target.read_text(encoding="utf-8")
    top_level = text[: text.index("[model_providers.minimax]")]
    assert tomllib.loads(text)["model"] == "custom-user-model"
    assert top_level.count('model = "custom-user-model"') == 1
    assert 'model = "gpt-5.5"' not in top_level


def test_sync_inserts_missing_reverso_provider_tables(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        'model_reasoning_effort = "medium"\n\n[tui]\nstatus_line = ["model"]\n',
        encoding="utf-8",
    )
    catalog_dir = tmp_path / "reverso"

    first = codex_sync.sync(
        target=target,
        fetcher=_make_fetcher(),
        catalog_dir=catalog_dir,
    )

    assert first.changed is True
    text = target.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    providers = parsed["model_providers"]
    for prefix in ("claude", "copilot", "auggie", "deepseek"):
        provider = providers[f"reverso_{prefix}"]
        assert provider["base_url"] == f"http://127.0.0.1:64946/{prefix}/v1"
        assert provider["wire_api"] == "responses"
    assert codex_sync.GATEWAY_PROVIDERS_BEGIN in text
    assert '[tui]\nstatus_line = ["model"]' in text

    second = codex_sync.sync(
        target=target,
        fetcher=_make_fetcher(),
        catalog_dir=catalog_dir,
    )
    assert second.changed is False


def test_sync_strips_legacy_block_and_creates_config_backup(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    fetcher = _make_fetcher()

    result = codex_sync.sync(
        target=target, fetcher=fetcher, catalog_dir=tmp_path / "reverso"
    )

    assert result.changed is True
    assert result.backup is not None
    assert result.backup.exists()
    new_text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN not in new_text
    assert codex_sync.PROFILES_END not in new_text
    assert "[profiles." not in new_text
    assert (tmp_path / "claude.config.toml").exists()
    assert (tmp_path / "copilot.config.toml").exists()
    assert (tmp_path / "auggie.config.toml").exists()
    assert (tmp_path / "deepseek.config.toml").exists()


def test_sync_is_idempotent_no_diff_no_backup(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    fetcher = _make_fetcher()
    catalog_dir = tmp_path / "reverso"

    first = codex_sync.sync(target=target, fetcher=fetcher, catalog_dir=catalog_dir)
    assert first.changed is True
    text_after_first = target.read_text(encoding="utf-8")

    second = codex_sync.sync(target=target, fetcher=fetcher, catalog_dir=catalog_dir)
    assert second.changed is False
    assert second.backup is None
    assert second.rotated == []
    text_after_second = target.read_text(encoding="utf-8")
    assert text_after_first == text_after_second


def test_sync_preserves_unrelated_keys_byte_for_byte(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    baseline = _baseline_config_text()
    target.write_text(baseline, encoding="utf-8")
    fetcher = _make_fetcher()

    codex_sync.sync(target=target, fetcher=fetcher, catalog_dir=tmp_path / "reverso")
    new_text = target.read_text(encoding="utf-8")

    untouched_lines = [
        "# user comment header that must survive",
        'model_reasoning_effort = "medium"',
        "[model_providers.minimax]",
        'name = "MiniMax"',
        'env_key = "MINIMAX_ANTHROPIC_API_KEY"',
        "# BEGIN REVERSO GATEWAY PROFILES (127.0.0.1:64946)",
        "[model_providers.reverso_claude]",
        "# END REVERSO GATEWAY PROFILES",
        "[tui]",
        'status_line = ["model-with-reasoning", "git-branch"]',
        '[projects."/Users/example/repo"]',
        'trust_level = "trusted"',
    ]
    for line in untouched_lines:
        assert line in new_text, f"unrelated content disturbed: {line!r}"


def test_sync_keeps_only_five_newest_backups(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    payloads: list[dict[str, list[str]]] = [
        {"claude": [f"claude-rev-{i}"], "copilot": [], "auggie": [], "deepseek": []}
        for i in range(7)
    ]

    base_ts = datetime.datetime(2026, 6, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
    for i, payload in enumerate(payloads):
        result = codex_sync.sync(
            target=target,
            fetcher=_make_fetcher(payload),
            now=base_ts + datetime.timedelta(minutes=i),
            catalog_dir=tmp_path / "reverso",
        )
        assert result.changed is True

    backups = sorted(
        p
        for p in target.parent.iterdir()
        if p.name.startswith("claude.config.toml" + codex_sync.BACKUP_SUFFIX_PREFIX)
    )
    assert len(backups) == codex_sync.BACKUPS_KEPT

    expected_minutes = list(range(2, 7))
    expected_stamps = [
        (base_ts + datetime.timedelta(minutes=m)).strftime("%Y%m%dT%H%M%SZ")
        for m in expected_minutes
    ]
    for stamp, backup in zip(expected_stamps, backups, strict=True):
        assert stamp in backup.name


def test_sync_no_existing_file_creates_target_no_backup(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "config.toml"
    fetcher = _make_fetcher()

    result = codex_sync.sync(
        target=target, fetcher=fetcher, catalog_dir=tmp_path / "reverso"
    )

    assert result.changed is True
    assert result.backup is None
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN not in text
    assert (tmp_path / "fresh" / "claude.config.toml").exists()


def test_sync_default_catalog_dir_is_config_parent_reverso(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    result = codex_sync.sync(target=target, fetcher=_make_fetcher())

    assert result.catalog_dir == tmp_path / "reverso"
    assert (tmp_path / "reverso" / "copilot.json").exists()


def test_sync_atomic_write_uses_temp_in_same_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    seen_dirs: list[str] = []
    real_mkstemp = codex_sync.tempfile.mkstemp

    def _spy_mkstemp(*args, **kwargs):
        seen_dirs.append(str(kwargs.get("dir")))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(codex_sync.tempfile, "mkstemp", _spy_mkstemp)

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "rev"
    )

    assert seen_dirs, "atomic write must mkstemp; none observed"
    assert str(target.parent) in seen_dirs


def test_sync_no_temp_files_left_behind(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "rev"
    )

    leftovers = [p for p in target.parent.rglob("*.tmp")]
    assert leftovers == []


def test_replace_managed_block_appends_when_absent() -> None:
    text = "existing = true\n"
    new_block = codex_sync.PROFILES_BEGIN + "\n[managed.x]\n" + codex_sync.PROFILES_END
    out = codex_sync._replace_managed_block(
        text, codex_sync.PROFILES_BEGIN, codex_sync.PROFILES_END, new_block
    )
    assert out.startswith("existing = true\n")
    assert codex_sync.PROFILES_BEGIN in out
    assert codex_sync.PROFILES_END in out


def test_replace_managed_block_replaces_when_present() -> None:
    text = (
        "head = 1\n"
        + codex_sync.PROFILES_BEGIN
        + "\nold = true\n"
        + codex_sync.PROFILES_END
        + "\ntail = 2\n"
    )
    new_block = codex_sync.PROFILES_BEGIN + "\nnew = true\n" + codex_sync.PROFILES_END
    out = codex_sync._replace_managed_block(
        text, codex_sync.PROFILES_BEGIN, codex_sync.PROFILES_END, new_block
    )
    assert "head = 1\n" in out
    assert "tail = 2\n" in out
    assert "old = true" not in out
    assert "new = true" in out


def test_replace_managed_block_raises_on_unclosed_sentinel() -> None:
    text = codex_sync.PROFILES_BEGIN + "\nopen forever\n"
    with pytest.raises(RuntimeError):
        codex_sync._replace_managed_block(
            text,
            codex_sync.PROFILES_BEGIN,
            codex_sync.PROFILES_END,
            codex_sync.PROFILES_BEGIN + "\n" + codex_sync.PROFILES_END,
        )


def test_extract_model_ids_handles_malformed_payloads() -> None:
    assert codex_sync._extract_model_ids({"data": []}) == []
    assert codex_sync._extract_model_ids({"data": [{"id": "ok"}]}) == ["ok"]
    assert codex_sync._extract_model_ids({"data": [{"id": ""}, {"id": "x"}]}) == ["x"]
    assert codex_sync._extract_model_ids({"data": [{}]}) == []
    assert codex_sync._extract_model_ids({"data": "not a list"}) == []
    assert codex_sync._extract_model_ids("nope") == []


def test_fetch_all_dedupes_model_ids_per_prefix() -> None:
    def _dup_fetcher(prefix: str) -> list[str]:
        return ["a", "b", "a", "c", "b"]

    pms = codex_sync.fetch_all(("claude",), _dup_fetcher)
    assert pms == [codex_sync.ProviderModels("claude", ("a", "b", "c"))]


def test_main_dry_run_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.toml"
    baseline = _baseline_config_text()
    target.write_text(baseline, encoding="utf-8")

    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setenv("REVERSO_CODEX_CATALOG_DIR", str(tmp_path / "reverso"))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main(["--dry-run"])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == baseline
    assert not (tmp_path / "reverso").exists()
    out = capsys.readouterr().out
    assert "claude-fable-5" in out


def test_main_writes_when_not_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setenv("REVERSO_CODEX_CATALOG_DIR", str(tmp_path / "reverso"))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main([])
    assert rc == 0
    new_text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN not in new_text
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] is True
    assert report["catalog_dir"] == str(tmp_path / "reverso")
    assert sorted(Path(p).name for p in report["catalogs"]) == [
        "auggie.json",
        "claude.json",
        "copilot.json",
        "deepseek.json",
    ]
    assert sorted(Path(p).name for p in report["profiles"]) == [
        "auggie.config.toml",
        "claude.config.toml",
        "copilot.config.toml",
        "deepseek.config.toml",
        "minimax.config.toml",
        "openai.config.toml",
    ]


def test_no_secret_material_written_anywhere(tmp_path: Path) -> None:
    """Backup, target, and rendered blocks must never contain secret tokens."""
    target = tmp_path / "config.toml"

    sensitive = "sk-test-secret-do-not-leak-XYZ"
    baseline = (
        f'# api_key (must not be picked up): "{sensitive}"\n' + _baseline_config_text()
    )
    target.write_text(baseline, encoding="utf-8")

    fetcher = _make_fetcher()
    codex_sync.sync(target=target, fetcher=fetcher, catalog_dir=tmp_path / "reverso")

    new_text = target.read_text(encoding="utf-8")
    assert sensitive in new_text, "byte-faithful preservation must keep the user line"

    backups = [
        p
        for p in target.parent.iterdir()
        if p.name.startswith(target.name + codex_sync.BACKUP_SUFFIX_PREFIX)
    ]
    for backup in backups:
        original_baseline_had_secret = sensitive in backup.read_text(encoding="utf-8")
        assert original_baseline_had_secret, (
            "backup must be a faithful copy of pre-write target (including the "
            "user-owned line); sync itself never adds new secret content"
        )

    pm = codex_sync.fetch_all(
        model_exposure.reverso_routed_codex_profile_prefixes(), fetcher
    )
    profiles = codex_sync._profile_files(pm, Path("/codex"), Path("/codex/reverso"))
    profile_text = "\n".join(profiles.values())
    assert "api_key" not in profile_text
    assert "env_key" not in profile_text
    assert "secret" not in profile_text.lower()
    assert sensitive not in profile_text


def test_resolve_helpers_prefer_explicit_then_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("REVERSO_CODEX_CONFIG", raising=False)
    monkeypatch.delenv("REVERSO_CODEX_BASE_URL", raising=False)
    assert codex_sync._resolve_config_path(None) == codex_sync.DEFAULT_CONFIG_PATH
    assert codex_sync._resolve_base_url(None) == codex_sync.GATEWAY_BASE_URL

    explicit = tmp_path / "x.toml"
    assert codex_sync._resolve_config_path(explicit) == explicit
    assert (
        codex_sync._resolve_base_url("http://example.invalid")
        == "http://example.invalid"
    )

    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(tmp_path / "env.toml"))
    monkeypatch.setenv("REVERSO_CODEX_BASE_URL", "http://env.invalid")
    assert codex_sync._resolve_config_path(None) == tmp_path / "env.toml"
    assert codex_sync._resolve_base_url(None) == "http://env.invalid"


def test_resolve_catalog_dir_prefers_explicit_then_env_then_config_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "sub" / "config.toml"
    monkeypatch.delenv("REVERSO_CODEX_CATALOG_DIR", raising=False)
    assert codex_sync._resolve_catalog_dir(None, config) == config.parent / "reverso"

    monkeypatch.setenv("REVERSO_CODEX_CATALOG_DIR", str(tmp_path / "env-cat"))
    assert codex_sync._resolve_catalog_dir(None, config) == tmp_path / "env-cat"

    explicit = tmp_path / "explicit-cat"
    assert codex_sync._resolve_catalog_dir(explicit, config) == explicit


def test_toml_table_key_replaces_invalid_characters() -> None:
    assert codex_sync._toml_table_key("gpt-5.5") == "gpt-5_5"
    assert codex_sync._toml_table_key("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert codex_sync._toml_table_key("a/b@c") == "a_b_c"
    assert codex_sync._toml_table_key("") == "model"


def test_sync_with_no_models_writes_direct_profiles_only(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    empty = _make_fetcher({"claude": [], "copilot": [], "auggie": [], "deepseek": []})

    result = codex_sync.sync(
        target=target, fetcher=empty, catalog_dir=tmp_path / "reverso"
    )
    text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN not in text
    assert "[profiles." not in text
    assert codex_sync.NUX_BEGIN not in text
    assert "[tui.model_availability_nux]" not in text
    assert result.catalogs == []
    assert {p.name for p in result.profiles} == {
        "openai.config.toml",
        "minimax.config.toml",
    }
    tomllib.loads(text)


def test_atomic_write_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "x.toml"
    payload = "alpha = 1\n"
    codex_sync._atomic_write(target, payload)
    assert target.read_text(encoding="utf-8") == payload


def test_atomic_write_unlinks_tmp_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "x.toml"

    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(codex_sync.os, "replace", _boom)

    with pytest.raises(OSError):
        codex_sync._atomic_write(target, "data\n")

    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_sync_refuses_to_write_when_user_toml_is_invalid(tmp_path: Path) -> None:
    """Fail closed: user-owned duplicate tables (invalid TOML) must abort the
    sync before any backup or write happens."""
    broken = _baseline_config_text() + "\n[tui]\nstatus_line = 1\n"
    target = tmp_path / "config.toml"
    target.write_text(broken, encoding="utf-8")

    with pytest.raises(RuntimeError):
        codex_sync.sync(
            target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
        )

    assert target.read_text(encoding="utf-8") == broken
    backups = [
        p
        for p in target.parent.iterdir()
        if p.name.startswith(target.name + codex_sync.BACKUP_SUFFIX_PREFIX)
    ]
    assert backups == []


def test_main_returns_3_on_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = _baseline_config_text() + "\n[tui]\nstatus_line = 1\n"
    target = tmp_path / "config.toml"
    target.write_text(broken, encoding="utf-8")
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setenv("REVERSO_CODEX_CATALOG_DIR", str(tmp_path / "reverso"))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main([])
    assert rc == 3
    assert target.read_text(encoding="utf-8") == broken
    err = capsys.readouterr().err
    assert "refusing to write" in err


def test_reverso_profile_files_dedupes_to_one_file_per_prefix(
    tmp_path: Path,
) -> None:
    pm = [codex_sync.ProviderModels("copilot", ("gpt-5.5", "gpt-4o"))]
    files = codex_sync._reverso_profile_files(pm, tmp_path, tmp_path)
    assert list(files) == [tmp_path / "copilot.config.toml"]


def test_sync_handles_crlf_config(tmp_path: Path) -> None:
    """Regression: CRLF-edited configs must still sync and stay idempotent."""
    crlf_baseline = _baseline_config_text().replace("\n", "\r\n")
    target = tmp_path / "config.toml"
    target.write_bytes(crlf_baseline.encode("utf-8"))
    catalog_dir = tmp_path / "reverso"

    first = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=catalog_dir
    )
    assert first.changed is True
    text = target.read_bytes().decode("utf-8")
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5.5"
    profile = tomllib.loads((tmp_path / "claude.config.toml").read_text())
    assert profile["model_provider"] == "reverso_claude"

    second = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=catalog_dir
    )
    assert second.changed is False


def test_renderers_escape_hostile_model_ids(tmp_path: Path) -> None:
    hostile = 'we"ird\\id'
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog_dir = tmp_path / "reverso"

    codex_sync.sync(
        target=target,
        fetcher=_make_fetcher({"claude": [hostile]}),
        catalog_dir=catalog_dir,
    )
    profile = tomllib.loads((tmp_path / "claude.config.toml").read_text())
    assert profile["model"] == hostile

    claude = json.loads((catalog_dir / "claude.json").read_text(encoding="utf-8"))
    assert claude["models"][0]["slug"] == hostile


def test_sentinel_mentioned_midline_in_comment_is_ignored(tmp_path: Path) -> None:
    baseline = (
        _baseline_config_text()
        + f"# note: the marker {codex_sync.PROFILES_BEGIN} is managed tooling\n"
    )
    target = tmp_path / "config.toml"
    target.write_text(baseline, encoding="utf-8")

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )
    assert result.changed is True
    text = target.read_text(encoding="utf-8")
    assert f"# note: the marker {codex_sync.PROFILES_BEGIN} is managed tooling" in text
    tomllib.loads(text)


def test_generate_catalog_json_shape_dedup_and_context_window() -> None:
    pm = codex_sync.ProviderModels("copilot", ("shared-model", "big-500k-model"))
    payload = json.loads(codex_sync._generate_catalog_json(pm))

    assert set(payload.keys()) == {"models"}
    slugs = [m["slug"] for m in payload["models"]]
    assert slugs == ["copilot/shared-model", "copilot/big-500k-model"]

    by_slug = {m["slug"]: m for m in payload["models"]}
    assert (
        by_slug["copilot/shared-model"]["display_name"]
        == "Reverso copilot shared-model"
    )
    assert by_slug["copilot/shared-model"]["context_window"] == 128000
    assert by_slug["copilot/big-500k-model"]["context_window"] == 500000
    required_keys = {
        "slug",
        "display_name",
        "description",
        "default_reasoning_level",
        "supported_reasoning_levels",
        "shell_type",
        "visibility",
        "context_window",
        "max_context_window",
        "supported_in_api",
        "priority",
        "base_instructions",
    }
    for model in payload["models"]:
        assert required_keys <= set(model.keys())
        assert model["supported_in_api"] is True
        assert model["shell_type"] == "shell_command"
        assert model["visibility"] == "list"
        assert model["default_reasoning_level"] == "medium"
        assert model["supported_reasoning_levels"]


def test_generate_catalog_json_dedupes_within_provider() -> None:
    pm = codex_sync.ProviderModels("copilot", ("gpt-5.5", "gpt-5.5", "gpt-4o"))
    payload = json.loads(codex_sync._generate_catalog_json(pm))
    slugs = [m["slug"] for m in payload["models"]]
    assert slugs == ["copilot/gpt-5.5", "copilot/gpt-4o"]


def test_generate_catalog_json_survives_hostile_model_ids() -> None:
    hostile = 'evil"\\\nmodel\t\x01id'
    pm = codex_sync.ProviderModels("claude", (hostile,))

    payload = json.loads(codex_sync._generate_catalog_json(pm))

    by_slug = {model["slug"]: model for model in payload["models"]}
    assert by_slug[hostile]["display_name"] == f"Claude (Claude Code) {hostile}"


def test_sync_unchanged_run_regenerates_deleted_catalogs(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog_dir = tmp_path / "reverso"
    fetcher = _make_fetcher()

    first = codex_sync.sync(target=target, fetcher=fetcher, catalog_dir=catalog_dir)
    assert first.changed is True
    copilot_catalog = catalog_dir / "copilot.json"
    assert copilot_catalog.exists()
    catalog_text = copilot_catalog.read_text(encoding="utf-8")

    copilot_catalog.unlink()
    second = codex_sync.sync(target=target, fetcher=fetcher, catalog_dir=catalog_dir)

    assert second.changed is False
    assert second.backup is None
    assert copilot_catalog.exists(), "unchanged config must still restore catalogs"
    assert copilot_catalog.read_text(encoding="utf-8") == catalog_text


def test_main_dry_run_reports_catalog_dir_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.toml"
    baseline = _baseline_config_text()
    target.write_text(baseline, encoding="utf-8")
    catalog_dir = tmp_path / "reverso"
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setenv("REVERSO_CODEX_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main(["--dry-run"])

    assert rc == 0
    assert not catalog_dir.exists()
    assert target.read_text(encoding="utf-8") == baseline
    report = json.loads(capsys.readouterr().out)
    assert report["catalog_dir"] == str(catalog_dir)


def test_sync_strips_legacy_orphan_profiles_block(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    legacy = (
        _baseline_config_text()
        + "\n[model_providers.reverso_copilot__gpt-5_5]\n"
        + 'name = "old"\n'
        + 'base_url = "http://127.0.0.1:64946/copilot/v1"\n'
        + 'wire_api = "responses"\n'
        + 'model = "gpt-5.5"\n'
        + codex_sync.PROFILES_END
        + "\n"
    )
    target.write_text(legacy, encoding="utf-8")

    codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_dir=tmp_path / "reverso"
    )

    text = target.read_text(encoding="utf-8")
    assert 'name = "old"' not in text
    assert "[model_providers.reverso_copilot__gpt-5_5]" not in text
    tomllib.loads(text)


def test_strip_orphan_preserves_interleaved_user_content() -> None:
    # Regression: a lost PROFILES_BEGIN with a surviving END must NOT delete a
    # user table sitting between an orphan overlay and the stray END line.
    text = (
        'model = "gpt-5.5"\n'
        "[model_providers.reverso_copilot__gpt-5_5]\n"
        'name = "old"\n'
        'model = "gpt-5.5"\n'
        "[my_important_user_table]\n"
        'precious = "data"\n' + codex_sync.PROFILES_END + "\n"
        "[after]\n"
        'k = "v"\n'
    )
    out = codex_sync._strip_managed_block(
        text, codex_sync.PROFILES_BEGIN, codex_sync.PROFILES_END
    )
    assert "[model_providers.reverso_copilot__gpt-5_5]" not in out
    assert 'name = "old"' not in out
    # User content on BOTH sides of the orphan is preserved byte-faithfully.
    assert "[my_important_user_table]" in out
    assert 'precious = "data"' in out
    assert "[after]" in out
    assert 'k = "v"' in out
    # The stray END comment is cleaned up.
    assert codex_sync.PROFILES_END not in out
    tomllib.loads(out)


def test_strip_orphan_overlay_after_stray_end_is_removed() -> None:
    # Mirror case: an orphan overlay table positioned AFTER a stray END line is
    # still stripped, while the user table between them is preserved.
    text = (
        'model = "gpt-5.5"\n' + codex_sync.PROFILES_END + "\n"
        "[user_keep]\n"
        'a = "b"\n'
        "[model_providers.reverso_auggie__opus]\n"
        'model = "opus"\n'
    )
    out = codex_sync._strip_managed_block(
        text, codex_sync.PROFILES_BEGIN, codex_sync.PROFILES_END
    )
    assert "[model_providers.reverso_auggie__opus]" not in out
    assert "[user_keep]" in out and 'a = "b"' in out
    assert codex_sync.PROFILES_END not in out
    tomllib.loads(out)


def test_merge_catalog_config_block_strips_legacy_block() -> None:
    base = (
        'model = "gpt-5.5"\n'
        + codex_sync.CATALOG_BEGIN
        + "\n"
        + 'model_catalog_json = "/old/catalog.json"\n'
        + codex_sync.CATALOG_END
        + "\n"
        + "[tui]\n"
    )
    removed = codex_sync._merge_catalog_config_block(base, None)
    assert codex_sync.CATALOG_BEGIN not in removed
    assert "model_catalog_json" not in removed
    assert 'model = "gpt-5.5"' in removed
    assert "[tui]" in removed
    tomllib.loads(removed)


def test_merge_catalog_config_block_rejects_a_path() -> None:
    with pytest.raises(ValueError):
        codex_sync._merge_catalog_config_block("", Path("/x.json"))
