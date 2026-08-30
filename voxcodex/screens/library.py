"""Main library screen: browse, search, download, and launch playback."""

from __future__ import annotations

import time

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, ProgressBar, Static

from voxcodex.models import Book
from voxcodex.screens.modals import ConfirmModal, MessageModal
from voxcodex.screens.player_screen import PlayerScreen
from voxcodex.services import download, library_cache, progress
from voxcodex.services.api import AudibleAPI, Chapter
from voxcodex.services.settings import Settings

COLUMNS = ("Title", "Author", "Series", "Length", "Progress", "Chapter", "Downloaded")


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
        ("r", "refresh", "Refresh"),
        ("o", "cycle_sort", "Sort"),
        ("f", "cycle_filter", "Filter"),
        ("t", "cycle_progress_display", "Progress display"),
        ("escape", "clear_search", "Clear search"),
    ]

    def __init__(self, api: AudibleAPI) -> None:
        super().__init__()
        self.api = api
        self.progress_store = progress.ProgressStore()
        self.settings = Settings()
        self._books: list[Book] = []
        self._filtered: list[Book] = []
        # In-memory only, refetched fresh each session -- a book's chapters
        # don't change, but its current-chapter does as you listen, so a
        # disk cache would need its own invalidation story. Reused across a
        # manual refresh within the same session so that doesn't re-fetch
        # what this session already knows.
        self._chapter_cache: dict[str, list[Chapter]] = {}
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
        self.query_one("#status", Static).update(text)

    def _load_library(self) -> None:
        self._set_status("Loading your library...")
        self._fetch_library()

    @work(thread=True, exclusive=True)
    def _fetch_library(self) -> None:
        try:
            books = self.api.get_library()
        except Exception as exc:  # noqa: BLE001
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

        library_cache.save(books)

        annotations = progress.fetch_remote_annotations(self.api, [b.asin for b in books])
        remote_positions = progress.positions_from_annotations(annotations)

        most_recent = progress.most_recent_external_play(annotations)
        if most_recent is not None:
            asin, updated_at = most_recent
            self.settings.set_last_played_externally(asin, updated_at)

        self._apply_local_state(books, remote_positions)
        self.app.call_from_thread(self._populate, books)

    def _apply_local_state(
        self, books: list[Book], remote_positions: dict[str, int] | None = None
    ) -> None:
        """Fills in whatever we can know without a network call: local
        download status and the further-along of the local/remote resume
        position. Used for both a live fetch and an offline cache fallback
        -- `remote_positions` is simply empty in the latter case."""
        remote_positions = remote_positions or {}
        for book in books:
            book.is_downloaded = download.is_downloaded(book.asin)
            local_ms = self.progress_store.get_position_ms(book.asin)
            remote_ms = remote_positions.get(book.asin, 0)
            book.progress_ms = max(book.progress_ms, local_ms, remote_ms)

    def _populate(self, books: list[Book]) -> None:
        self._books = books
        self._apply_filters_and_sort()
        self._set_status(f"{len(books)} titles")
        self._fetch_chapter_counts(books)

    def _populate_offline(self, books: list[Book], cached_at: float) -> None:
        self._books = books
        self._apply_filters_and_sort()
        age = _format_age(time.time() - cached_at)
        self._set_status(
            f"[yellow]Offline -- showing last known library "
            f"({len(books)} titles, cached {age} ago). Downloaded books still play.[/yellow]"
        )
        self._fetch_chapter_counts(books)

    @work(thread=True, exclusive=True, group="chapter_counts")
    def _fetch_chapter_counts(self, books: list[Book]) -> None:
        """Progressively fills in each book's chapter column after the table
        is already showing -- one extra network call per book not already
        known this session, so it's kept out of the main load/offline-
        fallback path entirely. A book that fails (typically: offline, or no
        chapter data for that title) just keeps its blank Chapter cell."""
        for book in books:
            chapters = self._chapter_cache.get(book.asin)
            if chapters is None:
                try:
                    chapters = self.api.get_chapters(book.asin)
                except Exception:  # noqa: BLE001
                    continue
                self._chapter_cache[book.asin] = chapters
            book.chapter_total = len(chapters)
            book.chapter_current = _current_chapter_number(chapters, book.progress_ms)
            self.app.call_from_thread(self._refresh_table)

    def _progress_cell(self, book: Book) -> str:
        if self._progress_display == "time_left":
            text = book.time_left_display
        elif self._progress_display == "both":
            text = f"{book.progress_pct}% ({book.time_left_display})"
        else:
            text = f"{book.progress_pct}%"
        return text + (" ✓" if book.is_finished else "")

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
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
        except Exception:
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

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._apply_filters_and_sort()

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
        self._progress_display = _PROGRESS_DISPLAY_OPTIONS[(idx + 1) % len(_PROGRESS_DISPLAY_OPTIONS)]
        self.settings.set_progress_display_mode(self._progress_display)
        self._refresh_table()
        self._set_status(f"Progress column: {_PROGRESS_DISPLAY_LABELS[self._progress_display]}")

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
        self.query_one("#sort-filter", Static).update(
            f"Sort: {_SORT_LABELS[self._sort_key]}   Filter: {_FILTER_LABELS[self._filter_key]}"
            f"   ({count} shown)"
        )

    def _matches_filter(self, book: Book) -> bool:
        if self._filter_key == "downloaded":
            return book.is_downloaded
        if self._filter_key == "in_progress":
            return not book.is_finished and 0 < book.progress_pct < 100
        if self._filter_key == "finished":
            return book.is_finished or book.progress_pct >= 100
        if self._filter_key == "not_started":
            return not book.is_finished and book.progress_pct == 0
        return True  # "all"

    def _sort_key_func(self):
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

    @work(thread=True, exclusive=True)
    def _do_download(self, book: Book) -> None:
        def on_progress(done: int, total: int) -> None:
            if total:
                self.app.call_from_thread(
                    self.query_one("#download-progress", ProgressBar).update,
                    total=total,
                    progress=done,
                )

        try:
            download.download_book(book, self.api, on_progress=on_progress)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._download_failed, book, str(exc))
            return
        self.app.call_from_thread(self._download_succeeded, book)

    def _download_failed(self, book: Book, message: str) -> None:
        self.query_one("#download-progress", ProgressBar).display = False
        self._set_status(f"[red]Download failed for {book.title}: {message}[/red]")

    def _download_succeeded(self, book: Book) -> None:
        self.query_one("#download-progress", ProgressBar).display = False
        book.is_downloaded = True
        self._refresh_table()
        self._set_status(f"Downloaded: {book.title}")

    def action_delete_selected(self) -> None:
        book = self._selected_book()
        if book is None or not book.is_downloaded:
            return

        def _confirmed(confirmed: bool) -> None:
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

    # -- playback ----------------------------------------------------------

    def action_play_selected(self) -> None:
        book = self._selected_book()
        if book is None:
            return
        self._set_status(f"Opening license for {book.title}...")
        self._open_player(book)

    @work(thread=True, exclusive=True)
    def _open_player(self, book: Book) -> None:
        try:
            if book.is_downloaded:
                voucher = download.load_voucher(book.asin)
                if voucher is None:
                    raise RuntimeError("Downloaded file is missing its decryption voucher")
                source = str(download.audio_path_for(book.asin))
                key, iv = voucher["key"], voucher["iv"]
                codec = voucher.get("codec", "")
                acr = voucher.get("acr", "")
                content_version = voucher.get("content_version", "")
            else:
                license_ = self.api.get_license(book.asin)
                source, key, iv = license_.content_url, license_.key, license_.iv
                codec = license_.codec
                acr = license_.acr
                content_version = license_.content_version
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._player_open_failed, str(exc))
            return

        # Reuse whatever the background library-table chapter fetch already
        # found for this book, rather than fetching it a second time.
        chapters = self._chapter_cache.get(book.asin)
        if chapters is None:
            try:
                chapters = self.api.get_chapters(book.asin)
            except Exception:  # noqa: BLE001
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

        self.app.call_from_thread(
            self._launch_player, book, source, key, iv, chapters, acr, content_version, codec
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
        content_version: str,
        codec: str,
    ) -> None:
        self._set_status("")

        def _on_close(final_position_ms: int) -> None:
            self.progress_store.set_position_ms(book.asin, final_position_ms, book.duration_ms)
            book.progress_ms = final_position_ms
            self._refresh_table()
            self._push_position(book.asin, acr, content_version, codec, final_position_ms)

        self.app.push_screen(PlayerScreen(book, source, key, iv, chapters=chapters), _on_close)

    @work(thread=True, exclusive=False, group="push_position")
    def _push_position(
        self, asin: str, acr: str, content_version: str, codec: str, position_ms: int
    ) -> None:
        progress.push_position(self.api, asin, acr, content_version, codec, position_ms)
