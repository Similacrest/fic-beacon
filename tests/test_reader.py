"""The /read/{slug} reader page — including the feedback action row (1.1.C)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import ensure_default_channel, get_db
from app.models import Base, Book, BookStatus, Channel, Config, Drop
from app.routers import reader


@pytest.fixture
def client():
    # StaticPool → one connection shared across threads, so the TestClient's worker thread
    # sees the same in-memory DB the fixture seeded (unlike the default per-thread pool).
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Config(id=1, wpm=250, cadence_cron="0 8 * * *",
                  thumbs_down_drop_threshold=3, feed_secret="test-secret"))
    db.flush()
    ensure_default_channel(db)
    db.commit()

    app = FastAPI()
    app.include_router(reader.router)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    db.close()
    engine.dispose()


def _seed_drop(db, *, total_chapters=5, cursor=1) -> Drop:
    channel_id = db.query(Channel.id).order_by(Channel.id).first()[0]
    book = Book(
        calibre_id=1, title="A Book", author="An Author", status=BookStatus.active,
        channel_id=channel_id, total_chapters=total_chapters, cursor_chapter_index=cursor,
    )
    db.add(book)
    db.flush()
    drop = Drop(
        book_id=book.id, word_count=500, chapter_start=0, chapter_end=0,
        chapter_titles="Chapter 1", content_html="<p>Once upon a time.</p>",
        feedback_token="tok123", reader_slug="slug123",
    )
    db.add(drop)
    db.commit()
    return drop


def test_reader_page_renders_content_and_feedback(client):
    tc, db = client
    _seed_drop(db)
    resp = tc.get("/read/slug123")
    assert resp.status_code == 200
    body = resp.text
    assert "Once upon a time." in body                       # the chapter content
    assert "/fb/tok123?action=up" in body                    # 👍 instant vote
    assert "/fb/tok123?action=down" in body                  # 👎 instant vote
    assert "/fb/confirm/tok123?action=drop" in body          # ❌ drop (confirm)
    assert "🪝" in body                                       # extra available (cursor 1 < 5)


def test_reader_hides_extra_when_caught_up(client):
    tc, db = client
    _seed_drop(db, total_chapters=5, cursor=5)               # at the end → no next unit
    body = tc.get("/read/slug123").text
    assert "🪝" not in body
    assert "/fb/tok123?action=up" in body                    # other actions still present


def test_reader_unknown_slug_404(client):
    tc, _ = client
    assert tc.get("/read/nope").status_code == 404
