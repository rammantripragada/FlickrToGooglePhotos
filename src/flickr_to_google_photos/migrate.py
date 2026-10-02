"""Explicit, crash-safe selected-album migration service."""
from __future__ import annotations
import logging
import random
import re
import shutil
import time
from pathlib import Path
import requests
from .database import MigrationDatabase
from .google import GooglePhotosClient
from .google_deduplication import GoogleDeduplicationGuard, GoogleUploadDecision
from .integrity import sha256_file

LOG = logging.getLogger(__name__)

class MigrationService:
    def __init__(self, database: MigrationDatabase, download_dir: Path, *, request_get=requests.get, sleep=time.sleep, max_download_retries: int = 8) -> None:
        self.database, self.download_dir = database, download_dir
        self._request_get, self._sleep, self.max_download_retries = request_get, sleep, max_download_retries
    def run(self) -> dict[str, int]:
        google, guard = GooglePhotosClient(), GoogleDeduplicationGuard(self.database)
        totals = {"albums": 0, "uploaded": 0, "skipped": 0, "reconcile_required": 0, "empty_or_uninventoried_albums": 0}
        for album in self.database.selected_albums():
            photos = self.database.album_photos(str(album["flickr_id"]))
            if not photos:
                totals["empty_or_uninventoried_albums"] += 1
                continue
            album_id = str(album["google_album_id"] or google.create_album(str(album["title"])))
            self.database.set_google_album_id(str(album["flickr_id"]), album_id); totals["albums"] += 1
            for photo in photos:
                decision = guard.decide(str(photo["flickr_id"]))
                if decision.decision is GoogleUploadDecision.SKIP_ALREADY_LINKED: totals["skipped"] += 1; continue
                if decision.decision is GoogleUploadDecision.RECONCILE_REQUIRED: totals["reconcile_required"] += 1; continue
                path = self._download(photo)
                self.database.mark_uploading(str(photo["flickr_id"]))
                self.database.mark_uploaded(str(photo["flickr_id"]), google.upload_and_create(path, album_id)); totals["uploaded"] += 1
        return totals
    def _download(self, photo: dict[str, object]) -> Path:
        if photo["local_path"] and Path(str(photo["local_path"])).is_file(): return Path(str(photo["local_path"]))
        if not photo["original_url"]: raise RuntimeError(f"No source URL for Flickr item {photo['flickr_id']}")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(photo["filename"] or photo["flickr_id"]))
        target = self.download_dir / f"{photo['flickr_id']}_{name}"; temporary = target.with_suffix(target.suffix + ".part")
        for attempt in range(self.max_download_retries + 1):
            try:
                with self._request_get(str(photo["original_url"]), stream=True, timeout=(15, 600)) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt >= self.max_download_retries:
                            response.raise_for_status()
                        self._wait_before_download_retry(response, attempt, str(photo["flickr_id"]))
                        continue
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        shutil.copyfileobj(response.raw, output)
                temporary.replace(target)
                self.database.set_local_file(str(photo["flickr_id"]), str(target), sha256_file(target))
                return target
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as error:
                if attempt >= self.max_download_retries:
                    raise RuntimeError(f"Download failed after {self.max_download_retries} retries for {photo['flickr_id']}") from error
                temporary.unlink(missing_ok=True)
                delay = min(300.0, 15.0 * (2**attempt)) + random.random()
                LOG.warning("flickr_download_retry", extra={"flickr_id": photo["flickr_id"], "delay_seconds": delay})
                self._sleep(delay)
        raise RuntimeError(f"Download retries exhausted for {photo['flickr_id']}")

    def _wait_before_download_retry(self, response: requests.Response, attempt: int, flickr_id: str) -> None:
        """Back off original-file requests without treating a 429 as fatal."""
        retry_after = response.headers.get("Retry-After")
        try:
            server_delay = float(retry_after) if retry_after is not None else 0.0
        except ValueError:
            server_delay = 0.0
        delay = max(server_delay, min(300.0, 60.0 * (2**attempt)))
        LOG.warning("flickr_download_retry", extra={"flickr_id": flickr_id, "delay_seconds": delay})
        self._sleep(delay)
