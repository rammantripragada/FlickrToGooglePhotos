# flickr-to-google-photos

`flickr-to-google-photos` is a safety-first, resumable migration application for a large Flickr library. It inventories Flickr into a durable local SQLite database before it ever downloads or uploads a byte.

**Current release includes archive migration.** Import Flickr Data metadata ZIPs and index photo/video ZIPs locally, select albums, then upload to Google Photos from the archives. The original Flickr OAuth inventory commands remain available.

## Archive workflow (recommended for large libraries)

Keep the metadata ZIPs in one directory. Media ZIPs may stay on several disks; no full extraction is required.

```zsh
cd ~/FlickrToGooglePhotos
source .venv/bin/activate
flickr-gphotos import-archive ~/Downloads/FlickrMetadata
flickr-gphotos index-archive-media '/Volumes/T7/FlickrData' '/Volumes/T7 1/FlickrData'
flickr-gphotos archive-gui
```

In **Albums**, check each album to migrate. The **Migration** tab shows selected albums, local indexed media, confirmed Google album membership, item counts and percentages. Authorize Google Photos there, choose a working directory with enough space for concurrent staged files, and click **Check local readiness**. Set **Parallel uploads (total)**, **Parallel albums**, and **Google batch limit**, then click **Migrate selected albums / Resume**. **Pause** preserves progress; current network requests may take time to finish before stopping.

The tab includes separate progress for each active album and a live photo/video transfer table with byte counts, percentage and status. It also shows retries, errors, and a link to the actual Google album. Albums shows persistent Google status and completed membership counts after restarting the app. Metadata imports preserve those membership states.

CLI equivalents:

```zsh
flickr-gphotos migrate-archive --dry-run
flickr-gphotos auth-google
flickr-gphotos migrate-archive --workers 4 --album-workers 2 --batch-size 50 --work-dir '/path/with/free/space/FlickrWork'
```

The dry run checks local metadata and archive availability without Google requests or extraction. Migration refuses to start if selected albums have missing source members or incomplete metadata. It uses local ZIP entries exclusively; it never falls back to a Flickr URL.

Defaults are **4 total file workers**, **2 active albums**, and a **50-item Google write batch limit**. File and album limits accept 1–8, while batch limits accept 1–50. The global transfer limit is shared across albums: 4 workers and 2 albums means at most 4 file jobs, not 8. Configure defaults with `MIGRATOR_UPLOAD_WORKERS`, `MIGRATOR_ALBUM_WORKERS`, and `MIGRATOR_GOOGLE_BATCH_SIZE` in `.env`, or override them using the CLI/GUI. Albums share the pool with round-robin scheduling; shared Flickr IDs are deferred instead of occupying multiple transfer slots.

### Space, deduplication and recovery

Only one media item is extracted at a time to avoid disk contention; completed extractions upload in parallel. There are at most as many active staged jobs as global file workers, plus any retained failed/paused copies. Extraction reads the complete ZIP member to check its CRC and calculates SHA-256. The original bytes and embedded EXIF are preserved. **Each successful working copy is removed immediately after its Google media ID and album membership are confirmed and saved—not at the end of the album.** Source ZIPs and existing download files outside the working directory are never deleted. Failed or paused working copies may remain for resumption. Keep the ZIP disks connected while migrating. For a conservative working-space allowance, reserve the worker count times the largest selected file size, plus a safety margin. Extraction checks remaining free space before writing each file and refuses insufficient space.

Exact SHA-256 matches reuse a confirmed Google media ID, even for different Flickr IDs. Per-source and per-checksum locks prevent two concurrent albums from uploading identical media twice. One photo/video can be added to multiple Google albums without uploading another copy. The migration journal tracks Google album membership separately from upload state. Deduplication cannot checksum the user's entire pre-existing Google Photos library; it covers known uploads managed by this tool.

Google byte transfers use resumable sessions and separate HTTP sessions for each worker. Session URLs, upload tokens and content checksums are journaled in the ignored SQLite database; OAuth credentials remain in Keychain. After interruption, Google is queried for its received byte offset. **File bytes must be transferred individually; Google media creation and album membership requests are batched.** A short 150 ms collection window groups ready items, capped by the batch limit; actual batches are bounded by ready workers, so 4 workers usually yield up to 4 items rather than staging 50 files. One account-wide writer serializes these calls with album creation. Successful and rejected items in partial batch responses are tracked separately. A Google 429 sets a shared cooldown respected by all workers. API batches stay within 50 items; an album cannot exceed 20,000 members. Photos are limited to 200 MB, videos to 20 GB. See [Google uploads](https://developers.google.com/photos/library/guides/upload-media) and [resumable uploads](https://developers.google.com/photos/library/guides/resumable-uploads).

The app stores creation intent before making a Google create request. A saved, unexpired upload token can be reused for the same bytes. If an ambiguous media-create token has expired, the item is blocked for reconciliation to avoid blindly creating a duplicate. An interrupted album-create response is also blocked for reconciliation; do not clear state or rerun a different database to bypass it. Three consecutive item failures stop migration so disk, authorization or API errors do not cause thousands of repeated failures.

The app reuses its saved Google album ID when resuming; existing linked destinations are not renamed. New destinations use `FlickrTitle_flickr`, then `FlickrTitle_flickr_1`, `_flickr_2`, etc. if an accessible album already has that name; an unlinked same-name album is never silently reused. The app checks all pages of app-created Google albums once per migration and also reserves names for albums it creates during that run. Google's API cannot see manually-created/other-app albums under these scopes, so it cannot guarantee unique names across that invisible part of the library. User-written media descriptions are sent (up to Google's 1,000-character limit). Flickr album descriptions, tags, GPS and dates remain in SQLite/raw archive JSON; embedded EXIF is retained in the uploaded bytes. The Google API does not provide equivalent writable fields for every Flickr attribute.

