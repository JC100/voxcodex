import pytest

from voxcodex import config


def test_ensure_dirs_creates_config_data_and_downloads_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", data_dir / "downloads")

    assert not config_dir.exists()
    assert not data_dir.exists()

    config.ensure_dirs()

    assert config_dir.is_dir()
    assert data_dir.is_dir()
    assert (data_dir / "downloads").is_dir()


def test_ensure_dirs_is_idempotent(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", data_dir / "downloads")

    config.ensure_dirs()
    config.ensure_dirs()  # must not raise on the second call

    assert config_dir.is_dir()


def test_downloads_dir_is_under_data_dir():
    assert config.DOWNLOADS_DIR.parent == config.DATA_DIR


def test_auth_and_settings_files_are_under_config_dir():
    assert config.AUTH_FILE.parent == config.CONFIG_DIR
    assert config.SETTINGS_FILE.parent == config.CONFIG_DIR


def test_progress_and_library_cache_files_are_under_data_dir():
    assert config.PROGRESS_CACHE_FILE.parent == config.DATA_DIR
    assert config.LIBRARY_CACHE_FILE.parent == config.DATA_DIR


# -- atomic_write_text (H2) -------------------------------------------------


def test_atomic_write_text_writes_the_file_and_creates_parent(tmp_path):
    target = tmp_path / "nested" / "dir" / "state.json"
    config.atomic_write_text(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'


def test_atomic_write_text_replaces_existing_content(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old")
    config.atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_text_leaves_old_file_intact_and_no_tmp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("good")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(config.os, "replace", boom)
    with pytest.raises(OSError):
        config.atomic_write_text(target, "half-written")

    assert target.read_text() == "good"
    assert list(tmp_path.iterdir()) == [target]  # temp file cleaned up
