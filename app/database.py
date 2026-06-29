import logging
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base, Channel, Config

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)

# Enable WAL mode for SQLite to allow concurrent reads during writes
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


DEFAULT_CHANNEL_NAME = "General"
DEFAULT_CHANNEL_SLUG = "general"


def _alembic_config():
    """Build an Alembic Config pointing at the repo's migration scripts and this DB.

    script_location is forced absolute so migrations run regardless of the process CWD
    (the app starts from /app in the container, tests from the repo root).
    """
    from alembic.config import Config as AlembicConfig

    root = Path(__file__).resolve().parent.parent  # repo root (parent of app/)
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(settings.database_url))
    return cfg


def run_migrations() -> None:
    """Bring the schema to head via Alembic. No `create_all` — migrations own the schema.

    A database created by the old `create_all` path has the full schema but no
    `alembic_version` table; we detect that (tables present, no version row) and stamp it
    at the baseline revision so the incremental migrations apply on top instead of trying
    to re-create existing tables. A brand-new DB just upgrades from scratch.
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    cfg = _alembic_config()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "alembic_version" not in tables and "config" in tables:
        # Legacy create_all database — already at baseline, just not stamped. Stamp the
        # base revision (the one with no down_revision) so upgrade picks up from there.
        bases = ScriptDirectory.from_config(cfg).get_bases()
        if bases:
            logger.info("Stamping pre-migration database at baseline %s", bases[0])
            command.stamp(cfg, bases[0])

    command.upgrade(cfg, "head")


def init_db() -> None:
    run_migrations()
    with SessionLocal() as session:
        _ensure_config(session)
        ensure_default_channel(session)
        session.commit()


def _ensure_config(session: Session) -> None:
    cfg = session.get(Config, 1)
    if cfg is None:
        session.add(
            Config(
                id=1,
                wpm=settings.default_wpm,
                cadence_cron=settings.default_cadence_cron,
                thumbs_down_drop_threshold=settings.default_thumbs_down_drop_threshold,
                feed_secret=settings.feed_secret or secrets.token_urlsafe(32),
            )
        )


def ensure_default_channel(session: Session) -> Channel:
    """Return the General channel, creating it if no channels exist yet.

    Every source must belong to a channel, so a fresh install needs at least one. The
    General channel is the import/fallback home; users can rename it or add more.
    """
    channel = (
        session.query(Channel).filter(Channel.slug == DEFAULT_CHANNEL_SLUG).first()
        or session.query(Channel).order_by(Channel.queue_order, Channel.id).first()
    )
    if channel is None:
        channel = Channel(name=DEFAULT_CHANNEL_NAME, slug=DEFAULT_CHANNEL_SLUG)
        session.add(channel)
        session.flush()
    return channel


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


@contextmanager
def db_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
