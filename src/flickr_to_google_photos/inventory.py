"""Read-only Flickr discovery orchestration."""

from __future__ import annotations

import logging
import json
import re
from pathlib import Path
from zipfile import ZipFile
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from .database import MigrationDatabase
from .flickr import FlickrClient, parse_album, parse_photo
from .flickr import FlickrAlbum, FlickrPhoto

LOG = logging.getLogger(__name__)


class InventoryService:
    def __init__(self, client: FlickrClient, database: MigrationDatabase) -> None:
        self.client = client
        self.database = database

    def run(
        self,
        include_photo_details: bool = True,
        dry_run: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
        photo_workers: int = 5,
    ) -> dict[str, int]:
        """Inventory source metadata; never downloads, edits, or deletes remote content."""
        account = self.client.authenticated_account()
        if dry_run:
            return {"photos": sum(1 for _ in self.client.iter_photos(account.nsid)), "albums": sum(1 for _ in self.client.iter_albums(account.nsid))}
        self.database.initialize()
        self.database.upsert_account(account.nsid, account.username, account.realname)

        # Albums are intentionally discovered first. Their names become available
        # for selection while a large photo/video inventory continues in parallel.
        album_total = 0
        def album_progress(_count: int, total: int) -> None:
            nonlocal album_total
            album_total = total
        albums = []
        for album_count, raw_album in enumerate(self.client.iter_albums(account.nsid, progress=album_progress), start=1):
            album = parse_album(raw_album)
            self.database.upsert_album(account.nsid, album)
            albums.append(album)
            if progress:
                progress("albums", album_count, album_total)

        photo_total = 0
        def photo_progress(_count: int, total: int) -> None:
            nonlocal photo_total
            photo_total = total
        def fetch_photo(listed: dict) -> object:
            worker = getattr(self.client, "new_worker", lambda: self.client)()
            photo_id = str(listed["id"])
            raw = worker.photo_info(photo_id) if include_photo_details else listed
            return parse_photo(raw, worker.original_url(photo_id))

        # Keep only a small bounded queue in memory for very large libraries.
        in_flight: set[Future[object]] = set()
        completed_photos = 0
        max_in_flight = max(1, photo_workers) * 3
        with ThreadPoolExecutor(max_workers=max(1, photo_workers), thread_name_prefix="flickr-detail") as executor:
            for listed in self.client.iter_photos(account.nsid, progress=photo_progress):
                in_flight.add(executor.submit(fetch_photo, listed))
                if len(in_flight) >= max_in_flight:
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        self.database.upsert_photo(account.nsid, future.result())
                        completed_photos += 1
                        if progress:
                            progress("photos and videos", completed_photos, photo_total)
            for future in in_flight:
                self.database.upsert_photo(account.nsid, future.result())
                completed_photos += 1
                if progress:
                    progress("photos and videos", completed_photos, photo_total)
        for membership_count, album in enumerate(albums, start=1):
            self.database.replace_album_membership(album.id, list(self.client.iter_album_photo_ids(album.id, account.nsid)))
            if progress:
                progress("album memberships", membership_count, album_total)
        summary = self.database.summary()
        LOG.info("inventory_complete")
        return summary

    def run_selected_albums(self, photo_workers: int = 5, progress: Callable[[str, int, int], None] | None = None) -> dict[str, int]:
        """Fast path: inventory only approved album members, not the full photostream."""
        account = self.client.authenticated_account()
        self.database.initialize(); self.database.upsert_account(account.nsid, account.username, account.realname)
        albums = self.database.selected_albums()
        if not albums: raise RuntimeError("No albums are approved for Google sync.")
        for album_number, album in enumerate(albums, start=1):
            listed = list(self.client.iter_album_photos(str(album["flickr_id"]), account.nsid))
            def fetch(item: dict) -> object:
                # photosets.getPhotos already supplies the requested title,
                # description, tags, dates, geo, media type, and url_o extras.
                # Avoid a second getInfo call for every item; fall back only
                # when Flickr omitted the original-size URL.
                original_url = item.get("url_o")
                if original_url:
                    return parse_photo(item, str(original_url))
                worker = getattr(self.client, "new_worker", lambda: self.client)()
                photo_id = str(item["id"])
                return parse_photo(item, worker.original_url(photo_id))
            with ThreadPoolExecutor(max_workers=max(1, photo_workers)) as executor:
                futures = [executor.submit(fetch, item) for item in listed]
                for count, future in enumerate(futures, start=1):
                    self.database.upsert_photo(account.nsid, future.result())
                    if progress: progress(f"{album['title']} media", count, len(listed))
            self.database.replace_album_membership(str(album["flickr_id"]), [str(item["id"]) for item in listed])
            if progress: progress("approved albums", album_number, len(albums))
        return self.database.summary()


