import logging
import stat

import pytest

from voxcodex import app as app_module
from voxcodex import config
from voxcodex.app import VoxCodexApp


@pytest.fixture(autouse=True)
def _config_dirs_in_tmp_path(tmp_path, monkeypatch):
    """VoxCodexApp.on_mount() calls config.ensure_dirs() and checks
    config.AUTH_FILE for real -- without this, every test here would create
    (harmless but real) directories under this machine's actual
    ~/.config/voxcodex and ~/.local/share/voxcodex."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "data" / "downloads")
    monkeypatch.setattr(config, "AUTH_FILE", tmp_path / "config" / "auth.json")


class FakeSettings:
    def __init__(self, theme="textual-dark"):
        self._theme = theme
        self.theme_calls = []

    @property
    def theme(self):
        return self._theme

    def set_theme(self, theme):
        self.theme_calls.append(theme)
        self._theme = theme


async def test_command_palette_has_a_clear_footer_label(monkeypatch):
    monkeypatch.setattr(app_module, "Settings", lambda: FakeSettings())
    app = VoxCodexApp()

    async with app.run_test():
        binding = next(
            b for _key, b in app._bindings if b.action == "command_palette"
        )
        assert binding.description == "Commands"


async def test_starts_with_the_saved_theme_applied(monkeypatch):
    monkeypatch.setattr(app_module, "Settings", lambda: FakeSettings(theme="nord"))
    app = VoxCodexApp()

    async with app.run_test():
        assert app.theme == "nord"


async def test_falls_back_to_default_when_saved_theme_is_unrecognized(monkeypatch):
    """Defensive against a theme that was removed/renamed since it was
    saved, or a corrupted value -- must not crash the app on startup."""
    monkeypatch.setattr(
        app_module, "Settings", lambda: FakeSettings(theme="some-removed-theme")
    )
    app = VoxCodexApp()

    async with app.run_test():
        assert app.theme != "some-removed-theme"


async def test_applying_the_saved_theme_on_launch_does_not_write_it_back(monkeypatch):
    """L10: setting self.theme in __init__ to restore the saved theme fires
    watch_theme synchronously, before the UI has even rendered -- it must
    not turn straight around and write that same value back to disk on
    every single launch."""
    fake_settings = FakeSettings(theme="nord")
    monkeypatch.setattr(app_module, "Settings", lambda: fake_settings)
    app = VoxCodexApp()

    async with app.run_test():
        assert app.theme == "nord"
        assert fake_settings.theme_calls == []


async def test_changing_theme_persists_it(monkeypatch):
    fake_settings = FakeSettings(theme="textual-dark")
    monkeypatch.setattr(app_module, "Settings", lambda: fake_settings)
    app = VoxCodexApp()

    async with app.run_test() as pilot:
        app.theme = "gruvbox"
        await pilot.pause()

        assert fake_settings.theme_calls[-1] == "gruvbox"


# -- log file permissions (M10) ----------------------------------------------


@pytest.fixture()
def _isolated_root_logger():
    """_setup_logging mutates the real, global root logger -- undo that
    after the test so it doesn't leak a handler (or a lowered level) into
    every other test in the suite."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(original_level)


def _log_file_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "data" / "downloads")
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "data" / "voxcodex.log")


def test_log_file_is_created_at_0600(monkeypatch, tmp_path, _isolated_root_logger):
    _log_file_in_tmp(tmp_path, monkeypatch)

    app_module._setup_logging()

    assert stat.S_IMODE(config.LOG_FILE.stat().st_mode) == 0o600


def test_log_file_keeps_0600_permissions_after_rotation(
    monkeypatch, tmp_path, _isolated_root_logger
):
    """M10: RotatingFileHandler.doRollover renames the current file away
    and reopens the base name through FileHandler._open, which calls plain
    open() -- umask defaults apply, silently undoing the 0600 the file was
    created at the moment it first crosses maxBytes."""
    _log_file_in_tmp(tmp_path, monkeypatch)
    app_module._setup_logging()
    logger = logging.getLogger("voxcodex")

    handler = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, app_module._PrivateRotatingFileHandler)
    )
    logger.info("first entry")  # opens the file lazily (delay=True)
    handler.doRollover()
    logger.info("second entry, after rotation")

    assert stat.S_IMODE(config.LOG_FILE.stat().st_mode) == 0o600
    backup = config.LOG_FILE.with_name(config.LOG_FILE.name + ".1")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
