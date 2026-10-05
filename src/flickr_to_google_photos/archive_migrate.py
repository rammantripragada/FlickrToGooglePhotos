"""Local ZIP migration with durable media IDs, membership and transfer state."""
from __future__ import annotations

import fcntl
import hashlib
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from zipfile import ZipFile

from .database import MigrationDatabase
from .google_upload import ArchiveGoogleClient, MediaCreateRejected, UploadPaused
from .integrity import sha256_file

LOG = logging.getLogger(__name__)


@dataclass
class AlbumProgress:
    album_id: str
    title: str
    total: int
    completed: int = 0
    uploaded: int = 0
    reused: int = 0
    failed: int = 0
    phase: str = "Queued"
    item: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""


class ArchiveMigrationService:
    def __init__(self, database: MigrationDatabase, work_dir: Path, *, google=None,
                 progress: Callable[[dict], None] = lambda _event: None, stop: Event | None = None):
        self.database, self.work_dir = database, work_dir.expanduser().resolve()
        self.google = google
        self.progress = progress
        self.stop = stop or Event()
        self.current: AlbumProgress | None = None

    def _emit(self, phase: str, message: str = "") -> None:
        if self.current:
            self.current.phase, self.current.message = phase, message
            self.progress(asdict(self.current))

    def preflight(self) -> dict:
        """Read-only local readiness check; no Google authentication required."""
        albums = self.database.selected_albums()
        if not albums:
            raise RuntimeError("Select at least one album in Albums first.")
        result = []
        for album in albums:
            photos = self.database.album_photos(str(album["flickr_id"]))
            missing = []
            largest = 0
            for photo in photos:
                if photo["google_media_id"]:
                    continue
                source = self.database.archive_source(str(photo["flickr_id"]))
                local = photo["local_path"] and Path(str(photo["local_path"])).is_file()
                if not local and (not source or not Path(str(source["archive_path"])).is_file()):
                    missing.append(str(photo["flickr_id"]))
                if source:
                    largest = max(largest, int(source["byte_size"] or 0))
            expected = int(album["photo_count"] or len(photos))
            result.append({"album_id": album["flickr_id"], "title": album["title"],
                "total": len(photos), "expected": expected, "missing": len(missing),
                "missing_sample": missing[:10], "largest_file_bytes": largest,
                "ready": bool(photos) and not missing and len(photos) >= expected and len(photos) <= 20000})
        return {"ready": all(row["ready"] for row in result), "albums": result}

    def run(self) -> dict[str, int]:
        self.database.initialize()
        lock_path = self.database.path.with_suffix(self.database.path.suffix + ".migration.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another migration is already running for this database.") from None
            return self._run_locked()

    def _run_locked(self) -> dict[str, int]:
        check = self.preflight()
        if not check["ready"]:
            details = "; ".join(f"{r['title']}: {r['missing']} missing files, {r['total']}/{r['expected']} metadata items" for r in check["albums"] if not r["ready"])
            raise RuntimeError(f"Local archive is incomplete for selected albums. Re-index both disks and import all metadata parts. {details}")
        google = self.google or ArchiveGoogleClient()
        self.google = google
        google.should_stop = self.stop.is_set
        google.on_status = lambda text: self._emit("Retrying", text)
        google.check_authorization()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        totals = {"albums_completed": 0, "uploaded": 0, "reused": 0, "failed": 0, "reconcile_required": 0, "paused": 0}
        occupied_titles: set[str] | None = None
        for album in self.database.selected_albums():
            album_id = str(album["flickr_id"])
            photos = self.database.album_photos(album_id)
            complete = sum(self.database.member_state(album_id, str(p["flickr_id"])) == "added" for p in photos)
            self.current = AlbumProgress(album_id, str(album["title"]), len(photos), completed=complete)
            creation_pending = not album["google_album_id"] and album["album_state"] in {"creating", "reconcile"}
            try:
                if self.stop.is_set():
                    raise UploadPaused("Migration paused")
                google_album_id = album["google_album_id"]
                if not google_album_id:
                    if album["album_state"] in {"creating", "reconcile"}:
                        raise RuntimeError("An earlier album-create response was interrupted. Reconcile the Google album ID before retrying.")
                    self._emit("Checking Google album names")
                    if occupied_titles is None:
                        occupied_titles = {str(item["title"]) for item in google.list_albums()
                                           if "title" in item}
                    destination_title = self._destination_title(str(album["title"]), occupied_titles)
                    self._emit("Creating album", destination_title)
                    self.database.set_album_state(album_id, "creating")
                    creation_pending = True
                    details = google.create_album_details(destination_title)
                    google_album_id = str(details["id"])
                    self.database.set_google_album_id(album_id, google_album_id)
                    creation_pending = False
                    occupied_titles.add(destination_title)
                    self.database.set_album_state(album_id, "migrating", url=details.get("productUrl"))
                else:
                    self.database.set_album_state(album_id, "migrating")
                    # Obtain the actual Google link, never construct one from an ID.
                    details = google.album_details(str(google_album_id))
                    if not details.get("isWriteable", True):
                        raise RuntimeError("The linked Google album is not writable by this application.")
                    self.database.set_album_state(album_id, "migrating", url=details.get("productUrl"))
                consecutive_failures = 0
                for photo in photos:
                    if self.stop.is_set():
                        raise UploadPaused("Migration paused")
                    flickr_id = str(photo["flickr_id"])
                    if self.database.member_state(album_id, flickr_id) == "added":
                        continue
                    self.current.item = str(photo["title"] or photo["filename"] or flickr_id)
                    self.current.bytes_done = self.current.bytes_total = 0
                    try:
                        media_id, uploaded, extracted = self._media_id(photo)
                        self._emit("Adding to album")
                        google.add_media(str(google_album_id), [media_id])
                        self.database.set_member_state(album_id, flickr_id, "added")
                        self.current.completed += 1
                        self.current.uploaded += int(uploaded)
                        self.current.reused += int(not uploaded)
                        totals["uploaded" if uploaded else "reused"] += 1
                        consecutive_failures = 0
                        if extracted:
                            self._cleanup(flickr_id, extracted)
                        self._emit("Migrating")
                    except UploadPaused:
                        raise
                    except Exception as error:
                        journal = self.database.upload_journal(flickr_id)
                        reconcile = bool(journal.get("create_started"))
                        self.database.set_member_state(album_id, flickr_id, "reconcile" if reconcile else "failed", str(error))
                        self.database.set_photo_error(flickr_id, str(error))
                        self.current.failed += 1
                        totals["reconcile_required" if reconcile else "failed"] += 1
                        LOG.exception("archive_item_failed", extra={"flickr_id": flickr_id, "album_id": album_id})
                        self._emit("Item failed", str(error))
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            raise RuntimeError("Three consecutive items failed. Retry after resolving the displayed error.") from error
                status = "completed" if self.current.completed == len(photos) else "failed"
                self.database.set_album_state(album_id, status)
                self._emit(status.title())
                if status == "completed":
                    totals["albums_completed"] += 1
            except UploadPaused:
                self.database.set_album_state(album_id, "reconcile" if creation_pending else "paused")
                self._emit("Paused")
                totals["paused"] = 1
                return totals
            except Exception as error:
                self.database.set_album_state(album_id, "reconcile" if creation_pending else "failed", str(error))
                self._emit("Failed", str(error))
                raise
        return totals

    @staticmethod
    def _destination_title(title: str, occupied_titles: set[str]) -> str:
        # Google cannot expose manually-created albums, so always suffix NEW
        # destinations. Only a durable saved album ID is trusted for resumption.
        number = 0
        while True:
            suffix = "_flickr" if number == 0 else f"_flickr_{number}"
            candidate = title[:500 - len(suffix)] + suffix
            if candidate not in occupied_titles:
                return candidate
            number += 1

    def _media_id(self, photo: dict[str, object]) -> tuple[str, bool, Path | None]:
        flickr_id = str(photo["flickr_id"])
        if photo["google_media_id"]:
            local = Path(str(photo["local_path"])) if photo["local_path"] else None
            managed = local if local and local.resolve().parent == self.work_dir and local.name.startswith(f"{flickr_id}_") else None
            return str(photo["google_media_id"]), False, managed
        state = self.database.upload_journal(flickr_id)
        if (photo["upload_state"] == "uploading" or state.get("create_started")) and not (state.get("token") and time.time() - state.get("token_time", 0) < 23 * 3600):
            raise RuntimeError("An earlier Google create may have completed; reconcile this item before retrying.")
        path, managed = self._extract(photo)
        # Extraction already computed SHA-256 while reading ZIP bytes. A reused
        # local file is hashed again by _extract to verify its recorded checksum.
        with self.database.connection() as conn:
            checksum = str(conn.execute("SELECT checksum_sha256 FROM flickr_photo WHERE flickr_id=?", (flickr_id,)).fetchone()[0])
        self.database.set_local_file(flickr_id, str(path), checksum)
        existing = self.database.uploaded_checksum_match(checksum)
        if existing:
            self.database.mark_uploaded(flickr_id, existing)
            return existing, False, path if managed else None
        if self.database.checksum_in_flight(checksum, flickr_id):
            raise RuntimeError("Identical content has an interrupted Google create; reconcile that item first.")
        if state.get("checksum") and state["checksum"] != checksum:
            if state.get("create_started"):
                raise RuntimeError("File content changed after Google creation began; reconciliation required.")
            state.clear()
        def save(new_state: dict) -> None:
            new_state["checksum"] = checksum
            self.database.save_upload_journal(flickr_id, new_state)
        self._emit("Uploading")
        token = self.google.upload_bytes(path, state, save, self._byte_progress)
        if self.stop.is_set():
            raise UploadPaused("Migration paused")
        state["create_started"] = True
        save(state)
        self.database.mark_uploading(flickr_id)
        self._emit("Creating media item")
        try:
            media_id = self.google.create_media(token, path.name.split("_", 1)[-1] if managed else path.name, str(photo["description"] or ""))
        except MediaCreateRejected:
            state.pop("create_started", None)
            save(state)
            self.database.set_photo_error(flickr_id, "Google explicitly rejected this media", upload_failed=True)
            # Clear the guard only for an explicit per-item rejection.
            with self.database.connection() as conn:
                conn.execute("UPDATE flickr_photo SET upload_state='failed' WHERE flickr_id=?", (flickr_id,))
            raise
        self.database.mark_uploaded(flickr_id, media_id)
        self.database.save_upload_journal(flickr_id, {})
        return media_id, True, path if managed else None

    def _byte_progress(self, sent: int, total: int) -> None:
        if self.current:
            self.current.bytes_done, self.current.bytes_total = sent, total
            self._emit("Uploading")

    def _extract(self, photo: dict[str, object]) -> tuple[Path, bool]:
        flickr_id = str(photo["flickr_id"])
        if photo["local_path"]:
            local = Path(str(photo["local_path"]))
            if local.is_file() and photo["checksum_sha256"] and sha256_file(local) == photo["checksum_sha256"]:
                return local, local.resolve().parent == self.work_dir
        source = self.database.archive_source(flickr_id)
        if not source:
            raise RuntimeError(f"No local ZIP member indexed for {flickr_id}")
        name = Path(str(source["member_name"])).name
        target = self.work_dir / f"{flickr_id}_{name}"
        partial = target.with_suffix(target.suffix + ".part")
        self._emit("Extracting", name)
        try:
            with ZipFile(str(source["archive_path"])) as archive:
                member = archive.getinfo(str(source["member_name"]))
                if member.file_size != source["byte_size"] or member.file_size == 0:
                    raise RuntimeError("Archive member size differs from the index; re-index this disk")
                if shutil.disk_usage(self.work_dir).free < member.file_size + 64 * 1024**2:
                    raise RuntimeError("Not enough working disk space for this item. Choose a different working folder.")
                digest, count = hashlib.sha256(), 0
                last_update = time.monotonic()
                with archive.open(member) as src, partial.open("wb") as dst:
                    while block := src.read(1024**2):
                        if self.stop.is_set():
                            raise UploadPaused("Migration paused")
                        dst.write(block)
                        digest.update(block)
                        count += len(block)
                        if self.current and (time.monotonic() - last_update >= 0.25 or count == member.file_size):
                            self.current.bytes_done, self.current.bytes_total = count, member.file_size
                            self._emit("Extracting", name)
                            last_update = time.monotonic()
                if count != member.file_size:
                    raise RuntimeError("Incomplete ZIP member extraction")
                partial.replace(target)
                self.database.set_local_file(flickr_id, str(target), digest.hexdigest())
                return target, True
        finally:
            partial.unlink(missing_ok=True)

    def _cleanup(self, flickr_id: str, path: Path) -> None:
        # Delete only this service's expendable working copy after confirmation.
        if path.resolve().parent == self.work_dir and path.name.startswith(f"{flickr_id}_"):
            path.unlink(missing_ok=True)
            self.database.clear_local_path(flickr_id)
