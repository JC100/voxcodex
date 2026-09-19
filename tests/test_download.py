import json
import stat

import pytest

from voxcodex import config
from voxcodex.models import Book
from voxcodex.services import download
from voxcodex.services.api import License


@pytest.fixture(autouse=True)
def _downloads_dir_in_tmp(tmp_path, monkeypatch):
    """Every test in this file gets an isolated, real DOWNLOADS_DIR."""
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")


class FakeResponse:
    def __init__(self, chunks, headers=None, raise_exc=None):
        self._chunks = chunks
        self.headers = headers or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def iter_bytes(self, chunk_size=None):
        yield from self._chunks


class FakeStreamContext:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeStreamContext(self._response)


class FakeClient:
    def __init__(self, session):
        self.session = session


class FakeAPI:
    def __init__(self, license_or_exc, response):
        self.client = FakeClient(FakeSession(response))
        self._license_or_exc = license_or_exc
        self.license_calls = []

    def get_license(self, asin, quality="high"):
        self.license_calls.append((asin, quality))
        if isinstance(self._license_or_exc, Exception):
            raise self._license_or_exc
        return self._license_or_exc


def _book(asin="B001"):
    return Book(asin=asin, title="Test Book")


# -- path helpers --------------------------------------------------------


def test_audio_and_voucher_paths_are_under_downloads_dir():
    assert download.audio_path_for("B001") == config.DOWNLOADS_DIR / "B001.aaxc"
    assert download.voucher_path_for("B001") == config.DOWNLOADS_DIR / "B001.voucher.json"


def test_is_downloaded_false_when_nothing_exists():
    assert download.is_downloaded("B001") is False


def test_is_downloaded_false_when_only_audio_exists():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    download.audio_path_for("B001").write_bytes(b"data")
    assert download.is_downloaded("B001") is False


def test_is_downloaded_true_when_both_files_exist():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    download.audio_path_for("B001").write_bytes(b"data")
    download.voucher_path_for("B001").write_text("{}")
    assert download.is_downloaded("B001") is True


def test_downloaded_size_returns_the_audio_file_size():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    download.audio_path_for("B001").write_bytes(b"x" * 12_345)
    download.voucher_path_for("B001").write_text("{}")

    assert download.downloaded_size("B001") == 12_345


def test_downloaded_size_none_when_not_downloaded():
    assert download.downloaded_size("B001") is None


def test_delete_download_removes_both_files():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    download.audio_path_for("B001").write_bytes(b"data")
    download.voucher_path_for("B001").write_text("{}")

    download.delete_download("B001")

    assert not download.audio_path_for("B001").exists()
    assert not download.voucher_path_for("B001").exists()


def test_delete_download_is_a_no_op_when_nothing_exists():
    download.delete_download("B001")  # must not raise


def test_load_voucher_returns_none_when_missing():
    assert download.load_voucher("B001") is None


def test_load_voucher_parses_saved_json():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    download.voucher_path_for("B001").write_text(json.dumps({"key": "k", "iv": "i"}))
    assert download.load_voucher("B001") == {"key": "k", "iv": "i"}


# -- download_book --------------------------------------------------------


def test_download_book_writes_audio_and_voucher_and_reports_progress():
    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="thekey", iv="theiv", acr="CR!ABC",
    )
    response = FakeResponse([b"hello ", b"world"], headers={"content-length": "11"})
    api = FakeAPI(license_, response)
    progress_calls = []

    result_path = download.download_book(
        _book("B001"), api, on_progress=lambda done, total: progress_calls.append((done, total))
    )

    assert result_path == download.audio_path_for("B001")
    assert result_path.read_bytes() == b"hello world"
    assert not result_path.with_suffix(".part").exists()

    voucher = json.loads(download.voucher_path_for("B001").read_text())
    assert voucher == {
        "asin": "B001",
        "key": "thekey",
        "iv": "theiv",
        "codec": "AAXC",
        "acr": "CR!ABC",
    }

    assert progress_calls == [(6, 11), (11, 11)]


def test_download_book_writes_the_voucher_private():
    """M2: the voucher holds the AES key + iv, so it should never be left
    at the process's default umask (typically 0644)."""
    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="thekey", iv="theiv", acr="CR!ABC",
    )
    response = FakeResponse([b"hello world"], headers={"content-length": "11"})
    api = FakeAPI(license_, response)

    download.download_book(_book("B001"), api)

    mode = stat.S_IMODE(download.voucher_path_for("B001").stat().st_mode)
    assert mode == 0o600


