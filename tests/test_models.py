from audible_tui.models import Book


def test_author_display_joins_multiple_authors():
    book = Book(asin="A1", title="T", authors=["Alice", "Bob"])
    assert book.author_display == "Alice, Bob"


def test_author_display_falls_back_when_no_authors():
    book = Book(asin="A1", title="T", authors=[])
    assert book.author_display == "Unknown"


def test_series_display_empty_when_no_series():
    book = Book(asin="A1", title="T")
    assert book.series_display == ""


def test_series_display_includes_sequence():
    book = Book(asin="A1", title="T", series="Bloodwing Academy", series_sequence="1")
    assert book.series_display == "Bloodwing Academy #1"


def test_series_display_omits_sequence_when_absent():
    book = Book(asin="A1", title="T", series="Bloodwing Academy")
    assert book.series_display == "Bloodwing Academy"


def test_runtime_display_hours_and_minutes():
    book = Book(asin="A1", title="T", runtime_min=7 * 60 + 15)
    assert book.runtime_display == "7h 15m"


def test_runtime_display_whole_hours_only():
    book = Book(asin="A1", title="T", runtime_min=5 * 60)
    assert book.runtime_display == "5h"


def test_runtime_display_minutes_only():
    book = Book(asin="A1", title="T", runtime_min=45)
    assert book.runtime_display == "45m"


def test_runtime_display_zero():
    book = Book(asin="A1", title="T", runtime_min=0)
    assert book.runtime_display == "0m"


def test_progress_pct_zero_duration_does_not_divide_by_zero():
    book = Book(asin="A1", title="T", progress_ms=1000, duration_ms=0)
    assert book.progress_pct == 0


def test_progress_pct_normal_case():
    book = Book(asin="A1", title="T", progress_ms=30_000, duration_ms=100_000)
    assert book.progress_pct == 30


def test_progress_pct_rounds_to_nearest():
    # 1/3 -> 33.33...% should round to 33, not truncate oddly
    book = Book(asin="A1", title="T", progress_ms=1, duration_ms=3)
    assert book.progress_pct == 33


def test_progress_pct_clamped_at_100_even_if_progress_exceeds_duration():
    # last_position_heard from Audible can exceed our locally-known duration
    # (e.g. duration estimate is stale); the UI should never show over 100%.
    book = Book(asin="A1", title="T", progress_ms=150_000, duration_ms=100_000)
    assert book.progress_pct == 100
