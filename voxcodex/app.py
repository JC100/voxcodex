from __future__ import annotations

import contextlib
import logging
import os
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler

from textual import on
from textual.app import App

from voxcodex import config
from voxcodex.screens.library import LibraryScreen
from voxcodex.screens.login import LoginScreen
from voxcodex.services import auth, download
from voxcodex.services.api import AudibleAPI
from voxcodex.services.settings import Settings


class VoxCodexApp(App[None]):
    TITLE = "VoxCodex"
    SUB_TITLE = "your library, in the terminal"

    # Textual auto-registers a binding for its built-in command palette
    # (ctrl+p -- themes, and any other registered commands) with the literal
    # footer description "palette", which doesn't say what's actually in
    # there. That description string is hardcoded, not settable via a class
    # var -- registering our own binding for the same action here is the
    # only way to override it (Textual skips its default once one already
    # exists for "command_palette").
    BINDINGS = [("ctrl+p", "command_palette", "Commands")]

    def __init__(self) -> None:
        super().__init__()
        self.api: AudibleAPI | None = None
        # One Settings for the whole app, passed down to the screens rather
        # than each constructing its own over the same file (which meant a
        # write from one silently reverting the others' unseen changes).
        self.settings = Settings()
        # Setting self.theme below fires watch_theme synchronously (still
        # inside __init__, before the message loop exists) -- without this
        # guard, every launch would write the theme straight back to disk,
        # unchanged, before the UI has even rendered.
        self._loading_theme = True
        saved_theme = self.settings.theme
        if saved_theme in self.available_themes:
            self.theme = saved_theme
        self._loading_theme = False

    def watch_theme(self, theme_name: str) -> None:
        if self._loading_theme:
            return
        # Textual's own App.theme doesn't persist across runs by itself --
        # save whatever the command palette's theme picker (or anything
        # else) sets it to, so next launch starts back where you left it.
        self.settings.set_theme(theme_name)

    def on_mount(self) -> None:
        config.ensure_dirs()
        download.sweep_stale_downloads()
        self.push_screen(LoginScreen(unlock_only=auth.is_registered()))

    @on(LoginScreen.Authenticated)
    def _authenticated(self, message: LoginScreen.Authenticated) -> None:
        if self.api is not None:
            # A second successful login (e.g. re-auth) would otherwise leak
            # the first Client's httpx connection pool.
            self.api.close()
        self.api = AudibleAPI(message.authenticator)
        # switch_screen swaps the top of the stack in one atomic step -- a
        # pop followed by a push (the old code here) nets out to the same
        # depth too, but leaves a frame where the stack is briefly empty (or,
        # from another thread, could be acted on mid-swap). login.py's own
        # _reset already made this switch for the same reason (L8); this
        # call site was the one place that pattern was left unfixed (L27).
        self.switch_screen(LibraryScreen(self.api, self.settings))

    def on_unmount(self) -> None:
        if self.api is not None:
            self.api.close()


_DEBUG_ENV_VAR = "VOXCODEX_DEBUG"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler.doRollover renames the current file away and
    reopens the base name via FileHandler._open, which calls plain open()
    -- umask defaults apply, silently undoing the 0600 this module creates
    the file at the moment it first crosses maxBytes (M10). Re-chmod on
    every open, not just the first."""

    def _open(self) -> TextIOWrapper:
        stream = super()._open()
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream


def _setup_logging() -> None:
    config.ensure_dirs()

    # Create the file ourselves at 0600 before the handler opens it -- it can
    # contain the account email, login-page form state, and (in debug mode)
    # raw API response bodies, none of which should be world-readable.
    try:
        fd = os.open(config.LOG_FILE, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(config.LOG_FILE, 0o600)
    except OSError:
        pass

    handler = _PrivateRotatingFileHandler(
        config.LOG_FILE, maxBytes=1_000_000, backupCount=2, delay=True
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(handler)

    # Our own logger is safe to keep chatty; it never logs credentials or
    # response bodies.
    logging.getLogger("voxcodex").setLevel(logging.INFO)

    # `audible.client` logs the full body of every response at DEBUG -- that
    # includes licence vouchers and signed CDN URLs -- and `audible.login`
    # dumps Amazon's login/verification pages. Keep all of that OFF unless the
    # user explicitly opts in, and tell them what they just enabled.
    if os.environ.get(_DEBUG_ENV_VAR) not in (None, "", "0"):
        logging.getLogger("voxcodex").setLevel(logging.DEBUG)
        for name in ("audible.login", "audible.auth", "audible.client"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        logging.getLogger("voxcodex").warning(
            "%s is set: the log at %s will contain raw API responses "
            "(including licence data) and login-page contents.",
            _DEBUG_ENV_VAR,
            config.LOG_FILE,
        )


def run() -> None:
    _setup_logging()
    VoxCodexApp().run()


if __name__ == "__main__":
    run()
