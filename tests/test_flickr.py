from __future__ import annotations

from dataclasses import dataclass

from flickr_to_google_photos.flickr import FlickrClient, parse_album, parse_photo


def test_parse_detailed_photo_metadata():
    photo = parse_photo({
        "id": "42", "title": {"_content": "Sunset"}, "description": {"_content": "At the beach"},
        "dates": {"taken": "2020-01-02 03:04:05", "posted": "1577934245"},
        "tags": {"tag": [{"raw": "travel"}, {"_content": "sunset"}]}, "media": "photo",
        "originalformat": "jpg", "location": {"latitude": "12.5", "longitude": "77.6", "accuracy": "16"},
    }, "https://original.example/42.jpg")
    assert photo.id == "42"
    assert photo.tags == ["travel", "sunset"]
    assert photo.date_taken == "2020-01-02 03:04:05"
    assert photo.latitude == 12.5
    assert photo.original_url == "https://original.example/42.jpg"


def test_parse_album_handles_flickr_content_wrappers():
    album = parse_album({"id": "set-1", "title": {"_content": "Holiday"}, "description": {"_content": "2020"}, "photos": "3"})
    assert (album.id, album.title, album.description, album.photo_count) == ("set-1", "Holiday", "2020", 3)


@dataclass
class FakeResponse:
    body: dict
    status_code: int = 200
    headers: dict | None = None

    def json(self):
        return self.body

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls: list[int] = []

    def get(self, _url, params, timeout):
        self.calls.append(params["page"])
        page = params["page"]
        return FakeResponse({"stat": "ok", "photos": {"page": page, "pages": 2, "photo": [{"id": str(page)}]}})


def test_photo_pagination_fetches_every_page():
    session = FakeSession()
    client = FlickrClient("key", "secret", session=session, sleep=lambda _: None)
    assert [photo["id"] for photo in client.iter_photos("me")] == ["1", "2"]
    assert session.calls == [1, 2]
