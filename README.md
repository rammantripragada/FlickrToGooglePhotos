# flickr-to-google-photos

`flickr-to-google-photos` is a safety-first, resumable migration application for a large Flickr library. It inventories Flickr into a durable local SQLite database before it ever downloads or uploads a byte.

**Current release: Phase 1 — Flickr inventory.** It supports read-only Flickr OAuth, identifies the authenticated account, enumerates all accessible photos and albums with pagination, retrieves detailed photo metadata and the largest permitted source URL, and records a restartable inventory. Downloading and Google Photos uploads are intentionally not implemented yet.

## Safety model

- `inventory`, `status`, and `report` never modify Flickr or Google Photos.
- No command deletes remote media. Destructive operations will not be added.
- OAuth access tokens live in the macOS Keychain through `keyring`; neither SQLite nor Git contains them.
- `.env`, local databases, downloads, logs, and credentials are ignored by Git.
- SQLite uses WAL mode, foreign keys, short transactions, and idempotent upserts. Re-running inventory resumes/refreshes known rows instead of duplicating them.
- Later download and upload phases will change a state only after the external side effect has been confirmed. An interrupted operation will be reconciled rather than assumed successful.

## Phase 1 capabilities

| Area | Included now |
| --- | --- |
| Flickr authentication | OAuth 1.0a with **read-only** permission |
| Account identity | `flickr.test.login` |
| Photo discovery | Paginated `flickr.people.getPhotos` plus per-photo `getInfo` and `getSizes` |
| Albums | Paginated photoset discovery and ordered membership inventory |
| Photos and videos | Both media types are inventoried; type is stored per Flickr item |
| Metadata | IDs, title, description, tags, dates, location/GPS when exposed, source format and raw Flickr API response |
| Recovery | SQLite upserts preserve future download/checksum/upload fields on rediscovery |
| Rate limits | HTTP 429/5xx, Flickr temporary error 105, connection, and timeout retries with exponential backoff and jitter |

Flickr exposes only the media and metadata that the authenticating account is permitted to access. “Original” URLs are requested with `flickr.photos.getSizes`; if an original is unavailable, Flickr’s largest available size is saved as the candidate source URL.

## macOS setup

Install Python 3.11 or newer (for example, `brew install python@3.13`) and create a virtual environment:

```bash
git clone https://github.com/rammantripragada/FlickrToGooglePhotos.git
cd FlickrToGooglePhotos
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

`keyring` uses macOS Keychain. The first authorization may cause macOS to ask whether Python may access the keychain; allow it for the migration tool.

## Flickr application and OAuth setup

1. Sign in to Flickr and create a non-commercial API application at [Flickr App Garden](https://www.flickr.com/services/apps/create/).
2. Record the API key and shared secret in your untracked `.env` file:

   ```dotenv
   FLICKR_API_KEY=...
   FLICKR_API_SECRET=...
   FLICKR_OAUTH_CALLBACK=http://127.0.0.1:8765/callback
   ```

3. Register `http://127.0.0.1:8765/callback` as the application callback in Flickr. The CLI starts a one-shot loopback receiver only during authorization. If you use another registered callback, call `auth-flickr --callback-url URL --manual-verifier` and paste the `oauth_verifier` query value after Flickr redirects.
4. Authorize the tool. It requests only `read` access:

   ```bash
   flickr-gphotos auth-flickr
   ```

   Open the displayed URL, approve access, and paste the verifier. The credential goes to Keychain, not `.env`.

## First run and workflow

Check that the source account is accessible, then perform the read-only inventory:

```bash
# Optional: calls Flickr but does not create or modify the local database.
flickr-gphotos inventory --dry-run

# Create/update the durable inventory.
flickr-gphotos inventory

# View local totals.
flickr-gphotos status
flickr-gphotos report --json
```

For a shorter diagnostic inventory that skips expensive per-item `getInfo` calls, use:

```bash
flickr-gphotos inventory --no-photo-details
```

An inventory can be stopped and run again. Existing source records update their Flickr metadata while retaining progress fields such as checksums, verified-download state, and future Google IDs.

## Local database

The default `flickr-to-google-photos.sqlite3` contains these main tables:

