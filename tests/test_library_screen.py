"""Integration tests for LibraryScreen, driven through Textual's real event
loop and key-handling (Pilot) rather than calling action_* methods directly --
so these exercise the actual keybindings a user would press, not just the
Python methods behind them.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from textual.app import App
from textual.widgets import DataTable, Input, ProgressBar

from voxcodex.models import Book
from voxcodex.screens import library as library_module
from voxcodex.screens.library import (
    COLUMNS,
    LibraryScreen,
    _current_chapter_number,
    _FINISHED_PCT,
    _reached_end,
    _resolve_progress_ms,
)
from voxcodex.services.api import Chapter, InvalidResponse, License, LicenseDenied


class FakeProgressStore:
    def __init__(self, *args, **kwargs):
        self._data: dict[str, int] = {}
        self._updated_at: dict[str, float] = {}

    def get_position_ms(self, asin: str) -> int:
        return self._data.get(asin, 0)

    def get_updated_at(self, asin: str) -> float | None:
        return self._updated_at.get(asin)

    def set_position_ms(self, asin: str, position_ms: int, duration_ms: int = 0) -> None:
        self._data[asin] = position_ms
        self._updated_at[asin] = time.time()

    def seed(self, asin: str, position_ms: int, updated_at: float) -> None:
        """Test-only helper: pre-populate a local position with an explicit
        timestamp, bypassing set_position_ms's "now" so recency-comparison
        tests can control which side is newer."""
        self._data[asin] = position_ms
        self._updated_at[asin] = updated_at


class FakeSettings:
    def __init__(self, *args, **kwargs):
        self.library_sort_key = "recent"
        self.library_filter_key = "all"
        self.progress_display_mode = "percent"
        # PlayerScreen surface -- LibraryScreen now passes its Settings
        # straight through to the player rather than the player building its
        # own, so one fake has to cover both.
        self.playback_speed = 1.0
        self.playback_volume = 100.0

    def set_library_sort_key(self, key):
        self.library_sort_key = key

    def set_library_filter_key(self, key):
        self.library_filter_key = key

    def set_progress_display_mode(self, mode):
        self.progress_display_mode = mode

    def set_playback_speed(self, speed):
        self.playback_speed = speed

    def set_playback_volume(self, volume):
        self.playback_volume = volume


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
        get_library_exc=None, license_acr="", license_id="", license_exc=None,
        license_last_position_ms=0, license_last_position_updated_at=None,
        push_position_exc=None, set_finished_exc=None,
        push_listening_session_exc=None,
    ):
        self._books = books
        self.client = FakeAudibleClient(annotations_response)
        self.license_calls = []
        self.chapter_calls = []
        self._chapters = chapters if chapters is not None else []
        self._chapters_exc = chapters_exc
        self._get_library_exc = get_library_exc
        self._license_acr = license_acr
        self._license_id = license_id
        self._license_exc = license_exc
        self._license_last_position_ms = license_last_position_ms
        self._license_last_position_updated_at = license_last_position_updated_at
        self._push_position_exc = push_position_exc
        self._set_finished_exc = set_finished_exc
        self._push_listening_session_exc = push_listening_session_exc
        self.push_position_calls = []
        self.set_finished_calls = []
        self.push_listening_session_calls = []

    def get_library(self):
        if self._get_library_exc is not None:
            raise self._get_library_exc
        return list(self._books)

    def get_license(self, asin, quality="high"):
        self.license_calls.append((asin, quality))
        if self._license_exc is not None:
            raise self._license_exc
        return License(
            asin=asin, content_url="https://cdn/x", codec="AAXC", key="k", iv="i",
            acr=self._license_acr, license_id=self._license_id,
            last_position_ms=self._license_last_position_ms,
            last_position_updated_at=self._license_last_position_updated_at,
        )

    def push_last_position(self, asin, acr, position_ms):
        self.push_position_calls.append((asin, acr, position_ms))
        if self._push_position_exc is not None:
            raise self._push_position_exc

    def set_finished(self, asin, finished):
        self.set_finished_calls.append((asin, finished))
        if self._set_finished_exc is not None:
            raise self._set_finished_exc

    def push_listening_session(
        self, asin, license_id, start_position_ms, end_position_ms,
        start_time, end_time, duration_ms, narration_speed, delivery_type,
    ):
        self.push_listening_session_calls.append(
            (asin, license_id, start_position_ms, end_position_ms,
             start_time, end_time, duration_ms, narration_speed, delivery_type)
        )
        if self._push_listening_session_exc is not None:
            raise self._push_listening_session_exc

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
    return Book(
        asin=asin, title=title, authors=authors or [], series=series, runtime_min=runtime_min
    )


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
    ~/.local/share/voxcodex/library_cache.json. `to_return` is what
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


class FakeChapterCache:
    """Stands in for services.chapter_cache -- never touches the real
    ~/.local/share/voxcodex/chapter_cache.json. `to_return` seeds what
    `load()` answers with; defaults to "no cache exists yet"."""

    def __init__(self):
        self.save_calls = []
        self.to_return: dict = {}

    def save(self, chapters_by_asin):
        self.save_calls.append(dict(chapters_by_asin))

    def load(self):
        return self.to_return


@pytest.fixture(autouse=True)
def _fake_chapter_cache(monkeypatch):
    instance = FakeChapterCache()
    monkeypatch.setattr(library_module, "chapter_cache", instance)
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


async def test_successful_but_empty_fetch_does_not_overwrite_the_offline_cache(
    _fake_library_cache,
):
    """L22: a transient backend quirk returning an empty first page (not
    "this library is empty") must not destroy a good offline cache with
    nothing."""
    api = FakeAPI([])  # a successful call, just zero items

    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._books == [])
        assert _fake_library_cache.save_calls == []


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

        await _wait_until(lambda: [b.asin for b in screen._filtered] == ["B2"])


async def test_search_debounces_rather_than_filtering_on_every_keystroke(monkeypatch):
    """L5: typing used to clear-and-rebuild the whole table once per
    keystroke. 8 Input.Changed events landing inside one debounce window
    should coalesce into a single _apply_filters_and_sort call, not eight.

    Fires _search_changed directly and synchronously (no `await` between
    calls) rather than via real Pilot keystrokes: real keystrokes go
    through the actual event loop, so how many land inside one 150ms
    debounce window depends on how fast the machine running the test is --
    fine on a quiet dev machine, but this flaked on a loaded CI runner.
    Firing the handler directly removes that dependency on wall-clock
    timing entirely while still exercising the same stop-and-reset timer
    logic. test_search_filters_by_title / _by_author already cover that
    real typing eventually produces the right filtered result."""
    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    apply_calls = 0
    original_apply = screen._apply_filters_and_sort

    def _counting_apply():
        nonlocal apply_calls
        apply_calls += 1
        original_apply()

    monkeypatch.setattr(screen, "_apply_filters_and_sort", _counting_apply)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        apply_calls_after_load = apply_calls

        for _ in range(8):
            screen._search_changed(None)  # event argument is unused

        await _wait_until(lambda: apply_calls - apply_calls_after_load == 1)
        # Confirm it stays at 1 -- no further calls trickling in afterward.
        for _ in range(10):
            await asyncio.sleep(0.02)
        assert apply_calls - apply_calls_after_load == 1


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

        await _wait_until(lambda: [b.asin for b in screen._filtered] == ["B1"])


async def test_clear_search_restores_full_list():
    books = [_book("B1", "One"), _book("B2", "Two")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        await pilot.press(*"one")
        await _wait_until(lambda: len(screen._filtered) == 1)

        await pilot.press("escape")
        await _wait_until(lambda: len(screen._filtered) == 2)


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


async def test_down_arrow_from_search_moves_focus_to_the_table():
    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        assert isinstance(app.focused, Input)  # default focus, per the earlier finding

        await pilot.press("down")
        await pilot.pause()

        assert isinstance(app.focused, DataTable)


async def test_enter_in_search_also_moves_focus_to_the_table():
    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        await pilot.press(*"one")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.focused, DataTable)


async def test_space_plays_the_selected_book(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("space")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))


async def test_up_arrow_at_top_row_moves_focus_to_search():
    books = [_book("B1", "One"), _book("B2", "Two")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        table = screen.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        assert isinstance(app.focused, Input)


async def test_up_arrow_below_top_row_just_moves_the_cursor():
    books = [_book("B1", "One"), _book("B2", "Two")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        table = screen.query_one(DataTable)
        table.focus()
        table.move_cursor(row=1)
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        assert isinstance(app.focused, DataTable)
        assert table.cursor_row == 0


def test_local_column_is_labeled_downloaded_not_local():
    assert "Downloaded" in COLUMNS
    assert "Local" not in COLUMNS


def test_current_chapter_number_is_zero_for_a_not_started_book():
    chapters = [Chapter(title="Ch1", start_ms=0, length_ms=1000)]
    assert _current_chapter_number(chapters, position_ms=0) == 0


def test_current_chapter_number_is_one_once_actually_into_chapter_one():
    chapters = [Chapter(title="Ch1", start_ms=0, length_ms=1000)]
    assert _current_chapter_number(chapters, position_ms=1) == 1


def test_current_chapter_number_none_for_no_chapters():
    assert _current_chapter_number([], position_ms=0) is None


# -- progress resolution (M5) -------------------------------------------


def test_resolve_progress_ms_prefers_the_newer_timestamp_even_if_smaller():
    """The regression this guards: restarting a book from chapter 1 on
    another device must actually lower the position here, not get stuck at
    the old high-water mark forever."""
    result = _resolve_progress_ms(
        library_ms=500_000,
        local_ms=900_000, local_updated_at=1_000.0,
        remote_ms=100_000, remote_updated_at=2_000.0,  # newer
    )
    assert result == 100_000


def test_resolve_progress_ms_prefers_local_when_it_is_newer():
    result = _resolve_progress_ms(
        library_ms=500_000,
        local_ms=100_000, local_updated_at=2_000.0,  # newer
        remote_ms=900_000, remote_updated_at=1_000.0,
    )
    assert result == 100_000


def test_resolve_progress_ms_falls_back_to_remote_when_no_local_record():
    result = _resolve_progress_ms(
        library_ms=500_000,
        local_ms=0, local_updated_at=None,
        remote_ms=100_000, remote_updated_at=2_000.0,
    )
    assert result == 100_000


def test_resolve_progress_ms_falls_back_to_local_when_no_remote_record():
    result = _resolve_progress_ms(
        library_ms=500_000,
        local_ms=100_000, local_updated_at=2_000.0,
        remote_ms=0, remote_updated_at=None,
    )
    assert result == 100_000


def test_resolve_progress_ms_falls_back_to_library_value_when_neither_exists():
    result = _resolve_progress_ms(
        library_ms=500_000,
        local_ms=0, local_updated_at=None,
        remote_ms=0, remote_updated_at=None,
    )
    assert result == 500_000


# -- reached-end detection --------------------------------------------


def test_reached_end_true_at_and_past_the_finished_fraction():
    assert _reached_end(98_000, 100_000) is True
    assert _reached_end(100_000, 100_000) is True


def test_reached_end_false_before_the_finished_fraction():
    assert _reached_end(97_000, 100_000) is False


def test_reached_end_false_without_a_known_duration():
    assert _reached_end(0, 0) is False
    assert _reached_end(5000, 0) is False


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
    finished = Book(
        asin="B3", title="Finished", progress_ms=1000, duration_ms=1000, is_finished=True
    )
    screen = LibraryScreen(FakeAPI([not_started, in_progress, finished]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 3)
        screen.query_one(DataTable).focus()
        await pilot.press("f")
        await pilot.press("f")  # all -> downloaded -> in_progress
        await pilot.pause()

        assert [b.title for b in screen._filtered] == ["In progress"]


async def test_filter_finished_includes_a_book_at_98_percent_never_played_here():
    """L9: a book synced at 98% complete via the library API's own
    percent_complete (but never actually played to the end *in this app*,
    so is_finished is still False) used to fall through the "Finished"
    filter's `>= 100` check and land in "In progress" instead -- despite
    98% being this app's own definition of finished everywhere else
    (_reached_end / _FINISHED_FRACTION)."""
    almost_done = Book(asin="B1", title="Almost done", progress_ms=980, duration_ms=1000)
    screen = LibraryScreen(FakeAPI([almost_done]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("f")
        await pilot.press("f")
        await pilot.press("f")  # all -> downloaded -> in_progress -> finished
        await pilot.pause()

        assert [b.title for b in screen._filtered] == ["Almost done"]


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

        await _wait_until(
            lambda: [b.title for b in screen._filtered] == ["Wanted Downloaded"]
        )


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


# -- progress display -----------------------------------------------------


async def test_progress_column_defaults_to_percent():
    book = _book("B1", "One")
    book.progress_ms, book.duration_ms = 3600_000, 7200_000  # 50%, 1h left
    screen = LibraryScreen(FakeAPI([book]))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        assert screen._progress_cell(book) == "50%"


async def test_cycle_progress_display_to_time_left(_fake_settings):
    book = _book("B1", "One")
    book.progress_ms, book.duration_ms = 3600_000, 7200_000  # 50%, 1h left
    screen = LibraryScreen(FakeAPI([book]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("t")  # percent -> time_left
        await pilot.pause()

        assert screen._progress_cell(book) == "1h left"
        assert _fake_settings.progress_display_mode == "time_left"


async def test_cycle_progress_display_to_both():
    book = _book("B1", "One")
    book.progress_ms, book.duration_ms = 3600_000, 7200_000
    screen = LibraryScreen(FakeAPI([book]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("t")
        await pilot.press("t")  # percent -> time_left -> both
        await pilot.pause()

        assert screen._progress_cell(book) == "50% (1h left)"


async def test_progress_display_mode_is_restored_from_settings(_fake_settings):
    _fake_settings.progress_display_mode = "both"
    screen = LibraryScreen(FakeAPI([]))
    assert screen._progress_display == "both"


async def test_finished_marker_appended_regardless_of_display_mode():
    book = _book("B1", "One")
    book.progress_ms, book.duration_ms, book.is_finished = 7200_000, 7200_000, True
    screen = LibraryScreen(FakeAPI([book]))

    assert screen._progress_cell(book) == "100% ✓"


# -- download ---------------------------------------------------------------


async def test_download_skipped_when_already_downloaded(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    download_calls = []
    monkeypatch.setattr(
        library_module.download, "download_book",
        lambda book, api, on_progress=None, cancel_check=None: download_calls.append(book.asin),
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


async def test_download_rejects_a_same_book_double_press(monkeypatch):
    """M7: a same-book double-press races two download_book() calls
    against each other -- _do_download's exclusive=True only flags the
    older worker cancelled, it doesn't stop its thread synchronously.
    action_download_selected now rejects a repeat press outright."""
    started = threading.Event()
    release = threading.Event()
    download_calls = []

    def blocking_download_book(book, api, on_progress=None, cancel_check=None):
        download_calls.append(book.asin)
        started.set()
        release.wait(timeout=5)
        return "/tmp/fake.aaxc"

    monkeypatch.setattr(library_module.download, "download_book", blocking_download_book)

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    try:
        async with app.run_test() as pilot:
            await _wait_until(lambda: len(screen._books) == 1)
            screen.query_one(DataTable).focus()
            await pilot.press("d")
            await _wait_until(lambda: started.is_set())

            await pilot.press("d")
            await pilot.pause()

            assert download_calls == ["B1"]  # not a second call
            assert "Already downloading" in str(screen.query_one("#status").content)
    finally:
        release.set()  # let the blocked worker thread finish promptly


async def test_download_completing_after_being_superseded_does_not_clobber_the_new_one(
    monkeypatch,
):
    """L20: switching to a different download mid-flight -- exclusive=True
    only flags the old worker cancelled, it doesn't stop its thread
    synchronously -- must not let the old download's own completion hide
    the new download's progress bar or post a stale status for itself
    while the new one is still running invisibly."""
    release_a = threading.Event()
    entered_a = threading.Event()
    release_b = threading.Event()
    entered_b = threading.Event()

    def blocking_download_book(book, api, on_progress=None, cancel_check=None):
        if book.asin == "A":
            entered_a.set()
            release_a.wait(timeout=5)
            return "/tmp/fake-a.aaxc"
        entered_b.set()
        release_b.wait(timeout=5)
        return "/tmp/fake-b.aaxc"

    monkeypatch.setattr(library_module.download, "download_book", blocking_download_book)

    book_a = _book("A", "Book A")
    book_b = _book("B", "Book B")
    screen = LibraryScreen(FakeAPI([book_a, book_b]))
    app = HostApp(screen)

    try:
        async with app.run_test() as pilot:
            await _wait_until(lambda: len(screen._books) == 2)
            table = screen.query_one(DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")  # start downloading A -- blocks
            await _wait_until(lambda: entered_a.is_set())

            table.move_cursor(row=1)
            await pilot.press("d")  # switch to downloading B -- also blocks
            await _wait_until(lambda: entered_b.is_set())
            assert "Downloading: Book B" in str(screen.query_one("#status").content)

            release_a.set()  # let A's now-superseded download finish
            await _wait_until(lambda: "A" not in screen._in_flight_downloads)

            # A's completion must not have touched the shared bar/status --
            # those still belong to B, which is still genuinely in flight.
            assert "Downloading: Book B" in str(screen.query_one("#status").content)
            assert screen.query_one("#download-progress", ProgressBar).display is True
            # The per-book state update is real and must still have happened.
            assert book_a.is_downloaded is True

            release_b.set()  # now let B finish -- its own completion must land
            await _wait_until(
                lambda: "Downloaded: Book B" in str(screen.query_one("#status").content)
            )
            assert screen.query_one("#download-progress", ProgressBar).display is False
    finally:
        release_a.set()
        release_b.set()


async def test_download_succeeded_recomputes_the_active_filter():
    """M13: _download_succeeded used to call _refresh_table() (re-renders
    self._filtered as it was last computed) instead of
    _apply_filters_and_sort() (recomputes it from self._books) -- so a
    download that completes while filtered to "Downloaded" left the
    newly-downloaded book invisible: the row count stayed at 0 even though
    the download succeeded."""
    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        screen._filter_key = "downloaded"
        screen._apply_filters_and_sort()
        assert screen._filtered == []  # not downloaded yet

        screen._download_succeeded(screen._books[0])

        assert [b.title for b in screen._filtered] == ["One"]


async def test_download_success_updates_status_and_table(monkeypatch):
    download_calls = []

    def fake_download_book(book, api, on_progress=None, cancel_check=None):
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


async def test_download_completing_after_a_refresh_still_marks_the_current_book(
    monkeypatch,
):
    """L21: a library refresh completing mid-download replaces
    self._books wholesale with fresh Book objects -- the download's
    completion handler used to mutate the closure-captured (now orphaned)
    Book, which had no effect on what's displayed until the next manual
    refresh."""
    release = threading.Event()
    entered = threading.Event()

    def blocking_download_book(book, api, on_progress=None, cancel_check=None):
        entered.set()
        release.wait(timeout=5)
        return "/tmp/fake.aaxc"

    monkeypatch.setattr(library_module.download, "download_book", blocking_download_book)

    orphaned_book = _book("B1", "One")
    screen = LibraryScreen(FakeAPI([orphaned_book]))
    app = HostApp(screen)

    try:
        async with app.run_test() as pilot:
            await _wait_until(lambda: len(screen._books) == 1)
            screen.query_one(DataTable).focus()
            await pilot.press("d")
            await _wait_until(lambda: entered.is_set())

            # A library refresh completing mid-download: a fresh Book
            # object for the same title replaces the one the download
            # started with.
            fresh_book = _book("B1", "One")
            screen._books = [fresh_book]
            screen._apply_filters_and_sort()

            release.set()
            await _wait_until(lambda: fresh_book.is_downloaded is True)
            assert orphaned_book.is_downloaded is False  # untouched, as expected
    finally:
        release.set()


async def test_download_failure_shows_error_and_book_stays_not_downloaded(monkeypatch):
    def fake_download_book(book, api, on_progress=None, cancel_check=None):
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


async def test_download_can_be_retried_after_a_failure(monkeypatch):
    """The in-flight guard must release on failure, not just success --
    otherwise a failed download would be permanently unretriable."""
    download_calls = []

    def fake_download_book(book, api, on_progress=None, cancel_check=None):
        download_calls.append(book.asin)
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

        await pilot.press("d")
        await pilot.pause()

        assert download_calls == ["B1", "B1"]
        assert "Already downloading" not in str(screen.query_one("#status").content)


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


async def test_delete_confirmed_updates_the_total_downloaded_size_label(monkeypatch):
    """M13: the size label used to go stale after a delete -- it kept
    reading the pre-deletion total because the handler called
    _refresh_table() instead of _apply_filters_and_sort() (which is what
    actually recomputes _update_sort_filter_label's total)."""
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    monkeypatch.setattr(library_module.download, "delete_download", lambda asin: None)
    monkeypatch.setattr(
        library_module.download, "downloaded_size", lambda asin: 245 * 1024 * 1024
    )

    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    books[0].is_downloaded = True
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        assert "245 MB downloaded" in str(screen.query_one("#sort-filter").content)

        screen.query_one(DataTable).focus()
        await pilot.press("x")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()

        assert "downloaded" not in str(screen.query_one("#sort-filter").content)


# -- download size column, total, and bulk cleanup (L13) --------------------


def test_format_size_formats_bytes_kb_mb_gb():
    from voxcodex.screens.library import _format_size

    assert _format_size(500) == "500 B"
    assert _format_size(2_048) == "2 KB"
    assert _format_size(5 * 1024 * 1024) == "5 MB"
    assert _format_size(int(1.5 * 1024 * 1024 * 1024)) == "1.5 GB"


async def test_size_column_shows_size_for_downloaded_books_blank_otherwise(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: asin == "B1")
    monkeypatch.setattr(
        library_module.download, "downloaded_size",
        lambda asin: 245 * 1024 * 1024 if asin == "B1" else None,
    )
    books = [_book("B1", "Downloaded"), _book("B2", "Not downloaded")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 2)

        from textual.coordinate import Coordinate

        size_column = COLUMNS.index("Size")
        table = screen.query_one(DataTable)
        assert table.get_cell_at(Coordinate(0, size_column)) == "245 MB"
        assert table.get_cell_at(Coordinate(1, size_column)) == ""


async def test_sort_filter_label_shows_total_downloaded_size(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: asin in ("B1", "B2"))
    sizes = {"B1": 1024 * 1024 * 1024, "B2": 512 * 1024 * 1024}
    monkeypatch.setattr(
        library_module.download, "downloaded_size", lambda asin: sizes.get(asin)
    )
    books = [_book("B1", "One"), _book("B2", "Two"), _book("B3", "Three")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 3)

        label = str(screen.query_one("#sort-filter").content)
        assert "1.5 GB downloaded" in label


async def test_sort_filter_label_omits_size_when_nothing_downloaded():
    books = [_book("B1", "One")]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)

        label = str(screen.query_one("#sort-filter").content)
        assert "downloaded" not in label


