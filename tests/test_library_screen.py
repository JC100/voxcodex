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


class FakeSettings:
    def __init__(self, *args, **kwargs):
        self.last_played_externally_calls = []
        self.library_sort_key = "recent"
        self.library_filter_key = "all"

    def set_last_played_externally(self, asin, updated_at):
        self.last_played_externally_calls.append((asin, updated_at))

    def set_library_sort_key(self, key):
        self.library_sort_key = key

    def set_library_filter_key(self, key):
        self.library_filter_key = key


class FakeAudibleClient:
    def __init__(self, annotations_response=None):
        self._annotations_response = (
            annotations_response if annotations_response is not None else {}
        )

    def get(self, *args, **kwargs):
        return self._annotations_response


class FakeAPI:
    def __init__(
        self, books, chapters=None, chapters_exc=None, annotations_response=None,
        get_library_exc=None,
    ):
        self._books = books
        self.client = FakeAudibleClient(annotations_response)
        self.license_calls = []
        self.chapter_calls = []
        self._chapters = chapters if chapters is not None else []
        self._chapters_exc = chapters_exc
        self._get_library_exc = get_library_exc

    def get_library(self):
        if self._get_library_exc is not None:
            raise self._get_library_exc
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
def _fake_settings(monkeypatch):
    instance = FakeSettings()
    monkeypatch.setattr(library_module, "Settings", lambda: instance)
    return instance


class FakeLibraryCache:
    """Stands in for services.library_cache -- never touches the real
    ~/.local/share/audible-tui/library_cache.json. `to_return` is what
    `load()` answers with; defaults to "no cache exists yet"."""

    def __init__(self):
        self.save_calls = []
        self.to_return = None

    def save(self, books):
        self.save_calls.append(books)

    def load(self):
        return self.to_return


@pytest.fixture(autouse=True)
def _fake_library_cache(monkeypatch):
    instance = FakeLibraryCache()
    monkeypatch.setattr(library_module, "library_cache", instance)
    return instance


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


