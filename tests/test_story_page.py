"""The per-story admin page (S6) and the book actions that return to it."""
import pytest
from fastapi.testclient import TestClient

from app.models import BookStatus, Channel, Drop, FeedbackAction, FeedbackEvent


@pytest.fixture
def ctx(monkeypatch):
    """(client, db) over a StaticPool in-memory DB — the TestClient serves on another thread."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.config import settings
    from app.database import ensure_default_channel, get_db
    from app.main import app
    from app.models import Base, Config
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Config(id=1, wpm=250, feed_secret="test-secret"))
    db.flush()
    ensure_default_channel(db)
    db.commit()
    monkeypatch.setattr(settings, "admin_open", True)
    monkeypatch.setattr(settings, "proxy_token", None)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    app.dependency_overrides.pop(get_db, None)
    db.close()
    engine.dispose()


def _book(db, title="Story", **kw):
    from tests.test_planner import _make_book
    b = _make_book(db, calibre_id=kw.pop("calibre_id", 1), title=title)
    for k, v in kw.items():
        setattr(b, k, v)
    db.commit()
    return b


def _drop(db, book, acked):
    from app.models import utcnow
    d = Drop(book_id=book.id, channel_id=book.channel_id, feed_key="1", word_count=1234,
             chapter_start=0, chapter_end=0, chapter_titles="Chapter One", content_html="x",
             feedback_token=f"t{book.id}{acked}", reader_slug=f"s{book.id}{acked}",
             acknowledged_at=utcnow() if acked else None)
    db.add(d)
    db.flush()
    return d


class TestStoryPage:
    def test_active_backlog_book(self, ctx):
        client, db = ctx
        b = _book(db, "Mistborn", slot_index=1, total_chapters=20, cursor_chapter_index=5)
        d = _drop(db, b, acked=False)
        db.add(FeedbackEvent(token="t", book_id=b.id, drop_id=d.id, action=FeedbackAction.up))
        db.add(FeedbackEvent(token="t", book_id=b.id, drop_id=d.id, action=FeedbackAction.read))
        db.commit()
        page = client.get(f"/admin/books/{b.id}")
        assert page.status_code == 200
        t = page.text
        assert "Mistborn" in t and "On air · 15 waiting" in t
        assert "⏸ Pause" in t and "❌ Drop" in t and "Re-queue" not in t
        assert 'name="next" value="/admin/books/%d"' % b.id in t
        assert "Chapter One" in t and "unread" in t
        assert "👍" in t and t.count('class="pill"') == 1  # 'read' acks aren't listed as feedback

    def test_tracked_story_has_fetch_now(self, ctx):
        client, db = ctx
        b = _book(db, "Serial", tracked=True, slot_index=1, last_fetch_status="error: HTTP 503")
        t = client.get(f"/admin/books/{b.id}").text
        assert "⤓ Fetch now" in t and f"/admin/ongoing/{b.id}/fetch-now" in t
        assert "HTTP 503" in t and "Stop tracking updates" in t

    def test_queued_book_shows_position_and_arrows(self, ctx):
        client, db = ctx
        _book(db, "First", calibre_id=1, status=BookStatus.queued, queue_position=1)
        b = _book(db, "Second", calibre_id=2, status=BookStatus.queued, queue_position=2)
        t = client.get(f"/admin/books/{b.id}").text
        assert "Queued #2" in t and "Queue position #2" in t

    def test_dropped_book_offers_requeue_only(self, ctx):
        client, db = ctx
        b = _book(db, "Gone", status=BookStatus.dropped)
        t = client.get(f"/admin/books/{b.id}").text
        assert "↺ Bring back" in t and "⏮ Start over" in t and "❌ Drop" not in t and "Placement" not in t

    def test_unknown_book_404(self, ctx):
        client, _ = ctx
        assert client.get("/admin/books/999").status_code == 404

    def test_slot_picker_for_multi_slot_channel(self, ctx):
        client, db = ctx
        ch = db.query(Channel).first()
        ch.parallel_slots = 3
        b = _book(db, "Mistborn", slot_index=1)
        t = client.get(f"/admin/books/{b.id}").text
        assert f"Slot 3 — /feed/{ch.slug}/3" in t
        assert 'step="any"' in t  # weights like 1.1 (from a custom vote step) must still save


class TestActionsReturnToTheStory:
    def test_pause_returns_to_story_page(self, ctx):
        client, db = ctx
        b = _book(db, slot_index=1)
        r = client.post(f"/admin/books/{b.id}/pause", data={"next": f"/admin/books/{b.id}"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/admin/books/{b.id}"

    @pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example/admin/",
                                     "/admin//evil", "/feed/x"])
    def test_next_never_leaves_admin(self, ctx, bad):
        client, db = ctx
        b = _book(db, slot_index=1)
        r = client.post(f"/admin/books/{b.id}/pause", data={"next": bad}, follow_redirects=False)
        assert r.headers["location"] == "/admin/"

    def test_without_next_goes_to_dashboard(self, ctx):
        client, db = ctx
        b = _book(db, slot_index=1)
        r = client.post(f"/admin/books/{b.id}/drop", follow_redirects=False)
        assert r.headers["location"] == "/admin/"

    def test_weight_plain_submit_returns_to_story(self, ctx):
        client, db = ctx
        b = _book(db, slot_index=1)
        r = client.post(f"/admin/books/{b.id}/set-weight",
                        data={"weight": "2.5", "next": f"/admin/books/{b.id}"}, follow_redirects=False)
        assert r.headers["location"] == f"/admin/books/{b.id}"
        db.refresh(b)
        assert b.quota_weight == 2.5


class TestQueueMoveStaysInChannel:
    def test_move_up_skips_other_channels(self, ctx):
        client, db = ctx
        other = Channel(name="Other", slug="other", parallel_slots=1, weight=1.0)
        db.add(other)
        db.flush()
        a = _book(db, "A", calibre_id=1, status=BookStatus.queued, queue_position=1)
        x = _book(db, "X", calibre_id=2, status=BookStatus.queued, queue_position=2, channel_id=other.id)
        b = _book(db, "B", calibre_id=3, status=BookStatus.queued, queue_position=3)
        client.post(f"/admin/books/{b.id}/move", data={"direction": "up"})
        db.refresh(a), db.refresh(b), db.refresh(x)
        assert (b.queue_position, a.queue_position, x.queue_position) == (1, 3, 2)


class TestChannelMove:
    def test_tracked_story_is_placed_in_the_new_channel_at_once(self, ctx):
        client, db = ctx
        other = Channel(name="Other", slug="other", parallel_slots=2, weight=1.0)
        db.add(other)
        db.commit()
        b = _book(db, tracked=True, slot_index=1)
        client.post(f"/admin/books/{b.id}/set-channel", data={"channel_id": other.id})
        db.refresh(b)
        assert b.channel_id == other.id and b.slot_index in (1, 2)


class TestChannelMoveBacklog:
    def test_streaming_book_joins_the_back_of_the_new_queue(self, ctx):
        client, db = ctx
        other = Channel(name="Other", slug="other", parallel_slots=1, weight=1.0)
        db.add(other)
        db.flush()
        mover = _book(db, "Mover", calibre_id=1, slot_index=1, queue_position=1)
        nxt = _book(db, "Next up", calibre_id=2, status=BookStatus.queued, queue_position=2)
        there = _book(db, "Streaming there", calibre_id=3, slot_index=1, queue_position=9,
                      channel_id=other.id)
        client.post(f"/admin/books/{mover.id}/set-channel", data={"channel_id": other.id})
        for b in (mover, nxt, there):
            db.refresh(b)
        assert there.status == BookStatus.active and there.slot_index == 1  # not pushed out
        assert mover.status == BookStatus.queued and mover.queue_position > there.queue_position
        assert nxt.status == BookStatus.active and nxt.slot_index == 1  # old slot refilled

    def test_moved_into_a_free_slot_streams_at_once(self, ctx):
        client, db = ctx
        other = Channel(name="Empty", slug="empty", parallel_slots=1, weight=1.0)
        db.add(other)
        db.flush()
        mover = _book(db, "Mover", calibre_id=1, slot_index=1)
        client.post(f"/admin/books/{mover.id}/set-channel", data={"channel_id": other.id})
        db.refresh(mover)
        assert mover.status == BookStatus.active and mover.slot_index == 1


class TestTimes:
    def test_tooltip_uses_beacon_tz(self, monkeypatch):
        from datetime import datetime
        from app import timefmt
        from app.config import settings
        monkeypatch.setattr(settings, "tz", "Europe/Tallinn")
        assert timefmt.absolute(datetime(2026, 1, 1, 12, 0)) == "2026-01-01 14:00 Europe/Tallinn"
        monkeypatch.setattr(settings, "tz", None)
        assert timefmt.absolute(datetime(2026, 1, 1, 12, 0)) == "2026-01-01 12:00 UTC"

    def test_relative(self):
        from datetime import datetime, timedelta, timezone
        from app.timefmt import relative
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        assert relative(None) == "never"
        assert relative(now - timedelta(hours=5), now) == "5h ago"
        assert relative(now + timedelta(days=2), now) == "in 2d"
        assert relative(now - timedelta(seconds=10), now) == "just now"


class TestPushIndicators:
    def _sub(self, db, topic, verified=True, lease_days=5):
        from datetime import timedelta
        from app.models import WebSubSubscription, utcnow
        db.add(WebSubSubscription(topic_url=topic, callback_url="https://cb.example/x", verified=verified,
                                  lease_expires_at=utcnow() + timedelta(days=lease_days)))
        db.commit()

    def test_per_slot_indicator_and_no_subscriber_table(self, ctx):
        client, db = ctx
        ch = db.query(Channel).first()
        ch.parallel_slots = 2
        db.commit()
        _book(db, "Mistborn", slot_index=1)
        self._sub(db, f"https://funnel.example/feed/{ch.slug}/1?token=t")
        self._sub(db, f"https://old.example/feed/{ch.slug}/1", lease_days=-1)   # expired, other host
        self._sub(db, "https://funnel.example/feed/gone/1?token=t")             # orphan
        t = client.get("/admin/").text
        assert "⚡ 1" in t and "⚠ 1 stale" in t and "polling" in t  # slot 1 push, slot 2 polling
        assert "1 subscription(s) on feeds that no longer exist" in t
        assert "WebSub subscribers (realtime push)" not in t


class TestDashboard:
    def test_titles_link_to_story_and_old_tables_are_gone(self, ctx):
        client, db = ctx
        b = _book(db, "Mistborn", slot_index=1, total_chapters=20)
        q = _book(db, "Elantris", calibre_id=2, status=BookStatus.queued, queue_position=5)
        t = client.get("/admin/").text
        assert f'href="/admin/books/{b.id}"' in t and f'href="/admin/books/{q.id}"' in t
        assert "📚 Active sources" not in t and "📋 Queue <span" not in t


class TestUndoAndRestore:
    def _drops(self, db, book, n, acked):
        from app.models import utcnow
        for i in range(n):
            db.add(Drop(book_id=book.id, channel_id=book.channel_id, feed_key="1", word_count=100,
                        chapter_start=i, chapter_end=i, chapter_titles=f"C{i}", content_html="x",
                        feedback_token=f"u{book.id}{i}{acked}", reader_slug=f"v{book.id}{i}{acked}",
                        acknowledged_at=utcnow() if acked else None))
        db.commit()

    def test_mark_all_read_lifts_the_slowdown_and_keeps_weight(self, ctx):
        from app.planner.planner import unread_streak
        client, db = ctx
        b = _book(db, "Serial", slot_index=1, quota_weight=1.5)
        self._drops(db, b, 3, acked=False)
        assert unread_streak(db, b) == 3
        assert "✓ Mark 3 read" in client.get(f"/admin/books/{b.id}").text
        client.post(f"/admin/books/{b.id}/mark-read", data={"next": f"/admin/books/{b.id}"})
        db.refresh(b)
        assert unread_streak(db, b) == 0 and b.quota_weight == 1.5
        assert "✓ Mark" not in client.get(f"/admin/books/{b.id}").text

    def test_cursor_move_can_be_undone_once(self, ctx):
        client, db = ctx
        b = _book(db, "Serial", slot_index=1, total_chapters=20, cursor_chapter_index=7)
        client.post(f"/admin/books/{b.id}/set-cursor", data={"chapter_index": "15"})
        db.refresh(b)
        assert b.cursor_chapter_index == 15
        assert "back to chapter 7" in client.get(f"/admin/books/{b.id}").text
        client.post(f"/admin/books/{b.id}/cursor-undo")
        db.refresh(b)
        assert b.cursor_chapter_index == 7
        assert "back to chapter" not in client.get(f"/admin/books/{b.id}").text   # one step only
        client.post(f"/admin/books/{b.id}/cursor-undo")                          # harmless repeat
        db.refresh(b)
        assert b.cursor_chapter_index == 7

    def test_undo_expires_once_the_cursor_has_moved_on(self, ctx):
        client, db = ctx
        b = _book(db, "Serial", slot_index=1, total_chapters=20, cursor_chapter_index=7)
        client.post(f"/admin/books/{b.id}/set-cursor", data={"chapter_index": "15"})
        b.cursor_chapter_index = 16          # a release delivered a chapter
        db.commit()
        assert "back to chapter" not in client.get(f"/admin/books/{b.id}").text
        client.post(f"/admin/books/{b.id}/cursor-undo")
        db.refresh(b)
        assert b.cursor_chapter_index == 16

    def test_unchanged_cursor_records_no_undo(self, ctx):
        client, db = ctx
        b = _book(db, "Serial", slot_index=1, total_chapters=20, cursor_chapter_index=7)
        client.post(f"/admin/books/{b.id}/set-cursor", data={"chapter_index": "7"})
        assert "back to chapter" not in client.get(f"/admin/books/{b.id}").text

    def test_restore_keeps_position_weight_and_votes(self, ctx):
        client, db = ctx
        b = _book(db, "Serial", tracked=True, slot_index=1, cursor_chapter_index=9, total_chapters=20,
                  quota_weight=2.25, thumbs_up=4, status=BookStatus.dropped)
        client.post(f"/admin/books/{b.id}/restore")
        db.refresh(b)
        assert b.status == BookStatus.active and b.slot_index is not None
        assert (b.cursor_chapter_index, b.quota_weight, b.thumbs_up) == (9, 2.25, 4)

    def test_restore_backlog_book_streams_when_a_slot_is_free(self, ctx):
        client, db = ctx
        b = _book(db, "Mistborn", tracked=False, status=BookStatus.dropped, cursor_chapter_index=3,
                  quota_weight=0.0)
        client.post(f"/admin/books/{b.id}/restore")
        db.refresh(b)
        assert b.status == BookStatus.active and b.slot_index == 1
        assert b.cursor_chapter_index == 3 and b.quota_weight == 1.0   # a 0 weight would never post

    def test_restore_ignores_books_that_are_not_dropped(self, ctx):
        client, db = ctx
        b = _book(db, "Live", slot_index=1, quota_weight=0.5)
        client.post(f"/admin/books/{b.id}/restore")
        db.refresh(b)
        assert b.status == BookStatus.active and b.quota_weight == 0.5
