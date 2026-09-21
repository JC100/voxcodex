"""Downloads a purchased title's AAXC file + decryption voucher for offline use."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

from voxcodex import config
from voxcodex.models import Book
from voxcodex.services.api import AudibleAPI, License

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class DownloadCancelled(Exception):
    """Raised by `download_book` when `cancel_check` asks it to stop."""


def voucher_path_for(asin: str) -> Path:
    return config.DOWNLOADS_DIR / f"{asin}.voucher.json"


def audio_path_for(asin: str) -> Path:
    return config.DOWNLOADS_DIR / f"{asin}.aaxc"


def is_downloaded(asin: str) -> bool:
    return audio_path_for(asin).exists() and voucher_path_for(asin).exists()


def downloaded_size(asin: str) -> int | None:
    """Size in bytes of `asin`'s downloaded audio file, or None if it isn't
    downloaded (or the file vanished between the is_downloaded check and
    this call -- e.g. deleted from another VoxCodex instance)."""
    try:
        return audio_path_for(asin).stat().st_size
    except OSError:
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
    quality: str = "high",
    cancel_check: CancelCheck | None = None,
) -> Path:
    config.ensure_dirs()
    license_ = api.get_license(book.asin, quality=quality)

    audio_path = audio_path_for(book.asin)
    tmp_path = audio_path.with_suffix(".part")

    # Fetched through the same authenticated session used for API calls (matching
    # audible-cli's own downloader), not a bare unauthenticated client -- Audible's
    # CDN has rejected the plain-httpx version of this request with a WAF "Request
    # blocked" 403 even though the signed URL itself was valid, while this same
    # signed-session request and mpv's own fetch (used for streaming) both work.
    try:
        with api.client.session.stream(
            "GET", license_.content_url, follow_redirects=True, timeout=60
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    if cancel_check is not None and cancel_check():
                        raise DownloadCancelled(book.asin)
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)

        # A connection dropped mid-stream leaves a short file that would
        # otherwise be renamed into place and look downloaded until it fails
        # to play. Only accept it when the server told us a size and we got it.
        if total and downloaded != total:
            raise OSError(
                f"download truncated: got {downloaded} of {total} bytes"
            )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # Voucher before rename: is_downloaded() requires both files, so a crash
    # in between leaves `.part` (swept at next startup) and a voucher with
    # no audio yet -- never an audio file reported as downloaded with no
    # voucher to decrypt it.
    _write_voucher(book.asin, license_)
    tmp_path.replace(audio_path)
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
                "codec": license_.codec,
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
    path = voucher_path_for(asin)
    if not path.exists():
        return None
    return cast("dict[str, str]", json.loads(path.read_text()))


def delete_download(asin: str) -> None:
    for path in (audio_path_for(asin), voucher_path_for(asin)):
        if path.exists():
            path.unlink()
