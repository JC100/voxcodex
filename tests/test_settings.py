import pytest

from voxcodex.services import settings


def test_playback_speed_defaults_to_1x(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.playback_speed == 1.0


def test_playback_speed_round_trips(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_playback_speed(1.5)
    assert s.playback_speed == 1.5


def test_playback_volume_defaults_to_100(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.playback_volume == 100.0


def test_playback_volume_round_trips(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_playback_volume(65.0)
    assert s.playback_volume == 65.0


def test_settings_persist_across_instances(tmp_path):
    path = tmp_path / "settings.json"
    settings.Settings(path=path).set_playback_speed(2.0)

    reloaded = settings.Settings(path=path)
    assert reloaded.playback_speed == 2.0


def test_settings_survive_corrupted_json_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json")

    s = settings.Settings(path=path)  # must not raise

    assert s.playback_speed == 1.0


def test_library_sort_key_defaults_to_recent(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.library_sort_key == "recent"


def test_library_sort_key_round_trips(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_library_sort_key("title")
    assert s.library_sort_key == "title"


def test_library_filter_key_defaults_to_all(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.library_filter_key == "all"


def test_library_filter_key_round_trips(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_library_filter_key("downloaded")
    assert s.library_filter_key == "downloaded"


def test_progress_display_mode_defaults_to_percent(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.progress_display_mode == "percent"


def test_progress_display_mode_round_trips(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_progress_display_mode("both")
    assert s.progress_display_mode == "both"


def test_theme_defaults_to_textual_dark(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.theme == "textual-dark"


def test_theme_round_trips(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_theme("nord")
    assert s.theme == "nord"


# -- concurrent / independent instances (H2) ----------------------------------


def test_two_instances_do_not_clobber_each_others_unseen_writes(tmp_path):
    """The bug: the app, library screen and player screen each held their own
    Settings over one file, and every setter serialised that instance's whole
    (possibly stale) copy -- so changing the theme reverted a sort cycled in
    the library. Each setter now reload-modify-writes just its own key."""
    path = tmp_path / "settings.json"
    a = settings.Settings(path=path)
    b = settings.Settings(path=path)  # both start from the same empty file

    a.set_library_sort_key("title")   # a writes; b's in-memory copy is now stale
    b.set_theme("nord")               # b must not wipe a's sort key

    reloaded = settings.Settings(path=path)
    assert reloaded.library_sort_key == "title"
    assert reloaded.theme == "nord"


def test_setter_picks_up_a_concurrent_change_before_writing(tmp_path):
    path = tmp_path / "settings.json"
    a = settings.Settings(path=path)
    b = settings.Settings(path=path)

    b.set_playback_volume(40.0)
    a.set_playback_speed(2.0)  # a never saw b's volume write

    reloaded = settings.Settings(path=path)
    assert reloaded.playback_speed == 2.0
    assert reloaded.playback_volume == 40.0


def test_a_failed_write_keeps_the_previous_settings_intact(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    settings.Settings(path=path).set_theme("gruvbox")

    monkeypatch.setattr(
        settings.config, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        settings.Settings(path=path).set_theme("nord")

    monkeypatch.undo()
    assert settings.Settings(path=path).theme == "gruvbox"


# -- default path resolution (L6) -------------------------------------------


def test_default_path_is_resolved_at_construction_not_at_import(tmp_path, monkeypatch):
    """Settings(path=config.SETTINGS_FILE) as a default argument would bind
    whatever config.SETTINGS_FILE was at import time -- monkeypatching
    config afterwards wouldn't be seen without also patching the Settings
    class itself. Resolving the default inside __init__ instead means this
    monkeypatch on `config` alone is enough."""
    patched_path = tmp_path / "settings.json"
    monkeypatch.setattr(settings.config, "SETTINGS_FILE", patched_path)

    settings.Settings().set_theme("nord")

    assert patched_path.exists()
    assert settings.Settings().theme == "nord"
