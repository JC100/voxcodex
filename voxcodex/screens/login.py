"""First-run interactive Amazon login, and the unlock prompt for returning users."""

from __future__ import annotations

import threading
from collections.abc import Callable

import audible
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, LoadingIndicator, Select, Static

from voxcodex.screens.modals import MessageModal, PromptModal
from voxcodex.services import auth

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

    BINDINGS = [("ctrl+q", "quit_app", "Quit"), ("escape", "quit_app", "Cancel / quit")]

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
        # Set on unmount so a login worker parked in _blocking_prompt (waiting
        # on an OTP/CAPTCHA the user will now never enter) can give up instead
        # of blocking asyncio's executor forever and hanging the process exit.
        self._shutting_down = threading.Event()

    def on_unmount(self) -> None:
        self._shutting_down.set()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b]VoxCodex[/b]")
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
                else:
                    yield Button(
                        "Sign in via browser instead (if Amazon blocks the form above)",
                        id="external-login",
                    )
                yield Button("Quit", id="quit")
            yield LoadingIndicator()
            yield Static("", id="status")
        yield Footer()

    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        if self._unlock_only:
            self._start_unlock()
        else:
            self._start_login()

    @on(Input.Submitted)
    def _input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a field advances to the next one, or submits on the last."""
        order = ["vault-password"] if self._unlock_only else ["username", "password", "vault-password"]
        if event.input.id not in order:
            return
        idx = order.index(event.input.id)
        if idx + 1 < len(order):
            self.query_one(f"#{order[idx + 1]}", Input).focus()
        else:
            self._submit()

    @on(Button.Pressed, "#external-login")
    def _external_login_pressed(self) -> None:
        self._start_external_login()

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        auth.logout()
        self.app.pop_screen()
        self.app.push_screen(LoginScreen(unlock_only=False))

    @on(Button.Pressed, "#quit")
    def _quit_pressed(self) -> None:
        self.action_quit_app()

    def action_quit_app(self) -> None:
        self.app.exit()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _start_unlock(self) -> None:
        if self.has_class("busy"):
            return
        password = self.query_one("#vault-password", Input).value or None
        self.add_class("busy")
        self._set_status("")
        self._do_unlock(password)

    @work(thread=True, exclusive=True, group="login", exit_on_error=False)
    def _do_unlock(self, password: str | None) -> None:
        try:
            authenticator = auth.load(password)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._call_back(self._unlock_failed, str(exc))
            return
        self._call_back(self._login_succeeded, authenticator)

    def _unlock_failed(self, message: str) -> None:
        self.remove_class("busy")
        self._set_status(f"[red]Could not unlock: {message}[/red]")

    def _start_login(self) -> None:
        if self.has_class("busy"):
            return
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

    @work(thread=True, exclusive=True, group="login", exit_on_error=False)
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
            self._call_back(self._login_failed, str(exc))
            return
        self._call_back(self._login_succeeded, authenticator)

    def _start_external_login(self) -> None:
        if self.has_class("busy"):
            return
        locale = str(self.query_one("#locale", Select).value)
        vault_password = self.query_one("#vault-password", Input).value or None
        self.add_class("busy")
        self._set_status("Starting browser login...")
        self._do_external_login(locale, vault_password)

    @work(thread=True, exclusive=True, group="login", exit_on_error=False)
    def _do_external_login(self, locale: str, vault_password: str | None) -> None:
        callbacks = auth.LoginCallbacks(login_url=self._external_url_prompt)
        try:
            authenticator = auth.login_external(locale, callbacks)
            auth.save(authenticator, vault_password)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._call_back(self._login_failed, str(exc))
            return
        self._call_back(self._login_succeeded, authenticator)

    def _external_url_prompt(self, url: str) -> str:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        message = (
            "Open this URL in any web browser (a tab may have opened for you "
            "already):\n\n"
            f"{url}\n\n"
            "Log in with your normal Amazon credentials there (you may be asked "
            "twice, plus a captcha -- that's normal for this flow). Afterwards "
            "the browser will land on a 'Page not found' error -- that's expected. "
            "Copy the FULL url from the address bar at that point and paste it below."
        )
        return self._blocking_prompt("Browser login", message, allow_empty=False)

    def _call_back(self, fn: Callable[..., None], *args: object) -> None:
        """call_from_thread, but a no-op once the screen is tearing down --
        the app loop may be gone, and there's nothing left to update."""
        if self._shutting_down.is_set():
            return
        try:
            self.app.call_from_thread(fn, *args)
        except RuntimeError:
            pass

    def _blocking_prompt(self, title: str, message: str, allow_empty: bool = False) -> str:
        """Runs on the login worker thread; blocks it until the user answers
        in the UI -- or until the screen is torn down (ctrl+q during an OTP
        prompt), in which case it returns "" so the login flow fails cleanly
        instead of the worker parking here forever and hanging process exit."""
        event = threading.Event()
        result: list[str] = [""]

        def _push() -> None:
            def _done(value: str) -> None:
                result[0] = value
                event.set()

            self.app.push_screen(
                PromptModal(title, message, password=False, allow_empty=allow_empty), _done
            )

        if self._shutting_down.is_set():
            return ""
        try:
            self.app.call_from_thread(_push)
        except RuntimeError:
            return ""

        while not event.wait(timeout=0.2):
            if self._shutting_down.is_set():
                return ""
        return result[0]

    def _login_failed(self, message: str) -> None:
        self.remove_class("busy")
        self._set_status(f"[red]Sign-in failed: {message}[/red]")

    def _login_succeeded(self, authenticator: audible.Authenticator) -> None:
        self.remove_class("busy")
        self.app.on_authenticated(authenticator)
