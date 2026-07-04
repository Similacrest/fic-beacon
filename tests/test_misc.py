"""Phase F: clear-dropped queue + configurable scheduler timezone."""
import secrets
import uuid
from zoneinfo import ZoneInfo

from app.models import (
    Book, BookStatus, Channel, Config, Drop, FeedbackAction, FeedbackEvent,
    WebSubSubscription,
)
from app.routers.admin import clear_dropped, regenerate_feed_secret


def _book(db, status, cid=1):
    channel_id = db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
    b = Book(calibre_id=cid, title="T", author="A", status=status,
             queue_position=cid, channel_id=channel_id)
    db.add(b)
    db.flush()
    return b


def _drop(db, book):
    d = Drop(
        book_id=book.id, word_count=1, chapter_start=0, chapter_end=0,
        chapter_titles="C", content_html="x",
        feedback_token=secrets.token_urlsafe(8), reader_slug=str(uuid.uuid4()),
    )
    db.add(d)
    db.flush()
    return d


class TestClearDropped:
    def test_removes_dropped_and_keeps_others(self, in_memory_db):
        kept = _book(in_memory_db, BookStatus.active, cid=1)
        gone = _book(in_memory_db, BookStatus.dropped, cid=2)
        d = _drop(in_memory_db, gone)
        in_memory_db.add(FeedbackEvent(
            token=d.feedback_token, book_id=gone.id, drop_id=d.id, action=FeedbackAction.down,
        ))
        in_memory_db.commit()

        clear_dropped(db=in_memory_db)

        assert in_memory_db.query(Book).filter_by(status=BookStatus.dropped).count() == 0
        assert in_memory_db.get(Book, kept.id) is not None
        assert in_memory_db.query(Drop).count() == 0          # dropped book's drops gone
        assert in_memory_db.query(FeedbackEvent).count() == 0  # and their feedback


class TestRegenerateFeedSecret:
    def test_rotates_secret_and_clears_subscriptions(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        old = cfg.feed_secret
        in_memory_db.add(WebSubSubscription(
            topic_url="http://testserver/feed/general/1",
            callback_url="http://reader/cb", verified=True,
        ))
        in_memory_db.commit()

        regenerate_feed_secret(db=in_memory_db)

        assert in_memory_db.get(Config, 1).feed_secret != old
        assert in_memory_db.query(WebSubSubscription).count() == 0


class TestStateHelpers:
    def test_run_stamp_roundtrip_and_missing(self, in_memory_db):
        from app.state import LAST_DROP_RUN, get_run, mark_run
        assert get_run(in_memory_db, LAST_DROP_RUN) is None
        mark_run(in_memory_db, LAST_DROP_RUN)
        in_memory_db.flush()
        stamped = get_run(in_memory_db, LAST_DROP_RUN)
        assert stamped is not None and stamped.tzinfo is not None


class TestTimezone:
    def test_returns_zoneinfo_when_set(self, monkeypatch):
        from app import scheduler
        monkeypatch.setattr(scheduler.settings, "tz", "Europe/Tallinn")
        assert scheduler._timezone() == ZoneInfo("Europe/Tallinn")

    def test_none_when_unset(self, monkeypatch):
        from app import scheduler
        monkeypatch.setattr(scheduler.settings, "tz", None)
        assert scheduler._timezone() is None

    def test_none_when_invalid(self, monkeypatch):
        from app import scheduler
        monkeypatch.setattr(scheduler.settings, "tz", "Not/AZone")
        assert scheduler._timezone() is None

    def test_now_is_timezone_aware(self, monkeypatch):
        # A naive now would get localised to the scheduler tz and land in the past; _now must
        # be tz-aware so schedule delays are honoured regardless of the container OS clock.
        from app import scheduler
        monkeypatch.setattr(scheduler.settings, "tz", "Europe/Tallinn")
        now = scheduler._now()
        assert now.tzinfo is not None and now.utcoffset() is not None


class TestFetchPollGrace:
    """A fetch poll can fire before the submitting cycle commits its fetch_job mapping. It must
    tolerate a few "not found yet" polls instead of unscheduling itself immediately — otherwise a
    story is stranded at `fetching…` forever (the scheduler timezone regression)."""

    def _use_session(self, monkeypatch, session):
        from contextlib import contextmanager
        from app import scheduler

        @contextmanager
        def fake_session():
            yield session
        monkeypatch.setattr(scheduler, "db_session", fake_session)

    def test_missing_mapping_tolerated_then_unschedules(self, in_memory_db, monkeypatch):
        from app import scheduler
        self._use_session(monkeypatch, in_memory_db)
        unscheduled = []
        monkeypatch.setattr(scheduler, "_unschedule_poll", unscheduled.append)
        scheduler._poll_misses.clear()
        jid = "deadbeef"  # no app_state fetch_job:{jid} row → get_value returns None

        for _ in range(scheduler._POLL_MISS_LIMIT - 1):
            scheduler._poll_fetch_job(jid)
        assert unscheduled == []                                    # tolerated, not given up yet
        assert scheduler._poll_misses[jid] == scheduler._POLL_MISS_LIMIT - 1

        scheduler._poll_fetch_job(jid)                              # the limit-th consecutive miss
        assert unscheduled == [jid]                                 # now it unschedules
