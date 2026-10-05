from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from zipfile import ZipFile
import time

import pytest

from flickr_to_google_photos.archive_migrate import ArchiveMigrationService
from flickr_to_google_photos.database import MigrationDatabase
from flickr_to_google_photos.flickr import FlickrAlbum, FlickrPhoto
from flickr_to_google_photos.inventory import index_archive_media
from flickr_to_google_photos.google_upload import MediaCreateRejected


class FakeGoogle:
    def __init__(self):
        self.transfers, self.creates, self.additions, self.albums = [], [], [], []
        self.checked = False
        self.existing_albums = []
        self.list_calls = 0
        self.creation_batches = []
        self.membership_batches = []

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

    def create_media_batch(self, items):
        self.creation_batches.append(len(items))
        result = []
        for item in items:
            try:
                result.append(self.create_media(item["token"], item["filename"], item.get("description", "")))
            except Exception as error:
                result.append(error)
        return result

    def add_media(self, album_id, media_ids):
        self.membership_batches.append(len(media_ids))
        self.additions.extend((album_id, [media_id]) for media_id in media_ids)


class ConcurrentGoogle(FakeGoogle):
    def __init__(self, *, synchronize=False, stop=None):
        super().__init__()
        self.guard = Lock()
        self.barrier = Barrier(2) if synchronize else None
        self.stop_after_transfers = stop
        self.active_uploads = self.active_writes = 0
        self.peak_uploads = self.peak_writes = self.peak_work_files = 0

    def upload_bytes(self, path, state, save, progress):
        if state.get("token"):
            return state["token"]
        with self.guard:
            self.active_uploads += 1
            self.peak_uploads = max(self.peak_uploads, self.active_uploads)
            self.peak_work_files = max(self.peak_work_files, len(list(path.parent.iterdir())))
            self.transfers.append(path.read_bytes())
            token = f"token-{len(self.transfers)}"
        try:
            state.update(token=token, token_time=time.time())
            save(state)
            progress(path.stat().st_size, path.stat().st_size)
            if self.barrier is not None:
                self.barrier.wait(timeout=3)
            else:
                time.sleep(0.02)
            if self.stop_after_transfers is not None:
                self.stop_after_transfers.set()
            return token
        finally:
            with self.guard:
                self.active_uploads -= 1

    def _write(self, operation, *args):
        with self.guard:
            self.active_writes += 1
            self.peak_writes = max(self.peak_writes, self.active_writes)
        try:
            time.sleep(0.01)
            return operation(*args)
        finally:
            with self.guard:
                self.active_writes -= 1

    def create_media(self, *args):
        return self._write(super().create_media, *args)

    def add_media(self, *args):
        return self._write(super().add_media, *args)


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


def two_independent_albums(db):
    for album_id, photo_id in (("a", "9300162799"), ("b", "9300162801")):
        db.replace_album_membership(album_id, [photo_id])
    with db.connection() as conn:
        conn.execute("UPDATE flickr_album SET photo_count=1")


def test_different_albums_upload_in_parallel_but_google_writes_are_serial(library, tmp_path):
    db, archive = library
    two_independent_albums(db)
    google, events = ConcurrentGoogle(synchronize=True), []
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=2, progress=events.append)
    result = service.run()
    assert result["albums_completed"] == 2 and result["uploaded"] == 2
    assert google.peak_uploads == 2
    assert google.creation_batches == [2]
    assert google.peak_writes == 1
    assert google.peak_work_files <= 2
    assert any({row["album_id"] for row in event["transfers"]} == {"a", "b"} for event in events)
    assert all(len(event["transfers"]) <= 2 for event in events)
    assert list(service.work_dir.iterdir()) == [] and archive.is_file()


def test_parallel_albums_deduplicate_shared_ids_and_identical_content(library, tmp_path):
    db, archive = library
    before = archive.read_bytes()
    google = ConcurrentGoogle()
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=4, album_workers=2)
    result = service.run()
    assert result["albums_completed"] == 2
    assert result["uploaded"] == result["reused"] == 2
    assert len(google.transfers) == len(google.creates) == 2
    assert google.peak_writes == 1
    assert len(google.additions) == 4
    assert all(row["completed"] == row["inventoried"] for row in db.migration_albums())
    assert archive.read_bytes() == before and list(service.work_dir.iterdir()) == []
    service.run()
    assert len(google.transfers) == 2 and len(google.additions) == 4


def test_parallel_pause_drains_workers_and_resumes_tokens_across_albums(library, tmp_path):
    db, _ = library
    two_independent_albums(db)
    stop = Event()
    google = ConcurrentGoogle(synchronize=True, stop=stop)
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=2, stop=stop)
    assert service.run()["paused"] == 1
    assert not google.creates and google.active_uploads == 0
    assert all(row["album_state"] == "paused" for row in db.selected_albums())
    assert db.upload_journal("9300162799")["token"]
    assert db.upload_journal("9300162801")["token"]
    google.barrier = google.stop_after_transfers = None
    stop.clear()
    assert service.run()["albums_completed"] == 2
    assert len(google.transfers) == len(google.creates) == 2
    assert len(google.albums) == 2 and list(service.work_dir.iterdir()) == []