def import_archive_metadata(database: MigrationDatabase, archive_part: Path) -> dict[str, int]:
    """Import Flickr Data JSON from all sibling ``*_partN`` folders, offline."""
    root = archive_part.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Archive folder does not exist: {root}")
    zip_paths = sorted(root.glob("*.zip"))
    if zip_paths:
        # Flickr account-data ZIPs contain account_profile.json; media ZIPs do
        # not, so they are naturally ignored here.
        profile: dict | None = None
        albums: dict[str, dict] = {}
        photos: list[dict] = []
        for zip_path in zip_paths:
            with ZipFile(zip_path) as archive:
                names = archive.namelist()
                if not ("account_profile.json" in names or "albums.json" in names or any(name.startswith("photo_") and name.endswith(".json") for name in names)):
                    continue
                if profile is None:
                    profile = json.loads(archive.read("account_profile.json"))
                if "albums.json" in names:
                    for raw in json.loads(archive.read("albums.json")).get("albums", []):
                        albums[str(raw["id"])] = raw
                for name in names:
                    if name.startswith("photo_") and name.endswith(".json"):
                        photos.append(json.loads(archive.read(name)))
        if profile is None:
            raise RuntimeError("No Flickr account-data ZIPs found (expected account_profile.json).")
        return _store_archive_records(database, profile, albums, photos)
    prefix = root.name.rsplit("_part", 1)[0]
    parts = sorted(path for path in root.parent.glob(f"{prefix}_part*") if path.is_dir())
    if not parts:
        parts = [root]
    profile_path = next((part / "account_profile.json" for part in parts if (part / "account_profile.json").is_file()), None)
    if not profile_path:
        raise RuntimeError("No account_profile.json found in the Flickr archive folders.")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    nsid = str(profile["nsid"])
    database.initialize()
    database.upsert_account(nsid, profile.get("screen_name"), profile.get("real_name"))
    albums: dict[str, dict] = {}
    imported_photo_ids: set[str] = set()
    for part in parts:
        album_path = part / "albums.json"
        if album_path.is_file():
            for raw in json.loads(album_path.read_text(encoding="utf-8")).get("albums", []):
                albums[str(raw["id"])] = raw
        for path in part.glob("photo_*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            imported_photo_ids.add(str(raw["id"]))
            original = raw.get("original")
            filename = Path(str(original)).name if original else raw.get("name")
            tags = [str(tag.get("name") if isinstance(tag, dict) else tag) for tag in raw.get("tags", [])]
            geo = raw.get("geo") or []
            location = geo[0] if geo and isinstance(geo[0], dict) else {}
            extension = Path(str(filename or "")).suffix.lower()
            media_type = "video" if extension in {".mp4", ".mov", ".avi", ".mkv", ".m4v"} else "photo"
            photo = FlickrPhoto(str(raw["id"]), filename, raw.get("name"), raw.get("description"), tags,
                raw.get("date_taken"), raw.get("date_imported"),
                _number(location.get("latitude")), _number(location.get("longitude")), _integer(location.get("accuracy")),
                original, extension.lstrip(".") or None, media_type, raw)
            database.upsert_photo(nsid, photo)
    for album_id, raw in albums.items():
        database.upsert_album(nsid, FlickrAlbum(album_id, raw.get("title") or "Untitled Flickr album", raw.get("description"), _integer(raw.get("photo_count")), raw))
        # A single archive part only contains a slice of photo JSON.  Keep the
        # memberships whose metadata is available; rerunning after all parts
        # arrive expands the same album deterministically.
        database.replace_album_membership(album_id, [str(photo_id) for photo_id in raw.get("photos", []) if str(photo_id) in imported_photo_ids])
    return database.summary()


def _number(value: object) -> float | None:
    try: return float(value) if value not in (None, "") else None
    except (TypeError, ValueError): return None


def _integer(value: object) -> int | None:
    try: return int(value) if value not in (None, "") else None
    except (TypeError, ValueError): return None


def _store_archive_records(database: MigrationDatabase, profile: dict, albums: dict[str, dict], records: list[dict]) -> dict[str, int]:
    """Store metadata read directly from Flickr account-data ZIP members."""
    nsid = str(profile["nsid"])
    database.initialize()
    database.upsert_account(nsid, profile.get("screen_name"), profile.get("real_name"))
    imported: set[str] = set()
    for raw in records:
        photo_id = str(raw["id"]); imported.add(photo_id)
        original = raw.get("original")
        filename = Path(str(original)).name if original else raw.get("name")
        extension = Path(str(filename or "")).suffix.lower()
        media_type = "video" if extension in {".mp4", ".mov", ".avi", ".mkv", ".m4v"} else "photo"
        tags = [str(tag.get("name") if isinstance(tag, dict) else tag) for tag in raw.get("tags", [])]
        geo = raw.get("geo") or []; location = geo[0] if geo and isinstance(geo[0], dict) else {}
        database.upsert_photo(nsid, FlickrPhoto(photo_id, filename, raw.get("name"), raw.get("description"), tags,
            raw.get("date_taken"), raw.get("date_imported"), _number(location.get("latitude")), _number(location.get("longitude")),
            _integer(location.get("accuracy")), original, extension.lstrip(".") or None, media_type, raw))
    for album_id, raw in albums.items():
        database.upsert_album(nsid, FlickrAlbum(album_id, raw.get("title") or "Untitled Flickr album", raw.get("description"), _integer(raw.get("photo_count")), raw))
        database.replace_album_membership(album_id, [str(item) for item in raw.get("photos", []) if str(item) in imported])
    return database.summary()


def index_archive_media(database: MigrationDatabase, directories: list[Path]) -> dict[str, int]:
    """Index Flickr media ZIP members locally; never extracts or contacts Flickr."""
    database.initialize()
    pattern = re.compile(r"_(\d+)_o(?:\.[^/]+)$", re.IGNORECASE)
    batch: list[tuple[str, str, str, int]] = []
    for directory in directories:
        for zip_path in sorted(directory.expanduser().glob("*.zip")):
            with ZipFile(zip_path) as archive:
                for member in archive.infolist():
                    match = pattern.search(member.filename)
                    if match and not member.is_dir():
                        batch.append((match.group(1), str(zip_path), member.filename, member.file_size))
                        if len(batch) >= 1000:
                            database.upsert_archive_media(batch); batch.clear()
    if batch: database.upsert_archive_media(batch)
    return database.summary()
