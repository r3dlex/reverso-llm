from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from reverso.protocols.adapters.kimi import KimiError
from reverso.protocols.kimi_usage import (
    KimiUsageService,
    kimi_codex_rate_limit_headers,
    parse_kimi_usage,
)


class _Auth:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def resolve_existing_bearer_token(
        self, *, force_refresh: bool = False
    ) -> str:
        self.calls.append(force_refresh)
        return "secret-token"


def _payload(*, weekly_limit: Any = "100", weekly_used: Any = "20") -> dict[str, Any]:
    return {
        "usage": {
            "limit": weekly_limit,
            "used": weekly_used,
            "remaining": "80",
            "resetTime": "2026-08-01T22:51:48.151162Z",
        },
        "limits": [
            {
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {
                    "limit": "100",
                    "used": "34",
                    "remaining": "66",
                    "resetTime": "2026-07-26T10:51:48.151162Z",
                },
            },
            {
                "window": {"duration": 1, "timeUnit": "TIME_UNIT_DAY"},
                "detail": {"limit": "10", "used": "9"},
            },
        ],
    }


def test_parse_kimi_usage_maps_weekly_and_300_minute_windows() -> None:
    parsed = parse_kimi_usage(_payload())

    assert parsed == {
        "five_hour": {
            "used_percent": 34.0,
            "resets_at": "2026-07-26T10:51:48.151162Z",
        },
        "weekly": {
            "used_percent": 20.0,
            "resets_at": "2026-08-01T22:51:48.151162Z",
        },
    }


def test_parse_kimi_usage_accepts_official_cli_payload_variants() -> None:
    payload = {
        "usage": {
            "limit": "100",
            "remaining": "80",
            "reset_at": "2026-08-01T22:51:48.151162Z",
        },
        "limits": [
            {
                "duration": "300",
                "timeUnit": "MANAGED_USAGE_TIME_UNIT_MINUTE",
                "limit": "100",
                "remaining": "66",
                "resetAt": "2026-07-26T10:51:48.151162Z",
            }
        ],
    }

    assert parse_kimi_usage(payload) == {
        "five_hour": {
            "used_percent": 34.0,
            "resets_at": "2026-07-26T10:51:48.151162Z",
        },
        "weekly": {
            "used_percent": 20.0,
            "resets_at": "2026-08-01T22:51:48.151162Z",
        },
    }


def test_parse_kimi_usage_accepts_five_hour_unit_and_reset_time_alias() -> None:
    payload = _payload()
    payload["limits"][0] = {
        "window": {"duration": "5", "timeUnit": "TIME_UNIT_HOUR"},
        "detail": {
            "limit": "100",
            "used": "34",
            "reset_time": "2026-07-26T10:51:48.151162Z",
        },
    }

    assert parse_kimi_usage(payload)["five_hour"] == {
        "used_percent": 34.0,
        "resets_at": "2026-07-26T10:51:48.151162Z",
    }


@pytest.mark.parametrize("reset_key", ["reset_in", "resetIn", "ttl", "window"])
def test_parse_kimi_usage_converts_official_relative_reset_aliases(
    reset_key: str,
) -> None:
    observed_at = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    payload = _payload()
    payload["usage"].pop("resetTime")
    payload["usage"][reset_key] = "90"
    payload["limits"][0]["detail"].pop("resetTime")
    payload["limits"][0]["detail"][reset_key] = 120

    parsed = parse_kimi_usage(payload, observed_at=observed_at)

    assert parsed["weekly"]["resets_at"] == (
        observed_at + timedelta(seconds=90)
    ).isoformat()
    assert parsed["five_hour"]["resets_at"] == (
        observed_at + timedelta(seconds=120)
    ).isoformat()


def test_kimi_codex_rate_limit_headers_maps_both_windows() -> None:
    snapshot = {
        "rate_limits": {
            "five_hour": {
                "used_percent": 34.0,
                "resets_at": "2026-07-26T10:51:48.151162Z",
            },
            "weekly": {
                "used_percent": 20.0,
                "resets_at": "2026-08-01T22:51:48.151162Z",
            },
        }
    }

    headers = dict(kimi_codex_rate_limit_headers(snapshot))

    assert headers == {
        b"x-codex-primary-used-percent": b"34.0",
        b"x-codex-primary-window-minutes": b"300",
        b"x-codex-primary-reset-at": b"1785063108",
        b"x-codex-secondary-used-percent": b"20.0",
        b"x-codex-secondary-window-minutes": b"10080",
        b"x-codex-secondary-reset-at": b"1785624708",
    }


def test_kimi_codex_rate_limit_headers_omits_malformed_windows() -> None:
    assert (
        kimi_codex_rate_limit_headers(
            {
                "rate_limits": {
                    "five_hour": {
                        "used_percent": 34.0,
                        "resets_at": "not-a-timestamp",
                    },
                    "weekly": None,
                }
            }
        )
        == []
    )


@pytest.mark.parametrize("limit", [0, "0", -1, "bad", None, True])
def test_parse_kimi_usage_rejects_invalid_or_zero_limits(limit: Any) -> None:
    parsed = parse_kimi_usage(_payload(weekly_limit=limit))

    assert parsed["weekly"] is None
    assert parsed["five_hour"]["used_percent"] == 34.0


