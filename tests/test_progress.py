import pytest

from audible_tui.services import progress


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


# -- fetch_remote_positions ---------------------------------------------


def test_fetch_remote_positions_empty_asins_short_circuits():
    api = FakeAPI(response={"should": "not be read"})
    result = progress.fetch_remote_positions(api, [])
    assert result == {}
    assert api.client.calls == []  # no network call should have been made


def test_fetch_remote_positions_flat_dict_of_ints():
    api = FakeAPI(response={"B001": 1000, "B002": 2000})
    result = progress.fetch_remote_positions(api, ["B001", "B002"])
    assert result == {"B001": 1000, "B002": 2000}


def test_fetch_remote_positions_wrapped_under_asin_positions_key():
    api = FakeAPI(response={"asin_positions": {"B001": {"position_ms": 4242}}})
    result = progress.fetch_remote_positions(api, ["B001"])
    assert result == {"B001": 4242}


def test_fetch_remote_positions_wrapped_under_positions_key():
    api = FakeAPI(response={"positions": {"B001": {"positionMs": 555}}})
    result = progress.fetch_remote_positions(api, ["B001"])
    assert result == {"B001": 555}


def test_fetch_remote_positions_list_of_records():
    api = FakeAPI(
        response=[
            {"asin": "B001", "position_ms": 111},
            {"asin": "B002", "lastPositionMs": 222},
        ]
    )
    result = progress.fetch_remote_positions(api, ["B001", "B002"])
    assert result == {"B001": 111, "B002": 222}


def test_fetch_remote_positions_list_skips_records_missing_asin():
    api = FakeAPI(response=[{"position_ms": 111}])
    result = progress.fetch_remote_positions(api, ["B001"])
    assert result == {}


def test_fetch_remote_positions_accepts_float_positions():
    api = FakeAPI(response={"B001": 1234.0})
    result = progress.fetch_remote_positions(api, ["B001"])
    assert result == {"B001": 1234}


def test_fetch_remote_positions_returns_empty_on_unrecognized_shape():
    api = FakeAPI(response="totally unexpected string response")
    result = progress.fetch_remote_positions(api, ["B001"])
    assert result == {}


def test_fetch_remote_positions_returns_empty_when_client_raises():
    api = FakeAPI(exc=RuntimeError("network exploded"))
    result = progress.fetch_remote_positions(api, ["B001"])
    assert result == {}


def test_fetch_remote_positions_passes_comma_joined_asins():
    api = FakeAPI(response={})
    progress.fetch_remote_positions(api, ["B001", "B002"])
    (_path, kwargs), = api.client.calls
    assert kwargs["asins"] == "B001,B002"
