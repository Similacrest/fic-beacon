import secrets
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base, BudgetMode, Config

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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        _ensure_config(session)
        session.commit()


def _ensure_config(session: Session) -> None:
    cfg = session.get(Config, 1)
    if cfg is None:
        session.add(
            Config(
                id=1,
                global_budget_words=settings.default_global_budget_words,
                global_budget_minutes=settings.default_global_budget_minutes,
                budget_mode=BudgetMode(settings.default_budget_mode),
                wpm=settings.default_wpm,
                overshoot_tolerance=settings.default_overshoot_tolerance,
                parallel_slots=settings.default_parallel_slots,
                cadence_cron=settings.default_cadence_cron,
                thumbs_down_drop_threshold=settings.default_thumbs_down_drop_threshold,
                feed_secret=settings.feed_secret or secrets.token_urlsafe(32),
                target_total_words=settings.default_target_total_words or None,
            )
        )


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


@contextmanager
def db_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
