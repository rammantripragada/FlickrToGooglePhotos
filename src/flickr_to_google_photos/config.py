"""Configuration loaded from environment variables, never committed secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """A required configuration value is absent or invalid."""


@dataclass(frozen=True)
class Settings:
    flickr_api_key: str | None
    flickr_api_secret: str | None
    flickr_oauth_callback: str
    database_path: Path
    download_dir: Path
    log_level: str

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(override=False)
        return cls(
            flickr_api_key=os.getenv("FLICKR_API_KEY"),
            flickr_api_secret=os.getenv("FLICKR_API_SECRET"),
            flickr_oauth_callback=os.getenv("FLICKR_OAUTH_CALLBACK", "http://127.0.0.1:8765/callback"),
            database_path=Path(os.getenv("MIGRATOR_DATABASE", "flickr-to-google-photos.sqlite3")),
            download_dir=Path(os.getenv("MIGRATOR_DOWNLOAD_DIR", "downloads")),
            log_level=os.getenv("MIGRATOR_LOG_LEVEL", "INFO").upper(),
        )

    def require_flickr(self) -> tuple[str, str]:
        if not self.flickr_api_key or not self.flickr_api_secret:
            raise ConfigurationError(
                "FLICKR_API_KEY and FLICKR_API_SECRET must be set (usually in your shell or .env)."
            )
        return self.flickr_api_key, self.flickr_api_secret
