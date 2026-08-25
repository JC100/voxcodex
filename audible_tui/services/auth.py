"""Login + credential persistence, built on the `audible` package's OAuth device-registration flow.

This module never touches purchase/checkout endpoints -- it only produces an
Authenticator that the api service uses for read/library/content/progress calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import audible

from audible_tui import config

Locale = str


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


def login(
    username: str,
    password: str,
    locale: Locale,
    callbacks: LoginCallbacks,
) -> audible.Authenticator:
    """Perform the interactive Amazon OAuth login. Does not persist anything yet."""
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
