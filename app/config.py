import secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEACON_", env_file=".env")

    # Calibre
    calibre_library_path: Path = Path("/calibre-library")

    # App database
    database_url: str = "sqlite:////data/fic-beacon.db"

    # Feed
    base_url: str = "http://localhost:8000"
    feed_secret: str = secrets.token_urlsafe(32)

    # FanFicFare/Calibre fetcher container — Fic-Beacon POSTs story URLs here to have
    # new chapters downloaded into the (read-only-to-us) Calibre library. See fetcher/.
    fetcher_url: str = "http://fetcher:8080"
    # Per-request timeout (seconds) for a fetch — FanFicFare downloads can be slow.
    fetcher_timeout: float = 300.0

    # Scheduling timezone for drop/poll cron (e.g. "Europe/Tallinn").
    # Unset → APScheduler uses the machine local tz, which is UTC in a stock container.
    tz: str | None = None

    # Global defaults seeded into the config table on first run (overridable in-app).
    default_wpm: int = 250
    default_cadence_cron: str = "0 7,19 * * *"  # 07:00 and 19:00 daily
    default_thumbs_down_drop_threshold: int = 3

    # Misc
    feed_item_limit: int = 50


settings = Settings()