- `flickr_account` — authenticated source account
- `flickr_photo` — normalized Flickr metadata, raw response, planned download/verification state, checksum and Google media ID fields
- `flickr_album` — Flickr photosets and a reserved Google album ID field
- `flickr_album_photo` — ordered many-to-many album membership

The schema already reserves the following crash-recovery states for later phases:

```text
download: discovered → downloading → downloaded → verified
upload:   not_started → uploading → uploaded
```

The process never treats an item as verified or uploaded solely because it was attempted. On a future restart, stale in-progress state will be reconciled with the file checksum or destination API result before advancing.

## Duplicate detection

Run this after inventory to identify a photo or video that Flickr places in more than one album:

```bash
flickr-gphotos duplicates
flickr-gphotos duplicates --json
```

This does **not** delete, move, or de-duplicate anything. It reports two distinct cases:

- `album_membership_duplicates`: one Flickr media ID belongs to multiple albums. This is normally intentional and lets the later Google phase preserve album membership.
- `content_duplicates`: two or more distinct Flickr IDs have the same verified SHA-256 checksum. This report becomes available after the planned download/verify phase; it detects byte-identical photos or videos without trusting filenames or metadata.

## Google Photos plan (not yet enabled)

Before Phase 2, create a Google Cloud project and configure OAuth according to the [Google Photos Library API setup](https://developers.google.com/photos/library/guides/get-started). Google Photos now limits Library API management to media and albums created by the app, so this project will create corresponding new destination albums; it will not manage pre-existing Google Photos albums. [Google’s current API update](https://developers.google.com/photos/support/updates)

The planned upload layer will use `photoslibrary.appendonly`, Google’s two-step upload flow, serial `batchCreate` operations per user, API-sized batches (up to 50), and persisted Google media/album IDs. It will also explicitly reconcile the small ambiguous window where a process dies after a remote request succeeds but before SQLite records its response.

### Google duplicate protection

The upload layer has a non-destructive `GoogleDeduplicationGuard` ready for integration. Before any Google create request, it checks the migration journal: a Flickr item with an existing Google media ID is skipped, and an item left in `uploading` by a crash is blocked for reconciliation instead of retried blindly. This prevents the migration itself from creating duplicate Google items.

Google's current API can list only media created by this app, not the user’s whole pre-existing Google Photos library. Consequently, the application will not claim an exact duplicate match against pre-existing Google photos or videos. It will audit app-created items and report candidates, but will never delete Google content automatically.

## CLI reference (Phase 1)

```text
flickr-gphotos auth-flickr [--callback-url URL] [--manual-verifier]
flickr-gphotos inventory [--database PATH] [--dry-run] [--no-photo-details]
flickr-gphotos status [--database PATH] [--json]
flickr-gphotos report [--database PATH] [--json]
flickr-gphotos duplicates [--database PATH] [--json]
```

Planned, but intentionally unavailable until their safety tests are implemented: `download`, `verify`, `auth-google`, `upload`, `albums`, and `retry-failed`.

## Testing

Tests do not use real Flickr, Google, or OAuth credentials:

```bash
pytest -q
```

Phase 1 tests cover Flickr metadata parsing, multi-page photo discovery, SQLite idempotency/state preservation, album membership replacement, and repeated inventory. Google-client mock tests will arrive with the Google upload implementation in Phase 2, rather than testing an API client that does not exist yet.

## Troubleshooting

**`No Flickr token in macOS keychain`** — run `flickr-gphotos auth-flickr` after setting the two Flickr API variables.

**OAuth callback mismatch** — make `FLICKR_OAUTH_CALLBACK` exactly match the callback configured in Flickr. Use `--manual-verifier` for a custom callback URL.

**Rate limiting or temporary Flickr errors** — the client retries safe read calls. Run `inventory` again after a prolonged outage; upserts make that safe.

**Interrupted inventory** — simply run `flickr-gphotos inventory` again. Discovery is idempotent and no remote content was changed.

**A photo has no original URL** — Flickr did not permit an original. The database stores Flickr’s largest returned source URL instead; review these before enabling the download phase.

## Development notes

The project uses a `src/` layout, modern `pyproject.toml` packaging, type hints, JSON structured logging, and separate configuration, credentials, Flickr, SQLite, inventory, and CLI layers.

## License

MIT. See [LICENSE](LICENSE).
