from flickr_to_google_photos.database import MigrationDatabase
from flickr_to_google_photos.flickr import FlickrPhoto
from flickr_to_google_photos.google_deduplication import GoogleDeduplicationGuard, GoogleUploadDecision


def _photo(photo_id: str) -> FlickrPhoto:
    return FlickrPhoto(photo_id, None, None, None, [], None, None, None, None, None, None, None, "photo", {"id": photo_id})


def test_google_guard_skips_known_media_and_blocks_ambiguous_request(tmp_path):
    database = MigrationDatabase(tmp_path / "state.sqlite3")
    database.initialize()
    database.upsert_account("account", "user", None)
    for flickr_id in ("new", "linked", "ambiguous"):
        database.upsert_photo("account", _photo(flickr_id))
    with database.connection() as conn:
        conn.execute("UPDATE flickr_photo SET google_media_id='google-1', upload_state='uploaded' WHERE flickr_id='linked'")
        conn.execute("UPDATE flickr_photo SET upload_state='uploading' WHERE flickr_id='ambiguous'")
    guard = GoogleDeduplicationGuard(database)
    assert guard.decide("new").decision is GoogleUploadDecision.CREATE
    assert guard.decide("linked").decision is GoogleUploadDecision.SKIP_ALREADY_LINKED
    assert guard.decide("ambiguous").decision is GoogleUploadDecision.RECONCILE_REQUIRED
