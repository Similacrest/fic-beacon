"""The dashboard's per-source "why" (app/planner/explain.py) and the skip-log facts behind it."""
import json
import random
from unittest.mock import MagicMock

import pytest

from app.models import Book, BookStatus
from app.planner.explain import explain
from tests.test_planner import epub_path  # noqa: F401  (fixture)


def _b(**kw) -> Book:
    fields = dict(
        title="T", author="A", tracked=False, status=BookStatus.active, paused=False,
        calibre_id=1, cooldown_remaining=0, quota_weight=1.0, total_chapters=10,
        cursor_chapter_index=4, fresh_from_index=None, last_fetch_status=None,
    )
    fields.update(kw)
    return Book(**fields)


HELD = {"held_out": True, "dropped": 0, "remaining": 6, "next_words": 2000,
        "oversized": False, "fresh": False, "penalty": 1.0, "fade": 1.0,
        "budget": 5000, "base": 5000, "used": 4000}


def _held(**kw):
    return {**HELD, **kw}


class TestExplainLiveState:
    def test_paused(self):
        assert explain(_b(paused=True)).text == "Paused"

    def test_paused_with_set_aside_note_shows_the_note(self):
        s = explain(_b(paused=True), note="EPUB has no chapters")
        assert s.tone == "warn" and s.text == "EPUB has no chapters"

    def test_queued_shows_position(self):
        s = explain(_b(status=BookStatus.queued), queue_pos=3)
        assert s.text == "Queued — #3 in line"

    def test_first_download_states(self):
        assert "Waiting for its first download" == explain(_b(tracked=True, calibre_id=None)).text
        assert "first time" in explain(_b(tracked=True, calibre_id=None,
                                           last_fetch_status="fetching: download")).text
        s = explain(_b(tracked=True, calibre_id=None, last_fetch_status="error: 404"))
        assert s.tone == "warn" and s.detail == "404"

    def test_cooldown_boundary(self):
        # cooldown is ticked *before* selection: 2 sits out the next release, 1 already sat out.
        assert explain(_b(cooldown_remaining=2)).text == "Sitting out the next release"
        s = explain(_b(cooldown_remaining=1))
        assert s.tone == "ok" and "back next time" in s.text

    def test_weight_zero(self):
        assert explain(_b(quota_weight=0)).text == "Weight 0 — never posts"

    def test_caught_up_tracked(self):
        assert "waiting for the author" in explain(_b(tracked=True, cursor_chapter_index=10)).text
        s = explain(_b(tracked=True, cursor_chapter_index=10, last_fetch_status="error: timeout"))
        assert s.tone == "warn" and "last fetch failed" in s.text

    def test_fresh_chapter(self):
        s = explain(_b(tracked=True, fresh_from_index=4))
        assert s.icon == "🆕" and "next release" in s.text

    def test_live_unread_streak(self):
        assert "3 unread drops" in explain(_b(), unread=3).text
        assert explain(_b(), unread=1).text == "On air — 6 waiting"  # one unread is normal

    def test_unknown_chapter_count_is_not_all_out(self):
        # A just-promoted backlog book (or fresh import) isn't chapterized until the next release.
        assert "next release" in explain(_b(total_chapters=None)).text
        assert "next release" in explain(_b(tracked=True, total_chapters=None)).text

    def test_missing_calibre_entry(self):
        assert explain(_b(), {"missing": True}).text == "Not found in Calibre"


class TestExplainLastRelease:
    def test_oversized(self):
        s = explain(_b(), _held(oversized=True, next_words=9000, base=3000))
        assert s.text == "Saving up for a 9,000-word chapter" and "3,000" in s.detail

    def test_partial_drop(self):
        assert explain(_b(), _held(held_out=False, dropped=2)).text.startswith("Dropped 2 chapters")

    def test_unread_backoff_still_unread(self):
        s = explain(_b(), _held(penalty=0.6), unread=3)
        assert s.text.startswith("Held back: 3 unread drops") and "×0.6" in s.detail

    def test_unread_backoff_since_read(self):
        # The reason is the recorded one, even though the streak has since cleared.
        assert "Held back last release for unread drops" in explain(_b(), _held(penalty=0.6)).text

    def test_weight_fade(self):
        assert "Low weight" in explain(_b(), _held(fade=0.5)).text

    def test_budget_ran_out_vs_lost_draw(self):
        assert explain(_b(), _held(used=4000, next_words=2000)).text.startswith("Budget ran out")
        assert explain(_b(), _held(used=1000, next_words=2000)).text.startswith("Lost the draw")

    def test_fresh_did_not_fit(self):
        assert "didn't fit" in explain(_b(tracked=True, fresh_from_index=4), _held(fresh=True)).text

    def test_old_entry_without_new_fields(self):
        old = {"channel": "General", "title": "T", "held_out": True, "dropped": 0, "remaining": 6}
        assert explain(_b(), old).text.startswith("Lost the draw")


