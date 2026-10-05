from flickr_to_google_photos.gui import MigrationApp
from queue import Queue
from types import SimpleNamespace


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


class TransferTree:
    def __init__(self):
        self.rows = {}
    def get_children(self):
        return list(self.rows)
    def delete(self, key):
        self.rows.pop(key)
    def exists(self, key):
        return key in self.rows
    def item(self, key, *, values):
        self.rows[key] = values
    def insert(self, _parent, _position, *, iid, values):
        self.rows[iid] = values


def test_progress_updates_album_row_and_video_byte_indicator():
    app = MigrationApp.__new__(MigrationApp)
    app.migration_updates = {}
    app.migration_tree = Tree(["Album", 100, 100, "0/100", "Ready"])
    app.album_tree = Tree(["☑", "a", "Album", 100, "Ready", "0/100"])
    for field in ("migration_progress", "migration_detail", "file_progress", "file_detail"):
        setattr(app, field, Value())
    app.transfer_tree = TransferTree()
    app.task_rows = {"Migrating local archive albums": "task"}
    app.task_tree = Tree(["Migration", "0%", "Running"])
    app.migration_totals = {"a": (0, 100), "b": (0, 100)}
    app._show_migration_progress({"album_id": "a", "title": "Album", "total": 100,
        "completed": 31, "uploaded": 30, "reused": 1, "failed": 0, "phase": "Uploading",
        "item": "video.mov", "bytes_done": 1024**2, "bytes_total": 4 * 1024**2, "message": ""})
    assert app.migration_progress.value == 31
    assert app.file_progress.value == 25
    assert app.album_tree.values[5] == "31/100 (31.0%)"
    assert app.migration_tree.values[4] == "Uploading"
    assert "video.mov" in app.file_detail.value
    assert app.task_tree.values[1] == "15.5%"  # includes the other queued album


def test_parallel_transfer_rows_show_each_album_and_remove_finished_files():
    app = MigrationApp.__new__(MigrationApp)
    app.migration_updates = {}
    app.migration_tree = Tree(["Album", 100, 100, "0/100", "Ready"])
    app.album_tree = Tree(["☑", "a", "Album", 100, "Ready", "0/100"])
    app.transfer_tree = TransferTree()
    for field in ("migration_progress", "migration_detail", "file_progress", "file_detail"):
        setattr(app, field, Value())
    event = {"album_id": "a", "title": "Album", "total": 100, "completed": 31,
        "uploaded": 30, "reused": 1, "failed": 0, "phase": "Uploading", "item": "video.mov",
        "bytes_done": 1024**2, "bytes_total": 4 * 1024**2, "message": "",
        "transfers": [{"key": "a:p", "album_title": "Album", "item": "image.jpg", "phase": "Uploading", "bytes_done": 1024**2, "bytes_total": 2 * 1024**2},
                      {"key": "b:v", "album_title": "Other", "item": "video.mov", "phase": "Uploading", "bytes_done": 1024**2, "bytes_total": 4 * 1024**2}]}
    app._show_migration_progress(event)
    assert app.transfer_tree.rows["a:p"][-1] == "50.0%"
    assert app.transfer_tree.rows["b:v"][-1] == "25.0%"
    assert app.transfer_tree.rows["b:v"][0] == "Other"
    app._show_migration_progress({**event, "transfers": []})
    assert not app.transfer_tree.rows


def test_pause_keeps_task_percentage_instead_of_claiming_completion():
    app = MigrationApp.__new__(MigrationApp)
    app.events = Queue()
    app.events.put(("success", ("Migrating local archive albums", {"paused": 1})))
    app.migration_running = True
    app.task_rows = {"Migrating local archive albums": "task"}
    app.task_tree = Tree(["Migration", "31.0%", "Running"])
    app.status_text, app.migration_detail = Value(), Value()
    button = SimpleNamespace(configure=lambda **_: None)
    app.upload_spin = app.album_spin = app.batch_spin = app.migrate_button = app.pause_button = button
    app.album_buttons = []
    app.root = SimpleNamespace(after=lambda *_: None)
    app.refresh = lambda: None
    app._drain_events()
    assert not app.migration_running
    assert app.task_tree.values[1] == "31.0%"
    assert app.task_tree.values[2].startswith("Paused")
    assert "paused" in app.status_text.value
