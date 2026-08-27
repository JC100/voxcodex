"""Integration tests for PlayerScreen, driven through real keybindings
(Pilot) against a fake MpvPlayer -- no real mpv process involved.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App
from textual.widgets import Static

from audible_tui.models import Book
from audible_tui.screens import player_screen as player_screen_module
from audible_tui.screens.player_screen import PlayerScreen
from audible_tui.services.player import MpvError, MpvNotFoundError


class FakePlayer:
    def __init__(self):
        self.started_with = None
        self.stopped = False
        self.position = 0.0
        self.duration = 100.0
        self.paused_ = False
        self.eof = False
        self.seek_calls = []
        self.speed_calls = []
        self.toggle_pause_calls = 0

    def start(self, source, key, iv, start_seconds=0.0):
        self.started_with = (source, key, iv, start_seconds)

    @property
    def is_running(self):
        return not self.stopped

    def toggle_pause(self):
        self.toggle_pause_calls += 1
        self.paused_ = not self.paused_

    def seek_relative(self, seconds):
        self.seek_calls.append(seconds)

    def set_speed(self, speed):
        self.speed_calls.append(speed)

    @property
    def position_seconds(self):
        return self.position

    @property
    def duration_seconds(self):
        return self.duration

    @property
    def paused(self):
        return self.paused_

    @property
    def eof_reached(self):
        return self.eof

    def stop(self):
        self.stopped = True


class FailingPlayer:
    def __init__(self, exc):
        self._exc = exc

    def start(self, *args, **kwargs):
        raise self._exc


class HostApp(App):
    def __init__(self, screen, callback=None):
        super().__init__()
        self._screen = screen
        self._callback = callback

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._callback)


def _book(progress_ms=0, duration_ms=100_000, is_finished=False):
    return Book(
        asin="B1", title="Test Book", authors=["Author"],
        progress_ms=progress_ms, duration_ms=duration_ms, is_finished=is_finished,
    )


async def _wait_until(condition, timeout=2.0, step=0.02):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture()
def fake_player(monkeypatch):
    instance = FakePlayer()
    monkeypatch.setattr(player_screen_module, "MpvPlayer", lambda: instance)
    return instance


# -- startup --------------------------------------------------------------


async def test_start_player_uses_progress_ms_as_start_seconds(fake_player):
    book = _book(progress_ms=45_000)
    screen = PlayerScreen(book, "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: fake_player.started_with is not None)

        source, key, iv, start_seconds = fake_player.started_with
        assert source == "source-url"
        assert key == "key"
        assert iv == "iv"
        assert start_seconds == 45.0


async def test_start_player_restarts_from_zero_when_book_already_finished(fake_player):
    book = _book(progress_ms=90_000, is_finished=True)
    screen = PlayerScreen(book, "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: fake_player.started_with is not None)
        assert fake_player.started_with[3] == 0.0


async def test_state_shows_playing_after_successful_start(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(
            lambda: screen.query_one("#state", Static).content == "Playing"
        )


async def test_start_failure_shows_error_message(monkeypatch):
    monkeypatch.setattr(
        player_screen_module, "MpvPlayer",
        lambda: FailingPlayer(MpvNotFoundError("mpv was not found on PATH.")),
    )
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(
            lambda: "mpv was not found" in str(screen.query_one("#state", Static).content)
        )
        assert screen._player is None


async def test_start_failure_from_mpv_error_also_shown(monkeypatch):
    monkeypatch.setattr(
        player_screen_module, "MpvPlayer",
        lambda: FailingPlayer(MpvError("could not connect to socket")),
    )
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(
            lambda: "could not connect" in str(screen.query_one("#state", Static).content)
        )


# -- transport controls, via real keybindings ------------------------------


async def test_space_toggles_pause(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("space")
        await pilot.pause()

        assert fake_player.toggle_pause_calls == 1


async def test_left_seeks_back_10s(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("left")
        await pilot.pause()

        assert fake_player.seek_calls == [-10]


async def test_right_seeks_forward_30s(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("right")
        await pilot.pause()

        assert fake_player.seek_calls == [30]


async def test_shift_left_seeks_back_60s(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("shift+left")
        await pilot.pause()

        assert fake_player.seek_calls == [-60]


async def test_shift_right_seeks_forward_60s(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("shift+right")
        await pilot.pause()

        assert fake_player.seek_calls == [60]


async def test_up_increases_speed(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("up")
        await pilot.pause()

        assert fake_player.speed_calls == [1.1]


async def test_down_decreases_speed(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("down")
        await pilot.pause()

        assert fake_player.speed_calls == [0.9]


async def test_speed_up_clamps_at_3x(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        for _ in range(25):  # 1.0 + 25*0.1 would overshoot 3.0 without clamping
            await pilot.press("up")
        await pilot.pause()

        assert fake_player.speed_calls[-1] == 3.0


async def test_speed_down_clamps_at_half_x(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        for _ in range(10):  # 1.0 - 10*0.1 would overshoot 0.5 without clamping
            await pilot.press("down")
        await pilot.pause()

        assert fake_player.speed_calls[-1] == 0.5


async def test_actions_are_no_ops_before_player_has_started(monkeypatch):
    """If a transport key is pressed in the brief window before the mpv
    worker thread finishes starting, there's no player yet to control --
    these must no-op rather than raise (e.g. on a None _player)."""
    import threading

    release = threading.Event()

    class BlocksUntilReleased:
        def start(self, *a, **k):
            release.wait(timeout=5)  # released below; timeout is just a safety net

    monkeypatch.setattr(player_screen_module, "MpvPlayer", BlocksUntilReleased)
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    try:
        async with app.run_test() as pilot:
            assert screen._player is None
            await pilot.press("space")
            await pilot.press("left")
            await pilot.press("up")
            await pilot.pause()
            # must not have raised -- if it had, the test itself would error out
    finally:
        release.set()  # let the blocked worker thread finish promptly


# -- _tick ------------------------------------------------------------


async def test_tick_updates_last_position_and_time_row(fake_player):
    screen = PlayerScreen(_book(duration_ms=100_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 30.0
        fake_player.duration = 100.0

        screen._tick()

        assert screen._last_position_ms == 30_000
        assert "playing" in str(screen.query_one("#time-row", Static).content)


async def test_tick_falls_back_to_book_duration_when_mpv_reports_zero(fake_player):
    screen = PlayerScreen(_book(duration_ms=200_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 50.0
        fake_player.duration = 0.0  # mpv hasn't reported a duration yet

        screen._tick()

        bar = screen.query_one("#bar")
        # 50s of a 200s (book-reported) duration -> 25%
        assert bar.progress == 25


async def test_tick_shows_finished_state_on_eof(fake_player):
    screen = PlayerScreen(_book(duration_ms=100_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.eof = True

        screen._tick()

        assert screen.query_one("#state", Static).content == "Finished"


# -- close / dismiss ------------------------------------------------------


async def test_close_stops_player_and_dismisses_with_last_position(fake_player):
    results = []
    screen = PlayerScreen(_book(progress_ms=1_000), "source-url", "key", "iv")
    app = HostApp(screen, results.append)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 77.0
        screen._tick()

        await pilot.press("q")
        await pilot.pause()

        assert fake_player.stopped is True
        assert results == [77_000]


async def test_escape_also_closes(fake_player):
    results = []
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen, results.append)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("escape")
        await pilot.pause()

        assert fake_player.stopped is True
        assert len(results) == 1