async def test_delete_finished_downloads_requires_confirmation_and_removes_matching(
    monkeypatch,
):
    monkeypatch.setattr(
        library_module.download, "is_downloaded", lambda asin: asin in ("B1", "B2")
    )
    monkeypatch.setattr(
        library_module.download, "downloaded_size", lambda asin: 100 * 1024 * 1024
    )
    delete_calls = []
    monkeypatch.setattr(
        library_module.download, "delete_download", lambda asin: delete_calls.append(asin)
    )

    finished_downloaded = _book("B1", "Finished, downloaded")
    finished_downloaded.is_downloaded = True
    finished_downloaded.is_finished = True
    in_progress_downloaded = _book("B2", "In progress, downloaded")
    in_progress_downloaded.is_downloaded = True
    finished_not_downloaded = _book("B3", "Finished, not downloaded")
    finished_not_downloaded.is_finished = True

    books = [finished_downloaded, in_progress_downloaded, finished_not_downloaded]
    screen = LibraryScreen(FakeAPI(books))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 3)
        screen.query_one(DataTable).focus()
        await pilot.press("X")
        await pilot.pause()

        # A confirmation modal should be blocking -- nothing deleted yet.
        assert delete_calls == []

        await pilot.click("#yes")
        await pilot.pause()

        assert delete_calls == ["B1"]
        assert finished_downloaded.is_downloaded is False
        assert in_progress_downloaded.is_downloaded is True  # untouched: not finished
        assert "Removed 1 finished download (100 MB)" in str(
            screen.query_one("#status").content
        )
        # M13: the size label must reflect the deletion (100 MB left, not
        # the pre-deletion 200 MB) -- it used to go stale here because the
        # handler called _refresh_table() instead of _apply_filters_and_sort().
        assert "100 MB downloaded" in str(screen.query_one("#sort-filter").content)


