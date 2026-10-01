"""Read-only Flickr discovery orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from .database import MigrationDatabase
from .flickr import FlickrClient, parse_album, parse_photo

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
                worker = getattr(self.client, "new_worker", lambda: self.client)()
                photo_id = str(item["id"])
                return parse_photo(worker.photo_info(photo_id), worker.original_url(photo_id))
            with ThreadPoolExecutor(max_workers=max(1, photo_workers)) as executor:
                futures = [executor.submit(fetch, item) for item in listed]
                for count, future in enumerate(futures, start=1):
                    self.database.upsert_photo(account.nsid, future.result())
                    if progress: progress(f"{album['title']} media", count, len(listed))
            self.database.replace_album_membership(str(album["flickr_id"]), [str(item["id"]) for item in listed])
            if progress: progress("approved albums", album_number, len(albums))
        return self.database.summary()
