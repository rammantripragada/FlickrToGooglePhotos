"""OS credential-store access. SQLite intentionally never contains OAuth tokens."""

from __future__ import annotations

import json
from dataclasses import dataclass

import keyring

SERVICE_NAME = "flickr-to-google-photos"


@dataclass(frozen=True)
class FlickrToken:
    oauth_token: str
    oauth_token_secret: str
    user_nsid: str
    username: str | None = None


class CredentialStore:
    def save_flickr(self, token: FlickrToken) -> None:
        keyring.set_password(SERVICE_NAME, f"flickr:{token.user_nsid}", json.dumps(token.__dict__))
        keyring.set_password(SERVICE_NAME, "flickr:active-user", token.user_nsid)

    def load_flickr(self) -> FlickrToken | None:
        user_nsid = keyring.get_password(SERVICE_NAME, "flickr:active-user")
        if not user_nsid:
            return None
        raw = keyring.get_password(SERVICE_NAME, f"flickr:{user_nsid}")
        return FlickrToken(**json.loads(raw)) if raw else None
