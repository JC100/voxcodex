"""Listening-position tracking.

Audible exposes a confirmed *read* endpoint for last-listened positions
(`GET 1.0/annotations/lastpositions`), and this app also *writes* a position
back via `AudibleAPI.push_last_position` (`PUT 1.0/lastpositions/{asin}`) --
see docs/whispersync-research.md for how the write path was found and verified
against a live account, including an earlier attempt that looked like a dead
end but turned out to just be using the wrong identifiers. So:

  - reads the real position from Audible when available,
  - pushes this app's own final position back after a playback session, so
    other devices/the official app pick up where this app left off, and
  - always keeps its own local cache of where you left off in *this* app,
    which is what drives the resume point here regardless of whether either
    remote call succeeds.

Sync is therefore two-directional, but the push is best-effort: it needs the
real per-content `acr` from a `get_license()` response for that title (see
`push_position` below), and network/API failures there are swallowed the same
way remote-read failures are -- this app's own resume point must never depend
on Audible's sync working.

The read response shape below (`asin_last_position_heard_annots`, a list of
per-asin records each with a nested `last_position_heard` dict) is confirmed
directly against a live account, not guessed -- an earlier version of this
module guessed at several plausible-looking shapes none of which were the
real one, so the remote-read path silently returned nothing for every real
response since this app's first commit. The bulk-loaded library table still
looked reasonable throughout because it separately falls back to the
library API's own `percent_complete` field, which masked the bug -- but the
more precise resume-to-the-exact-second value this was meant to provide
never actually worked until this fix.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from voxcodex import config
from voxcodex.services.api import AudibleAPI, parse_audible_timestamp

logger = logging.getLogger(__name__)

_FILE_LOCK = threading.RLock()


class ProgressStore:
    def __init__(self, path: Path | None = None) -> None:
        # A `Path = config.PROGRESS_CACHE_FILE` default is evaluated once,
        # at import time -- resolving it here instead means a test that
        # monkeypatches `config.PROGRESS_CACHE_FILE` before constructing a
        # ProgressStore() actually takes effect, rather than needing to
        # monkeypatch the class itself as a workaround.
        self._path = path if path is not None else config.PROGRESS_CACHE_FILE
        with _FILE_LOCK:
            self._data: dict[str, dict[str, Any]] = self._read_file()

    def _read_file(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_position_ms(self, asin: str) -> int:
        entry = self._data.get(asin)
        if not isinstance(entry, dict):
            # A valid-JSON, wrong-shape entry (e.g. {"B001": 5000}) --
            # called once per book on every library load, so one bad entry
            # must not take down the whole load (M5).
            return 0
        try:
            return int(entry.get("position_ms", 0))
        except (TypeError, ValueError):
            return 0

    def get_updated_at(self, asin: str) -> float | None:
        """Unix timestamp of the last local write for `asin`, or None if
        there isn't one -- lets a caller compare recency against Audible's
        own `last_updated` for the same title (see
        `positions_with_updated_at_from_annotations`)."""
        entry = self._data.get(asin)
        if not isinstance(entry, dict):
            return None
        value = entry.get("updated_at")
        return float(value) if isinstance(value, (int, float)) else None

    def set_position_ms(self, asin: str, position_ms: int, duration_ms: int = 0) -> None:
        # Reload-modify-write atomically: the player screen checkpoints
        # position on a timer as well as on close, so writes land often and
        # must not truncate the file or drop another title's entry.
        with _FILE_LOCK:
            data = self._read_file()
            entry = data.get(asin)
            if not isinstance(entry, dict):
                # Replace rather than mutate -- setdefault would return the
                # existing non-dict value as-is, and item assignment on it
                # (below) would raise TypeError.
                entry = {}
                data[asin] = entry
            entry["position_ms"] = int(position_ms)
            if duration_ms:
                entry["duration_ms"] = int(duration_ms)
            entry["updated_at"] = time.time()
            config.atomic_write_text(self._path, json.dumps(data, indent=2))
            self._data = data


# asins=... is one query-string parameter -- at ~11 bytes/ASIN, an
# unchunked request for a several-hundred-title library is plausibly past a
# gateway's request-line limit (M2). 100 per request keeps that comfortably
# small while still batching most libraries into one or two round trips.
_ANNOTATIONS_CHUNK_SIZE = 100


def fetch_remote_annotations(api: AudibleAPI, asins: list[str]) -> list[dict[str, Any]]:
    """Best-effort raw fetch of Audible's own last-heard annotations.

    Returns the list of per-asin records (never raises, never None) --
    empty if there are no asins to ask about, every chunked request fails,
    or none of the responses match the confirmed shape. Kept separate from
    parsing so a single fetch can feed more than one derived view
    (positions, most-recently-played) without a second round trip.
    """
    records: list[dict[str, Any]] = []
    for i in range(0, len(asins), _ANNOTATIONS_CHUNK_SIZE):
        chunk = asins[i : i + _ANNOTATIONS_CHUNK_SIZE]
        try:
            # See api.py's get_library for why this goes through a
            # dict[str, Any] rather than a plain kwarg -- audible.Client.
            # get's **kwargs stub.
            params: dict[str, Any] = {"asins": ",".join(chunk)}
            resp = api.client.get("annotations/lastpositions", **params)
            chunk_records = (
                resp.get("asin_last_position_heard_annots")
                if isinstance(resp, dict) else None
            )
            if isinstance(chunk_records, list):
                records.extend(chunk_records)
        except Exception:
            logger.warning(
                "lastpositions fetch failed for a chunk of %d asins", len(chunk),
                exc_info=True,
            )
    return records


def positions_with_updated_at_from_annotations(
    records: list[dict[str, Any]],
) -> dict[str, tuple[int, float]]:
    """asin -> (position_ms, updated_at as a Unix timestamp), for every
    record with both a recorded position and a parseable timestamp.

    The timestamp is what lets `LibraryScreen` resolve a local vs. remote
    position by *recency* rather than by magnitude -- a plain position_ms
    can only ever grow, which gets a book started over elsewhere stuck
    showing (and resuming at) the old, higher position forever.
    """
    result: dict[str, tuple[int, float]] = {}
    for record in records:
        existing = _existing_last_position_heard(record)
        if existing is None:
            continue
        asin, lph = existing
        updated_at = parse_audible_timestamp(lph.get("last_updated"))
        if updated_at is None:
            continue
        try:
            position_ms = int(lph.get("position_ms", 0))
        except (TypeError, ValueError):
            continue
        result[asin] = (position_ms, updated_at.timestamp())
    return result


def _existing_last_position_heard(
    record: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Pulls (asin, last_position_heard) out of one annotation record, but
    only if it actually has a recorded position -- Audible returns a record
    with status "DoesNotExist" (no `position_ms`/`last_updated` at all) for
    titles that have never been played anywhere, which is not an error, just
    nothing to report.

    A single Optional return (rather than a `(None, None)` sentinel pair)
    is what lets callers narrow both elements together with one `is None`
    check, instead of asin's check leaving `lph` statically `dict | None`.
    """
    if not isinstance(record, dict):
        return None
    asin = record.get("asin")
    lph = record.get("last_position_heard")
    if not asin or not isinstance(lph, dict) or lph.get("status") != "Exists":
        return None
    return asin, lph


