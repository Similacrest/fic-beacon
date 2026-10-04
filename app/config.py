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

    # Reverse-proxy shared secret (see app/security.py:ProxyTokenMiddleware). When set, every
    # route outside the public prefixes (/feed /read /fb /img /static) needs a matching
    # `X-Beacon-Proxy-Token` header. When unset, non-public routes are refused unless bind_host is
    # loopback (local development).
    proxy_token: str | None = None
    # Address uvicorn is bound to (the Dockerfile binds 0.0.0.0). Only used for the fail-closed
    # check above — set BEACON_BIND_HOST=127.0.0.1 when running locally on loopback.
    bind_host: str = "0.0.0.0"
    # Explicit opt-out of the fail-closed check: with no proxy token, serve /admin to anyone who can
    # reach the port (local / trusted-network use only). Ignored when proxy_token is set.
    admin_open: bool = False
    # Expose /websub without the proxy token (only if the proxy publishes it).
    public_websub: bool = False

    # WebSub: optional comma-separated allowlist of callback hosts (e.g. incoming.inoreader.com).
    # Empty → any host that resolves only to public addresses (https only).
    websub_callback_hosts: str = ""

    # FanFicFare/Calibre fetcher container — Fic-Beacon POSTs story URLs here to have
    # new chapters downloaded into the (read-only-to-us) Calibre library. See fetcher/.
    # Fetches are async: POST /fetch returns a job_id immediately; we poll GET /fetch/{id}.
    # Shared secret sent to the fetcher as X-Fetcher-Token (must equal FETCHER_TOKEN there).
    fetcher_token: str | None = None
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
    default_release_budget: float = 5000.0  # words per release for the seeded "Default" schedule

    # Misc
    feed_item_limit: int = 50

    # Root log level for the app's own loggers (e.g. "DEBUG" to trace the WebSub
    # subscribe/verify/push flow). Anything Python's logging accepts: DEBUG/INFO/WARNING/…
    log_level: str = "INFO"


settings = Settings()
