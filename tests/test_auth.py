"""Unit tests for services.auth -- the monkeypatch/restore of audible.login's
private check_for_* functions, and the cvf-page logging redaction."""

from __future__ import annotations

import logging

import audible.login as login_internals

from voxcodex.services import auth


def _current_targets():
    return {
        name: getattr(login_internals, name)
        for name in (
            "check_for_captcha",
            "check_for_choice_mfa",
            "check_for_mfa",
            "check_for_cvf",
            "check_for_approval_alert",
        )
    }


def test_diagnostics_context_restores_every_patched_function():
    before = _current_targets()

    with auth._login_flow_diagnostics():
        during = _current_targets()
        assert during != before  # wrappers are installed

    assert _current_targets() == before


def test_diagnostics_context_restores_even_if_the_body_raises():
    before = _current_targets()

    try:
        with auth._login_flow_diagnostics():
            raise RuntimeError("login blew up")
    except RuntimeError:
        pass

    assert _current_targets() == before


def test_nested_diagnostics_does_not_capture_wrappers_as_originals():
    """The bug the reentrancy guard prevents: an inner enter that patched
    again would, on exit, 'restore' the outer entry's wrappers -- leaving
    audible.login monkeypatched for the life of the process."""
    before = _current_targets()

    with auth._login_flow_diagnostics():
        with auth._login_flow_diagnostics():  # inner: must be a no-op
            pass
        # still patched by the outer context, exactly once
        assert login_internals.check_for_cvf is not before["check_for_cvf"]

    assert _current_targets() == before


def test_log_cvf_page_records_field_length_not_value(caplog):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<form><input name="metadata1" type="hidden" value="super-secret-token">'
        '<input name="code" type="text" value=""></form>',
        "html.parser",
    )

    with caplog.at_level(logging.INFO, logger="voxcodex.auth"):
        auth._log_cvf_page(soup)

    logged = "\n".join(r.message for r in caplog.records)
    assert "super-secret-token" not in logged
    assert "value_len=18" in logged  # len("super-secret-token")
    assert "value_len=0" in logged
