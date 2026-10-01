"""Google Photos OAuth and append-only REST operations."""
from __future__ import annotations
import json, mimetypes
from pathlib import Path
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from .credentials import CredentialStore

SCOPES=["https://www.googleapis.com/auth/photoslibrary.appendonly","https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"]
BASE="https://photoslibrary.googleapis.com/v1"
def authorize(path: Path) -> None:
    if not path.is_file(): raise RuntimeError(f"Google client file not found: {path}")
    CredentialStore().save_google(InstalledAppFlow.from_client_secrets_file(str(path), SCOPES).run_local_server(port=0).to_json())
def access_token() -> str:
    raw=CredentialStore().load_google()
    if not raw: raise RuntimeError("Google is not authorized. Run auth-google first.")
    creds=Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid: creds.refresh(Request()); CredentialStore().save_google(creds.to_json())
    return str(creds.token)
class GooglePhotosClient:
 def __init__(self): self.session=requests.Session()
 def headers(self): return {"Authorization":f"Bearer {access_token()}"}
 def create_album(self,title:str)->str:
  r=self.session.post(f"{BASE}/albums",headers={**self.headers(),"Content-Type":"application/json"},json={"album":{"title":title}},timeout=60); r.raise_for_status(); return r.json()["id"]
 def upload_and_create(self,path:Path,album_id:str)->str:
  mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
  with path.open("rb") as f: r=self.session.post(f"{BASE}/uploads",headers={**self.headers(),"Content-Type":"application/octet-stream","X-Goog-Upload-Content-Type":mime,"X-Goog-Upload-Protocol":"raw"},data=f,timeout=600)
  r.raise_for_status(); r=self.session.post(f"{BASE}/mediaItems:batchCreate",headers={**self.headers(),"Content-Type":"application/json"},json={"albumId":album_id,"newMediaItems":[{"simpleMediaItem":{"uploadToken":r.text,"fileName":path.name}}]},timeout=60); r.raise_for_status(); item=r.json()["newMediaItemResults"][0]
  if "mediaItem" not in item: raise RuntimeError(str(item))
  return item["mediaItem"]["id"]
