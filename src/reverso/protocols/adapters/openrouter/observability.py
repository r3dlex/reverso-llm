"""Bounded OpenRouter observability (OR-G7).

Emits counters and a status snapshot. Never records secrets, capability
handles, prompts, request bodies, local paths, or usernames.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

__all__ = [
    "OpenRouterMetrics",
    "StatusSnapshot",
    "snapshot_status",
]


_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]+"),
    re.compile(r"Bearer\s+sk-or-v1-[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"\bcap_[A-Za-z0-9_\-]{16,}\b"),
)


@dataclass
class OpenRouterMetrics:
    """Bounded counters for the OpenRouter runtime."""

    admissions: Counter[str] = field(default_factory=Counter)
    denials: Counter[str] = field(default_factory=Counter)
    granted_capabilities: int = 0
    revoked_capabilities: int = 0
    completed_streams: int = 0
    rejected_streams: int = 0

    def record_admission(
        self,
        *,
        surface: str,
        model: str,
        request_id: str = "",
        reported_cost_usd: float = 0.0,
    ) -> None:
        self.admissions[f"{surface}:{model}"] += 1

    def record_denial(self, *, reason: str) -> None:
        self.denials[reason] += 1

    def record_grant(self) -> None:
        self.granted_capabilities += 1

    def record_revoke(self) -> None:
        self.revoked_capabilities += 1

    def record_completed_stream(self) -> None:
        self.completed_streams += 1

    def record_rejected_stream(self) -> None:
        self.rejected_streams += 1

    def export(self) -> dict[str, object]:
        return {
            "admissions": dict(self.admissions),
            "denials": dict(self.denials),
            "granted_capabilities": self.granted_capabilities,
            "revoked_capabilities": self.revoked_capabilities,
            "completed_streams": self.completed_streams,
            "rejected_streams": self.rejected_streams,
        }

    def __repr__(self) -> str:  # pragma: no cover - leak guard only
        return f"OpenRouterMetrics(export_keys={list(self.export().keys())})"


@dataclass(frozen=True)
class StatusSnapshot:
    """A bounded status shape safe to expose through diagnostics."""

    admissions_total: int
    denials_total: int
    granted_capabilities: int
    revoked_capabilities: int
    completed_streams: int
    rejected_streams: int
    last_request_id: str


def snapshot_status(
    metrics: OpenRouterMetrics, *, last_request_id: str = ""
) -> StatusSnapshot:
    """Build a deterministic status snapshot."""
    return StatusSnapshot(
        admissions_total=sum(metrics.admissions.values()),
        denials_total=sum(metrics.denials.values()),
        granted_capabilities=metrics.granted_capabilities,
        revoked_capabilities=metrics.revoked_capabilities,
        completed_streams=metrics.completed_streams,
        rejected_streams=metrics.rejected_streams,
        last_request_id=last_request_id,
    )
