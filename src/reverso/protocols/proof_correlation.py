"""Ephemeral request correlation for trusted-machine live proof traffic."""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

PROOF_HEADER = b"x-reverso-kimi-proof"
_NONCE_RE = re.compile(r"^[a-f0-9]{64}$")
_MAX_NONCES = 128


def proof_nonce_from_headers(headers: Iterable[Any]) -> str | None:
    """Return a validated proof nonce without accepting ambiguous duplicates."""
    values: list[str] = []
    for row in headers:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        key, value = row
        if isinstance(key, bytes) and key.lower() == PROOF_HEADER:
            try:
                values.append(
                    value.decode("ascii") if isinstance(value, bytes) else str(value)
                )
            except UnicodeDecodeError:
                return None
    if len(values) != 1 or _NONCE_RE.fullmatch(values[0]) is None:
        return None
    return values[0]


class ProofCorrelationStore:
    """Bounded process-local counts keyed by unguessable per-lane nonces."""

    def __init__(self) -> None:
        self._counts: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, nonce: str | None, lane: str) -> None:
        if nonce is None or _NONCE_RE.fullmatch(nonce) is None:
            return
        if lane not in {"responses", "messages"}:
            return
        with self._lock:
            counts = self._counts.setdefault(nonce, {"responses": 0, "messages": 0})
            counts[lane] += 1
            self._counts.move_to_end(nonce)
            while len(self._counts) > _MAX_NONCES:
                self._counts.popitem(last=False)

    def consume(self, nonce: str) -> dict[str, int] | None:
        if _NONCE_RE.fullmatch(nonce) is None:
            return None
        with self._lock:
            counts = self._counts.pop(nonce, None)
        return dict(counts) if counts is not None else {"responses": 0, "messages": 0}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


DEFAULT_PROOF_CORRELATION = ProofCorrelationStore()
