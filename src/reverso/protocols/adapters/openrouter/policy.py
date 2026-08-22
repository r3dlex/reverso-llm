"""Fresh policy/pricing evaluation for the OpenRouter provider (OR-G1, I1).

The policy evaluator guarantees:

* cached or last-known-good metadata is discovery-only and cannot authorize
  inference;
* every admission requires a fresh snapshot with an immutable revision and a
  bounded age;
* the evaluator blocks dispatch when the freshness bound is exceeded and
  surfaces a ``FreshnessBoundError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = [
    "FreshnessBoundError",
    "OpenRouterPolicy",
    "PolicyEvaluator",
    "PolicyRejectionError",
    "UnknownPolicyError",
]


class PolicyRejectionError(RuntimeError):
    """A policy decision refused the request."""


class FreshnessBoundError(PolicyRejectionError):
    """The policy evidence exceeded the freshness bound."""


class UnknownPolicyError(PolicyRejectionError):
    """The policy evidence is missing or unknown."""


@dataclass(frozen=True)
class OpenRouterPolicy:
    """A fresh, endpoint-specific policy/pricing snapshot for OpenRouter."""

    revision: str
    observed_at: float
    requires_zdr: bool
    denies_data_collection: bool
    pricing_prompt: str = "0"
    pricing_completion: str = "0"


class PolicyEvaluator:
    """Validate freshness, privacy, and pricing for a single admission."""

    def __init__(
        self,
        *,
        policy: OpenRouterPolicy,
        freshness_bound_seconds: int,
        clock: Callable[[], float],
        compatibility_providers: Iterable[str] = (),
    ) -> None:
        self._policy = policy
        self._freshness_bound_seconds = freshness_bound_seconds
        self._clock = clock
        self._compatibility_providers = tuple(compatibility_providers)

    @property
    def policy(self) -> OpenRouterPolicy:
        return self._policy

    @property
    def compatibility_providers(self) -> tuple[str, ...]:
        return self._compatibility_providers

    def assert_fresh(self) -> None:
        if not self._policy.revision:
            raise UnknownPolicyError("policy revision is empty")
        age = self._clock() - self._policy.observed_at
        if age < 0:
            raise FreshnessBoundError(
                f"policy revision {self._policy.revision} observed in the future"
            )
        if age > self._freshness_bound_seconds:
            raise FreshnessBoundError(
                f"policy revision {self._policy.revision} is {age:.0f}s old, "
                f"bound is {self._freshness_bound_seconds}s"
            )

    def require_zdr(self) -> None:
        if not self._policy.requires_zdr:
            raise PolicyRejectionError(
                f"policy revision {self._policy.revision} does not require ZDR"
            )

    def require_data_denial(self) -> None:
        if not self._policy.denies_data_collection:
            raise PolicyRejectionError(
                f"policy revision {self._policy.revision} does not deny data collection"
            )

    def assert_endpoint_compatible(self, provider: str) -> None:
        if not self._compatibility_providers:
            raise UnknownPolicyError(
                "no compatible endpoint providers recorded for this evidence"
            )
        if provider not in self._compatibility_providers:
            raise PolicyRejectionError(
                f"endpoint provider {provider} is not covered by evidence"
            )
