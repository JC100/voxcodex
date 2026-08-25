"""Now-playing screen: wraps MpvPlayer with transport controls and a position display."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, ProgressBar, Static

from audible_tui.models import Book
from audible_tui.services.player import MpvNotFoundError, MpvError, MpvPlayer


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
        ("left", "seek_back", "-10s"),
        ("right", "seek_forward", "+30s"),
        ("shift+left", "seek_back_long", "-60s"),
        ("shift+right", "seek_forward_long", "+60s"),
        ("up", "speed_up", "Speed +"),
        ("down", "speed_down", "Speed -"),
        ("q", "close", "Stop & back"),
        ("escape", "close", "Stop & back"),
    ]

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

    def __init__(self, book: Book, source: str, key: str, iv: str) -> None:
        super().__init__()
        self.book = book
        self._source = source
        self._key = key
        self._iv = iv
        self._player: MpvPlayer | None = None
        self._last_position_ms = book.progress_ms
        self._speed = 1.0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.book.title, classes="title")
            yield Static(self.book.author_display)
            yield Static("Starting player...", id="state")
            yield ProgressBar(id="bar", total=100, show_eta=False)
            yield Static("", id="time-row")
        yield Footer()

    def on_mount(self) -> None:
        start_seconds = 0.0 if self.book.is_finished else self.book.progress_ms / 1000
        self._start_player(start_seconds)

    @work(thread=True, exclusive=True)
    def _start_player(self, start_seconds: float) -> None:
        try:
            player = MpvPlayer()
            player.start(self._source, self._key, self._iv, start_seconds=start_seconds)
        except (MpvNotFoundError, MpvError) as exc:
            self.app.call_from_thread(self._start_failed, str(exc))
            return
        self._player = player
        self.app.call_from_thread(self._start_succeeded)

    def _start_failed(self, message: str) -> None:
        self.query_one("#state", Static).update(f"[red]{message}[/red]")

    def _start_succeeded(self) -> None:
        self.query_one("#state", Static).update("Playing")
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        player = self._player
        if player is None or not player.is_running:
            return
        try:
            position = player.position_seconds
            duration = player.duration_seconds or (self.book.duration_ms / 1000)
            paused = player.paused
        except Exception:  # noqa: BLE001
            return
        self._last_position_ms = int(position * 1000)
        pct = int(position / duration * 100) if duration else 0
        self.query_one("#bar", ProgressBar).update(total=100, progress=min(100, pct))
        self.query_one("#time-row", Static).update(
            f"{_fmt_hms(position)} / {_fmt_hms(duration)}   "
            f"{'paused' if paused else 'playing'}   speed {self._speed:.1f}x"
        )
        if player.eof_reached:
            self.query_one("#state", Static).update("Finished")

    def action_toggle_pause(self) -> None:
        if self._player:
            self._player.toggle_pause()

    def action_seek_back(self) -> None:
        if self._player:
            self._player.seek_relative(-10)

    def action_seek_forward(self) -> None:
        if self._player:
            self._player.seek_relative(30)

    def action_seek_back_long(self) -> None:
        if self._player:
            self._player.seek_relative(-60)

    def action_seek_forward_long(self) -> None:
        if self._player:
            self._player.seek_relative(60)

    def action_speed_up(self) -> None:
        if self._player:
            self._speed = min(3.0, round(self._speed + 0.1, 1))
            self._player.set_speed(self._speed)

    def action_speed_down(self) -> None:
        if self._player:
            self._speed = max(0.5, round(self._speed - 0.1, 1))
            self._player.set_speed(self._speed)

    def action_close(self) -> None:
        if self._player:
            self._player.stop()
        self.dismiss(self._last_position_ms)

    def on_unmount(self) -> None:
        if self._player:
            self._player.stop()
