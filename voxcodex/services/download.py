"""Downloads a purchased title's AAXC file + decryption voucher for offline use."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from voxcodex import config
from voxcodex.models import Book
from voxcodex.services.api import AudibleAPI, License

ProgressCallback = Callable[[int, int], None]


def voucher_path_for(asin: str) -> Path:
    return config.DOWNLOADS_DIR / f"{asin}.voucher.json"


def audio_path_for(asin: str) -> Path:
    return config.DOWNLOADS_DIR / f"{asin}.aaxc"


def is_downloaded(asin: str) -> bool:
    return audio_path_for(asin).exists() and voucher_path_for(asin).exists()


def download_book(
    book: Book,
    api: AudibleAPI,
    on_progress: ProgressCallback | None = None,
    quality: str = "high",
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
    with api.client.session.stream(
        "GET", license_.content_url, follow_redirects=True, timeout=60
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)

    tmp_path.replace(audio_path)
    _write_voucher(book.asin, license_)
    return audio_path


def _write_voucher(asin: str, license_: License) -> None:
    voucher_path_for(asin).write_text(
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
            },
            indent=2,
        )
    )


def load_voucher(asin: str) -> dict[str, str] | None:
    path = voucher_path_for(asin)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def delete_download(asin: str) -> None:
    for path in (audio_path_for(asin), voucher_path_for(asin)):
        if path.exists():
            path.unlink()
