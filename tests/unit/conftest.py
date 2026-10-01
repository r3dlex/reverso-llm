"""Shared unit-test seams for client convergence."""

from __future__ import annotations

import pytest

from reverso import client_sync
from reverso.client_sync_mutations import PreparedGroup


@pytest.fixture
def stub_opencode_convergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize OpenCode convergence for tests that assert other surfaces.

    ``client_sync`` prepares the OpenCode fragments from the gateway's live
    ``/v1/models`` listing, the authority on model exposure. A unit test must not
    depend on a running gateway: without this the listing is refused and the run
    degrades to ``partial_freshness``, which passes on a developer machine that
    happens to serve :64946 and fails in CI. Tests covering OpenCode convergence
    itself patch ``prepare_sync`` directly instead.
    """
    monkeypatch.setattr(
        client_sync.opencode_sync,
        "prepare_sync",
        lambda _config_dir, **_kwargs: client_sync.opencode_sync.PreparedOpenCodeSync(
            PreparedGroup("opencode", ()),
            client_sync.opencode_sync.OpenCodeSyncResult(
                config_dir="",
                changed=False,
                dry_run=True,
                changed_profiles=(),
                conflicting_profiles=(),
                model_count=0,
                manual_step=None,
            ),
        ),
    )
