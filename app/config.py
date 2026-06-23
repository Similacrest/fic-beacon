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

    # Scheduling timezone for drop/poll cron (e.g. "Europe/Tallinn").
    # Unset → APScheduler uses the machine local tz, which is UTC in a stock container.
    tz: str | None = None

    # Defaults (overridable from the config table in DB)
    default_global_budget_words: int = 5000
    default_global_budget_minutes: int = 20
    default_budget_mode: str = "words"  # words | minutes
    default_wpm: int = 250
    default_overshoot_tolerance: int = 1000
    default_parallel_slots: int = 2
    default_cadence_cron: str = "0 8 * * *"  # 08:00 daily
    default_thumbs_down_drop_threshold: int = 3

    # Misc
    feed_item_limit: int = 50


settings = Settings()
