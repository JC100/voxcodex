"""Integration tests for PlayerScreen, driven through real keybindings
(Pilot) against a fake MpvPlayer -- no real mpv process involved.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App
from textual.widgets import Static

from voxcodex.models import Book
from voxcodex.screens import player_screen as player_screen_module
from voxcodex.screens.player_screen import PlayerScreen
from voxcodex.services.api import Chapter
from voxcodex.services.player import MpvError, MpvNotFoundError


class FakePlayer:
    def __init__(self):
        self.started_with = None
        self.stopped = False
        self.position = 0.0
        self.duration = 100.0
        self.paused_ = False
        self.eof = False
        self.volume_ = 100.0
        self.seek_calls = []
        self.seek_absolute_calls = []
        self.speed_calls = []
        self.volume_calls = []
        self.set_paused_calls = []
        self.toggle_pause_calls = 0

    def start(self, source, key, iv, start_seconds=0.0):
        self.started_with = (source, key, iv, start_seconds)

    @property
    def is_running(self):
        return not self.stopped

    def toggle_pause(self):
        self.toggle_pause_calls += 1
        self.paused_ = not self.paused_

    def set_paused(self, paused):
        self.set_paused_calls.append(paused)
        self.paused_ = paused

    def seek_relative(self, seconds):
        self.seek_calls.append(seconds)

    def seek_absolute(self, seconds):
        self.seek_absolute_calls.append(seconds)
        self.position = seconds

    def set_speed(self, speed):
        self.speed_calls.append(speed)

    def set_volume(self, volume):
        self.volume_calls.append(volume)
        self.volume_ = volume

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

    @property
    def volume(self):
        return self.volume_

    def stop(self):
        self.stopped = True


class FailingPlayer:
    def __init__(self, exc):
        self._exc = exc
        self.stopped = False

    def start(self, *args, **kwargs):
        raise self._exc

    def stop(self):
        self.stopped = True


class FakeSettings:
    """Stands in for services.settings.Settings -- avoids ever touching the
    real config dir, and lets tests assert on what got persisted."""

    def __init__(self, playback_speed=1.0, playback_volume=100.0):
        self.playback_speed = playback_speed
        self.playback_volume = playback_volume
        self.speed_calls = []
        self.volume_calls = []
        self.last_played_in_app_calls = []

    def set_playback_speed(self, speed):
        self.speed_calls.append(speed)
        self.playback_speed = speed

    def set_playback_volume(self, volume):
        self.volume_calls.append(volume)
        self.playback_volume = volume

    def set_last_played_in_app(self, asin):
        self.last_played_in_app_calls.append(asin)


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


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    instance = FakeSettings()
    monkeypatch.setattr(player_screen_module, "Settings", lambda: instance)
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


async def test_left_seeks_back_30s(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("left")
        await pilot.pause()

        assert fake_player.seek_calls == [-30]


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

        assert fake_player.speed_calls[-1] == 1.1


async def test_down_decreases_speed(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("down")
        await pilot.pause()

        assert fake_player.speed_calls[-1] == 0.9


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


# -- volume -----------------------------------------------------------


async def test_starts_at_persisted_speed_and_volume(fake_player, fake_settings):
    fake_settings.playback_speed = 1.3
    fake_settings.playback_volume = 82.0
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)

        assert screen._speed == 1.3
        assert screen._volume == 82.0
        assert fake_player.speed_calls == [1.3]
        assert fake_player.volume_calls == [82.0]


async def test_starting_playback_records_last_played_in_app(fake_player, fake_settings):
    book = _book()
    book.asin = "B42"
    screen = PlayerScreen(book, "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        assert fake_settings.last_played_in_app_calls == ["B42"]


async def test_bracket_right_increases_volume(fake_player, fake_settings):
    fake_settings.playback_volume = 50.0  # below the ceiling, so the increment is observable
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("]")
        await pilot.pause()

        assert fake_player.volume_calls[-1] == 55.0


async def test_bracket_left_decreases_volume(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("[")
        await pilot.pause()

        assert fake_player.volume_calls[-1] == 95.0


async def test_volume_up_clamps_at_100(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        for _ in range(3):  # 100 + 3*5 would overshoot 100 without clamping
            await pilot.press("]")
        await pilot.pause()

        assert fake_player.volume_calls[-1] == 100.0


async def test_volume_down_clamps_at_0(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        for _ in range(25):  # 100 - 25*5 would go well below 0 without clamping
            await pilot.press("[")
        await pilot.pause()

        assert fake_player.volume_calls[-1] == 0.0


# -- sleep timer --------------------------------------------------------


async def test_sleep_timer_starts_off():
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    assert screen._sleep_remaining_seconds is None


async def test_cycling_sleep_timer_sets_first_preset(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("s")
        await pilot.pause()

        assert screen._sleep_remaining_seconds == 15 * 60


async def test_cycling_sleep_timer_wraps_back_to_off(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        for _ in range(len(screen._SLEEP_PRESETS_MIN)):
            await pilot.press("s")
        await pilot.pause()

        assert screen._sleep_remaining_seconds is None


async def test_tick_counts_down_sleep_timer_while_playing(fake_player):
    screen = PlayerScreen(_book(duration_ms=1_000_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        screen._sleep_remaining_seconds = 20.0
        fake_player.paused_ = False

        screen._tick()

        assert screen._sleep_remaining_seconds == 19.0


async def test_tick_does_not_count_down_sleep_timer_while_paused(fake_player):
    screen = PlayerScreen(_book(duration_ms=1_000_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        screen._sleep_remaining_seconds = 20.0
        fake_player.paused_ = True

        screen._tick()

        assert screen._sleep_remaining_seconds == 20.0


async def test_sleep_timer_auto_pauses_playback_on_expiry(fake_player):
    screen = PlayerScreen(_book(duration_ms=1_000_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        screen._sleep_remaining_seconds = 1.0
        fake_player.paused_ = False

        screen._tick()

        assert fake_player.set_paused_calls == [True]
        assert screen._sleep_remaining_seconds is None
        assert screen._sleep_preset_index == 0
        assert "paused" in str(screen.query_one("#time-row").content)


async def test_time_row_shows_volume_and_sleep_countdown(fake_player):
    screen = PlayerScreen(_book(duration_ms=1_000_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        screen._volume = 65.0
        screen._sleep_remaining_seconds = 90.0  # 1:30
        fake_player.paused_ = True  # avoid the countdown ticking down mid-assertion

        screen._tick()

        text = str(screen.query_one("#time-row").content)
        assert "vol 65%" in text
        assert "sleep 1:30" in text


# -- chapters -----------------------------------------------------------


_CHAPTERS = [
    Chapter(title="Opening Credits", start_ms=0, length_ms=5_000),
    Chapter(title="Chapter 1", start_ms=5_000, length_ms=60_000),
    Chapter(title="Chapter 2", start_ms=65_000, length_ms=60_000),
]


async def test_chapter_row_blank_when_no_chapters(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 10.0
        screen._tick()

        assert str(screen.query_one("#chapter-row").content) == ""


async def test_chapter_row_shows_current_chapter(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv", chapters=_CHAPTERS)
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 10.0  # inside "Chapter 1" (starts at 5s, len 60s)
        screen._tick()

        assert (
            str(screen.query_one("#chapter-row").content)
            == "Chapter 2/3: Chapter 1   (0:05 / 1:00)"
        )


async def test_next_chapter_seeks_to_next_chapters_start(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv", chapters=_CHAPTERS)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 10.0  # in "Chapter 1"
        screen._tick()

        await pilot.press("n")
        await pilot.pause()

        assert fake_player.seek_absolute_calls == [65.0]  # start of "Chapter 2"


async def test_next_chapter_is_a_no_op_on_the_last_chapter(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv", chapters=_CHAPTERS)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 70.0  # in "Chapter 2", the last one
        screen._tick()

        await pilot.press("n")
        await pilot.pause()

        assert fake_player.seek_absolute_calls == []


async def test_previous_chapter_restarts_current_chapter_when_well_into_it(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv", chapters=_CHAPTERS)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 30.0  # well into "Chapter 1" (starts at 5s)
        screen._tick()

        await pilot.press("p")
        await pilot.pause()

        assert fake_player.seek_absolute_calls == [5.0]  # restart "Chapter 1"


async def test_previous_chapter_goes_back_a_chapter_when_near_the_start(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv", chapters=_CHAPTERS)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 66.0  # 1s into "Chapter 2" (starts at 65s)
        screen._tick()

        await pilot.press("p")
        await pilot.pause()

        assert fake_player.seek_absolute_calls == [5.0]  # back to "Chapter 1"


async def test_previous_chapter_on_first_chapter_just_restarts_it(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv", chapters=_CHAPTERS)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 1.0  # in the very first chapter
        screen._tick()

        await pilot.press("p")
        await pilot.pause()

        assert fake_player.seek_absolute_calls == [0.0]


async def test_chapter_navigation_is_a_no_op_without_chapters(fake_player):
    screen = PlayerScreen(_book(), "source-url", "key", "iv")  # no chapters
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        await pilot.press("n")
        await pilot.press("p")
        await pilot.pause()

        assert fake_player.seek_absolute_calls == []


async def test_actions_are_safe_while_player_is_still_starting(monkeypatch):
    """The player handle is published before the blocking start() so the
    screen can stop mpv if it closes mid-startup -- but that handle isn't
    connected yet, so a transport keypress in that window must be swallowed
    (a real MpvPlayer raises MpvError('Player is not running')) rather than
    crash the app."""
    import threading

    release = threading.Event()

    class BlocksUntilReleased:
        def __init__(self):
            self.stopped = False

        def start(self, *a, **k):
            release.wait(timeout=5)  # released below; timeout is just a safety net

        def stop(self):
            self.stopped = True

        def _not_running(self, *a, **k):
            raise MpvError("Player is not running")

        toggle_pause = seek_relative = set_speed = set_volume = _not_running

    monkeypatch.setattr(player_screen_module, "MpvPlayer", BlocksUntilReleased)
    screen = PlayerScreen(_book(), "source-url", "key", "iv")
    app = HostApp(screen)

    try:
        async with app.run_test() as pilot:
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


async def test_tick_shows_finished_when_mpv_has_exited(fake_player):
    """With --idle=once mpv quits at end-of-file, so a tick that finds the
    process gone should land on 'Finished' rather than stay on 'Playing'."""
    screen = PlayerScreen(_book(duration_ms=100_000), "source-url", "key", "iv")
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(
            lambda: screen.query_one("#state", Static).content == "Playing"
        )
        fake_player.stopped = True  # is_running -> False

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


# -- progress checkpointing (H3) ----------------------------------------------


async def test_progress_is_checkpointed_on_a_timer_during_playback(fake_player):
    saved = []
    screen = PlayerScreen(
        _book(duration_ms=1_000_000), "s", "k", "iv",
        on_progress=lambda pos, *, final: saved.append((pos, final)),
    )
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 123.0

        for _ in range(screen._CHECKPOINT_EVERY_TICKS):
            screen._tick()

        assert saved == [(123_000, False)]  # exactly one, and not "final"


async def test_periodic_checkpoint_is_skipped_when_position_has_not_moved(fake_player):
    saved = []
    screen = PlayerScreen(
        _book(progress_ms=10_000, duration_ms=1_000_000), "s", "k", "iv",
        on_progress=lambda pos, *, final: saved.append((pos, final)),
    )
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 10.0  # right where the book was already left

        for _ in range(screen._CHECKPOINT_EVERY_TICKS * 2):
            screen._tick()

        assert saved == []


async def test_final_progress_is_flushed_on_unmount_even_without_an_explicit_close(fake_player):
    """A ctrl+q / closed terminal never runs action_close -- on_unmount is
    the backstop so the session isn't lost."""
    saved = []
    screen = PlayerScreen(
        _book(progress_ms=5_000, duration_ms=1_000_000), "s", "k", "iv",
        on_progress=lambda pos, *, final: saved.append((pos, final)),
    )
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 42.0
        screen._tick()

    assert saved[-1] == (42_000, True)


async def test_closing_with_q_flushes_a_final_checkpoint(fake_player):
    saved = []
    screen = PlayerScreen(
        _book(progress_ms=1_000, duration_ms=1_000_000), "s", "k", "iv",
        on_progress=lambda pos, *, final: saved.append((pos, final)),
    )
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 88.0
        await pilot.press("q")
        await pilot.pause()

    assert (88_000, True) in saved


async def test_checkpoint_failure_does_not_crash_the_player(fake_player):
    def boom(pos, *, final):
        raise RuntimeError("owner blew up")

    screen = PlayerScreen(
        _book(duration_ms=1_000_000), "s", "k", "iv", on_progress=boom,
    )
    app = HostApp(screen)

    async with app.run_test():
        await _wait_until(lambda: screen._player is not None)
        fake_player.position = 30.0
        for _ in range(screen._CHECKPOINT_EVERY_TICKS):
            screen._tick()  # must not raise despite the callback raising
