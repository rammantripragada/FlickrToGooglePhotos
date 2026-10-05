"""Bounded Google write batching; workers wait for durable confirmation."""
from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread

from .google_upload import UploadPaused


@dataclass
class _Job:
    kind: str
    payload: dict
    prepare: Callable[[], None]
    confirm: Callable[[object], None]
    future: Future = field(default_factory=Future)


class GoogleWriteBatcher:
    """One writer for the account; queue size is bounded by waiting workers."""

    def __init__(self, google, acquire_write, should_stop, *, batch_size: int = 50,
                 wait_seconds: float = 0.15, max_pending: int = 8):
        self.google, self.acquire_write, self.should_stop = google, acquire_write, should_stop
        self.batch_size, self.wait_seconds = batch_size, wait_seconds
        self.queue: Queue[_Job | None] = Queue(maxsize=max_pending)
        self.thread = Thread(target=self._run, name="gphotos-batch-writer", daemon=True)
        self.thread.start()
        self.closed = False

    def create(self, item: dict, prepare: Callable[[], None], confirm: Callable[[object], None]) -> str:
        job = _Job("create", item, prepare, confirm)
        self.queue.put(job)
        return str(job.future.result())

    def add(self, album_id: str, media_id: str, confirm: Callable[[object], None]) -> None:
        job = _Job("add", {"album_id": album_id, "media_id": media_id}, lambda: None, confirm)
        self.queue.put(job)
        job.future.result()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put(None)
            self.thread.join()

    @staticmethod
    def _resolve(job: _Job, result) -> None:
        try:
            if isinstance(result, Exception):
                raise result
            job.confirm(result)
            job.future.set_result(result)
        except Exception as error:
            job.future.set_exception(error)

    def _run(self) -> None:
        while True:
            first = self.queue.get()
            if first is None:
                return
            jobs = [first]
            deadline = time.monotonic() + self.wait_seconds
            closing = False
            while len(jobs) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self.queue.get(timeout=remaining)
                except Empty:
                    break
                if item is None:
                    closing = True
                    break
                jobs.append(item)
            try:
                if self.should_stop():
                    raise UploadPaused("Migration paused")
                with self.acquire_write():
                    self._flush(jobs)
            except Exception as error:
                for job in jobs:
                    if not job.future.done():
                        job.future.set_exception(error)
            if closing:
                return

    def _flush(self, jobs: list[_Job]) -> None:
        creates = []
        albums = defaultdict(list)
        for job in jobs:
            if job.kind == "add":
                albums[job.payload["album_id"]].append(job)
            else:
                try:
                    job.prepare()  # Commit create intent BEFORE the HTTP call.
                    creates.append(job)
                except Exception as error:
                    job.future.set_exception(error)
        if creates:
            try:
                results = self.google.create_media_batch([job.payload for job in creates])
                if len(results) != len(creates):
                    raise RuntimeError("Incomplete Google batch response; reconciliation required")
            except Exception as error:
                results = [error] * len(creates)
            for job, result in zip(creates, results):
                self._resolve(job, result)
        for album_id, members in albums.items():
            try:
                self.google.add_media(album_id, [job.payload["media_id"] for job in members])
                result = None
            except Exception as error:
                result = error
            for job in members:
                self._resolve(job, result)