def test_download_book_propagates_license_denied():
    from voxcodex.services.api import LicenseDenied

    api = FakeAPI(LicenseDenied("no rights"), response=None)

    with pytest.raises(LicenseDenied):
        download.download_book(_book("B001"), api)

    assert not download.audio_path_for("B001").exists()


def test_download_book_leaves_no_files_when_cdn_request_fails():
    class FakeHTTPError(Exception):
        pass

    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="thekey", iv="theiv",
    )
    response = FakeResponse([], raise_exc=FakeHTTPError("403 Forbidden"))
    api = FakeAPI(license_, response)

    with pytest.raises(FakeHTTPError):
        download.download_book(_book("B001"), api)

    assert not download.audio_path_for("B001").exists()
    assert not download.audio_path_for("B001").with_suffix(".part").exists()
    assert not download.voucher_path_for("B001").exists()


def test_download_book_rejects_a_truncated_stream_and_cleans_up():
    # Server promises 100 bytes, connection delivers 4 -- the old code renamed
    # the short file into place and it looked downloaded until playback failed.
    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="k", iv="i",
    )
    response = FakeResponse([b"abcd"], headers={"content-length": "100"})
    api = FakeAPI(license_, response)

    with pytest.raises(OSError, match="truncated"):
        download.download_book(_book("B001"), api)

    assert not download.audio_path_for("B001").exists()
    assert not download.audio_path_for("B001").with_suffix(".part").exists()
    assert not download.voucher_path_for("B001").exists()


def test_download_book_stops_and_cleans_up_when_cancel_check_fires():
    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="k", iv="i",
    )
    response = FakeResponse([b"one", b"two", b"three"], headers={"content-length": "11"})
    api = FakeAPI(license_, response)

    seen = []

    def cancel_after_first_chunk():
        seen.append(1)
        return len(seen) > 1  # let the first chunk through, then bail

    with pytest.raises(download.DownloadCancelled):
        download.download_book(_book("B001"), api, cancel_check=cancel_after_first_chunk)

    assert not download.audio_path_for("B001").with_suffix(".part").exists()
    assert not download.audio_path_for("B001").exists()


def test_download_book_passes_content_url_and_uses_get_method():
    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="k", iv="i",
    )
    response = FakeResponse([b"x"], headers={"content-length": "1"})
    api = FakeAPI(license_, response)

    download.download_book(_book("B001"), api)

    (method, url, _kwargs), = api.client.session.calls
    assert method == "GET"
    assert url == "https://cdn.example/x.aaxc"


def test_download_book_writes_the_voucher_before_renaming_the_audio_file():
    """M3: is_downloaded() requires both files, so writing the voucher
    first means a crash between the two writes leaves a `.part` + an
    orphaned voucher -- never a "downloaded" audio file with no voucher to
    decrypt it."""
    license_ = License(
        asin="B001", content_url="https://cdn.example/x.aaxc", codec="AAXC",
        key="k", iv="i",
    )
    response = FakeResponse([b"hello"], headers={"content-length": "5"})
    api = FakeAPI(license_, response)

    seen_audio_exists_when_voucher_written = None
    original_write_voucher = download._write_voucher

    def _spy(asin, license_arg):
        nonlocal seen_audio_exists_when_voucher_written
        seen_audio_exists_when_voucher_written = download.audio_path_for(asin).exists()
        original_write_voucher(asin, license_arg)

    download._write_voucher = _spy
    try:
        download.download_book(_book("B001"), api)
    finally:
        download._write_voucher = original_write_voucher

    assert seen_audio_exists_when_voucher_written is False


# -- sweep_stale_downloads (M3) -------------------------------------------


def test_sweep_stale_downloads_removes_orphaned_part_files():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    stale = download.audio_path_for("B001").with_suffix(".part")
    stale.write_bytes(b"partial")

    download.sweep_stale_downloads()

    assert not stale.exists()


def test_sweep_stale_downloads_leaves_completed_downloads_alone():
    config.DOWNLOADS_DIR.mkdir(parents=True)
    download.audio_path_for("B001").write_bytes(b"data")
    download.voucher_path_for("B001").write_text("{}")

    download.sweep_stale_downloads()

    assert download.audio_path_for("B001").exists()
    assert download.voucher_path_for("B001").exists()


def test_sweep_stale_downloads_is_a_no_op_when_the_dir_does_not_exist_yet():
    download.sweep_stale_downloads()  # must not raise
