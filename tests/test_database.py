from __future__ import annotations

from io import BytesIO

import requests

from flickr_to_google_photos.database import MigrationDatabase
from flickr_to_google_photos.flickr import FlickrAlbum, FlickrPhoto
from flickr_to_google_photos.migrate import MigrationService


def photo(photo_id: str = "photo-1") -> FlickrPhoto:
    return FlickrPhoto(photo_id, "original.jpg", "Title", "Description", ["tag"], "2020-01-01", "1", None, None, None, "https://example.test/a.jpg", "jpg", "photo", {"id": photo_id})


def test_photo_upsert_preserves_resume_state(tmp_path):
    db = MigrationDatabase(tmp_path / "migration.sqlite3")
    db.initialize()
    db.upsert_account("account", "user", None)
    db.upsert_photo("account", photo())
    with db.connection() as conn:
        conn.execute("UPDATE flickr_photo SET download_state='verified', verification_state='verified', checksum_sha256='abc' WHERE flickr_id='photo-1'")
    db.upsert_photo("account", photo())
    with db.connection() as conn:
        row = conn.execute("SELECT download_state, verification_state, checksum_sha256 FROM flickr_photo WHERE flickr_id='photo-1'").fetchone()
    assert tuple(row) == ("verified", "verified", "abc")


def test_replacing_album_membership_is_idempotent(tmp_path):
    db = MigrationDatabase(tmp_path / "migration.sqlite3")
    db.initialize()
    db.upsert_account("account", "user", None)
    db.upsert_photo("account", photo("one"))
    db.upsert_photo("account", photo("two"))
    db.upsert_album("account", FlickrAlbum("album", "Album", None, 2, {}))
    db.replace_album_membership("album", ["one", "two"])
    db.replace_album_membership("album", ["two"])
    assert db.summary()["album_memberships"] == 1


def test_duplicate_report_finds_shared_album_membership_and_verified_content(tmp_path):
    db = MigrationDatabase(tmp_path / "migration.sqlite3")
    db.initialize()
    db.upsert_account("account", "user", None)
    db.upsert_photo("account", photo("one"))
    db.upsert_photo("account", photo("two"))
    db.upsert_album("account", FlickrAlbum("album-a", "A", None, 1, {}))
    db.upsert_album("account", FlickrAlbum("album-b", "B", None, 1, {}))
    db.replace_album_membership("album-a", ["one"])
    db.replace_album_membership("album-b", ["one"])
    with db.connection() as conn:
        conn.execute("UPDATE flickr_photo SET verification_state='verified', checksum_sha256='same' WHERE flickr_id IN ('one', 'two')")
    report = db.duplicate_report()
    assert report["album_membership_duplicates"][0]["flickr_id"] == "one"
    assert report["content_duplicates"][0]["flickr_ids"] == "one,two"


def test_album_selection_replaces_prior_selection_and_rejects_unknown_ids(tmp_path):
    db = MigrationDatabase(tmp_path / "migration.sqlite3")
    db.initialize()
    db.upsert_account("account", "user", None)
    db.upsert_album("account", FlickrAlbum("a", "A", None, 0, {}))
    db.upsert_album("account", FlickrAlbum("b", "B", None, 0, {}))
    db.set_selected_albums({"a"})
    assert [album["flickr_id"] for album in db.albums() if album["selected_for_migration"]] == ["a"]
    db.set_selected_albums({"b"})
    assert [album["flickr_id"] for album in db.albums() if album["selected_for_migration"]] == ["b"]


def test_download_retries_a_flickr_rate_limit(tmp_path):
    class Database:
        def set_local_file(self, *_args):
            pass

    class Response:
        def __init__(self, status: int, body: bytes = b"file"):
            self.status_code, self.headers, self.raw = status, {}, BytesIO(body)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                from requests import HTTPError
                raise HTTPError(f"HTTP {self.status_code}")

    responses = iter([Response(429), Response(200, b"original")])
    waits: list[float] = []
    service = MigrationService(Database(), tmp_path, request_get=lambda *_args, **_kwargs: next(responses), sleep=waits.append)
    result = service._download({"flickr_id": "42", "filename": "source.jpg", "local_path": None, "original_url": "https://example.test/42"})
    assert result.read_bytes() == b"original"
    assert waits == [60.0]


def test_download_retries_a_temporary_network_failure(tmp_path):
    class Database:
        def set_local_file(self, *_args):
            pass

    class Response:
        status_code, headers, raw = 200, {}, BytesIO(b"original")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            pass

    responses = iter([requests.ConnectionError("DNS lookup failed"), Response()])
    waits: list[float] = []

    def request_get(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    service = MigrationService(Database(), tmp_path, request_get=request_get, sleep=waits.append)
    result = service._download({"flickr_id": "42", "filename": "source.jpg", "local_path": None, "original_url": "https://example.test/42"})
    assert result.read_bytes() == b"original"
    assert len(waits) == 1
    assert 15 <= waits[0] < 16


def test_download_paces_consecutive_flickr_file_requests(tmp_path):
    class Database:
        def set_local_file(self, *_args):
            pass

    class Response:
        status_code, headers = 200, {}

        def __init__(self, body: bytes):
            self.raw = BytesIO(body)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            pass

    responses = iter([Response(b"one"), Response(b"two")])
    clock = [0.0]
    waits: list[float] = []

    def sleep(delay: float):
        waits.append(delay)
        clock[0] += delay

    service = MigrationService(
        Database(), tmp_path, request_get=lambda *_args, **_kwargs: next(responses),
        sleep=sleep, monotonic=lambda: clock[0], download_interval_seconds=5,
    )
    first = {"flickr_id": "one", "filename": "one.jpg", "local_path": None, "original_url": "https://example.test/one"}
    second = {"flickr_id": "two", "filename": "two.jpg", "local_path": None, "original_url": "https://example.test/two"}
    service._download(first)
    service._download(second)
    assert waits == [5]


def test_download_does_not_add_delay_after_a_long_file(tmp_path):
    class Database:
        def set_local_file(self, *_args):
            pass

    clock = [0.0]
    waits: list[float] = []

    class Response:
        status_code, headers = 200, {}

        def __init__(self, body: bytes, duration: float):
            self.raw, self.duration = BytesIO(body), duration

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            clock[0] += self.duration
            return False

        def raise_for_status(self):
            pass

    responses = iter([Response(b"video", 20), Response(b"image", 0)])
    service = MigrationService(
        Database(), tmp_path, request_get=lambda *_args, **_kwargs: next(responses),
        sleep=waits.append, monotonic=lambda: clock[0], download_interval_seconds=5,
    )
    first = {"flickr_id": "video", "filename": "clip.mp4", "local_path": None, "original_url": "https://example.test/video"}
    second = {"flickr_id": "image", "filename": "image.jpg", "local_path": None, "original_url": "https://example.test/image"}
    service._download(first)
    service._download(second)
    assert waits == []
