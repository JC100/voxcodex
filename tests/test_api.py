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
        get_pages=[FakeJsonResponse({"items": [{"asin": "B001", "title": "Book One"}]})]
    )
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert [b.asin for b in books] == ["B001"]
    assert len(client.get_calls) == 1


def test_get_library_paginates_until_a_short_page():
    # First page full of exactly num_results (1000) items triggers another
    # fetch; the loop only stops once a page comes back shorter than that.
    full_page_items = [{"asin": f"B{i:04d}"} for i in range(1000)]
    short_page_items = [{"asin": "LAST"}]
    client = FakeAudibleClient(
        get_pages=[
            FakeJsonResponse({"items": full_page_items}),
            FakeJsonResponse({"items": short_page_items}),
        ]
    )
    api = _api_with_fake_client(client)

    books = api.get_library()

    assert len(books) == 1001
    assert books[-1].asin == "LAST"
    assert len(client.get_calls) == 2
    assert client.get_calls[0][1]["page"] == 1
    assert client.get_calls[1][1]["page"] == 2


# -- get_license -------------------------------------------------------


def _license_response(status_code="Granted", content_url="https://cdn/x.aaxc", position_ms=None):
    content_license = {
        "status_code": status_code,
        "content_metadata": {
            "content_url": {"offline_url": content_url} if content_url else {},
            "content_reference": {"content_format": "AAXC"},
        },
    }
    if position_ms is not None:
        content_license["last_position_heard"] = {"position_ms": position_ms}
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


def test_get_license_defaults_to_zero_position_when_absent():
    client = FakeAudibleClient(post_response=_license_response())
    api = _api_with_fake_client(client)

    license_ = api.get_license("B001")

    assert license_.last_position_ms == 0


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
