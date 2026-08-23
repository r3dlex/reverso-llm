"""Owned-artifact lifecycle primitives shared by every client-sync family."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from reverso.client_sync_mutations import (
    capture_state,
    file_state,
    is_owned_by_marker,
    is_path_owned_by_marker,
    next_backup_path,
)

MARKER = "# Managed by reverso-test-sync."


def _owned_bytes() -> bytes:
    return f"{MARKER}\nbody\n".encode("utf-8")


def test_owned_by_marker_reads_head_lines_of_captured_state() -> None:
    assert is_owned_by_marker(file_state(_owned_bytes()), MARKER)
    assert is_owned_by_marker(file_state(_owned_bytes()), MARKER, head_lines=1)


def test_owned_by_marker_rejects_absent_unmarked_and_non_files() -> None:
    assert not is_owned_by_marker(capture_state(Path("/nonexistent")), MARKER)
    assert not is_owned_by_marker(file_state("{ user content }\n"), MARKER)
    assert not is_owned_by_marker(file_state(b"\xff\xfe\x00"), MARKER)


def test_owned_by_marker_head_window_is_respected() -> None:
    late_marker = f"header\nfooter\n{MARKER}\n".encode("utf-8")
    assert not is_owned_by_marker(file_state(late_marker), MARKER)
    assert is_owned_by_marker(file_state(late_marker), MARKER, head_lines=3)


def test_path_owned_by_marker_requires_real_regular_file(tmp_path: Path) -> None:
    owned = tmp_path / "managed.conf"
    owned.write_bytes(_owned_bytes())
    link = tmp_path / "link.conf"
    link.symlink_to(owned)
    unmarked = tmp_path / "user.conf"
    unmarked.write_text("{ user }\n", encoding="utf-8")
    missing = tmp_path / "missing.conf"

    assert is_path_owned_by_marker(owned, MARKER)
    assert not is_path_owned_by_marker(link, MARKER)
    assert not is_path_owned_by_marker(unmarked, MARKER)
    assert not is_path_owned_by_marker(missing, MARKER)


def test_next_backup_path_stamps_timestamp_and_skips_existing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "settings.json"
    frozen = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

    first = next_backup_path(target, suffix_prefix=".reverso.bak.", now=frozen)
    assert first.name == "settings.json.reverso.bak.20260823T120000Z"
    assert not first.exists()

    first.write_text("taken", encoding="utf-8")
    second = next_backup_path(target, suffix_prefix=".reverso.bak.", now=frozen)
    assert second.name == "settings.json.reverso.bak.20260823T120000Z.1"

    second.write_text("also taken", encoding="utf-8")
    third = next_backup_path(target, suffix_prefix=".reverso.bak.", now=frozen)
    assert third.name == "settings.json.reverso.bak.20260823T120000Z.2"


def test_next_backup_path_skips_symlink_siblings(tmp_path: Path) -> None:
    target = tmp_path / "cfg.toml"
    decoy_name = "cfg.toml.reverso.bak.20260823T120000Z"
    decoy_target = tmp_path / "elsewhere"
    decoy_target.write_text("x", encoding="utf-8")
    (tmp_path / decoy_name).symlink_to(decoy_target)

    candidate = next_backup_path(
        target,
        suffix_prefix=".reverso.bak.",
        now=datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC),
    )
    assert candidate.name == f"{decoy_name}.1"
