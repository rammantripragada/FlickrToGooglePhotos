# Flickr to Google Photos — Local Web Dashboard

A local-only browser dashboard for the Flickr to Google Photos migration.

It listens only on `127.0.0.1`; no migration data, OAuth token, photo, or video is sent to a web server by this dashboard.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
flickr-gphotos-web
```

Open <http://127.0.0.1:8765>.

Set `MIGRATOR_DATABASE` if your SQLite journal is somewhere other than `../flickr-to-google-photos.sqlite3`.
