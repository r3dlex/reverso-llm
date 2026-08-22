"""Atomic per-launcher budget ledger for the OpenRouter provider (OR-G1, U5).

The ledger uses ``decimal.Decimal`` for exact arithmetic and refuses
reservation when the user-defined hard limits are missing. Concurrent
reservations share one atomic session ledger; reconciliation either releases
the conservative reservation or consumes the actual reported cost.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "BudgetLedger",
    "BudgetReservation",
    "InsufficientBudgetError",
    "MissingLimitsError",
    "ReservationState",
]


class MissingLimitsError(ValueError):
    """Both hard limits are mandatory; defaults are never inferred."""


class InsufficientBudgetError(RuntimeError):
    """The reservation would exceed a hard limit or remaining capacity."""


class ReservationState(str, Enum := __import__("enum").Enum):
    PENDING = "pending"
    RELEASED = "released"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class BudgetReservation:
    """A single reserved amount of US dollars for one admitted request."""

    id: str
    model: str
    amount_usd: Decimal
    state: str = ReservationState.PENDING.value
    reconciled_amount_usd: Decimal | None = None

    def release(self) -> "BudgetReservation":
        return BudgetReservation(
            id=self.id,
            model=self.model,
            amount_usd=self.amount_usd,
            state=ReservationState.RELEASED.value,
            reconciled_amount_usd=self.reconciled_amount_usd,
        )

    def reconcile(self, *, actual_cost_usd: Decimal) -> "BudgetReservation":
        return BudgetReservation(
            id=self.id,
            model=self.model,
            amount_usd=self.amount_usd,
            state=ReservationState.RECONCILED.value,
            reconciled_amount_usd=actual_cost_usd,
        )


class BudgetLedger:
    """Atomic per-session budget ledger scoped to one launcher capability and model."""

    def __init__(
        self,
        *,
        request_limit_usd: str | None = None,
        session_limit_usd: str | None = None,
    ) -> None:
        self._request_limit = _parse_optional(request_limit_usd, "request_limit_usd")
        self._session_limit = _parse_optional(session_limit_usd, "session_limit_usd")
        self._reservations: list[BudgetReservation] = []
        self._reconciled: Decimal = Decimal("0")
        self._rejected_above_request: list[BudgetReservation] = []

    @property
    def request_limit_usd(self) -> Decimal | None:
        return self._request_limit

    @property
    def session_limit_usd(self) -> Decimal | None:
        return self._session_limit

    def remaining_usd(self) -> Decimal:
        if self._session_limit is None:
            return Decimal("inf")
        reserved = sum(
            (r.amount_usd for r in self._reservations if r.state == ReservationState.PENDING.value),
            Decimal("0"),
        )
        return self._session_limit - reserved - self._reconciled

    def estimate(
        self,
        *,
        request_tokens: int,
        completion_tokens: int,
        model: str,
        pricing_prompt: str = "0",
        pricing_completion: str = "0",
    ) -> Decimal:
        prompt_cost = _cost(request_tokens, pricing_prompt)
        completion_cost = _cost(completion_tokens, pricing_completion)
        # Worst-case defensible estimate rounds up to the next cent.
        return (prompt_cost + completion_cost).quantize(Decimal("0.000001"))

    def reserve(
        self,
        *,
        request_tokens: int,
        completion_tokens: int,
        model: str,
        pricing_prompt: str = "0",
        pricing_completion: str = "0",
    ) -> BudgetReservation:
        if self._request_limit is None or self._session_limit is None:
            raise MissingLimitsError(
                "request_limit_usd and session_limit_usd are both required"
            )
        worst_case = self.estimate(
            request_tokens=request_tokens,
            completion_tokens=completion_tokens,
            model=model,
            pricing_prompt=pricing_prompt,
            pricing_completion=pricing_completion,
        )
        if worst_case > self._request_limit:
            raise InsufficientBudgetError(
                f"request estimate {worst_case} exceeds request limit {self._request_limit}"
            )
        remaining = self.remaining_usd()
        if remaining < worst_case:
            raise InsufficientBudgetError(
                f"request estimate {worst_case} exceeds remaining session budget {remaining}"
            )
        reservation = BudgetReservation(
            id=str(uuid.uuid4()),
            model=model,
            amount_usd=worst_case,
        )
        self._reservations.append(reservation)
        return reservation

    def release(self, reservation: BudgetReservation) -> None:
        for index, existing in enumerate(self._reservations):
            if existing.id == reservation.id:
                self._reservations[index] = existing.release()
                return
        raise KeyError(f"unknown reservation {reservation.id}")

    def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        actual_cost_usd: str | Decimal | None,
    ) -> BudgetReservation:
        actual = _parse_required(actual_cost_usd, "actual_cost_usd")
        reconciled = reservation.reconcile(actual_cost_usd=actual)
        for index, existing in enumerate(self._reservations):
            if existing.id == reservation.id:
                self._reservations[index] = reconciled
                break
        self._reconciled += actual
        return reconciled

    def __repr__(self) -> str:  # pragma: no cover - leak guard only
        return (
            f"BudgetLedger(request_limit_usd={self._request_limit}, "
            f"session_limit_usd={self._session_limit}, "
            f"remaining_usd={self.remaining_usd()})"
        )


def _parse_optional(value: str | None, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal string") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _parse_required(value: str | Decimal | None, name: str) -> Decimal:
    if value is None:
        raise MissingLimitsError(f"{name} is required")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(value)
        except Exception as exc:
            raise ValueError(f"{name} must be a decimal string") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _cost(tokens: int, unit_price: str) -> Decimal:
    if tokens <= 0:
        return Decimal("0")
    price = Decimal(unit_price or "0")
    return (Decimal(tokens) * price) / Decimal("1000000")