class TestSkipLogFacts:
    def test_plan_drops_records_oversized_and_multipliers(self, in_memory_db, epub_path):
        from app.calibre.adapter import CalibreBook
        from app.planner.planner import _plan_drops
        from tests.test_planner import _make_book
        random.seed(0)
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=0.5)
        adapter = MagicMock()
        adapter.get_book.return_value = CalibreBook(
            calibre_id=1, title="T", author="A", path="A/T (1)", epub_name="T - A", source_url=None,
        )
        adapter.epub_path.return_value = epub_path
        skips = []
        # Base budget smaller than any chapter → oversized; nothing saved up → held.
        _plan_drops([book], adapter, budget=10, base_budget=10, skips_out=skips,
                    penalties={book.id: 0.6}, weight_skip_floor=1.0)
        (s,) = skips
        assert s.oversized and s.next_words > 10
        assert s.penalty == 0.6 and s.fade == 0.5

    def test_release_cycle_log_carries_book_id_and_budget(self, in_memory_db, epub_path, monkeypatch):
        from app.calibre.adapter import CalibreBook
        from app.planner import planner
        from app.state import LAST_SKIPS, get_value
        from tests.test_planner import _make_book
        random.seed(1)
        books = [_make_book(in_memory_db, calibre_id=i, title=f"B{i}", queue_position=i)
                 for i in (1, 2)]
        adapter = MagicMock()
        adapter.get_book.side_effect = lambda cid: None if cid == 2 else CalibreBook(
            calibre_id=1, title="T", author="A", path="A/T (1)", epub_name="T - A", source_url=None,
        )
        adapter.epub_path.return_value = epub_path
        monkeypatch.setattr(planner, "CalibreAdapter", lambda path: adapter)
        planner.run_release_cycle(in_memory_db, epub_path.parent)
        log = json.loads(get_value(in_memory_db, LAST_SKIPS))
        missing = [e for e in log if e.get("missing")]
        assert [e["book_id"] for e in missing] == [books[1].id]
        for e in log:
            if not e.get("missing"):
                assert e["book_id"] == books[0].id and {"budget", "used", "base"} <= e.keys()


class TestDashboardRendersWhy:
    @pytest.fixture
    def db(self):
        """StaticPool: the TestClient serves on another thread and must see the same in-memory DB."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.database import ensure_default_channel
        from app.models import Base, Config
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        session.add(Config(id=1, wpm=250, feed_secret="test-secret"))
        session.flush()
        ensure_default_channel(session)
        yield session
        session.close()
        engine.dispose()

    def test_slot_rows_and_queue_show_reasons(self, db, monkeypatch):
        from fastapi.testclient import TestClient

        from app.config import settings
        from app.database import get_db
        from app.main import app
        from app.state import LAST_SKIPS, set_value
        from tests.test_planner import _make_book
        in_memory_db = db
        a = _make_book(in_memory_db, calibre_id=1, title="Streaming Book")
        a.slot_index, a.total_chapters, a.cursor_chapter_index = 1, 10, 2
        q = _make_book(in_memory_db, calibre_id=2, title="Waiting Book",
                       status=BookStatus.queued, queue_position=2)
        set_value(in_memory_db, LAST_SKIPS, json.dumps([_held(book_id=a.id, title=a.title,
                                                              oversized=True, next_words=9000)]))
        in_memory_db.commit()
        monkeypatch.setattr(settings, "admin_open", True)
        monkeypatch.setattr(settings, "proxy_token", None)
        app.dependency_overrides[get_db] = lambda: in_memory_db
        try:
            page = TestClient(app).get("/admin/")
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert page.status_code == 200
        assert "Saving up for a 9,000-word chapter" in page.text
        assert f'data-book-id="{a.id}"' in page.text  # still draggable
        assert "Queued — #1 in line" in page.text and q.title in page.text
        assert "Next broadcast — waiting" not in page.text
