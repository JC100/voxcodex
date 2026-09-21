import pytest

from voxcodex.services.api import (
    AudibleAPI,
    LicenseDenied,
    NoDownloadUrl,
    _book_from_item,
)


def _api_with_fake_client(client):
    """Builds an AudibleAPI without going through __init__ (which needs a
    real audible.Authenticator + real httpx.Client construction)."""
    api = AudibleAPI.__new__(AudibleAPI)
    api._auth = None
    api.client = client
    return api


# -- _book_from_item -------------------------------------------------------


def test_book_from_item_basic_fields():
    item = {
        "asin": "B001",
        "title": "How to Win Friends & Influence People",
        "authors": [{"name": "Dale Carnegie"}],
        "narrators": [{"name": "Andrew MacMillan"}],
        "runtime_length_min": 435,
        "purchase_date": "2020-01-01",
    }
    book = _book_from_item(item)
    assert book.asin == "B001"
    assert book.title == "How to Win Friends & Influence People"
    assert book.authors == ["Dale Carnegie"]
    assert book.narrators == ["Andrew MacMillan"]
    assert book.runtime_min == 435
    assert book.purchase_date == "2020-01-01"


def test_book_from_item_defaults_when_fields_missing():
    book = _book_from_item({})
    assert book.asin == ""
    assert book.title == "Untitled"
    assert book.authors == []
    assert book.narrators == []
    assert book.series == ""
    assert book.runtime_min == 0
    assert book.progress_ms == 0
    assert book.is_finished is False


def test_book_from_item_multiple_authors():
    item = {"authors": [{"name": "A"}, {"name": "B"}, {"name": None}]}
    book = _book_from_item(item)
    # entries without a name are dropped, not turned into "None"
    assert book.authors == ["A", "B"]


def test_book_from_item_series_with_sequence():
    item = {"series": [{"title": "Bloodwing Academy", "sequence": "1"}]}
    book = _book_from_item(item)
    assert book.series == "Bloodwing Academy"
    assert book.series_sequence == "1"


def test_book_from_item_no_series():
    item = {"series": []}
    book = _book_from_item(item)
    assert book.series == ""
    assert book.series_sequence == ""


def test_book_from_item_cover_prefers_500_over_300():
    item = {"product_images": {"300": "small.jpg", "500": "big.jpg"}}
    book = _book_from_item(item)
    assert book.cover_url == "big.jpg"


def test_book_from_item_cover_falls_back_to_300():
    item = {"product_images": {"300": "small.jpg"}}
    book = _book_from_item(item)
    assert book.cover_url == "small.jpg"


def test_book_from_item_computes_progress_ms_from_percent_complete():
    item = {"runtime_length_min": 100, "percent_complete": 50}
    book = _book_from_item(item)
    assert book.duration_ms == 100 * 60_000
    assert book.progress_ms == 50 * 60_000


def test_book_from_item_zero_runtime_gives_zero_progress_even_with_percent():
    item = {"runtime_length_min": 0, "percent_complete": 50}
    book = _book_from_item(item)
    assert book.duration_ms == 0
    assert book.progress_ms == 0


def test_book_from_item_is_finished_flag():
    assert _book_from_item({"is_finished": True}).is_finished is True
    assert _book_from_item({"is_finished": False}).is_finished is False


# -- get_library -------------------------------------------------------


class FakeJsonResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeAudibleClient:
    def __init__(self, get_pages=None, post_response=None):
        self._get_pages = list(get_pages or [])
        self._post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, path, **kwargs):
        self.get_calls.append((path, kwargs))
        return self._get_pages[len(self.get_calls) - 1]

    def post(self, path, **kwargs):
        self.post_calls.append((path, kwargs))
        return self._post_response


def test_get_library_returns_parsed_books_for_a_single_page():
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": [{"asin": "B001", "title": "Book One"}]}),
            FakeJsonResponse({"items": []}),
        ]
    )
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert [b.asin for b in books] == ["B001"]
    assert len(client.get_calls) == 2


