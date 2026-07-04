"""The /feed/{channel}/{slot} route — slot-range validation (#6)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import ensure_default_channel, get_db
from app.models import Base, Book, BookStatus, Channel, Config, Drop
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
    yield TestClient(app), db
    db.close()
    engine.dispose()


def _seed_drops(db, channel_slug, feed_key, n):
    ch = db.query(Channel).filter(Channel.slug == channel_slug).one()
    book = Book(calibre_id=1, title="B", author="A", status=BookStatus.active, channel_id=ch.id)
    db.add(book); db.flush()
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        db.add(Drop(book_id=book.id, channel_id=ch.id, feed_key=feed_key,
                    chapter_start=i, chapter_end=i, word_count=100,
                    feedback_token=f"t{i}", reader_slug=f"s{i}",
                    published_at=base + timedelta(hours=i)))
    db.commit()
    return ch


def test_valid_slots_resolve(client):
    tc, _ = client
    for key in ("1", "2", "3"):
        assert tc.get(f"/feed/fantasy/{key}?token=secret").status_code == 200


@pytest.mark.parametrize("key", ["4", "0", "99", "abc", "-1"])
def test_out_of_range_or_nonnumeric_slot_404(client, key):
    tc, _ = client
    assert tc.get(f"/feed/fantasy/{key}?token=secret").status_code == 404


def test_slot_check_needs_valid_token_first(client):
    tc, _ = client
    # A bad token is rejected (403) regardless of slot validity — no slot-existence leak.
    assert tc.get("/feed/fantasy/4?token=wrong").status_code == 403


def test_unknown_channel_404(client):
    tc, _ = client
    assert tc.get("/feed/nope/1?token=secret").status_code == 404


def test_feed_item_limit_caps_items(client):
    """A channel's feed_item_limit caps how many (newest) items the slot feed carries."""
    import feedparser
    tc, db = client
    ch = _seed_drops(db, "fantasy", "1", n=5)
    ch.feed_item_limit = 2
    db.commit()

    body = tc.get("/feed/fantasy/1?token=secret").text
    assert len(feedparser.parse(body).entries) == 2   # only the 2 newest served
    # DB still holds all 5 rows (kept, not trimmed).
    assert db.query(Drop).filter(Drop.feed_key == "1").count() == 5
