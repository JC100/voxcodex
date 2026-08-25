"""Listening-position tracking.

Audible exposes a confirmed *read* endpoint for last-listened positions
(`GET 1.0/annotations/lastpositions`, cross-checked against the `audible`
package, `audible-cli`, and the community `audible.cr` API reference), but
none of those sources document any endpoint the official apps use to *write*
a position back. Rather than guess at an unverified write call and risk it
silently doing nothing (or something wrong), this app:

  - reads the real position from Audible when available, and
  - always keeps its own local cache of where you left off in *this* app,
    which is what drives the resume point here regardless of whether the
    remote read succeeds.

So "sync" here is one-directional (Audible -> audible-tui). If you also use
the official app, its plays will still be reflected next time this app reads
that endpoint -- this app just can't push its own plays back to Audible.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from audible_tui import config
from audible_tui.services.api import AudibleAPI

logger = logging.getLogger(__name__)


class ProgressStore:
    def __init__(self, path: Path = config.PROGRESS_CACHE_FILE) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self) -> None:
        config.ensure_dirs()
        self._path.write_text(json.dumps(self._data, indent=2))

    def get_position_ms(self, asin: str) -> int:
        return int(self._data.get(asin, {}).get("position_ms", 0))

    def set_position_ms(self, asin: str, position_ms: int, duration_ms: int = 0) -> None:
        entry = self._data.setdefault(asin, {})
        entry["position_ms"] = int(position_ms)
        if duration_ms:
            entry["duration_ms"] = int(duration_ms)
        entry["updated_at"] = time.time()
        self.save()


def fetch_remote_positions(api: AudibleAPI, asins: list[str]) -> dict[str, int]:
    """Best-effort bulk read of Audible's own last-heard positions.

    Returns an asin -> position_ms map. Returns an empty map (never raises)
    if the call fails or the response doesn't match any shape we recognize,
    since this endpoint's exact response schema isn't documented anywhere
    we could confirm -- callers should treat this purely as an enhancement
    over the local cache, not a dependency.
    """
    if not asins:
        return {}
    try:
        resp = api.client.get("annotations/lastpositions", asins=",".join(asins))
    except Exception:
        logger.debug("lastpositions fetch failed", exc_info=True)
        return {}

    positions: dict[str, int] = {}
    try:
        records: Any = resp
        if isinstance(resp, dict):
            records = (
                resp.get("asin_positions")
                or resp.get("positions")
                or resp.get("lastPositions")
                or resp
            )
        if isinstance(records, dict):
            for asin, value in records.items():
                ms = _extract_position_ms(value)
                if ms is not None:
                    positions[asin] = ms
        elif isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                asin = record.get("asin")
                ms = _extract_position_ms(record)
                if asin and ms is not None:
                    positions[asin] = ms
    except Exception:
        logger.debug("lastpositions response shape unrecognized", exc_info=True)
        return {}
    return positions


def _extract_position_ms(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, dict):
        for key in ("position_ms", "positionMs", "lastPositionMs", "position"):
            if key in value:
                try:
                    return int(value[key])
                except (TypeError, ValueError):
                    return None
    return None
