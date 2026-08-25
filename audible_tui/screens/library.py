"""Main library screen: browse, search, download, and launch playback."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, ProgressBar, Static

from audible_tui.models import Book
from audible_tui.screens.modals import ConfirmModal, MessageModal
from audible_tui.screens.player_screen import PlayerScreen
from audible_tui.services import download, progress
from audible_tui.services.api import AudibleAPI

COLUMNS = ("Title", "Author", "Series", "Length", "Progress", "Local")


class LibraryScreen(Screen[None]):
    BINDINGS = [
        ("/", "focus_search", "Search"),
        ("d", "download_selected", "Download"),
        ("p", "play_selected", "Play"),
        ("x", "delete_selected", "Delete download"),
        ("r", "refresh", "Refresh"),
        ("escape", "clear_search", "Clear search"),
    ]

    def __init__(self, api: AudibleAPI) -> None:
        super().__init__()
        self.api = api
        self.progress_store = progress.ProgressStore()
        self._books: list[Book] = []
        self._filtered: list[Book] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Input(placeholder="Search title / author / series...", id="search")
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
            self.app.call_from_thread(self._set_status, f"[red]Failed to load library: {exc}[/red]")
            return

        try:
            remote_positions = progress.fetch_remote_positions(
                self.api, [b.asin for b in books]
            )
        except Exception:  # noqa: BLE001
            remote_positions = {}

        for book in books:
            book.is_downloaded = download.is_downloaded(book.asin)
            local_ms = self.progress_store.get_position_ms(book.asin)
            remote_ms = remote_positions.get(book.asin, 0)
            book.progress_ms = max(book.progress_ms, local_ms, remote_ms)

        self.app.call_from_thread(self._populate, books)

    def _populate(self, books: list[Book]) -> None:
        self._books = books
        self._filtered = books
        self._refresh_table()
        self._set_status(f"{len(books)} titles")

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for book in self._filtered:
            table.add_row(
                book.title,
                book.author_display,
                book.series_display,
                book.runtime_display,
                f"{book.progress_pct}%" + (" ✓" if book.is_finished else ""),
                "yes" if book.is_downloaded else "",
                key=book.asin,
            )

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

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        self.query_one(DataTable).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        if not query:
            self._filtered = self._books
        else:
            self._filtered = [
                b
                for b in self._books
                if query in b.title.lower()
                or query in b.author_display.lower()
                or query in b.series_display.lower()
            ]
        self._refresh_table()

    def action_refresh(self) -> None:
        self._load_library()

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
            else:
                license_ = self.api.get_license(book.asin)
                source, key, iv = license_.content_url, license_.key, license_.iv
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._player_open_failed, str(exc))
            return
        self.app.call_from_thread(self._launch_player, book, source, key, iv)

    def _player_open_failed(self, message: str) -> None:
        self._set_status(f"[red]Could not start playback: {message}[/red]")

    def _launch_player(self, book: Book, source: str, key: str, iv: str) -> None:
        self._set_status("")

        def _on_close(final_position_ms: int) -> None:
            self.progress_store.set_position_ms(book.asin, final_position_ms, book.duration_ms)
            book.progress_ms = final_position_ms
            self._refresh_table()

        self.app.push_screen(PlayerScreen(book, source, key, iv), _on_close)
