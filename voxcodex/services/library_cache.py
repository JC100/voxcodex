"""Offline fallback for the library listing.

Browsing, and playing anything already downloaded, shouldn't hard-require a
live connection to Audible every time this app starts -- a flaky connection
or an Audible outage shouldn't lock you out of books that are already sitting
on disk. This caches the last successfully fetched library to disk so
`LibraryScreen` can fall back to it (see `screens/library.py`) when a fresh
fetch fails, rather than just showing an empty screen and an error.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict

from voxcodex import config
from voxcodex.models import Book

logger = logging.getLogger(__name__)


def save(books: list[Book]) -> None:
    """Best-effort write; a failure here shouldn't interrupt a successful
    live fetch, so it's logged rather than raised."""
    config.ensure_dirs()
    data = {"cached_at": time.time(), "books": [asdict(book) for book in books]}
    try:
        config.LIBRARY_CACHE_FILE.write_text(json.dumps(data))
    except OSError:
        logger.debug("failed to write library cache", exc_info=True)


def load() -> tuple[list[Book], float] | None:
    """The last successfully cached (books, cached_at) pair, or None if
    there's no cache yet, it's corrupt, or its shape doesn't match the
    current `Book` fields (e.g. after an app update) -- never raises,
    since this is only ever a fallback for when the real fetch failed."""
    if not config.LIBRARY_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(config.LIBRARY_CACHE_FILE.read_text())
        books = [Book(**item) for item in data["books"]]
        cached_at = float(data["cached_at"])
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        logger.debug("failed to read library cache", exc_info=True)
        return None
    return books, cached_at
