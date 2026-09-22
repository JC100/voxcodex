import json
import os
import socket
import stat
import threading
import time

import pytest

from voxcodex.services import player as player_module
from voxcodex.services.player import MpvError, MpvNotFoundError, MpvPlayer
import contextlib


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


def test_duration_paused_eof_default_when_not_connected(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    assert p.duration_seconds == 0.0
    assert p.paused is False
    assert p.eof_reached is False


def test_position_seconds_raises_instead_of_defaulting_when_not_connected(monkeypatch):
    """Unlike the other properties above, a failed read of time-pos must not
    silently default to 0.0 -- callers persist this value and push it to
    Audible, so a swallowed failure would erase a real resume point
    (see docs/code-review-2026-09-21.html H3)."""
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    with pytest.raises(MpvError):
        _ = p.position_seconds


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
        self.cmd = cmd
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
                except TimeoutError:
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
        with contextlib.suppress(OSError):
            self._srv.close()

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
    p.start("src", "de", "ad")

    tmp_dir = p._dir
    assert tmp_dir is not None and tmp_dir.is_dir()
    assert stat.S_IMODE(tmp_dir.stat().st_mode) == 0o700
    assert p._socket_path.parent == tmp_dir
    assert p.is_running

    p.stop()
    assert not tmp_dir.exists()
    assert p._dir is None and p._socket_path is None
    assert p.is_running is False


# -- key/iv never on the command line (M8) ---------------------------------


def test_start_never_puts_the_key_or_iv_on_the_command_line(fake_mpv):
    p = MpvPlayer()
    p.start("src", "deadbeef", "cafebabe")

    cmd_str = " ".join(fake_mpv[-1].cmd)
    assert "deadbeef" not in cmd_str
    assert "cafebabe" not in cmd_str

    p.stop()


def test_start_passes_the_key_and_iv_via_a_private_include_file(fake_mpv):
    p = MpvPlayer()
    p.start("src", "abc123", "def456")

    include_arg = next(arg for arg in fake_mpv[-1].cmd if arg.startswith("--include="))
    options_path = include_arg.split("=", 1)[1]
    assert stat.S_IMODE(os.stat(options_path).st_mode) == 0o600
    with open(options_path) as f:
        contents = f.read()
    assert "audible_key=abc123" in contents
    assert "audible_iv=def456" in contents

    p.stop()
    assert not os.path.exists(options_path)  # cleaned up with the rest of _dir


# -- H5: key/iv are rejected unless they're plain hex ----------------------


@pytest.mark.parametrize(
    "key,iv",
    [
        ("not-hex!", "ad"),
        ("de", "not-hex!"),
        ("odd", "ad"),  # odd length -- can't be a whole number of bytes
        ("", "ad"),
        ("de", ""),
        # The actual exploit: an embedded newline followed by a second mpv
        # config-file line (mpv's config parser is line-oriented and a
        # malformed line doesn't abort parsing) -- confirmed against real
        # mpv to load and execute an attacker-supplied script.
        ("de\nscript=/tmp/evil.lua", "ad"),
    ],
)
def test_start_rejects_non_hex_key_or_iv(monkeypatch, key, iv):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    with pytest.raises(MpvError):
        p.start("src", key, iv)


def test_start_accepts_plain_hex_key_and_iv(fake_mpv):
    p = MpvPlayer()
    p.start("src", "deadbeef", "cafebabe")  # must not raise
    p.stop()


# -- H6: the stream URL is passed after a `--` terminator -------------------


def test_start_puts_a_double_dash_terminator_before_the_source(fake_mpv):
    p = MpvPlayer()
    p.start("src", "de", "ad")

    cmd = fake_mpv[-1].cmd
    assert cmd[-2:] == ["--", "src"]

    p.stop()


def test_start_with_a_leading_dash_source_is_not_treated_as_an_option(fake_mpv):
    """A stream URL beginning with '-' (server-controlled: license.content_url)
    would otherwise be parsed by mpv as an option rather than a filename."""
    p = MpvPlayer()
    p.start("--script=/tmp/evil.lua", "de", "ad")

    cmd = fake_mpv[-1].cmd
    assert cmd[-2:] == ["--", "--script=/tmp/evil.lua"]

    p.stop()


def test_start_with_a_start_seconds_puts_it_before_the_dash_terminator(fake_mpv):
    p = MpvPlayer()
    p.start("src", "de", "ad", start_seconds=12.5)

    cmd = fake_mpv[-1].cmd
    assert cmd[-2:] == ["--", "src"]
    assert "--start=12.50" in cmd
    assert cmd.index("--start=12.50") < cmd.index("--")

    p.stop()


def test_commands_round_trip_over_the_real_socket(fake_mpv):
    p = MpvPlayer()
    p.start("src", "de", "ad")

    assert p.get_property("time-pos") == 42  # FakeMpv answers every read with 42
    assert p.position_seconds == 42.0
    p.set_property("pause", True)
    p.seek_relative(-30)

    p.stop()
    assert ["set_property", "pause", True] in fake_mpv[0].commands
    assert ["seek", -30, "relative"] in fake_mpv[0].commands


def test_command_wraps_a_dropped_connection_as_mpv_error(fake_mpv):
    p = MpvPlayer()
    p.start("src", "de", "ad")

    fake_mpv[0].terminate()  # kill the server out from under the client
    time.sleep(0.05)

    with pytest.raises(MpvError):
        p.set_property("pause", True)
    # get_property swallows it and returns the default
    assert p.get_property("time-pos", 1.5) == 1.5
    # position_seconds must not swallow the same failure into a phantom 0
    # (see docs/code-review-2026-09-21.html H3) -- it has to raise so the
    # caller's poll loop skips the tick instead of committing a lost read
    # as the real position.
    with pytest.raises(MpvError):
        _ = p.position_seconds
    p.stop()


def test_command_honors_an_absolute_deadline_against_a_trickling_peer(monkeypatch):
    """L4: _read_line used to re-arm a fixed per-recv timeout on every
    iteration instead of shrinking it against the overall deadline -- a
    peer sending a byte just before each recv's timeout fired could keep
    resetting the clock and hold _io_lock (and every transport key,
    compounding M8) far past the requested timeout."""
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")
    p = MpvPlayer()
    server_sock, client_sock = socket.socketpair()
    p._sock = client_sock

    stop_trickling = threading.Event()

    def trickle():
        # One byte well inside the command timeout, forever -- never a
        # newline, so _read_line's inner loop never completes a message.
        while not stop_trickling.wait(0.05):
            try:
                server_sock.sendall(b"x")
            except OSError:
                return

    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(MpvError, match="timed out"):
            p._command("get_property", "time-pos", timeout=0.3)
        elapsed = time.monotonic() - started

        # Bounded by the deadline (with slack for scheduling), not reset on
        # every trickled byte -- the trickle interval (0.05s) is well under
        # the 0.3s command timeout, so the bug this guards against would
        # keep this blocked for several seconds at least.
        assert elapsed < 1.0
    finally:
        stop_trickling.set()
        thread.join(timeout=2)
        server_sock.close()
        client_sock.close()


def test_stop_is_idempotent_and_safe_from_several_threads(fake_mpv):
    p = MpvPlayer()
    p.start("src", "de", "ad")
    tmp_dir = p._dir

    threads = [threading.Thread(target=p.stop) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert p.is_running is False
    assert not tmp_dir.exists()


def test_stop_shuts_down_the_socket_before_closing_it(fake_mpv, monkeypatch):
    """L2: stop() doesn't take _io_lock, so a concurrent _command on
    another thread (the poll or control worker) may still be blocked in
    recv() on this same socket. A bare close() can race that -- the fd
    could be reused by an unrelated new socket before the blocked recv()
    wakes up. shutdown() first forces that recv() to return immediately
    (as EOF) without invalidating the fd, closing the window."""
    p = MpvPlayer()
    p.start("src", "de", "ad")
    client_sock = p._sock

    calls = []
    original_shutdown = socket.socket.shutdown
    original_close = socket.socket.close

    def recording_shutdown(self, *args, **kwargs):
        if self is client_sock:
            calls.append("shutdown")
        return original_shutdown(self, *args, **kwargs)

    def recording_close(self, *args, **kwargs):
        if self is client_sock:
            calls.append("close")
        return original_close(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "shutdown", recording_shutdown)
    monkeypatch.setattr(socket.socket, "close", recording_close)

    p.stop()

    assert calls == ["shutdown", "close"]


def test_stop_reaps_the_process_after_a_sigkill(fake_mpv):
    """L3: proc.kill() alone doesn't reap the process -- nothing calls
    wait() on it afterwards, so it lingers as a zombie until this
    MpvPlayer (and its self._proc reference) is garbage collected, rather
    than until the next book is played."""
    p = MpvPlayer()
    p.start("src", "de", "ad")

    class NeverTerminates:
        def __init__(self):
            self.kill_called = False
            self.wait_calls = 0
            self._killed = False

        def poll(self):
            return 0 if self._killed else None

        def terminate(self):
            pass  # ignored -- the process doesn't actually die

        def wait(self, timeout=None):
            self.wait_calls += 1
            if not self._killed:
                raise player_module.subprocess.TimeoutExpired(cmd="mpv", timeout=timeout)
            return 0

        def kill(self):
            self.kill_called = True
            self._killed = True

    fake_proc = NeverTerminates()
    p._proc = fake_proc

    p.stop()

    assert fake_proc.kill_called is True
    assert fake_proc.wait_calls == 2  # once after terminate() times out, once after kill()


def test_command_is_serialised_across_threads(fake_mpv):
    """The player screen polls position from a background worker while
    transport actions run on theirs -- interleaved sendall/recv on one
    socket would scramble request/response framing. _io_lock prevents it."""
    p = MpvPlayer()
    p.start("s", "0a", "ad")

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
        p.start("src", "de", "ad")
    assert time.monotonic() - started < 3.0  # nowhere near the 8s ceiling


def test_start_cleans_up_the_temp_dir_when_popen_itself_fails(monkeypatch):
    """M6: Popen used to run outside the surrounding cleanup try -- a
    FileNotFoundError (mpv removed/renamed between the shutil.which check
    in __init__ and here) left the 0700 temp dir, with the plaintext DRM
    key inside it, on disk until the next reboot."""
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/usr/bin/mpv")

    created_dirs = []
    real_mkdtemp = player_module.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created_dirs.append(path)
        return path

    monkeypatch.setattr(player_module.tempfile, "mkdtemp", recording_mkdtemp)

    def fake_popen(cmd, **kwargs):
        raise FileNotFoundError("mpv: No such file or directory")

    monkeypatch.setattr(player_module.subprocess, "Popen", fake_popen)

    p = MpvPlayer()
    with pytest.raises(FileNotFoundError):
        p.start("src", "de", "ad")

    assert p._dir is None
    (created_dir,) = created_dirs
    assert not os.path.exists(created_dir)
