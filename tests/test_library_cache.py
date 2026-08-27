import pytest

from voxcodex import config
from voxcodex.models import Book
from voxcodex.services import library_cache


@pytest.fixture(autouse=True)
def _cache_in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY_CACHE_FILE", tmp_path / "library_cache.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "data" / "downloads")


def _book(asin="B1", title="Title"):
    return Book(asin=asin, title=title, authors=["Author"], runtime_min=90, progress_ms=1000)


def test_load_returns_none_when_no_cache_exists():
    assert library_cache.load() is None


def test_save_then_load_round_trips_books():
    books = [_book("B1", "One"), _book("B2", "Two")]

    library_cache.save(books)
    result = library_cache.load()

    assert result is not None
    loaded_books, cached_at = result
    assert [b.asin for b in loaded_books] == ["B1", "B2"]
    assert [b.title for b in loaded_books] == ["One", "Two"]
    assert cached_at > 0


def test_round_trip_preserves_book_fields():
    book = _book("B1", "One")
    library_cache.save([book])

    loaded_books, _ = library_cache.load()

    assert loaded_books[0] == book


def test_load_returns_none_for_corrupted_json():
    config.LIBRARY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LIBRARY_CACHE_FILE.write_text("{not valid json")

    assert library_cache.load() is None


def test_load_returns_none_for_unrecognized_shape():
    config.LIBRARY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LIBRARY_CACHE_FILE.write_text('{"unexpected": "shape"}')

    assert library_cache.load() is None


def test_load_returns_none_when_book_schema_has_drifted():
    config.LIBRARY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LIBRARY_CACHE_FILE.write_text(
        '{"cached_at": 123.0, "books": [{"asin": "B1", "title": "One", '
        '"a_field_that_no_longer_exists": true}]}'
    )

    assert library_cache.load() is None


def test_save_overwrites_previous_cache():
    library_cache.save([_book("B1", "One")])
    library_cache.save([_book("B2", "Two")])

    loaded_books, _ = library_cache.load()

    assert [b.asin for b in loaded_books] == ["B2"]
