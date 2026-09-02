import time
from datetime import datetime, timezone

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


def test_last_played_in_app_defaults_to_none(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.last_played_in_app is None


def test_last_played_in_app_round_trips_with_a_recent_timestamp(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    before = time.time()

    s.set_last_played_in_app("B001")

    asin, updated_at = s.last_played_in_app
    assert asin == "B001"
    assert updated_at >= before


def test_last_played_externally_defaults_to_none(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    assert s.last_played_externally is None


def test_last_played_externally_round_trips_with_the_given_timestamp(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    when = datetime(2026, 8, 27, 8, 56, 11, tzinfo=timezone.utc)

    s.set_last_played_externally("B002", when)

    asin, updated_at = s.last_played_externally
    assert asin == "B002"
    assert updated_at == when.timestamp()


def test_last_played_in_app_and_externally_are_independent(tmp_path):
    s = settings.Settings(path=tmp_path / "settings.json")
    s.set_last_played_in_app("B001")
    s.set_last_played_externally("B002", datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert s.last_played_in_app[0] == "B001"
    assert s.last_played_externally[0] == "B002"


def test_malformed_last_played_entry_is_ignored_not_raised(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"last_played_in_app": {"asin": "B001"}}')  # missing updated_at

    s = settings.Settings(path=path)

    assert s.last_played_in_app is None


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
