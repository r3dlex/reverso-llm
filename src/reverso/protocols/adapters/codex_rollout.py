"""Read 5h/weekly quota from the Codex session rollout file (Slice 5).

``codex exec --json`` deterministically writes a session rollout ``.jsonl``
under ``$CODEX_HOME/sessions/<Y>/<M>/<D>/rollout-*.jsonl`` when each turn
completes.  The file carries an ``event_msg`` record whose
``payload.type == "token_count"`` includes a ``payload.rate_limits`` block
with the 5h (primary, window_minutes:300) and weekly (secondary,
window_minutes:10080) quota state.

This module reads that file AFTER the codex turn is done - it does NOT spawn
codex and never modifies any files (INV-2).

Contract notes (from spike S1):
  - ``primary``   maps to ``five_hour``  (window_minutes == 300).
  - ``secondary`` maps to ``weekly``     (window_minutes == 10080).
  - Map by ``window_minutes`` value, NOT by key name, so a future codex build
    that reorders primary/secondary still routes correctly.
  - ``resets_at`` is a Unix epoch integer in the rollout; the contract and HUD
    want an ISO-8601 UTC string ending in ``Z``.  Bad/missing values → None.
  - A missing or malformed ``rate_limits`` block → return None (keep-last
    semantics at the call site; caller does NOT clear the stored snapshot).
  - Multiple ``token_count`` records may appear in one session file; take the
    LAST one.

Correlation:
  The ``thread_id`` from the ``thread.started`` stdout event appears verbatim
  in the rollout filename pattern ``rollout-<ISO>-<thread_id-prefix>-.jsonl``.
  When ``thread_id`` is supplied:
    - Search for all filenames that contain the thread_id substring.
    - If NONE match → return None immediately (keep-last / n/a). Do NOT fall
      back to the newest unrelated file - that would leak a different session's
      quota (cross-session / cross-account data hazard, fix #2).
    - If MULTIPLE match → pick the newest by mtime.
  When ``thread_id`` is None → use the newest rollout file across all
  session subdirectories (capped to the newest ``_MAX_CANDIDATES`` files so
  the rglob+stat scan is bounded, fix #3).

I/O safety:
  - Rollout files are line-delimited JSON; read line-by-line via open() so the
    full file is never loaded into memory at once (fix #3).
  - Candidate set is capped at ``_MAX_CANDIDATES`` (20) newest files to bound
    the stat overhead when the sessions dir accumulates many runs.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum number of rollout files to stat when doing the newest-file fallback.
# Bounds the rglob+stat scan on busy machines with many historical sessions.
_MAX_CANDIDATES = 20


def _codex_home() -> Path:
    """Resolve ``CODEX_HOME``, defaulting to ``~/.codex``."""
    raw = os.environ.get("CODEX_HOME", "")
    if raw:
        return Path(raw)
    return Path.home() / ".codex"


def _sessions_root(codex_home: Path) -> Path:
    return codex_home / "sessions"


def _newest_rollout_files(sessions_root: Path, n: int = _MAX_CANDIDATES) -> list[Path]:
    """Return up to ``n`` rollout-*.jsonl files under sessions/, newest first.

    Bounds the candidate set to avoid unbounded stat overhead on large histories.
    """
    if not sessions_root.is_dir():
        return []
    files = sorted(
        sessions_root.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:n]


def _find_rollout_for_thread(sessions_root: Path, thread_id: str) -> Path | None:
    """Find the best rollout file whose filename contains ``thread_id``.

    Returns the newest matching file by mtime when multiple filenames match, or
    None when no filename contains the thread_id substring.  Does NOT fall back
    to unrelated files - that would leak a different session's quota.
    """
    if not sessions_root.is_dir():
        return None
    matches = [f for f in sessions_root.rglob("rollout-*.jsonl") if thread_id in f.name]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Multiple matches: pick newest by mtime.
    return max(matches, key=lambda p: p.stat().st_mtime)


def _epoch_to_iso(resets_at: Any) -> str | None:
    """Convert a Unix epoch int/float to ISO-8601 UTC string ending in ``Z``.

    Returns None on bad/missing input so callers can omit the field rather than
    surfacing a non-ISO str like "None" or "N/A".
    """
    if resets_at is None:
        return None
    try:
        ts = float(resets_at)
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _last_token_count_rate_limits(path: Path) -> dict[str, Any] | None:
    """Parse ``path`` line-by-line and return the last ``token_count``
    rate_limits block, or None.

    Reads line-by-line via open() to avoid loading the whole file into memory
    (fix #3: I/O bounding).
    """
    last_rate_limits: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "token_count":
                    continue
                rl = payload.get("rate_limits")
                if isinstance(rl, dict):
                    last_rate_limits = rl
    except OSError as exc:
        logger.debug("codex rollout read failed %s: %s", path, exc)
        return None

    return last_rate_limits


def _map_rate_limits(raw_rl: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw ``rate_limits`` block to the contract shape.

    Maps by ``window_minutes`` value (300→five_hour, 10080→weekly) so key
    order changes in future codex versions do not break routing.
    Returns None if neither window can be identified.
    ``resets_at`` is converted to ISO-8601 Z format; None when unconvertible.
    """
    five_hour: dict[str, Any] | None = None
    weekly: dict[str, Any] | None = None

    for key in ("primary", "secondary"):
        window = raw_rl.get(key)
        if not isinstance(window, dict):
            continue
        minutes = window.get("window_minutes")
        used_pct = window.get("used_percent")
        resets_at_raw = window.get("resets_at")
        if used_pct is None:
            continue
        entry: dict[str, Any] = {
            "used_percent": float(used_pct),
        }
        iso = _epoch_to_iso(resets_at_raw)
        if iso is not None:
            entry["resets_at"] = iso
        if minutes == 300:
            five_hour = entry
        elif minutes == 10080:
            weekly = entry

    if five_hour is None and weekly is None:
        return None

    plan_type = raw_rl.get("plan_type")
    return {
        "five_hour": five_hour,
        "weekly": weekly,
        "plan_type": plan_type,
    }


def read_rate_limits(
    thread_id: str | None = None,
    *,
    codex_home: Path | None = None,
) -> dict[str, Any] | None:
    """Read the 5h/weekly rate_limits from the Codex session rollout file.

    Returns the contract-shaped rate_limits dict, or None when the rollout
    cannot be found, parsed, or does not contain a ``token_count`` record with
    a ``rate_limits`` block.

    ``thread_id`` - the ``thread.started`` thread_id from the codex stdout.
    When supplied and no filename matches, returns None immediately (no
    cross-session fallback - fix #2).  When None, uses the newest rollout file
    (capped to ``_MAX_CANDIDATES`` - fix #3).
    ``codex_home`` - injectable for tests; defaults to ``_codex_home()``.

    This function is SYNCHRONOUS and intended to be called via
    ``asyncio.to_thread()`` from the ASGI loop (fix #3).
    """
    home = codex_home if codex_home is not None else _codex_home()
    sessions = _sessions_root(home)

    rollout: Path | None = None
    if thread_id:
        rollout = _find_rollout_for_thread(sessions, thread_id)
        if rollout is None:
            # No file matched the thread_id - return None (keep-last at caller).
            # Do NOT fall back to the newest unrelated file.
            logger.debug(
                "codex rollout: no file matched thread_id=%s; returning None (keep-last)",
                thread_id,
            )
            return None
    else:
        # thread_id not known: use newest file across all sessions (bounded).
        candidates = _newest_rollout_files(sessions)
        if not candidates:
            logger.debug("codex rollout: no rollout files found under %s", sessions)
            return None
        rollout = candidates[0]

    raw_rl = _last_token_count_rate_limits(rollout)
    if raw_rl is None:
        logger.debug("codex rollout: no token_count.rate_limits in %s", rollout)
        return None

    mapped = _map_rate_limits(raw_rl)
    if mapped is None:
        logger.debug(
            "codex rollout: rate_limits in %s had no recognisable windows", rollout
        )
    return mapped