## Safety model

- `inventory`, `status`, and `report` never modify Flickr or Google Photos.
- No command deletes remote media. Destructive operations will not be added.
- OAuth access tokens live in the macOS Keychain through `keyring`; neither SQLite nor Git contains them.
- `.env`, local databases, downloads, logs, and credentials are ignored by Git.
- SQLite uses WAL mode, foreign keys, short transactions, and idempotent upserts. Re-running inventory resumes/refreshes known rows instead of duplicating them.
- Download and upload states advance only after verification or a confirmed Google response. An interrupted operation is resumed or blocked for reconciliation rather than assumed successful.

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

## Desktop GUI

Launch the native desktop interface with:

```bash
flickr-gphotos gui
# or
flickr-gphotos-gui
```

The original GUI provides Flickr authorization and inventory controls. The `archive-gui` interface uses local metadata and ZIPs and includes Google authorization and migration controls. Both have album checkboxes, status, and duplicate reports; no remote delete controls are implemented.

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

By default, the detailed Flickr calls run concurrently with 5 workers. Increase cautiously for a fast connection, while respecting Flickr rate limits:

```bash
flickr-gphotos inventory --workers 8
```

The desktop GUI offers the same **Parallel Flickr requests** control (1–12 workers).

When you click **Migrate approved albums**, the GUI first refreshes only the approved albums’ Flickr members, then creates/reuses their Google Photos albums and transfers their media. A full-library inventory is not required.

An inventory can be stopped and run again. Existing source records update their Flickr metadata while retaining progress fields such as checksums, verified-download state, and future Google IDs.

Album names are discovered first and appear in the GUI's **Albums** tab while the larger parallel photo/video scan continues. You can review and approve album names for the future Google sync without waiting for every media item to finish.

Click an Albums table heading to sort by Google-sync approval, Flickr ID, album name, or item count; click the same heading again to reverse it.

After a normal interactive inventory, the tool prints all discovered Flickr albums and asks which album IDs should migrate. Selection is stored locally and can be changed any time:

```bash
flickr-gphotos albums                 # list albums and select interactively
flickr-gphotos albums --all           # select every discovered album
flickr-gphotos albums --select 123 --select 456
```

For a scripted inventory, select explicitly with `--all-albums` or one or more `--album FLICKR_ID` flags. Discovery remains complete regardless of selection; the choice controls only the later Google album/media migration phase.

## Local database

The default `flickr-to-google-photos.sqlite3` contains these main tables:

- `flickr_account` — authenticated source account
- `flickr_photo` — normalized Flickr metadata, raw response, planned download/verification state, checksum and Google media ID fields
- `flickr_album` — Flickr photosets and a reserved Google album ID field
- `flickr_album_photo` — ordered many-to-many album membership

The migration tracks separate crash-recovery states:

```text
download: discovered → downloading → downloaded → verified
upload:   not_started → uploading → uploaded
membership: pending → added (or failed/reconcile)
```

The process never treats an item as verified or uploaded solely because it was attempted. `archive_media` stores ZIP paths and member names; `google_upload_journal` persists resumable transfer state. On restart, existing Google IDs are reused, retained local files are verified against their checksum, and Google transfer sessions are queried before advancing.

## Duplicate detection

Run this after inventory to identify a photo or video that Flickr places in more than one album:

```bash
flickr-gphotos duplicates
flickr-gphotos duplicates --json
```

This does **not** delete, move, or de-duplicate anything. It reports two distinct cases:

- `album_membership_duplicates`: one Flickr media ID belongs to multiple albums. This is normally intentional and the Google phase preserves album membership without another upload.
- `content_duplicates`: two or more distinct Flickr IDs have the same verified SHA-256 checksum. This report becomes available as migration verifies files; it detects byte-identical photos or videos without trusting filenames or metadata.

## Google Photos setup

