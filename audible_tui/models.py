"""Plain data models for library items and playback state."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Book:
    asin: str
    title: str
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series: str = ""
    series_sequence: str = ""
    runtime_min: int = 0
    cover_url: str = ""
    purchase_date: str = ""
    is_downloaded: bool = False
    local_audio_path: str = ""
    local_voucher_path: str = ""
    progress_ms: int = 0
    duration_ms: int = 0
    is_finished: bool = False

    @property
    def author_display(self) -> str:
        return ", ".join(self.authors) or "Unknown"

    @property
    def series_display(self) -> str:
        if not self.series:
            return ""
        if self.series_sequence:
            return f"{self.series} #{self.series_sequence}"
        return self.series

    @property
    def runtime_display(self) -> str:
        h, m = divmod(self.runtime_min, 60)
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"

    @property
    def progress_pct(self) -> int:
        if not self.duration_ms:
            return 0
        return min(100, round(self.progress_ms / self.duration_ms * 100))