def test_get_library_paginates_until_an_empty_page():
    # M4: the loop only stops on a genuinely *empty* page -- a short-but-
    # nonempty page (a server capping page size below `num_results`, say)
    # must not be mistaken for "no more data" and silently truncate.
    full_page_items = [{"asin": f"B{i:04d}"} for i in range(1000)]
    short_page_items = [{"asin": "LAST"}]
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": full_page_items}),
            FakeJsonResponse({"items": short_page_items}),
            FakeJsonResponse({"items": []}),
        ]
    )
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert len(books) == 1001
    assert books[-1].asin == "LAST"
    assert len(client.get_calls) == 3
    assert client.get_calls[0][1]["page"] == 1
    assert client.get_calls[1][1]["page"] == 2
    assert client.get_calls[2][1]["page"] == 3


def test_get_library_stops_on_a_short_nonempty_final_page_too():
    # The common case: the last page is short but still nonempty, and the
    # *next* page comes back empty -- still just one extra request, not a
    # truncation.
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": [{"asin": "ONLY"}]}),
            FakeJsonResponse({"items": []}),
        ]
    )
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert [b.asin for b in books] == ["ONLY"]
    assert len(client.get_calls) == 2


def test_get_library_stops_at_the_page_safety_limit_if_pages_never_go_empty():
    from voxcodex.services import api as api_module

    # Every page comes back "full" (a misbehaving server that never signals
    # the end) -- the hard page cap is what stops this from looping forever.
    pages = [
        FakeJsonResponse({"items": [{"asin": f"P{page}-{i}"} for i in range(1000)]})
        for page in range(api_module._MAX_LIBRARY_PAGES + 5)
    ]
    client = FakeAudibleClient(get_pages=pages)
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert len(client.get_calls) == api_module._MAX_LIBRARY_PAGES
    assert len(books) == api_module._MAX_LIBRARY_PAGES * 1000


def test_get_library_returns_empty_list_for_an_empty_library():
    client = FakeAudibleClient(get_pages=[FakeJsonResponse({"items": []})])
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert books == []
    assert len(client.get_calls) == 1


def test_get_library_skips_items_with_no_asin(caplog):
    # L7: DataTable rows are keyed by asin -- two items defaulting to ""
    # would crash add_row with DuplicateKey the moment the second one
    # rendered. Dropping them here is what keeps the rest of the library
    # loading instead of losing the whole page to one bad item.
    items = [
        {"asin": "B001", "title": "Has an ASIN"},
        {"asin": "", "title": "Missing ASIN"},
        {"title": "No asin key at all"},
    ]
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": items}),
            FakeJsonResponse({"items": []}),
        ]
    )
    api = _api_with_fake_client(client)

    with caplog.at_level("WARNING"):
        books = api.get_library()

    assert [b.asin for b in books] == ["B001"]
    assert "Missing ASIN" in caplog.text
    assert "No asin key at all" in caplog.text


def test_get_library_dedupes_repeated_asins_within_a_page(caplog):
    # H2: table.add_row(..., key=book.asin) raises Textual's DuplicateKey
    # on a repeated key -- keep the first occurrence and drop the rest
    # rather than crashing the library render.
    items = [
        {"asin": "B001", "title": "First copy"},
        {"asin": "B001", "title": "Second copy, same ASIN"},
        {"asin": "B002", "title": "Different book"},
    ]
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": items}),
            FakeJsonResponse({"items": []}),
        ]
    )
    api = _api_with_fake_client(client)

    with caplog.at_level("WARNING"):
        books = api.get_library()

    assert [b.asin for b in books] == ["B001", "B002"]
    assert [b.title for b in books] == ["First copy", "Different book"]
    assert "B001" in caplog.text


def test_get_library_dedupes_an_asin_repeated_across_pages(caplog):
    # A purchase landing mid-pagination shifts the page window, so the
    # same ASIN can legitimately reappear on a later page, not just within
    # one -- the dedupe set must persist across the pagination loop.
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": [{"asin": "B001", "title": "Page 1 copy"}] * 1000}),
            FakeJsonResponse(
                {"items": [{"asin": "B001", "title": "Page 2 copy"}, {"asin": "B002"}]}
            ),
            FakeJsonResponse({"items": []}),
        ]
    )
    api = _api_with_fake_client(client)

    with caplog.at_level("WARNING"):
        books = api.get_library()

    assert [b.asin for b in books] == ["B001", "B002"]


# -- get_license -------------------------------------------------------


