"""SQLite migration state. All write methods are idempotent upserts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .flickr import FlickrAlbum, FlickrPhoto

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS flickr_account (
  nsid TEXT PRIMARY KEY, username TEXT, realname TEXT, discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS flickr_photo (
  flickr_id TEXT PRIMARY KEY, account_nsid TEXT NOT NULL REFERENCES flickr_account(nsid),
  filename TEXT, title TEXT, description TEXT, tags_json TEXT NOT NULL DEFAULT '[]',
  date_taken TEXT, date_uploaded TEXT, latitude REAL, longitude REAL, accuracy INTEGER,
  original_url TEXT, original_format TEXT, media_type TEXT, raw_json TEXT NOT NULL,
  checksum_sha256 TEXT, local_path TEXT, download_state TEXT NOT NULL DEFAULT 'discovered'
    CHECK(download_state IN ('discovered','downloading','downloaded','verified','failed')),
  verification_state TEXT NOT NULL DEFAULT 'pending' CHECK(verification_state IN ('pending','verified','failed')),
  google_media_id TEXT, upload_state TEXT NOT NULL DEFAULT 'not_started'
    CHECK(upload_state IN ('not_started','uploading','uploaded','failed')),
  last_error TEXT, discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS flickr_album (
  flickr_id TEXT PRIMARY KEY, account_nsid TEXT NOT NULL REFERENCES flickr_account(nsid),
  title TEXT NOT NULL, description TEXT, photo_count INTEGER, raw_json TEXT NOT NULL,
  google_album_id TEXT, album_state TEXT NOT NULL DEFAULT 'discovered',
  discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS flickr_album_photo (
  album_flickr_id TEXT NOT NULL REFERENCES flickr_album(flickr_id) ON DELETE CASCADE,
  photo_flickr_id TEXT NOT NULL REFERENCES flickr_photo(flickr_id) ON DELETE CASCADE,
  position INTEGER, PRIMARY KEY(album_flickr_id, photo_flickr_id)
);
CREATE INDEX IF NOT EXISTS idx_photo_download_state ON flickr_photo(download_state);
CREATE INDEX IF NOT EXISTS idx_photo_upload_state ON flickr_photo(upload_state);
"""


class MigrationDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))

    def upsert_account(self, nsid: str, username: str | None, realname: str | None) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT INTO flickr_account(nsid, username, realname) VALUES (?, ?, ?)
            ON CONFLICT(nsid) DO UPDATE SET username=excluded.username, realname=excluded.realname""", (nsid, username, realname))

    def upsert_photo(self, account_nsid: str, photo: FlickrPhoto) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT INTO flickr_photo(
              flickr_id,account_nsid,filename,title,description,tags_json,date_taken,date_uploaded,
              latitude,longitude,accuracy,original_url,original_format,media_type,raw_json,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(flickr_id) DO UPDATE SET filename=excluded.filename,title=excluded.title,
              description=excluded.description,tags_json=excluded.tags_json,date_taken=excluded.date_taken,
              date_uploaded=excluded.date_uploaded,latitude=excluded.latitude,longitude=excluded.longitude,
              accuracy=excluded.accuracy,original_url=excluded.original_url,original_format=excluded.original_format,
              media_type=excluded.media_type,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP""", (
                photo.id, account_nsid, photo.filename, photo.title, photo.description, json.dumps(photo.tags),
                photo.date_taken, photo.date_uploaded, photo.latitude, photo.longitude, photo.accuracy,
                photo.original_url, photo.original_format, photo.media_type, json.dumps(photo.raw),
            ))

    def upsert_album(self, account_nsid: str, album: FlickrAlbum) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT INTO flickr_album(flickr_id,account_nsid,title,description,photo_count,raw_json,updated_at)
              VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(flickr_id) DO UPDATE SET title=excluded.title,
              description=excluded.description,photo_count=excluded.photo_count,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP""",
                (album.id, account_nsid, album.title, album.description, album.photo_count, json.dumps(album.raw)))

    def replace_album_membership(self, album_id: str, photo_ids: list[str]) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM flickr_album_photo WHERE album_flickr_id = ?", (album_id,))
            conn.executemany("INSERT INTO flickr_album_photo(album_flickr_id,photo_flickr_id,position) VALUES(?,?,?)",
                             [(album_id, photo_id, position) for position, photo_id in enumerate(photo_ids)])

    def summary(self) -> dict[str, int]:
        with self.connection() as conn:
            return {
                "photos": conn.execute("SELECT count(*) FROM flickr_photo").fetchone()[0],
                "image_items": conn.execute("SELECT count(*) FROM flickr_photo WHERE media_type='photo'").fetchone()[0],
                "video_items": conn.execute("SELECT count(*) FROM flickr_photo WHERE media_type='video'").fetchone()[0],
                "albums": conn.execute("SELECT count(*) FROM flickr_album").fetchone()[0],
                "album_memberships": conn.execute("SELECT count(*) FROM flickr_album_photo").fetchone()[0],
                "verified_downloads": conn.execute("SELECT count(*) FROM flickr_photo WHERE verification_state='verified'").fetchone()[0],
                "google_uploaded": conn.execute("SELECT count(*) FROM flickr_photo WHERE upload_state='uploaded'").fetchone()[0],
                "google_reconciliation_required": conn.execute("SELECT count(*) FROM flickr_photo WHERE upload_state='uploading'").fetchone()[0],
            }

    def duplicate_report(self) -> dict[str, list[dict[str, object]]]:
        """Return duplicate relationships without changing any migration state.

        ``album_membership_duplicates`` identifies one Flickr media item placed in
        multiple albums. ``content_duplicates`` is populated after the download
        phase verifies SHA-256 checksums for distinct Flickr IDs.
        """
        with self.connection() as conn:
            memberships = conn.execute(
                """SELECT p.flickr_id, p.media_type, p.filename, COUNT(*) AS album_count,
                   GROUP_CONCAT(a.title, ' | ') AS album_titles
                   FROM flickr_album_photo ap
                   JOIN flickr_photo p ON p.flickr_id = ap.photo_flickr_id
                   JOIN flickr_album a ON a.flickr_id = ap.album_flickr_id
                   GROUP BY p.flickr_id HAVING COUNT(*) > 1
                   ORDER BY album_count DESC, p.flickr_id"""
            ).fetchall()
            content = conn.execute(
                """SELECT checksum_sha256, media_type, COUNT(*) AS item_count,
                   GROUP_CONCAT(flickr_id, ',') AS flickr_ids
                   FROM flickr_photo
                   WHERE verification_state='verified' AND checksum_sha256 IS NOT NULL
                   GROUP BY checksum_sha256, media_type HAVING COUNT(*) > 1
                   ORDER BY item_count DESC, checksum_sha256"""
            ).fetchall()
        return {
            "album_membership_duplicates": [dict(row) for row in memberships],
            "content_duplicates": [dict(row) for row in content],
        }
