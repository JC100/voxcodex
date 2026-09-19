"""Unit tests for services.auth -- the monkeypatch/restore of audible.login's
private check_for_* functions, and the cvf-page logging redaction."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import audible
import audible.login as login_internals

from voxcodex import config
from voxcodex.services import auth


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class _FakeAuthenticator:
    """Stands in for audible.Authenticator -- enough of `to_file`'s
    contract (writes the given path, taking password/encryption kwargs) to
    exercise `auth.save`'s permission handling without a real login."""

    def to_file(self, filename, password=None, encryption=False):
        Path(filename).write_text('{"fake": true}')


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


# -- save() file permissions (M2) -----------------------------------------


def _patch_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "AUTH_FILE", tmp_path / "auth.json")


def test_save_leaves_the_unencrypted_auth_file_private(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)

    auth.save(_FakeAuthenticator(), vault_password=None)

    assert _mode(config.AUTH_FILE) == 0o600


def test_save_leaves_the_encrypted_auth_file_private(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)

    auth.save(_FakeAuthenticator(), vault_password="hunter2")

    assert _mode(config.AUTH_FILE) == 0o600


def test_save_pre_creates_the_auth_file_at_0600_before_writing(tmp_path, monkeypatch):
    """Regression test: the file must never be created (even transiently)
    at the process's default umask -- audible's to_file() truncates an
    existing file in place, so pre-creating it at 0600 is what closes that
    window, not the chmod that runs after."""
    _patch_dirs(monkeypatch, tmp_path)
    seen_mode = None

    class _RecordingAuthenticator(_FakeAuthenticator):
        def to_file(self, filename, password=None, encryption=False):
            nonlocal seen_mode
            seen_mode = _mode(Path(filename))
            super().to_file(filename, password=password, encryption=encryption)

    auth.save(_RecordingAuthenticator(), vault_password=None)

    assert seen_mode == 0o600


# -- load / logout / is_registered (M10) -----------------------------------


def test_load_passes_the_auth_file_path_and_vault_password(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        audible.Authenticator,
        "from_file",
        lambda filename, password=None: calls.append((filename, password)) or "AUTHENTICATOR",
    )

    result = auth.load(vault_password="hunter2")

    assert calls == [(config.AUTH_FILE, "hunter2")]
    assert result == "AUTHENTICATOR"


def test_is_registered_false_when_no_auth_file_exists(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)
    assert auth.is_registered() is False


def test_is_registered_true_once_saved(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)
    auth.save(_FakeAuthenticator(), vault_password=None)
    assert auth.is_registered() is True


def test_logout_removes_the_auth_file(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)
    auth.save(_FakeAuthenticator(), vault_password=None)
    assert config.AUTH_FILE.exists()

    auth.logout()

    assert not config.AUTH_FILE.exists()
    assert auth.is_registered() is False


def test_logout_is_a_no_op_when_never_registered(tmp_path, monkeypatch):
    _patch_dirs(monkeypatch, tmp_path)
    auth.logout()  # must not raise
