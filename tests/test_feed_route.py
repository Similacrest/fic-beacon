"""The /feed/{channel}/{slot} route — slot-range validation (#6)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import ensure_default_channel, get_db
from app.models import Base, Channel, Config
from app.routers import feed


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Config(id=1, wpm=250, cadence_cron="0 8 * * *",
                  thumbs_down_drop_threshold=3, feed_secret="secret"))
    db.flush()
    ensure_default_channel(db)
    db.add(Channel(name="Fantasy", slug="fantasy", genre_match="", parallel_slots=3))
    db.commit()

    app = FastAPI()
    app.include_router(feed.router)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    db.close()
    engine.dispose()


def test_valid_slots_resolve(client):
    for key in ("1", "2", "3"):
        assert client.get(f"/feed/fantasy/{key}?token=secret").status_code == 200


@pytest.mark.parametrize("key", ["4", "0", "99", "abc", "-1"])
def test_out_of_range_or_nonnumeric_slot_404(client, key):
    assert client.get(f"/feed/fantasy/{key}?token=secret").status_code == 404


def test_slot_check_needs_valid_token_first(client):
    # A bad token is rejected (403) regardless of slot validity — no slot-existence leak.
    assert client.get("/feed/fantasy/4?token=wrong").status_code == 403


def test_unknown_channel_404(client):
    assert client.get("/feed/nope/1?token=secret").status_code == 404
