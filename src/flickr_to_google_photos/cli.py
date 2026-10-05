"""Command-line entry point for Flickr inventory and local archive migration."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from .config import ConfigurationError, Settings
from .credentials import CredentialStore
from .database import MigrationDatabase
from .flickr import FlickrClient
from .inventory import InventoryService
from .logging import configure_logging
from .oauth_callback import OAuthCallbackServer


def _database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, help="SQLite migration database path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flickr-gphotos", description="Safe, resumable Flickr migration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    auth = subparsers.add_parser("auth-flickr", help="authorize read-only access to Flickr")
    auth.add_argument("--callback-url", help="registered Flickr callback URL; defaults to FLICKR_OAUTH_CALLBACK")
    auth.add_argument("--manual-verifier", action="store_true", help="do not start the loopback callback receiver")
    subparsers.add_parser("auth-google", help="authorize Google Photos uploads")
    migrate = subparsers.add_parser("migrate", help="create approved Google albums and copy their media")
    _database_argument(migrate)
    archive_migration = subparsers.add_parser("migrate-archive", help="upload selected albums from local indexed ZIPs")
    _database_argument(archive_migration)
    archive_migration.add_argument("--dry-run", action="store_true", help="check local selected-album readiness without Google writes")
    archive_migration.add_argument("--work-dir", type=Path, help="temporary one-item extraction folder")
    inventory = subparsers.add_parser("inventory", help="discover Flickr photos and albums into SQLite")
    _database_argument(inventory)
    inventory.add_argument("--dry-run", action="store_true", help="count source items without changing SQLite")
    inventory.add_argument("--no-photo-details", action="store_true", help="skip one getInfo call per photo")
    inventory.add_argument("--workers", type=int, help="concurrent Flickr detail requests (default: MIGRATOR_INVENTORY_WORKERS)")
    selected_inventory = subparsers.add_parser("inventory-selected", help="inventory only approved album members")
    _database_argument(selected_inventory)
    selected_inventory.add_argument("--workers", type=int, help="concurrent Flickr detail requests")
    archive = subparsers.add_parser("import-archive", help="import Flickr Data JSON metadata without Flickr API calls")
    _database_argument(archive)
    archive.add_argument("archive_part", type=Path, help="directory of metadata ZIPs or an extracted ..._partN folder")
    media = subparsers.add_parser("index-archive-media", help="index local Flickr media ZIPs without extracting them")
    _database_argument(media)
    media.add_argument("directories", nargs="+", type=Path, help="one or more directories containing media ZIPs")
    selection = inventory.add_mutually_exclusive_group()
    selection.add_argument("--all-albums", action="store_true", help="select every discovered album for the future migration")
    selection.add_argument("--album", action="append", default=[], metavar="FLICKR_ID", help="select one album; repeat for multiple")
    selection.add_argument("--no-album-selection", action="store_true", help="keep existing album selection without prompting")
    for name, help_text in (("status", "show local migration state"), ("report", "emit local migration report")):
        command = subparsers.add_parser(name, help=help_text)
        _database_argument(command)
        command.add_argument("--json", action="store_true", help="emit JSON")
    duplicates = subparsers.add_parser("duplicates", help="report repeated album membership and verified duplicate files")
    _database_argument(duplicates)
    duplicates.add_argument("--json", action="store_true", help="emit JSON")
    albums = subparsers.add_parser("albums", help="list discovered Flickr albums and choose the migration set")
    _database_argument(albums)
    albums.add_argument("--all", action="store_true", help="select every album without prompting")
    albums.add_argument("--select", action="append", default=[], metavar="FLICKR_ID", help="select one album; repeat for multiple")
    subparsers.add_parser("gui", help="launch the native desktop interface")
    subparsers.add_parser("archive-gui", help="launch archive-only metadata interface (no Flickr login)")
    return parser


def _db(settings: Settings, override: Path | None) -> MigrationDatabase:
    return MigrationDatabase(override or settings.database_path)


def _print_albums(database: MigrationDatabase) -> None:
    for album in database.albums():
        marker = "[x]" if album["selected_for_migration"] else "[ ]"
        print(f"{marker} {album['flickr_id']}  {album['title']} ({album['photo_count'] or 0} items)")


def _select_albums_interactively(database: MigrationDatabase) -> None:
    albums = database.albums()
    if not albums:
        print("No Flickr albums were discovered.")
        return
    print("Discovered Flickr albums:")
    _print_albums(database)
    answer = input("Albums to migrate: enter IDs separated by commas, 'all', or 'none': ").strip()
    if answer.lower() == "all":
        database.set_selected_albums({str(album["flickr_id"]) for album in albums})
    elif answer.lower() in {"", "none"}:
        database.set_selected_albums(set())
    else:
        database.set_selected_albums({part.strip() for part in answer.split(",") if part.strip()})


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_environment()
    configure_logging(settings.log_level, settings.log_file)
    try:
        if args.command == "gui":
            from .gui import launch
            launch(settings)
            return
        if args.command == "archive-gui":
            from .gui import launch
            launch(settings, archive_only=True)
            return
        if args.command == "auth-flickr":
            key, secret = settings.require_flickr()
            client = FlickrClient(key, secret)
            callback_url = args.callback_url or settings.flickr_oauth_callback
            url, _ = client.authorization_url(callback_url)
            print("Open this URL in a browser and approve read-only access:")
            print(url)
            if args.manual_verifier:
                verifier = input("Flickr verifier: ").strip()
            else:
                webbrowser.open(url)
                print("Waiting for the local OAuth callback…")
                verifier = OAuthCallbackServer(callback_url).wait_for_verifier()
            if not verifier:
                raise ConfigurationError("No Flickr verifier was supplied.")
            token = client.exchange_verifier(verifier)
            CredentialStore().save_flickr(token)
            print(f"Stored Flickr credentials for {token.username or token.user_nsid} in the macOS keychain.")
            return
        if args.command == "auth-google":
            from .google import authorize
            authorize(settings.google_client_secrets_file)
            print("Google Photos authorization completed.")
            return

        database = _db(settings, args.database)
        if args.command in {"status", "report"}:
            database.initialize()
            result = database.summary()
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                for key, value in result.items():
                    print(f"{key.replace('_', ' '):20} {value}")
            return

        if args.command == "duplicates":
            database.initialize()
            result = database.duplicate_report()
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                for category, records in result.items():
                    print(f"{category.replace('_', ' ')}: {len(records)}")
                    for record in records:
                        print(f"  {json.dumps(record, sort_keys=True)}")
            return

        if args.command == "albums":
            database.initialize()
            if args.all:
                database.set_selected_albums({str(album["flickr_id"]) for album in database.albums()})
            elif args.select:
                database.set_selected_albums(set(args.select))
            else:
                _select_albums_interactively(database)
            _print_albums(database)
            return

        if args.command == "inventory":
            key, secret = settings.require_flickr()
            token = CredentialStore().load_flickr()
            if not token:
                raise ConfigurationError("No Flickr token in macOS keychain. Run `flickr-gphotos auth-flickr` first.")
            client = FlickrClient(key, secret, token)
            result = InventoryService(client, database).run(
                include_photo_details=not args.no_photo_details, dry_run=args.dry_run,
                photo_workers=args.workers or settings.inventory_workers,
            )
            if not args.dry_run:
                if args.all_albums:
                    database.set_selected_albums({str(album["flickr_id"]) for album in database.albums()})
                elif args.album:
                    database.set_selected_albums(set(args.album))
                elif not args.no_album_selection and sys.stdin.isatty():
                    _select_albums_interactively(database)
                elif not args.no_album_selection:
                    print("Album selection not prompted (non-interactive input). Run `flickr-gphotos albums` to choose albums.")
            print(json.dumps(result, sort_keys=True))
            return
        if args.command == "inventory-selected":
            key, secret = settings.require_flickr(); token = CredentialStore().load_flickr()
            if not token: raise ConfigurationError("Run `flickr-gphotos auth-flickr` first.")
            result = InventoryService(FlickrClient(key, secret, token), database).run_selected_albums(args.workers or settings.inventory_workers)
            print(json.dumps(result, sort_keys=True)); return
        if args.command == "import-archive":
            from .inventory import import_archive_metadata
            print(json.dumps(import_archive_metadata(database, args.archive_part), sort_keys=True)); return
        if args.command == "index-archive-media":
            from .inventory import index_archive_media
            print(json.dumps(index_archive_media(database, args.directories), sort_keys=True)); return
        if args.command == "migrate":
            from .migrate import MigrationService
            result = MigrationService(
                database, settings.download_dir,
                download_interval_seconds=settings.download_interval_seconds,
            ).run()
            print(json.dumps(result, sort_keys=True))
            return
        if args.command == "migrate-archive":
            from .archive_migrate import ArchiveMigrationService
            database.initialize()
            service = ArchiveMigrationService(database, args.work_dir or settings.download_dir / "archive-work",
                progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
            print(json.dumps(service.preflight() if args.dry_run else service.run(), sort_keys=True))
            return
    except (ConfigurationError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
