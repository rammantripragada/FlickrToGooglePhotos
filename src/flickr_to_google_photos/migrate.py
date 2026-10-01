"""Explicit, crash-safe selected-album migration service."""
from __future__ import annotations
import re, shutil
from pathlib import Path
import requests
from .database import MigrationDatabase
from .google import GooglePhotosClient
from .google_deduplication import GoogleDeduplicationGuard, GoogleUploadDecision
from .integrity import sha256_file

class MigrationService:
    def __init__(self, database: MigrationDatabase, download_dir: Path) -> None:
        self.database, self.download_dir = database, download_dir
    def run(self) -> dict[str, int]:
        google, guard = GooglePhotosClient(), GoogleDeduplicationGuard(self.database)
        totals = {"albums": 0, "uploaded": 0, "skipped": 0, "reconcile_required": 0}
        for album in self.database.selected_albums():
            album_id = str(album["google_album_id"] or google.create_album(str(album["title"])))
            self.database.set_google_album_id(str(album["flickr_id"]), album_id); totals["albums"] += 1
            for photo in self.database.album_photos(str(album["flickr_id"])):
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
        with requests.get(str(photo["original_url"]), stream=True, timeout=(15, 600)) as response:
            response.raise_for_status()
            with temporary.open("wb") as output: shutil.copyfileobj(response.raw, output)
        temporary.replace(target); self.database.set_local_file(str(photo["flickr_id"]), str(target), sha256_file(target)); return target
