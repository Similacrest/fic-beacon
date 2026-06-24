"""Shared fixtures for all tests."""
import os
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, BudgetMode, Config


@pytest.fixture(scope="session", autouse=True)
def set_test_env():
    os.environ.setdefault("BEACON_CALIBRE_LIBRARY_PATH", "/tmp/calibre-test")
    os.environ.setdefault("BEACON_BASE_URL", "http://testserver")


@pytest.fixture
def in_memory_db():
    """Return a SQLAlchemy Session backed by an in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    # Seed Config row
    session.add(Config(
        id=1,
        global_budget_words=5000,
        global_budget_minutes=20,
        budget_mode=BudgetMode.words,
        wpm=250,
        parallel_slots=2,
        cadence_cron="0 8 * * *",
        thumbs_down_drop_threshold=3,
        feed_secret="test-secret",
    ))
    session.commit()
    yield session
    session.close()
    engine.dispose()