@pytest.mark.asyncio
async def test_usage_refresh_retries_401_with_forced_auth_refresh() -> None:
    auth = _Auth()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/coding/v1/usages"
        assert request.headers["authorization"] == "Bearer secret-token"
        assert request.headers["accept"] == "application/json"
        if calls == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=_payload())

    service = KimiUsageService(
        auth=auth,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    snapshot = await service.snapshot()

    assert auth.calls == [False, True]
    assert calls == 2
    assert snapshot["stale"] is False
    assert snapshot["rate_limits"]["weekly"]["used_percent"] == 20.0


@pytest.mark.asyncio
async def test_usage_cache_is_ttl_bounded_and_single_flight() -> None:
    auth = _Auth()
    clock_value = 100.0
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=_payload())

    service = KimiUsageService(
        auth=auth,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
        ttl_seconds=60.0,
        clock=lambda: clock_value,
    )

    tasks = [asyncio.create_task(service.snapshot()) for _ in range(5)]
    await entered.wait()
    assert calls == 1
    release.set()
    snapshots = await asyncio.gather(*tasks)
    assert all(snapshot == snapshots[0] for snapshot in snapshots)

    await service.snapshot()
    assert calls == 1

    clock_value = 161.0
    release.clear()
    refresh = asyncio.create_task(service.snapshot())
    await entered.wait()
    release.set()
    await refresh
    assert calls == 2


@pytest.mark.asyncio
async def test_usage_cache_ttl_starts_after_refresh_completes() -> None:
    clock_value = 100.0
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls, clock_value
        calls += 1
        clock_value += 5.0
        return httpx.Response(200, json=_payload())

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
        ttl_seconds=60.0,
        clock=lambda: clock_value,
    )

    await service.snapshot()
    clock_value = 164.0
    await service.snapshot()
    assert calls == 1

    clock_value = 166.0
    await service.snapshot()
    assert calls == 2


@pytest.mark.asyncio
async def test_usage_refresh_failure_returns_last_known_good_as_stale() -> None:
    auth = _Auth()
    clock_value = 100.0
    responses = [httpx.Response(200, json=_payload()), httpx.Response(503)]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    service = KimiUsageService(
        auth=auth,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
        ttl_seconds=10.0,
        clock=lambda: clock_value,
    )
    fresh = await service.snapshot()
    clock_value = 111.0
    stale = await service.snapshot()

    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert stale["rate_limits"] == fresh["rate_limits"]
    assert stale["refreshed_at"] == fresh["refreshed_at"]
    assert "secret-token" not in repr(stale)


@pytest.mark.asyncio
async def test_usage_failure_backoff_starts_after_refresh_completes() -> None:
    clock_value = 100.0
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls, clock_value
        calls += 1
        clock_value += 20.0
        return httpx.Response(503)

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
        ttl_seconds=60.0,
        clock=lambda: clock_value,
    )

    await service.snapshot()
    clock_value = 129.0
    await service.snapshot()
    assert calls == 1

    clock_value = 131.0
    await service.snapshot()
    assert calls == 2


@pytest.mark.asyncio
async def test_usage_malformed_object_preserves_last_known_good_as_stale() -> None:
    responses = [httpx.Response(200, json=_payload()), httpx.Response(200, json={})]
    clock_value = 100.0

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
        ttl_seconds=10.0,
        clock=lambda: clock_value,
    )

    fresh = await service.snapshot()
    clock_value = 111.0
    stale = await service.snapshot()

    assert stale["stale"] is True
    assert stale["rate_limits"] == fresh["rate_limits"]
    assert stale["refreshed_at"] == fresh["refreshed_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, {"usage": {}}, {"limits": [{}]}, {"usage": {"limit": "bad"}}],
)
async def test_usage_malformed_object_returns_safe_unavailable(payload: Any) -> None:
    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            )
        ),
    )

    snapshot = await service.snapshot()

    assert snapshot["rate_limits"] is None
    assert snapshot["stale"] is True


@pytest.mark.asyncio
async def test_usage_missing_credentials_returns_safe_unavailable_without_login() -> (
    None
):
    class MissingAuth:
        async def resolve_existing_bearer_token(
            self, *, force_refresh: bool = False
        ) -> str:
            raise KimiError("credentials unavailable: secret must not surface")

    service = KimiUsageService(
        auth=MissingAuth(),
        client_factory=lambda: pytest.fail("missing auth must not reach upstream"),
    )

    snapshot = await service.snapshot()

    assert snapshot["provider"] == "kimi"
    assert snapshot["rate_limits"] is None
    assert snapshot["refreshed_at"] is None
    assert snapshot["stale"] is True
    assert "secret" not in repr(snapshot)


@pytest.mark.asyncio
async def test_usage_malformed_non_object_payload_returns_safe_unavailable() -> None:
    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=["not", "an", "object"])
            )
        ),
    )

    snapshot = await service.snapshot()

    assert snapshot["rate_limits"] is None
    assert snapshot["stale"] is True


@pytest.mark.asyncio
async def test_usage_timeout_returns_safe_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    snapshot = await service.snapshot()

    assert snapshot["rate_limits"] is None
    assert snapshot["stale"] is True


@pytest.mark.asyncio
async def test_background_refresh_close_cancels_inflight_poll() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    service.refresh_in_background()
    await entered.wait()
    await service.close()

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_usage_close_prevents_future_network_work_and_is_idempotent() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_payload())

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    await service.close()
    service.refresh_in_background()
    await asyncio.sleep(0)
    snapshot = await service.snapshot()
    await service.close()

    assert requests == 0
    assert snapshot["rate_limits"] is None
    assert snapshot["stale"] is True


@pytest.mark.asyncio
async def test_usage_close_wins_race_with_scheduled_background_refresh() -> None:
    entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        return httpx.Response(200, json=_payload())

    service = KimiUsageService(
        auth=_Auth(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    service.refresh_in_background()
    await service.close()
    await asyncio.sleep(0)

    assert not entered.is_set()
