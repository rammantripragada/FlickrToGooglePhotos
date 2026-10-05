import pytest

from flickr_to_google_photos.cli import build_parser
from flickr_to_google_photos.config import Settings


def test_parallel_cli_configuration():
    args = build_parser().parse_args(["migrate-archive", "--workers", "6", "--album-workers", "3", "--batch-size", "20", "--dry-run"])
    assert (args.workers, args.album_workers, args.batch_size) == (6, 3, 20)
    assert args.dry_run


def test_parallel_environment_defaults_and_limits(monkeypatch):
    monkeypatch.setattr("flickr_to_google_photos.config.load_dotenv", lambda **_: None)
    for name in ("MIGRATOR_UPLOAD_WORKERS", "MIGRATOR_ALBUM_WORKERS", "MIGRATOR_GOOGLE_BATCH_SIZE"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_environment()
    assert (settings.upload_workers, settings.album_workers, settings.google_batch_size) == (4, 2, 50)
    monkeypatch.setenv("MIGRATOR_UPLOAD_WORKERS", "100")
    monkeypatch.setenv("MIGRATOR_ALBUM_WORKERS", "0")
    monkeypatch.setenv("MIGRATOR_GOOGLE_BATCH_SIZE", "100")
    settings = Settings.from_environment()
    assert (settings.upload_workers, settings.album_workers, settings.google_batch_size) == (8, 1, 50)


@pytest.mark.parametrize("args", [["--workers", "9"], ["--album-workers", "0"], ["--batch-size", "51"]])
def test_parallel_cli_invalid_values(args):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["migrate-archive", *args])
