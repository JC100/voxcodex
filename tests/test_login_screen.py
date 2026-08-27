"""Integration tests for LoginScreen's own state machine (field navigation,
validation, busy/error states, and the blocking-prompt relay used for
OTP/CVF/CAPTCHA callbacks) -- against a faked `auth` module, never real
Amazon endpoints. That real HTTP flow is intentionally not covered here; see
README.md.
"""

from __future__ import annotations

import asyncio

from textual.app import App
from textual.widgets import Input, Static

from audible_tui.screens import login as login_module
from audible_tui.screens.login import LoginScreen
from audible_tui.screens.modals import PromptModal


class FakeAuthenticator:
    pass


class HostApp(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen
        self.authenticated_with = None

    def on_mount(self) -> None:
        self.push_screen(self._screen)

    def on_authenticated(self, authenticator) -> None:
        self.authenticated_with = authenticator


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
        await pilot.click("#reset")
        await pilot.pause()

        assert logout_calls == [1]
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
