"""Filesystem paths used by the app. All local state lives under the user's config/data dirs."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "audible-tui"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
DATA_DIR = Path(user_data_dir(APP_NAME))

AUTH_FILE = CONFIG_DIR / "auth.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PROGRESS_CACHE_FILE = DATA_DIR / "progress_cache.json"
LIBRARY_CACHE_FILE = DATA_DIR / "library_cache.json"
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOG_FILE = DATA_DIR / "audible-tui.log"


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
