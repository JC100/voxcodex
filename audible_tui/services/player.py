"""Controls an mpv subprocess over its JSON IPC socket.

mpv is fed the AAXC stream/file directly with the DRM key+iv handed to
ffmpeg's mov demuxer (confirmed via `ffmpeg -h demuxer=mov`: -audible_key /
-audible_iv are real private options of that demuxer), so no separate
decrypt-to-disk step is needed for either streaming or local playback.
"""

from __future__ import annotations

import itertools
import json
import shutil
import socket
import subprocess
import tempfile
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
        self._sockfile = None
        self._socket_path = Path(tempfile.gettempdir()) / f"audible-tui-mpv-{id(self)}.sock"
        self._request_ids = itertools.count(1)

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, source: str, key: str, iv: str, start_seconds: float = 0.0) -> None:
        self.stop()
        if self._socket_path.exists():
            self._socket_path.unlink()

        lavf_opts = f"audible_key={key},audible_iv={iv}"
        cmd = [
            "mpv",
            "--no-video",
            "--no-terminal",
            "--idle=yes",
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
        self._connect()

    def _connect(self, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if not self.is_running:
                raise MpvError("mpv exited before the IPC socket became available")
            if self._socket_path.exists():
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(5.0)
                    sock.connect(str(self._socket_path))
                    self._sock = sock
                    self._sockfile = sock.makefile("rwb")
                    return
                except OSError as e:
                    last_err = e
            time.sleep(0.05)
        raise MpvError(f"Could not connect to mpv IPC socket: {last_err}")

    def _command(self, *args: object, timeout: float = 5.0) -> object:
        if self._sock is None or self._sockfile is None:
            raise MpvError("Player is not running")
        req_id = next(self._request_ids)
        payload = json.dumps({"command": list(args), "request_id": req_id}) + "\n"
        self._sock.settimeout(timeout)
        self._sockfile.write(payload.encode())
        self._sockfile.flush()
        while True:
            line = self._sockfile.readline()
            if not line:
                raise MpvError("mpv IPC connection closed")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("request_id") == req_id:
                if msg.get("error") not in (None, "success"):
                    raise MpvError(str(msg.get("error")))
                return msg.get("data")

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
        if self._sock is not None:
            try:
                self._command("quit", timeout=2.0)
            except MpvError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._sockfile = None
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass
