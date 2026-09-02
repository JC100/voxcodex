import json
import socket
import stat
import threading
import time

import pytest

from voxcodex.services import player as player_module
from voxcodex.services.player import MpvError, MpvNotFoundError, MpvPlayer


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


def test_seek_absolute_sends_absolute_seek_command(monkeypatch):
    p, calls = _player_with_captured_commands(monkeypatch)
    p.seek_absolute(123.4)
    assert calls == [("seek", 123.4, "absolute")]


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


# -- socket lifecycle, against a real AF_UNIX server standing in for mpv ----


class FakeMpv:
    """A stand-in mpv: binds the IPC socket the real one would create, and
    answers get_property/set_property/seek with a success envelope. Lets the
    tests exercise MpvPlayer's actual socket code (connect, framing, timeout
    handling, teardown) without a real mpv binary."""

    def __init__(self, cmd):
        socket_path = next(
            arg.split("=", 1)[1] for arg in cmd if arg.startswith("--input-ipc-server=")
        )
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(socket_path)
        self._srv.listen(1)
        self.alive = True
        self.commands = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        conn.settimeout(0.5)
        buf = b""
        with conn:
            while self.alive:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    self.commands.append(msg["command"])
                    if msg["command"] == ["quit"]:
                        self.alive = False
                        return
                    reply = {"error": "success", "data": 42}
                    if "request_id" in msg:
                        reply["request_id"] = msg["request_id"]
                    conn.sendall((json.dumps(reply) + "\n").encode())

    # -- subprocess.Popen surface --
    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False
        try:
            self._srv.close()
        except OSError:
            pass

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return 0

    def kill(self):
        self.terminate()


@pytest.fixture()
def fake_mpv(monkeypatch):
    servers = []

    def fake_popen(cmd, **kwargs):
        server = FakeMpv(cmd)
        servers.append(server)
        return server

    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    monkeypatch.setattr(player_module.subprocess, "Popen", fake_popen)
    yield servers
    for server in servers:
        server.terminate()


def test_start_uses_a_private_0700_temp_dir_and_removes_it_on_stop(fake_mpv):
    p = MpvPlayer()
    p.start("src", "key", "iv")

    tmp_dir = p._dir
    assert tmp_dir is not None and tmp_dir.is_dir()
    assert stat.S_IMODE(tmp_dir.stat().st_mode) == 0o700
    assert p._socket_path.parent == tmp_dir
    assert p.is_running

    p.stop()
    assert not tmp_dir.exists()
    assert p._dir is None and p._socket_path is None
    assert p.is_running is False


def test_commands_round_trip_over_the_real_socket(fake_mpv):
    p = MpvPlayer()
    p.start("src", "key", "iv")

    assert p.get_property("time-pos") == 42  # FakeMpv answers every read with 42
    p.set_property("pause", True)
    p.seek_relative(-30)

    p.stop()
    assert ["set_property", "pause", True] in fake_mpv[0].commands
    assert ["seek", -30, "relative"] in fake_mpv[0].commands


def test_command_wraps_a_dropped_connection_as_mpv_error(fake_mpv):
    p = MpvPlayer()
    p.start("src", "key", "iv")

    fake_mpv[0].terminate()  # kill the server out from under the client
    time.sleep(0.05)

    with pytest.raises(MpvError):
        p.set_property("pause", True)
    # get_property swallows it and returns the default
    assert p.get_property("time-pos", 1.5) == 1.5
    p.stop()


def test_stop_is_idempotent_and_safe_from_several_threads(fake_mpv):
    p = MpvPlayer()
    p.start("src", "key", "iv")
    tmp_dir = p._dir

    threads = [threading.Thread(target=p.stop) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert p.is_running is False
    assert not tmp_dir.exists()


def test_command_is_serialised_across_threads(fake_mpv):
    """The player screen polls position from a background worker while
    transport actions run on theirs -- interleaved sendall/recv on one
    socket would scramble request/response framing. _io_lock prevents it."""
    p = MpvPlayer()
    p.start("s", "k", "iv")

    results = []
    errors = []

    def hammer():
        try:
            for _ in range(15):
                results.append(p.get_property("time-pos"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    p.stop()
    assert not errors
    assert results and all(r == 42 for r in results)  # never a mismatched reply


def test_stop_during_startup_breaks_connect_out_of_its_retry_loop(monkeypatch):
    """mpv is up but never creates the IPC socket; a stop() from another
    thread must make the blocking start() bail promptly instead of spinning
    for the full 8s connect timeout."""

    class NeverListens:
        def __init__(self, cmd):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.alive = False

    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    monkeypatch.setattr(player_module.subprocess, "Popen", lambda cmd, **kw: NeverListens(cmd))

    p = MpvPlayer()
    threading.Timer(0.3, p.stop).start()

    started = time.monotonic()
    with pytest.raises(MpvError):
        p.start("src", "key", "iv")
    assert time.monotonic() - started < 3.0  # nowhere near the 8s ceiling
