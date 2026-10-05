"""Local ZIP migration with durable media IDs, membership and transfer state."""
from __future__ import annotations

import fcntl
import hashlib
import logging
import shutil
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, RLock, local
from zipfile import ZipFile

from .database import MigrationDatabase
from .google_batch import GoogleWriteBatcher
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
                 progress: Callable[[dict], None] = lambda _event: None, stop: Event | None = None,
                 upload_workers: int = 1, album_workers: int = 1, batch_size: int = 50):
        if not 1 <= upload_workers <= 8:
            raise ValueError("Upload workers must be between 1 and 8")
        if not 1 <= album_workers <= 8:
            raise ValueError("Parallel albums must be between 1 and 8")
        if not 1 <= batch_size <= 50:
            raise ValueError("Batch size must be between 1 and 50")
        self.database, self.work_dir = database, work_dir.expanduser().resolve()
        self.google = google
        self.progress = progress
        self.stop = stop or Event()
        self.upload_workers = upload_workers
        self.album_workers = album_workers
        self.batch_size = batch_size
        self.current: AlbumProgress | None = None
        self._abort = Event()
        self._progress_lock = RLock()
        self._extract_lock, self._write_lock = Lock(), Lock()
        self._checksum_locks: dict[str, Lock] = {}
        self._photo_locks: dict[str, Lock] = {}
        self._active: dict[str, dict] = {}
        self._thread = local()

    def _stopping(self) -> bool:
        return self.stop.is_set() or self._abort.is_set()

    @contextmanager
    def _acquire(self, lock):
        while not lock.acquire(timeout=0.1):
            if self._stopping():
                raise UploadPaused("Migration paused")
        try:
            if self._stopping():
                raise UploadPaused("Migration paused")
            yield
        finally:
            lock.release()

    def _emit(self, phase: str, message: str = "", *, album: AlbumProgress | None = None) -> None:
        with self._progress_lock:
            current = album or getattr(self._thread, "album", None) or self.current
            if not current:
                return
            item = getattr(self._thread, "item", None)
            if item is not None:
                item.update(phase=phase, message=message)
                event = {**asdict(current), **{k: item[k] for k in
                         ("phase", "message", "item", "bytes_done", "bytes_total")}}
            else:
                current.phase, current.message = phase, message
                event = asdict(current)
            event["transfers"] = [dict(row) for row in self._active.values()]
            event["upload_workers"] = self.upload_workers
            event["album_workers"] = self.album_workers
            event["batch_size"] = self.batch_size
            self.progress(event)

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
        self._abort.clear()
        lock_path = self.database.path.with_suffix(self.database.path.suffix + ".migration.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another migration is already running for this database.") from None
            try:
                return self._run_locked()
            finally:
                batcher = getattr(self, "_batcher", None)
                if batcher:
                    batcher.close()
                if isinstance(self.google, ArchiveGoogleClient):
                    self.google.close()

    def _run_locked(self) -> dict[str, int]:
        check = self.preflight()
        if not check["ready"]:
            details = "; ".join(f"{r['title']}: {r['missing']} missing files, {r['total']}/{r['expected']} metadata items" for r in check["albums"] if not r["ready"])
            raise RuntimeError(f"Local archive is incomplete for selected albums. Re-index both disks and import all metadata parts. {details}")
        google = self.google or ArchiveGoogleClient()
        self.google = google
        google.should_stop = self._stopping
        google.on_status = lambda text: self._emit("Retrying", text)
        google.check_authorization()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        totals = {"albums_completed": 0, "uploaded": 0, "reused": 0, "failed": 0, "reconcile_required": 0, "paused": 0}
        self._occupied_titles = None
        self._batcher = GoogleWriteBatcher(google, lambda: self._acquire(self._write_lock),
            self._stopping, batch_size=self.batch_size, max_pending=self.upload_workers)
        self._schedule(self.database.selected_albums(), totals)
        return totals

    def _prepare_album(self, album: dict) -> dict:
        album_id = str(album["flickr_id"])
        photos = self.database.album_photos(album_id)
        with self.database.connection() as conn:
            added = {row[0] for row in conn.execute("SELECT photo_flickr_id FROM flickr_album_photo WHERE album_flickr_id=? AND google_state='added'", (album_id,))}
        progress = AlbumProgress(album_id, str(album["title"]), len(photos), completed=len(added))
        self.current = progress
        creation_pending = not album["google_album_id"] and album["album_state"] in {"creating", "reconcile"}
        try:
            google_album_id = album["google_album_id"]
            if not google_album_id:
                if creation_pending:
                    raise RuntimeError("An earlier album-create response was interrupted. Reconcile the Google album ID before retrying.")
                self._emit("Checking Google album names")
                if self._occupied_titles is None:
                    self._occupied_titles = {str(item["title"]) for item in self.google.list_albums() if "title" in item}
                title = self._destination_title(str(album["title"]), self._occupied_titles)
                self._emit("Creating album", title)
                with self._acquire(self._write_lock):
                    self.database.set_album_state(album_id, "creating")
                    creation_pending = True
                    details = self.google.create_album_details(title)
                    google_album_id = str(details["id"])
                    self.database.set_google_album_id(album_id, google_album_id)
                    creation_pending = False
                    self._occupied_titles.add(title)
            else:
                details = self.google.album_details(str(google_album_id))
                if not details.get("isWriteable", True):
                    raise RuntimeError("The linked Google album is not writable by this application.")
            self.database.set_album_state(album_id, "migrating", url=details.get("productUrl"))
            self._emit("Migrating")
            return {"progress": progress, "google_id": str(google_album_id), "in_flight": 0,
                    "pending": iter(p for p in photos if str(p["flickr_id"]) not in added),
                    "deferred": deque(), "exhausted": False}
        except Exception as error:
            state = "reconcile" if creation_pending else "paused" if isinstance(error, UploadPaused) else "failed"
            self.database.set_album_state(album_id, state, str(error))
            self._emit(state.title(), str(error))
            raise

    def _schedule(self, albums: list[dict], totals: dict) -> None:
        contexts, futures, rotation = {}, {}, deque()
        next_album = 0
        consecutive_failures = 0
        fatal_error = None
        paused = False
        # One global file pool across all albums, with fair round-robin dispatch.
        # No album can multiply upload concurrency or flood an unbounded queue.
        def admit() -> None:
            nonlocal next_album
            while len(contexts) < self.album_workers and next_album < len(albums) and not self._stopping():
                album = albums[next_album]
                next_album += 1
                album_id = str(album["flickr_id"])
                contexts[album_id] = self._prepare_album(album)
                rotation.append(album_id)

        try:
            with ThreadPoolExecutor(max_workers=self.upload_workers, thread_name_prefix="gphotos-upload") as pool:
                try:
                    while contexts or futures or (next_album < len(albums) and not self._stopping()):
                        for album_id, ctx in list(contexts.items()):
                            progress = ctx["progress"]
                            exhausted = ctx["exhausted"] and not ctx["deferred"]
                            processed = progress.completed + progress.failed == progress.total
                            if not ctx["in_flight"] and (exhausted or processed):
                                status = "completed" if progress.completed == progress.total else "failed"
                                self.database.set_album_state(album_id, status)
                                self._emit(status.title(), album=progress)
                                totals["albums_completed"] += int(status == "completed")
                                contexts.pop(album_id)
                                rotation = deque(key for key in rotation if key != album_id)
                        admit()
                        self._fill_transfers(pool, contexts, futures, rotation)
                        if not futures:
                            if self._stopping():
                                break
                            continue
                        done, _ = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
                        for future in done:
                            ctx, photo = futures.pop(future)
                            ctx["in_flight"] -= 1
                            progress = ctx["progress"]
                            album_id = progress.album_id
                            flickr_id = str(photo["flickr_id"])
                            try:
                                uploaded = future.result()
                                with self._progress_lock:
                                    progress.completed += 1
                                    progress.uploaded += int(uploaded)
                                    progress.reused += int(not uploaded)
                                totals["uploaded" if uploaded else "reused"] += 1
                                consecutive_failures = 0
                                self._emit("Migrating", album=progress)
                            except UploadPaused:
                                paused = True
                            except Exception as error:
                                journal = self.database.upload_journal(flickr_id)
                                reconcile = bool(journal.get("create_started"))
                                self.database.set_member_state(album_id, flickr_id, "reconcile" if reconcile else "failed", str(error))
                                self.database.set_photo_error(flickr_id, str(error))
                                with self._progress_lock:
                                    progress.failed += 1
                                totals["reconcile_required" if reconcile else "failed"] += 1
                                LOG.exception("archive_item_failed", extra={"flickr_id": flickr_id, "album_id": album_id})
                                self._emit("Item failed", str(error), album=progress)
                                consecutive_failures += 1
                                if consecutive_failures >= 3:
                                    fatal_error = error
                                    self._abort.set()
                    if fatal_error:
                        raise RuntimeError("Three consecutive items failed. Retry after resolving the displayed error.") from fatal_error
                    if paused or self.stop.is_set():
                        raise UploadPaused("Migration paused")
                except BaseException:
                    self._abort.set()
                    raise
        except UploadPaused:
            totals["paused"] = 1
            for album_id, ctx in contexts.items():
                self.database.set_album_state(album_id, "paused")
                self._emit("Paused", album=ctx["progress"])
        except BaseException as error:
            for album_id, ctx in contexts.items():
                self.database.set_album_state(album_id, "failed", str(error))
                self._emit("Failed", str(error), album=ctx["progress"])
            raise

    def _fill_transfers(self, pool: ThreadPoolExecutor, contexts: dict, futures: dict, rotation: deque) -> None:
        misses = 0
        while len(futures) < self.upload_workers and rotation and not self._stopping():
            album_id = rotation.popleft()
            ctx = contexts[album_id]
            blocked_ids = {str(p["flickr_id"]) for _ctx, p in futures.values()}
            photo = None
            for _ in range(len(ctx["deferred"])):
                candidate = ctx["deferred"].popleft()
                if str(candidate["flickr_id"]) not in blocked_ids:
                    photo = candidate
                    break
                ctx["deferred"].append(candidate)
            while photo is None and not ctx["exhausted"]:
                candidate = next(ctx["pending"], None)
                if candidate is None:
                    ctx["exhausted"] = True
                    break
                if str(candidate["flickr_id"]) in blocked_ids:
                    ctx["deferred"].append(candidate)
                else:
                    photo = candidate
            if photo is None:
                if ctx["deferred"]:
                    rotation.append(album_id)
                misses += 1
                if misses >= max(1, len(rotation)):
                    break
                continue
            misses = 0
            rotation.append(album_id)
            ctx["in_flight"] += 1
            future = pool.submit(self._migrate_photo, ctx["progress"], ctx["google_id"], photo)
            futures[future] = (ctx, photo)

    def _migrate_photo(self, album: AlbumProgress, google_album_id: str, photo: dict) -> bool:
        album_id = album.album_id
        flickr_id = str(photo["flickr_id"])
        key = f"{album_id}:{flickr_id}"
        item = {"key": key, "album_id": album_id, "album_title": album.title,
                "flickr_id": flickr_id, "item": str(photo["title"] or photo["filename"] or flickr_id),
                "phase": "Queued", "bytes_done": 0, "bytes_total": 0, "message": ""}
        with self._progress_lock:
            self._active[key] = item
            photo_lock = self._photo_locks.setdefault(flickr_id, Lock())
        self._thread.item = item
        self._thread.album = album
        try:
            self._emit("Checking shared media")
            with self._acquire(photo_lock):
                # Another album may have finished this item while we waited.
                with self.database.connection() as conn:
                    photo = dict(conn.execute("SELECT * FROM flickr_photo WHERE flickr_id=?", (flickr_id,)).fetchone())
                media_id, uploaded, extracted = self._media_id(photo)
                self._emit("Adding to album")
                self._batcher.add(google_album_id, media_id,
                    lambda _result: self.database.set_member_state(album_id, flickr_id, "added"))
                if extracted:
                    self._cleanup(flickr_id, extracted)
                return uploaded
        finally:
            with self._progress_lock:
                self._active.pop(key, None)
            self._thread.item = None
            self._emit("Migrating")
            self._thread.album = None

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
        with self._progress_lock:
            checksum_lock = self._checksum_locks.setdefault(checksum, Lock())
        self._emit("Checking duplicates")
        with self._acquire(checksum_lock):
            return self._upload_unique(photo, path, managed, checksum, state)

    def _upload_unique(self, photo: dict, path: Path, managed: bool, checksum: str, state: dict) -> tuple[str, bool, Path | None]:
        flickr_id = str(photo["flickr_id"])
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
        self._set_bytes(0, path.stat().st_size, "Uploading")
        token = self.google.upload_bytes(path, state, save, self._byte_progress)
        self._emit("Creating media item")
        def prepare() -> None:
            state["create_started"] = True
            save(state)
            self.database.mark_uploading(flickr_id)
        def confirm(media_id: object) -> None:
            self.database.mark_uploaded(flickr_id, str(media_id))
            self.database.save_upload_journal(flickr_id, {})
        try:
            media_id = self._batcher.create({"token": token,
                "filename": path.name.split("_", 1)[-1] if managed else path.name,
                "description": str(photo["description"] or "")}, prepare, confirm)
        except MediaCreateRejected:
            state.pop("create_started", None)
            save(state)
            self.database.set_photo_error(flickr_id, "Google explicitly rejected this media", upload_failed=True)
            with self.database.connection() as conn:
                conn.execute("UPDATE flickr_photo SET upload_state='failed' WHERE flickr_id=?", (flickr_id,))
            raise
        return media_id, True, path if managed else None

    def _byte_progress(self, sent: int, total: int) -> None:
        self._set_bytes(sent, total, "Uploading")

    def _set_bytes(self, sent: int, total: int, phase: str, message: str = "") -> None:
        with self._progress_lock:
            item = getattr(self._thread, "item", None)
            if item is not None:
                item.update(bytes_done=sent, bytes_total=total)
            elif self.current:
                self.current.bytes_done, self.current.bytes_total = sent, total
            self._emit(phase, message)

    def _extract(self, photo: dict[str, object]) -> tuple[Path, bool]:
        self._emit("Waiting for extraction")
        # Only extraction is serialized: completed files upload concurrently.
        # This avoids racing disk-space checks and random reads of huge ZIPs.
        with self._acquire(self._extract_lock):
            return self._extract_locked(photo)

    def _extract_locked(self, photo: dict[str, object]) -> tuple[Path, bool]:
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
                        if self._stopping():
                            raise UploadPaused("Migration paused")
                        dst.write(block)
                        digest.update(block)
                        count += len(block)
                        if self.current and (time.monotonic() - last_update >= 0.25 or count == member.file_size):
                            self._set_bytes(count, member.file_size, "Extracting", name)
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
