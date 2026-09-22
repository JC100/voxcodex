import stat

import pytest

from voxcodex import config


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


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


# -- private permissions (M2) ------------------------------------------


def test_ensure_dirs_creates_directories_private(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", data_dir / "downloads")

    config.ensure_dirs()

    assert _mode(config_dir) == 0o700
    assert _mode(data_dir) == 0o700
    assert _mode(data_dir / "downloads") == 0o700


def test_ensure_dirs_tightens_a_pre_existing_looser_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o755)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", data_dir / "downloads")

    config.ensure_dirs()

    assert _mode(config_dir) == 0o700


def test_atomic_write_text_writes_the_file_private(tmp_path):
    target = tmp_path / "state.json"
    config.atomic_write_text(target, '{"a": 1}')
    assert _mode(target) == 0o600


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


def test_atomic_write_text_fsyncs_before_the_rename(tmp_path, monkeypatch):
    """L5: without an fsync, power loss shortly after a write can land the
    rename durable but the data behind it not -- a crash-safety guarantee
    the docstring already claimed but didn't actually provide."""
    target = tmp_path / "state.json"
    calls = []

    original_fsync = config.os.fsync
    original_replace = config.os.replace

    def recording_fsync(fd):
        calls.append("fsync")
        return original_fsync(fd)

    def recording_replace(src, dst):
        calls.append("replace")
        return original_replace(src, dst)

    monkeypatch.setattr(config.os, "fsync", recording_fsync)
    monkeypatch.setattr(config.os, "replace", recording_replace)

    config.atomic_write_text(target, '{"a": 1}')

    assert calls == ["fsync", "replace"]


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
