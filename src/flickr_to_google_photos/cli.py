"""Command-line entry point for Phase 1 (read-only Flickr inventory)."""

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
    inventory = subparsers.add_parser("inventory", help="discover Flickr photos and albums into SQLite")
    _database_argument(inventory)
    inventory.add_argument("--dry-run", action="store_true", help="count source items without changing SQLite")
    inventory.add_argument("--no-photo-details", action="store_true", help="skip one getInfo call per photo")
    for name, help_text in (("status", "show local migration state"), ("report", "emit local migration report")):
        command = subparsers.add_parser(name, help=help_text)
        _database_argument(command)
        command.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def _db(settings: Settings, override: Path | None) -> MigrationDatabase:
    return MigrationDatabase(override or settings.database_path)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    try:
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

        if args.command == "inventory":
            key, secret = settings.require_flickr()
            token = CredentialStore().load_flickr()
            if not token:
                raise ConfigurationError("No Flickr token in macOS keychain. Run `flickr-gphotos auth-flickr` first.")
            client = FlickrClient(key, secret, token)
            result = InventoryService(client, database).run(
                include_photo_details=not args.no_photo_details, dry_run=args.dry_run
            )
            print(json.dumps(result, sort_keys=True))
            return
    except (ConfigurationError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
