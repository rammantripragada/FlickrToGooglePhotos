"""Non-destructive guard used before any Google Photos create request.

The Google Photos Library API exposes only media created by this app.  The
migration journal is therefore the authoritative exact-deduplication index for
uploads this application performs; no filename or timestamp heuristics are
mistaken for a content match.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .database import MigrationDatabase


class GoogleUploadDecision(StrEnum):
    CREATE = "create"
    SKIP_ALREADY_LINKED = "skip_already_linked"
    RECONCILE_REQUIRED = "reconcile_required"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class GoogleDeduplicationResult:
    flickr_id: str
    decision: GoogleUploadDecision
    google_media_id: str | None
    reason: str


class GoogleDeduplicationGuard:
    """Makes duplicate Google creates impossible for confirmed migration rows.

    An upload process must call this immediately before a ``batchCreate``.  It
    never deletes or changes remote media.  A prior process that died while an
    API request was in flight is deliberately blocked for reconciliation,
    rather than blindly retrying and potentially creating a duplicate.
    """

    def __init__(self, database: MigrationDatabase) -> None:
        self.database = database

    def decide(self, flickr_id: str) -> GoogleDeduplicationResult:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT google_media_id, upload_state FROM flickr_photo WHERE flickr_id=?", (flickr_id,)
            ).fetchone()
        if row is None:
            return GoogleDeduplicationResult(flickr_id, GoogleUploadDecision.NOT_FOUND, None, "Flickr item is not inventoried")
        if row["google_media_id"] or row["upload_state"] == "uploaded":
            return GoogleDeduplicationResult(
                flickr_id, GoogleUploadDecision.SKIP_ALREADY_LINKED, row["google_media_id"],
                "A confirmed Google Photos media ID is already recorded",
            )
        if row["upload_state"] == "uploading":
            return GoogleDeduplicationResult(
                flickr_id, GoogleUploadDecision.RECONCILE_REQUIRED, None,
                "A prior Google create may have completed; reconcile before retrying",
            )
        return GoogleDeduplicationResult(flickr_id, GoogleUploadDecision.CREATE, None, "No Google create has been recorded")

    def linked_media_collisions(self) -> list[dict[str, object]]:
        """Detect data anomalies where distinct Flickr IDs point at one Google item."""
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT google_media_id, COUNT(*) AS flickr_item_count,
                   GROUP_CONCAT(flickr_id, ',') AS flickr_ids
                   FROM flickr_photo WHERE google_media_id IS NOT NULL
                   GROUP BY google_media_id HAVING COUNT(*) > 1"""
            ).fetchall()
        return [dict(row) for row in rows]
