"""Unit tests for ``reverso.codex_sync`` (B5).

No live network. The fetcher is always injected so calls never hit the
gateway. The target ``config.toml`` path is always under ``tmp_path`` so
``~/.codex/config.toml`` is never touched.
"""

from __future__ import annotations

import datetime
import json
import tomllib
from pathlib import Path

import pytest

from reverso import codex_sync


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
        "# END REVERSO GATEWAY PROFILES\n"
        "\n"
        "[tui]\n"
        'status_line = ["model-with-reasoning", "git-branch"]\n'
        "\n"
        "[tui.model_availability_nux]\n"
        '"gpt-5.5" = 4\n'
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


def test_catalog_json_includes_static_metadata_aliases() -> None:
    catalog = json.loads(
        codex_sync._generate_catalog_json(
            [codex_sync.ProviderModels("claude", ("claude-opus-4-8",))]
        )
    )

    slugs = {model["slug"] for model in catalog["models"]}

    assert "claude-opus-4-8" in slugs
    assert "MiniMax-M3" in slugs
    assert "gemini-2.5-pro" in slugs
    assert "gemini-2.5-flash" in slugs


def test_catalog_json_dedupes_static_metadata_aliases() -> None:
    catalog = json.loads(
        codex_sync._generate_catalog_json(
            [
                codex_sync.ProviderModels(
                    "copilot", ("gemini-2.5-pro", "gemini-2.5-flash")
                )
            ]
        )
    )

    slugs = [model["slug"] for model in catalog["models"]]

    assert slugs.count("gemini-2.5-pro") == 1
    assert slugs.count("gemini-2.5-flash") == 1
    assert "MiniMax-M3" in slugs


