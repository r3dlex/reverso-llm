"""Cached, secret-free Kimi subscription usage telemetry."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

import httpx

from reverso.protocols.adapters.kimi import (
    KIMI_API_BASE,
    KIMI_DEFAULT_MODEL,
    KimiError,
    KimiOAuthAuth,
)
from reverso.protocols.model_exposure import (
    KIMI_CODEX_AUTO_COMPACT_TOKEN_LIMIT,
    KIMI_CODEX_CONTEXT_WINDOW,
)

_USAGE_TTL_SECONDS = 60.0
_FAILURE_RETRY_SECONDS = 10.0
_USAGE_TIMEOUT_SECONDS = 5.0
_KIMI_CODE_PLATFORM = "kimi_code_cli"
_FIVE_HOUR_WINDOW_MINUTES = 300
_WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _reset_time(detail: dict[str, Any], observed_at: datetime) -> str | None:
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return value
    for key in ("reset_in", "resetIn", "ttl", "window"):
        seconds = _finite_number(detail.get(key))
        if seconds is not None and seconds > 0:
            return (observed_at + timedelta(seconds=seconds)).isoformat()
    return None


def _used_percent(detail: Any, observed_at: datetime) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    limit = _finite_number(detail.get("limit"))
    used = _finite_number(detail.get("used"))
    if used is None:
        remaining = _finite_number(detail.get("remaining"))
        if remaining is not None and limit is not None:
            used = limit - remaining
    reset_at = _reset_time(detail, observed_at)
    if limit is None or used is None or limit <= 0 or used < 0 or reset_at is None:
        return None
    return {
        "used_percent": min(100.0, used / limit * 100.0),
        "resets_at": reset_at,
    }


def _window_minutes(row: dict[str, Any], detail: dict[str, Any]) -> float | None:
    raw_window = row.get("window")
    window = raw_window if isinstance(raw_window, dict) else {}
    duration = _finite_number(
        window.get("duration", row.get("duration", detail.get("duration")))
    )
    raw_unit = window.get("timeUnit", row.get("timeUnit", detail.get("timeUnit")))
    if duration is None or not isinstance(raw_unit, str):
        return None
    unit = raw_unit.upper()
    if "MINUTE" in unit:
        return duration
    if "HOUR" in unit:
        return duration * 60
    return None


def parse_kimi_usage(
    payload: Any,
    *,
    observed_at: datetime | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Parse official Kimi weekly and five-hour usage windows."""
    if not isinstance(payload, dict):
        raise TypeError("Kimi usage payload must be an object")
    observed_at = observed_at or _utc_now()
    if observed_at.tzinfo is None:
        raise ValueError("Kimi usage observation time must include a timezone")
    five_hour = None
    limits = payload.get("limits")
    if isinstance(limits, list):
        for row in limits:
            if not isinstance(row, dict):
                continue
            raw_detail = row.get("detail")
            detail = raw_detail if isinstance(raw_detail, dict) else row
            if _window_minutes(row, detail) == _FIVE_HOUR_WINDOW_MINUTES:
                five_hour = _used_percent(detail, observed_at)
                break
    parsed = {
        "five_hour": five_hour,
        "weekly": _used_percent(payload.get("usage"), observed_at),
    }
    if not any(parsed.values()):
        raise ValueError("Kimi usage payload contains no valid quota window")
    return parsed


def _reset_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())


def kimi_codex_rate_limit_headers(
    snapshot: dict[str, Any],
) -> list[tuple[bytes, bytes]]:
    """Map a Kimi usage snapshot to the headers Codex consumes from SSE."""
    rate_limits = snapshot.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return []

    headers: list[tuple[bytes, bytes]] = []
    for codex_name, kimi_name, window_minutes in (
        ("primary", "five_hour", _FIVE_HOUR_WINDOW_MINUTES),
        ("secondary", "weekly", _WEEKLY_WINDOW_MINUTES),
    ):
        window = rate_limits.get(kimi_name)
        if not isinstance(window, dict):
            continue
        used_percent = window.get("used_percent")
        reset_at = _reset_epoch(window.get("resets_at"))
        if (
            isinstance(used_percent, bool)
            or not isinstance(used_percent, int | float)
            or not isfinite(float(used_percent))
            or reset_at is None
        ):
            continue
        prefix = f"x-codex-{codex_name}".encode("ascii")
        headers.extend(
            [
                (prefix + b"-used-percent", str(float(used_percent)).encode("ascii")),
                (prefix + b"-window-minutes", str(window_minutes).encode("ascii")),
                (prefix + b"-reset-at", str(reset_at).encode("ascii")),
            ]
        )
    return headers


