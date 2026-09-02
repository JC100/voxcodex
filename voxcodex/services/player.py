"""Controls an mpv subprocess over its JSON IPC socket.

mpv is fed the AAXC stream/file directly with the DRM key+iv handed to
ffmpeg's mov demuxer (confirmed via `ffmpeg -h demuxer=mov`: -audible_key /
-audible_iv are real private options of that demuxer), so no separate
decrypt-to-disk step is needed for either streaming or local playback.

The IPC socket lives in a private, per-instance `mkdtemp` directory (mode
0700) rather than a guessable path in shared `/tmp` -- mpv's JSON IPC can
run arbitrary programs, so anyone able to connect to the socket would get
code execution as the user running VoxCodex.
"""

from __future__ import annotations

import itertools
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class MpvNotFoundError(Exception):
    pass


class MpvError(Exception):
    pass


class MpvPlayer:
    def __init__(self) -> None:
        if shutil.which("mpv") is None:
            raise MpvNotFoundError(
                "mpv was not found on PATH. Install mpv to enable playback."
            )
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._recv_buf = b""
        self._dir: Path | None = None
        self._socket_path: Path | None = None
        self._request_ids = itertools.count(1)
        # start() and stop() can run on different threads (the player-screen
        # worker vs. on_unmount on the event loop); _stopping lets a stop()
        # during startup break _connect out of its retry loop promptly, and
        # _stop_lock serialises concurrent teardowns.
        self._stopping = threading.Event()
        self._stop_lock = threading.Lock()
        # One request/response on the socket at a time -- the player screen
        # now polls position from a background worker while transport actions
        # run on their own, so interleaved sendall/recv would corrupt framing.
        self._io_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, source: str, key: str, iv: str, start_seconds: float = 0.0) -> None:
        self.stop()
        self._stopping.clear()

        self._dir = Path(tempfile.mkdtemp(prefix="voxcodex-mpv-"))
        self._socket_path = self._dir / "mpv.sock"

        lavf_opts = f"audible_key={key},audible_iv={iv}"
        cmd = [
            "mpv",
            "--no-video",
            "--no-terminal",
            # `once`, not `yes`: mpv exits when the file ends instead of sitting
            # idle forever holding the audio device.
            "--idle=once",
            "--force-seekable=yes",
            f"--input-ipc-server={self._socket_path}",
            f"--demuxer-lavf-o={lavf_opts}",
            source,
        ]
        if start_seconds > 0:
            cmd.insert(-1, f"--start={start_seconds:.2f}")

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            self._connect()
        except Exception:
            # Don't leave the mpv process we just spawned running headless
            # with no IPC channel to control or stop it.
            self.stop()
            raise

    def _connect(self, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                raise MpvError("player was stopped before mpv finished starting")
            if not self.is_running:
                raise MpvError("mpv exited before the IPC socket became available")
            if self._socket_path is not None and self._socket_path.exists():
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(str(self._socket_path))
                except OSError as e:
                    last_err = e
                    sock.close()  # otherwise every failed retry leaks an fd
                else:
                    self._sock = sock
                    self._recv_buf = b""
                    return
            time.sleep(0.05)
        raise MpvError(f"Could not connect to mpv IPC socket: {last_err}")

    def _read_line(self, sock: socket.socket, timeout: float) -> bytes | None:
        """Read one newline-terminated IPC message off `sock`. Returns None if
        the peer closed the connection. Buffers manually rather than via
        socket.makefile(), whose internal state is left inconsistent by a
        timeout on the underlying socket."""
        while b"\n" not in self._recv_buf:
            sock.settimeout(timeout)
            chunk = sock.recv(65536)
            if not chunk:
                return None
            self._recv_buf += chunk
        line, self._recv_buf = self._recv_buf.split(b"\n", 1)
        return line

    def _command(self, *args: object, timeout: float = 1.5) -> object:
        with self._io_lock:
            sock = self._sock
            if sock is None:
                raise MpvError("Player is not running")
            req_id = next(self._request_ids)
            payload = json.dumps({"command": list(args), "request_id": req_id}) + "\n"
            deadline = time.monotonic() + timeout
            try:
                sock.settimeout(timeout)
                sock.sendall(payload.encode())
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MpvError(f"timed out waiting for mpv response to {args[0]!r}")
                    line = self._read_line(sock, remaining)
                    if line is None:
                        raise MpvError("mpv IPC connection closed")
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("request_id") == req_id:
                        if msg.get("error") not in (None, "success"):
                            raise MpvError(str(msg.get("error")))
                        return msg.get("data")
            except OSError as exc:
                # Socket I/O fails as BrokenPipeError / ConnectionResetError /
                # socket.timeout -- all OSError, none MpvError. Funnel them into
                # the one exception type callers actually catch.
                raise MpvError(f"mpv IPC error: {exc}") from exc

    def get_property(self, name: str, default: object = None) -> object:
        try:
            return self._command("get_property", name)
        except MpvError:
            return default

    def set_property(self, name: str, value: object) -> None:
        self._command("set_property", name, value)

    @property
    def position_seconds(self) -> float:
        return float(self.get_property("time-pos", 0.0) or 0.0)

    @property
    def duration_seconds(self) -> float:
        return float(self.get_property("duration", 0.0) or 0.0)

    @property
    def paused(self) -> bool:
        return bool(self.get_property("pause", False))

    @property
    def eof_reached(self) -> bool:
        return bool(self.get_property("eof-reached", False))

    @property
    def volume(self) -> float:
        return float(self.get_property("volume", 100.0) or 0.0)

    def set_paused(self, paused: bool) -> None:
        self.set_property("pause", paused)

    def toggle_pause(self) -> None:
        self.set_paused(not self.paused)

    def seek_relative(self, seconds: float) -> None:
        self._command("seek", seconds, "relative")

    def seek_absolute(self, seconds: float) -> None:
        self._command("seek", seconds, "absolute")

    def set_speed(self, speed: float) -> None:
        self.set_property("speed", max(0.5, min(3.0, speed)))

    def set_volume(self, volume: float) -> None:
        self.set_property("volume", max(0.0, min(100.0, volume)))

    def stop(self) -> None:
        # Called from the event loop (on_unmount / action_close) as well as
        # the player-screen worker, so it stays deliberately quick: a best-
        # effort quit with a short timeout, then SIGTERM, then SIGKILL.
        # Closing the socket also unblocks any in-flight _command on the poll
        # or control worker (its recv raises, surfaced as MpvError).
        self._stopping.set()
        with self._stop_lock:
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.settimeout(0.5)
                    sock.sendall(b'{"command": ["quit"]}\n')
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

            proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            tmp_dir, self._dir = self._dir, None
            self._socket_path = None
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
