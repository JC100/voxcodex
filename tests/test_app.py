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


async def test_changing_theme_persists_it(monkeypatch):
    fake_settings = FakeSettings(theme="textual-dark")
    monkeypatch.setattr(app_module, "Settings", lambda: fake_settings)
    app = VoxCodexApp()

    async with app.run_test() as pilot:
        app.theme = "gruvbox"
        await pilot.pause()

        assert fake_settings.theme_calls[-1] == "gruvbox"
