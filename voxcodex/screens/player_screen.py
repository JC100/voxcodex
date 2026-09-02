"""Now-playing screen: wraps MpvPlayer with transport controls and a position display."""

from __future__ import annotations

import logging
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ProgressBar, Static
from textual.worker import get_current_worker

from voxcodex.models import Book
from voxcodex.services.api import Chapter
from voxcodex.services.player import MpvNotFoundError, MpvError, MpvPlayer
from voxcodex.services.settings import Settings

logger = logging.getLogger(__name__)


def _fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class PlayerScreen(Screen[int]):
    """Dismisses with the last known playback position in milliseconds."""

    BINDINGS = [
        ("space", "toggle_pause", "Play/Pause"),
        ("left", "seek_back", "-30s"),
        ("right", "seek_forward", "+30s"),
        ("shift+left", "seek_back_long", "-60s"),
        ("shift+right", "seek_forward_long", "+60s"),
        ("up", "speed_up", "Speed +"),
        ("down", "speed_down", "Speed -"),
        ("]", "volume_up", "Volume +"),
        ("[", "volume_down", "Volume -"),
        ("s", "cycle_sleep_timer", "Sleep timer"),
        ("n", "next_chapter", "Next chapter"),
        ("p", "previous_chapter", "Prev chapter"),
        ("q", "close", "Stop & back"),
        ("escape", "close", "Stop & back"),
    ]

    # Minutes cycled through by repeatedly pressing the sleep-timer key;
    # 0 means "off". Index into this list is tracked in _sleep_preset_index.
    _SLEEP_PRESETS_MIN = (0, 15, 30, 45, 60)

    # "Previous chapter" restarts the current chapter unless already this
    # close to its start, matching the usual podcast/audiobook-player
    # convention -- otherwise a slightly-late press would just replay the
    # last few seconds you already heard instead of going back a chapter.
    _CHAPTER_RESTART_THRESHOLD_MS = 3000

    # _tick fires ~1/s; checkpoint the listening position roughly this often
    # so an abnormal exit (ctrl+q, closed terminal, crash) loses seconds, not
    # the whole session. The close/unmount flush is the authoritative write.
    _CHECKPOINT_EVERY_TICKS = 15

    DEFAULT_CSS = """
    PlayerScreen {
        align: center middle;
    }
    PlayerScreen > Vertical {
        width: 80;
        height: auto;
        padding: 1 3;
        border: round $accent;
        background: $panel;
    }
    PlayerScreen .title {
        text-style: bold;
    }
    PlayerScreen #time-row {
        margin-top: 1;
    }
    """

    def __init__(
        self,
        book: Book,
        source: str,
        key: str,
        iv: str,
        chapters: list[Chapter] | None = None,
        settings: Settings | None = None,
        on_progress: Callable[..., None] | None = None,
    ) -> None:
        super().__init__()
        self.book = book
        self._source = source
        self._key = key
        self._iv = iv
        self._chapters = chapters or []
        self._player: MpvPlayer | None = None
        self._last_position_ms = book.progress_ms
        # (position_ms, *, final) -> None. The owner persists it and, when
        # final, pushes it to Audible. See LibraryScreen._launch_player.
        self._on_progress = on_progress
        self._saved_position_ms = book.progress_ms
        self._ticks_since_checkpoint = 0
        self._settings = settings if settings is not None else Settings()
        self._speed = self._settings.playback_speed
        self._volume = self._settings.playback_volume
        self._sleep_preset_index = 0
        self._sleep_remaining_seconds: float | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.book.title, classes="title")
            yield Static(self.book.author_display)
            yield Static("Starting player...", id="state")
            yield ProgressBar(id="bar", total=100, show_eta=False)
            yield Static("", id="time-row")
            yield Static("", id="chapter-row")
        yield Footer()

    def on_mount(self) -> None:
        start_seconds = 0.0 if self.book.is_finished else self.book.progress_ms / 1000
        self._start_player(start_seconds)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _start_player(self, start_seconds: float) -> None:
        worker = get_current_worker()
        try:
            player = MpvPlayer()
        except MpvNotFoundError as exc:
            if not worker.is_cancelled and self.is_mounted:
                self.app.call_from_thread(self._start_failed, str(exc))
            return

        # Publish the handle *before* the blocking start() (up to 8s inside
        # mpv's IPC connect): if the screen is dismissed or the app quits
        # mid-startup, action_close / on_unmount need something to stop, and
        # MpvPlayer.stop() breaks a concurrent start() out of its connect loop.
        self._player = player
        try:
            player.start(self._source, self._key, self._iv, start_seconds=start_seconds)
        except (MpvNotFoundError, MpvError) as exc:
            player.stop()
            self._player = None
            if not worker.is_cancelled and self.is_mounted:
                self.app.call_from_thread(self._start_failed, str(exc))
            return

        # If the screen went away during start(), on_unmount already called
        # stop() (a no-op to repeat) -- but cover the race where it read
        # self._player before we assigned it and stopped nothing.
        if worker.is_cancelled or not self.is_mounted:
            player.stop()
            self._player = None
            return

        self.app.call_from_thread(self._start_succeeded)

    def _start_failed(self, message: str) -> None:
        try:
            self.query_one("#state", Static).update(f"[red]{message}[/red]")
        except NoMatches:
            pass

    def _start_succeeded(self) -> None:
        try:
            self.query_one("#state", Static).update("Playing")
        except NoMatches:
            # Screen went away between the is_mounted check and here.
            if self._player:
                self._player.stop()
            return
        self._control(lambda p: p.set_speed(self._speed))
        self._control(lambda p: p.set_volume(self._volume))
        self._settings.set_last_played_in_app(self.book.asin)
        self.set_interval(1.0, self._tick)

    def _control(self, fn: Callable[[MpvPlayer], object]) -> None:
        """Run a transport command against the player, swallowing an
        MpvError from a dead or stalled socket so a keypress can't take the
        whole app down. The next _tick notices `is_running` went False."""
        player = self._player
        if player is None:
            return
        try:
            fn(player)
        except MpvError as exc:
            logger.warning("mpv command failed: %s", exc)

    def _tick(self) -> None:
        player = self._player
        if player is None:
            return
        if not player.is_running:
            # With --idle=once mpv exits on its own at end-of-file; reflect
            # that rather than leaving the state stuck on "Playing".
            try:
                self.query_one("#state", Static).update("Finished")
            except NoMatches:
                pass
            return
        try:
            position = player.position_seconds
            duration = player.duration_seconds or (self.book.duration_ms / 1000)
            paused = player.paused
        except Exception:  # noqa: BLE001
            return
        self._last_position_ms = int(position * 1000)
        self._ticks_since_checkpoint += 1
        if self._ticks_since_checkpoint >= self._CHECKPOINT_EVERY_TICKS:
            self._ticks_since_checkpoint = 0
            self._flush_progress(final=False)
        pct = int(position / duration * 100) if duration else 0
        self.query_one("#bar", ProgressBar).update(total=100, progress=min(100, pct))

        if self._sleep_remaining_seconds is not None and not paused:
            self._sleep_remaining_seconds = max(0.0, self._sleep_remaining_seconds - 1.0)
            if self._sleep_remaining_seconds <= 0:
                self._control(lambda p: p.set_paused(True))
                paused = True
                self._sleep_remaining_seconds = None
                self._sleep_preset_index = 0

        sleep_part = ""
        if self._sleep_remaining_seconds is not None:
            sleep_part = f"   sleep {_fmt_hms(self._sleep_remaining_seconds)}"

        self.query_one("#time-row", Static).update(
            f"{_fmt_hms(position)} / {_fmt_hms(duration)}   "
            f"{'paused' if paused else 'playing'}   speed {self._speed:.1f}x   "
            f"vol {self._volume:.0f}%{sleep_part}"
        )
        if player.eof_reached:
            self.query_one("#state", Static).update("Finished")

        chapter_row = self.query_one("#chapter-row", Static)
        if self._chapters:
            idx = self._current_chapter_index()
            if idx is not None:
                chapter = self._chapters[idx]
                chapter_position = max(0.0, position - chapter.start_ms / 1000)
                chapter_length = chapter.length_ms / 1000
                chapter_row.update(
                    f"Chapter {idx + 1}/{len(self._chapters)}: {chapter.title}   "
                    f"({_fmt_hms(chapter_position)} / {_fmt_hms(chapter_length)})"
                )
        else:
            chapter_row.update("")

    def _current_chapter_index(self) -> int | None:
        """Index of the chapter containing `_last_position_ms`, or None if
        there are no chapters (or the position is somehow before the first
        one's start, which shouldn't normally happen)."""
        index = None
        for i, chapter in enumerate(self._chapters):
            if chapter.start_ms <= self._last_position_ms:
                index = i
            else:
                break
        return index

    def action_next_chapter(self) -> None:
        if not self._player or not self._chapters:
            return
        idx = self._current_chapter_index()
        if idx is not None and idx + 1 < len(self._chapters):
            target = self._chapters[idx + 1].start_ms / 1000
            self._control(lambda p: p.seek_absolute(target))

    def action_previous_chapter(self) -> None:
        if not self._player or not self._chapters:
            return
        idx = self._current_chapter_index()
        if idx is None:
            return
        chapter = self._chapters[idx]
        into_chapter_ms = self._last_position_ms - chapter.start_ms
        if idx > 0 and into_chapter_ms <= self._CHAPTER_RESTART_THRESHOLD_MS:
            target = self._chapters[idx - 1]
        else:
            target = chapter
        target_s = target.start_ms / 1000
        self._control(lambda p: p.seek_absolute(target_s))

    def action_toggle_pause(self) -> None:
        self._control(lambda p: p.toggle_pause())

    def action_seek_back(self) -> None:
        self._control(lambda p: p.seek_relative(-30))

    def action_seek_forward(self) -> None:
        self._control(lambda p: p.seek_relative(30))

    def action_seek_back_long(self) -> None:
        self._control(lambda p: p.seek_relative(-60))

    def action_seek_forward_long(self) -> None:
        self._control(lambda p: p.seek_relative(60))

    def action_speed_up(self) -> None:
        if self._player is None:
            return
        self._speed = min(3.0, round(self._speed + 0.1, 1))
        self._control(lambda p: p.set_speed(self._speed))
        self._settings.set_playback_speed(self._speed)

    def action_speed_down(self) -> None:
        if self._player is None:
            return
        self._speed = max(0.5, round(self._speed - 0.1, 1))
        self._control(lambda p: p.set_speed(self._speed))
        self._settings.set_playback_speed(self._speed)

    def action_volume_up(self) -> None:
        if self._player is None:
            return
        self._volume = min(100.0, self._volume + 5)
        self._control(lambda p: p.set_volume(self._volume))
        self._settings.set_playback_volume(self._volume)

    def action_volume_down(self) -> None:
        if self._player is None:
            return
        self._volume = max(0.0, self._volume - 5)
        self._control(lambda p: p.set_volume(self._volume))
        self._settings.set_playback_volume(self._volume)

    def action_cycle_sleep_timer(self) -> None:
        self._sleep_preset_index = (self._sleep_preset_index + 1) % len(self._SLEEP_PRESETS_MIN)
        minutes = self._SLEEP_PRESETS_MIN[self._sleep_preset_index]
        self._sleep_remaining_seconds = float(minutes * 60) if minutes else None

    def _flush_progress(self, *, final: bool) -> None:
        """Hand the current position to the owner to persist. Periodic
        (final=False) calls are skipped when nothing moved; the final call
        (close / unmount) always goes through so the owner can also push it
        to Audible and run end-of-book detection."""
        if self._on_progress is None:
            return
        if not final and self._last_position_ms == self._saved_position_ms:
            return
        try:
            self._on_progress(self._last_position_ms, final=final)
        except Exception:  # noqa: BLE001
            logger.warning("progress checkpoint failed", exc_info=True)
        self._saved_position_ms = self._last_position_ms

    def action_close(self) -> None:
        if self._player and self._player.is_running:
            # Grab a fresh position before stopping -- the last _tick can be
            # up to a second stale. Ignore a 0 (a transient IPC read failure
            # shouldn't rewind the resume point to the start of the book).
            pos_ms = int(self._player.position_seconds * 1000)
            if pos_ms > 0:
                self._last_position_ms = pos_ms
            self._player.stop()
        self.dismiss(self._last_position_ms)

    def on_unmount(self) -> None:
        if self._player:
            self._player.stop()
        # Fires for an explicit q/esc *and* for a hard app quit -- the sole
        # write path used to be the dismiss() result callback, which a
        # ctrl+q never reached, losing the whole session.
        self._flush_progress(final=True)