def _license_response(
    status_code="Granted", content_url="https://cdn/x.aaxc", position_ms=None,
    last_updated=None, position_status="Exists",
):
    content_license = {
        "status_code": status_code,
        "content_metadata": {
            "content_url": {"offline_url": content_url} if content_url else {},
            "content_reference": {"content_format": "AAXC"},
        },
    }
    if position_ms is not None:
        lph = {"status": position_status, "position_ms": position_ms}
        if last_updated is not None:
            lph["last_updated"] = last_updated
        content_license["last_position_heard"] = lph
    return {"content_license": content_license}


def test_get_license_happy_path_without_drm_voucher():
    # No "license_response" key -> the decrypt-voucher path is skipped, so
    # this doesn't need a real Authenticator/crypto to exercise.
    client = FakeAudibleClient(post_response=_license_response(position_ms=42_000))
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.asin == "B001"
    assert license_.content_url == "https://cdn/x.aaxc"
    assert license_.codec == "AAXC"
    assert license_.key == ""
    assert license_.iv == ""
    assert license_.last_position_ms == 42_000


def test_get_license_raises_on_denied_status():
    resp = _license_response(status_code="Denied")
    resp["content_license"]["message"] = "You do not own this title"
    client = FakeAudibleClient(post_response=resp)
    api = _api_with_fake_client(client)

    with pytest.raises(LicenseDenied, match="You do not own this title"):
        api.get_license("B001")


def test_get_license_raises_when_no_content_url():
    client = FakeAudibleClient(post_response=_license_response(content_url=None))
    api = _api_with_fake_client(client)

    with pytest.raises(NoDownloadUrl):
        api.get_license("B001")


def test_get_license_posts_to_the_asin_specific_endpoint():
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    api.get_license("B12345")

    (path, _kwargs), = client.post_calls
    assert path == "content/B12345/licenserequest"


def test_get_license_accepts_normal_quality():
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    api.get_license("B001", quality="normal")

    (_path, kwargs), = client.post_calls
    assert kwargs["body"]["quality"] == "Normal"


def test_get_license_rejects_an_unrecognized_quality():
    # L12: this used to silently coerce any non-"normal" value (a typo
    # included) to "High" instead of rejecting it.
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    with pytest.raises(ValueError, match="quality"):
        api.get_license("B001", quality="hihg")


def test_get_license_defaults_to_zero_position_when_absent():
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.last_position_ms == 0
    assert license_.last_position_updated_at is None


def test_get_license_extracts_last_position_updated_at():
    import datetime

    client = FakeAudibleClient(
        post_response=_license_response(
            position_ms=221_643, last_updated="2026-08-30 10:54:00.671"
        )
    )
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    expected = datetime.datetime(
        2026, 8, 30, 10, 54, 0, 671000, tzinfo=datetime.UTC
    )
    assert license_.last_position_updated_at == expected.timestamp()


def test_get_license_position_updated_at_none_without_a_timestamp():
    client = FakeAudibleClient(post_response=_license_response(position_ms=1000))
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.last_position_updated_at is None


def test_get_license_ignores_a_does_not_exist_position(monkeypatch):
    # M1: progress._existing_last_position_heard rejects any
    # last_position_heard record whose status isn't "Exists" (Audible
    # returns "DoesNotExist" -- no real position -- for a never-played
    # title). get_license parses the same field shape and used to have no
    # such guard, so a DoesNotExist record with a fresh timestamp could
    # feed a bogus 0 in as the "most recent" position, silently resetting
    # a real resume point.
    client = FakeAudibleClient(
        post_response=_license_response(
            position_ms=999_000, last_updated="2026-08-30 10:54:00.671",
            position_status="DoesNotExist",
        )
    )
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.last_position_ms == 0
    assert license_.last_position_updated_at is None


def test_get_license_extracts_acr_from_content_reference():
    # `acr` is what push_last_position needs to key the record it writes back
    # to Audible's cross-device sync -- see that method's docstring and
    # docs/whispersync-research.md for why a placeholder here silently
    # writes a record nothing ever reads.
    resp = _license_response()
    resp["content_license"]["content_metadata"]["content_reference"].update(
        {"acr": "CR!SOMEACR"}
    )
    client = FakeAudibleClient(post_response=resp)
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.acr == "CR!SOMEACR"


def test_get_license_defaults_acr_to_empty_when_absent():
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.acr == ""


def test_get_license_extracts_license_id():
    # `license_id` is what push_listening_session needs to report a
    # listening session against -- see that method's docstring.
    resp = _license_response()
    resp["content_license"]["license_id"] = "40f2193e-602f-4fb2-8905-90451f2ed5e0"
    client = FakeAudibleClient(post_response=resp)
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.license_id == "40f2193e-602f-4fb2-8905-90451f2ed5e0"