Create a Google Cloud project, enable the **Photos Library API**, and configure OAuth according to the [Google Photos application setup](https://developers.google.com/photos/overview/configure-your-app). Create an OAuth **Desktop app** client and download its JSON to `credentials/google-client.json`, or set `GOOGLE_CLIENT_SECRETS_FILE` in `.env` to its path. Do not commit this file. When your consent configuration is in testing mode, your Google account must be approved as a test user.

Run `flickr-gphotos auth-google` or click **Authorize Google Photos** in Migration. The tool requests `photoslibrary.appendonly` for creation and `photoslibrary.readonly.appcreateddata` for checking its albums. Keep using the same Google account and OAuth client ID throughout migration. Resources created under one client ID cannot be managed using a different client ID. API uploads are original quality and count against Google account storage. Google Photos limits Library API management to media and albums created by this app; it cannot append to your manually-created albums. [Google’s current API update](https://developers.google.com/photos/support/updates)

### Google duplicate protection

Before any Google create request, archive migration checks the stored Flickr media ID, confirmed SHA-256 matches and the upload journal. An item left in ambiguous `uploading` state is resumed with its still-valid token or blocked for reconciliation instead of retried blindly. Albums store the destination ID so resuming does not generate another suffixed destination.

Google's current API can list only media created by this app, not the user’s whole pre-existing Google Photos library. Consequently, the application will not claim an exact duplicate match against pre-existing Google photos or videos. It will audit app-created items and report candidates, but will never delete Google content automatically.

## CLI reference

```text
flickr-gphotos auth-flickr [--callback-url URL] [--manual-verifier]
flickr-gphotos inventory [--database PATH] [--dry-run] [--no-photo-details] [--workers N]
                         [--all-albums | --album FLICKR_ID | --no-album-selection]
flickr-gphotos albums [--database PATH] [--all | --select FLICKR_ID]
flickr-gphotos gui
flickr-gphotos archive-gui
flickr-gphotos import-archive METADATA_DIRECTORY [--database PATH]
flickr-gphotos index-archive-media MEDIA_DIRECTORY [MEDIA_DIRECTORY ...] [--database PATH]
flickr-gphotos auth-google
flickr-gphotos migrate-archive [--database PATH] [--dry-run] [--work-dir PATH]
                              [--workers 1-8] [--album-workers 1-8] [--batch-size 1-50]
flickr-gphotos migrate [--database PATH]  # legacy direct-Flickr download workflow
flickr-gphotos status [--database PATH] [--json]
flickr-gphotos report [--database PATH] [--json]
flickr-gphotos duplicates [--database PATH] [--json]
```

Archive migration includes extraction, verification and uploading. Standalone `download`, `verify`, `upload`, and `retry-failed` commands are not provided; use **Migrate selected albums / Resume** to retry safe failures with the same database.

## Testing

Tests do not use real Flickr, Google, or OAuth credentials:

```bash
pytest -q
```

Tests cover Flickr metadata parsing and pagination, SQLite state preservation, archive filename matching, photo/video extraction, checksum deduplication across albums, persistent membership, pause/resume, ambiguous creation guards, mocked resumable Google uploads, paginated album-name checks and suffix collision handling. GUI progress handlers are tested without a display or credentials.

## Troubleshooting

**`No Flickr token in macOS keychain`** — run `flickr-gphotos auth-flickr` after setting the two Flickr API variables.

**OAuth callback mismatch** — make `FLICKR_OAUTH_CALLBACK` exactly match the callback configured in Flickr. Use `--manual-verifier` for a custom callback URL.

**Rate limiting or temporary Flickr errors** — the client retries safe read calls. Run `inventory` again after a prolonged outage; upserts make that safe.

**Interrupted inventory** — simply run `flickr-gphotos inventory` again. Discovery is idempotent and no remote content was changed.

**Missing local media** — connect every archive disk, refresh ZIP indexing, and check local readiness. Do not expand all archives. An indexed ZIP must remain accessible at its recorded path.

**Not enough working space** — choose a working folder on a disk with space for the largest selected file plus a safety margin. Only managed successful temporary copies are removed, never source ZIPs.

**Google authorization expired or access blocked** — authorize again using the original client and account. Check that the account is approved for testing if your consent configuration is still in testing mode. Migration progress stays in SQLite.

**Reconciliation required** — do not clear the database or create another migration database. An uncertain create request may already have succeeded. Confirm the destination album/media ID before changing its mapping; expired ambiguous creation state is intentionally not retried automatically.

**Application logs** — structured events and exception tracebacks are written to `logs/flickr-gphotos.jsonl` by default (`MIGRATOR_LOG_FILE` overrides this). Upload session URLs and tokens are not included in structured events; keep the SQLite database private because it contains transfer credentials.

**A photo has no original URL** — Flickr did not permit an original. The database stores Flickr’s largest returned source URL instead; review these before enabling the download phase.

## Development notes

The project uses a `src/` layout, modern `pyproject.toml` packaging, type hints, JSON structured logging, and separate configuration, credentials, Flickr, SQLite, inventory, and CLI layers.

## License

MIT. See [LICENSE](LICENSE).
