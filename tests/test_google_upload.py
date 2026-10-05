import requests
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from flickr_to_google_photos.google_upload import ArchiveGoogleClient, MediaCreateRejected, UploadPaused


class Response:
    def __init__(self, body=None, *, status=200, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}
        self.body = body
    def json(self):
        return self.body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result
    def get(self, url, **kwargs):
        return self.post(url, **kwargs)


SESSION_URL = "https://photoslibrary.googleapis.com/v1/uploads?upload_id=test"


def test_resumable_upload_recovers_server_offset_after_network_failure(tmp_path):
    path = tmp_path / "video.mov"
    path.write_bytes(b"abcdefgh")
    session = Session([
        Response(headers={"X-Goog-Upload-URL": SESSION_URL, "X-Goog-Upload-Chunk-Granularity": "1"}),
        requests.ConnectionError("lost"),
        Response(headers={"X-Goog-Upload-Status": "active", "X-Goog-Upload-Size-Received": "3"}),
        Response(text="upload-token")])
    states, progress = [], []
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test", sleep=lambda _: None)
    state = {}
    assert client.upload_bytes(path, state, lambda s: states.append(dict(s)), lambda a,b: progress.append((a,b))) == "upload-token"
    assert session.calls[-1][1]["data"] == b"defgh"
    assert session.calls[-1][1]["headers"]["X-Goog-Upload-Offset"] == "3"
    assert states[0]["session_url"] == SESSION_URL
    assert states[-1]["token"] == "upload-token"
    assert progress[-1] == (8, 8)


def test_saved_session_is_queried_before_upload(tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"abcdef")
    session = Session([Response(headers={"X-Goog-Upload-Status": "active", "X-Goog-Upload-Size-Received": "2"}), Response(text="token")])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    client.upload_bytes(path, {"session_url": SESSION_URL, "granularity": 1}, lambda _: None)
    assert session.calls[0][1]["headers"]["X-Goog-Upload-Command"] == "query"
    assert session.calls[1][1]["data"] == b"cdef"


def test_album_memberships_respect_50_item_batch_limit():
    session = Session([Response(), Response()])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    client.add_media("album", [str(i) for i in range(51)])
    assert [len(call[1]["json"]["mediaItemIds"]) for call in session.calls] == [50, 1]


def test_creation_preserves_filename_and_user_description():
    session = Session([Response({"newMediaItemResults": [{"mediaItem": {"id": "media"}}]})])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    assert client.create_media("token", "original.mov", "Holiday") == "media"
    item = session.calls[0][1]["json"]["newMediaItems"][0]
    assert item["simpleMediaItem"]["fileName"] == "original.mov"
    assert item["description"] == "Holiday"


def test_creation_detects_per_item_rejection():
    session = Session([Response({"newMediaItemResults": [{"status": {"code": 3, "message": "unsupported"}}]})])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    with pytest.raises(MediaCreateRejected):
        client.create_media("token", "bad.mov")


def test_missing_media_id_without_explicit_error_is_ambiguous():
    session = Session([Response({"newMediaItemResults": [{"status": {"code": 0}}]})])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    with pytest.raises(RuntimeError, match="reconciliation") as error:
        client.create_media("token", "image.jpg")
    assert not isinstance(error.value, MediaCreateRejected)


def test_album_name_lookup_handles_pagination():
    session = Session([Response({"albums": [{"id": "other", "title": "Other"}], "nextPageToken": "next"}),
                       Response({"albums": [{"id": "found", "title": "Trip", "isWriteable": True}]})])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    assert client.find_album("Trip")["id"] == "found"
    assert session.calls[1][1]["params"]["pageToken"] == "next"


def test_album_name_lookup_does_not_choose_ambiguous_match():
    session = Session([Response({"albums": [{"id": "a", "title": "Trip"}, {"id": "b", "title": "Trip"}]})])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    with pytest.raises(RuntimeError, match="Several accessible"):
        client.find_album("Trip")


class FakeClock:
    def __init__(self):
        self.now = 0
        self.waits = []
    def clock(self):
        return self.now
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def test_429_retry_after_sets_shared_cooldown():
    timer = FakeClock()
    session = Session([Response(status=429, headers={"Retry-After": "45"}), Response()])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test", sleep=timer.sleep, clock=timer.clock)
    client.add_media("album", ["media"])
    assert client._cooldown_until == 45
    assert timer.now == 45 and len(session.calls) == 2


def test_requests_from_any_worker_wait_for_shared_cooldown():
    timer = FakeClock()
    session = Session([Response()])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test", sleep=timer.sleep, clock=timer.clock)
    client._cooldown_until = 3
    client.add_media("album", ["media"])
    assert timer.now == 3 and len(session.calls) == 1


def test_pause_interrupts_shared_cooldown_without_network_request():
    session = Session([])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    client.should_stop = lambda: True
    with pytest.raises(UploadPaused):
        client.add_media("album", ["media"])
    assert not session.calls


def test_each_worker_has_its_own_http_session():
    client = ArchiveGoogleClient(token_provider=lambda: "test")
    barrier = Barrier(2)
    def get_session():
        session = client.session
        barrier.wait(timeout=3)
        return session
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: get_session(), range(2)))
    assert first is not second
    assert len(client._sessions) == 2
    client.close()
    assert not client._sessions


def test_media_batch_returns_individual_successes_and_rejections():
    session = Session([Response({"newMediaItemResults": [
        {"mediaItem": {"id": "image-id"}}, {"status": {"code": 3, "message": "unsupported"}}]}, status=207)])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    result = client.create_media_batch([{"token": "photo", "filename": "image.jpg"}, {"token": "video", "filename": "video.mov"}])
    assert result[0] == "image-id" and isinstance(result[1], MediaCreateRejected)
    assert len(session.calls) == 1
    assert len(session.calls[0][1]["json"]["newMediaItems"]) == 2


@pytest.mark.parametrize("size", [0, 51])
def test_media_batch_size_limits_are_checked_without_network(size):
    session = Session([])
    client = ArchiveGoogleClient(session=session, token_provider=lambda: "test")
    with pytest.raises(ValueError):
        client.create_media_batch([{"token": "token", "filename": "image.jpg"}] * size)
    assert not session.calls
