"""Controls an mpv subprocess over its JSON IPC socket.

mpv is fed the AAXC stream/file directly with the DRM key+iv handed to
ffmpeg's mov demuxer (confirmed via `ffmpeg -h demuxer=mov`: -audible_key /
-audible_iv are real private options of that demuxer), so no separate
decrypt-to-disk step is needed for either streaming or local playback.

The IPC socket lives in a private, per-instance `mkdtemp` directory (mode
0700) rather than a guessable path in shared `/tmp` -- mpv's JSON IPC can
run arbitrary programs, so anyone able to connect to the socket would get
code execution as the user running VoxCodex. The same directory holds the
key/iv themselves: `/proc/<pid>/cmdline` is world-readable, so they're
written to a private (0600) mpv config file and handed to mpv via
`--include=` rather than ever appearing in argv.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class MpvNotFoundError(Exception):
    pass


class MpvError(Exception):
    pass


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _require_hex(value: str, name: str) -> None:
    if not value or len(value) % 2 != 0 or not _HEX_RE.fullmatch(value):
        raise MpvError(f"{name} is not a valid hex string")


def _write_private_file(path: Path, text: str) -> None:
    """Writes `text` to `path`, created (or truncated) at 0600 -- the parent
    mkdtemp directory is already 0700, but this is written defensively in
    case that ever changes."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


class MpvPlayer:
    def __init__(self) -> None:
        if shutil.which("mpv") is None:
            raise MpvNotFoundError(
                "mpv was not found on PATH. Install mpv to enable playback."
            )
        self._proc: subprocess.Popen[bytes] | None = None
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
        # key/iv are server-controlled plaintext (from a licenserequest
        # response) written verbatim into mpv's line-oriented config-file
        # parser -- an embedded newline plus a follow-on option line (e.g.
        # `script=...`) is otherwise arbitrary code execution the moment
        # mpv loads the include file. Reject anything that isn't plain hex
        # before it ever reaches the options file (docs/code-review-
        # 2026-09-21.html H5).
        _require_hex(key, "key")
        _require_hex(iv, "iv")

        self.stop()
        self._stopping.clear()

        self._dir = Path(tempfile.mkdtemp(prefix="voxcodex-mpv-"))
        self._socket_path = self._dir / "mpv.sock"

        # The key/iv never touch argv (see module docstring): they go in a
        # private mpv config file instead, in the same directory as the IPC
        # socket so it's cleaned up by the same rmtree in stop().
        options_path = self._dir / "options.conf"
        _write_private_file(options_path, f"demuxer-lavf-o=audible_key={key},audible_iv={iv}\n")

        cmd = [
            "mpv",
            "--no-video",
            "--no-terminal",
            # `once`, not `yes`: mpv exits when the file ends instead of sitting
            # idle forever holding the audio device.
            "--idle=once",
            "--force-seekable=yes",
            f"--input-ipc-server={self._socket_path}",
            f"--include={options_path}",
        ]
        if start_seconds > 0:
            cmd.append(f"--start={start_seconds:.2f}")
        # `--` terminates option parsing: source is server-controlled
        # (license.content_url for a stream) and mpv treats any positional
        # argument starting with "-" as an option rather than a filename
        # without it (docs/code-review-2026-09-21.html H6).
        cmd.append("--")
        cmd.append(source)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._connect()
        except Exception:
            # Covers both a Popen failure (e.g. mpv removed/renamed between
            # the shutil.which check in __init__ and here, or a resource
            # limit) and a failed _connect -- either way, don't leave the
            # temp dir (holding the plaintext DRM key) on disk, or an mpv
            # process running headless with no IPC channel to control it.
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

    def _command(self, *args: object, timeout: float = 1.5) -> Any:
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

    def get_property(self, name: str, default: object = None) -> Any:
        try:
            return self._command("get_property", name)
        except MpvError:
            return default

    def set_property(self, name: str, value: object) -> None:
        self._command("set_property", name, value)

    @property
    def position_seconds(self) -> float:
        # No default here, unlike the other properties below: a failed IPC
        # read (timeout, a stall mid-seek, mpv exiting between the caller's
        # is_running check and this call) must not be indistinguishable
        # from "genuinely at position 0" -- callers persist this value and
        # push it to Audible, so a swallowed failure silently erases the
        # real resume point (see docs/code-review-2026-09-21.html H3).
        value = self.get_property("time-pos")
        if value is None:
            raise MpvError("time-pos unavailable")
        return float(value)

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
                # stop() doesn't take _io_lock, so a concurrent _command on
                # another thread (the poll or control worker) may still be
                # blocked in recv() on this same socket. A bare close() can
                # race that: the fd could be reused by an unrelated new
                # socket before the blocked recv() wakes up. shutdown()
                # first forces that recv() to return immediately (as EOF)
                # without invalidating the fd, closing the window (L2).
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    sock.close()

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
