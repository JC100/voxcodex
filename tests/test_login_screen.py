"""Integration tests for LoginScreen's own state machine (field navigation,
validation, busy/error states, and the blocking-prompt relay used for
OTP/CVF/CAPTCHA callbacks) -- against a faked `auth` module, never real
Amazon endpoints. That real HTTP flow is intentionally not covered here; see
README.md.
"""

from __future__ import annotations

import asyncio
import threading

from textual import on
from textual.app import App
from textual.widgets import Input, Static

from voxcodex.screens import login as login_module
from voxcodex.screens.login import LoginScreen
from voxcodex.screens.modals import PromptModal


class FakeAuthenticator:
    pass


class HostApp(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen
        self.authenticated_with = None

    def on_mount(self) -> None:
        self.push_screen(self._screen)

    @on(LoginScreen.Authenticated)
    def _authenticated(self, message: LoginScreen.Authenticated) -> None:
        self.authenticated_with = message.authenticator


async def _wait_until(condition, timeout=2.0, step=0.02):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


# -- field navigation (Enter key) ------------------------------------------


async def test_enter_advances_through_login_fields_and_submits_on_last(monkeypatch):
    login_calls = []
    monkeypatch.setattr(
        login_module.auth, "login",
        lambda *a, **k: (login_calls.append((a, k)), FakeAuthenticator())[1],
    )
    monkeypatch.setattr(login_module.auth, "save", lambda auth, pw: None)

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test(size=(80, 50)) as pilot:
        # Default focus lands on the locale Select (first widget in compose
        # order) -- the Enter-to-advance chain only fires between Input
        # widgets (via Input.Submitted), so it doesn't start until focus
        # moves off the Select onto an actual Input first.
        assert app.focused.id == "locale"
        screen.query_one("#username", Input).focus()
        await pilot.pause()

        await pilot.press(*"me@example.com")
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused.id == "password"

        await pilot.press(*"hunter2")
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused.id == "vault-password"

        await pilot.press("enter")  # last field -> submits
        await _wait_until(lambda: login_calls != [])

        args, _kwargs = login_calls[0]
        username, password, locale, _callbacks = args
        assert username == "me@example.com"
        assert password == "hunter2"


async def test_enter_in_unlock_only_mode_submits_immediately(monkeypatch):
    load_calls = []
    monkeypatch.setattr(
        login_module.auth, "load",
        lambda pw: (load_calls.append(pw), FakeAuthenticator())[1],
    )

    screen = LoginScreen(unlock_only=True)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        assert app.focused.id == "vault-password"
        await pilot.press(*"secret")
        await pilot.press("enter")
        await _wait_until(lambda: load_calls != [])

        assert load_calls == ["secret"]


# -- validation -------------------------------------------------------------


async def test_login_requires_username_and_password(monkeypatch):
    login_calls = []
    monkeypatch.setattr(login_module.auth, "login", lambda *a, **k: login_calls.append(1))

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test(size=(80, 50)) as pilot:
        await pilot.click("#submit")
        await pilot.pause()

        assert login_calls == []
        assert "required" in str(screen.query_one("#status", Static).content)


# -- success / failure ------------------------------------------------------


async def test_successful_login_calls_on_authenticated(monkeypatch):
    fake_auth = FakeAuthenticator()
    monkeypatch.setattr(login_module.auth, "login", lambda *a, **k: fake_auth)
    save_calls = []
    monkeypatch.setattr(
        login_module.auth, "save", lambda auth, pw: save_calls.append((auth, pw))
    )

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test(size=(80, 50)) as pilot:
        screen.query_one("#username", Input).value = "me@example.com"
        screen.query_one("#password", Input).value = "hunter2"
        await pilot.click("#submit")

        await _wait_until(lambda: app.authenticated_with is not None)

        assert app.authenticated_with is fake_auth
        assert save_calls == [(fake_auth, None)]
        assert not screen.has_class("busy")


async def test_failed_login_shows_error_and_clears_busy_state(monkeypatch):
    monkeypatch.setattr(
        login_module.auth, "login",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad credentials")),
    )

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test(size=(80, 50)) as pilot:
        screen.query_one("#username", Input).value = "me@example.com"
        screen.query_one("#password", Input).value = "wrong"
        await pilot.click("#submit")

        await _wait_until(
            lambda: "bad credentials" in str(screen.query_one("#status", Static).content)
        )
        assert app.authenticated_with is None
        assert not screen.has_class("busy")


async def test_failed_unlock_shows_error(monkeypatch):
    monkeypatch.setattr(
        login_module.auth, "load",
        lambda pw: (_ for _ in ()).throw(ValueError("wrong vault password")),
    )

    screen = LoginScreen(unlock_only=True)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        await pilot.click("#submit")

        await _wait_until(
            lambda: "wrong vault password" in str(screen.query_one("#status", Static).content)
        )
        assert app.authenticated_with is None


# -- reset / switch account ------------------------------------------------


async def test_reset_logs_out_and_shows_a_fresh_login_form(monkeypatch):
    logout_calls = []
    monkeypatch.setattr(login_module.auth, "logout", lambda: logout_calls.append(1))

    screen = LoginScreen(unlock_only=True)
    app = HostApp(screen)

    async with app.run_test() as pilot:
        depth_before = len(app.screen_stack)

        await pilot.click("#reset")
        await pilot.pause()

        assert logout_calls == [1]
        # L8: switch_screen replaces the top of the stack -- a pop followed
        # by a push (the old code) nets out to the same depth too, but only
        # switch_screen does it as one atomic step with no frame in between
        # where the stack is briefly empty (or, from another thread, could
        # be acted on mid-swap).
        assert len(app.screen_stack) == depth_before
        assert isinstance(app.screen, LoginScreen)
        assert app.screen is not screen  # a genuinely fresh LoginScreen
        assert not app.screen._unlock_only  # logging out always resets to full login
        new_screen = app.screen
        assert isinstance(new_screen, LoginScreen)
        assert new_screen is not screen
        assert new_screen._unlock_only is False


# -- blocking-prompt relay (OTP/CVF/CAPTCHA callbacks) ----------------------


async def test_otp_callback_relays_through_a_prompt_modal(monkeypatch):
    captured = {}

    def fake_login(username, password, locale, callbacks):
        captured["otp"] = callbacks.otp()
        return FakeAuthenticator()

    monkeypatch.setattr(login_module.auth, "login", fake_login)
    monkeypatch.setattr(login_module.auth, "save", lambda auth, pw: None)

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test(size=(80, 50)) as pilot:
        screen.query_one("#username", Input).value = "me@example.com"
        screen.query_one("#password", Input).value = "hunter2"
        await pilot.click("#submit")

        await _wait_until(lambda: isinstance(app.screen, PromptModal))
        await pilot.press(*"123456")
        await pilot.click("#submit")

        await _wait_until(lambda: app.authenticated_with is not None)
        assert captured["otp"] == "123456"


# -- double-submit / shutdown (H9, H7) -----------------------------------------


async def test_double_submit_triggers_only_one_login(monkeypatch):
    release = threading.Event()
    calls = []

    def fake_login(*a, **k):
        calls.append(1)
        release.wait(timeout=5)
        return FakeAuthenticator()

    monkeypatch.setattr(login_module.auth, "login", fake_login)
    monkeypatch.setattr(login_module.auth, "save", lambda auth, pw: None)

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    try:
        async with app.run_test(size=(80, 50)) as pilot:
            screen.query_one("#username", Input).value = "me@example.com"
            screen.query_one("#password", Input).value = "hunter2"
            await pilot.click("#submit")
            await _wait_until(lambda: calls == [1])

            await pilot.click("#submit")  # busy -> ignored
            await pilot.click("#submit")
            await pilot.pause()

            assert calls == [1]
    finally:
        release.set()


async def test_blocking_prompt_gives_up_when_the_screen_shuts_down(monkeypatch):
    """A ctrl+q while an OTP prompt is open must not park the login worker in
    _blocking_prompt forever (which hangs process exit)."""
    otp_result = {}

    def fake_login(username, password, locale, callbacks):
        otp_result["value"] = callbacks.otp()  # blocks until shutdown
        return FakeAuthenticator()

    monkeypatch.setattr(login_module.auth, "login", fake_login)
    monkeypatch.setattr(login_module.auth, "save", lambda auth, pw: None)

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test(size=(80, 50)) as pilot:
        screen.query_one("#username", Input).value = "me@example.com"
        screen.query_one("#password", Input).value = "hunter2"
        await pilot.click("#submit")
        await _wait_until(lambda: isinstance(app.screen, PromptModal))

        screen._shutting_down.set()  # stand in for the unmount on app exit

        await _wait_until(lambda: "value" in otp_result)
        assert otp_result["value"] == ""


# -- worker descriptions don't leak credentials (H4) -------------------------


async def test_do_login_worker_description_does_not_contain_credentials(monkeypatch):
    """Without an explicit description=, Textual builds a worker's debug
    description by repr()-ing its positional args -- both the account
    password and the vault password would otherwise sit in plaintext on a
    long-lived Worker attribute, reachable via Textual devtools and via a
    crash traceback rendered with show_locals=True."""
    monkeypatch.setattr(login_module.auth, "login", lambda *a, **k: FakeAuthenticator())
    monkeypatch.setattr(login_module.auth, "save", lambda auth, pw: None)

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test():
        worker = screen._do_login("me@example.com", "SuperSecret123", "us", "VaultPw456")

        assert worker.description == "signing in"
        assert "SuperSecret123" not in worker.description
        assert "VaultPw456" not in worker.description

        await _wait_until(lambda: worker.is_finished)


async def test_do_unlock_worker_description_does_not_contain_the_vault_password(
    monkeypatch,
):
    monkeypatch.setattr(login_module.auth, "load", lambda pw: FakeAuthenticator())

    screen = LoginScreen(unlock_only=True)
    app = HostApp(screen)

    async with app.run_test():
        worker = screen._do_unlock("VaultPw456")

        assert worker.description == "unlocking vault"
        assert "VaultPw456" not in worker.description

        await _wait_until(lambda: worker.is_finished)


async def test_do_external_login_worker_description_does_not_contain_the_vault_password(
    monkeypatch,
):
    monkeypatch.setattr(
        login_module.auth, "login_external", lambda *a, **k: FakeAuthenticator()
    )
    monkeypatch.setattr(login_module.auth, "save", lambda auth, pw: None)

    screen = LoginScreen(unlock_only=False)
    app = HostApp(screen)

    async with app.run_test():
        worker = screen._do_external_login("us", "VaultPw456")

        assert worker.description == "signing in via browser"
        assert "VaultPw456" not in worker.description

        await _wait_until(lambda: worker.is_finished)
