"""Shared fixtures for all tests."""
import os
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import ensure_default_channel
from app.models import Base, Config


@pytest.fixture(scope="session", autouse=True)
def set_test_env():
    os.environ.setdefault("BEACON_CALIBRE_LIBRARY_PATH", "/tmp/calibre-test")
    os.environ.setdefault("BEACON_BASE_URL", "http://testserver")


@pytest.fixture
def in_memory_db():
    """Return a SQLAlchemy Session backed by an in-memory SQLite DB.

    Seeds the single Config row and the auto-created General channel, so book
    fixtures (channel_id is NOT NULL) always have a channel to land in.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(Config(
        id=1,
        wpm=250,
        cadence_cron="0 8 * * *",
        thumbs_down_drop_threshold=3,
        feed_secret="test-secret",
    ))
    session.flush()
    ensure_default_channel(session)
    session.commit()
    yield session
    session.close()
    engine.dispose()
