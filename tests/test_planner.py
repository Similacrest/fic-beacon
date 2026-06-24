"""Tests for the Budget / Drop Planner.

Pure-stochastic budget invariants:
  - The cycle total stays near the budget (not N × budget); there is no phase-1
    guarantee, so a source may get nothing some cycles.
  - A unit larger than the whole budget is posted whole (it could never fit otherwise).
  - Weight biases a source's per-unit inclusion probability upward.

Uses the in-memory DB fixture and mock EPUB fixtures.
"""
import os
import random
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.models import Book, BookStatus, BudgetMode, Drop, FeedbackAction
from app.planner.planner import (
    run_drop_cycle,
    apply_feedback,
    _plan_drops,
    _fill_empty_slots,
    _channel_budget,
)
from app.epub.chapterizer import Chapter
from tests.make_epub import make_epub


def _make_book(db, calibre_id: int, title: str = "Test Book", status=BookStatus.active,
               queue_position: int = 1, quota_weight: float = 1.0,
               channel_id: int | None = None) -> Book:
    from app.models import Channel
    if channel_id is None:  # default to the seeded General channel
        channel_id = db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
    book = Book(
        calibre_id=calibre_id,
        title=title,
        author="Test Author",
        status=status,
        queue_position=queue_position,
        quota_weight=quota_weight,
        channel_id=channel_id,
    )
    db.add(book)
    db.flush()
    return book


@pytest.fixture
def epub_path(tmp_path):
    chapters = [
        (f"Chapter {i}", f"<p>{'word ' * 500}</p>") for i in range(1, 6)
    ]
    path = make_epub(chapters=chapters)
    yield Path(path)
    os.unlink(path)


def _mock_adapter(calibre_id: int, epub_path: Path):
    """Return a mock CalibreAdapter that resolves one book to epub_path."""
    from app.calibre.adapter import CalibreBook
    mock = MagicMock()
    cbook = CalibreBook(
        calibre_id=calibre_id,
        title="Test Book",
        author="Test Author",
        path="Test Author/Test Book (1)",
        epub_name="Test Book - Test Author",
        source_url=None,
    )
    mock.get_book.return_value = cbook
    mock.epub_path.return_value = epub_path
    return mock


