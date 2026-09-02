"""Persisted, app-wide preferences and small bits of "what happened last"
state -- distinct from progress.py's per-book position cache.

`last_played_in_app` and `last_played_externally` are kept as two separate
fields rather than reconciled into one on purpose: this app's own plays
never reach Audible's servers (see progress.py's module docstring), so
"most recently played" can only be answered separately for "in this app"
vs. "as far as Audible's own record shows" until real two-way sync exists
(see docs/whispersync-research.md). Collapsing them into a single value now
would just mean guessing which one to trust.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from voxcodex import config

DEFAULT_PLAYBACK_SPEED = 1.0
DEFAULT_PLAYBACK_VOLUME = 100.0
DEFAULT_LIBRARY_SORT = "recent"
DEFAULT_LIBRARY_FILTER = "all"
DEFAULT_THEME = "textual-dark"
DEFAULT_PROGRESS_DISPLAY = "percent"

# Process-wide: the app, the library screen and the player screen have each
# historically held their own Settings() over the same file, and the library
# fetch worker writes `last_played_externally` from a background thread while
# the main thread may be mid-write. One lock plus a read-modify-write per
# setter (below) keeps those from clobbering each other or truncating the
# file. The app threads a single instance through where it can; this is the
# backstop for anything that still constructs its own.
_FILE_LOCK = threading.RLock()


def _read_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


class Settings:
    def __init__(self, path: Path = config.SETTINGS_FILE) -> None:
        self._path = path
        with _FILE_LOCK:
            self._data: dict[str, Any] = _read_file(path)

    def _set(self, key: str, value: Any) -> None:
        """Read the current file, apply just this one key, write it back
        atomically -- so a stale in-memory copy of the *other* keys can never
        overwrite a change another writer made in the meantime."""
        with _FILE_LOCK:
            data = _read_file(self._path)
            data[key] = value
            config.atomic_write_text(self._path, json.dumps(data, indent=2))
            self._data = data

    def reload(self) -> None:
        with _FILE_LOCK:
            self._data = _read_file(self._path)

    @property
    def playback_speed(self) -> float:
        return float(self._data.get("playback_speed", DEFAULT_PLAYBACK_SPEED))

    def set_playback_speed(self, speed: float) -> None:
        self._set("playback_speed", float(speed))

    @property
    def playback_volume(self) -> float:
        return float(self._data.get("playback_volume", DEFAULT_PLAYBACK_VOLUME))

    def set_playback_volume(self, volume: float) -> None:
        self._set("playback_volume", float(volume))

    @property
    def last_played_in_app(self) -> tuple[str, float] | None:
        return self._last_played("last_played_in_app")

    def set_last_played_in_app(self, asin: str) -> None:
        self._set("last_played_in_app", {"asin": asin, "updated_at": time.time()})

    @property
    def last_played_externally(self) -> tuple[str, float] | None:
        return self._last_played("last_played_externally")

    def set_last_played_externally(self, asin: str, updated_at: datetime) -> None:
        self._set(
            "last_played_externally",
            {"asin": asin, "updated_at": updated_at.timestamp()},
        )

    @property
    def library_sort_key(self) -> str:
        return str(self._data.get("library_sort_key", DEFAULT_LIBRARY_SORT))

    def set_library_sort_key(self, key: str) -> None:
        self._set("library_sort_key", key)

    @property
    def library_filter_key(self) -> str:
        return str(self._data.get("library_filter_key", DEFAULT_LIBRARY_FILTER))

    def set_library_filter_key(self, key: str) -> None:
        self._set("library_filter_key", key)

    @property
    def progress_display_mode(self) -> str:
        return str(self._data.get("progress_display_mode", DEFAULT_PROGRESS_DISPLAY))

    def set_progress_display_mode(self, mode: str) -> None:
        self._set("progress_display_mode", mode)

    @property
    def theme(self) -> str:
        # Textual itself doesn't persist the command-palette theme picker's
        # choice across runs -- App.theme just resets to its class default
        # every launch unless the app saves/restores it itself.
        return str(self._data.get("theme", DEFAULT_THEME))

    def set_theme(self, theme: str) -> None:
        self._set("theme", theme)

    def _last_played(self, key: str) -> tuple[str, float] | None:
        entry = self._data.get(key)
        if not isinstance(entry, dict) or "asin" not in entry or "updated_at" not in entry:
            return None
        try:
            return str(entry["asin"]), float(entry["updated_at"])
        except (TypeError, ValueError):
            return None
