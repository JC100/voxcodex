"""Downloads a purchased title's AAXC file + decryption voucher for offline use."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

from voxcodex import config
from voxcodex.models import Book
from voxcodex.services.api import AudibleAPI, InvalidAsin, License, require_valid_asin

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

# InvalidAsin/require_valid_asin live in api.py (asin's the ASIN's real
# home -- it's where every asin first enters the app, and api.py itself
# needs the same validation for the request paths it builds, L15) and are
# re-exported here rather than duplicated: used below wherever an asin is
# turned into a filename with no other validation -- a value like
# "../../../../etc/cron.d/x" would otherwise write outside DOWNLOADS_DIR
# (L1).


class DownloadCancelled(Exception):
    """Raised by `download_book` when `cancel_check` asks it to stop."""


def voucher_path_for(asin: str) -> Path:
    return config.DOWNLOADS_DIR / f"{require_valid_asin(asin)}.voucher.json"


def audio_path_for(asin: str) -> Path:
    return config.DOWNLOADS_DIR / f"{require_valid_asin(asin)}.aaxc"


def is_downloaded(asin: str) -> bool:
    # Read-only and called unconditionally for every book on every library
    # load -- an invalid ASIN should make this title report "not
    # downloaded" rather than take the whole load down.
    try:
        return audio_path_for(asin).exists() and voucher_path_for(asin).exists()
    except InvalidAsin:
        return False


def downloaded_size(asin: str) -> int | None:
    """Size in bytes of `asin`'s downloaded audio file, or None if it isn't
    downloaded (or the file vanished between the is_downloaded check and
    this call -- e.g. deleted from another VoxCodex instance)."""
    try:
        return audio_path_for(asin).stat().st_size
    except (OSError, InvalidAsin):
        return None


def sweep_stale_downloads() -> None:
    """Removes any leftover `*.part` file in DOWNLOADS_DIR. Under normal
    operation `download_book` cleans up its own tmp file on every failure
    path, but a hard kill (SIGKILL, power loss, an unclean container exit)
    skips that finally-equivalent cleanup entirely -- so a stale `.part`
    from a previous run is swept once at startup, before it can be mistaken
    for an in-progress download by anything else that walks this directory.
    """
    if not config.DOWNLOADS_DIR.is_dir():
        return
    for part_file in config.DOWNLOADS_DIR.glob("*.part"):
        try:
            part_file.unlink()
        except OSError:
            logger.debug("failed to remove stale .part file %s", part_file, exc_info=True)


def download_book(
    book: Book,
    api: AudibleAPI,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    config.ensure_dirs()
    license_ = api.get_license(book.asin)

    audio_path = audio_path_for(book.asin)
    # A unique name per attempt, not the deterministic {asin}.part: two
    # downloads racing on the same book (a same-book double-press, or a
    # second VoxCodex instance) would otherwise collide on one temp file --
    # one attempt's cleanup (or the startup sweep) unlinking the file out
    # from under the other, which is still writing to the now-unlinked
    # inode. sweep_stale_downloads's `*.part` glob still matches this (M7).
    fd, tmp_name = tempfile.mkstemp(
        dir=config.DOWNLOADS_DIR, prefix=f"{book.asin}-", suffix=".part"
    )
    tmp_path = Path(tmp_name)

    try:
        # Fetched through the same authenticated session used for API calls
        # (matching audible-cli's own downloader), not a bare
        # unauthenticated client -- Audible's CDN has rejected the plain-
        # httpx version of this request with a WAF "Request blocked" 403
        # even though the signed URL itself was valid, while this same
        # signed-session request and mpv's own fetch (used for streaming)
        # both work.
        with api.client.session.stream(
            "GET", license_.content_url, follow_redirects=True, timeout=60
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    if cancel_check is not None and cancel_check():
                        raise DownloadCancelled(book.asin)
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
                # Without this, power loss shortly after a completed
                # download can land the rename below durable while the
                # audio data behind it isn't (L5).
                f.flush()
                os.fsync(f.fileno())

        # A connection dropped mid-stream leaves a short file that would
        # otherwise be renamed into place and look downloaded until it fails
        # to play. Only accept it when the server told us a size and we got
        # it -- compared against resp.num_bytes_downloaded (the raw,
        # possibly-still-compressed transfer size, tracked from iter_raw
        # underneath iter_bytes), not the local `downloaded` counter of
        # decoded bytes actually written to disk: httpx negotiates gzip by
        # default, so a CDN that ever compresses would otherwise make every
        # download fail as "truncated" (L11).
        if total and resp.num_bytes_downloaded != total:
            raise OSError(
                f"download truncated: got {resp.num_bytes_downloaded} of {total} bytes"
            )

        # Voucher before rename: is_downloaded() requires both files, so a
        # crash in between leaves `.part` (swept at next startup) and a
        # voucher with no audio yet -- never an audio file reported as
        # downloaded with no voucher to decrypt it. Both now inside this
        # same try: a failure in either must also clean up the other,
        # rather than leaving an orphaned voucher with no audio to match it.
        _write_voucher(book.asin, license_)
        tmp_path.replace(audio_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        voucher_path_for(book.asin).unlink(missing_ok=True)
        raise
    return audio_path


def _write_voucher(asin: str, license_: License) -> None:
    # atomic_write_text's temp-file-then-rename also leaves the voucher at
    # 0600 -- it holds the AES key and iv needed to decrypt the audio.
    config.atomic_write_text(
        voucher_path_for(asin),
        json.dumps(
            {
                "asin": asin,
                "key": license_.key,
                "iv": license_.iv,
                # Needed to push a position back for a downloaded/offline play
                # (see services.progress.push_position) -- not for decryption.
                # A voucher saved before this field existed loads fine via
                # .get(); that title just can't push until it's re-downloaded
                # or played once while streaming.
                "acr": license_.acr,
                # Same story, for push_listening_session -- needed to report
                # a listening session for a downloaded/offline play.
                "license_id": license_.license_id,
            },
            indent=2,
        ),
    )


def load_voucher(asin: str) -> dict[str, str] | None:
    """The saved voucher for `asin`, or None if it's missing, corrupted
    (a partial disk, a bad sync -- atomic_write_text only protects the
    write itself, not later corruption), or unreadable. The caller's
    existing "no voucher" handling (a clean, user-visible error) already
    covers all three the same way -- a JSONDecodeError/OSError here
    otherwise escaped unhandled and hung the play flow forever with no
    message shown (M14)."""
    path = voucher_path_for(asin)
    if not path.exists():
        return None
    try:
        return cast("dict[str, str]", json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        logger.debug("failed to read voucher for %s", asin, exc_info=True)
        return None


def delete_download(asin: str) -> None:
    # unlink(missing_ok=True) rather than a separate exists() check -- a
    # second instance, or the bulk-delete loop below racing this same
    # title, can otherwise remove the file in the gap between the two,
    # tripping a FileNotFoundError here (L6).
    for path in (audio_path_for(asin), voucher_path_for(asin)):
        path.unlink(missing_ok=True)
