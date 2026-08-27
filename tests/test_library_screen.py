"""Integration tests for LibraryScreen, driven through Textual's real event
loop and key-handling (Pilot) rather than calling action_* methods directly --
so these exercise the actual keybindings a user would press, not just the
Python methods behind them.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App
from textual.widgets import DataTable

from audible_tui.models import Book
from audible_tui.screens import library as library_module
from audible_tui.screens.library import LibraryScreen
from audible_tui.services.api import Chapter, License


class FakeProgressStore:
    def __init__(self, *args, **kwargs):
        self._data: dict[str, int] = {}

    def get_position_ms(self, asin: str) -> int:
        return self._data.get(asin, 0)

    def set_position_ms(self, asin: str, position_ms: int, duration_ms: int = 0) -> None:
        self._data[asin] = position_ms


class FakeAudibleClient:
    def get(self, *args, **kwargs):
        return {}  # empty best-effort remote-positions response


class FakeAPI:
    def __init__(self, books, chapters=None, chapters_exc=None):
        self._books = books
        self.client = FakeAudibleClient()
        self.license_calls = []
        self.chapter_calls = []
        self._chapters = chapters if chapters is not None else []
        self._chapters_exc = chapters_exc

    def get_library(self):
        return list(self._books)

    def get_license(self, asin, quality="high"):
        self.license_calls.append((asin, quality))
        return License(asin=asin, content_url="https://cdn/x", codec="AAXC", key="k", iv="i")

    def get_chapters(self, asin):
        self.chapter_calls.append(asin)
        if self._chapters_exc is not None:
            raise self._chapters_exc
        return list(self._chapters)


class HostApp(App):
    """Minimal host so LibraryScreen can be driven exactly as it runs for real."""

    def __init__(self, screen):
        super().__init__()
        self._screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._screen)


def _book(asin, title, authors=None, series="", runtime_min=60):
    return Book(asin=asin, title=title, authors=authors or [], series=series, runtime_min=runtime_min)


@pytest.fixture(autouse=True)
def _fake_progress_store(monkeypatch):
    monkeypatch.setattr(library_module.progress, "ProgressStore", FakeProgressStore)


@pytest.fixture(autouse=True)
def _no_downloads_by_default(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: False)


async def _wait_until(condition, timeout=2.0, step=0.02):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


# -- loading / populating -------------------------------------------------


async def test_library_populates_table_from_api():
    books = [_book("B1", "Book One"), _book("B2", "Book Two")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 2)
        table = screen.query_one(DataTable)
        assert table.row_count == 2


# -- search ---------------------------------------------------------------


async def test_search_filters_by_title():
    books = [
        _book("B1", "How to Win Friends", authors=["Dale Carnegie"]),
        _book("B2", "Before & Laughter", authors=["Jimmy Carr"]),
    ]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        # The search Input already has focus on mount (Textual's default
        # focus-first-widget behavior), so typing lands directly in it --
        # no need to press "/" first here (see test_slash_types_literal_
        # slash_when_search_already_focused for that specific quirk).
        await pilot.press(*"laughter")
        await pilot.pause()

        assert [b.asin for b in screen._filtered] == ["B2"]


async def test_search_filters_by_author():
    books = [
        _book("B1", "How to Win Friends", authors=["Dale Carnegie"]),
        _book("B2", "Before & Laughter", authors=["Jimmy Carr"]),
    ]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        await pilot.press(*"carnegie")
        await pilot.pause()

        assert [b.asin for b in screen._filtered] == ["B1"]


async def test_clear_search_restores_full_list():
    books = [_book("B1", "One"), _book("B2", "Two")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        await pilot.press(*"one")
        await pilot.pause()
        assert len(screen._filtered) == 1

        await pilot.press("escape")
        await pilot.pause()
        assert len(screen._filtered) == 2


async def test_slash_types_literal_slash_when_search_already_focused():
    """Documents a real, minor UX rough edge found while writing these tests:
    the search Input has focus by default on mount, so the "/" keybinding
    (meant to jump focus *into* search) never fires when you're already
    there -- Input consumes "/" as a literal character first. Harmless (it
    just adds a stray "/" to your query), but worth pinning down rather than
    leaving as an undocumented surprise."""
    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        await pilot.press("/")
        await pilot.pause()

        from textual.widgets import Input

        assert screen.query_one("#search", Input).value == "/"


# -- download ---------------------------------------------------------------


async def test_download_skipped_when_already_downloaded(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    download_calls = []
    monkeypatch.setattr(
        library_module.download, "download_book",
        lambda book, api, on_progress=None: download_calls.append(book.asin),
    )

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("d")
        await pilot.pause()

        assert download_calls == []
        assert "Already downloaded" in screen.query_one("#status").content


async def test_download_success_updates_status_and_table(monkeypatch):
    download_calls = []

    def fake_download_book(book, api, on_progress=None):
        download_calls.append(book.asin)
        return "/tmp/fake.aaxc"

    monkeypatch.setattr(library_module.download, "download_book", fake_download_book)

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("d")
        await _wait_until(lambda: download_calls == ["B1"])
        await _wait_until(lambda: screen._books[0].is_downloaded is True)
        await pilot.pause()

        assert "Downloaded: One" in str(screen.query_one("#status").content)


async def test_download_failure_shows_error_and_book_stays_not_downloaded(monkeypatch):
    def fake_download_book(book, api, on_progress=None):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(library_module.download, "download_book", fake_download_book)

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("d")
        await _wait_until(
            lambda: "Download failed" in str(screen.query_one("#status").content)
        )

        assert screen._books[0].is_downloaded is False


# -- delete -------------------------------------------------------------


async def test_delete_requires_confirmation(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    delete_calls = []
    monkeypatch.setattr(
        library_module.download, "delete_download", lambda asin: delete_calls.append(asin)
    )

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    books[0].is_downloaded = True
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("x")
        await pilot.pause()

        # A confirmation modal should be blocking -- nothing deleted yet.
        assert delete_calls == []

        await pilot.click("#no")
        await pilot.pause()
        assert delete_calls == []
        assert screen._books[0].is_downloaded is True  # untouched


async def test_delete_confirmed_removes_download(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    delete_calls = []
    monkeypatch.setattr(
        library_module.download, "delete_download", lambda asin: delete_calls.append(asin)
    )

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    books[0].is_downloaded = True
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("x")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()

        assert delete_calls == ["B1"]
        assert screen._books[0].is_downloaded is False


# -- playback / chapters -------------------------------------------------


class _FakeMpvPlayer:
    """Stands in for MpvPlayer so `p` never spawns a real mpv subprocess."""

    volume = 100.0

    def start(self, *args, **kwargs):
        pass

    def stop(self):
        pass

    @property
    def is_running(self):
        return True


async def test_play_passes_fetched_chapters_to_the_player_screen(monkeypatch):
    from audible_tui.screens import player_screen as player_screen_module
    from audible_tui.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    chapters = [Chapter(title="Chapter 1", start_ms=0, length_ms=60_000)]
    books = [_book("B1", "One")]
    api = FakeAPI(books, chapters=chapters)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))

        assert api.chapter_calls == ["B1"]
        assert app.screen._chapters == chapters


async def test_play_still_works_when_chapter_fetch_fails(monkeypatch):
    from audible_tui.screens import player_screen as player_screen_module
    from audible_tui.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    books = [_book("B1", "One")]
    api = FakeAPI(books, chapters_exc=RuntimeError("metadata endpoint exploded"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))

        # Chapter navigation is degraded, not the whole play action.
        assert app.screen._chapters == []