def push_position(api: AudibleAPI, asin: str, acr: str, position_ms: int) -> bool:
    """Best-effort push of this app's position back to Audible's sync store.

    Returns whether it actually went through -- never raises, and callers
    should treat a False the same as if this were never called: this app's
    own local resume point (`ProgressStore`) must not depend on it. Silently
    does nothing (returns False) if `acr` is missing, e.g. a voucher saved
    before this feature existed -- there's no ASIN-only fallback because a
    guessed/placeholder identifier here doesn't fail loudly, it just writes a
    record nothing ever reads (see `AudibleAPI.push_last_position`).
    """
    if not acr:
        logger.debug("push_position: no acr for %s, not pushing", asin)
        return False
    try:
        api.push_last_position(asin, acr, position_ms)
        return True
    except Exception:
        logger.debug("push_position failed for %s", asin, exc_info=True)
        return False


def push_finished(api: AudibleAPI, asin: str, finished: bool) -> bool:
    """Best-effort push of a title's finished state back to Audible.

    Returns whether it went through -- never raises. Like `push_position`,
    this is a sync enhancement, not something VoxCodex's own view depends on:
    the local library's finished flag stands on its own whether or not this
    call succeeds. Unlike the position push it needs no per-content
    identifiers, just the asin (see `AudibleAPI.set_finished`).
    """
    try:
        api.set_finished(asin, finished)
        return True
    except Exception:
        logger.debug("push_finished failed for %s", asin, exc_info=True)
        return False


def push_listening_session(
    api: AudibleAPI,
    asin: str,
    license_id: str,
    start_position_ms: int,
    end_position_ms: int,
    start_time: datetime,
    end_time: datetime,
    length_of_book_ms: int,
    narration_speed: float,
    delivery_type: str,
) -> bool:
    """Best-effort report of one playback session back to Audible, so the
    official app/website's library tile shows the right mid-book progress.

    Returns whether it went through -- never raises. Like `push_position`
    and `push_finished`, this is a sync enhancement: VoxCodex's own resume
    point and library view don't depend on it (see
    `AudibleAPI.push_listening_session`).
    """
    try:
        api.push_listening_session(
            asin,
            license_id,
            start_position_ms,
            end_position_ms,
            start_time,
            end_time,
            length_of_book_ms,
            narration_speed,
            delivery_type,
        )
        return True
    except Exception:
        logger.debug("push_listening_session failed for %s", asin, exc_info=True)
        return False
