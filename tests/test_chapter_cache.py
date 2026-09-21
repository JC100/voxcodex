import threading

import pytest

from voxcodex import config
from voxcodex.services import chapter_cache
from voxcodex.services.api import Chapter


@pytest.fixture(autouse=True)
def _cache_in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHAPTER_CACHE_FILE", tmp_path / "chapter_cache.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "data" / "downloads")


def _chapters():
    return [
        Chapter(title="Chapter 1", start_ms=0, length_ms=60_000),
        Chapter(title="Chapter 2", start_ms=60_000, length_ms=45_000),
    ]


def test_load_returns_empty_dict_when_no_cache_exists():
    assert chapter_cache.load() == {}


def test_save_then_load_round_trips_chapters():
    chapter_cache.save({"B1": _chapters(), "B2": []})

    result = chapter_cache.load()

    assert result == {"B1": _chapters(), "B2": []}


def test_load_returns_empty_dict_for_corrupted_json():
    config.CHAPTER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CHAPTER_CACHE_FILE.write_text("{not valid json")

    assert chapter_cache.load() == {}


def test_load_returns_empty_dict_when_chapter_schema_has_drifted():
    config.CHAPTER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CHAPTER_CACHE_FILE.write_text(
        '{"B1": [{"title": "One", "a_field_that_no_longer_exists": true}]}'
    )

    assert chapter_cache.load() == {}


@pytest.mark.parametrize("raw", ["[]", "null", '"a string"', "42", "true"])
def test_load_returns_empty_dict_for_a_non_dict_top_level_shape(raw):
    # M11: a cache file whose top-level JSON is valid but not an object --
    # a cache file damaged by truncation, a sync-tool mishap, or a future
    # schema change -- made data.items() raise AttributeError, which
    # wasn't in the except tuple. Reproduced against all three JSON shapes
    # that aren't objects (list, null, and non-dict scalars raise the same
    # way once .items() is called on them).
    config.CHAPTER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CHAPTER_CACHE_FILE.write_text(raw)

    assert chapter_cache.load() == {}


def test_save_overwrites_previous_cache():
    chapter_cache.save({"B1": _chapters()})
    chapter_cache.save({"B2": []})

    assert chapter_cache.load() == {"B2": []}


def test_save_tolerates_the_dict_being_mutated_concurrently():
    """L19: the caller passes its own live dict (LibraryScreen's
    self._chapter_cache), which a background worker (_fetch_chapter_counts)
    can be extending on another thread at the same moment save() iterates
    it -- raising "dictionary changed size during iteration", which isn't
    an OSError, so save()'s own except wouldn't have caught it, silently
    losing that session's chapter cache write. dict(...) snapshots
    atomically before iterating, closing the race."""
    # A large dict (a wide iteration window per save() call, so the race
    # actually lands within a handful of attempts) and a small sleep per
    # mutation (not a tight busy loop -- that starves the main thread of
    # the GIL badly enough that save() barely makes progress at all,
    # rather than genuinely racing it). Calibrated to reliably reproduce
    # "dictionary changed size during iteration" within ~50 attempts
    # against the pre-fix code, and to complete in well under a second
    # either way.
    chapters_by_asin = {f"B{i}": _chapters() for i in range(2000)}
    stop = threading.Event()

    def mutate():
        i = 2000
        while not stop.wait(0.0002):
            chapters_by_asin[f"B{i}"] = _chapters()
            i += 1

    thread = threading.Thread(target=mutate, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            chapter_cache.save(chapters_by_asin)  # must not raise
    finally:
        stop.set()
        thread.join(timeout=2)


def test_save_is_a_best_effort_write_that_does_not_raise(monkeypatch):
    monkeypatch.setattr(config, "CHAPTER_CACHE_FILE", config.DATA_DIR / "nonexistent" / "x" / "y")

    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(config, "atomic_write_text", boom)

    chapter_cache.save({"B1": _chapters()})  # must not raise