async def test_successful_fetch_caches_the_library_for_offline_use(_fake_library_cache):
    books = [_book("B1", "Book One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        assert len(_fake_library_cache.save_calls) == 1
        assert [b.asin for b in _fake_library_cache.save_calls[0]] == ["B1"]


async def test_failed_fetch_with_no_cache_shows_the_error(_fake_library_cache):
    _fake_library_cache.to_return = None  # no cache exists yet
    api = FakeAPI([], get_library_exc=RuntimeError("connection refused"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(
            lambda: "Failed to load library" in str(screen.query_one("#status").content)
        )
        assert screen._books == []


async def test_failed_fetch_falls_back_to_cache_and_shows_offline_status(
    _fake_library_cache, monkeypatch,
):
    import time as time_module

    cached_books = [_book("B1", "Cached Book")]
    _fake_library_cache.to_return = (cached_books, time_module.time() - 3600)  # 1h old
    api = FakeAPI([], get_library_exc=RuntimeError("connection refused"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        assert screen._books[0].asin == "B1"

        status = str(screen.query_one("#status").content)
        assert "Offline" in status
        assert "1h ago" in status
        # A failed fetch never had books to cache again.
        assert _fake_library_cache.save_calls == []


async def test_offline_fallback_still_reflects_local_download_and_progress_state(
    _fake_library_cache, monkeypatch,
):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: asin == "B1")

    cached_books = [_book("B1", "Cached Book"), _book("B2", "Other Book")]
    _fake_library_cache.to_return = (cached_books, 0.0)
    api = FakeAPI([], get_library_exc=RuntimeError("connection refused"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 2)
        by_asin = {b.asin: b for b in screen._books}
        assert by_asin["B1"].is_downloaded is True
        assert by_asin["B2"].is_downloaded is False


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


# -- sort / filter -----------------------------------------------------


def _sortable_book(asin, title, author="", series="", series_sequence="", purchase_date=""):
    return Book(
        asin=asin, title=title, authors=[author] if author else [],
        series=series, series_sequence=series_sequence, purchase_date=purchase_date,
    )


async def test_default_sort_is_recent_by_purchase_date_descending():
    books = [
        _sortable_book("B1", "Old", purchase_date="2020-01-01"),
        _sortable_book("B2", "New", purchase_date="2026-01-01"),
        _sortable_book("B3", "Middle", purchase_date="2023-01-01"),
    ]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 3)
        assert [b.title for b in screen._filtered] == ["New", "Middle", "Old"]


async def test_cycle_sort_to_title_orders_alphabetically(_fake_settings):
    books = [
        _sortable_book("B1", "Charlie"),
        _sortable_book("B2", "Alpha"),
        _sortable_book("B3", "Bravo"),
    ]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 3)
        screen.query_one(DataTable).focus()
        await pilot.press("o")  # recent -> title
        await pilot.pause()

        assert [b.title for b in screen._filtered] == ["Alpha", "Bravo", "Charlie"]
        assert _fake_settings.library_sort_key == "title"
        assert "Sort: Title" in str(screen.query_one("#sort-filter").content)


async def test_cycle_sort_wraps_all_the_way_around():
    screen = LibraryScreen(FakeAPI([_sortable_book("B1", "One")]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        for _ in range(5):  # recent -> title -> author -> series -> progress -> recent
            await pilot.press("o")
        await pilot.pause()

        assert screen._sort_key == "recent"


async def test_sort_by_series_puts_unseried_books_last():
    books = [
        _sortable_book("B1", "No Series"),
        _sortable_book("B2", "Second In Series", series="Zeta", series_sequence="2"),
        _sortable_book("B3", "First In Series", series="Zeta", series_sequence="1"),
    ]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 3)
        screen.query_one(DataTable).focus()
        await pilot.press("o")
        await pilot.press("o")
        await pilot.press("o")  # recent -> title -> author -> series
        await pilot.pause()

        assert [b.title for b in screen._filtered] == [
            "First In Series", "Second In Series", "No Series",
        ]


async def test_cycle_filter_to_downloaded_only(_fake_settings, monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: asin == "B1")
    books = [_sortable_book("B1", "Downloaded"), _sortable_book("B2", "Not downloaded")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        screen.query_one(DataTable).focus()
        await pilot.press("f")  # all -> downloaded
        await pilot.pause()

        assert [b.title for b in screen._filtered] == ["Downloaded"]
        assert _fake_settings.library_filter_key == "downloaded"
        label = str(screen.query_one("#sort-filter").content)
        assert "Filter: Downloaded" in label
        assert "(1/2 shown)" in label


async def test_sort_filter_label_shows_plain_count_when_nothing_is_filtered_out():
    books = [_sortable_book("B1", "One"), _sortable_book("B2", "Two")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 2)
        label = str(screen.query_one("#sort-filter").content)
        assert "(2 shown)" in label


async def test_filter_in_progress_excludes_finished_and_not_started():
    not_started = Book(asin="B1", title="Not started", progress_ms=0, duration_ms=1000)
    in_progress = Book(asin="B2", title="In progress", progress_ms=500, duration_ms=1000)
    finished = Book(asin="B3", title="Finished", progress_ms=1000, duration_ms=1000, is_finished=True)
    screen = LibraryScreen(FakeAPI([not_started, in_progress, finished]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 3)
        screen.query_one(DataTable).focus()
        await pilot.press("f")
        await pilot.press("f")  # all -> downloaded -> in_progress
        await pilot.pause()

        assert [b.title for b in screen._filtered] == ["In progress"]


async def test_filter_and_search_combine(monkeypatch):
    from textual.widgets import Input

    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: asin in ("B1", "B2"))
    books = [
        _sortable_book("B1", "Wanted Downloaded"),
        _sortable_book("B2", "Other Downloaded"),
        _sortable_book("B3", "Wanted Not Downloaded"),
    ]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 3)
        screen.query_one(DataTable).focus()
        await pilot.press("f")  # all -> downloaded (excludes B3)
        await pilot.pause()

        screen.query_one("#search", Input).focus()
        await pilot.press(*"wanted")  # further narrows to B1 (excludes B2)
        await pilot.pause()

        assert [b.title for b in screen._filtered] == ["Wanted Downloaded"]


async def test_sort_and_filter_are_restored_from_settings(_fake_settings):
    _fake_settings.library_sort_key = "title"
    _fake_settings.library_filter_key = "finished"

    screen = LibraryScreen(FakeAPI([]))

    assert screen._sort_key == "title"
    assert screen._filter_key == "finished"


async def test_unknown_persisted_sort_and_filter_keys_fall_back_to_defaults(_fake_settings):
    """Defensive against a future removed/renamed option in a settings.json
    left over from an older version of the app."""
    _fake_settings.library_sort_key = "some_removed_option"
    _fake_settings.library_filter_key = "some_removed_option"

    screen = LibraryScreen(FakeAPI([]))

    assert screen._sort_key == "recent"
    assert screen._filter_key == "all"


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

    def set_speed(self, speed):
        pass

    def set_volume(self, volume):
        pass

    @property
    def is_running(self):
        return True


class _FakeSettingsForLibraryTests:
    """Stands in for services.settings.Settings inside PlayerScreen, so these
    library-screen tests never touch the real config dir either."""

    playback_speed = 1.0
    playback_volume = 100.0

    def set_last_played_in_app(self, asin):
        pass


async def test_play_passes_fetched_chapters_to_the_player_screen(monkeypatch):
    from audible_tui.screens import player_screen as player_screen_module
    from audible_tui.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)
    monkeypatch.setattr(player_screen_module, "Settings", _FakeSettingsForLibraryTests)

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
    monkeypatch.setattr(player_screen_module, "Settings", _FakeSettingsForLibraryTests)

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


# -- last played externally -----------------------------------------------


def _annotations_response(records):
    return {"asin_last_position_heard_annots": records}


def _existing(asin, last_updated):
    return {
        "asin": asin,
        "last_position_heard": {
            "status": "Exists", "position_ms": 1000, "last_updated": last_updated,
        },
    }


async def test_library_load_records_the_most_recently_played_external_title(_fake_settings):
    response = _annotations_response(
        [
            _existing("B1", "2019-01-24 09:21:16.892"),
            _existing("B2", "2026-08-27 08:56:11.849"),
        ]
    )
    books = [_book("B1", "One"), _book("B2", "Two")]
    api = FakeAPI(books, annotations_response=response)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 2)

        assert len(_fake_settings.last_played_externally_calls) == 1
        asin, updated_at = _fake_settings.last_played_externally_calls[0]
        assert asin == "B2"
        assert updated_at.year == 2026


async def test_library_load_does_not_record_anything_when_nothing_was_ever_played(
    _fake_settings,
):
    response = _annotations_response(
        [{"asin": "B1", "last_position_heard": {"status": "DoesNotExist"}}]
    )
    books = [_book("B1", "One")]
    api = FakeAPI(books, annotations_response=response)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        assert _fake_settings.last_played_externally_calls == []