def test_sync_catalog_aliases_do_not_create_routing_entries(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    catalog = tmp_path / "catalog.json"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(
        target=target,
        fetcher=_make_fetcher({"claude": ["claude-opus-4-8"]}),
        catalog_target=catalog,
    )

    text = target.read_text(encoding="utf-8")
    catalog_slugs = {
        model["slug"]
        for model in json.loads(catalog.read_text(encoding="utf-8"))["models"]
    }

    assert "MiniMax-M3" in catalog_slugs
    assert "gemini-2.5-pro" in catalog_slugs
    assert "gemini-2.5-flash" in catalog_slugs
    assert "MiniMax-M3" not in text
    assert "gemini-2.5-pro" not in text
    assert "gemini-2.5-flash" not in text


def test_sync_writes_block_and_creates_backup(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    fetcher = _make_fetcher()

    result = codex_sync.sync(target=target, fetcher=fetcher)

    assert result.changed is True
    assert result.backup is not None
    assert result.backup.exists()
    new_text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN in new_text
    assert codex_sync.PROFILES_END in new_text
    assert codex_sync.NUX_BEGIN in new_text
    assert codex_sync.NUX_END in new_text
    assert "claude-fable-5" in new_text
    assert "prism-a" in new_text


def test_sync_is_idempotent_no_diff_no_backup(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    fetcher = _make_fetcher()

    first = codex_sync.sync(target=target, fetcher=fetcher)
    assert first.changed is True
    text_after_first = target.read_text(encoding="utf-8")

    second = codex_sync.sync(target=target, fetcher=fetcher)
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

    codex_sync.sync(target=target, fetcher=fetcher)
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
        "[tui.model_availability_nux]",
        '"gpt-5.5" = 4',
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
        )
        assert result.changed is True

    backups = sorted(
        p
        for p in target.parent.iterdir()
        if p.name.startswith(target.name + codex_sync.BACKUP_SUFFIX_PREFIX)
    )
    assert len(backups) == codex_sync.BACKUPS_KEPT

    expected_minutes = list(range(7 - codex_sync.BACKUPS_KEPT, 7))
    expected_stamps = [
        (base_ts + datetime.timedelta(minutes=m)).strftime("%Y%m%dT%H%M%SZ")
        for m in expected_minutes
    ]
    for stamp, backup in zip(expected_stamps, backups, strict=True):
        assert stamp in backup.name


def test_sync_no_existing_file_creates_target_no_backup(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "config.toml"
    fetcher = _make_fetcher()

    result = codex_sync.sync(target=target, fetcher=fetcher)

    assert result.changed is True
    assert result.backup is None
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN in text


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

    codex_sync.sync(target=target, fetcher=_make_fetcher())

    assert seen_dirs, "atomic write must mkstemp; none observed"
    for d in seen_dirs:
        assert d == str(
            target.parent
        ), f"temp file must live in target.parent; saw dir={d}"


def test_sync_no_temp_files_left_behind(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(target=target, fetcher=_make_fetcher())

    leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_render_profiles_block_emits_per_model_tables() -> None:
    pm = [
        codex_sync.ProviderModels("claude", ("claude-fable-5",)),
        codex_sync.ProviderModels("copilot", ("gpt-5.5",)),
    ]
    block = codex_sync._render_profiles_block(pm)
    assert block.startswith(codex_sync.PROFILES_BEGIN)
    assert block.rstrip().endswith(codex_sync.PROFILES_END)
    assert "[model_providers.reverso_claude__claude-fable-5]" in block
    assert "[model_providers.reverso_copilot__gpt-5_5]" in block
    assert 'base_url = "http://127.0.0.1:64946/claude/v1"' in block
    assert 'base_url = "http://127.0.0.1:64946/copilot/v1"' in block
    assert 'wire_api = "responses"' in block
    assert 'model = "claude-fable-5"' in block


def test_render_nux_block_dedupes_model_ids() -> None:
    pm = [
        codex_sync.ProviderModels("claude", ("claude-fable-5", "shared-id")),
        codex_sync.ProviderModels("copilot", ("shared-id",)),
    ]
    block = codex_sync._render_nux_block(pm)
    assert block.count('"shared-id" = 4') == 1
    assert '"claude-fable-5" = 4' in block


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
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main(["--dry-run"])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == baseline
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
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main([])
    assert rc == 0
    new_text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN in new_text
    out = capsys.readouterr().out
    assert '"changed": true' in out


def test_no_secret_material_written_anywhere(tmp_path: Path) -> None:
    """Backup, target, and rendered blocks must never contain secret tokens."""
    target = tmp_path / "config.toml"

    sensitive = "sk-test-secret-do-not-leak-XYZ"
    baseline = (
        f'# api_key (must not be picked up): "{sensitive}"\n' + _baseline_config_text()
    )
    target.write_text(baseline, encoding="utf-8")

    fetcher = _make_fetcher()
    codex_sync.sync(target=target, fetcher=fetcher)

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

    pm = codex_sync.fetch_all(codex_sync.GATEWAY_PREFIXES, fetcher)
    profiles_block = codex_sync._render_profiles_block(pm)
    nux_block = codex_sync._render_nux_block(pm)
    for produced in (profiles_block, nux_block):
        assert "api_key" not in produced
        assert "env_key" not in produced
        assert "secret" not in produced.lower()


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


def test_toml_table_key_replaces_invalid_characters() -> None:
    assert codex_sync._toml_table_key("gpt-5.5") == "gpt-5_5"
    assert codex_sync._toml_table_key("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert codex_sync._toml_table_key("a/b@c") == "a_b_c"
    assert codex_sync._toml_table_key("") == "model"


def test_sync_with_no_models_writes_empty_profiles_and_no_nux_block(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    empty = _make_fetcher({"claude": [], "copilot": [], "auggie": [], "deepseek": []})

    codex_sync.sync(target=target, fetcher=empty)
    text = target.read_text(encoding="utf-8")
    assert codex_sync.PROFILES_BEGIN in text
    assert "[model_providers.reverso_claude__" not in text
    # The user already owns [tui.model_availability_nux]; with nothing new to
    # add, no managed NUX block may exist (an empty one earned its deletion).
    assert codex_sync.NUX_BEGIN not in text
    assert text.count("[tui.model_availability_nux]") == 1
    assert '"gpt-5.5" = 4' in text


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


def test_sync_merges_into_existing_nux_table_single_header(tmp_path: Path) -> None:
    """Regression: a pre-existing user NUX table must never get a second header.

    The live ~/.codex/config.toml already had [tui.model_availability_nux]
    (with "gpt-5.5" = 4); the old renderer emitted its own header at EOF,
    producing a duplicate-table TOML error that broke codex entirely.
    """
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(target=target, fetcher=_make_fetcher())
    text = target.read_text(encoding="utf-8")

    assert text.count("[tui.model_availability_nux]") == 1
    parsed = tomllib.loads(text)
    nux = parsed["tui"]["model_availability_nux"]
    assert nux["gpt-5.5"] == 4
    assert nux["claude-fable-5"] == 4
    assert nux["deepseek-v3"] == 4
    # User-owned colliding key must appear exactly once, never re-emitted.
    assert text.count('"gpt-5.5" = 4') == 1


def test_sync_inserts_nux_entries_inside_user_table(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    codex_sync.sync(target=target, fetcher=_make_fetcher())
    text = target.read_text(encoding="utf-8")

    header_idx = text.index("[tui.model_availability_nux]")
    begin_idx = text.index(codex_sync.NUX_BEGIN)
    end_idx = text.index(codex_sync.NUX_END)
    projects_idx = text.index('[projects."/Users/example/repo"]')
    assert header_idx < begin_idx < end_idx < projects_idx


def test_sync_idempotent_with_existing_nux_table(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    first = codex_sync.sync(target=target, fetcher=_make_fetcher())
    assert first.changed is True
    text_first = target.read_text(encoding="utf-8")

    second = codex_sync.sync(target=target, fetcher=_make_fetcher())
    assert second.changed is False
    text_second = target.read_text(encoding="utf-8")
    assert text_first == text_second
    assert text_second.count("[tui.model_availability_nux]") == 1


def test_sync_all_keys_already_present_writes_no_nux_block(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    only_collision = _make_fetcher(
        {"claude": [], "copilot": ["gpt-5.5"], "auggie": [], "deepseek": []}
    )

    codex_sync.sync(target=target, fetcher=only_collision)
    text = target.read_text(encoding="utf-8")

    assert codex_sync.NUX_BEGIN not in text
    assert text.count('"gpt-5.5" = 4') == 1
    assert tomllib.loads(text)["tui"]["model_availability_nux"]["gpt-5.5"] == 4


def test_sync_relocates_legacy_eof_nux_block_into_user_table(tmp_path: Path) -> None:
    """Self-healing: a block written by the buggy version (duplicate header at
    EOF) must be stripped and re-merged inside the user table."""
    legacy_block = "\n".join(
        [
            codex_sync.NUX_BEGIN,
            "[tui.model_availability_nux]",
            '"claude-fable-5" = 4',
            '"gpt-5.5" = 4',
            codex_sync.NUX_END,
        ]
    )
    corrupted = _baseline_config_text() + "\n" + legacy_block + "\n"
    assert corrupted.count("[tui.model_availability_nux]") == 2

    target = tmp_path / "config.toml"
    target.write_text(corrupted, encoding="utf-8")

    result = codex_sync.sync(target=target, fetcher=_make_fetcher())
    assert result.changed is True
    text = target.read_text(encoding="utf-8")

    assert text.count("[tui.model_availability_nux]") == 1
    parsed = tomllib.loads(text)
    nux = parsed["tui"]["model_availability_nux"]
    assert nux["gpt-5.5"] == 4
    assert nux["claude-fable-5"] == 4
    assert text.count('"gpt-5.5" = 4') == 1


def test_sync_refuses_to_write_when_user_toml_is_invalid(tmp_path: Path) -> None:
    """Fail closed: user-owned duplicate tables (invalid TOML) must abort the
    sync before any backup or write happens."""
    broken = _baseline_config_text() + "\n[tui.model_availability_nux]\n'again' = 1\n"
    target = tmp_path / "config.toml"
    target.write_text(broken, encoding="utf-8")

    with pytest.raises(RuntimeError):
        codex_sync.sync(target=target, fetcher=_make_fetcher())

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
    broken = _baseline_config_text() + "\n[tui.model_availability_nux]\n'again' = 1\n"
    target = tmp_path / "config.toml"
    target.write_text(broken, encoding="utf-8")
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
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


def test_render_nux_entries_excludes_existing_keys() -> None:
    pm = [
        codex_sync.ProviderModels("claude", ("claude-fable-5",)),
        codex_sync.ProviderModels("copilot", ("gpt-5.5",)),
    ]
    block = codex_sync._render_nux_entries(pm, frozenset({"gpt-5.5"}))
    assert block is not None
    assert '"claude-fable-5" = 4' in block
    assert "gpt-5.5" not in block
    assert "[tui.model_availability_nux]" not in block

    nothing_new = codex_sync._render_nux_entries(
        pm, frozenset({"gpt-5.5", "claude-fable-5"})
    )
    assert nothing_new is None


def test_render_profiles_block_dedupes_coerced_section_collisions() -> None:
    pm = [codex_sync.ProviderModels("copilot", ("gpt-5.5", "gpt-5_5"))]
    block = codex_sync._render_profiles_block(pm)
    assert block.count("[model_providers.reverso_copilot__gpt-5_5]") == 1


def test_sync_handles_crlf_config_with_existing_nux_table(tmp_path: Path) -> None:
    """Regression: CRLF-edited configs must take the header-aware merge path.

    Before the \\r-tolerant header regex, the user table went undetected and
    sync emitted a duplicate header, failing closed with exit 3 forever.
    """
    crlf_baseline = _baseline_config_text().replace("\n", "\r\n")
    target = tmp_path / "config.toml"
    target.write_bytes(crlf_baseline.encode("utf-8"))

    first = codex_sync.sync(target=target, fetcher=_make_fetcher())
    assert first.changed is True
    text = target.read_bytes().decode("utf-8")

    assert text.count("[tui.model_availability_nux]") == 1
    parsed = tomllib.loads(text)
    nux = parsed["tui"]["model_availability_nux"]
    assert nux["gpt-5.5"] == 4
    assert nux["claude-fable-5"] == 4

    second = codex_sync.sync(target=target, fetcher=_make_fetcher())
    assert second.changed is False


def test_renderers_escape_hostile_model_ids(tmp_path: Path) -> None:
    hostile = 'we"ird\\id'
    pm = [codex_sync.ProviderModels("claude", (hostile,))]

    entries = codex_sync._render_nux_entries(pm, frozenset())
    assert entries is not None
    body = "\n".join(line for line in entries.splitlines() if not line.startswith("#"))
    assert tomllib.loads("[t]\n" + body) == {"t": {hostile: 4}}

    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    codex_sync.sync(target=target, fetcher=_make_fetcher({"claude": [hostile]}))
    text = target.read_text(encoding="utf-8")
    full = tomllib.loads(text)
    assert full["tui"]["model_availability_nux"][hostile] == 4
    section = f"reverso_claude__{codex_sync._toml_table_key(hostile)}"
    assert full["model_providers"][section]["model"] == hostile


def test_nux_entries_dedupe_on_coerced_key_matching_profiles() -> None:
    pm = [codex_sync.ProviderModels("copilot", ("gpt-5.5", "gpt-5_5"))]
    entries = codex_sync._render_nux_entries(pm, frozenset())
    assert entries is not None
    assert '"gpt-5.5" = 4' in entries
    assert '"gpt-5_5"' not in entries

    block = codex_sync._render_nux_block(pm)
    assert block.count(" = 4") == 1


def test_sentinel_mentioned_midline_in_comment_is_ignored(tmp_path: Path) -> None:
    baseline = (
        _baseline_config_text()
        + f"# note: the marker {codex_sync.NUX_BEGIN} is managed tooling\n"
    )
    target = tmp_path / "config.toml"
    target.write_text(baseline, encoding="utf-8")

    result = codex_sync.sync(target=target, fetcher=_make_fetcher())
    assert result.changed is True
    text = target.read_text(encoding="utf-8")
    assert f"# note: the marker {codex_sync.NUX_BEGIN} is managed tooling" in text
    assert text.count("[tui.model_availability_nux]") == 1
    tomllib.loads(text)


def test_generate_catalog_json_shape_dedup_and_context_window() -> None:
    pm = [
        codex_sync.ProviderModels("claude", ("shared-model", "big-500k-model")),
        codex_sync.ProviderModels("copilot", ("shared-model", "gpt-4o")),
    ]
    payload = json.loads(codex_sync._generate_catalog_json(pm))

    assert set(payload.keys()) == {"models"}
    slugs = [m["slug"] for m in payload["models"]]
    assert slugs[:3] == ["shared-model", "big-500k-model", "gpt-4o"]
    assert "MiniMax-M3" in slugs
    assert "gemini-2.5-pro" in slugs
    assert "gemini-2.5-flash" in slugs

    by_slug = {m["slug"]: m for m in payload["models"]}
    # First prefix carrying the id owns the display name.
    assert by_slug["shared-model"]["display_name"] == "Reverso claude shared-model"
    assert by_slug["shared-model"]["context_window"] == 128000
    assert by_slug["big-500k-model"]["context_window"] == 500000
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


def test_generate_catalog_json_survives_hostile_model_ids() -> None:
    hostile = 'evil"\\\nmodel\t\x01id'
    pm = [codex_sync.ProviderModels("claude", (hostile,))]

    payload = json.loads(codex_sync._generate_catalog_json(pm))

    assert payload["models"][0]["slug"] == hostile
    assert payload["models"][0]["display_name"] == f"Reverso claude {hostile}"


def test_resolve_catalog_path_prefers_explicit_then_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("REVERSO_CODEX_CATALOG", raising=False)
    assert codex_sync._resolve_catalog_path(None) == codex_sync.DEFAULT_CATALOG_PATH

    monkeypatch.setenv("REVERSO_CODEX_CATALOG", str(tmp_path / "env-catalog.json"))
    assert codex_sync._resolve_catalog_path(None) == tmp_path / "env-catalog.json"

    explicit = tmp_path / "explicit-catalog.json"
    assert codex_sync._resolve_catalog_path(explicit) == explicit


def test_sync_with_catalog_target_writes_catalog_and_references_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog = tmp_path / "catalog.json"

    result = codex_sync.sync(
        target=target, fetcher=_make_fetcher(), catalog_target=catalog
    )

    assert result.changed is True
    assert result.catalog == catalog
    assert catalog.exists()
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    assert any(m["slug"] == "claude-fable-5" for m in payload["models"])

    text = target.read_text(encoding="utf-8")
    assert codex_sync.CATALOG_BEGIN in text
    assert codex_sync.CATALOG_END in text
    assert f'model_catalog_json = "{catalog}"' in text
    assert text.index("model_catalog_json") < text.index("[model_providers.minimax]")
    assert "model_catalog_json" not in text[text.index(codex_sync.PROFILES_BEGIN) :]
    tomllib.loads(text)


def test_sync_without_catalog_target_writes_no_catalog(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")

    result = codex_sync.sync(target=target, fetcher=_make_fetcher())

    assert result.catalog is None
    assert "model_catalog_json" not in target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.json"))


def test_sync_unchanged_run_regenerates_deleted_catalog(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    fetcher = _make_fetcher()

    first = codex_sync.sync(target=target, fetcher=fetcher, catalog_target=catalog)
    assert first.changed is True
    assert catalog.exists()
    catalog_text = catalog.read_text(encoding="utf-8")

    catalog.unlink()
    second = codex_sync.sync(target=target, fetcher=fetcher, catalog_target=catalog)

    assert second.changed is False
    assert second.backup is None
    assert second.catalog == catalog
    assert catalog.exists(), "unchanged config must still restore a deleted catalog"
    assert catalog.read_text(encoding="utf-8") == catalog_text


def test_main_write_catalog_flag_writes_and_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main(["--write-catalog", "--catalog", str(catalog)])

    assert rc == 0
    assert catalog.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["catalog"] == str(catalog)


def test_main_without_write_catalog_flag_ignores_catalog_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_baseline_config_text(), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main(["--catalog", str(catalog)])

    assert rc == 0
    assert not catalog.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["catalog"] is None


def test_main_dry_run_reports_catalog_target_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.toml"
    baseline = _baseline_config_text()
    target.write_text(baseline, encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(target))
    monkeypatch.setattr(
        codex_sync,
        "_default_fetcher",
        lambda base_url: _make_fetcher(),
    )

    rc = codex_sync.main(["--dry-run", "--write-catalog", "--catalog", str(catalog)])

    assert rc == 0
    assert not catalog.exists()
    assert target.read_text(encoding="utf-8") == baseline
    report = json.loads(capsys.readouterr().out)
    assert report["catalog_target"] == str(catalog)


def test_merge_catalog_config_block_replaces_and_removes_when_disabled(
    tmp_path: Path,
) -> None:
    catalog_a = tmp_path / "a.json"
    catalog_b = tmp_path / "b.json"
    base = _baseline_config_text()

    first = codex_sync._merge_catalog_config_block(base, catalog_a)
    assert f'model_catalog_json = "{catalog_a}"' in first
    assert first.index("model_catalog_json") < first.index("[model_providers.minimax]")

    second = codex_sync._merge_catalog_config_block(first, catalog_b)
    assert f'model_catalog_json = "{catalog_a}"' not in second
    assert f'model_catalog_json = "{catalog_b}"' in second
    assert second.count(codex_sync.CATALOG_BEGIN) == 1

    removed = codex_sync._merge_catalog_config_block(second, None)
    assert codex_sync.CATALOG_BEGIN not in removed
    assert "model_catalog_json" not in removed
    tomllib.loads(removed)
