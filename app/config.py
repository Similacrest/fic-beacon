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
    # Fetches are async: POST /fetch returns a job_id immediately; we poll GET /fetch/{id}.
    fetcher_url: str = "http://fetcher:8080"
    # Per-request HTTP timeout (seconds). Only covers the quick 202 submit / status GET —
    # the actual (slow, up to ~15 min) FanFicFare run happens in the fetcher's background.
    fetcher_timeout: float = 30.0
    # How often to poll a running fetch job for completion (seconds).
    fetcher_poll_interval: float = 30.0
    # Give up on a fetch job after this many seconds (mark the books as errored).
    fetcher_job_timeout: float = 1200.0

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
