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


def test_positions_from_annotations_includes_only_existing_positions():
    records = [_existing("B001", 1000), _does_not_exist("B002")]
    assert progress.positions_from_annotations(records) == {"B001": 1000}


def test_positions_from_annotations_empty_when_nothing_exists():
    assert progress.positions_from_annotations([_does_not_exist("B001")]) == {}


def test_fetch_remote_positions_end_to_end():
    records = [_existing("B001", 4242), _does_not_exist("B002")]
    api = FakeAPI(response=_annotations_response(records))
    assert progress.fetch_remote_positions(api, ["B001", "B002"]) == {"B001": 4242}


# -- most_recent_external_play -------------------------------------------


def test_most_recent_external_play_picks_the_newest_timestamp():
    records = [
        _existing("B001", 100, last_updated="2019-01-24 09:21:16.892"),
        _existing("B002", 200, last_updated="2026-08-27 08:56:11.849"),
        _existing("B003", 300, last_updated="2026-08-10 22:17:44.988"),
    ]
    result = progress.most_recent_external_play(records)
    assert result is not None
    asin, updated_at = result
    assert asin == "B002"
    assert updated_at.year == 2026
    assert updated_at.month == 8
    assert updated_at.day == 27


def test_most_recent_external_play_ignores_titles_never_played():
    records = [_does_not_exist("B001")]
    assert progress.most_recent_external_play(records) is None


def test_most_recent_external_play_none_when_no_records():
    assert progress.most_recent_external_play([]) is None


def test_most_recent_external_play_skips_unparseable_timestamps():
    records = [_existing("B001", 100, last_updated="not-a-real-timestamp")]
    assert progress.most_recent_external_play(records) is None


# -- push_position -------------------------------------------------------


class FakePushAPI:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []
        self.finished_calls = []

    def push_last_position(self, asin, acr, position_ms):
        self.calls.append((asin, acr, position_ms))
        if self.exc is not None:
            raise self.exc

    def set_finished(self, asin, finished):
        self.finished_calls.append((asin, finished))
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
