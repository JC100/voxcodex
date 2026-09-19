"""Filesystem paths used by the app. All local state lives under the user's config/data dirs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "voxcodex"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
DATA_DIR = Path(user_data_dir(APP_NAME))

AUTH_FILE = CONFIG_DIR / "auth.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PROGRESS_CACHE_FILE = DATA_DIR / "progress_cache.json"
LIBRARY_CACHE_FILE = DATA_DIR / "library_cache.json"
CHAPTER_CACHE_FILE = DATA_DIR / "chapter_cache.json"
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOG_FILE = DATA_DIR / "voxcodex.log"


def _mkdir_private(path: Path) -> None:
    # mode= only applies at creation -- exist_ok=True silently no-ops on an
    # already-existing dir, so a directory made before this hardening (or by
    # some other, laxer umask) needs an explicit chmod too. This directory
    # holds auth tokens, purchase history, and DRM keys/vouchers; nothing
    # under it should be group/other-readable.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def ensure_dirs() -> None:
    _mkdir_private(CONFIG_DIR)
    _mkdir_private(DATA_DIR)
    _mkdir_private(DOWNLOADS_DIR)


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file in the same directory followed
    by `os.replace`, so a crash mid-write or a concurrent reader never sees a
    half-written or truncated file. The rename is atomic on POSIX when both
    paths are on the same filesystem, which they are (same parent dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
