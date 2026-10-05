"""SQLite migration state. All write methods are idempotent upserts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .flickr import FlickrAlbum, FlickrPhoto

SCHEMA_VERSION = 3
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
  selected_for_migration INTEGER NOT NULL DEFAULT 0 CHECK(selected_for_migration IN (0,1)),
  discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS flickr_album_photo (
  album_flickr_id TEXT NOT NULL REFERENCES flickr_album(flickr_id) ON DELETE CASCADE,
  photo_flickr_id TEXT NOT NULL REFERENCES flickr_photo(flickr_id) ON DELETE CASCADE,
  position INTEGER, PRIMARY KEY(album_flickr_id, photo_flickr_id)
);
CREATE INDEX IF NOT EXISTS idx_photo_download_state ON flickr_photo(download_state);
CREATE INDEX IF NOT EXISTS idx_photo_upload_state ON flickr_photo(upload_state);
CREATE TABLE IF NOT EXISTS archive_media (
  flickr_id TEXT PRIMARY KEY REFERENCES flickr_photo(flickr_id) ON DELETE CASCADE,
  archive_path TEXT NOT NULL, member_name TEXT NOT NULL, byte_size INTEGER, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS google_upload_journal (
  flickr_id TEXT PRIMARY KEY REFERENCES flickr_photo(flickr_id),
  state_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_photo_checksum ON flickr_photo(checksum_sha256);
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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(flickr_album)")}
            if "selected_for_migration" not in columns:
                conn.execute("ALTER TABLE flickr_album ADD COLUMN selected_for_migration INTEGER NOT NULL DEFAULT 0")
            for name in ("google_album_url", "last_error"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE flickr_album ADD COLUMN {name} TEXT")
            membership_columns = {row[1] for row in conn.execute("PRAGMA table_info(flickr_album_photo)")}
            for name, definition in (("google_state", "TEXT NOT NULL DEFAULT 'pending'"), ("last_error", "TEXT")):
                if name not in membership_columns:
                    conn.execute(f"ALTER TABLE flickr_album_photo ADD COLUMN {name} {definition}")
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
            previous = {row[0] for row in conn.execute("SELECT photo_flickr_id FROM flickr_album_photo WHERE album_flickr_id=?", (album_id,))}
            conn.executemany("DELETE FROM flickr_album_photo WHERE album_flickr_id=? AND photo_flickr_id=?", [(album_id, photo_id) for photo_id in previous - set(photo_ids)])
            conn.executemany("INSERT INTO flickr_album_photo(album_flickr_id,photo_flickr_id,position) VALUES(?,?,?) ON CONFLICT(album_flickr_id,photo_flickr_id) DO UPDATE SET position=excluded.position",
                             [(album_id, photo_id, position) for position, photo_id in enumerate(photo_ids)])

    def albums(self) -> list[dict[str, object]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT flickr_id, title, description, photo_count, selected_for_migration FROM flickr_album ORDER BY title COLLATE NOCASE, flickr_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_selected_albums(self, flickr_album_ids: set[str]) -> None:
        """Replace selection atomically; unknown IDs are rejected to avoid typos."""
        with self.connection() as conn:
            known = {row[0] for row in conn.execute("SELECT flickr_id FROM flickr_album")}
            unknown = flickr_album_ids - known
            if unknown:
                raise ValueError(f"Unknown Flickr album ID(s): {', '.join(sorted(unknown))}")
            conn.execute("UPDATE flickr_album SET selected_for_migration=0")
            conn.executemany(
                "UPDATE flickr_album SET selected_for_migration=1, updated_at=CURRENT_TIMESTAMP WHERE flickr_id=?",
                [(album_id,) for album_id in flickr_album_ids],
            )

    def summary(self) -> dict[str, int]:
        with self.connection() as conn:
            return {
                "photos": conn.execute("SELECT count(*) FROM flickr_photo").fetchone()[0],
                "image_items": conn.execute("SELECT count(*) FROM flickr_photo WHERE media_type='photo'").fetchone()[0],
                "video_items": conn.execute("SELECT count(*) FROM flickr_photo WHERE media_type='video'").fetchone()[0],
                "albums": conn.execute("SELECT count(*) FROM flickr_album").fetchone()[0],
                "selected_albums": conn.execute("SELECT count(*) FROM flickr_album WHERE selected_for_migration=1").fetchone()[0],
                "album_memberships": conn.execute("SELECT count(*) FROM flickr_album_photo").fetchone()[0],
                "verified_downloads": conn.execute("SELECT count(*) FROM flickr_photo WHERE verification_state='verified'").fetchone()[0],
                "google_uploaded": conn.execute("SELECT count(*) FROM flickr_photo WHERE upload_state='uploaded'").fetchone()[0],
                "google_reconciliation_required": conn.execute("SELECT count(*) FROM flickr_photo WHERE upload_state='uploading'").fetchone()[0],
                "archive_media_indexed": conn.execute("SELECT count(*) FROM archive_media").fetchone()[0],
            }

    def upsert_archive_media(self, records: list[tuple[str, str, str, int]]) -> None:
        with self.connection() as conn:
            conn.executemany("""INSERT INTO archive_media(flickr_id,archive_path,member_name,byte_size)
              SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM flickr_photo WHERE flickr_id=?)
              ON CONFLICT(flickr_id) DO UPDATE SET archive_path=excluded.archive_path,
              member_name=excluded.member_name,byte_size=excluded.byte_size,indexed_at=CURRENT_TIMESTAMP""",
              [(*record, record[0]) for record in records])
            # The ZIP member extension is authoritative for media type; Flickr
            # metadata sometimes gives a JPG thumbnail URL for a video.
            videos = [record for record in records if Path(record[2]).suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".mts", ".mpg", ".mpeg"}]
            conn.executemany("UPDATE flickr_photo SET media_type='video',original_format=? WHERE flickr_id=?", [(Path(record[2]).suffix.lstrip('.').lower(), record[0]) for record in videos])

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

    def selected_albums(self) -> list[dict[str, object]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM flickr_album WHERE selected_for_migration=1 ORDER BY title COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def album_photos(self, album_id: str) -> list[dict[str, object]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT p.* FROM flickr_album_photo ap JOIN flickr_photo p ON p.flickr_id=ap.photo_flickr_id WHERE ap.album_flickr_id=? ORDER BY ap.position", (album_id,)).fetchall()
        return [dict(row) for row in rows]

    def set_google_album_id(self, album_id: str, google_id: str) -> None:
        with self.connection() as conn: conn.execute("UPDATE flickr_album SET google_album_id=?, album_state='created' WHERE flickr_id=?", (google_id, album_id))

    def set_local_file(self, flickr_id: str, path: str, checksum: str) -> None:
        with self.connection() as conn: conn.execute("UPDATE flickr_photo SET local_path=?, checksum_sha256=?, download_state='verified', verification_state='verified' WHERE flickr_id=?", (path, checksum, flickr_id))

    def mark_uploading(self, flickr_id: str) -> None:
        with self.connection() as conn: conn.execute("UPDATE flickr_photo SET upload_state='uploading' WHERE flickr_id=?", (flickr_id,))

    def mark_uploaded(self, flickr_id: str, google_id: str) -> None:
        with self.connection() as conn: conn.execute("UPDATE flickr_photo SET upload_state='uploaded', google_media_id=? WHERE flickr_id=?", (google_id, flickr_id))

    def archive_source(self, flickr_id: str) -> dict[str, object] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM archive_media WHERE flickr_id=?", (flickr_id,)).fetchone()
        return dict(row) if row else None

    def upload_journal(self, flickr_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT state_json FROM google_upload_journal WHERE flickr_id=?", (flickr_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_upload_journal(self, flickr_id: str, state: dict) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO google_upload_journal VALUES(?,?) ON CONFLICT(flickr_id) DO UPDATE SET state_json=excluded.state_json", (flickr_id, json.dumps(state)))

    def member_state(self, album_id: str, flickr_id: str) -> str:
        with self.connection() as conn:
            row = conn.execute("SELECT google_state FROM flickr_album_photo WHERE album_flickr_id=? AND photo_flickr_id=?", (album_id, flickr_id)).fetchone()
        return str(row[0])

    def set_member_state(self, album_id: str, flickr_id: str, state: str, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE flickr_album_photo SET google_state=?,last_error=? WHERE album_flickr_id=? AND photo_flickr_id=?", (state, error, album_id, flickr_id))

    def set_album_state(self, album_id: str, state: str, error: str | None = None, url: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE flickr_album SET album_state=?,last_error=?,google_album_url=COALESCE(?,google_album_url),updated_at=CURRENT_TIMESTAMP WHERE flickr_id=?", (state, error, url, album_id))

    def uploaded_checksum_match(self, checksum: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT google_media_id FROM flickr_photo WHERE checksum_sha256=? AND google_media_id IS NOT NULL AND upload_state='uploaded' LIMIT 1", (checksum,)).fetchone()
        return str(row[0]) if row else None

    def checksum_in_flight(self, checksum: str, flickr_id: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM flickr_photo WHERE checksum_sha256=? AND flickr_id<>? AND upload_state='uploading' LIMIT 1", (checksum, flickr_id)).fetchone() is not None

    def migration_albums(self) -> list[dict[str, object]]:
        with self.connection() as conn:
            rows = conn.execute("""SELECT a.*, COUNT(ap.photo_flickr_id) AS inventoried,
                COALESCE(SUM(am.flickr_id IS NOT NULL),0) AS indexed,
                COALESCE(SUM(ap.google_state='added'),0) AS completed,
                COALESCE(SUM(ap.google_state='failed'),0) AS failed,
                COALESCE(SUM(ap.google_state='reconcile'),0) AS reconcile
                FROM flickr_album a LEFT JOIN flickr_album_photo ap ON ap.album_flickr_id=a.flickr_id
                LEFT JOIN archive_media am ON am.flickr_id=ap.photo_flickr_id
                GROUP BY a.flickr_id ORDER BY a.title COLLATE NOCASE""").fetchall()
        return [dict(row) for row in rows]

    def set_photo_error(self, flickr_id: str, error: str, *, upload_failed: bool = False) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE flickr_photo SET last_error=?, upload_state=CASE WHEN ? AND upload_state<>'uploading' THEN 'failed' ELSE upload_state END WHERE flickr_id=?", (error, upload_failed, flickr_id))

    def clear_local_path(self, flickr_id: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE flickr_photo SET local_path=NULL WHERE flickr_id=?", (flickr_id,))
