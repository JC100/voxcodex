"""Disk cache of chapter lists, keyed by ASIN.

A book's chapters never change, so once fetched they're reusable in every
future session -- unlike the chapter *cursor* (`chapter_current`), which
tracks playback position and is always recomputed locally rather than
cached. This is what lets `LibraryScreen` show correct chapter totals
instantly, without re-fetching from the API every session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from voxcodex import config
from voxcodex.services.api import Chapter

logger = logging.getLogger(__name__)


def save(chapters_by_asin: dict[str, list[Chapter]]) -> None:
    """Best-effort write; a failure here shouldn't interrupt playback or
    browsing, so it's logged rather than raised."""
    # dict(...) snapshots in one atomic step before iterating -- the caller
    # (LibraryScreen) passes its own live self._chapter_cache, which a
    # background worker (_fetch_chapter_counts) can be extending on another
    # thread at the same moment this iterates it, raising "dictionary
    # changed size during iteration" -- not an OSError, so save()'s own
    # except wouldn't have caught it (L19).
    data = {
        asin: [asdict(chapter) for chapter in chapters]
        for asin, chapters in dict(chapters_by_asin).items()
    }
    try:
        config.atomic_write_text(config.CHAPTER_CACHE_FILE, json.dumps(data))
    except OSError:
        logger.debug("failed to write chapter cache", exc_info=True)


def load() -> dict[str, list[Chapter]]:
    """The cached chapter lists, or an empty dict if there's no cache yet,
    it's corrupt, or its shape doesn't match `Chapter`'s current fields --
    never raises, since a miss just means "fetch it fresh" upstream."""
    if not config.CHAPTER_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(config.CHAPTER_CACHE_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        return {
            asin: [Chapter(**item) for item in chapters]
            for asin, chapters in data.items()
        }
    except (json.JSONDecodeError, OSError, AttributeError, KeyError, TypeError, ValueError):
        logger.debug("failed to read chapter cache", exc_info=True)
        return {}
