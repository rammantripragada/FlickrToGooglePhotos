"""Flickr OAuth 1.0a and REST client with retry-aware pagination."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests
from requests import Response
from requests_oauthlib import OAuth1Session

from .credentials import FlickrToken

LOG = logging.getLogger(__name__)
REST_URL = "https://www.flickr.com/services/rest"
REQUEST_TOKEN_URL = "https://www.flickr.com/services/oauth/request_token"
AUTHORIZE_URL = "https://www.flickr.com/services/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.flickr.com/services/oauth/access_token"


class FlickrError(RuntimeError):
    pass


class FlickrAuthenticationError(FlickrError):
    pass


@dataclass(frozen=True)
class FlickrAccount:
    nsid: str
    username: str | None
    realname: str | None


@dataclass(frozen=True)
class FlickrPhoto:
    id: str
    filename: str | None
    title: str | None
    description: str | None
    tags: list[str]
    date_taken: str | None
    date_uploaded: str | None
    latitude: float | None
    longitude: float | None
    accuracy: int | None
    original_url: str | None
    original_format: str | None
    media_type: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class FlickrAlbum:
    id: str
    title: str
    description: str | None
    photo_count: int | None
    raw: dict[str, Any]


def _content(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("_content")
    return str(value) if value not in (None, "") else None


def parse_photo(raw: dict[str, Any], original_url: str | None = None) -> FlickrPhoto:
    """Normalize both `people.getPhotos` and `photos.getInfo` response shapes."""
    location = raw.get("location") or {}
    tags = raw.get("tags", {}).get("tag", [])
    if isinstance(tags, str):
        tags = tags.split()
    normalized_tags = [str(tag.get("raw") or tag.get("_content") or "") for tag in tags] if isinstance(tags, list) else []
    dates = raw.get("dates") or {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    return FlickrPhoto(
        id=str(raw["id"]),
        filename=raw.get("original") or raw.get("filename") or _content(raw.get("title")),
        title=_content(raw.get("title")),
        description=_content(raw.get("description")),
        tags=[tag for tag in normalized_tags if tag],
        date_taken=raw.get("datetaken") or dates.get("taken"),
        date_uploaded=str(raw["dateupload"]) if raw.get("dateupload") else dates.get("posted"),
        latitude=float(latitude) if latitude not in (None, "", "0") else None,
        longitude=float(longitude) if longitude not in (None, "", "0") else None,
        accuracy=int(location["accuracy"]) if location.get("accuracy") else None,
        original_url=original_url or raw.get("url_o"),
        original_format=raw.get("originalformat") or raw.get("original_format"),
        media_type=raw.get("media"),
        raw=raw,
    )


def parse_album(raw: dict[str, Any]) -> FlickrAlbum:
    return FlickrAlbum(
        id=str(raw["id"]),
        title=_content(raw.get("title")) or "Untitled Flickr album",
        description=_content(raw.get("description")),
        photo_count=int(raw["photos"]) if raw.get("photos") not in (None, "") else None,
        raw=raw,
    )


class FlickrClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        token: FlickrToken | None = None,
        session: OAuth1Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session or OAuth1Session(
            api_key,
            client_secret=api_secret,
            resource_owner_key=token.oauth_token if token else None,
            resource_owner_secret=token.oauth_token_secret if token else None,
        )
        self.sleep = sleep
        self.max_retries = max_retries

    def authorization_url(self, callback_uri: str) -> tuple[str, str]:
        temporary = OAuth1Session(self.api_key, client_secret=self.api_secret, callback_uri=callback_uri)
        try:
            request_token = temporary.fetch_request_token(REQUEST_TOKEN_URL)
        except requests.RequestException as error:
            raise FlickrAuthenticationError(f"Could not get Flickr request token: {error}") from error
        self._authorization_session = temporary
        return temporary.authorization_url(AUTHORIZE_URL, perms="read"), request_token["oauth_token"]

    def exchange_verifier(self, verifier: str) -> FlickrToken:
        temporary = getattr(self, "_authorization_session", None)
        if not temporary:
            raise FlickrAuthenticationError("Start authorization and use its verifier in the same command.")
        try:
            access = temporary.fetch_access_token(ACCESS_TOKEN_URL, verifier=verifier)
        except requests.RequestException as error:
            raise FlickrAuthenticationError(f"Could not exchange Flickr verifier: {error}") from error
        return FlickrToken(
            oauth_token=access["oauth_token"], oauth_token_secret=access["oauth_token_secret"],
            user_nsid=access["user_nsid"], username=access.get("username"),
        )

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        payload = {"method": method, "format": "json", "nojsoncallback": "1", "api_key": self.api_key, **params}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(REST_URL, params=payload, timeout=(10, 60))
                if response.status_code == 429 or response.status_code >= 500:
                    self._retry(response, attempt, method)
                    continue
                response.raise_for_status()
                body = response.json()
                if body.get("stat") != "ok":
                    # Flickr error 105 is service unavailable; retry it like a 5xx.
                    if str(body.get("code")) == "105":
                        self._retry(response, attempt, method)
                        continue
                    raise FlickrError(f"{method} failed ({body.get('code')}): {body.get('message')}")
                return body
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt >= self.max_retries:
                    raise FlickrError(f"{method} failed after retries: {error}") from error
                self._backoff(attempt, method)
        raise FlickrError(f"{method} exhausted retries")

    def _retry(self, response: Response, attempt: int, method: str) -> None:
        if attempt >= self.max_retries:
            raise FlickrError(f"{method} failed after {self.max_retries} retries (HTTP {response.status_code})")
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else None
        self._backoff(attempt, method, delay)

    def _backoff(self, attempt: int, method: str, override: float | None = None) -> None:
        delay = override if override is not None else min(60.0, (2**attempt) + random.random())
        LOG.warning("flickr_retry", extra={"method": method, "delay_seconds": delay})
        self.sleep(delay)

    def _pages(self, method: str, result_key: str, **params: Any) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            body = self.call(method, page=page, per_page=500, **params)
            result = body[result_key]
            yield from result.get("photo", [])
            if page >= int(result.get("pages", 1)):
                return
            page += 1

    def authenticated_account(self) -> FlickrAccount:
        body = self.call("flickr.test.login")
        user = body["user"]
        return FlickrAccount(str(user["id"]), _content(user.get("username")), _content(user.get("fullname")))

    def iter_photos(self, user_id: str) -> Iterator[dict[str, Any]]:
        extras = "description,date_upload,date_taken,original_format,tags,geo,url_o,media"
        yield from self._pages("flickr.people.getPhotos", "photos", user_id=user_id, extras=extras)

    def photo_info(self, photo_id: str) -> dict[str, Any]:
        return self.call("flickr.photos.getInfo", photo_id=photo_id)["photo"]

    def original_url(self, photo_id: str) -> str | None:
        sizes = self.call("flickr.photos.getSizes", photo_id=photo_id)["sizes"]["size"]
        original = next((item for item in sizes if item.get("label") == "Original"), None)
        return (original or sizes[-1]).get("source") if sizes else None

    def iter_albums(self, user_id: str) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            body = self.call("flickr.photosets.getList", user_id=user_id, page=page, per_page=500)
            result = body["photosets"]
            yield from result.get("photoset", [])
            if page >= int(result.get("pages", 1)):
                return
            page += 1

    def iter_album_photo_ids(self, album_id: str) -> Iterator[str]:
        for raw in self._pages("flickr.photosets.getPhotos", "photoset", photoset_id=album_id):
            yield str(raw["id"])
