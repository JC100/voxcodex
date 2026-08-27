from __future__ import annotations

import logging

import audible
from textual.app import App

from voxcodex import config
from voxcodex.screens.library import LibraryScreen
from voxcodex.screens.login import LoginScreen
from voxcodex.services import auth
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
        self._settings = Settings()
        saved_theme = self._settings.theme
        if saved_theme in self.available_themes:
            self.theme = saved_theme

    def watch_theme(self, theme_name: str) -> None:
        # Textual's own App.theme doesn't persist across runs by itself --
        # save whatever the command palette's theme picker (or anything
        # else) sets it to, so next launch starts back where you left it.
        self._settings.set_theme(theme_name)

    def on_mount(self) -> None:
        config.ensure_dirs()
        self.push_screen(LoginScreen(unlock_only=auth.is_registered()))

    def on_authenticated(self, authenticator: audible.Authenticator) -> None:
        self.api = AudibleAPI(authenticator)
        self.pop_screen()
        self.push_screen(LibraryScreen(self.api))

    def on_unmount(self) -> None:
        if self.api is not None:
            self.api.close()


def _setup_logging() -> None:
    config.ensure_dirs()
    handler = logging.FileHandler(config.LOG_FILE)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(handler)
    # Nothing here logs credentials in plaintext, but the login flow's HTML
    # inspection can be verbose -- keep it out of WARNING-level noise from
    # other libraries while still capturing it in the file.
    for name in ("voxcodex", "audible.login", "audible.auth", "audible.client"):
        logging.getLogger(name).setLevel(logging.DEBUG)


def run() -> None:
    _setup_logging()
    VoxCodexApp().run()


if __name__ == "__main__":
    run()
