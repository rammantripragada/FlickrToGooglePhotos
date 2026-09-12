from __future__ import annotations

from flickr_to_google_photos.database import MigrationDatabase
from flickr_to_google_photos.flickr import FlickrAlbum, FlickrPhoto


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
