from voxcodex.models import Book


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


def test_runtime_display_blank_when_unknown():
    # L24: runtime_min == 0 means "unknown", same as duration_ms == 0 does
    # for progress_pct/time_left_display -- used to render "0m" here,
    # inconsistent with those blank renders for the same "unknown" state.
    book = Book(asin="A1", title="T", runtime_min=0)
    assert book.runtime_display == ""


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


def test_time_left_display_empty_when_no_duration_known():
    book = Book(asin="A1", title="T", progress_ms=1000, duration_ms=0)
    assert book.time_left_display == ""


def test_time_left_display_hours_and_minutes():
    book = Book(asin="A1", title="T", progress_ms=0, duration_ms=(7 * 3600 + 15 * 60) * 1000)
    assert book.time_left_display == "7h 15m left"


def test_time_left_display_whole_hours_only():
    book = Book(asin="A1", title="T", progress_ms=0, duration_ms=5 * 3600 * 1000)
    assert book.time_left_display == "5h left"


def test_time_left_display_minutes_only():
    book = Book(asin="A1", title="T", progress_ms=0, duration_ms=45 * 60 * 1000)
    assert book.time_left_display == "45m left"


def test_time_left_display_done_when_finished():
    book = Book(asin="A1", title="T", progress_ms=100_000, duration_ms=100_000)
    assert book.time_left_display == "done"


def test_time_left_display_never_goes_negative_past_duration():
    book = Book(asin="A1", title="T", progress_ms=150_000, duration_ms=100_000)
    assert book.time_left_display == "done"


def test_time_left_display_sub_minute_remainder_is_not_done():
    # L24: a never-started 20s title has remaining_ms == 20_000, which used
    # to round to 0 minutes and get reported as "done" -- indistinguishable
    # from an actually-finished book -- even though it hasn't been played
    # at all.
    book = Book(asin="A1", title="T", progress_ms=0, duration_ms=20_000)
    assert book.time_left_display == "<1m left"


def test_time_left_display_done_only_when_truly_zero_remaining():
    book = Book(asin="A1", title="T", progress_ms=20_000, duration_ms=20_000)
    assert book.time_left_display == "done"


def test_chapter_display_blank_when_not_yet_fetched():
    book = Book(asin="A1", title="T")
    assert book.chapter_display == ""


def test_chapter_display_blank_when_confirmed_no_chapters():
    book = Book(asin="A1", title="T", chapter_total=0)
    assert book.chapter_display == ""


def test_chapter_display_shows_current_over_total():
    book = Book(asin="A1", title="T", chapter_total=15, chapter_current=6)
    assert book.chapter_display == "6/15"


def test_chapter_display_defaults_current_to_1_when_unknown():
    book = Book(asin="A1", title="T", chapter_total=15, chapter_current=None)
    assert book.chapter_display == "1/15"


def test_chapter_display_shows_0_for_a_not_yet_started_book():
    # chapter_current=0 is a real, meaningful value (not started), distinct
    # from None (not fetched yet) -- must not fall back to "1/15".
    book = Book(asin="A1", title="T", chapter_total=15, chapter_current=0)
    assert book.chapter_display == "0/15"
