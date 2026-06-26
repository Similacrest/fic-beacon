import secrets
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base, Channel, Config

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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
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
