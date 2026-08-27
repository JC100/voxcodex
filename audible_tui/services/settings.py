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
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from audible_tui import config

DEFAULT_PLAYBACK_SPEED = 1.0
DEFAULT_PLAYBACK_VOLUME = 100.0


class Settings:
    def __init__(self, path: Path = config.SETTINGS_FILE) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
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

    @property
    def playback_speed(self) -> float:
        return float(self._data.get("playback_speed", DEFAULT_PLAYBACK_SPEED))

    def set_playback_speed(self, speed: float) -> None:
        self._data["playback_speed"] = float(speed)
        self.save()

    @property
    def playback_volume(self) -> float:
        return float(self._data.get("playback_volume", DEFAULT_PLAYBACK_VOLUME))

    def set_playback_volume(self, volume: float) -> None:
        self._data["playback_volume"] = float(volume)
        self.save()

    @property
    def last_played_in_app(self) -> tuple[str, float] | None:
        return self._last_played("last_played_in_app")

    def set_last_played_in_app(self, asin: str) -> None:
        self._data["last_played_in_app"] = {"asin": asin, "updated_at": time.time()}
        self.save()

    @property
    def last_played_externally(self) -> tuple[str, float] | None:
        return self._last_played("last_played_externally")

    def set_last_played_externally(self, asin: str, updated_at: datetime) -> None:
        self._data["last_played_externally"] = {
            "asin": asin,
            "updated_at": updated_at.timestamp(),
        }
        self.save()

    def _last_played(self, key: str) -> tuple[str, float] | None:
        entry = self._data.get(key)
        if not isinstance(entry, dict) or "asin" not in entry or "updated_at" not in entry:
            return None
        try:
            return str(entry["asin"]), float(entry["updated_at"])
        except (TypeError, ValueError):
            return None
