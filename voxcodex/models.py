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
    # Filled in lazily (library table only, fetched in the background after
    # the table itself is already showing) -- None means "not fetched yet
    # this session"; chapter_total == 0 means "fetched, this title genuinely
    # has none" (podcasts, samples, some older titles). Both render blank.
    chapter_current: int | None = None
    chapter_total: int | None = None

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

    @property
    def time_left_display(self) -> str:
        if not self.duration_ms:
            return ""
        remaining_min = round(max(0, self.duration_ms - self.progress_ms) / 60_000)
        if remaining_min <= 0:
            return "done"
        h, m = divmod(remaining_min, 60)
        if h and m:
            return f"{h}h {m}m left"
        if h:
            return f"{h}h left"
        return f"{m}m left"

    @property
    def chapter_display(self) -> str:
        if not self.chapter_total:
            return ""
        # `or 1` would be wrong here: chapter_current == 0 (not started
        # yet) is a real, meaningful value, not falsy-for-"unknown" like
        # None is.
        current = 1 if self.chapter_current is None else self.chapter_current
        return f"{current}/{self.chapter_total}"
