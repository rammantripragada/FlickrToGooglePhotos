"""Native, read-safe Tk desktop interface for the migration journal."""

from __future__ import annotations

import json
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


class MigrationApp:
    def __init__(self, root, settings: Settings) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root, self.settings = tk, ttk, root, settings
        self.database = MigrationDatabase(settings.database_path)
        self.database.initialize()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
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
        self.auth_state = self.ttk.Label(frame, text="Flickr authorization: not completed", foreground="#9b1c1c")
        self.auth_state.pack(anchor="w", pady=(8, 0))

    def _inventory_tab(self, notebook) -> None:
        frame = self.ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Inventory")
        self.summary_text = self.tk.StringVar()
        self.ttk.Label(frame, textvariable=self.summary_text, justify="left").pack(anchor="w")
        self.inventory_progress_text = self.tk.StringVar(value="Inventory idle")
        self.inventory_progress = self.ttk.Progressbar(frame, mode="indeterminate", length=460)
        self.inventory_progress.pack(anchor="w", pady=(14, 2))
        self.ttk.Label(frame, textvariable=self.inventory_progress_text).pack(anchor="w")
        buttons = self.ttk.Frame(frame)
        buttons.pack(anchor="w", pady=18)
        self.ttk.Button(buttons, text="Run Flickr Inventory", command=self.run_inventory).pack(side="left")
        self.ttk.Button(buttons, text="Refresh Local Status", command=self.refresh).pack(side="left", padx=8)
        self.ttk.Label(frame, text=(
            "Inventory reads Flickr only. When complete, use Albums to choose the set that may be migrated later."
        ), wraplength=700).pack(anchor="w")

    def _albums_tab(self, notebook) -> None:
        frame = self.ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Albums")
        self.album_tree = self.ttk.Treeview(frame, columns=("selected", "id", "title", "items"), show="headings", selectmode="extended")
        for column, title, width in (("selected", "Migrate", 80), ("id", "Flickr ID", 165), ("title", "Album", 360), ("items", "Items", 70)):
            self.album_tree.heading(column, text=title)
            self.album_tree.column(column, width=width, anchor="center" if column in {"selected", "items"} else "w")
        self.album_tree.pack(fill="both", expand=True)
        buttons = self.ttk.Frame(frame)
        buttons.pack(anchor="w", pady=(10, 0))
        self.ttk.Button(buttons, text="Reload", command=self.load_albums).pack(side="left")
        self.ttk.Button(buttons, text="Select highlighted", command=lambda: self._apply_album_selection(True)).pack(side="left", padx=6)
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
        if shows_inventory_progress:
            self.inventory_progress.start(12)
            self.inventory_progress_text.set("Connecting to Flickr…")
        def worker() -> None:
            try:
                self.events.put(("success", (label, operation())))
            except Exception as error:  # surfaced in UI, never silently swallowed
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
            def progress(stage: str, count: int) -> None:
                self.events.put(("inventory_progress", f"Read {count:,} {stage}…"))
            return InventoryService(FlickrClient(key, secret, token), self.database).run(progress=progress)
        self._background("Running read-only Flickr inventory", operation, shows_inventory_progress=True)

    def refresh(self) -> None:
        summary = self.database.summary()
        self.summary_text.set("\n".join(f"{key.replace('_', ' ').title()}: {value}" for key, value in summary.items()))
        self.load_albums()
        self.load_duplicates()
        if CredentialStore().load_flickr():
            self._set_authorized_state()

    def _set_authorized_state(self) -> None:
        self.auth_button.configure(text="Flickr authorized ✓", style="Authorized.TButton")
        self.auth_state.configure(text="Flickr authorization: completed successfully", foreground="#137333")

    def load_albums(self) -> None:
        for item in self.album_tree.get_children():
            self.album_tree.delete(item)
        for album in self.database.albums():
            self.album_tree.insert("", "end", iid=str(album["flickr_id"]), values=("✓" if album["selected_for_migration"] else "", album["flickr_id"], album["title"], album["photo_count"] or 0))

    def _apply_album_selection(self, _selected: bool) -> None:
        chosen = set(self.album_tree.selection())
        self.database.set_selected_albums(chosen)
        self.load_albums()
        self.status_text.set(f"Selected {len(chosen)} album(s) for the future migration.")

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
                    self.inventory_progress_text.set(str(payload))
                    continue
                label, result = payload  # type: ignore[misc]
                if kind == "success":
                    self.status_text.set(f"{label} completed: {result}")
                    if label == "Waiting for Flickr authorization":
                        self._set_authorized_state()
                    if label == "Running read-only Flickr inventory":
                        self.inventory_progress.stop()
                        self.inventory_progress_text.set("Inventory completed successfully")
                    self.refresh()
                else:
                    self.status_text.set(f"{label} failed: {result}")
                    if label == "Running read-only Flickr inventory":
                        self.inventory_progress.stop()
                        self.inventory_progress_text.set("Inventory stopped; you can safely run it again")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)


def launch(settings: Settings | None = None) -> None:
    try:
        import tkinter as tk
    except ImportError as error:
        raise RuntimeError("Tkinter is unavailable. Install a Python build with Tk support.") from error
    root = tk.Tk()
    MigrationApp(root, settings or Settings.from_environment())
    root.mainloop()
