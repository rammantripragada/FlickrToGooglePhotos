from __future__ import annotations

from flickr_to_google_photos.database import MigrationDatabase
from flickr_to_google_photos.flickr import FlickrAccount
from flickr_to_google_photos.inventory import InventoryService


class FakeFlickr:
    def authenticated_account(self): return FlickrAccount("me", "me", None)
    def iter_photos(self, _, progress=None):
        if progress: progress(1, 1)
        return iter([{"id": "p1"}])
    def photo_info(self, _): return {"id": "p1", "title": {"_content": "One"}, "tags": {"tag": []}}
    def original_url(self, _): return "https://example.test/p1.jpg"
    def iter_albums(self, _, progress=None):
        if progress: progress(1, 1)
        return iter([{"id": "a1", "title": {"_content": "Set"}, "photos": "1"}])
    def iter_album_photo_ids(self, _, __=None): return iter(["p1"])


def test_inventory_is_resumable(tmp_path):
    database = MigrationDatabase(tmp_path / "state.sqlite3")
    service = InventoryService(FakeFlickr(), database)
    progress: list[tuple[str, int, int]] = []
    assert service.run(progress=lambda stage, count, total: progress.append((stage, count, total)))["photos"] == 1
    assert service.run()["album_memberships"] == 1
    assert progress == [("albums", 1, 1), ("photos and videos", 1, 1), ("album memberships", 1, 1)]
