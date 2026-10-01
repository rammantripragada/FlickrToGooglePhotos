"""Read-only Flickr discovery orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable

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
    ) -> dict[str, int]:
        """Inventory source metadata; never downloads, edits, or deletes remote content."""
        account = self.client.authenticated_account()
        if dry_run:
            return {"photos": sum(1 for _ in self.client.iter_photos(account.nsid)), "albums": sum(1 for _ in self.client.iter_albums(account.nsid))}
        self.database.initialize()
        self.database.upsert_account(account.nsid, account.username, account.realname)
        photo_total = 0
        def photo_progress(_count: int, total: int) -> None:
            nonlocal photo_total
            photo_total = total
        for photo_count, listed in enumerate(self.client.iter_photos(account.nsid, progress=photo_progress), start=1):
            photo_id = str(listed["id"])
            raw = self.client.photo_info(photo_id) if include_photo_details else listed
            original_url = self.client.original_url(photo_id)
            self.database.upsert_photo(account.nsid, parse_photo(raw, original_url))
            if progress:
                progress("photos and videos", photo_count, photo_total)
        album_total = 0
        def album_progress(_count: int, total: int) -> None:
            nonlocal album_total
            album_total = total
        for album_count, raw_album in enumerate(self.client.iter_albums(account.nsid, progress=album_progress), start=1):
            album = parse_album(raw_album)
            self.database.upsert_album(account.nsid, album)
            self.database.replace_album_membership(album.id, list(self.client.iter_album_photo_ids(album.id)))
            if progress:
                progress("albums", album_count, album_total)
        summary = self.database.summary()
        LOG.info("inventory_complete")
        return summary
