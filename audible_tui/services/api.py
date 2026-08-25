"""Thin wrapper around `audible.Client` for the read-only calls this app needs:
library listing, content licensing (for download/playback decryption), and
listening-position lookup. No purchase/checkout endpoints are used anywhere here.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

import audible
import httpx
from audible.aescipher import decrypt_voucher_from_licenserequest
from audible.client import raise_for_status

from audible_tui.models import Book

LIBRARY_RESPONSE_GROUPS = (
    "contributors, customer_rights, media, product_attrs, product_desc, "
    "product_extended_attrs, series, is_finished, is_downloaded, "
    "listening_status, percent_complete, product_details"
)

LICENSE_RESPONSE_GROUPS = "last_position_heard, pdf_url, content_reference"

# Extra headers Amazon's licensing endpoint expects, mirroring what the
# official apps send. Confirmed against audible-cli's implementation.
_LICENSE_HEADERS = {
    "X-ADP-SW": "37801821",
    "X-ADP-Transport": "WIFI",
    "X-ADP-LTO": "120",
    "X-Device-Type-Id": "A2CZJZGLK2JJVM",
    "device_idiom": "phone",
}


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


def _full_response(resp: httpx.Response) -> httpx.Response:
    raise_for_status(resp)
    return resp


class AudibleAPI:
    def __init__(self, auth: audible.Authenticator) -> None:
        self._auth = auth
        self.client = audible.Client(auth=auth, timeout=30)

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
        codec = (content_metadata.get("content_reference") or {}).get(
            "content_format", "AAXC"
        )

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
        )


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
