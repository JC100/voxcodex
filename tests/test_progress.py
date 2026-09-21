import time
from datetime import datetime

from voxcodex.services import progress


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeAPI:
    def __init__(self, response=None, exc=None):
        self.client = FakeClient(response, exc)


# -- ProgressStore -----------------------------------------------------


def test_progress_store_defaults_to_zero_for_unknown_asin(tmp_path):
    store = progress.ProgressStore(path=tmp_path / "progress.json")
    assert store.get_position_ms("UNKNOWN") == 0


def test_progress_store_round_trips_position(tmp_path):
    store = progress.ProgressStore(path=tmp_path / "progress.json")
    store.set_position_ms("B001", 12_345, duration_ms=100_000)
    assert store.get_position_ms("B001") == 12_345


def test_progress_store_persists_across_instances(tmp_path):
    path = tmp_path / "progress.json"
    progress.ProgressStore(path=path).set_position_ms("B001", 99_000)

    reloaded = progress.ProgressStore(path=path)
    assert reloaded.get_position_ms("B001") == 99_000


def test_progress_store_survives_corrupted_json_file(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text("{not valid json")

    store = progress.ProgressStore(path=path)  # must not raise

    assert store.get_position_ms("B001") == 0


def test_progress_store_set_position_without_duration_does_not_error(tmp_path):
    store = progress.ProgressStore(path=tmp_path / "progress.json")
    store.set_position_ms("B001", 500)
    assert store.get_position_ms("B001") == 500


def test_progress_store_write_does_not_drop_another_titles_entry(tmp_path):
    """The player checkpoints position on a timer now, so two stores over the
    same file (or the same store after an external write) must merge, not
    overwrite -- a stale in-memory copy can't wipe B002."""
    path = tmp_path / "progress.json"
    a = progress.ProgressStore(path=path)
    b = progress.ProgressStore(path=path)

    a.set_position_ms("B001", 1_000)
    b.set_position_ms("B002", 2_000)  # b never saw a's B001 write

    reloaded = progress.ProgressStore(path=path)
    assert reloaded.get_position_ms("B001") == 1_000
    assert reloaded.get_position_ms("B002") == 2_000


# -- fetch_remote_annotations / positions_from_annotations --------------
#
# Response shape confirmed directly against a live account (see
# progress.py's module docstring for why that matters): a top-level
# "asin_last_position_heard_annots" list, each entry an asin plus a nested
# last_position_heard dict with a "status" that's "Exists" or
# "DoesNotExist" -- titles never played anywhere have the latter, with no
# position/timestamp fields at all.


def _annotations_response(records):
    return {"asin_last_position_heard_annots": records, "response_groups": ["always-returned"]}


def _existing(asin, position_ms, last_updated="2026-08-20 23:35:05.608"):
    return {
        "asin": asin,
        "last_position_heard": {
            "status": "Exists",
            "position_ms": position_ms,
            "last_updated": last_updated,
        },
    }


def _does_not_exist(asin):
    return {"asin": asin, "last_position_heard": {"status": "DoesNotExist"}}


def test_fetch_remote_annotations_empty_asins_short_circuits():
    api = FakeAPI(response={"should": "not be read"})
    result = progress.fetch_remote_annotations(api, [])
    assert result == []
    assert api.client.calls == []  # no network call should have been made


def test_fetch_remote_annotations_extracts_the_records_list():
    records = [_existing("B001", 1000)]
    api = FakeAPI(response=_annotations_response(records))
    assert progress.fetch_remote_annotations(api, ["B001"]) == records


def test_fetch_remote_annotations_returns_empty_on_unrecognized_shape():
    api = FakeAPI(response="totally unexpected string response")
    assert progress.fetch_remote_annotations(api, ["B001"]) == []


def test_fetch_remote_annotations_returns_empty_when_client_raises():
    api = FakeAPI(exc=RuntimeError("network exploded"))
    assert progress.fetch_remote_annotations(api, ["B001"]) == []


def test_fetch_remote_annotations_passes_comma_joined_asins():
    api = FakeAPI(response=_annotations_response([]))
    progress.fetch_remote_annotations(api, ["B001", "B002"])
    (_path, kwargs), = api.client.calls
    assert kwargs["asins"] == "B001,B002"


class _ChunkedFakeClient:
    """Each .get() call answers from the next queued response, in order --
    lets a test assert on the args of (and merge across) several chunked
    requests, unlike FakeClient's single fixed response."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self._responses[len(self.calls) - 1]


class _ChunkedFakeAPI:
    def __init__(self, responses):
        self.client = _ChunkedFakeClient(responses)


def test_fetch_remote_annotations_chunks_a_large_asin_list():
    # M2: asins=... is one query-string parameter -- at ~11 bytes/ASIN, an
    # unchunked request for a several-hundred-title library is plausibly
    # past a gateway's request-line limit, silently breaking the whole
    # feature with no indication to the user.
    asins = [f"B{i:09d}" for i in range(250)]
    responses = [
        _annotations_response([_existing(asins[0], 1000)]),
        _annotations_response([_existing(asins[100], 2000)]),
        _annotations_response([_existing(asins[200], 3000)]),
    ]
    api = _ChunkedFakeAPI(responses)

    records = progress.fetch_remote_annotations(api, asins)

    assert len(api.client.calls) == 3
    sent_asins = [kwargs["asins"].split(",") for _path, kwargs in api.client.calls]
    assert sent_asins == [asins[:100], asins[100:200], asins[200:250]]
    assert [r["asin"] for r in records] == [asins[0], asins[100], asins[200]]


def test_fetch_remote_annotations_merges_across_a_failed_chunk(caplog):
    asins = [f"B{i:09d}" for i in range(150)]
    responses = [
        RuntimeError("network exploded"),
        _annotations_response([_existing(asins[100], 2000)]),
    ]

    class _MixedFakeClient(_ChunkedFakeClient):
        def get(self, path, **kwargs):
            self.calls.append((path, kwargs))
            response = self._responses[len(self.calls) - 1]
            if isinstance(response, Exception):
                raise response
            return response

    api = _ChunkedFakeAPI([])
    api.client = _MixedFakeClient(responses)

    with caplog.at_level("WARNING"):
        records = progress.fetch_remote_annotations(api, asins)

    assert len(api.client.calls) == 2
    assert [r["asin"] for r in records] == [asins[100]]
    assert "lastpositions fetch failed" in caplog.text


def test_positions_from_annotations_includes_only_existing_positions():
    records = [_existing("B001", 1000), _does_not_exist("B002")]
    assert progress.positions_from_annotations(records) == {"B001": 1000}


def test_positions_from_annotations_empty_when_nothing_exists():
    assert progress.positions_from_annotations([_does_not_exist("B001")]) == {}


def test_fetch_remote_positions_end_to_end():
    records = [_existing("B001", 4242), _does_not_exist("B002")]
    api = FakeAPI(response=_annotations_response(records))
    assert progress.fetch_remote_positions(api, ["B001", "B002"]) == {"B001": 4242}


# -- positions_with_updated_at_from_annotations (M5) ----------------------


def test_positions_with_updated_at_includes_the_timestamp():
    records = [_existing("B001", 4242, last_updated="2026-08-20 23:35:05.608")]
    result = progress.positions_with_updated_at_from_annotations(records)
    assert result.keys() == {"B001"}
    position_ms, updated_at = result["B001"]
    assert position_ms == 4242
    import datetime
    expected = datetime.datetime(2026, 8, 20, 23, 35, 5, 608000, tzinfo=datetime.UTC)
    assert updated_at == expected.timestamp()


def test_positions_with_updated_at_excludes_titles_never_played():
    assert progress.positions_with_updated_at_from_annotations([_does_not_exist("B001")]) == {}


def test_positions_with_updated_at_excludes_an_unparseable_timestamp():
    record = _existing("B001", 4242, last_updated="not-a-timestamp")
    assert progress.positions_with_updated_at_from_annotations([record]) == {}


# -- ProgressStore.get_updated_at (M5) -------------------------------------


def test_get_updated_at_is_none_for_unknown_asin(tmp_path):
    store = progress.ProgressStore(path=tmp_path / "progress.json")
    assert store.get_updated_at("UNKNOWN") is None


def test_get_updated_at_reflects_the_last_set_position_ms_call(tmp_path):
    store = progress.ProgressStore(path=tmp_path / "progress.json")
    before = time.time()
    store.set_position_ms("B001", 1_000)
    after = time.time()
    assert before <= store.get_updated_at("B001") <= after


# -- push_position -------------------------------------------------------


class FakePushAPI:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []
        self.finished_calls = []
        self.listening_session_calls = []

    def push_last_position(self, asin, acr, position_ms):
        self.calls.append((asin, acr, position_ms))
        if self.exc is not None:
            raise self.exc

    def set_finished(self, asin, finished):
        self.finished_calls.append((asin, finished))
        if self.exc is not None:
            raise self.exc

    def push_listening_session(self, *args):
        self.listening_session_calls.append(args)
        if self.exc is not None:
            raise self.exc


def test_push_position_calls_through_and_reports_success():
    api = FakePushAPI()
    assert progress.push_position(api, "B001", "CR!ABC", 5000) is True
    assert api.calls == [("B001", "CR!ABC", 5000)]


def test_push_position_skips_the_call_without_acr():
    api = FakePushAPI()
    assert progress.push_position(api, "B001", "", 5000) is False
    assert api.calls == []


def test_push_position_swallows_failure_and_reports_it():
    api = FakePushAPI(exc=RuntimeError("network exploded"))
    assert progress.push_position(api, "B001", "CR!ABC", 5000) is False


# -- push_finished ------------------------------------------------------


def test_push_finished_calls_through_and_reports_success():
    api = FakePushAPI()
    assert progress.push_finished(api, "B001", True) is True
    assert api.finished_calls == [("B001", True)]


def test_push_finished_passes_through_unfinished_too():
    api = FakePushAPI()
    assert progress.push_finished(api, "B001", False) is True
    assert api.finished_calls == [("B001", False)]


def test_push_finished_swallows_failure_and_reports_it():
    api = FakePushAPI(exc=RuntimeError("network exploded"))
    assert progress.push_finished(api, "B001", True) is False


# -- push_listening_session ----------------------------------------------


def test_push_listening_session_calls_through_and_reports_success():
    api = FakePushAPI()
    start = datetime(2026, 9, 21, 10, 0)
    end = datetime(2026, 9, 21, 10, 5)

    result = progress.push_listening_session(
        api, "B001", "lic-123", 1000, 5000, start, end, 100_000, 1.0, "Streaming",
    )

    assert result is True
    assert api.listening_session_calls == [
        ("B001", "lic-123", 1000, 5000, start, end, 100_000, 1.0, "Streaming"),
    ]


def test_push_listening_session_swallows_failure_and_reports_it():
    api = FakePushAPI(exc=RuntimeError("network exploded"))
    start = datetime(2026, 9, 21, 10, 0)
    end = datetime(2026, 9, 21, 10, 5)

    result = progress.push_listening_session(
        api, "B001", "lic-123", 1000, 5000, start, end, 100_000, 1.0, "Streaming",
    )

    assert result is False


# -- default path resolution (L6) -------------------------------------------


def test_default_path_is_resolved_at_construction_not_at_import(tmp_path, monkeypatch):
    """ProgressStore(path=config.PROGRESS_CACHE_FILE) as a default argument
    would bind whatever config.PROGRESS_CACHE_FILE was at import time --
    monkeypatching config afterwards wouldn't be seen without also patching
    the ProgressStore class itself. Resolving the default inside __init__
    instead means this monkeypatch on `config` alone is enough."""
    patched_path = tmp_path / "progress_cache.json"
    monkeypatch.setattr(progress.config, "PROGRESS_CACHE_FILE", patched_path)

    progress.ProgressStore().set_position_ms("B001", 5_000)

    assert patched_path.exists()
    assert progress.ProgressStore().get_position_ms("B001") == 5_000
