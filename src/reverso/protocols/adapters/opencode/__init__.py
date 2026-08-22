"""OpenCode Go provider support (ADR 0020, OCG-G3).

This package carries the credential, catalog and endpoint facts the OpenCode Go
verticals are built on. It deliberately contains no ``ProviderAdapter``: the
Codex and Claude verticals land in later slices, against the facts fixed here.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
