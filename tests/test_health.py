"""Dashboard health (S1) and run progress/outcome (S15)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import health, progress, scheduler
from app.models import Book, BookStatus, Channel, Drop, utcnow
from app.state import LAST_RUN_RESULT, get_value
from app.timefmt import compact, duration
from tests.test_story_page import _book, ctx  # noqa: F401  (fixture)


def _drop(db, book, when, words=1000, acked=True, c0=0, c1=0):
    d = Drop(book_id=book.id, channel_id=book.channel_id, feed_key="1", word_count=words,
             chapter_start=c0, chapter_end=c1, chapter_titles="C", content_html="x",
             feedback_token=f"t{db.query(Drop).count()}", reader_slug=f"s{db.query(Drop).count()}",
             published_at=when, acknowledged_at=utcnow() if acked else None)
    db.add(d)
    db.flush()
    return d


class TestFormatting:
    def test_compact(self):
        assert [compact(n) for n in (0, 999, 1000, 1234, 9999, 10000, 42300, 1500000, None)] == \
            ["0", "999", "1k", "1.2k", "10k", "10k", "42k", "1.5M", "0"]

    def test_duration(self):
        assert [duration(n) for n in (5, 75, 3700, None)] == ["5s", "1m 15s", "1h 01m", ""]


class TestVolumeSeries:
    def test_buckets_by_local_day_and_totals(self, in_memory_db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "tz", "Europe/Tallinn")  # UTC+3 in October
        b = _book(in_memory_db, tracked=False)
        now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
        _drop(in_memory_db, b, now - timedelta(hours=1), words=2000, c0=3, c1=4)      # today
        _drop(in_memory_db, b, datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc), words=500)  # 01:30 on the 13th, local
        _drop(in_memory_db, b, now - timedelta(days=3), words=700)
        _drop(in_memory_db, b, now - timedelta(days=40), words=9999)                  # outside
        v = health.volume_series(in_memory_db, days=14, now=now)
        assert len(v["bars"]) == 14 and v["bars"][-1]["today"]
        assert v["bars"][-1]["words"] == 2500 and v["bars"][-1]["chapters"] == 3
        assert v["total_words"] == 3200 and v["peak_words"] == 2500
        assert v["bars"][-1]["pct"] == 100 and v["bars"][-4]["words"] == 700

    def test_empty(self, in_memory_db):
        v = health.volume_series(in_memory_db)
        assert v["total_words"] == 0 and v["peak_date"] is None
        assert all(b["pct"] == 0 for b in v["bars"])

    def test_unread_counts_recent_active_only(self, in_memory_db):
        a = _book(in_memory_db, tracked=False, calibre_id=1)
        gone = _book(in_memory_db, tracked=False, calibre_id=2, status=BookStatus.dropped)
        now = utcnow()
        _drop(in_memory_db, a, now - timedelta(days=1), acked=False)
        _drop(in_memory_db, a, now - timedelta(days=1), acked=True)
        _drop(in_memory_db, a, now - timedelta(days=30), acked=False)
        _drop(in_memory_db, gone, now - timedelta(days=1), acked=False)
        assert health.unread_count(in_memory_db) == 1


def _attn(db, **kw):
    base = dict(book_notes={}, last_skips=[], fetch_progress=[], push_bad=0, push_orphans=0,
                last_run=None, schedules_enabled=1, cycle_running=False)
    base.update(kw)
    return health.attention_items(db, **base)


class TestAttention:
    def test_healthy_is_empty(self, in_memory_db):
        _book(in_memory_db, tracked=False, slot_index=1)
        assert _attn(in_memory_db) == []

    def test_each_problem(self, in_memory_db):
        fetchfail = _book(in_memory_db, "A", calibre_id=1, tracked=True, last_fetch_status="error: HTTP 503")
        aside = _book(in_memory_db, "B", calibre_id=2, tracked=False, paused=True)
        gone = _book(in_memory_db, "C", calibre_id=3, tracked=False)
        slow = _book(in_memory_db, "D", calibre_id=4, tracked=True, last_fetch_status="fetching…")
        paused_err = _book(in_memory_db, "E", calibre_id=5, tracked=True, paused=True,
                           last_fetch_status="error: x")
        items = _attn(
            in_memory_db,
            book_notes={aside.id: "EPUB has no chapters"},
            last_skips=[{"book_id": gone.id, "missing": True}],
            fetch_progress=[{"book": slow, "elapsed": 40 * 60}],
            push_bad=2, push_orphans=1, schedules_enabled=0,
            last_run={"op": "release", "error": "OperationalError: locked"},
        )
        text = "\n".join(i["text"] for i in items)
        assert items[0]["tone"] == "bad" and "Release failed: OperationalError" in items[0]["text"]
        assert "No release schedule is enabled" in text
        assert "B: EPUB has no chapters" in text and "A: fetch failed — HTTP 503" in text
        assert "C: not found in Calibre" in text and "D: download running 40 min" in text
        assert "2 reader subscription(s)" in text and "1 subscription(s) on feeds" in text
        assert "E:" not in text  # a paused story's old error isn't an alert
        assert len([i for i in items if "B:" in i["text"]]) == 1  # note not repeated as a fetch error

    def test_failure_hidden_while_a_new_run_is_in_flight(self, in_memory_db):
        items = _attn(in_memory_db, last_run={"op": "release", "error": "boom"}, cycle_running=True)
        assert items == []


class TestRunRecording:
    @pytest.fixture(autouse=True)
    def _db(self, in_memory_db, monkeypatch):
        from contextlib import contextmanager

        @contextmanager
        def fake_session():
            yield in_memory_db
        monkeypatch.setattr(scheduler, "db_session", fake_session)
        self.db = in_memory_db
        monkeypatch.setattr(scheduler, "_active_op", None)

    def test_success_is_recorded_per_op_and_clears_stage(self):
        def fn():
            progress.set_stage("Planning X (1/2)…")
            assert scheduler.cycle_status()["stage"] == "Planning X (1/2)…"
            return {"chapters": 4, "words": 5000}
        scheduler._execute("release", fn)
        rec = json.loads(get_value(self.db, f"{LAST_RUN_RESULT}:release"))
        assert rec["chapters"] == 4 and rec["error"] is None and rec["op"] == "release"
        assert get_value(self.db, f"{LAST_RUN_RESULT}:feedcheck") is None
        assert scheduler.cycle_status() is None and progress.get_stage() == ""

    def test_failure_is_recorded_and_lock_released(self):
        def boom():
            raise RuntimeError("calibre locked")
        with pytest.raises(RuntimeError):
            scheduler._execute("release", boom)
        rec = json.loads(get_value(self.db, f"{LAST_RUN_RESULT}:release"))
        assert rec["error"] == "RuntimeError: calibre locked"
        assert scheduler._cycle_lock.acquire(blocking=False)  # freed, not stuck
        scheduler._cycle_lock.release()


def _schedule(db, enabled=True):
    from app.models import BudgetMode, Schedule
    db.add(Schedule(name="Evening", cron="0 19 * * *", budget=5000, budget_mode=BudgetMode.words,
                    enabled=enabled, sort_order=1))
    db.commit()


class TestDashboardHealth:
    def test_healthy_dashboard(self, ctx):
        client, db = ctx
        _schedule(db)
        b = _book(db, "Mistborn", tracked=False, slot_index=1, total_chapters=10)
        _drop(db, b, utcnow() - timedelta(hours=2), words=3200)
        db.commit()
        t = client.get("/admin/").text
        assert "All healthy" in t and "3.2k</strong> words" in t
        assert "▶ Run release" in t and "🔄 Check feeds" in t
        assert 'class="attention"' not in t

    def test_attention_and_run_outcome_shown(self, ctx):
        client, db = ctx
        from app.state import set_value
        _schedule(db)
        b = _book(db, "Serial", tracked=True, slot_index=1, last_fetch_status="error: HTTP 503")
        set_value(db, f"{LAST_RUN_RESULT}:release", json.dumps(
            {"op": "release", "error": None, "seconds": 75, "schedule": "Evening", "chapters": 4,
             "words": 5100, "downloads": 2, "feeds_changed": 1}))
        db.commit()
        t = client.get("/admin/").text
        assert "1 needs attention" in t and f'href="/admin/books/{b.id}"' in t
        import re
        assert "Last run: Evening — 1m 15s, 3 downloads queued" in re.sub(r"\s+", " ", t)

    def test_failed_run_is_surfaced(self, ctx):
        client, db = ctx
        from app.state import set_value
        _schedule(db)
        _book(db, "Serial", tracked=False, slot_index=1)
        set_value(db, f"{LAST_RUN_RESULT}:release", json.dumps(
            {"op": "release", "error": "OperationalError: database is locked", "seconds": 3}))
        db.commit()
        t = client.get("/admin/").text
        assert "Release failed: OperationalError: database is locked" in t
        assert "Last run failed" in t

    def test_banner_shows_stage_and_elapsed(self, ctx, monkeypatch):
        client, _ = ctx
        monkeypatch.setattr(scheduler, "_active_op", {"op": "release", "started": scheduler._now()})
        progress.set_stage("Planning Fantasy (1/2)…")
        try:
            t = client.get("/admin/").text
        finally:
            progress.clear()
        assert "Release running" in t and "Planning Fantasy (1/2)…" in t
        assert "disabled" in t


class TestGaveUpAttention:
    def test_gave_up_feed_entry_is_surfaced_until_chapters_land(self, in_memory_db):
        from app.state import FETCH_GIVEUP_PREFIX, set_value
        b = _book(in_memory_db, "AGARES", calibre_id=1, tracked=True, last_fetch_status="ok")
        assert _attn(in_memory_db) == []
        set_value(in_memory_db, FETCH_GIVEUP_PREFIX + str(b.id), '{"guid": "g", "at": "2026-11-01T00:00:00+00:00"}')
        items = _attn(in_memory_db)
        assert len(items) == 1 and items[0]["href"] == f"/admin/books/{b.id}"
        assert "AGARES" in items[0]["text"] and "gave up" in items[0]["text"]
