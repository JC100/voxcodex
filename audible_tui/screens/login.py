"""First-run interactive Amazon login, and the unlock prompt for returning users."""

from __future__ import annotations

import threading

import audible
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, LoadingIndicator, Select, Static

from audible_tui.screens.modals import MessageModal, PromptModal
from audible_tui.services import auth

LOCALES = [
    ("United States", "us"),
    ("United Kingdom", "uk"),
    ("Germany", "de"),
    ("France", "fr"),
    ("Canada", "ca"),
    ("Australia", "au"),
    ("India", "in"),
    ("Italy", "it"),
    ("Spain", "es"),
    ("Japan", "jp"),
]


class LoginScreen(Screen[None]):
    """Handles both first-run login and unlocking an existing encrypted auth file."""

    DEFAULT_CSS = """
    LoginScreen {
        align: center middle;
    }
    LoginScreen > Vertical {
        width: 64;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    LoginScreen .field-label {
        margin-top: 1;
    }
    LoginScreen #status {
        margin-top: 1;
        color: $text-muted;
    }
    LoginScreen LoadingIndicator {
        height: 3;
        display: none;
    }
    LoginScreen.busy LoadingIndicator {
        display: block;
    }
    LoginScreen.busy #form {
        display: none;
    }
    """

    def __init__(self, unlock_only: bool) -> None:
        super().__init__()
        self._unlock_only = unlock_only

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b]Audible TUI[/b]")
            if self._unlock_only:
                yield Static("Enter your vault password to unlock your saved login.")
            else:
                yield Static(
                    "Sign in with your Amazon account. This app only reads your "
                    "existing Audible library -- it never touches purchasing or "
                    "payment info."
                )
            with Vertical(id="form"):
                if self._unlock_only:
                    yield Static("Vault password (blank if you skipped encryption):", classes="field-label")
                    yield Input(password=True, id="vault-password")
                else:
                    yield Static("Marketplace:", classes="field-label")
                    yield Select(LOCALES, value="us", id="locale")
                    yield Static("Amazon email:", classes="field-label")
                    yield Input(id="username")
                    yield Static("Amazon password:", classes="field-label")
                    yield Input(password=True, id="password")
                    yield Static(
                        "Vault password to encrypt your saved login locally "
                        "(leave blank to store unencrypted, file-permission-protected):",
                        classes="field-label",
                    )
                    yield Input(password=True, id="vault-password")
                yield Button(
                    "Unlock" if self._unlock_only else "Sign in",
                    variant="primary",
                    id="submit",
                )
                if self._unlock_only:
                    yield Button("Use a different account", id="reset")
            yield LoadingIndicator()
            yield Static("", id="status")

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        if self._unlock_only:
            self._start_unlock()
        else:
            self._start_login()

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        auth.logout()
        self.app.pop_screen()
        self.app.push_screen(LoginScreen(unlock_only=False))

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _start_unlock(self) -> None:
        password = self.query_one("#vault-password", Input).value or None
        self.add_class("busy")
        self._set_status("")
        self._do_unlock(password)

    @work(thread=True)
    def _do_unlock(self, password: str | None) -> None:
        try:
            authenticator = auth.load(password)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self.app.call_from_thread(self._unlock_failed, str(exc))
            return
        self.app.call_from_thread(self._login_succeeded, authenticator)

    def _unlock_failed(self, message: str) -> None:
        self.remove_class("busy")
        self._set_status(f"[red]Could not unlock: {message}[/red]")

    def _start_login(self) -> None:
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        locale = str(self.query_one("#locale", Select).value)
        vault_password = self.query_one("#vault-password", Input).value or None
        if not username or not password:
            self._set_status("[red]Email and password are required.[/red]")
            return
        self.add_class("busy")
        self._set_status("Signing in...")
        self._do_login(username, password, locale, vault_password)

    @work(thread=True)
    def _do_login(
        self, username: str, password: str, locale: str, vault_password: str | None
    ) -> None:
        callbacks = auth.LoginCallbacks(
            otp=lambda: self._blocking_prompt(
                "One-time password", "Enter the OTP code sent to your device:"
            ),
            cvf=lambda: self._blocking_prompt(
                "Verification code", "Enter the verification code Amazon sent you:"
            ),
            captcha=lambda url: self._blocking_prompt(
                "CAPTCHA",
                f"Open this URL in a browser, then type what you see:\n{url}",
            ),
            approval=lambda: self._blocking_prompt(
                "Approval needed",
                "Approve this sign-in in your Amazon/Audible app, then press Submit.",
                allow_empty=True,
            ),
        )
        try:
            authenticator = auth.login(username, password, locale, callbacks)
            auth.save(authenticator, vault_password)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self.app.call_from_thread(self._login_failed, str(exc))
            return
        self.app.call_from_thread(self._login_succeeded, authenticator)

    def _blocking_prompt(self, title: str, message: str, allow_empty: bool = False) -> str:
        """Runs on the login worker thread; blocks it until the user answers in the UI."""
        event = threading.Event()
        result: list[str] = [""]

        def _push() -> None:
            def _done(value: str) -> None:
                result[0] = value
                event.set()

            self.app.push_screen(
                PromptModal(title, message, password=False, allow_empty=allow_empty), _done
            )

        self.app.call_from_thread(_push)
        event.wait()
        return result[0]

    def _login_failed(self, message: str) -> None:
        self.remove_class("busy")
        self._set_status(f"[red]Sign-in failed: {message}[/red]")

    def _login_succeeded(self, authenticator: audible.Authenticator) -> None:
        self.remove_class("busy")
        self.app.on_authenticated(authenticator)
