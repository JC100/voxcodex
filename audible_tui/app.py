from __future__ import annotations

import logging

import audible
from textual.app import App

from audible_tui import config
from audible_tui.screens.library import LibraryScreen
from audible_tui.screens.login import LoginScreen
from audible_tui.services import auth
from audible_tui.services.api import AudibleAPI


class AudibleTUIApp(App[None]):
    TITLE = "Audible TUI"
    SUB_TITLE = "your library, in the terminal"

    def __init__(self) -> None:
        super().__init__()
        self.api: AudibleAPI | None = None

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
    for name in ("audible_tui", "audible.login", "audible.auth", "audible.client"):
        logging.getLogger(name).setLevel(logging.DEBUG)


def run() -> None:
    _setup_logging()
    AudibleTUIApp().run()


if __name__ == "__main__":
    run()