async def test_delete_finished_downloads_one_failure_does_not_stop_the_rest(monkeypatch):
    """L6: one book's delete failing (a permissions error, a TOCTOU race
    with another instance or action_delete_selected) must not abort the
    rest of the batch."""
    monkeypatch.setattr(
        library_module.download, "is_downloaded", lambda asin: asin in ("B1", "B2")
    )
    monkeypatch.setattr(library_module.download, "downloaded_size", lambda asin: 1024)

    def flaky_delete(asin):
        if asin == "B1":
            raise OSError("permission denied")

    delete_calls = []
    monkeypatch.setattr(
        library_module.download, "delete_download",
        lambda asin: (delete_calls.append(asin), flaky_delete(asin))[1],
    )

    book1 = _book("B1", "One")
    book1.is_downloaded = True
    book1.is_finished = True
    book2 = _book("B2", "Two")
    book2.is_downloaded = True
    book2.is_finished = True
    screen = LibraryScreen(FakeAPI([book1, book2]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 2)
        screen.query_one(DataTable).focus()
        await pilot.press("X")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()

        assert delete_calls == ["B1", "B2"]  # both attempted
        assert book1.is_downloaded is True  # the failed one, untouched
        assert book2.is_downloaded is False  # the rest still succeeded
        assert "1 failed" in str(screen.query_one("#status").content)


async def test_delete_finished_downloads_cancelled_removes_nothing(monkeypatch):
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    monkeypatch.setattr(library_module.download, "downloaded_size", lambda asin: 1024)
    delete_calls = []
    monkeypatch.setattr(
        library_module.download, "delete_download", lambda asin: delete_calls.append(asin)
    )

    book = _book("B1", "One")
    book.is_downloaded = True
    book.is_finished = True
    screen = LibraryScreen(FakeAPI([book]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("X")
        await pilot.pause()
        await pilot.click("#no")
        await pilot.pause()

        assert delete_calls == []
        assert screen._books[0].is_downloaded is True


async def test_delete_finished_downloads_is_a_no_op_when_none_match():
    book = _book("B1", "One")  # not downloaded, not finished
    screen = LibraryScreen(FakeAPI([book]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("X")
        await pilot.pause()

        assert "No finished downloads to remove" in str(screen.query_one("#status").content)


# -- unmark finished (L3) ---------------------------------------------------


async def test_unmark_finished_clears_the_flag_and_pushes_the_change():
    book = _book("B1", "One")
    book.is_finished = True
    api = FakeAPI([book])
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("u")

        await _wait_until(lambda: api.set_finished_calls == [("B1", False)])
        assert screen._books[0].is_finished is False
        assert "Unmarked as finished" in str(screen.query_one("#status").content)


async def test_unmark_finished_recomputes_the_active_filter():
    """M13: action_unmark_finished used to call _refresh_table() instead of
    _apply_filters_and_sort() -- so with the filter set to "Finished", an
    unmarked book stayed visible in that filtered view until some other
    action recomputed it."""
    book = _book("B1", "One")
    book.is_finished = True
    api = FakeAPI([book])
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen._filter_key = "finished"
        screen._apply_filters_and_sort()
        assert screen._filtered == [book]

        screen.query_one(DataTable).focus()
        await pilot.press("u")
        await _wait_until(lambda: api.set_finished_calls == [("B1", False)])

        assert screen._filtered == []


async def test_unmark_finished_makes_the_book_reachable_by_in_progress_filter():
    """M15: clearing is_finished alone left a book still at ~100% progress
    matching only the "Finished" filter (is_finished OR pct >=
    _FINISHED_PCT) -- not "In progress", not "Not started" -- so the state
    was permanently unreachable by any further keypress, and a second `u`
    was a no-op since the flag was already clear."""
    book = _book("B1", "One")
    book.is_finished = True
    book.progress_ms = 1000
    book.duration_ms = 1000  # mis-fired at ~100%, per the "u" feature's purpose
    api = FakeAPI([book])
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("u")
        await _wait_until(lambda: api.set_finished_calls == [("B1", False)])

        assert book.is_finished is False
        assert book.progress_pct < _FINISHED_PCT

        screen._filter_key = "in_progress"
        screen._apply_filters_and_sort()
        assert screen._filtered == [book]

        screen._filter_key = "finished"
        screen._apply_filters_and_sort()
        assert screen._filtered == []


async def test_unmark_finished_is_a_no_op_when_not_finished():
    book = _book("B1", "One")
    api = FakeAPI([book])
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("u")
        await pilot.pause()

        assert api.set_finished_calls == []
        assert screen._books[0].is_finished is False


async def test_unmark_finished_shows_a_status_when_the_push_fails():
    book = _book("B1", "One")
    book.is_finished = True
    api = FakeAPI([book], set_finished_exc=RuntimeError("boom"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("u")

        await _wait_until(
            lambda: "Un-finished status saved locally; Audible sync failed"
            in str(screen.query_one("#status").content)
        )
        assert screen._books[0].is_finished is False  # local state still updated


# -- playback / chapters -------------------------------------------------


class _FakeMpvPlayer:
    """Stands in for MpvPlayer so `p` never spawns a real mpv subprocess."""

    volume = 100.0
    position_seconds = 0.0
    duration_seconds = 0.0
    paused = False
    eof_reached = False

    def start(self, source, key, iv, start_seconds=0.0):
        self.start_seconds = start_seconds

    def stop(self):
        pass

    def set_speed(self, speed):
        pass

    def set_volume(self, volume):
        pass

    @property
    def is_running(self):
        return True


async def test_playback_progress_is_persisted_when_the_player_is_closed(monkeypatch):
    """The player hands its final position back to LibraryScreen, which
    writes it to the progress store and updates the in-memory Book."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    screen = LibraryScreen(FakeAPI([book]))
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")
        await pilot.pause()

    assert screen.progress_store.get_position_ms("B1") == 512_000
    assert book.progress_ms == 512_000


# -- license position at play time (L2) -------------------------------------


async def test_play_uses_the_license_position_when_it_is_newer_than_local(monkeypatch):
    """License.last_position_ms is fetched fresh at the moment of playback
    -- more authoritative than whatever the library table showed from the
    last full load. A book never played in this app (no local record at
    all) should still pick it up."""
    from voxcodex.screens import player_screen as player_screen_module

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    book = _book("B1", "One")
    book.progress_ms = 0  # what the library table showed at load time
    api = FakeAPI(
        [book],
        license_last_position_ms=250_000,
        license_last_position_updated_at=time.time(),
    )
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")

        await _wait_until(lambda: book.progress_ms == 250_000)


async def test_play_keeps_the_newer_local_position_over_an_older_license_position(
    monkeypatch,
):
    from voxcodex.screens import player_screen as player_screen_module

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    book = _book("B1", "One")
    book.progress_ms = 0
    api = FakeAPI(
        [book],
        license_last_position_ms=900_000,
        license_last_position_updated_at=1_000.0,  # older
    )
    screen = LibraryScreen(api)
    screen.progress_store.seed("B1", 100_000, updated_at=time.time())  # newer
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")

        await _wait_until(lambda: len(api.license_calls) == 1)
        for _ in range(10):
            await asyncio.sleep(0.02)
        assert book.progress_ms == 100_000


# -- sync-failure status (M6) ----------------------------------------------


async def test_playback_close_shows_a_status_when_the_position_push_fails(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    api = FakeAPI([book], license_acr="CR!ABC", push_position_exc=RuntimeError("token expired"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")

        await _wait_until(
            lambda: "Audible sync failed" in str(screen.query_one("#status").content)
        )
        assert api.push_position_calls == [("B1", "CR!ABC", 512_000)]


async def test_playback_close_shows_no_status_when_the_position_push_succeeds(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    api = FakeAPI([book], license_acr="CR!ABC")
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")
        await _wait_until(lambda: api.push_position_calls == [("B1", "CR!ABC", 512_000)])

        for _ in range(10):
            await asyncio.sleep(0.02)
        assert "sync failed" not in str(screen.query_one("#status").content)


async def test_playback_close_does_not_warn_when_there_is_no_acr_to_push_with(monkeypatch):
    """A voucher saved before `acr` existed is a known compatibility gap,
    not a sync failure -- nothing was attempted, so nothing should warn."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    api = FakeAPI([book])  # license_acr defaults to ""
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")
        await pilot.pause()

        for _ in range(10):
            await asyncio.sleep(0.02)
        assert api.push_position_calls == []
        assert "sync failed" not in str(screen.query_one("#status").content)


async def test_playback_close_shows_a_status_when_the_finished_push_fails(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 995.0  # past the 0.98 finished threshold
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    book.duration_ms = 1_000_000  # matches PlayingMpv's duration_seconds so _reached_end fires
    api = FakeAPI([book], license_acr="CR!ABC", set_finished_exc=RuntimeError("boom"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")

        await _wait_until(
            lambda: "Finished status saved locally; Audible sync failed"
            in str(screen.query_one("#status").content)
        )
        assert api.set_finished_calls == [("B1", True)]


# -- listening-session push (mid-book percent_complete sync) ---------------


async def test_playback_close_pushes_a_listening_session(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    book.progress_ms = 100_000
    book.duration_ms = 1_000_000
    api = FakeAPI([book], license_id="lic-abc")
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")

        await _wait_until(lambda: api.push_listening_session_calls != [])

    (call,) = api.push_listening_session_calls
    (asin, license_id, start_pos, end_pos, start_time, end_time,
     duration_ms, speed, delivery) = call
    assert asin == "B1"
    assert license_id == "lic-abc"
    assert start_pos == 100_000
    assert end_pos == 512_000
    assert end_time >= start_time
    assert duration_ms == 1_000_000
    assert speed == 1.0
    assert delivery == "Streaming"


async def test_playback_close_reports_download_as_the_delivery_type(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    book.is_downloaded = True
    book.duration_ms = 1_000_000
    api = FakeAPI([book], license_id="lic-abc")
    screen = LibraryScreen(api)
    app = HostApp(screen)
    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)
    monkeypatch.setattr(
        library_module.download, "load_voucher",
        lambda asin: {"key": "k", "iv": "i", "acr": "CR!ABC", "license_id": "lic-abc"},
    )

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")

        await _wait_until(lambda: api.push_listening_session_calls != [])

    (call,) = api.push_listening_session_calls
    assert call[8] == "Download"


async def test_playback_close_starts_the_session_at_zero_for_a_finished_book(monkeypatch):
    """PlayerScreen.on_mount restarts a finished book from 0 rather than
    resuming -- the listening session reported at close must start from the
    same point, not the stale progress_ms."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 60.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    book.is_finished = True
    book.progress_ms = 900_000  # stale -- should not be used as the session start
    book.duration_ms = 1_000_000
    api = FakeAPI([book], license_id="lic-abc")
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")

        await _wait_until(lambda: api.push_listening_session_calls != [])

    (call,) = api.push_listening_session_calls
    assert call[2] == 0  # start position
    assert call[3] == 60_000  # end position


# -- resuming a finished book un-finishes it (session tile stays stale --
# a zero-length reset push was tried live and confirmed a server-side
# no-op, see AudibleAPI.push_listening_session's docstring) -------------


async def test_resuming_a_finished_book_clears_the_flag_immediately(monkeypatch):
    """The un-finish must happen the moment playback starts, not deferred
    to close -- otherwise another device checked mid-session would still
    see "Finished"."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    book = _book("B1", "One")
    book.is_finished = True
    book.duration_ms = 1_000_000
    api = FakeAPI([book], license_id="lic-abc")
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))

        # Before closing the player at all -- this must already have fired.
        assert book.is_finished is False
        await _wait_until(lambda: api.set_finished_calls == [("B1", False)])

        # No listening-session push at open time -- only at close (see the
        # module docstring above for why a reset push isn't sent at all).
        assert api.push_listening_session_calls == []


async def test_resuming_a_finished_book_starts_mpv_at_zero_not_at_the_stale_progress(
    monkeypatch,
):
    """Regression test for H1 (docs/code-review-2026-09-21.html): a finished
    book's mpv start position must actually be 0, not just its is_finished
    flag flip -- _launch_player clears the flag before PlayerScreen is
    constructed, so PlayerScreen must not re-derive the start position from
    that (by-then-cleared) flag."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    book = _book("B1", "One")
    book.is_finished = True
    book.progress_ms = 900_000
    book.duration_ms = 1_000_000
    api = FakeAPI([book], license_id="lic-abc")
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)

        assert app.screen._player.start_seconds == 0.0


async def test_resuming_a_not_finished_book_does_not_touch_the_finished_flag(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    book = _book("B1", "One")
    api = FakeAPI([book], license_id="lic-abc")
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await pilot.pause()

        for _ in range(10):
            await asyncio.sleep(0.02)
        assert api.set_finished_calls == []
        assert api.push_listening_session_calls == []


async def test_playback_close_skips_the_listening_session_without_a_license_id(monkeypatch):
    """A voucher/license saved before `license_id` existed is a known
    compatibility gap, not a sync failure -- nothing should be attempted or
    warned about."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    api = FakeAPI([book])  # license_id defaults to ""
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")
        await pilot.pause()

        for _ in range(10):
            await asyncio.sleep(0.02)
        assert api.push_listening_session_calls == []
        assert "sync failed" not in str(screen.query_one("#status").content)


async def test_playback_close_shows_a_status_when_the_listening_session_push_fails(
    monkeypatch,
):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 512.0
        duration_seconds = 1000.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    api = FakeAPI(
        [book], license_id="lic-abc",
        push_listening_session_exc=RuntimeError("token expired"),
    )
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        await pilot.press("q")

        await _wait_until(
            lambda: "Audible sync failed" in str(screen.query_one("#status").content)
        )
        assert api.push_listening_session_calls != []


async def test_play_passes_fetched_chapters_to_the_player_screen(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

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
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    books = [_book("B1", "One")]
    api = FakeAPI(books, chapters_exc=httpx.HTTPError("metadata endpoint exploded"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))

        # Chapter navigation is degraded, not the whole play action.
        assert app.screen._chapters == []


async def test_play_still_works_when_chapter_metadata_is_non_json(monkeypatch):
    """M3: InvalidResponse (a non-JSON 200 from the metadata endpoint) is
    caught by _CHAPTER_FETCH_ERRORS the same way a network error already
    is -- chapter navigation degrades, playback still works."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    books = [_book("B1", "One")]
    api = FakeAPI(books, chapters_exc=InvalidResponse("metadata returned a non-JSON body"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))

        assert app.screen._chapters == []


async def test_chapter_column_advances_after_a_listening_session_closes(monkeypatch):
    """M16: _on_progress updated progress_ms and rebuilt the table on
    close, but never recomputed chapter_current -- so a book listened to
    well past chapter 1 still showed its pre-session chapter number (a
    real, non-placeholder value, so it doesn't fall back to blank) until
    the next full library refresh."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 70.0  # inside "Chapter 3" (starts at 65s)
        duration_seconds = 130.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    chapters = [
        Chapter(title="Chapter 1", start_ms=0, length_ms=5_000),
        Chapter(title="Chapter 2", start_ms=5_000, length_ms=60_000),
        Chapter(title="Chapter 3", start_ms=65_000, length_ms=65_000),
    ]
    book = _book("B1", "One")
    book.duration_ms = 130_000
    api = FakeAPI([book], chapters=chapters)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)
        assert screen._books[0].chapter_current == 0  # not started yet

        await pilot.press("q")
        await pilot.pause()

        await _wait_until(lambda: screen._books[0].chapter_current == 3)
        assert screen._books[0].chapter_total == 3


async def test_chapter_column_unchanged_when_the_chapter_fetch_never_succeeded(
    monkeypatch,
):
    """A transient chapter-fetch failure is deliberately never cached (see
    _open_player) -- closing the player must not regress an unrelated,
    already-known chapter_total/chapter_current to 0/None on the strength
    of that session's empty `chapters` list."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    class PlayingMpv(_FakeMpvPlayer):
        position_seconds = 70.0
        duration_seconds = 130.0

    monkeypatch.setattr(player_screen_module, "MpvPlayer", PlayingMpv)

    book = _book("B1", "One")
    book.duration_ms = 130_000
    book.chapter_total = 5  # from an earlier, successful fetch this session
    book.chapter_current = 2
    api = FakeAPI([book], chapters_exc=httpx.HTTPError("metadata endpoint exploded"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        await _wait_until(lambda: app.screen._player is not None)

        await pilot.press("q")
        await pilot.pause()
        for _ in range(10):
            await asyncio.sleep(0.02)

        assert screen._books[0].chapter_total == 5
        assert screen._books[0].chapter_current == 2


# -- narrowed exception handling on the license path (M7) -----------------


async def test_play_shows_an_error_instead_of_hanging_on_a_corrupt_voucher(
    monkeypatch, tmp_path
):
    """M14: load_voucher used to raise json.JSONDecodeError uncaught on a
    corrupt voucher (a realistic trigger: partial disk, a bad sync --
    atomic_write_text only protects the write itself, not later
    corruption), which wasn't in _PLAYER_OPEN_ERRORS -- the worker's
    exception was swallowed by exit_on_error=False, leaving the status
    line reading "Opening license for..." permanently with no error shown
    and no retry possible. Uses the real download.load_voucher (not a
    monkeypatched fake) against an actually-corrupted file on disk, so
    this exercises load_voucher's own fix, not just the library-screen
    wiring around it."""
    from voxcodex import config

    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "downloads")
    config.DOWNLOADS_DIR.mkdir(parents=True)
    library_module.download.voucher_path_for("B1").write_text("{not valid json")
    library_module.download.audio_path_for("B1").write_bytes(b"data")

    monkeypatch.setattr(library_module.download, "is_downloaded", lambda asin: True)

    books = [_book("B1", "One")]
    books[0].is_downloaded = True
    api = FakeAPI(books)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")

        await _wait_until(
            lambda: "Could not start playback" in str(screen.query_one("#status").content)
        )
        assert "missing or unreadable" in str(screen.query_one("#status").content)


async def test_play_shows_the_denial_message_when_the_license_is_denied():
    books = [_book("B1", "One")]
    api = FakeAPI(books, license_exc=LicenseDenied("Not entitled to this title"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")

        await _wait_until(
            lambda: "Not entitled to this title" in str(screen.query_one("#status").content)
        )


async def test_play_surfaces_a_network_error_from_get_license():
    books = [_book("B1", "One")]
    api = FakeAPI(books, license_exc=httpx.HTTPError("connection reset"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")

        await _wait_until(
            lambda: "Could not start playback" in str(screen.query_one("#status").content)
        )


async def test_play_surfaces_a_non_json_license_response_as_a_playback_failure():
    """M3: a non-JSON 200 (captive portal, proxy error page, Amazon
    maintenance page) used to raise an untyped TypeError indexing the raw
    text as a dict, escaping _PLAYER_OPEN_ERRORS and leaving the user with
    no message at all instead of "Could not start playback...".
    """
    books = [_book("B1", "One")]
    api = FakeAPI(books, license_exc=InvalidResponse("licenserequest returned a non-JSON body"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")

        await _wait_until(
            lambda: "Could not start playback" in str(screen.query_one("#status").content)
        )


async def test_play_does_not_mask_an_unexpected_bug_as_a_playback_failure(monkeypatch):
    """An exception type nobody anticipated (a real bug, not a known
    failure mode) must not be swallowed and relabeled -- it should
    propagate to the worker's error handler instead of quietly showing
    "Could not start playback" for something that isn't a license/network
    problem at all."""
    books = [_book("B1", "One")]
    api = FakeAPI(books, license_exc=ValueError("this is a bug, not a license failure"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await pilot.pause()

        for _ in range(10):
            await asyncio.sleep(0.02)
        assert "Could not start playback" not in str(screen.query_one("#status").content)


# -- worker group collisions (C2/M10) --------------------------------------


async def test_pressing_play_twice_quickly_opens_only_one_player_screen(monkeypatch):
    """_open_player runs with group="player", exclusive=True -- starting a
    second one is meant to cancel the first rather than have both race to
    push a PlayerScreen. Regression test for C2 (all three background
    workers used to share Textual's *default* group, so any one of them
    starting cancelled the others outright)."""
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    unblock_first_call = threading.Event()
    entered_calls = []

    class BlockingFirstCallAPI(FakeAPI):
        def get_license(self, asin, quality="high"):
            # The first call blocks until released below, standing in for a
            # slow network response -- long enough for a second "p" press to
            # land and start a second worker in the same exclusive group.
            # Recorded on entry (not via the base class's license_calls,
            # which only grows once a call actually returns) so the test can
            # detect that worker #1 has started, not just that it finished.
            is_first = len(entered_calls) == 0
            entered_calls.append((asin, quality))
            if is_first:
                unblock_first_call.wait(timeout=5)
            return super().get_license(asin, quality)

    book = _book("B1", "One")
    api = BlockingFirstCallAPI([book])
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        screen.query_one(DataTable).focus()

        await pilot.press("p")  # worker #1: enters get_license, blocks
        await _wait_until(lambda: len(entered_calls) == 1)
        await pilot.press("p")  # worker #2: exclusive=True cancels #1

        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))
        unblock_first_call.set()  # let worker #1 resume and hit is_cancelled
        for _ in range(10):
            await asyncio.sleep(0.02)

        assert len(api.license_calls) == 2  # both workers did call get_license...
        player_screens = [s for s in app.screen_stack if isinstance(s, PlayerScreen)]
        assert len(player_screens) == 1  # ...but only the second ever opened a player


async def test_library_load_fetches_chapter_counts_in_the_background():
    chapters = [
        Chapter(title="Ch1", start_ms=0, length_ms=1000),
        Chapter(title="Ch2", start_ms=1000, length_ms=1000),
        Chapter(title="Ch3", start_ms=2000, length_ms=1000),
    ]
    book = _book("B1", "One")
    book.progress_ms = 1500  # inside "Ch2" -> chapter 2 of 3
    screen = LibraryScreen(FakeAPI([book], chapters=chapters))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        await _wait_until(lambda: screen._books[0].chapter_total is not None)

        assert screen._books[0].chapter_total == 3
        assert screen._books[0].chapter_current == 2

        from textual.coordinate import Coordinate

        chapter_column = COLUMNS.index("Chapter")
        cell = screen.query_one(DataTable).get_cell_at(Coordinate(0, chapter_column))
        assert cell == "2/3"


async def test_background_chapter_fetch_does_not_disturb_current_selection():
    """Regression test: _refresh_table's table.clear() resets the cursor to
    row 0 -- fine when the rebuild follows directly from something you did
    to the selected row, but the background chapter fetch calls it on its
    own timer while you might be doing anything else. Caught originally by
    the up-arrow-at-non-top-row test unexpectedly landing on search."""
    books = [_book("B1", "One"), _book("B2", "Two"), _book("B3", "Three")]
    chapters = [Chapter(title="Ch1", start_ms=0, length_ms=1000)]
    screen = LibraryScreen(FakeAPI(books, chapters=chapters))
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 3)
        table = screen.query_one(DataTable)
        table.focus()
        table.move_cursor(row=2)  # select "Three"

        await _wait_until(lambda: all(b.chapter_total is not None for b in screen._books))

        assert table.cursor_row == 2
        assert screen._selected_book().asin == "B3"


async def test_chapter_fetch_failure_leaves_chapter_column_blank():
    api = FakeAPI([_book("B1", "One")], chapters_exc=httpx.HTTPError("boom"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        # Give the background worker a moment to have tried and failed.
        for _ in range(20):
            await asyncio.sleep(0.02)
        assert api.chapter_calls == ["B1"]
        assert screen._books[0].chapter_total is None


async def test_chapter_counts_are_cached_and_reused_without_a_second_fetch(monkeypatch):
    from voxcodex.screens import player_screen as player_screen_module
    from voxcodex.screens.player_screen import PlayerScreen

    monkeypatch.setattr(player_screen_module, "MpvPlayer", _FakeMpvPlayer)

    chapters = [Chapter(title="Ch1", start_ms=0, length_ms=60_000)]
    api = FakeAPI([_book("B1", "One")], chapters=chapters)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: len(screen._books) == 1)
        await _wait_until(lambda: screen._books[0].chapter_total is not None)

        screen.query_one(DataTable).focus()
        await pilot.press("p")
        await _wait_until(lambda: isinstance(app.screen, PlayerScreen))

        assert api.chapter_calls == ["B1"]  # not fetched again for playback


async def test_chapter_counts_seeded_from_disk_cache_skip_the_network_call(
    _fake_chapter_cache,
):
    chapters = [
        Chapter(title="Ch1", start_ms=0, length_ms=1000),
        Chapter(title="Ch2", start_ms=1000, length_ms=1000),
    ]
    _fake_chapter_cache.to_return = {"B1": chapters}
    book = _book("B1", "One")
    book.progress_ms = 1500  # inside "Ch2" -> chapter 2 of 2
    api = FakeAPI([book])
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        # Give the background worker a moment; there's nothing left to fetch.
        for _ in range(20):
            await asyncio.sleep(0.02)
        assert api.chapter_calls == []
        assert screen._books[0].chapter_total == 2
        assert screen._books[0].chapter_current == 2


async def test_offline_populate_does_not_fetch_chapter_counts(_fake_library_cache):
    book = _book("B1", "One")
    _fake_library_cache.to_return = ([book], 12345.0)
    api = FakeAPI([book], get_library_exc=RuntimeError("offline"))
    screen = LibraryScreen(api)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        for _ in range(20):
            await asyncio.sleep(0.02)
        assert api.chapter_calls == []  # never even tried while offline


async def test_chapter_count_fetch_batches_table_rebuilds(monkeypatch):
    """Regression test for the O(n) call_from_thread(_refresh_table) per
    book: a large library should rebuild the table a handful of times, not
    once per title."""
    books = [_book(f"B{i}", f"Title {i}") for i in range(60)]
    chapters = [Chapter(title="Ch1", start_ms=0, length_ms=1000)]
    api = FakeAPI(books, chapters=chapters)
    screen = LibraryScreen(api)
    app = HostApp(screen)

    refresh_calls = 0
    original_refresh = screen._refresh_table

    def _counting_refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        original_refresh()

    monkeypatch.setattr(screen, "_refresh_table", _counting_refresh)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 60)
        await _wait_until(lambda: all(b.chapter_total is not None for b in screen._books))

        # 60 books at a batch size of 25 -> at most 3 rebuilds from the
        # chapter-count worker, plus whatever _apply_filters_and_sort did on
        # load -- nowhere near one per book.
        assert refresh_calls <= 5


# -- last played externally -----------------------------------------------


def _annotations_response(records):
    return {"asin_last_position_heard_annots": records}


# -- progress merge on load (M5) -------------------------------------------


def _existing_at(asin, last_updated, position_ms):
    return {
        "asin": asin,
        "last_position_heard": {
            "status": "Exists", "position_ms": position_ms, "last_updated": last_updated,
        },
    }


async def test_library_load_prefers_the_newer_remote_position_over_a_larger_local_one():
    """Regression test for the plain-max() bug: restarting a book from
    chapter 1 on another device is a genuinely newer, lower position -- it
    must not lose to a higher position left over locally from before."""
    response = _annotations_response(
        [_existing_at("B1", "2026-08-27 08:56:11.849", position_ms=100_000)]
    )
    books = [_book("B1", "One")]
    api = FakeAPI(books, annotations_response=response)
    screen = LibraryScreen(api)
    screen.progress_store.seed("B1", 900_000, updated_at=1_000.0)  # older, larger
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        assert screen._books[0].progress_ms == 100_000


async def test_library_load_keeps_the_newer_local_position_over_a_larger_remote_one():
    response = _annotations_response(
        [_existing_at("B1", "2019-01-24 09:21:16.892", position_ms=900_000)]
    )
    books = [_book("B1", "One")]
    api = FakeAPI(books, annotations_response=response)
    screen = LibraryScreen(api)
    screen.progress_store.seed("B1", 100_000, updated_at=time.time())  # newer, smaller
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: len(screen._books) == 1)
        assert screen._books[0].progress_ms == 100_000
