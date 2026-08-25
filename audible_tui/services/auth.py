"""Login + credential persistence, built on the `audible` package's OAuth device-registration flow.

This module never touches purchase/checkout endpoints -- it only produces an
Authenticator that the api service uses for read/library/content/progress calls.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import audible
import audible.login as _login_internals

from audible_tui import config

Locale = str
logger = logging.getLogger("audible_tui.auth")


@dataclass
class LoginCallbacks:
    """Synchronous callbacks the audible package calls mid-login when Amazon needs more input.

    Each callback blocks the login worker thread until it returns a value, so the caller
    (the login screen) is expected to pump these through a UI prompt and wait for a result.
    """

    otp: Callable[[], str] | None = None
    cvf: Callable[[], str] | None = None
    captcha: Callable[[str], str] | None = None
    approval: Callable[[], Any] | None = None


def is_registered() -> bool:
    return config.AUTH_FILE.exists()


@contextlib.contextmanager
def _login_flow_diagnostics():
    """Logs which branch of Amazon's login flow fired (captcha / 2FA-method
    choice / OTP / verification-code / approval), and -- for the 2FA method
    choice specifically -- which delivery options the page actually offered.

    `audible.Authenticator.from_login` has no callback for the "choose your
    2FA method" step (it silently auto-picks an authenticator-app/TOTP option
    if the account has one configured, with no way to ask for SMS instead),
    so this is the only way to see what really happened there.
    """
    names = [
        "check_for_captcha",
        "check_for_choice_mfa",
        "check_for_mfa",
        "check_for_cvf",
        "check_for_approval_alert",
    ]
    originals = {name: getattr(_login_internals, name) for name in names}

    def _make_wrapper(name: str, original: Callable[..., bool]) -> Callable[..., bool]:
        def wrapper(soup: Any, *a: Any, **kw: Any) -> bool:
            result = original(soup, *a, **kw)
            if result:
                logger.info("login flow: %s matched", name)
                if name == "check_for_choice_mfa":
                    options = [
                        node.get("class")
                        for node in soup.select("div[data-a-input-name=otpDeviceContext]")
                    ]
                    logger.info("login flow: 2FA method options on page: %s", options)
            return result

        return wrapper

    for name, original in originals.items():
        setattr(_login_internals, name, _make_wrapper(name, original))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(_login_internals, name, original)


def login(
    username: str,
    password: str,
    locale: Locale,
    callbacks: LoginCallbacks,
) -> audible.Authenticator:
    """Perform the interactive Amazon OAuth login. Does not persist anything yet."""
    with _login_flow_diagnostics():
        return audible.Authenticator.from_login(
            username,
            password,
            locale=locale,
            otp_callback=callbacks.otp,
            cvf_callback=callbacks.cvf,
            captcha_callback=callbacks.captcha,
            approval_callback=callbacks.approval,
        )


def save(auth: audible.Authenticator, vault_password: str | None) -> None:
    config.ensure_dirs()
    auth.to_file(
        config.AUTH_FILE,
        password=vault_password or None,
        encryption="json" if vault_password else False,
    )
    if not vault_password:
        try:
            config.AUTH_FILE.chmod(0o600)
        except OSError:
            pass


def load(vault_password: str | None = None) -> audible.Authenticator:
    return audible.Authenticator.from_file(config.AUTH_FILE, password=vault_password)


def logout() -> None:
    if config.AUTH_FILE.exists():
        config.AUTH_FILE.unlink()
