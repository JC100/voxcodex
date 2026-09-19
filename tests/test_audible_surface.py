"""Guards the private bits of the `audible` package that VoxCodex reaches
into. These aren't public API, so a version bump can move or rename them --
better to fail here, in CI, than at a user's first login or first download.

If one of these breaks, the fix is in the module named in the assertion
(auth._login_flow_diagnostics, api.py's imports), plus the version pin in
pyproject.toml -- not here.
"""

from __future__ import annotations

import importlib


def test_audible_login_check_functions_still_exist():
    login = importlib.import_module("audible.login")
    # auth._login_flow_diagnostics monkeypatches exactly these.
    for name in (
        "check_for_captcha",
        "check_for_choice_mfa",
        "check_for_mfa",
        "check_for_cvf",
        "check_for_approval_alert",
    ):
        assert callable(getattr(login, name)), f"audible.login.{name} is gone or not callable"


def test_audible_internals_used_by_the_api_wrapper_still_import():
    from audible.aescipher import decrypt_voucher_from_licenserequest
    from audible.client import raise_for_status

    assert callable(decrypt_voucher_from_licenserequest)
    assert callable(raise_for_status)


def test_authenticator_login_entry_points_still_exist():
    import audible

    assert hasattr(audible.Authenticator, "from_login")
    assert hasattr(audible.Authenticator, "from_login_external")
    assert hasattr(audible.Authenticator, "from_file")


def test_textual_theme_api_is_available():
    """app.py reads App.available_themes / sets App.theme at startup -- the
    reason for the textual>=0.86 floor."""
    from textual.app import App

    assert hasattr(App, "available_themes")
    assert hasattr(App, "theme")
