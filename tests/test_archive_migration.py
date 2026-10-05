from dataclasses import replace
from pathlib import Path
from threading import Event
from zipfile import ZipFile
import time

import pytest

from flickr_to_google_photos.archive_migrate import ArchiveMigrationService
from flickr_to_google_photos.database import MigrationDatabase
from flickr_to_google_photos.flickr import FlickrAlbum, FlickrPhoto
from flickr_to_google_photos.inventory import index_archive_media


class FakeGoogle:
    def __init__(self):
        self.transfers, self.creates, self.additions, self.albums = [], [], [], []
        self.checked = False
        self.existing_albums = []
        self.list_calls = 0

    def check_authorization(self):
        self.checked = True

    def create_album_details(self, title):
        self.albums.append(title)
        return {"id": f"album-{len(self.albums)}", "productUrl": "https://photos.google.com/album/example"}

    def list_albums(self):
        self.list_calls += 1
        return self.existing_albums + [{"title": title} for title in self.albums]

    def album_details(self, _album_id):
        return {"productUrl": "https://photos.google.com/album/example"}

    def upload_bytes(self, path, state, save, progress):
        if state.get("token"):
            return state["token"]
        self.transfers.append(path.read_bytes())
        state.update(token=f"token-{len(self.transfers)}", token_time=time.time())
        save(state)
        progress(path.stat().st_size, path.stat().st_size)
        return state["token"]

    def create_media(self, token, filename, description):
        self.creates.append((token, filename))
        return f"media-{token}"

    def add_media(self, album_id, media_ids):
        self.additions.append((album_id, media_ids))


@pytest.fixture
def library(tmp_path):
    db = MigrationDatabase(tmp_path / "state.sqlite3")
    db.initialize()
    db.upsert_account("account", "name", None)
    base = FlickrPhoto("9300162799", "image.jpg", "Title", "Description", [], None, None, None, None, None, None, "jpg", "photo", {})
    for photo_id in ("9300162799", "9300162800", "9300162801"):
        db.upsert_photo("account", replace(base, id=photo_id))
    for album_id, members in (("a", ["9300162799", "9300162801"]), ("b", ["9300162799", "9300162800"])):
        db.upsert_album("account", FlickrAlbum(album_id, album_id, None, len(members), {}))
        db.replace_album_membership(album_id, members)
    db.set_selected_albums({"a", "b"})
    archive = tmp_path / "media.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("image_9300162799_o.jpg", b"same-image")
        z.writestr("9300162800_abcd_o.jpg", b"same-image")
        z.writestr("video_9300162801.mov", b"video")
    index_archive_media(db, [tmp_path])
    return db, archive


def test_archive_migration_deduplicates_and_adds_every_album(library, tmp_path):
    db, archive = library
    before = archive.read_bytes()
    google, events = FakeGoogle(), []
    service = ArchiveMigrationService(db, tmp_path / "work", google=google, progress=events.append)
    result = service.run()
    assert result["albums_completed"] == 2
    assert result["uploaded"] == 2  # one image and one video, despite four memberships
    assert result["reused"] == 2
    assert len(google.creates) == 2
    assert len(google.additions) == 4
    assert b"video" in google.transfers
    assert list((tmp_path / "work").iterdir()) == []
    assert archive.read_bytes() == before
    assert events[-1]["phase"] == "Completed"
    assert all(row["completed"] == row["inventoried"] for row in db.migration_albums())
    service.run()
    assert len(google.creates) == 2
    assert len(google.additions) == 4
    db.replace_album_membership("a", ["9300162799", "9300162801"])
    assert db.member_state("a", "9300162799") == "added"


def test_missing_media_blocks_google_writes(library, tmp_path):
    db, archive = library
    archive.rename(tmp_path / "unavailable.zip")
    google = FakeGoogle()
    service = ArchiveMigrationService(db, tmp_path / "work", google=google)
    assert service.preflight()["ready"] is False
    with pytest.raises(RuntimeError, match="incomplete"):
        service.run()
    assert not google.checked and not google.albums


def test_existing_album_name_is_not_reused_without_saved_mapping(library, tmp_path):
    db, _ = library
    google = FakeGoogle()
    google.existing_albums = [{"id": "existing-a", "title": "a"}, {"id": "existing-b", "title": "b"}]
    assert ArchiveMigrationService(db, tmp_path / "work", google=google).run()["albums_completed"] == 2
    assert google.albums == ["a_flickr", "b_flickr"]
    assert {item[0] for item in google.additions} == {"album-1", "album-2"}


def test_new_albums_use_suffix(library, tmp_path):
    db, _ = library
    google = FakeGoogle()
    ArchiveMigrationService(db, tmp_path / "work", google=google).run()
    assert google.albums == ["a_flickr", "b_flickr"]


