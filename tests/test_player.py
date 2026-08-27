import pytest

from audible_tui.services import player as player_module
from audible_tui.services.player import MpvError, MpvNotFoundError, MpvPlayer


def test_raises_when_mpv_not_on_path(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: None)
    with pytest.raises(MpvNotFoundError):
        MpvPlayer()


def test_does_not_raise_when_mpv_is_on_path(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    MpvPlayer()  # must not raise


def test_is_running_false_before_start(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    assert p.is_running is False


def test_get_property_returns_default_when_not_connected(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    assert p.get_property("time-pos", 1.23) == 1.23


def test_position_and_duration_default_to_zero_when_not_connected(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    assert p.position_seconds == 0.0
    assert p.duration_seconds == 0.0
    assert p.paused is False
    assert p.eof_reached is False


def test_volume_defaults_to_100_when_not_connected(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    assert p.volume == 100.0


def test_stop_before_start_does_not_raise(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    p.stop()  # must not raise


def test_set_property_raises_mpv_error_when_not_connected(monkeypatch):
    """Documents current behavior: unlike get_property (which has a default
    fallback), set_property/toggle_pause propagate MpvError if called before
    a successful start(). Library screen code only calls these once start()
    has already succeeded, so this isn't reachable in practice -- but it's
    worth pinning down explicitly rather than leaving it implicit."""
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    with pytest.raises(MpvError):
        p.toggle_pause()


# -- set_speed clamping ---------------------------------------------------


def _player_with_captured_commands(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    calls = []
    monkeypatch.setattr(p, "_command", lambda *args, **kwargs: calls.append(args))
    return p, calls


def test_set_speed_clamps_above_max(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_speed(10.0)
    assert calls == [("set_property", "speed", 3.0)]


def test_set_speed_clamps_below_min(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_speed(0.1)
    assert calls == [("set_property", "speed", 0.5)]


def test_set_speed_passes_through_value_in_range(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_speed(1.5)
    assert calls == [("set_property", "speed", 1.5)]


def test_seek_relative_sends_relative_seek_command(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.seek_relative(-10)
    assert calls == [("seek", -10, "relative")]


def test_set_volume_clamps_above_max(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_volume(150)
    assert calls == [("set_property", "volume", 100.0)]


def test_set_volume_clamps_below_min(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_volume(-20)
    assert calls == [("set_property", "volume", 0.0)]


def test_set_volume_passes_through_value_in_range(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_volume(65)
    assert calls == [("set_property", "volume", 65)]


def test_set_paused_sets_pause_property_explicitly(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.set_paused(True)
    assert calls == [("set_property", "pause", True)]


# -- _connect --------------------------------------------------------------


def test_connect_raises_immediately_if_process_already_exited(monkeypatch):
    """If mpv dies right after launch (bad args, missing codec, etc.),
    _connect should fail fast rather than spin for the full timeout waiting
    for a socket that will never appear."""
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()

    class ExitedProcess:
        def poll(self):
            return 1  # non-None => already exited

    p._proc = ExitedProcess()

    with pytest.raises(MpvError, match="exited before"):
        p._connect(timeout=1.0)