def test_get_license_defaults_license_id_to_empty_when_absent():
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.license_id == ""


def test_get_license_decrypts_voucher_when_license_response_present(monkeypatch):
    """The real, common case for DRM-protected (Adrm) content: the license
    response includes an encrypted `license_response` blob that has to be
    decrypted to get the AES key/iv. Everything above this test avoided that
    path entirely to dodge needing real crypto -- which meant the actual
    happy path for protected content was unexercised. Faking only the crypto
    call itself (not the whole get_license flow) closes that gap."""
    import voxcodex.services.api as api_module

    resp = _license_response()
    resp["content_license"]["license_response"] = "opaque-encrypted-blob"
    client = FakeAudibleClient(post_response=resp)
    api = _api_with_fake_client(client)

    captured_args = {}

    def fake_decrypt(auth, lr):
        captured_args["auth"] = auth
        captured_args["lr"] = lr
        return {"key": "decrypted-key", "iv": "decrypted-iv"}

    monkeypatch.setattr(api_module, "decrypt_voucher_from_licenserequest", fake_decrypt)

    license_ = api.get_license("B001")

    assert license_.key == "decrypted-key"
    assert license_.iv == "decrypted-iv"
    # decrypt_voucher_from_licenserequest must get this account's auth object
    # and the *whole* license response (not just content_license) -- it needs
    # top-level fields the decryption depends on.
    assert captured_args["auth"] is api._auth
    assert captured_args["lr"] is resp


# -- get_chapters -------------------------------------------------------


class FakeMetadataClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self._response


def test_get_chapters_parses_flat_chapter_list():
    response = {
        "content_metadata": {
            "chapter_info": {
                "chapters": [
                    {"title": "Opening Credits", "start_offset_ms": 0, "length_ms": 5000},
                    {"title": "Chapter 1", "start_offset_ms": 5000, "length_ms": 120000},
                ]
            }
        }
    }
    api = _api_with_fake_client(FakeMetadataClient(response))

    chapters = api.get_chapters("B001")

    assert [c.title for c in chapters] == ["Opening Credits", "Chapter 1"]
    assert [c.start_ms for c in chapters] == [0, 5000]
    assert [c.length_ms for c in chapters] == [5000, 120000]


def test_get_chapters_returns_empty_list_when_no_chapter_info():
    # e.g. podcasts/samples/older titles that simply have none
    api = _api_with_fake_client(FakeMetadataClient({"content_metadata": {}}))
    assert api.get_chapters("B001") == []


def test_get_chapters_requests_the_metadata_endpoint_for_the_asin():
    client = FakeMetadataClient({"content_metadata": {"chapter_info": {"chapters": []}}})
    api = _api_with_fake_client(client)

    api.get_chapters("B12345")

    (path, kwargs), = client.calls
    assert path == "content/B12345/metadata"
    assert kwargs["response_groups"] == "chapter_info"


def test_get_chapters_accepts_normal_quality():
    client = FakeMetadataClient({"content_metadata": {"chapter_info": {"chapters": []}}})
    api = _api_with_fake_client(client)

    api.get_chapters("B001", quality="normal")

    (_path, kwargs), = client.calls
    assert kwargs["quality"] == "Normal"


def test_get_chapters_rejects_an_unrecognized_quality():
    client = FakeMetadataClient({"content_metadata": {"chapter_info": {"chapters": []}}})
    api = _api_with_fake_client(client)

    with pytest.raises(ValueError, match="quality"):
        api.get_chapters("B001", quality="hihg")


# -- push_last_position + set_finished ------------------------------------
#
# Both are writes through `client.put` (PUT /1.0/lastpositions/{asin} and
# PUT /1.0/stats/events respectively), so this one fake models both.


class FakePutClient:
    def __init__(self, exc=None):
        self.exc = exc
        self.put_calls = []

    def put(self, path, body=None, response_callback=None, **kwargs):
        self.put_calls.append((path, body, kwargs))
        if self.exc is not None:
            raise self.exc


def test_push_last_position_raises_without_acr():
    client = FakePutClient()
    api = _api_with_fake_client(client)
    with pytest.raises(ValueError):
        api.push_last_position("B001", "", 1000)
    assert client.put_calls == []