def with_kimi_usage_headers(send: Any, usage: Any) -> Any:
    """Decorate response starts from cache while refreshing usage off-path."""
    with suppress(Exception):
        usage.refresh_in_background()

    async def wrapped(message: dict[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            try:
                usage_headers = kimi_codex_rate_limit_headers(
                    usage.cached_snapshot()
                )
            except Exception:  # noqa: BLE001 - optional telemetry must not break output
                usage_headers = []
            if usage_headers:
                enriched = dict(message)
                enriched["headers"] = [*message.get("headers", []), *usage_headers]
                message = enriched
        await send(message)

    return wrapped


def _base_snapshot() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "provider": "kimi",
        "model_id": KIMI_DEFAULT_MODEL,
        "context": {
            "window_tokens": KIMI_CODEX_CONTEXT_WINDOW,
            "auto_compact_token_limit": KIMI_CODEX_AUTO_COMPACT_TOKEN_LIMIT,
        },
        "rate_limits": None,
        "refreshed_at": None,
        "stale": True,
    }


class KimiUsageService:
    """Fetch Kimi usage through existing OAuth with bounded single-flight caching.

    Usage polling intentionally resolves existing credentials only. It never starts
    the interactive Kimi login flow or any other subprocess.
    """

    def __init__(
        self,
        *,
        auth: KimiOAuthAuth | Any | None = None,
        api_base: str = KIMI_API_BASE,
        client_factory: Any | None = None,
        ttl_seconds: float = _USAGE_TTL_SECONDS,
        clock: Any = time.monotonic,
        wall_clock: Any = _utc_now,
    ) -> None:
        self._auth = auth or KimiOAuthAuth()
        self._api_base = api_base.rstrip("/")
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=_USAGE_TIMEOUT_SECONDS)
        )
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._clock = clock
        self._wall_clock = wall_clock
        self._refresh_lock = asyncio.Lock()
        self._background_task: asyncio.Task[dict[str, Any]] | None = None
        self._cached: dict[str, Any] | None = None
        self._next_refresh_at = 0.0
        self._closed = False

    async def _get(self, *, force_refresh: bool = False) -> httpx.Response:
        token = await self._auth.resolve_existing_bearer_token(
            force_refresh=force_refresh
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Msh-Platform": _KIMI_CODE_PLATFORM,
        }
        async with self._client_factory() as client:
            return await client.get(f"{self._api_base}/usages", headers=headers)

    async def _refresh(self) -> dict[str, Any]:
        response = await self._get()
        if response.status_code == 401:
            response = await self._get(force_refresh=True)
        if not 200 <= response.status_code < 300:
            raise KimiError(
                f"kimi usage upstream returned status {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise KimiError("kimi usage upstream returned invalid JSON") from exc
        observed_at = self._wall_clock()
        rate_limits = parse_kimi_usage(payload, observed_at=observed_at)
        snapshot = _base_snapshot()
        snapshot.update(
            {
                "rate_limits": rate_limits,
                "refreshed_at": observed_at.isoformat(),
                "stale": False,
            }
        )
        return snapshot

    def cached_snapshot(self) -> dict[str, Any]:
        """Return current data immediately without starting network work."""
        snapshot = dict(self._cached or _base_snapshot())
        if self._closed or (
            self._cached is not None and self._clock() >= self._next_refresh_at
        ):
            snapshot["stale"] = True
        return snapshot

    def refresh_in_background(self) -> None:
        """Schedule at most one due refresh without delaying the caller."""
        if self._closed or self._clock() < self._next_refresh_at:
            return
        task = self._background_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self.snapshot())
        self._background_task = task
        task.add_done_callback(self._finish_background_refresh)

    def _finish_background_refresh(
        self, task: asyncio.Task[dict[str, Any]]
    ) -> None:
        if self._background_task is task:
            self._background_task = None
        with suppress(asyncio.CancelledError):
            task.exception()

    async def close(self) -> None:
        """Cancel and reap an in-flight background refresh."""
        self._closed = True
        task = self._background_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._background_task is task:
            self._background_task = None

    async def snapshot(self) -> dict[str, Any]:
        """Return a fresh snapshot, or a safe stale/unavailable bounded fallback."""
        if self._closed:
            return self.cached_snapshot()
        now = self._clock()
        if now < self._next_refresh_at:
            return dict(self._cached or _base_snapshot())
        async with self._refresh_lock:
            if self._closed:
                return self.cached_snapshot()
            now = self._clock()
            if now < self._next_refresh_at:
                return dict(self._cached or _base_snapshot())
            try:
                snapshot = await self._refresh()
            except (KimiError, httpx.HTTPError, TypeError, ValueError, OSError):
                retry_delay = min(self._ttl_seconds, _FAILURE_RETRY_SECONDS)
                self._next_refresh_at = self._clock() + retry_delay
                if self._cached is None:
                    return _base_snapshot()
                stale = dict(self._cached)
                stale["stale"] = True
                self._cached = stale
                return dict(stale)
            self._cached = snapshot
            self._next_refresh_at = self._clock() + self._ttl_seconds
            return dict(snapshot)
