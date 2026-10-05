"""Resumable uploads and append-only album membership for archive migration."""
from __future__ import annotations

import logging
import mimetypes
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from .google import BASE, GooglePhotosClient, access_token

LOG = logging.getLogger(__name__)


class UploadPaused(RuntimeError):
    """The user requested a pause; journaled transfer state is retained."""


class MediaCreateRejected(RuntimeError):
    """Google explicitly rejected creation; a later attempt is unambiguous."""


class ArchiveGoogleClient(GooglePhotosClient):
    def __init__(self, session=None, token_provider=access_token, sleep=time.sleep):
        self.session = session or requests.Session()
        self.token_provider, self.sleep = token_provider, sleep
        self.on_status: Callable[[str], None] = lambda _text: None
        self.should_stop: Callable[[], bool] = lambda: False

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_provider()}"}

    def check_authorization(self) -> None:
        self.headers()

    def _wait(self, seconds: float) -> None:
        while seconds > 0:
            if self.should_stop():
                raise UploadPaused("Migration paused")
            step = min(seconds, 1.0)
            self.sleep(step)
            seconds -= step

    def _delay(self, response, attempt: int) -> None:
        minimum = 30.0 if response is not None and response.status_code == 429 else 2.0
        delay = min(300.0, minimum * 2**attempt)
        value = response.headers.get("Retry-After") if response is not None else None
        if value:
            try:
                delay = max(delay, float(value))
            except ValueError:
                try:
                    delay = max(delay, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError):
                    pass
        self.on_status(f"Google retry: waiting {delay:.0f}s")
        LOG.warning("google_retry", extra={"delay_seconds": delay,
                    "http_status": response.status_code if response is not None else None})
        self._wait(delay)

    def _post(self, url: str, *, retry: bool = True, **kwargs):
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(6):
            if self.should_stop():
                raise UploadPaused("Migration paused")
            try:
                response = self.session.post(url, headers={**self.headers(), **extra_headers}, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if not retry or attempt == 5:
                    raise RuntimeError("Google connection failed; saved migration state is retained.") from None
                self._delay(None, attempt)
                continue
            if (response.status_code == 429 or (retry and response.status_code >= 500)) and attempt < 5:
                self._delay(response, attempt)
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Google request failed (HTTP {response.status_code}). Check authorization/storage and retry.")
            return response
        raise RuntimeError("Google retries exhausted")

    def create_album_details(self, title: str) -> dict:
        # Do not repeat ambiguous album creation after connection failure.
        return self._post(f"{BASE}/albums", retry=False, json={"album": {"title": title[:500]}}, timeout=60).json()

    def album_details(self, album_id: str) -> dict:
        r = self.session.get(f"{BASE}/albums/{album_id}", headers=self.headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    def list_albums(self) -> list[dict]:
        """Only app-created albums are visible under Google's current scopes."""
        albums = []
        page = None
        while True:
            params = {"pageSize": 50}
            if page:
                params["pageToken"] = page
            for attempt in range(6):
                if self.should_stop():
                    raise UploadPaused("Migration paused")
                try:
                    r = self.session.get(f"{BASE}/albums", headers=self.headers(), params=params, timeout=60)
                except (requests.ConnectionError, requests.Timeout):
                    if attempt == 5:
                        raise RuntimeError("Could not read Google albums. Check connection and retry.") from None
                    self._delay(None, attempt)
                    continue
                if (r.status_code == 429 or r.status_code >= 500) and attempt < 5:
                    self._delay(r, attempt)
                    continue
                r.raise_for_status()
                break
            body = r.json()
            albums.extend(body.get("albums", []))
            page = body.get("nextPageToken")
            if not page:
                break
        return albums

    def find_album(self, title: str) -> dict | None:
        matches = [album for album in self.list_albums()
                   if album.get("title") == title[:500] and album.get("isWriteable", True)]
        if len(matches) > 1:
            raise RuntimeError(f"Several accessible Google albums are named {title!r}. Link the intended Google album ID before migrating.")
        return matches[0] if matches else None

    def add_media(self, album_id: str, media_ids: list[str]) -> None:
        for offset in range(0, len(media_ids), 50):
            self._post(f"{BASE}/albums/{album_id}:batchAddMediaItems",
                       json={"mediaItemIds": media_ids[offset:offset + 50]}, timeout=60)

    @staticmethod
    def _check_session_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "photoslibrary.googleapis.com":
            raise RuntimeError("Invalid Google upload session URL")

    def upload_bytes(self, path: Path, state: dict, save: Callable[[dict], None],
                     progress: Callable[[int, int], None] = lambda _sent, _total: None) -> str:
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if size == 0:
            raise RuntimeError("Empty media file")
        if mime.startswith("image/") and size > 200 * 1024**2:
            raise RuntimeError("Photo exceeds Google's 200 MB upload limit")
        if size > 20 * 1024**3:
            raise RuntimeError("Media exceeds Google's 20 GB upload limit")
        if state.get("token") and time.time() - state.get("token_time", 0) < 23 * 3600:
            return str(state["token"])
        url = state.get("session_url")
        offset = 0
        if url:
            self._check_session_url(str(url))
            try:
                r = self._post(url, headers={"X-Goog-Upload-Command": "query"}, data=b"", timeout=60)
                if r.headers.get("X-Goog-Upload-Status") == "active":
                    offset = int(r.headers.get("X-Goog-Upload-Size-Received", "0"))
                else:
                    url = None
            except UploadPaused:
                raise
            except RuntimeError:
                url = None
        if not url:
            r = self._post(f"{BASE}/uploads", headers={
                "Content-Type": "application/octet-stream", "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Content-Type": mime,
                "X-Goog-Upload-Raw-Size": str(size)}, data=b"", timeout=60)
            url = r.headers["X-Goog-Upload-URL"]
            self._check_session_url(url)
            state.clear()
            state.update(session_url=url, granularity=int(r.headers.get("X-Goog-Upload-Chunk-Granularity", "262144")))
            save(state)
            offset = 0
        granularity = int(state.get("granularity", 262144))
        if granularity < 1 or not 0 <= offset <= size:
            raise RuntimeError("Invalid Google upload offset or chunk size")
        chunk_size = max(granularity, (8 * 1024**2 // granularity) * granularity)
        failures = 0
        with path.open("rb") as stream:
            while offset < size:
                if self.should_stop():
                    raise UploadPaused("Migration paused")
                stream.seek(offset)
                data = stream.read(min(chunk_size, size - offset))
                final = offset + len(data) == size
                try:
                    r = self.session.post(url, headers={**self.headers(), "X-Goog-Upload-Command": "upload, finalize" if final else "upload",
                        "X-Goog-Upload-Offset": str(offset), "Content-Type": "application/octet-stream"}, data=data, timeout=(15, 600))
                except (requests.ConnectionError, requests.Timeout):
                    r = None
                if r is None or r.status_code == 429 or r.status_code >= 500:
                    if failures >= 5:
                        raise RuntimeError("Google byte upload retries exhausted; resume the saved session later")
                    self._delay(r, failures)
                    failures += 1
                    query = self._post(url, headers={"X-Goog-Upload-Command": "query"}, data=b"", timeout=60)
                    if query.headers.get("X-Goog-Upload-Status") != "active":
                        state.clear(); save(state)
                        raise RuntimeError("Google transfer ended without a token; retry to start a fresh transfer")
                    offset = int(query.headers.get("X-Goog-Upload-Size-Received", "0"))
                    if not 0 <= offset <= size:
                        raise RuntimeError("Invalid Google upload offset")
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"Google byte upload failed (HTTP {r.status_code})")
                offset += len(data)
                failures = 0
                progress(offset, size)
                if final:
                    if not r.text.strip():
                        raise RuntimeError("Google did not return an upload token")
                    state.update(token=r.text.strip(), token_time=time.time())
                    save(state)
                    return str(state["token"])
        state.clear(); save(state)
        raise RuntimeError("Transfer has no token; retry to restart the byte transfer")

    def create_media(self, token: str, filename: str, description: str = "") -> str:
        item = {"simpleMediaItem": {"uploadToken": token, "fileName": filename}}
        if description:
            item["description"] = description[:1000]
        r = self._post(f"{BASE}/mediaItems:batchCreate", json={"newMediaItems": [item]}, timeout=60)
        results = r.json().get("newMediaItemResults", [])
        if len(results) != 1:
            raise RuntimeError("Google returned an incomplete media-creation result")
        result = results[0]
        if not result.get("mediaItem", {}).get("id"):
            status = result.get("status", {})
            if status.get("code", 0) != 0:
                raise MediaCreateRejected(str(status))
            raise RuntimeError("Google did not confirm a media ID; creation requires reconciliation")
        return str(result["mediaItem"]["id"])