def test_album_concurrency_limit_is_respected(library, tmp_path):
    db, _ = library
    google, events = ConcurrentGoogle(), []
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=1, progress=events.append)
    assert service.run()["albums_completed"] == 2
    assert all(len({row["album_id"] for row in event["transfers"]}) <= 1 for event in events)


def test_more_albums_do_not_multiply_global_transfer_limit(library, tmp_path):
    db, _ = library
    db.upsert_album("account", FlickrAlbum("c", "c", None, 2, {}))
    db.replace_album_membership("c", ["9300162799", "9300162801"])
    db.set_selected_albums({"a", "b", "c"})
    google, events = ConcurrentGoogle(), []
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=3, progress=events.append)
    assert service.run()["albums_completed"] == 3
    assert all(len(event["transfers"]) <= 2 for event in events)
    assert google.peak_uploads <= 2
    assert len(google.creates) == 2 and len(google.additions) == 6


def test_successful_copy_is_removed_before_other_file_finishes(library, tmp_path):
    db, archive = library
    db.set_selected_albums({"a"})
    first_complete, slow_started, release = Event(), Event(), Event()
    google = ConcurrentGoogle()
    original_upload = google.upload_bytes
    def slow_video(path, *args):
        if path.suffix == ".mov":
            slow_started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test upload timed out")
        return original_upload(path, *args)
    google.upload_bytes = slow_video
    def progress(event):
        if event["completed"] == 1:
            first_complete.set()
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, progress=progress)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.run)
        try:
            assert slow_started.wait(timeout=3)
            assert first_complete.wait(timeout=3)
            assert not list(service.work_dir.glob("9300162799_*"))
            assert list(service.work_dir.glob("9300162801_*"))
            assert archive.is_file()
        finally:
            release.set()
        assert future.result(timeout=5)["albums_completed"] == 1
    assert not list(service.work_dir.iterdir())


def test_membership_writes_are_batched_for_one_album(library, tmp_path):
    db, _ = library
    db.set_selected_albums({"a"})
    google = ConcurrentGoogle(synchronize=True)
    service = ArchiveMigrationService(db, tmp_path / "work", google=google, upload_workers=2)
    assert service.run()["albums_completed"] == 1
    assert google.creation_batches == [2]
    assert google.membership_batches == [2]


def test_batch_size_one_disables_grouping_without_disabling_parallel_bytes(library, tmp_path):
    db, _ = library
    two_independent_albums(db)
    google = ConcurrentGoogle(synchronize=True)
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=2, batch_size=1)
    assert service.run()["albums_completed"] == 2
    assert google.creation_batches == [1, 1]
    assert google.peak_uploads == 2


def test_partial_batch_failure_retains_successful_ids_and_retries_only_failure(library, tmp_path):
    db, _ = library
    two_independent_albums(db)
    google = ConcurrentGoogle(synchronize=True)
    original_create = google.create_media
    def reject_video(token, filename, description):
        if filename.endswith(".mov"):
            raise MediaCreateRejected("video rejected")
        return original_create(token, filename, description)
    google.create_media = reject_video
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=2)
    result = service.run()
    assert result["albums_completed"] == 1 and result["failed"] == 1
    assert db.member_state("a", "9300162799") == "added"
    assert db.member_state("b", "9300162801") == "failed"
    assert not db.upload_journal("9300162801").get("create_started")
    google.create_media = original_create
    google.barrier = None
    assert service.run()["albums_completed"] == 2
    assert len(google.transfers) == 2
    assert len(google.albums) == 2
    assert db.member_state("b", "9300162801") == "added"


def test_incomplete_batch_response_keeps_all_creation_intents(library, tmp_path):
    db, _ = library
    two_independent_albums(db)
    google = ConcurrentGoogle(synchronize=True)
    original_create = google.create_media_batch
    google.create_media_batch = lambda _items: []
    service = ArchiveMigrationService(db, tmp_path / "work", google=google,
        upload_workers=2, album_workers=2)
    assert service.run()["reconcile_required"] == 2
    assert db.upload_journal("9300162799")["create_started"]
    assert db.upload_journal("9300162801")["create_started"]
    google.create_media_batch = original_create
    google.barrier = None
    assert service.run()["albums_completed"] == 2
    assert len(google.transfers) == 2


@pytest.mark.parametrize("kwargs", [{"upload_workers": 0}, {"upload_workers": 9}, {"album_workers": 0}, {"album_workers": 9}, {"batch_size": 0}, {"batch_size": 51}])
def test_invalid_concurrency_is_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        ArchiveMigrationService(MigrationDatabase(tmp_path / "state.sqlite3"), tmp_path / "work", **kwargs)
