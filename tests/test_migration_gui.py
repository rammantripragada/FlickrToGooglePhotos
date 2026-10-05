from flickr_to_google_photos.gui import MigrationApp


class Value:
    def __init__(self):
        self.value = None
    def set(self, value):
        self.value = value
    def configure(self, **kwargs):
        self.value = kwargs["value"]


class Tree:
    def __init__(self, values):
        self.values = values
    def exists(self, _item):
        return True
    def item(self, _item, option=None, *, values=None):
        if values is not None:
            self.values = values
        return self.values


def test_progress_updates_album_row_and_video_byte_indicator():
    app = MigrationApp.__new__(MigrationApp)
    app.migration_updates = {}
    app.migration_tree = Tree(["Album", 100, 100, "0/100", "Ready"])
    app.album_tree = Tree(["☑", "a", "Album", 100, "Ready", "0/100"])
    for field in ("migration_progress", "migration_detail", "file_progress", "file_detail"):
        setattr(app, field, Value())
    app._show_migration_progress({"album_id": "a", "title": "Album", "total": 100,
        "completed": 31, "uploaded": 30, "reused": 1, "failed": 0, "phase": "Uploading",
        "item": "video.mov", "bytes_done": 1024**2, "bytes_total": 4 * 1024**2, "message": ""})
    assert app.migration_progress.value == 31
    assert app.file_progress.value == 25
    assert app.album_tree.values[5] == "31/100 (31.0%)"
    assert app.migration_tree.values[4] == "Uploading"
    assert "video.mov" in app.file_detail.value
