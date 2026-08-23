"""OpenCode client-surface sync invariants (mirrors test_codex/test_claude_code)."""

import json
from pathlib import Path

import pytest

from reverso import opencode_sync
from reverso.client_sync_lock import acquire_client_sync_lock

MANAGED_MARKER = opencode_sync.MANAGED_MARKER


def _listing(*rows: str) -> dict:
    return {"data": [{"type": "model", "id": row, "display_name": row} for row in rows]}


def _fetcher(payload: dict | Exception):
    def fetch(base_url: str) -> dict:
        if isinstance(payload, Exception):
            raise payload
        return payload

    return fetch


FULL_LISTING = _listing(
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "deepseek-v4-pro",
    "kimi-k3",
    "anthropic-codex-gpt-5.5",
    "anthropic-copilot-claude-opus-4-8",
    "anthropic-auggie-default",
    "anthropic-ollama-llama3:8b",
)

FULL_BARE_IDS = {
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "deepseek-v4-pro",
    "kimi-k3",
}


def _prepare(tmp_path: Path, fetcher, **kwargs):
    return opencode_sync.prepare_sync(
        tmp_path / "opencode",
        models_fetcher=fetcher,
        **kwargs,
    )


def test_prepare_makes_no_filesystem_changes(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    existing = config_dir / "opencode-reverso.jsonc"
    existing.write_text(f"{MANAGED_MARKER}\n{{}}\n", encoding="utf-8")
    before = existing.read_bytes()

    prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))

    assert existing.read_bytes() == before
    assert prepared.group.changed
    assert prepared.result.dry_run


def test_prepare_fail_closed_on_discovery_error(tmp_path: Path) -> None:
    prepared = _prepare(
        tmp_path,
        _fetcher(ConnectionError("gateway down")),
    )
    assert prepared.result.error is not None
    assert prepared.result.model_count is None
    assert not prepared.group.mutations


def test_prepare_fail_closed_on_empty_listing(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path, _fetcher(_listing()))
    assert prepared.result.error is not None
    assert not prepared.group.mutations


def test_prepare_fail_closed_on_unroutable_bare_id(tmp_path: Path) -> None:
    listing = _listing("totally-unknown-model")
    prepared = _prepare(tmp_path, _fetcher(listing))
    assert prepared.result.error is not None
    assert "totally-unknown-model" in (prepared.result.error or "")


def test_unmanaged_conflict_fails_closed_without_writes(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    user_file = config_dir / "opencode-claude.jsonc"
    user_file.write_text("{ user owned }\n", encoding="utf-8")

    prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))

    assert "opencode-claude" in prepared.result.conflicting_profiles
    assert prepared.result.error is not None
    assert user_file.read_text(encoding="utf-8") == "{ user owned }\n"
    assert not [m for m in prepared.group.mutations if m.path == user_file]


def test_apply_requires_lock_token(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))
    with pytest.raises(RuntimeError, match="lock"):
        opencode_sync.apply_prepared(prepared, lock_token=None)  # type: ignore[arg-type]


def test_apply_under_held_lock_writes_managed_fragments(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    with acquire_client_sync_lock(path=tmp_path / "sync.lock") as held:
        prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))
        result = opencode_sync.apply_prepared(prepared, lock_token=held)

    assert result.error is None
    assert not result.dry_run
    names = sorted(
        path.name.removesuffix(".jsonc") for path in config_dir.glob("opencode-*.jsonc")
    )
    assert names == sorted(name for name, _route in opencode_sync.LAUNCHER_CATALOGS)


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    with acquire_client_sync_lock(path=tmp_path / "sync.lock") as held:
        first = _prepare(tmp_path, _fetcher(FULL_LISTING))
        opencode_sync.apply_prepared(first, lock_token=held)
        second = _prepare(tmp_path, _fetcher(FULL_LISTING))
        second_result = opencode_sync.apply_prepared(second, lock_token=held)
    assert not second_result.changed
    assert second_result.changed_profiles == ()


def test_failed_discovery_preserves_managed_artifacts(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    existing = config_dir / "opencode-reverso.jsonc"
    existing.write_text(f"{MANAGED_MARKER}\n{{}}\n", encoding="utf-8")
    before = existing.read_bytes()

    failed = _prepare(tmp_path, _fetcher(_listing()))
    assert failed.result.error is not None
    assert existing.read_bytes() == before


def test_user_opencode_json_is_never_touched_and_manual_step_printed(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    user_file = config_dir / "opencode.json"
    user_file.write_text('{"theme": "solarized"}\n', encoding="utf-8")

    with acquire_client_sync_lock(path=tmp_path / "sync.lock") as held:
        prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))
        result = opencode_sync.apply_prepared(prepared, lock_token=held)

    assert user_file.read_text(encoding="utf-8") == '{"theme": "solarized"}\n'
    assert result.manual_step is not None
    assert "OPENCODE_CONFIG" in result.manual_step
    assert not [m for m in prepared.group.mutations if m.path == user_file]


def test_fragment_binds_anthropic_messages_surface(tmp_path: Path) -> None:
    with acquire_client_sync_lock(path=tmp_path / "sync.lock") as held:
        prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))
        opencode_sync.apply_prepared(prepared, lock_token=held)

    text = (tmp_path / "opencode" / "opencode-reverso.jsonc").read_text(
        encoding="utf-8"
    )
    assert text.splitlines()[0] == MANAGED_MARKER
    document = json.loads("\n".join(text.splitlines()[1:]))
    provider = document["provider"]["reverso"]
    assert provider["options"]["baseURL"] == "http://127.0.0.1:64946"
    assert provider["npm"] == "@ai-sdk/anthropic"


def test_selectors_are_prefix_correct_per_route(tmp_path: Path) -> None:
    with acquire_client_sync_lock(path=tmp_path / "sync.lock") as held:
        prepared = _prepare(tmp_path, _fetcher(FULL_LISTING))
        opencode_sync.apply_prepared(prepared, lock_token=held)

    config_dir = tmp_path / "opencode"

    def models_of(name: str) -> set[str]:
        text = (config_dir / f"{name}.jsonc").read_text(encoding="utf-8")
        document = json.loads("\n".join(text.splitlines()[1:]))
        return set(document["provider"]["reverso"]["models"])

    all_models = models_of("opencode-reverso")
    assert all_models == {
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "deepseek-v4-pro",
        "kimi-k3",
        "anthropic-codex-gpt-5.5",
        "anthropic-copilot-claude-opus-4-8",
        "anthropic-auggie-default",
        "anthropic-ollama-llama3:8b",
    }
    # No new prefix namespaces: every selector is either a machine-minted
    # anthropic-<backend>-<bare> discovery alias over a known backend or one of
    # the bare Anthropic-surface ids from the live listing.
    for selector in all_models - FULL_BARE_IDS:
        assert selector.startswith("anthropic-")
        assert any(
            selector.startswith(f"anthropic-{backend}-")
            for backend in opencode_sync.KNOWN_ANTHROPIC_BACKENDS
        )
    assert models_of("opencode-claude") == {
        "claude-opus-4-8",
        "claude-sonnet-4-6",
    }
    assert models_of("opencode-deepseek") == {"deepseek-v4-pro"}
    assert models_of("opencode-kimi") == {"kimi-k3"}
    assert models_of("opencode-codex") == {"anthropic-codex-gpt-5.5"}
    assert models_of("opencode-copilot") == {"anthropic-copilot-claude-opus-4-8"}
    assert models_of("opencode-auggie") == {"anthropic-auggie-default"}
    assert models_of("opencode-ollama") == {"anthropic-ollama-llama3:8b"}
