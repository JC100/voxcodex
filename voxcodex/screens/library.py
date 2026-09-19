"""Main library screen: browse, search, download, and launch playback."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from audible.exceptions import AudibleError
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, ProgressBar, Static
from textual.widgets.data_table import CellDoesNotExist
from textual.worker import get_current_worker

from voxcodex.models import Book
from voxcodex.screens.modals import ConfirmModal
from voxcodex.screens.player_screen import PlayerScreen
from voxcodex.services import chapter_cache, download, library_cache, progress
from voxcodex.services.api import AudibleAPI, Chapter, LicenseDenied, NoDownloadUrl
from voxcodex.services.settings import Settings

logger = logging.getLogger(__name__)

COLUMNS = (
    "Title", "Author", "Series", "Length", "Progress", "Chapter", "Downloaded", "Size",
)

# Failures a chapter-metadata fetch can actually raise: a network/API
# problem. Anything else (a real bug -- bad response shape, etc.) should
# propagate to the worker's error handler instead of quietly leaving the
# Chapter column blank forever.
_CHAPTER_FETCH_ERRORS = (httpx.HTTPError, AudibleError)

# Failures opening a title for playback can legitimately raise: a missing/
# malformed local voucher, a denied license or a license response with no
# download URL, or a network/API problem reaching Audible.
_PLAYER_OPEN_ERRORS = (
    RuntimeError, KeyError, LicenseDenied, NoDownloadUrl, httpx.HTTPError, AudibleError,
)


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _series_sequence_number(sequence: str) -> float:
    try:
        return float(sequence)
    except (TypeError, ValueError):
        return 0.0


# Order here is the cycle order for the "o"/"f" keys. Labels are what's shown
# in the always-visible sort/filter line -- this is state you can't easily
# infer from the table alone, unlike e.g. the search query.
_SORT_OPTIONS = ("recent", "title", "author", "series", "progress")
_SORT_LABELS = {
    "recent": "Recent", "title": "Title", "author": "Author",
    "series": "Series", "progress": "Progress",
}
_FILTER_OPTIONS = ("all", "downloaded", "in_progress", "finished", "not_started")
_FILTER_LABELS = {
    "all": "All", "downloaded": "Downloaded", "in_progress": "In progress",
    "finished": "Finished", "not_started": "Not started",
}
_PROGRESS_DISPLAY_OPTIONS = ("percent", "time_left", "both")
_PROGRESS_DISPLAY_LABELS = {
    "percent": "%", "time_left": "Time left", "both": "% + time left",
}


# A playback session that ends at or past this fraction of a book's runtime is
# treated as "finished" -- both for VoxCodex's own library flag and for the
# best-effort push back to Audible's finished state. Audible's own clients mark
# a title finished a hair before the very end too; 0.98 leaves room for
# trailing credits/silence without needing a hard 100%.
#
# The "Finished"/"In progress" library filter uses this same threshold (as a
# percent) rather than its own hardcoded 100% -- they used to disagree, so a
# book synced at 98-99% complete (percent_complete from the library API, but
# never actually played to the end *in this app*) showed as "in progress"
# even though this app's own is_finished rule would call that finished.
_FINISHED_FRACTION = 0.98
_FINISHED_PCT = round(_FINISHED_FRACTION * 100)


def _reached_end(position_ms: int, duration_ms: int) -> bool:
    return duration_ms > 0 and position_ms >= duration_ms * _FINISHED_FRACTION


def _resolve_progress_ms(
    library_ms: int,
    local_ms: int,
    local_updated_at: float | None,
    remote_ms: int,
    remote_updated_at: float | None,
) -> int:
    """Picks the position to show/resume from -- the *newer* of the local
    (this app) and remote (Audible's own last-heard record) positions by
    timestamp, not the larger of the two by value. A plain max() gets stuck
    at the old high-water mark forever once you restart a book from
    chapter 1 on another device: only an in-app close can ever lower it.

    Falls back to `library_ms` (the library listing's own percent_complete-
    derived value, always fresh as of this fetch but untimestamped) when
    there's no local or remote record to compare at all.
    """
    if local_updated_at is not None and remote_updated_at is not None:
        return local_ms if local_updated_at >= remote_updated_at else remote_ms
    if local_updated_at is not None:
        return local_ms
    if remote_updated_at is not None:
        return remote_ms
    return library_ms


def _current_chapter_number(chapters: list[Chapter], position_ms: int) -> int | None:
    """1-based number of the chapter containing `position_ms`, or None if
    `chapters` is empty.

    A book with no progress at all reports 0, not 1 -- you can't be "on"
    chapter 1 before you've actually started listening to it.
    """
    if not chapters:
        return None
    if position_ms <= 0:
        return 0
    number = None
    for i, chapter in enumerate(chapters, start=1):
        if chapter.start_ms <= position_ms:
            number = i
        else:
            break
    return number


class LibraryScreen(Screen[None]):
    BINDINGS = [
        ("/", "focus_search", "Search"),
        ("down", "focus_table", "To list"),
        # DataTable already binds "up" itself (move cursor up a row), so a
        # normal binding here would never fire while the table has focus --
        # it always wins for that key. priority=True checks this before the
        # table gets a chance, so we can decide: at the top row, go to
        # search (the counterpart of "down" from search); otherwise, do
        # exactly what the table would have done anyway.
        Binding("up", "cursor_up_or_focus_search", "To search", priority=True),
        ("d", "download_selected", "Download"),
        ("p,space", "play_selected", "Play"),
        ("x", "delete_selected", "Delete download"),
        ("X", "delete_finished_downloads", "Delete finished downloads"),
        ("u", "unmark_finished", "Unmark finished"),
        ("r", "refresh", "Refresh"),
        ("o", "cycle_sort", "Sort"),
        ("f", "cycle_filter", "Filter"),
        ("t", "cycle_progress_display", "Progress display"),
        ("escape", "clear_search", "Clear search"),
    ]

    def __init__(self, api: AudibleAPI, settings: Settings | None = None) -> None:
        super().__init__()
        self.api = api
        self.progress_store = progress.ProgressStore()
        self.settings = settings if settings is not None else Settings()
        self._books: list[Book] = []
        self._filtered: list[Book] = []
        self._search_debounce_timer: Timer | None = None
        # Seeded from disk (chapter *lists* never change for a book, so
        # they're safe to persist), then extended in-memory as new titles
        # are fetched this session. chapter_current is never cached here --
        # it depends on progress and is always recomputed locally from the
        # cached start_ms values via _current_chapter_number.
        self._chapter_cache: dict[str, list[Chapter]] = chapter_cache.load()
        self._sort_key = (
            self.settings.library_sort_key
            if self.settings.library_sort_key in _SORT_OPTIONS
            else _SORT_OPTIONS[0]
        )
        self._filter_key = (
            self.settings.library_filter_key
            if self.settings.library_filter_key in _FILTER_OPTIONS
            else _FILTER_OPTIONS[0]
        )
        self._progress_display = (
            self.settings.progress_display_mode
            if self.settings.progress_display_mode in _PROGRESS_DISPLAY_OPTIONS
            else _PROGRESS_DISPLAY_OPTIONS[0]
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Input(
                placeholder="Search title / author / series... (↓ or Enter for list)",
                id="search",
            )
            yield Static("", id="sort-filter")
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="status-bar"):
                yield Static("", id="status")
            yield ProgressBar(id="download-progress", total=100, show_eta=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for col in COLUMNS:
            table.add_column(col, key=col)
        self.query_one("#download-progress", ProgressBar).display = False
        self._update_sort_filter_label()
        self._load_library()

    # -- loading -------------------------------------------------------

    def _set_status(self, text: str) -> None:
        # Called from background workers via call_from_thread; the screen may
        # already have been popped by the time one lands.
        with contextlib.suppress(NoMatches):
            self.query_one("#status", Static).update(text)

    def _load_library(self) -> None:
        self._set_status("Loading your library...")
        self._fetch_library()

    @work(thread=True, exclusive=True, group="library", exit_on_error=False)
    def _fetch_library(self) -> None:
        worker = get_current_worker()
        try:
            books = self.api.get_library()
        except Exception as exc:  # noqa: BLE001
            if worker.is_cancelled:
                return
            cached = library_cache.load()
            if cached is None:
                self.app.call_from_thread(
                    self._set_status, f"[red]Failed to load library: {exc}[/red]"
                )
                return
            cached_books, cached_at = cached
            self._apply_local_state(cached_books)
            self.app.call_from_thread(self._populate_offline, cached_books, cached_at)
            return

        if worker.is_cancelled:
            return

        library_cache.save(books)

        annotations = progress.fetch_remote_annotations(self.api, [b.asin for b in books])
        remote_positions = progress.positions_with_updated_at_from_annotations(annotations)

        most_recent = progress.most_recent_external_play(annotations)
        if most_recent is not None:
            asin, updated_at = most_recent
            self.settings.set_last_played_externally(asin, updated_at)

        self._apply_local_state(books, remote_positions)
        if worker.is_cancelled:
            return
        self.app.call_from_thread(self._populate, books)

    def _apply_local_state(
        self,
        books: list[Book],
        remote_positions: dict[str, tuple[int, float]] | None = None,
    ) -> None:
        """Fills in whatever we can know without a network call: local
        download status and the resume position. Used for both a live fetch
        and an offline cache fallback -- `remote_positions` is simply empty
        in the latter case."""
        remote_positions = remote_positions or {}
        for book in books:
            book.is_downloaded = download.is_downloaded(book.asin)
            local_ms = self.progress_store.get_position_ms(book.asin)
            local_updated_at = self.progress_store.get_updated_at(book.asin)
            remote_ms, remote_updated_at = remote_positions.get(book.asin, (0, None))
            book.progress_ms = _resolve_progress_ms(
                book.progress_ms, local_ms, local_updated_at, remote_ms, remote_updated_at
            )

    def _apply_cached_chapters(self, books: list[Book]) -> None:
        """Fills in chapter_total/chapter_current for any book already in
        `_chapter_cache` (this session's fetches, or seeded from disk) --
        no network call, so safe to run inline before the table first
        renders."""
        for book in books:
            chapters = self._chapter_cache.get(book.asin)
            if chapters is not None:
                book.chapter_total = len(chapters)
                book.chapter_current = _current_chapter_number(chapters, book.progress_ms)

    def _populate(self, books: list[Book]) -> None:
        self._books = books
        self._apply_cached_chapters(books)
        self._apply_filters_and_sort()
        self._set_status(f"{len(books)} titles")
        self._fetch_chapter_counts(books)

    def _populate_offline(self, books: list[Book], cached_at: float) -> None:
        self._books = books
        self._apply_cached_chapters(books)
        self._apply_filters_and_sort()
        age = _format_age(time.time() - cached_at)
        self._set_status(
            f"[yellow]Offline -- showing last known library "
            f"({len(books)} titles, cached {age} ago). Downloaded books still play.[/yellow]"
        )
        # No _fetch_chapter_counts here: every uncached title would fail
        # slowly against the API client's 30s timeout while offline, and
        # _apply_cached_chapters above already showed everything this
        # session can know without a network call.

    # How many newly-fetched chapter counts to batch into one table rebuild,
    # whichever comes first: this many books, or this many seconds. Without
    # batching, a full clear-and-rebuild of every row runs once per book --
    # O(n^2) over a library of any size.
    _CHAPTER_REFRESH_BATCH_SIZE = 25
    _CHAPTER_REFRESH_INTERVAL_S = 0.5

    @work(thread=True, exclusive=True, group="chapter_counts", exit_on_error=False)
    def _fetch_chapter_counts(self, books: list[Book]) -> None:
        """Progressively fills in each book's chapter column after the table
        is already showing -- one extra network call per book not already
        cached (this session or on disk), batched into occasional table
        rebuilds rather than one per book. A book that fails (typically:
        offline, or no chapter data for that title) just keeps its blank
        Chapter cell."""
        worker = get_current_worker()
        to_fetch = [book for book in books if book.asin not in self._chapter_cache]
        if not to_fetch:
            return

        fetched_since_refresh = 0
        last_refresh = time.monotonic()
        newly_cached = False
        for book in to_fetch:
            if worker.is_cancelled:
                break
            try:
                chapters = self.api.get_chapters(book.asin)
            except _CHAPTER_FETCH_ERRORS:
                continue
            self._chapter_cache[book.asin] = chapters
            newly_cached = True
            book.chapter_total = len(chapters)
            book.chapter_current = _current_chapter_number(chapters, book.progress_ms)

            fetched_since_refresh += 1
            now = time.monotonic()
            if (
                fetched_since_refresh >= self._CHAPTER_REFRESH_BATCH_SIZE
                or now - last_refresh >= self._CHAPTER_REFRESH_INTERVAL_S
            ):
                if worker.is_cancelled:
                    break
                self.app.call_from_thread(self._refresh_table)
                fetched_since_refresh = 0
                last_refresh = now

        if fetched_since_refresh and not worker.is_cancelled:
            self.app.call_from_thread(self._refresh_table)
        if newly_cached:
            chapter_cache.save(self._chapter_cache)

    def _progress_cell(self, book: Book) -> str:
        if self._progress_display == "time_left":
            text = book.time_left_display
        elif self._progress_display == "both":
            text = f"{book.progress_pct}% ({book.time_left_display})"
        else:
            text = f"{book.progress_pct}%"
        return text + (" ✓" if book.is_finished else "")

    def _size_cell(self, book: Book) -> str:
        if not book.is_downloaded:
            return ""
        size = download.downloaded_size(book.asin)
        return _format_size(size) if size is not None else ""

    def _refresh_table(self) -> None:
        # Reached from the background chapter-count worker too, which may
        # outlive the screen.
        try:
            table = self.query_one(DataTable)
        except NoMatches:
            return
        # table.clear() resets the cursor to the top row -- fine when the
        # rebuild follows straight from acting on the selected row (most
        # calls here), but the background chapter-count fetch calls this
        # independently of anything you're doing, so without restoring the
        # selection it would silently yank your cursor back to the top of
        # the list while you're just browsing.
        previously_selected = self._selected_book()
        selected_asin = previously_selected.asin if previously_selected else None
        table.clear()
        for book in self._filtered:
            table.add_row(
                book.title,
                book.author_display,
                book.series_display,
                book.runtime_display,
                self._progress_cell(book),
                book.chapter_display,
                "yes" if book.is_downloaded else "",
                self._size_cell(book),
                key=book.asin,
            )
        if selected_asin is not None:
            for row_index, book in enumerate(self._filtered):
                if book.asin == selected_asin:
                    table.move_cursor(row=row_index)
                    break

    def _selected_book(self) -> Book | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or not self._filtered:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except CellDoesNotExist:
            return None
        asin = row_key.value
        for book in self._books:
            if book.asin == asin:
                return book
        return None

    # -- search ----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one(DataTable).focus()

    def action_cursor_up_or_focus_search(self) -> None:
        table = self.query_one(DataTable)
        if self.focused is not table:
            # "up" wasn't bound to anything here before this binding
            # existed either (Input doesn't claim it) -- leave it a no-op
            # rather than guessing new behavior for some other widget.
            return
        if table.cursor_row == 0:
            self.action_focus_search()
        else:
            table.action_cursor_up()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        self.query_one(DataTable).focus()

    # How long to wait after the last keystroke before actually re-filtering
    # and re-sorting -- without this, every keystroke cleared and rebuilt
    # the whole table (noticeable lag on a large library).
    _SEARCH_DEBOUNCE_S = 0.15

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        if self._search_debounce_timer is not None:
            self._search_debounce_timer.stop()
        self._search_debounce_timer = self.set_timer(
            self._SEARCH_DEBOUNCE_S, self._apply_filters_and_sort
        )

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.action_focus_table()

    def action_refresh(self) -> None:
        self._load_library()

    # -- sort / filter -----------------------------------------------------

    def action_cycle_sort(self) -> None:
        idx = _SORT_OPTIONS.index(self._sort_key)
        self._sort_key = _SORT_OPTIONS[(idx + 1) % len(_SORT_OPTIONS)]
        self.settings.set_library_sort_key(self._sort_key)
        self._apply_filters_and_sort()

    def action_cycle_filter(self) -> None:
        idx = _FILTER_OPTIONS.index(self._filter_key)
        self._filter_key = _FILTER_OPTIONS[(idx + 1) % len(_FILTER_OPTIONS)]
        self.settings.set_library_filter_key(self._filter_key)
        self._apply_filters_and_sort()

    def action_cycle_progress_display(self) -> None:
        idx = _PROGRESS_DISPLAY_OPTIONS.index(self._progress_display)
        self._progress_display = (
            _PROGRESS_DISPLAY_OPTIONS[(idx + 1) % len(_PROGRESS_DISPLAY_OPTIONS)]
        )
        self.settings.set_progress_display_mode(self._progress_display)
        self._refresh_table()
        self._set_status(f"Progress column: {_PROGRESS_DISPLAY_LABELS[self._progress_display]}")

    def _total_downloaded_size(self) -> int:
        total = 0
        for book in self._books:
            if book.is_downloaded:
                size = download.downloaded_size(book.asin)
                if size is not None:
                    total += size
        return total

    def _update_sort_filter_label(self) -> None:
        # Shown count lives here rather than in #status: #status carries
        # transient messages (download progress, offline banner, etc.) that
        # a search keystroke or a sort/filter cycle shouldn't be able to
        # clobber, but "how many of my books am I actually looking at" needs
        # to stay live through both.
        count = (
            f"{len(self._filtered)}/{len(self._books)}"
            if len(self._filtered) != len(self._books)
            else str(len(self._books))
        )
        total_size = self._total_downloaded_size()
        size_suffix = f"   {_format_size(total_size)} downloaded" if total_size else ""
        self.query_one("#sort-filter", Static).update(
            f"Sort: {_SORT_LABELS[self._sort_key]}   Filter: {_FILTER_LABELS[self._filter_key]}"
            f"   ({count} shown){size_suffix}"
        )

    def _matches_filter(self, book: Book) -> bool:
        if self._filter_key == "downloaded":
            return book.is_downloaded
        if self._filter_key == "in_progress":
            return not book.is_finished and 0 < book.progress_pct < _FINISHED_PCT
        if self._filter_key == "finished":
            return book.is_finished or book.progress_pct >= _FINISHED_PCT
        if self._filter_key == "not_started":
            return not book.is_finished and book.progress_pct == 0
        return True  # "all"

    def _sort_key_func(self) -> Callable[[Book], Any]:
        if self._sort_key == "title":
            return lambda b: b.title.lower()
        if self._sort_key == "author":
            return lambda b: b.author_display.lower()
        if self._sort_key == "series":
            # Books with no series sort after all series-having ones, rather
            # than before (empty string would sort first).
            return lambda b: (
                b.series.lower() or "￿", _series_sequence_number(b.series_sequence)
            )
        if self._sort_key == "progress":
            return lambda b: b.progress_pct
        return lambda b: b.purchase_date  # "recent"

    def _sort_reverse(self) -> bool:
        # "Recent" and "Progress" read most naturally furthest-first; the
        # rest (title/author/series) are conventionally A-Z.
        return self._sort_key in ("recent", "progress")

    def _apply_filters_and_sort(self) -> None:
        query = self.query_one("#search", Input).value.strip().lower()
        books = self._books
        if query:
            books = [
                b
                for b in books
                if query in b.title.lower()
                or query in b.author_display.lower()
                or query in b.series_display.lower()
            ]
        books = [b for b in books if self._matches_filter(b)]
        self._filtered = sorted(books, key=self._sort_key_func(), reverse=self._sort_reverse())
        self._refresh_table()
        self._update_sort_filter_label()

    # -- download ----------------------------------------------------------

    def action_download_selected(self) -> None:
        book = self._selected_book()
        if book is None:
            return
        if book.is_downloaded:
            self._set_status(f"Already downloaded: {book.title}")
            return
        self._set_status(f"Downloading: {book.title}")
        bar = self.query_one("#download-progress", ProgressBar)
        bar.display = True
        bar.update(total=100, progress=0)
        self._do_download(book)

    @work(thread=True, exclusive=True, group="download", exit_on_error=False)
    def _do_download(self, book: Book) -> None:
        worker = get_current_worker()
        last_bar_update = 0.0

        def on_progress(done: int, total: int) -> None:
            nonlocal last_bar_update
            if not total or worker.is_cancelled:
                return
            # download.download_book calls this once per 256 KB chunk -- a
            # ~1 GB file is thousands of calls, each a blocking hop onto the
            # event loop. Cap it at ~4/s, but always let the final one land.
            now = time.monotonic()
            if done < total and now - last_bar_update < 0.25:
                return
            last_bar_update = now
            self.app.call_from_thread(self._update_download_bar, done, total)

        try:
            download.download_book(
                book,
                self.api,
                on_progress=on_progress,
                cancel_check=lambda: worker.is_cancelled,
            )
        except download.DownloadCancelled:
            return
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._download_failed, book, str(exc))
            return
        self.app.call_from_thread(self._download_succeeded, book)

    def _update_download_bar(self, done: int, total: int) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#download-progress", ProgressBar).update(
                total=total, progress=done
            )

    def _download_failed(self, book: Book, message: str) -> None:
        try:
            self.query_one("#download-progress", ProgressBar).display = False
        except NoMatches:
            return
        self._set_status(f"[red]Download failed for {book.title}: {message}[/red]")

    def _download_succeeded(self, book: Book) -> None:
        try:
            self.query_one("#download-progress", ProgressBar).display = False
        except NoMatches:
            return
        book.is_downloaded = True
        self._refresh_table()
        self._set_status(f"Downloaded: {book.title}")

    def action_delete_selected(self) -> None:
        book = self._selected_book()
        if book is None or not book.is_downloaded:
            return

        def _confirmed(confirmed: bool | None) -> None:
            if not confirmed:
                return
            download.delete_download(book.asin)
            book.is_downloaded = False
            self._refresh_table()
            self._set_status(f"Removed local copy of {book.title}")

        self.app.push_screen(
            ConfirmModal("Delete download", f"Delete the local copy of '{book.title}'?"),
            _confirmed,
        )

    def action_delete_finished_downloads(self) -> None:
        """The only bulk cleanup affordance downloads/ has: one-at-a-time
        via action_delete_selected doesn't scale once you've got a few
        dozen finished books sitting on disk. Deliberately scoped to
        is_downloaded AND is_finished -- an in-progress book never gets
        swept up in this even if you're low on space."""
        targets = [book for book in self._books if book.is_downloaded and book.is_finished]
        if not targets:
            self._set_status("No finished downloads to remove")
            return
        total_size = sum(
            size for book in targets
            if (size := download.downloaded_size(book.asin)) is not None
        )

        def _confirmed(confirmed: bool | None) -> None:
            if not confirmed:
                return
            for book in targets:
                download.delete_download(book.asin)
                book.is_downloaded = False
            self._refresh_table()
            self._set_status(
                f"Removed {len(targets)} finished download"
                f"{'s' if len(targets) != 1 else ''} ({_format_size(total_size)})"
            )

        self.app.push_screen(
            ConfirmModal(
                "Delete finished downloads",
                f"Delete the local copy of {len(targets)} finished book"
                f"{'s' if len(targets) != 1 else ''} ({_format_size(total_size)})?",
            ),
            _confirmed,
        )

    def action_unmark_finished(self) -> None:
        """The only way to undo a "Finished" flag: the 0.98-of-duration
        heuristic (`_reached_end`) can mis-fire on a book with long
        trailing credits, and until now that was permanent from inside
        VoxCodex -- push_finished(asin, False) was wired up but nothing
        ever called it."""
        book = self._selected_book()
        if book is None or not book.is_finished:
            return
        book.is_finished = False
        self._refresh_table()
        self._set_status(f"Unmarked as finished: {book.title}")
        self._push_finished(book.asin, False)

    # -- playback ----------------------------------------------------------

    def action_play_selected(self) -> None:
        book = self._selected_book()
        if book is None:
            return
        self._set_status(f"Opening license for {book.title}...")
        self._open_player(book)

    @work(thread=True, exclusive=True, group="player", exit_on_error=False)
    def _open_player(self, book: Book) -> None:
        worker = get_current_worker()
        try:
            if book.is_downloaded:
                voucher = download.load_voucher(book.asin)
                if voucher is None:
                    raise RuntimeError("Downloaded file is missing its decryption voucher")
                source = str(download.audio_path_for(book.asin))
                key, iv = voucher["key"], voucher["iv"]
                acr = voucher.get("acr", "")
            else:
                license_ = self.api.get_license(book.asin)
                source, key, iv = license_.content_url, license_.key, license_.iv
                acr = license_.acr
                # The license response's own last_position_heard is the
                # most authoritative resume position available -- fetched
                # fresh at the moment of playback, not at library-load
                # time. Resolve it against the local position the same way
                # _apply_local_state does on load: by recency, not by
                # magnitude (see _resolve_progress_ms).
                book.progress_ms = _resolve_progress_ms(
                    book.progress_ms,
                    self.progress_store.get_position_ms(book.asin),
                    self.progress_store.get_updated_at(book.asin),
                    license_.last_position_ms,
                    license_.last_position_updated_at,
                )
        except _PLAYER_OPEN_ERRORS as exc:
            self.app.call_from_thread(self._player_open_failed, str(exc))
            return

        # Reuse whatever the background library-table chapter fetch already
        # found for this book, rather than fetching it a second time.
        chapters = self._chapter_cache.get(book.asin)
        if chapters is None:
            try:
                chapters = self.api.get_chapters(book.asin)
            except _CHAPTER_FETCH_ERRORS:
                # Chapter navigation is an enhancement, not a playback
                # requirement -- a book without (or a failed fetch of)
                # chapter data should still play, just without next/
                # previous-chapter navigation. Deliberately *not* cached on
                # failure (unlike a real empty result): this might just be a
                # transient blip, and caching [] here would wrongly look
                # identical to "confirmed no chapters" and block any later
                # retry (background re-fetch on refresh, or a future play).
                chapters = []
            else:
                self._chapter_cache[book.asin] = chapters

        if worker.is_cancelled:
            return
        self.app.call_from_thread(
            self._launch_player, book, source, key, iv, chapters, acr
        )

    def _player_open_failed(self, message: str) -> None:
        self._set_status(f"[red]Could not start playback: {message}[/red]")

    def _launch_player(
        self,
        book: Book,
        source: str,
        key: str,
        iv: str,
        chapters: list[Chapter],
        acr: str,
    ) -> None:
        self._set_status("")

        def _on_progress(position_ms: int, *, final: bool) -> None:
            # Called both on a ~15s timer during playback and once on close /
            # unmount (final=True) -- so progress survives a hard quit, not
            # just an explicit q/esc out of the player.
            self.progress_store.set_position_ms(book.asin, position_ms, book.duration_ms)
            book.progress_ms = position_ms
            newly_finished = (
                final
                and _reached_end(position_ms, book.duration_ms)
                and not book.is_finished
            )
            if newly_finished:
                book.is_finished = True
            if final:
                # The library table is behind the player during a periodic
                # checkpoint -- only worth rebuilding when we're heading back
                # to it. The push to Audible is also close-only.
                self._refresh_table()
                # On a hard app quit the screen may already be tearing down,
                # in which case spawning a push worker can fail -- the local
                # save above is what actually matters, so don't let this
                # propagate out of the player's on_unmount.
                try:
                    self._push_position(book.asin, acr, position_ms)
                    if newly_finished:
                        self._push_finished(book.asin, True)
                except Exception:  # noqa: BLE001
                    logger.debug("progress push on close failed", exc_info=True)

        self.app.push_screen(
            PlayerScreen(
                book, source, key, iv,
                chapters=chapters,
                settings=self.settings,
                on_progress=_on_progress,
            )
        )

    @work(thread=True, exclusive=False, group="push_position", exit_on_error=False)
    def _push_position(self, asin: str, acr: str, position_ms: int) -> None:
        if not acr:
            # No per-content identifier to push with (see push_position's
            # docstring) -- nothing was attempted, so this isn't a sync
            # failure worth surfacing, just a known compatibility gap for
            # vouchers saved before `acr` existed.
            return
        if not progress.push_position(self.api, asin, acr, position_ms):
            self.app.call_from_thread(
                self._set_status,
                "[yellow]Progress saved locally; Audible sync failed[/yellow]",
            )

    @work(thread=True, exclusive=False, group="push_finished", exit_on_error=False)
    def _push_finished(self, asin: str, finished: bool) -> None:
        if not progress.push_finished(self.api, asin, finished):
            state = "Finished" if finished else "Un-finished"
            self.app.call_from_thread(
                self._set_status,
                f"[yellow]{state} status saved locally; Audible sync failed[/yellow]",
            )