def test_suffix_skips_occupied_names_and_resume_reuses_saved_ids(library, tmp_path):
    db, _ = library
    google = FakeGoogle()
    google.existing_albums = [{"title": "a_flickr"}, {"title": "a_flickr_1", "isWriteable": False}, {"title": "b_flickr"}]
    service = ArchiveMigrationService(db, tmp_path / "work", google=google)
    service.run()
    assert google.albums == ["a_flickr_2", "b_flickr_1"]
    assert google.list_calls == 1  # once per migration, not once per album
    service.run()
    assert google.albums == ["a_flickr_2", "b_flickr_1"]
    assert google.list_calls == 1


def test_suffix_keeps_long_album_titles_within_google_limit():
    title = "a" * 500
    candidate = ArchiveMigrationService._destination_title(title, {"a" * 493 + "_flickr"})
    assert candidate == "a" * 491 + "_flickr_1"
    assert len(candidate) == 500


def test_same_named_flickr_albums_get_distinct_destinations(library, tmp_path):
    db, _ = library
    with db.connection() as conn:
        conn.execute("UPDATE flickr_album SET title='Trip'")
    google = FakeGoogle()
    ArchiveMigrationService(db, tmp_path / "work", google=google).run()
    assert google.albums == ["Trip_flickr", "Trip_flickr_1"]


def test_pause_after_transfer_retains_token_for_resume(library, tmp_path):
    db, _ = library
    stop = Event()
    google = FakeGoogle()
    original = google.upload_bytes

    def pause_transfer(*args):
        token = original(*args)
        stop.set()
        return token

    google.upload_bytes = pause_transfer
    service = ArchiveMigrationService(db, tmp_path / "work", google=google, stop=stop)
    assert service.run()["paused"] == 1
    assert len(google.transfers) == 1 and not google.creates
    google.upload_bytes = original
    stop.clear()
    assert service.run()["albums_completed"] == 2
    assert len(google.transfers) == 2


def test_membership_retry_reuses_media_and_cleans_retained_working_copies(library, tmp_path):
    db, archive = library
    db.set_selected_albums({"a"})
    google = FakeGoogle()
    original_add = google.add_media
    def fail_add(*_args):
        raise RuntimeError("temporary album failure")
    google.add_media = fail_add
    service = ArchiveMigrationService(db, tmp_path / "work", google=google)
    assert service.run()["failed"] == 2
    assert len(list(service.work_dir.iterdir())) == 2
    google.add_media = original_add
    assert service.run()["albums_completed"] == 1
    assert len(google.transfers) == 2
    assert len(google.creates) == 2
    assert list(service.work_dir.iterdir()) == []
    assert archive.is_file()


def test_index_matches_variants_ignores_sidecars_and_unknown_ids(library, tmp_path):
    db, _ = library
    (tmp_path / "._media.zip").write_bytes(b"macOS sidecar")
    with ZipFile(tmp_path / "extra.zip", "w") as z:
        z.writestr("video_99999999999.mov", b"not inventoried")
        z.writestr("unidentifiable.jpg", b"no id")
    result = index_archive_media(db, [tmp_path])
    assert result["archive_media_indexed"] == 3
    assert result["archive_zips_skipped"] == 0
    assert result["archive_members_unmatched"] == 2
    with db.connection() as c:
        assert c.execute("SELECT media_type FROM flickr_photo WHERE flickr_id='9300162801'").fetchone()[0] == "video"


def test_ambiguous_album_create_is_not_repeated(library, tmp_path):
    db, _ = library
    google = FakeGoogle()
    def fail_create(_title):
        raise RuntimeError("response lost")
    google.create_album_details = fail_create
    service = ArchiveMigrationService(db, tmp_path / "work", google=google)
    with pytest.raises(RuntimeError, match="response lost"):
        service.run()
    assert db.selected_albums()[0]["album_state"] == "reconcile"
    with pytest.raises(RuntimeError, match="Reconcile"):
        service.run()
    with pytest.raises(RuntimeError, match="Reconcile"):
        service.run()


def test_create_intent_blocks_expired_token_even_before_upload_state_commit(library, tmp_path):
    db, _ = library
    photo_id = "9300162799"
    db.save_upload_journal(photo_id, {"create_started": True, "token": "expired",
                                    "token_time": time.time() - 24 * 3600})
    google = FakeGoogle()
    service = ArchiveMigrationService(db, tmp_path / "work", google=google)
    photo = next(p for p in db.album_photos("a") if p["flickr_id"] == photo_id)
    with pytest.raises(RuntimeError, match="reconcile"):
        service._media_id(photo)
    assert not google.transfers and not google.creates


def test_extraction_uses_basename_and_leaves_source_zip(library, tmp_path):
    db, archive = library
    with ZipFile(archive, "a") as z:
        z.writestr("../../escape_9300162801.mov", b"safe-video")
    db.upsert_archive_media([("9300162801", str(archive), "../../escape_9300162801.mov", 10)])
    service = ArchiveMigrationService(db, tmp_path / "work", google=FakeGoogle())
    service.work_dir.mkdir()
    photo = db.album_photos("a")[1]
    path, _ = service._extract(photo)
    assert path.parent == service.work_dir
    assert path.read_bytes() == b"safe-video"
    assert not (tmp_path.parent / "escape_9300162801.mov").exists()