def test_push_last_position_puts_json_to_the_lastpositions_endpoint():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.push_last_position("B001", "CR!ABC", 12345)

    (path, body, _), = client.put_calls
    assert path == "lastpositions/B001"
    assert body == {"acr": "CR!ABC", "asin": "B001", "position_ms": 12345}


def test_push_last_position_propagates_http_errors():
    client = FakePutClient(exc=RuntimeError("boom"))
    api = _api_with_fake_client(client)

    with pytest.raises(RuntimeError):
        api.push_last_position("B001", "CR!ABC", 1000)


def test_set_finished_puts_a_manual_mark_finished_event():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.set_finished("B001", True)

    (path, body, _), = client.put_calls
    assert path == "stats/events"
    (event,) = body["stats"]
    assert event["event_type"] == "ManualMarkAsFinished"
    assert event["asin"] == "B001"
    assert event["event_timestamp"].endswith("Z")
    assert event["store"] == "Audible"


def test_set_finished_false_sends_the_unfinished_event():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.set_finished("B001", False)

    (_, body, _), = client.put_calls
    assert body["stats"][0]["event_type"] == "ManualMarkAsUnfinished"


def test_set_finished_propagates_http_errors():
    client = FakePutClient(exc=RuntimeError("boom"))
    api = _api_with_fake_client(client)

    with pytest.raises(RuntimeError):
        api.set_finished("B001", True)


# -- push_listening_session -------------------------------------------------
#
# Payload shape confirmed against the real Android app's own traffic
# (2026-09-21, see docs/library-progress-sync-investigation.md) -- not
# guessed. An earlier guessed shape drove `percent_complete` to 0% instead
# of the real value.


def _dt(hour, minute, second=0, microsecond=0):
    from datetime import datetime, UTC

    return datetime(2026, 9, 21, hour, minute, second, microsecond, tzinfo=UTC)


def test_push_listening_session_sends_start_listening_and_listening_events():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.push_listening_session(
        "B001", "lic-123", 100_000, 250_000,
        _dt(10, 0, 0, 59_000), _dt(10, 5, 13, 671_000),
        19_040_890, 1.2, "Download",
    )

    (path, body, _), = client.put_calls
    assert path == "stats/events"
    start_event, listening_event = body["stats"]

    assert start_event["event_type"] == "StartListening"
    assert start_event["asin"] == "B001"
    assert start_event["license_id"] == "lic-123"
    assert start_event["event_timestamp"] == "2026-09-21T10:00:00.059Z"
    assert start_event["event_start_position"] == 100_000
    assert start_event["event_end_position"] == 0
    assert start_event["length_of_book"] == 19_040_890
    assert start_event["narration_speed"] == 1.2
    assert start_event["delivery_type"] == "Download"
    assert "event_end_timestamp" not in start_event

    assert listening_event["event_type"] == "Listening"
    assert listening_event["event_timestamp"] == "2026-09-21T10:00:00.059Z"
    assert listening_event["event_end_timestamp"] == "2026-09-21T10:05:13.671Z"
    assert listening_event["event_start_position"] == 100_000
    assert listening_event["event_end_position"] == 250_000

    # Same session_id ties both events together, matching the real app.
    assert start_event["session_id"] == listening_event["session_id"]


def test_push_listening_session_skips_without_a_license_id():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.push_listening_session(
        "B001", "", 0, 1000, _dt(10, 0), _dt(10, 1), 100_000, 1.0, "Streaming",
    )

    assert client.put_calls == []


def test_push_listening_session_skips_when_no_forward_progress():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.push_listening_session(
        "B001", "lic-123", 5000, 5000, _dt(10, 0), _dt(10, 1), 100_000, 1.0, "Streaming",
    )

    assert client.put_calls == []


def test_push_listening_session_skips_when_position_went_backward():
    client = FakePutClient()
    api = _api_with_fake_client(client)

    api.push_listening_session(
        "B001", "lic-123", 5000, 1000, _dt(10, 0), _dt(10, 1), 100_000, 1.0, "Streaming",
    )

    assert client.put_calls == []


def test_push_listening_session_propagates_http_errors():
    client = FakePutClient(exc=RuntimeError("boom"))
    api = _api_with_fake_client(client)

    with pytest.raises(RuntimeError):
        api.push_listening_session(
            "B001", "lic-123", 0, 1000, _dt(10, 0), _dt(10, 1), 100_000, 1.0, "Streaming",
        )