class TestPlanDrops:
    def test_always_posts_at_least_one_chapter(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        adapter = _mock_adapter(1, epub_path)
        # Budget of 1 word — the oversized first chapter still posts whole
        plans = _plan_drops([book], adapter, budget=1)
        assert len(plans) == 1
        assert len(plans[0].chapters) >= 1

    def test_packs_multiple_chapters_within_budget(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        adapter = _mock_adapter(1, epub_path)
        # Each chapter is ~500 words; budget 1500 comfortably fits multiple chapters
        plans = _plan_drops([book], adapter, budget=1500)
        assert plans[0].word_count >= 1000

    def test_respects_cursor(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        book.cursor_chapter_index = 3  # start at chapter 4
        adapter = _mock_adapter(1, epub_path)
        plans = _plan_drops([book], adapter, budget=99999)
        # Only chapters 4 and 5 remain (indices 3 and 4)
        assert len(plans[0].chapters) == 2

    def test_budget_shared_across_books_is_bounded(self, in_memory_db, epub_path):
        # Two books share one budget. Pure stochastic does not guarantee each a chapter,
        # but the cycle total must stay near the budget (not budget-per-book).
        book1 = _make_book(in_memory_db, calibre_id=1, title="Book 1", quota_weight=1.0)
        book2 = _make_book(in_memory_db, calibre_id=2, title="Book 2", quota_weight=1.0)

        from app.calibre.adapter import CalibreBook
        mock_all = MagicMock()
        mock_all.get_book.return_value = CalibreBook(
            calibre_id=1, title="T", author="A", path="A/T (1)", epub_name="T - A", source_url=None,
        )
        mock_all.epub_path.return_value = epub_path

        plans = _plan_drops([book1, book2], mock_all, budget=1000)
        total = sum(p.word_count for p in plans)
        assert plans                       # at least one source dropped
        assert total <= 1000 + 502         # bounded by budget + at most one boundary chapter


class TestGlobalRoundRobin:
    """Verify the global round-robin budget cap."""

    def test_global_words_bounded_by_budget(self, in_memory_db, epub_path):
        # 4 books, ~500-word chapters; budget=1000. Pure stochastic must keep the
        # cycle total near the budget — NOT 4 × 500 — and need not give every book a
        # chapter (no phase-1 guarantee).
        books = [
            _make_book(in_memory_db, calibre_id=i, title=f"Book {i}", queue_position=i)
            for i in range(1, 5)
        ]
        from app.calibre.adapter import CalibreBook
        mock_all = MagicMock()
        mock_all.get_book.return_value = CalibreBook(
            calibre_id=1, title="T", author="A",
            path="A/T (1)", epub_name="T - A", source_url=None,
        )
        mock_all.epub_path.return_value = epub_path

        plans = _plan_drops(books, mock_all, budget=1000)
        total_words = sum(p.word_count for p in plans)
        # Budget 1000 / 500-word chapters → ~2 chapters; bounded well under 4×500.
        assert 0 < total_words <= 1500
        assert sum(len(p.chapters) for p in plans) <= 3

    def test_high_budget_allows_extra_chapters(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        adapter = _mock_adapter(1, epub_path)
        # Budget large enough to pack more than one chapter
        plans = _plan_drops([book], adapter, budget=9999)
        assert plans[0].word_count > 500  # more than one chapter's worth

    def test_minutes_budget_mode(self, in_memory_db):
        # A channel in minutes mode multiplies its budget by the global wpm.
        from app.models import Channel, Config
        cfg = in_memory_db.get(Config, 1)
        cfg.wpm = 300
        channel = in_memory_db.query(Channel).order_by(Channel.id).first()
        channel.budget_mode = BudgetMode.minutes
        channel.budget = 10
        in_memory_db.flush()
        assert _channel_budget(channel, cfg) == 3000  # 10 min × 300 wpm


class TestDropCycle:
    def test_creates_drop_rows(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_drop_cycle(in_memory_db, Path("/fake"))
        assert len(drops) >= 1
        assert all(isinstance(d, Drop) for d in drops)

    def test_advances_cursor(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        in_memory_db.commit()
        initial_cursor = book.cursor_chapter_index

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            run_drop_cycle(in_memory_db, Path("/fake"))

        in_memory_db.refresh(book)
        assert book.cursor_chapter_index > initial_cursor

    def test_marks_completed_when_exhausted(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        book.cursor_chapter_index = 4  # only 1 chapter left in 5-chapter epub
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            # Big budget → will consume last chapter and mark complete
            run_drop_cycle(in_memory_db, Path("/fake"))

        in_memory_db.refresh(book)
        assert book.status == BookStatus.completed

    def test_fresh_library_all_queued_still_drops(self, in_memory_db, epub_path):
        """Regression: a fresh import leaves every book 'queued'. The cycle must
        promote them into open slots *before* looking for active books, otherwise
        it bails early and nothing is ever dropped (the deadlock bug)."""
        # 3 queued books, no active ones — exactly the fresh-deploy scenario.
        # Small budget so each promoted book takes one chapter and stays active
        # (rather than exhausting the short 5-chapter mock epub in one cycle).
        from app.models import Channel
        channel = in_memory_db.query(Channel).order_by(Channel.id).first()
        channel.budget = 100
        for i in range(1, 4):
            _make_book(
                in_memory_db, calibre_id=i, title=f"Book {i}",
                status=BookStatus.queued, queue_position=i,
            )
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_drop_cycle(in_memory_db, Path("/fake"))

        # parallel_slots=2 → 2 books promoted to active and dropped from.
        assert len(drops) >= 1
        active = in_memory_db.query(Book).filter(Book.status == BookStatus.active).count()
        assert active == 2

    def test_promotes_queued_book_when_slot_freed(self, in_memory_db, epub_path):
        # 1 active book that's about to exhaust + 1 queued.
        # Small budget so the promoted book takes one chapter and stays active.
        from app.models import Channel
        channel = in_memory_db.query(Channel).order_by(Channel.id).first()
        channel.budget = 100
        book1 = _make_book(in_memory_db, calibre_id=1, status=BookStatus.active)
        book1.cursor_chapter_index = 4  # last chapter
        book2 = _make_book(in_memory_db, calibre_id=2, status=BookStatus.queued, queue_position=2)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            run_drop_cycle(in_memory_db, Path("/fake"))

        in_memory_db.refresh(book2)
        assert book2.status == BookStatus.active


class TestFeedback:
    def _make_drop(self, db, book: Book) -> Drop:
        import secrets, uuid
        drop = Drop(
            book_id=book.id,
            word_count=500,
            chapter_start=0,
            chapter_end=0,
            chapter_titles="Chapter 1",
            content_html="<p>content</p>",
            feedback_token=secrets.token_urlsafe(24),
            reader_slug=str(uuid.uuid4()),
        )
        db.add(drop)
        db.flush()
        return drop

    def test_thumbs_up_increases_quota(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.up, Path("/fake"))
        assert book.quota_weight > 1.0
        assert book.thumbs_up == 1

    def test_thumbs_down_reduces_quota(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.down, Path("/fake"))
        assert book.quota_weight < 1.0
        assert book.thumbs_down == 1

    def test_thumbs_down_threshold_drops_book(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        book.thumbs_down = 2  # threshold is 3 in test config
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.down, Path("/fake"))
        assert book.status == BookStatus.dropped

    def test_feedback_event_recorded(self, in_memory_db, epub_path):
        from app.models import FeedbackEvent
        book = _make_book(in_memory_db, calibre_id=1)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.up, Path("/fake"))
        event = in_memory_db.query(FeedbackEvent).filter_by(drop_id=drop.id).first()
        assert event is not None
        assert event.action == FeedbackAction.up

    def test_extra_is_super_up_and_injects_drop(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            apply_feedback(in_memory_db, drop, FeedbackAction.extra, Path("/fake"))

        assert book.thumbs_up == 3
        assert book.quota_weight > 1.9  # ×1.25**3 ≈ 1.95
        # An out-of-cycle drop was injected (original + injected = 2 for this book).
        assert in_memory_db.query(Drop).filter(Drop.book_id == book.id).count() == 2

    def test_drop_action_drops_immediately(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        assert book.thumbs_down == 0  # no threshold needed
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.drop, Path("/fake"))
        assert book.status == BookStatus.dropped

    def test_feedback_idempotent_per_drop_action(self, in_memory_db, epub_path):
        from app.models import FeedbackEvent
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        # Two identical up-votes on the same drop (e.g. a prefetch + a real click).
        apply_feedback(in_memory_db, drop, FeedbackAction.up, Path("/fake"))
        apply_feedback(in_memory_db, drop, FeedbackAction.up, Path("/fake"))

        assert book.thumbs_up == 1                       # counted once
        assert book.quota_weight == pytest.approx(1.25)  # not compounded to 1.5625
        events = in_memory_db.query(FeedbackEvent).filter_by(
            drop_id=drop.id, action=FeedbackAction.up
        ).count()
        assert events == 1


class TestChannels:
    def test_per_channel_slots_and_feed_key_stamping(self, in_memory_db, epub_path):
        from app.models import Channel, Config
        cfg = in_memory_db.get(Config, 1)
        ch = Channel(name="Fantasy", slug="fantasy", parallel_slots=2, budget=100)
        in_memory_db.add(ch)
        in_memory_db.flush()
        for i in (1, 2, 3):
            b = _make_book(
                in_memory_db, calibre_id=i, title=f"B{i}",
                status=BookStatus.queued, queue_position=i,
            )
            b.channel_id = ch.id
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_drop_cycle(in_memory_db, Path("/fake"))

        active = (
            in_memory_db.query(Book)
            .filter(Book.status == BookStatus.active, Book.channel_id == ch.id)
            .all()
        )
        assert len(active) == 2                                  # channel's 2 slots filled
        assert sorted(b.slot_index for b in active) == [1, 2]    # stable slot numbers
        assert drops and all(d.channel_id == ch.id for d in drops)
        assert {d.feed_key for d in drops} == {"1", "2"}         # one drop per slot

    def test_general_channel_cycle(self, in_memory_db, epub_path):
        # A book imported with no explicit channel lands in the auto-created General
        # channel and drops from there (no global/default group anymore).
        from app.models import Channel
        general = in_memory_db.query(Channel).order_by(Channel.id).first()
        _make_book(in_memory_db, calibre_id=1, status=BookStatus.queued, queue_position=1)
        in_memory_db.commit()
        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_drop_cycle(in_memory_db, Path("/fake"))
        assert len(drops) >= 1
        assert drops[0].channel_id == general.id
        assert drops[0].feed_key == "1"


class TestStochasticBudget:
    def test_unit_within_budget_always_included(self):
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(100, used=0, budget=1000, weight=1.0, first_for_book=True) == 1.0

    def test_over_budget_excluded(self):
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(100, used=1000, budget=1000, weight=1.0, first_for_book=False) == 0.0

    def test_boundary_fraction_at_weight_one(self):
        from app.planner.planner import _inclusion_probability
        # 50 words of budget left for a 100-word unit → p = 0.5
        p = _inclusion_probability(100, used=950, budget=1000, weight=1.0, first_for_book=False)
        assert p == pytest.approx(0.5)

    def test_higher_weight_raises_probability(self):
        from app.planner.planner import _inclusion_probability
        low = _inclusion_probability(100, 950, 1000, weight=0.5, first_for_book=False)
        high = _inclusion_probability(100, 950, 1000, weight=2.0, first_for_book=False)
        assert high > 0.5 > low

    def test_oversized_first_unit_posts_whole(self):
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(5000, used=0, budget=1000, weight=1.0, first_for_book=True) == 1.0

    def test_oversized_defers_when_not_first_and_over(self):
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(5000, used=1000, budget=1000, weight=1.0, first_for_book=False) == 0.0

    def test_mean_words_tracks_budget(self):
        # Repeatedly draw same-size units until one is rejected; mean total ≈ budget.
        from app.planner.planner import _inclusion_probability
        random.seed(1234)
        budget, w, trials, totals = 1000, 300, 4000, []
        for _ in range(trials):
            used, first = 0, True
            while random.random() < _inclusion_probability(w, used, budget, 1.0, first):
                used += w
                first = False
            totals.append(used)
        mean = sum(totals) / trials
        assert abs(mean - budget) < 120  # tracks budget without even the credit smoothing
