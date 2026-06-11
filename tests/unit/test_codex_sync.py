"""Unit tests for ``reverso.codex_sync`` (B5).

No live network. The fetcher is always injected so calls never hit the
gateway. The target ``config.toml`` path is always under ``tmp_path`` so
``~/.codex/config.toml`` is never touched.
"""

from __future__ import annotations

import datetime
import tomllib
from pathlib import Path

import pytest

from reverso import codex_sync


def _fixture_payload() -> dict[str, list[str]]:
    """Stable fixture model id payload, frozen here so changes are deliberate."""
    return {
        "claude": ["claude-fable-5", "claude-sonnet-4-6"],
        "copilot": ["gpt-4o", "gpt-5.5"],
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
