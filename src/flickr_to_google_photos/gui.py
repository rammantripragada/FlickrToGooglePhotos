"""Native, read-safe Tk desktop interface for the migration journal."""

from __future__ import annotations

import json
import logging
import queue
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from .config import Settings
from .credentials import CredentialStore
from .database import MigrationDatabase
from .flickr import FlickrClient
from .google_deduplication import GoogleDeduplicationGuard
from .inventory import InventoryService
from .oauth_callback import OAuthCallbackServer

LOG = logging.getLogger(__name__)


class MigrationApp:
    def __init__(self, root, settings: Settings, archive_only: bool = False) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root, self.settings = tk, ttk, root, settings
        self.database, self.archive_only = MigrationDatabase(settings.database_path), archive_only
        self.database.initialize()
        self.album_sort_column = "title"
        self.album_sort_reverse = False
        self.album_rows: list[dict[str, object]] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.task_rows: dict[str, str] = {}
        self.style = ttk.Style(root)
        self.style.configure("Authorized.TButton", foreground="#137333")
        root.title("Flickr → Google Photos")
        root.minsize(780, 530)
        self.status_text = tk.StringVar(value="Ready. Inventory and album selection are read-only on Flickr.")
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self._setup_tab(notebook)
        self._inventory_tab(notebook)
        self._albums_tab(notebook)
        self._duplicates_tab(notebook)
        ttk.Label(root, textvariable=self.status_text, anchor="w").pack(fill="x", padx=12, pady=(0, 12))
        self.refresh()
        root.after(100, self._drain_events)

    def _setup_tab(self, notebook) -> None:
        frame = self.ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Setup")
        self.ttk.Label(frame, text="Flickr → Google Photos", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        if self.archive_only:
            self.ttk.Label(frame, justify="left", wraplength=700, text=(
                "Archive-only migration mode. Import Flickr account-data ZIPs locally; no Flickr login, API inventory, or direct Flickr downloads are used."
            )).pack(anchor="w", pady=16)
            self.archive_path = self.tk.StringVar()
            self.ttk.Label(frame, text="Metadata ZIP folder:").pack(anchor="w")
            self.ttk.Entry(frame, textvariable=self.archive_path, width=80).pack(anchor="w", pady=(2, 8))
            self.ttk.Button(frame, text="Import metadata ZIPs", command=self.import_archive).pack(anchor="w")
            self.ttk.Label(frame, text=f"Database: {self.settings.database_path}").pack(anchor="w", pady=(16, 0))
            return
        self.ttk.Label(frame, justify="left", wraplength=700, text=(
            "Phase 1 safely inventories Flickr photos, videos, and albums into local SQLite. "
            "OAuth tokens are stored in macOS Keychain. Google uploads and all destructive operations remain unavailable.\n\n"
            "Set FLICKR_API_KEY and FLICKR_API_SECRET in .env, register the configured loopback callback "
            "with Flickr, then authorize."),
        ).pack(anchor="w", pady=16)
        self.ttk.Label(frame, text=f"Database: {self.settings.database_path}").pack(anchor="w")
        self.ttk.Label(frame, text=f"Callback: {self.settings.flickr_oauth_callback}").pack(anchor="w", pady=(4, 16))
        self.auth_button = self.ttk.Button(frame, text="Authorize Flickr (read-only)", command=self.authorize_flickr)
        self.auth_button.pack(anchor="w")
        self.ttk.Button(frame, text="Authorize Google Photos", command=self.authorize_google).pack(anchor="w", pady=(10, 0))
        self.auth_state = self.tk.Label(
            frame, text="Flickr authorization: not completed", background="#fee2e2", foreground="#991b1b", padx=8, pady=5
        )
        self.auth_state.pack(anchor="w", pady=(8, 0))
        self.ttk.Button(frame, text="Check authorization status", command=self._refresh_authorization_state).pack(anchor="w", pady=(10, 0))

    def _inventory_tab(self, notebook) -> None:
        frame = self.ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Inventory")
        self.summary_text = self.tk.StringVar()
        self.ttk.Label(frame, textvariable=self.summary_text, justify="left").pack(anchor="w")
        workers = self.ttk.Frame(frame)
        workers.pack(anchor="w", pady=(12, 0))
        self.ttk.Label(workers, text="Parallel Flickr requests:").pack(side="left")
        self.inventory_workers = self.tk.IntVar(value=self.settings.inventory_workers)
        self.ttk.Spinbox(workers, from_=1, to=12, width=5, textvariable=self.inventory_workers).pack(side="left", padx=8)
        self.inventory_progress_text = self.tk.StringVar(value="Inventory idle")
        self.inventory_percent = self.tk.StringVar(value="0%")
        self.inventory_progress = self.ttk.Progressbar(frame, mode="determinate", maximum=100, length=560)
        self.inventory_progress.pack(anchor="w", pady=(14, 2))
        self.ttk.Label(frame, textvariable=self.inventory_percent, font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        self.ttk.Label(frame, textvariable=self.inventory_progress_text).pack(anchor="w")
        self.ttk.Label(frame, text="Tasks", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(18, 4))
        self.task_tree = self.ttk.Treeview(frame, columns=("task", "progress", "state"), show="headings", height=5)
        self.task_tree.heading("task", text="Task"); self.task_tree.heading("progress", text="Progress"); self.task_tree.heading("state", text="Status")
        self.task_tree.column("task", width=280); self.task_tree.column("progress", width=90, anchor="center"); self.task_tree.column("state", width=350)
        self.task_tree.pack(fill="x")
        buttons = self.ttk.Frame(frame)
        buttons.pack(anchor="w", pady=18)
        if not self.archive_only:
            self.ttk.Button(buttons, text="Run Flickr Inventory", command=self.run_inventory).pack(side="left")
            self.ttk.Button(buttons, text="Inventory approved albums only", command=self.run_selected_inventory).pack(side="left", padx=8)
            self.ttk.Button(buttons, text="Migrate approved albums", command=self.migrate_approved).pack(side="left", padx=8)
        self.ttk.Button(buttons, text="Refresh Local Status", command=self.refresh).pack(side="left", padx=8)
        self.ttk.Label(frame, text=("Archive metadata is local and safe to import repeatedly." if self.archive_only else "Inventory reads Flickr only. When complete, use Albums to choose the set that may be migrated later."), wraplength=700).pack(anchor="w")

    def import_archive(self) -> None:
        path = Path(self.archive_path.get()).expanduser()
        if not path.is_dir():
            self.status_text.set("Choose a folder containing Flickr account-data ZIPs.")
            return
        def operation():
            from .inventory import import_archive_metadata
            return import_archive_metadata(self.database, path)
        self._background("Importing local Flickr metadata ZIPs", operation)

    def _albums_tab(self, notebook) -> None:
        frame = self.ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Albums")
        self.album_tree = self.ttk.Treeview(frame, columns=("selected", "id", "title", "items"), show="headings", selectmode="extended")
        for column, title, width in (("selected", "Google sync", 100), ("id", "Flickr ID", 165), ("title", "Album name", 340), ("items", "Items", 70)):
            self.album_tree.heading(column, text=title, command=lambda field=column: self._sort_albums(field))
            self.album_tree.column(column, width=width, anchor="center" if column in {"selected", "items"} else "w")
        self.album_tree.pack(fill="both", expand=True)
        self.album_tree.bind("<Button-1>", self._toggle_album_checkbox, add="+")
        buttons = self.ttk.Frame(frame)
        buttons.pack(anchor="w", pady=(10, 0))
        self.ttk.Button(buttons, text="Reload", command=self.load_albums).pack(side="left")
        self.ttk.Button(buttons, text="Approve highlighted for Google sync", command=lambda: self._apply_album_selection(True)).pack(side="left", padx=6)
        self.ttk.Button(buttons, text="Remove highlighted", command=self._remove_album_selection).pack(side="left", padx=6)
        self.ttk.Button(buttons, text="Clear selection", command=lambda: self.database.set_selected_albums(set()) or self.load_albums()).pack(side="left")
        self.ttk.Button(buttons, text="Select all", command=self._select_all_albums).pack(side="left", padx=6)

    def _duplicates_tab(self, notebook) -> None:
        frame = self.ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Duplicates")
        self.duplicate_text = self.tk.Text(frame, height=22, wrap="word", state="disabled")
        self.duplicate_text.pack(fill="both", expand=True)
        self.ttk.Button(frame, text="Refresh duplicate report", command=self.load_duplicates).pack(anchor="w", pady=(10, 0))

    def _background(self, label: str, operation: Callable[[], object], shows_inventory_progress: bool = False) -> None:
        self.status_text.set(f"{label}…")
        row = self.task_rows.get(label)
        if row and self.task_tree.exists(row): self.task_tree.item(row, values=(label, "0%", "Running"))
        else: self.task_rows[label] = self.task_tree.insert("", "end", values=(label, "0%", "Running"))
        if shows_inventory_progress:
            self.inventory_progress.configure(value=0)
            self.inventory_percent.set("0%")
            self.inventory_progress_text.set("Connecting to Flickr…")
        def worker() -> None:
            try:
                self.events.put(("success", (label, operation())))
            except Exception as error:  # surfaced in UI, never silently swallowed
                LOG.exception("gui_background_task_failed")
                self.events.put(("error", (label, str(error))))
        threading.Thread(target=worker, daemon=True).start()

    def authorize_flickr(self) -> None:
        def operation() -> str:
            key, secret = self.settings.require_flickr()
            client = FlickrClient(key, secret)
            url, _ = client.authorization_url(self.settings.flickr_oauth_callback)
            webbrowser.open(url)
            verifier = OAuthCallbackServer(self.settings.flickr_oauth_callback).wait_for_verifier()
            token = client.exchange_verifier(verifier)
            CredentialStore().save_flickr(token)
            return token.username or token.user_nsid
        self._background("Waiting for Flickr authorization", operation)

    def run_inventory(self) -> None:
        def operation() -> dict[str, int]:
            key, secret = self.settings.require_flickr()
            token = CredentialStore().load_flickr()
            if not token:
                raise RuntimeError("Authorize Flickr first.")
            def progress(stage: str, count: int, total: int) -> None:
                self.events.put(("inventory_progress", ("Running read-only Flickr inventory", stage, count, total)))
            return InventoryService(FlickrClient(key, secret, token), self.database).run(
                progress=progress, photo_workers=self.inventory_workers.get()
            )
        self._background("Running read-only Flickr inventory", operation, shows_inventory_progress=True)

    def run_selected_inventory(self) -> None:
        def operation() -> object:
            key, secret = self.settings.require_flickr(); token = CredentialStore().load_flickr()
            if not token: raise RuntimeError("Authorize Flickr first.")
            def progress(stage: str, count: int, total: int) -> None: self.events.put(("inventory_progress", ("Inventorying approved albums only", stage, count, total)))
            return InventoryService(FlickrClient(key, secret, token), self.database).run_selected_albums(self.inventory_workers.get(), progress)
        self._background("Inventorying approved albums only", operation, shows_inventory_progress=True)

    def authorize_google(self) -> None:
        def operation() -> str:
            from .google import authorize
            authorize(self.settings.google_client_secrets_file)
            return "Google Photos authorized"
        self._background("Waiting for Google authorization", operation)

    def migrate_approved(self) -> None:
        from tkinter import messagebox
        if not self.database.selected_albums():
            messagebox.showwarning("No albums selected", "Approve one or more albums in the Albums tab first.")
            return
        if not messagebox.askyesno("Start migration", "Create approved Google Photos albums and copy their media now? This may use Google storage."):
            return
        def operation() -> object:
            # Refresh only selected album members immediately before any Google
            # write. This avoids a 190k-item whole-library inventory and prevents
            # empty destination albums from stale/absent membership data.
            key, secret = self.settings.require_flickr()
            token = CredentialStore().load_flickr()
            if not token:
                raise RuntimeError("Authorize Flickr first.")
            def progress(stage: str, count: int, total: int) -> None:
                self.events.put(("inventory_progress", ("Refreshing approved albums, then migrating", stage, count, total)))
            InventoryService(FlickrClient(key, secret, token), self.database).run_selected_albums(
                self.inventory_workers.get(), progress
            )
            from .migrate import MigrationService
            return MigrationService(
                self.database, self.settings.download_dir,
                download_interval_seconds=self.settings.download_interval_seconds,
            ).run()
        self._background("Refreshing approved albums, then migrating", operation, shows_inventory_progress=True)

    def refresh(self) -> None:
        summary = self.database.summary()
        self.summary_text.set("\n".join(f"{key.replace('_', ' ').title()}: {value}" for key, value in summary.items()))
        self.load_albums()
        self.load_duplicates()
        if not self.archive_only:
            self._refresh_authorization_state()

    def _set_authorized_state(self) -> None:
        self.auth_button.configure(text="Flickr authorized ✓", style="Authorized.TButton")
        self.auth_state.configure(text="Flickr authorization: completed successfully ✓", background="#dcfce7", foreground="#137333")

    def _refresh_authorization_state(self) -> None:
        if CredentialStore().load_flickr():
            self._set_authorized_state()
        else:
            self.auth_button.configure(text="Authorize Flickr (read-only)", style="TButton")
            self.auth_state.configure(text="Flickr authorization: not completed", background="#fee2e2", foreground="#991b1b")

    def load_albums(self) -> None:
        self.album_rows = self.database.albums()
        self._render_albums()

    def _sort_albums(self, column: str) -> None:
        if column == self.album_sort_column:
            self.album_sort_reverse = not self.album_sort_reverse
        else:
            self.album_sort_column = column
            self.album_sort_reverse = False
        self._render_albums()

    def _render_albums(self) -> None:
        for item in self.album_tree.get_children():
            self.album_tree.delete(item)
        field_map = {"selected": "selected_for_migration", "id": "flickr_id", "title": "title", "items": "photo_count"}
        field = field_map[self.album_sort_column]
        def sort_key(album: dict[str, object]) -> object:
            value = album[field]
            return value.casefold() if isinstance(value, str) else (value or 0)
        for column, title in (("selected", "Google sync"), ("id", "Flickr ID"), ("title", "Album name"), ("items", "Items")):
            arrow = " ↓" if column == self.album_sort_column and self.album_sort_reverse else " ↑" if column == self.album_sort_column else ""
            self.album_tree.heading(column, text=title + arrow)
        for album in sorted(self.album_rows, key=sort_key, reverse=self.album_sort_reverse):
            self.album_tree.insert("", "end", iid=str(album["flickr_id"]), values=("☑" if album["selected_for_migration"] else "☐", album["flickr_id"], album["title"], album["photo_count"] or 0))

    def _toggle_album_checkbox(self, event) -> None:
        if self.album_tree.identify_column(event.x) != "#1":
            return
        item = self.album_tree.identify_row(event.y)
        if not item:
            return
        chosen = {str(album["flickr_id"]) for album in self.database.albums() if album["selected_for_migration"]}
        if item in chosen:
            chosen.remove(item)
        else:
            chosen.add(item)
        self.database.set_selected_albums(chosen)
        self.load_albums()
        return "break"

    def _apply_album_selection(self, _selected: bool) -> None:
        chosen = {str(album["flickr_id"]) for album in self.database.albums() if album["selected_for_migration"]}
        chosen.update(self.album_tree.selection())
        self.database.set_selected_albums(chosen)
        self.load_albums()
        self.status_text.set(f"Approved {len(chosen)} album(s) for the future Google sync.")

    def _remove_album_selection(self) -> None:
        chosen = {str(album["flickr_id"]) for album in self.database.albums() if album["selected_for_migration"]}
        chosen.difference_update(self.album_tree.selection())
        self.database.set_selected_albums(chosen)
        self.load_albums()

    def _select_all_albums(self) -> None:
        self.database.set_selected_albums({str(album["flickr_id"]) for album in self.database.albums()})
        self.load_albums()

    def load_duplicates(self) -> None:
        report = self.database.duplicate_report()
        report["google_link_collisions"] = GoogleDeduplicationGuard(self.database).linked_media_collisions()
        self.duplicate_text.configure(state="normal")
        self.duplicate_text.delete("1.0", "end")
        self.duplicate_text.insert("1.0", json.dumps(report, indent=2, sort_keys=True))
        self.duplicate_text.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "inventory_progress":
                    task_label, stage, count, total = payload  # type: ignore[misc]
                    ratio = (count / total) if total else 0.0
                    if stage == "albums":
                        percent = ratio * 10
                        self.load_albums()
                    elif stage == "photos and videos":
                        percent = 10 + ratio * 80
                    elif str(stage).endswith(" media"):
                        # Targeted selected-album inventory: media discovery is
                        # the main phase, followed by a short membership phase.
                        percent = ratio * 90
                    else:
                        percent = 90 + ratio * 10
                    self.inventory_progress.configure(value=percent)
                    self.inventory_percent.set(f"{percent:.0f}%")
                    suffix = f"{count:,} of {total:,}" if total else f"{count:,}"
                    self.inventory_progress_text.set(f"{stage.title()}: {suffix} ({percent:.0f}%)")
                    row = self.task_rows.get(task_label)
                    if row and self.task_tree.exists(row):
                        self.task_tree.item(row, values=(task_label, f"{percent:.0f}%", f"{stage.title()}: {suffix}"))
                    continue
                label, result = payload  # type: ignore[misc]
                if kind == "success":
                    row = self.task_rows.get(label)
                    if row and self.task_tree.exists(row): self.task_tree.item(row, values=(label, "100%", "Completed"))
                    self.status_text.set(f"{label} completed: {result}")
                    if label == "Waiting for Flickr authorization":
                        self._set_authorized_state()
                    if label == "Running read-only Flickr inventory":
                        self.inventory_progress.configure(value=100)
                        self.inventory_percent.set("100%")
                        self.inventory_progress_text.set("Inventory completed successfully")
                    self.refresh()
                else:
                    row = self.task_rows.get(label)
                    if row and self.task_tree.exists(row): self.task_tree.item(row, values=(label, "—", f"Failed: {result}"))
                    self.status_text.set(f"{label} failed: {result}")
                    if label == "Running read-only Flickr inventory":
                        self.inventory_progress_text.set("Inventory stopped; you can safely run it again")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)


def launch(settings: Settings | None = None, archive_only: bool = False) -> None:
    try:
        import tkinter as tk
    except ImportError as error:
        raise RuntimeError("Tkinter is unavailable. Install a Python build with Tk support.") from error
    active_settings = settings or Settings.from_environment()
    from .logging import configure_logging
    configure_logging(active_settings.log_level, active_settings.log_file)
    root = tk.Tk()
    MigrationApp(root, active_settings, archive_only=archive_only)
    root.mainloop()
