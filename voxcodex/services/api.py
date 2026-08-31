"""Thin wrapper around `audible.Client` for the calls this app needs: library
listing, content licensing (for download/playback decryption), listening-position
read/write, and finished-state write. No purchase/checkout endpoints are used
anywhere here.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import audible
import httpx
from audible.aescipher import decrypt_voucher_from_licenserequest
from audible.client import raise_for_status

from voxcodex.models import Book

LIBRARY_RESPONSE_GROUPS = (
    "contributors, customer_rights, media, product_attrs, product_desc, "
    "product_extended_attrs, series, is_finished, is_downloaded, "
    "listening_status, percent_complete, product_details"
)

LICENSE_RESPONSE_GROUPS = "last_position_heard, pdf_url, content_reference"

# Every request this app makes -- API calls and the raw content download alike
# -- rides on a device registration that already identifies us as the Audible
# iOS app (that's what `audible.Authenticator`'s login/registration flow sets
# up). httpx's own default User-Agent breaks that identity for just the CDN
# download leg, which is the one place Amazon's CloudFront-fronted content
# servers apparently check it: the API host accepts the mismatch, the CDN
# doesn't. This isn't bypassing anything the auth flow doesn't already rely
# on -- it's completing it consistently, so the download identifies as the
# same already-registered app as everything else.
_DEVICE_USER_AGENT = "Audible/671 CFNetwork/1240.0.4 Darwin/20.6.0"

# Extra headers Amazon's licensing endpoint expects, mirroring what the
# official apps send. Confirmed against audible-cli's implementation.
_LICENSE_HEADERS = {
    "X-ADP-SW": "37801821",
    "X-ADP-Transport": "WIFI",
    "X-ADP-LTO": "120",
    "X-Device-Type-Id": "A2CZJZGLK2JJVM",
    "device_idiom": "phone",
}

# `PUT /1.0/stats/events` is Audible's own telemetry sink (session lifecycle +
# listening-interval events). VoxCodex only uses it for one thing: flipping a
# title's finished state, which is confirmed to propagate to
# `listening_status.is_finished` (and thus the official app/website "Finished"
# badge) within seconds, and is fully reversible. The listening-interval
# ("Listening") events are deliberately NOT sent -- their exact accepted shape
# isn't pinned down, and malformed ones were observed to reset a title's
# library-page `percent_complete` to 0. See
# docs/library-progress-sync-investigation.md for the full trail, including the
# enum of event types the endpoint accepts.
_STATS_EVENTS_PATH = "stats/events"


class LicenseDenied(Exception):
    pass


class NoDownloadUrl(Exception):
    pass


@dataclass
class License:
    asin: str
    content_url: str
    codec: str
    key: str
    iv: str
    last_position_ms: int = 0
    # Per-content identifier `push_last_position` needs to write a position
    # back to Audible's cross-device sync. Empty when a response doesn't
    # include it -- callers must treat that as "can't push for this title".
    acr: str = ""


@dataclass
class Chapter:
    title: str
    start_ms: int
    length_ms: int


def _full_response(resp: httpx.Response) -> httpx.Response:
    raise_for_status(resp)
    return resp


class AudibleAPI:
    def __init__(self, auth: audible.Authenticator) -> None:
        self._auth = auth
        self.client = audible.Client(
            auth=auth, timeout=30, headers={"User-Agent": _DEVICE_USER_AGENT}
        )

    def close(self) -> None:
        self.client.close()

    # -- library -----------------------------------------------------

    def get_library(self) -> list[Book]:
        books: list[Book] = []
        page = 1
        num_results = 1000
        while True:
            resp = self.client.get(
                "library",
                response_callback=_full_response,
                response_groups=LIBRARY_RESPONSE_GROUPS,
                num_results=num_results,
                page=page,
                sort_by="-PurchaseDate",
            )
            data = resp.json()
            items = data.get("items", [])
            books.extend(_book_from_item(item) for item in items)
            if len(items) < num_results:
                break
            page += 1
        return books

    # -- licensing / download -----------------------------------------

    def get_license(self, asin: str, quality: str = "high") -> License:
        api_quality = "High" if quality != "normal" else "Normal"
        body = {
            "supported_drm_types": ["Mpeg", "Adrm"],
            "quality": api_quality,
            "consumption_type": "Download",
            "response_groups": LICENSE_RESPONSE_GROUPS,
        }
        headers = {
            "X-Amzn-RequestId": secrets.token_hex(20).upper(),
            **_LICENSE_HEADERS,
        }
        lr = self.client.post(
            f"content/{asin}/licenserequest", body=body, headers=headers
        )
        content_license = lr["content_license"]

        if content_license.get("status_code") == "Denied":
            msg = content_license.get("message", "License denied")
            raise LicenseDenied(msg)

        content_metadata = content_license.get("content_metadata") or {}
        content_url = (content_metadata.get("content_url") or {}).get("offline_url")
        if not content_url:
            raise NoDownloadUrl(asin)
        content_reference = content_metadata.get("content_reference") or {}
        codec = content_reference.get("content_format", "AAXC")
        acr = content_reference.get("acr", "")

        key = iv = ""
        if "license_response" in content_license:
            voucher = decrypt_voucher_from_licenserequest(self._auth, lr)
            key = voucher.get("key", "")
            iv = voucher.get("iv", "")

        last_position_ms = 0
        lph = content_license.get("last_position_heard") or {}
        if isinstance(lph, dict) and "position_ms" in lph:
            last_position_ms = int(lph["position_ms"])

        return License(
            asin=asin,
            content_url=content_url,
            codec=codec,
            key=key,
            iv=iv,
            last_position_ms=last_position_ms,
            acr=acr,
        )

    # -- listening position (write) ------------------------------------

    def push_last_position(self, asin: str, acr: str, position_ms: int) -> None:
        """Writes this app's current position back to Audible's cross-device
        sync store, so the official app/website pick up where playback left
        off here. Raises on failure -- callers wanting best-effort semantics
        should use `services.progress.push_position` instead of calling this
        directly.

        `acr` must be the real per-content identifier from a `get_license()`
        response for *this* asin -- it's what keys the record. A stale or
        placeholder value gets a 200 OK same as a real one but silently never
        reaches `annotations/lastpositions` or any other device (that exact
        mistake is written up in docs/whispersync-research.md).
        """
        if not acr:
            raise ValueError("push_last_position needs a real acr from get_license()")
        self.client.put(
            f"lastpositions/{asin}",
            body={"acr": acr, "asin": asin, "position_ms": position_ms},
            response_callback=_full_response,
        )

    # -- finished state (write) --------------------------------------

    def set_finished(self, asin: str, finished: bool) -> None:
        """Marks `asin` finished (or un-finished) in Audible's cross-device
        state, so the official app/website library show the same "Finished"
        badge VoxCodex does once you reach the end of a book here.

        Confirmed against a live account (2026-08-30, see
        docs/library-progress-sync-investigation.md): a `ManualMarkAsFinished`
        event flips `listening_status.is_finished` to true within seconds, and
        `ManualMarkAsUnfinished` flips it straight back -- nothing here is
        one-way. Raises on HTTP failure; callers wanting best-effort semantics
        should use `services.progress.push_finished` rather than calling this
        directly.
        """
        now = datetime.now(timezone.utc)
        payload = {
            "stats": [
                {
                    "event_type": (
                        "ManualMarkAsFinished" if finished else "ManualMarkAsUnfinished"
                    ),
                    "asin": asin,
                    "event_timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{now.microsecond // 1000:03d}Z",
                    "listening_mode": "Online",
                    "delivery_type": "Streaming",
                    "audio_type": "FullTitle",
                    "store": "Audible",
                    "asin_owned": True,
                    "playing_immersion_reading": False,
                    "local_timezone": "Etc/UTC",
                    "social_network_site": "Unknown",
                }
            ]
        }
        self.client.put(
            _STATS_EVENTS_PATH, body=payload, response_callback=_full_response
        )

    # -- chapters -----------------------------------------------------

    def get_chapters(self, asin: str, quality: str = "high") -> list[Chapter]:
        """Fetches this title's chapter list (title + timing).

        Podcasts/samples and the odd older title may simply have none -- an
        empty result here isn't an error, just "nothing to navigate by".
        """
        api_quality = "High" if quality != "normal" else "Normal"
        resp = self.client.get(
            f"content/{asin}/metadata",
            response_groups="chapter_info",
            quality=api_quality,
            drm_type="Adrm",
            chapter_titles_type="Flat",
        )
        content_metadata = resp.get("content_metadata") or {}
        chapter_info = content_metadata.get("chapter_info") or {}
        raw_chapters = chapter_info.get("chapters") or []
        return [
            Chapter(
                title=c.get("title", ""),
                start_ms=int(c.get("start_offset_ms", 0)),
                length_ms=int(c.get("length_ms", 0)),
            )
            for c in raw_chapters
        ]


def _book_from_item(item: dict[str, Any]) -> Book:
    authors = [a.get("name", "") for a in (item.get("authors") or []) if a.get("name")]
    narrators = [
        n.get("name", "") for n in (item.get("narrators") or []) if n.get("name")
    ]
    series_list = item.get("series") or []
    series = series_list[0].get("title", "") if series_list else ""
    series_sequence = series_list[0].get("sequence", "") if series_list else ""
    images = item.get("product_images") or {}
    cover_url = images.get("500") or images.get("300") or ""
    runtime_min = item.get("runtime_length_min") or 0
    percent_complete = item.get("percent_complete") or 0
    duration_ms = runtime_min * 60_000
    progress_ms = round(duration_ms * (percent_complete / 100)) if duration_ms else 0
    is_finished = bool(item.get("is_finished"))

    return Book(
        asin=item.get("asin", ""),
        title=item.get("title", "Untitled"),
        subtitle=item.get("subtitle", "") or "",
        authors=authors,
        narrators=narrators,
        series=series,
        series_sequence=str(series_sequence) if series_sequence else "",
        runtime_min=runtime_min,
        cover_url=cover_url,
        purchase_date=item.get("purchase_date", "") or "",
        progress_ms=progress_ms,
        duration_ms=duration_ms,
        is_finished=is_finished,
    )
