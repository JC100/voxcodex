"""Listening-position tracking.

Audible exposes a confirmed *read* endpoint for last-listened positions
(`GET 1.0/annotations/lastpositions`), and this app can also *write* a
position back via `AudibleAPI.push_last_heard` (an older Whispersync
endpoint, not the modern api.audible.<domain> one -- see
docs/whispersync-research.md for how that was found and verified against a
live account, including an earlier attempt that looked like a dead end but
turned out to just be using the wrong identifiers). So:

  - reads the real position from Audible when available,
  - pushes this app's own final position back after a playback session, so
    other devices/the official app pick up where this app left off, and
  - always keeps its own local cache of where you left off in *this* app,
    which is what drives the resume point here regardless of whether either
    remote call succeeds.

Sync is therefore two-directional, but the push is best-effort: it needs the
real per-content `acr`/`content_version` from a `get_license()` response for
that title (see `push_position` below), and network/API failures there are
swallowed the same way remote-read failures are -- this app's own resume
point must never depend on Audible's sync working.

The read response shape below (`asin_last_position_heard_annots`, a list of
per-asin records each with a nested `last_position_heard` dict) is confirmed
directly against a live account, not guessed -- an earlier version of this
module guessed at several plausible-looking shapes none of which were the
real one, so `fetch_remote_positions` silently returned {} for every real
response since this app's first commit. The bulk-loaded library table still
looked reasonable throughout because it separately falls back to the
library API's own `percent_complete` field, which masked the bug -- but the
more precise resume-to-the-exact-second value this was meant to provide
never actually worked until this fix.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from voxcodex import config
from voxcodex.services.api import AudibleAPI

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


def fetch_remote_annotations(api: AudibleAPI, asins: list[str]) -> list[dict[str, Any]]:
    """Best-effort raw fetch of Audible's own last-heard annotations.

    Returns the list of per-asin records (never raises, never None) --
    empty if there are no asins to ask about, the call fails, or the
    response doesn't match the confirmed shape. Kept separate from parsing
    so a single fetch can feed more than one derived view (positions,
    most-recently-played) without a second round trip.
    """
    if not asins:
        return []
    try:
        resp = api.client.get("annotations/lastpositions", asins=",".join(asins))
        records = resp.get("asin_last_position_heard_annots") if isinstance(resp, dict) else None
        return records if isinstance(records, list) else []
    except Exception:
        logger.debug("lastpositions fetch failed", exc_info=True)
        return []


def positions_from_annotations(records: list[dict[str, Any]]) -> dict[str, int]:
    """asin -> position_ms for every record with an actual recorded position."""
    positions: dict[str, int] = {}
    for record in records:
        asin, lph = _existing_last_position_heard(record)
        if asin is None:
            continue
        try:
            positions[asin] = int(lph.get("position_ms", 0))
        except (TypeError, ValueError):
            continue
    return positions


def most_recent_external_play(
    records: list[dict[str, Any]],
) -> tuple[str, datetime] | None:
    """Of these records, the asin Audible most recently recorded a position
    for -- i.e. the book you most recently played somewhere other than this
    app (this app's own plays don't reach this endpoint; see the module
    docstring). Returns (asin, updated_at) for the newest one, or None if no
    record has both an existing position and a parseable timestamp.
    """
    best: tuple[str, datetime] | None = None
    for record in records:
        asin, lph = _existing_last_position_heard(record)
        if asin is None:
            continue
        raw_updated = lph.get("last_updated")
        if not raw_updated:
            continue
        try:
            updated_at = datetime.strptime(raw_updated, "%Y-%m-%d %H:%M:%S.%f").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if best is None or updated_at > best[1]:
            best = (asin, updated_at)
    return best


def _existing_last_position_heard(
    record: Any,
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Pulls (asin, last_position_heard) out of one annotation record, but
    only if it actually has a recorded position -- Audible returns a record
    with status "DoesNotExist" (no `position_ms`/`last_updated` at all) for
    titles that have never been played anywhere, which is not an error, just
    nothing to report.
    """
    if not isinstance(record, dict):
        return None, None
    asin = record.get("asin")
    lph = record.get("last_position_heard")
    if not asin or not isinstance(lph, dict) or lph.get("status") != "Exists":
        return None, None
    return asin, lph


def fetch_remote_positions(api: AudibleAPI, asins: list[str]) -> dict[str, int]:
    """Best-effort bulk read of Audible's own last-heard positions.

    Returns an asin -> position_ms map, or an empty map if the call fails or
    no title has a recorded position -- callers should treat this purely as
    an enhancement over the local cache, not a dependency.
    """
    return positions_from_annotations(fetch_remote_annotations(api, asins))


def push_position(
    api: AudibleAPI, asin: str, acr: str, content_version: str, codec: str, position_ms: int
) -> bool:
    """Best-effort push of this app's position back to Audible's sync store.

    Returns whether it actually went through -- never raises, and callers
    should treat a False the same as if this were never called: this app's
    own local resume point (`ProgressStore`) must not depend on it. Silently
    does nothing (returns False) if `acr`/`content_version` are missing, e.g.
    a voucher saved before this feature existed -- there's no ASIN-only
    fallback because a guessed/placeholder identifier here doesn't fail
    loudly, it just writes a record nothing ever reads (see
    `AudibleAPI.push_last_heard`).
    """
    if not acr or not content_version:
        logger.debug(
            "push_position: no acr/content_version for %s, not pushing", asin
        )
        return False
    try:
        api.push_last_heard(asin, acr, content_version, codec, position_ms)
        return True
    except Exception:
        logger.debug("push_position failed for %s", asin, exc_info=True)
        return False
