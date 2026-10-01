from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DEFAULT_DB = Path(__file__).resolve().parents[3] / "flickr-to-google-photos.sqlite3"
DATABASE = Path(os.getenv("MIGRATOR_DATABASE", DEFAULT_DB))
app = FastAPI(title="Flickr → Google Photos", docs_url=None, redoc_url=None)


def query(sql: str, params: tuple = ()) -> list[dict]:
    if not DATABASE.exists():
        return []
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


class Selection(BaseModel):
    album_ids: list[str]


@app.get("/api/status")
def status() -> dict:
    rows = query("SELECT 'photos' AS key, count(*) AS value FROM flickr_photo UNION ALL SELECT 'albums', count(*) FROM flickr_album UNION ALL SELECT 'selected albums', count(*) FROM flickr_album WHERE selected_for_migration=1 UNION ALL SELECT 'album memberships', count(*) FROM flickr_album_photo")
    return {row["key"]: row["value"] for row in rows}


@app.get("/api/albums")
def albums() -> list[dict]:
    return query("SELECT flickr_id, title, photo_count, selected_for_migration FROM flickr_album ORDER BY title COLLATE NOCASE")


@app.post("/api/albums/selection")
def select_albums(selection: Selection) -> dict:
    if not DATABASE.exists():
        raise HTTPException(400, "Migration database has not been created yet.")
    connection = sqlite3.connect(DATABASE)
    try:
        known = {row[0] for row in connection.execute("SELECT flickr_id FROM flickr_album")}
        if unknown := set(selection.album_ids) - known:
            raise HTTPException(400, f"Unknown album IDs: {', '.join(sorted(unknown))}")
        connection.execute("UPDATE flickr_album SET selected_for_migration=0")
        connection.executemany("UPDATE flickr_album SET selected_for_migration=1 WHERE flickr_id=?", [(item,) for item in selection.album_ids])
        connection.commit()
    finally:
        connection.close()
    return {"selected": len(selection.album_ids)}


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return """<!doctype html><html><head><title>Flickr → Google Photos</title><style>body{font:16px system-ui;margin:0;background:#0d1526;color:#edf2ff}main{max-width:1100px;margin:auto;padding:32px}h1{margin:0 0 8px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.card,table{background:#17223c;border:1px solid #294064;border-radius:12px}.card{padding:16px}.n{font-size:28px;font-weight:700}table{width:100%;border-collapse:separate;border-spacing:0;overflow:hidden}th,td{padding:12px;text-align:left;border-bottom:1px solid #294064}button{background:#4f8cff;color:white;border:0;border-radius:8px;padding:10px 14px;font-weight:600}small{color:#adbedc}</style></head><body><main><h1>Flickr → Google Photos</h1><small>Local dashboard · migration data stays on this Mac</small><section class=grid id=status></section><button onclick=save()>Save approved albums</button><table><thead><tr><th>Sync</th><th>Album name</th><th>Items</th><th>Flickr ID</th></tr></thead><tbody id=albums></tbody></table></main><script>let albums=[];async function load(){let s=await fetch('/api/status').then(r=>r.json());document.querySelector('#status').innerHTML=Object.entries(s).map(([k,v])=>`<div class=card><small>${k}</small><div class=n>${v}</div></div>`).join('');albums=await fetch('/api/albums').then(r=>r.json());document.querySelector('#albums').innerHTML=albums.map(a=>`<tr><td><input type=checkbox data-id="${a.flickr_id}" ${a.selected_for_migration?'checked':''}></td><td>${a.title}</td><td>${a.photo_count||0}</td><td><small>${a.flickr_id}</small></td></tr>`).join('')}async function save(){let album_ids=[...document.querySelectorAll('input:checked')].map(x=>x.dataset.id);await fetch('/api/albums/selection',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({album_ids})});await load()}load()</script></body></html>"""


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
