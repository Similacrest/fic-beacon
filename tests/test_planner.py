"""Tests for the Budget / Drop Planner.

Budget invariants:
  - The cycle total stays near the budget (not N × budget); there is no phase-1
    guarantee, so a source may get nothing some cycles.
  - A unit larger than the whole budget is posted whole (it could never fit otherwise).
  - Pass 2 round-robins across occupied slots (diversity); quota_weight throttles a
    source's share within its slot, and an idle slot's budget spills to the others.

Uses the in-memory DB fixture and mock EPUB fixtures.
"""
import os
import random
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.models import Book, BookStatus, BudgetMode, Drop, FeedbackAction
from app.planner.planner import (
    assign_channel_slots,
    run_release_cycle,
    apply_feedback,
    _plan_drops,
    _assign_slots,
    schedule_budget,
)
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


def _set_release_budget(db, words: float) -> None:
    """Set the seeded default schedule's whole-release budget (in words)."""
    from app.models import Schedule
    db.query(Schedule).first().budget = words
    db.flush()


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
    def test_oversized_chapter_paces_by_accumulation(self, in_memory_db, epub_path):
        # An oversized chapter (bigger than the base per-cycle budget) is NO LONGER force-posted
        # every cycle — it waits until the effective budget (base + saved-up credit) affords it,
        # then posts whole, exactly once. base_budget=1 makes each ~500w chapter 'oversized'.
        book = _make_book(in_memory_db, calibre_id=1)
        adapter = _mock_adapter(1, epub_path)
        # Too little saved up → defers entirely (no guaranteed first chapter).
        assert _plan_drops([book], adapter, budget=1, base_budget=1) == []
        # Enough saved up → exactly one oversized chapter posts, whole.
        plans = _plan_drops([book], adapter, budget=5000, base_budget=1)
        assert len(plans) == 1 and len(plans[0].chapters) == 1

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

    def test_skips_out_records_rolled_over_sources(self, in_memory_db, epub_path):
        random.seed(0)
        # 4 books × 5 chapters of ~500 words, tight budget → most units roll over.
        books = [
            _make_book(in_memory_db, calibre_id=i, title=f"Book {i}", queue_position=i)
            for i in range(1, 5)
        ]
        from app.calibre.adapter import CalibreBook
        mock_all = MagicMock()
        mock_all.get_book.return_value = CalibreBook(
            calibre_id=1, title="T", author="A", path="A/T (1)", epub_name="T - A", source_url=None,
        )
        mock_all.epub_path.return_value = epub_path

        skips = []
        plans = _plan_drops(books, mock_all, budget=1000, skips_out=skips)

        assert skips, "tight budget should leave sources with rolled-over units"
        assert all(s.remaining_count > 0 for s in skips)
        # A fully held-out source dropped nothing; reflected by held_out.
        for s in skips:
            assert s.held_out == (s.dropped_count == 0)
        # Books that dropped something are not double-counted as fully held out.
        dropped_ids = {p.book.id for p in plans}
        for s in skips:
            if s.book.id in dropped_ids:
                assert not s.held_out

    def test_minutes_budget_mode(self, in_memory_db):
        # A schedule in minutes mode multiplies its budget by the global wpm.
        from app.models import Config, Schedule
        cfg = in_memory_db.get(Config, 1)
        cfg.wpm = 300
        schedule = in_memory_db.query(Schedule).first()
        schedule.budget_mode = BudgetMode.minutes
        schedule.budget = 10
        in_memory_db.flush()
        assert schedule_budget(schedule, cfg) == 3000  # 10 min × 300 wpm


class TestDropCycle:
    def test_creates_drop_rows(self, in_memory_db, epub_path):
        _make_book(in_memory_db, calibre_id=1)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_release_cycle(in_memory_db, Path("/fake"))
        assert len(drops) >= 1
        assert all(isinstance(d, Drop) for d in drops)

    def test_advances_cursor(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        in_memory_db.commit()
        initial_cursor = book.cursor_chapter_index

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            run_release_cycle(in_memory_db, Path("/fake"))

        in_memory_db.refresh(book)
        assert book.cursor_chapter_index > initial_cursor

    def test_marks_completed_when_exhausted(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        book.cursor_chapter_index = 4  # only 1 chapter left in 5-chapter epub
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            # Big budget → will consume last chapter and mark complete
            run_release_cycle(in_memory_db, Path("/fake"))

        in_memory_db.refresh(book)
        assert book.status == BookStatus.completed

    def test_fresh_library_all_queued_still_drops(self, in_memory_db, epub_path):
        """Regression: a fresh import leaves every book 'queued'. The cycle must
        promote them into open slots *before* looking for active books, otherwise
        it bails early and nothing is ever dropped (the deadlock bug)."""
        # 3 queued books, no active ones — exactly the fresh-deploy scenario.
        # Budget fits ~one ~500w chapter so a promoted book drops one and stays active
        # (rather than exhausting the short 5-chapter mock epub in one cycle).
        _set_release_budget(in_memory_db, 600)
        for i in range(1, 4):
            _make_book(
                in_memory_db, calibre_id=i, title=f"Book {i}",
                status=BookStatus.queued, queue_position=i,
            )
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_release_cycle(in_memory_db, Path("/fake"))

        # parallel_slots=2 → 2 books promoted to active and dropped from.
        assert len(drops) >= 1
        active = in_memory_db.query(Book).filter(Book.status == BookStatus.active).count()
        assert active == 2

    def test_promotes_queued_book_when_slot_freed(self, in_memory_db, epub_path):
        # 1 active book that's about to exhaust + 1 queued.
        # Small budget so the promoted book takes one chapter and stays active.
        _set_release_budget(in_memory_db, 100)
        book1 = _make_book(in_memory_db, calibre_id=1, status=BookStatus.active)
        book1.cursor_chapter_index = 4  # last chapter
        book2 = _make_book(in_memory_db, calibre_id=2, status=BookStatus.queued, queue_position=2)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            run_release_cycle(in_memory_db, Path("/fake"))

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
        assert book.quota_weight == pytest.approx(1.25)   # additive: + Config.vote_step (0.25)
        assert book.thumbs_up == 1

    def test_thumbs_down_reduces_quota(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.down, Path("/fake"))
        assert book.quota_weight == pytest.approx(0.75)   # additive: − Config.vote_step
        assert book.thumbs_down == 1

    def test_thumbs_down_to_zero_drops_book(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=0.25)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.down, Path("/fake"))
        assert book.quota_weight == 0.0
        assert book.status == BookStatus.dropped

    def test_many_thumbs_down_do_not_drop_while_weight_positive(self, in_memory_db, epub_path):
        """The old count threshold is retired: only the weight reaching 0 auto-drops."""
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=3.0)
        book.thumbs_down = 10
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.down, Path("/fake"))
        assert book.status == BookStatus.active
        assert book.quota_weight == pytest.approx(2.75)

    def test_weight_capped_at_100(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=99.9)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        apply_feedback(in_memory_db, drop, FeedbackAction.up, Path("/fake"))
        assert book.quota_weight == 100.0

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
        # + Config.extra_boost_step (default 0.5; configurable in admin settings) — additive.
        assert book.quota_weight == pytest.approx(1.5)
        # An out-of-cycle drop was injected (original + injected = 2 for this book).
        assert in_memory_db.query(Drop).filter(Drop.book_id == book.id).count() == 2

    def test_extra_drop_is_born_acknowledged_and_does_not_penalise(self, in_memory_db, epub_path):
        """Regression — "I asked for an extra chapter and got skipped next time": the injected
        drop used to sit unread and trigger the unread penalty on the next cycle."""
        from app.planner.planner import _unread_penalties
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        drop = self._make_drop(in_memory_db, book)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            apply_feedback(in_memory_db, drop, FeedbackAction.extra, Path("/fake"))

        injected = (in_memory_db.query(Drop).filter(Drop.book_id == book.id, Drop.id != drop.id)
                    .one())
        assert injected.acknowledged_at is not None
        assert _unread_penalties(in_memory_db, [book], floor=0.2) == {}

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
        assert book.quota_weight == pytest.approx(1.25)  # counted once, not 1.5
        events = in_memory_db.query(FeedbackEvent).filter_by(
            drop_id=drop.id, action=FeedbackAction.up
        ).count()
        assert events == 1


class TestChannels:
    def test_per_channel_slots_and_feed_key_stamping(self, in_memory_db, epub_path):
        from app.models import Channel
        ch = Channel(name="Fantasy", slug="fantasy", parallel_slots=2, weight=1)
        in_memory_db.add(ch)
        in_memory_db.flush()
        _set_release_budget(in_memory_db, 1500)  # small enough that no book exhausts its EPUB
        for i in (1, 2, 3):
            b = _make_book(
                in_memory_db, calibre_id=i, title=f"B{i}",
                status=BookStatus.queued, queue_position=i,
            )
            b.channel_id = ch.id
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_release_cycle(in_memory_db, Path("/fake"))

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
            drops = run_release_cycle(in_memory_db, Path("/fake"))
        assert len(drops) >= 1
        assert drops[0].channel_id == general.id
        assert drops[0].feed_key == "1"


class TestAssignSlots:
    """Tests for the _assign_slots slot assignment / load-balancing logic."""

    def _make_ongoing(self, db, title: str, channel_id: int,
                      slot_index: int | None = None) -> Book:
        from sqlalchemy import func
        max_pos = db.query(func.max(Book.queue_position)).scalar() or 0
        book = Book(
            tracked=True,
            calibre_id=max_pos + 1000,  # unique placeholder; slot tests don't read the EPUB
            source_url=f"https://example.com/{title}",
            feed_url=f"https://example.com/{title}.rss",
            title=title,
            author="Author",
            status=BookStatus.active,
            queue_position=max_pos + 1,
            channel_id=channel_id,
            slot_index=slot_index,
        )
        db.add(book)
        db.flush()
        return book

    def _make_channel(self, db, parallel_slots: int = 3):
        from app.models import Channel
        ch = Channel(name=f"Ch{parallel_slots}", slug=f"ch{id(parallel_slots)}",
                     parallel_slots=parallel_slots, weight=1)
        db.add(ch)
        db.flush()
        return ch

    def test_ongoings_get_valid_slot_assigned(self, in_memory_db):
        ch = self._make_channel(in_memory_db, parallel_slots=2)
        for i in range(5):
            self._make_ongoing(in_memory_db, f"Serial {i}", ch.id)
        in_memory_db.flush()

        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)

        ongoings = in_memory_db.query(Book).filter(Book.channel_id == ch.id).all()
        for o in ongoings:
            assert o.slot_index in (1, 2), f"{o.title} got slot {o.slot_index}"
            assert o.status == BookStatus.active  # ongoings never demoted to queued

    def test_heavy_serial_slot_is_avoided(self, in_memory_db):
        """Balance is by summed quota_weight, not by how many works a slot holds."""
        ch = self._make_channel(in_memory_db, parallel_slots=2)
        heavy = self._make_ongoing(in_memory_db, "Heavy", ch.id, slot_index=1)
        heavy.quota_weight = 5.0
        light = self._make_ongoing(in_memory_db, "Light", ch.id, slot_index=2)
        light.quota_weight = 0.5
        newcomers = [self._make_ongoing(in_memory_db, f"New{i}", ch.id) for i in range(3)]
        in_memory_db.flush()

        assign_channel_slots(in_memory_db, ch.id)

        # Slot 2 (load 0.5) absorbs the newcomers until it outweighs slot 1 (load 5.0).
        assert all(b.slot_index == 2 for b in newcomers)

    def test_assign_channel_slots_leaves_backlog_queued(self, in_memory_db):
        ch = self._make_channel(in_memory_db, parallel_slots=2)
        queued = Book(calibre_id=999, title="Q", author="A", status=BookStatus.queued,
                      queue_position=1, channel_id=ch.id)
        in_memory_db.add(queued)
        self._make_ongoing(in_memory_db, "S", ch.id)
        in_memory_db.flush()

        assign_channel_slots(in_memory_db, ch.id)

        assert queued.status == BookStatus.queued and queued.slot_index is None

    def test_ongoings_load_balanced_across_slots(self, in_memory_db):
        ch = self._make_channel(in_memory_db, parallel_slots=3)
        for i in range(9):
            self._make_ongoing(in_memory_db, f"Serial {i}", ch.id)
        in_memory_db.flush()

        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)

        counts = {1: 0, 2: 0, 3: 0}
        for o in in_memory_db.query(Book).filter(Book.channel_id == ch.id).all():
            counts[o.slot_index] += 1
        # 9 ongoings across 3 slots → exactly 3 each
        assert counts == {1: 3, 2: 3, 3: 3}

    def test_ongoings_with_out_of_range_slots_reassigned(self, in_memory_db):
        """Ongoings with slot_index beyond parallel_slots (e.g. from old code) get clamped."""
        ch = self._make_channel(in_memory_db, parallel_slots=3)
        o = self._make_ongoing(in_memory_db, "Serial", ch.id, slot_index=12)
        in_memory_db.flush()

        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)

        # Check in-memory state (no refresh — _assign_slots updates the object directly).
        assert 1 <= o.slot_index <= 3

    def test_ongoings_sticky_valid_slot_kept(self, in_memory_db):
        ch = self._make_channel(in_memory_db, parallel_slots=3)
        o = self._make_ongoing(in_memory_db, "Serial", ch.id, slot_index=2)
        in_memory_db.flush()

        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)
        in_memory_db.refresh(o)

        assert o.slot_index == 2  # kept sticky

    def test_epub_cap_demotes_excess_to_queued(self, in_memory_db):
        ch = self._make_channel(in_memory_db, parallel_slots=2)
        books = []
        for i in range(4):
            b = _make_book(in_memory_db, calibre_id=i + 1, title=f"EPUB {i}",
                           status=BookStatus.active, queue_position=i + 1,
                           channel_id=ch.id)
            books.append(b)
        in_memory_db.flush()

        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)

        active = [b for b in books if b.status == BookStatus.active]
        queued = [b for b in books if b.status == BookStatus.queued]
        assert len(active) == 2
        assert len(queued) == 2
        assert sorted(b.slot_index for b in active) == [1, 2]
        for b in queued:
            assert b.slot_index is None

    def test_epub_and_ongoing_share_slot(self, in_memory_db, epub_path):
        """A slot's feed carries both an EPUB and the ongoings pinned to it."""
        ch = self._make_channel(in_memory_db, parallel_slots=2)
        epub = _make_book(in_memory_db, calibre_id=1, title="EPUB",
                          status=BookStatus.queued, queue_position=1,
                          channel_id=ch.id)
        ongoing = self._make_ongoing(in_memory_db, "Serial", ch.id)
        in_memory_db.flush()

        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)

        in_memory_db.refresh(epub)
        in_memory_db.refresh(ongoing)
        assert epub.status == BookStatus.active
        assert 1 <= epub.slot_index <= 2
        assert 1 <= ongoing.slot_index <= 2


class TestStochasticBudget:
    def test_unit_within_budget_always_included(self):
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(100, used=0, budget=1000) == 1.0

    def test_over_budget_excluded(self):
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(100, used=1000, budget=1000) == 0.0

    def test_boundary_fraction(self):
        from app.planner.planner import _inclusion_probability
        # 50 words of budget left for a 100-word unit → p = 0.5
        assert _inclusion_probability(100, used=950, budget=1000) == pytest.approx(0.5)

    def test_penalty_lowers_probability(self):
        # The read-gating penalty scales acceptance down (absolute back-off), independent of weight.
        from app.planner.planner import _inclusion_probability
        base = _inclusion_probability(100, used=950, budget=1000)
        gated = _inclusion_probability(100, used=950, budget=1000, penalty=0.5)
        assert gated == pytest.approx(base * 0.5)

    def test_oversized_returns_zero_deferred_to_accumulation(self):
        # A unit larger than the base per-cycle budget is not selected by the stochastic pass
        # (p=0); the accumulation pass in _plan_drops owns it once enough credit builds up.
        from app.planner.planner import _inclusion_probability
        assert _inclusion_probability(5000, used=0, budget=1000, base_budget=1000) == 0.0
        assert _inclusion_probability(5000, used=0, budget=9000, base_budget=1000) == 0.0

    def test_mean_words_tracks_budget(self):
        # Repeatedly draw same-size units until one is rejected; mean total ≈ budget.
        from app.planner.planner import _inclusion_probability
        random.seed(1234)
        budget, w, trials, totals = 1000, 300, 4000, []
        for _ in range(trials):
            used = 0
            while random.random() < _inclusion_probability(w, used, budget):
                used += w
            totals.append(used)
        mean = sum(totals) / trials
        assert abs(mean - budget) < 120  # tracks budget without even the credit smoothing


class TestSlotRoundRobin:
    """Pass 2 spreads drops across occupied slots; weight throttles share within a slot."""

    def test_budget_spreads_across_slots(self, in_memory_db, epub_path):
        # Three books, each pinned to a different slot, generous budget. The round-robin must
        # give every slot content (diversity) — not drain one slot before touching the others.
        random.seed(0)
        from app.calibre.adapter import CalibreBook
        from app.planner.planner import _plan_drops
        books = []
        for i in range(1, 4):
            b = _make_book(in_memory_db, calibre_id=i, title=f"Book {i}", queue_position=i)
            b.slot_index = i
            books.append(b)
        mock_all = MagicMock()
        mock_all.get_book.return_value = CalibreBook(
            calibre_id=1, title="T", author="A", path="A/T (1)", epub_name="T - A", source_url=None)
        mock_all.epub_path.return_value = epub_path

        plans = _plan_drops(books, mock_all, budget=99999)
        slots_hit = {p.book.slot_index for p in plans}
        assert slots_hit == {1, 2, 3}  # every slot got at least one drop

    def test_idle_slot_spills_to_others(self, in_memory_db, epub_path):
        # Only slot 1 has content (slot 2's source is caught up). Its budget must spill to slot 1
        # rather than go unused — no wasted budget.
        from app.calibre.adapter import CalibreBook
        from app.planner.planner import _plan_drops
        active = _make_book(in_memory_db, calibre_id=1, title="Active", queue_position=1)
        active.slot_index = 1
        idle = _make_book(in_memory_db, calibre_id=2, title="Idle", queue_position=2)
        idle.slot_index = 2
        idle.cursor_chapter_index = 5  # caught up: no pending units

        def get_book(cid):
            return CalibreBook(calibre_id=cid, title="T", author="A",
                               path="A/T (1)", epub_name="T - A", source_url=None)
        mock_all = MagicMock()
        mock_all.get_book.side_effect = get_book
        mock_all.epub_path.return_value = epub_path

        plans = _plan_drops([active, idle], mock_all, budget=1600)  # ~3 of the ~500w chapters
        assert {p.book.slot_index for p in plans} == {1}
        assert sum(len(p.chapters) for p in plans) >= 2  # slot 1 used the spilled budget

    def test_weight_throttles_share_within_slot(self, in_memory_db, epub_path):
        # Two sources share one slot; the heavier one wins more of the slot's round-robin turns.
        random.seed(7)
        from app.calibre.adapter import CalibreBook
        from app.planner.planner import _plan_drops
        heavy = _make_book(in_memory_db, calibre_id=1, title="Heavy", quota_weight=4.0)
        light = _make_book(in_memory_db, calibre_id=2, title="Light", quota_weight=1.0)
        heavy.slot_index = light.slot_index = 1
        mock_all = MagicMock()
        mock_all.get_book.return_value = CalibreBook(
            calibre_id=1, title="T", author="A", path="A/T (1)", epub_name="T - A", source_url=None)
        mock_all.epub_path.return_value = epub_path

        heavy_total = light_total = 0
        for _ in range(80):
            heavy.cursor_chapter_index = light.cursor_chapter_index = 0
            plans = _plan_drops([heavy, light], mock_all, budget=1200)  # ~2 chapters/cycle
            for p in plans:
                if p.book.id == heavy.id:
                    heavy_total += len(p.chapters)
                else:
                    light_total += len(p.chapters)
        assert heavy_total > light_total  # weight governs share within the slot


class TestSetSlot:
    """Manual drag-to-repin endpoint (admin.set_slot)."""

    def _req(self):
        from types import SimpleNamespace
        return SimpleNamespace(headers={})  # no HX-Request → plain redirect response

    def test_backlog_move_to_occupied_slot_swaps(self, in_memory_db):
        from app.models import Channel
        from app.routers.admin import set_slot
        ch = Channel(name="C", slug="c", parallel_slots=2, weight=1)
        in_memory_db.add(ch); in_memory_db.flush()
        a = _make_book(in_memory_db, calibre_id=1, title="A", channel_id=ch.id)
        b = _make_book(in_memory_db, calibre_id=2, title="B", channel_id=ch.id)
        a.slot_index, b.slot_index = 1, 2
        in_memory_db.commit()

        set_slot(a.id, self._req(), slot_index=2, db=in_memory_db)

        assert a.slot_index == 2   # moved
        assert b.slot_index == 1   # swapped into A's old slot (one backlog per slot)

    def test_tracked_move_does_not_swap(self, in_memory_db):
        from app.models import Channel, Book, BookStatus
        from app.routers.admin import set_slot
        ch = Channel(name="C", slug="c", parallel_slots=2, weight=1)
        in_memory_db.add(ch); in_memory_db.flush()
        backlog = _make_book(in_memory_db, calibre_id=1, title="EP", channel_id=ch.id)
        backlog.slot_index = 1
        tracked = Book(tracked=True, calibre_id=99, title="T", author="x",
                       status=BookStatus.active, channel_id=ch.id, slot_index=2)
        in_memory_db.add(tracked); in_memory_db.commit()

        set_slot(tracked.id, self._req(), slot_index=1, db=in_memory_db)

        assert tracked.slot_index == 1   # tracked stories are uncapped — just moves
        assert backlog.slot_index == 1   # backlog book untouched (no swap)

    def test_out_of_range_slot_ignored(self, in_memory_db):
        from app.models import Channel
        from app.routers.admin import set_slot
        ch = Channel(name="C", slug="c", parallel_slots=2, weight=1)
        in_memory_db.add(ch); in_memory_db.flush()
        a = _make_book(in_memory_db, calibre_id=1, title="A", channel_id=ch.id)
        a.slot_index = 1
        in_memory_db.commit()

        set_slot(a.id, self._req(), slot_index=5, db=in_memory_db)

        assert a.slot_index == 1   # out of [1, parallel_slots] → no change


class TestPause:
    """1.1.B — pausing a source removes it from broadcasting; backlog frees its slot."""

    def _channel(self, db, parallel_slots: int = 2):
        from app.models import Channel
        ch = Channel(name="P", slug=f"p{id(db)}", parallel_slots=parallel_slots, weight=1)
        db.add(ch); db.flush()
        return ch

    def test_pause_backlog_frees_slot_and_promotes_next(self, in_memory_db):
        from app.planner.planner import pause_book
        ch = self._channel(in_memory_db, parallel_slots=1)  # one slot → contention
        a = _make_book(in_memory_db, calibre_id=1, title="A", queue_position=1, channel_id=ch.id)
        b = _make_book(in_memory_db, calibre_id=2, title="B", status=BookStatus.queued,
                       queue_position=2, channel_id=ch.id)
        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)
        assert a.slot_index == 1 and b.status == BookStatus.queued

        pause_book(in_memory_db, a)

        assert a.paused is True
        assert a.slot_index is None
        assert a.status == BookStatus.queued          # re-enters the queue
        assert b.status == BookStatus.active           # next queued streams into the freed slot
        assert b.slot_index == 1

    def test_paused_backlog_not_repromoted(self, in_memory_db):
        from app.planner.planner import pause_book
        ch = self._channel(in_memory_db, parallel_slots=1)
        a = _make_book(in_memory_db, calibre_id=1, title="A", queue_position=1, channel_id=ch.id)
        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)
        pause_book(in_memory_db, a)          # frees slot 1 (no other book)
        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)  # must not re-promote the paused book
        assert a.status == BookStatus.queued
        assert a.slot_index is None

    def test_pause_tracked_keeps_slot_but_excluded_from_candidates(self, in_memory_db):
        from app.planner.planner import pause_book, _active_books_in
        from app.models import Book
        ch = self._channel(in_memory_db, parallel_slots=2)
        t = Book(tracked=True, calibre_id=50, title="T", author="x",
                 status=BookStatus.active, channel_id=ch.id, slot_index=1)
        in_memory_db.add(t); in_memory_db.flush()

        pause_book(in_memory_db, t)

        assert t.paused is True
        assert t.status == BookStatus.active   # tracked never demoted
        assert t.id not in {b.id for b in _active_books_in(in_memory_db, ch.id)}

    def test_resume_restores_eligibility(self, in_memory_db):
        from app.planner.planner import pause_book, resume_book, _active_books_in
        ch = self._channel(in_memory_db, parallel_slots=1)
        a = _make_book(in_memory_db, calibre_id=1, title="A", queue_position=1, channel_id=ch.id)
        _assign_slots(in_memory_db, ch.parallel_slots, ch.id)
        pause_book(in_memory_db, a)
        resume_book(in_memory_db, a)

        assert a.paused is False
        assert a.status == BookStatus.active     # re-promoted (slot was free again)
        assert a.slot_index == 1
        assert a.id in {b.id for b in _active_books_in(in_memory_db, ch.id)}

    def test_pause_feedback_action(self, in_memory_db, epub_path):
        """The ⏸ feed link routes through apply_feedback(FeedbackAction.pause)."""
        ch = self._channel(in_memory_db, parallel_slots=1)
        a = _make_book(in_memory_db, calibre_id=1, title="A", channel_id=ch.id)
        a.slot_index = 1
        drop = Drop(book_id=a.id, feedback_token="ptok", reader_slug="pslug",
                    chapter_start=0, chapter_end=0, word_count=100)
        in_memory_db.add(drop); in_memory_db.flush()

        apply_feedback(in_memory_db, drop, FeedbackAction.pause, epub_path.parent)

        assert a.paused is True


class TestCooldown:
    """A 👎 sets cooldown_remaining=2, ticked at the start of each channel turn, so the source sits
    out exactly one broadcast."""

    def _drop_for(self, db, book, token="dtok", slug="dslug"):
        d = Drop(book_id=book.id, feedback_token=token, reader_slug=slug,
                 chapter_start=0, chapter_end=0, word_count=100)
        db.add(d); db.flush()
        return d

    def test_down_sets_cooldown(self, in_memory_db, epub_path):
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        d = self._drop_for(in_memory_db, a)
        apply_feedback(in_memory_db, d, FeedbackAction.down, epub_path.parent)
        assert a.cooldown_remaining == 2
        assert a.status == BookStatus.active   # weight still above 0 → not auto-dropped

    def test_cooled_source_excluded_from_candidates(self, in_memory_db):
        from app.planner.planner import _active_books_in
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        a.cooldown_remaining = 2
        in_memory_db.flush()
        assert a.id not in {b.id for b in _active_books_in(in_memory_db, a.channel_id)}

    def test_tick_decrements_and_reeligible_after_two(self, in_memory_db):
        from app.planner.planner import _active_books_in, _tick_cooldowns
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        a.cooldown_remaining = 2
        in_memory_db.flush()
        cid = a.channel_id

        _tick_cooldowns(in_memory_db, cid); in_memory_db.refresh(a)
        assert a.cooldown_remaining == 1
        assert a.id not in {b.id for b in _active_books_in(in_memory_db, cid)}

        _tick_cooldowns(in_memory_db, cid); in_memory_db.refresh(a)
        assert a.cooldown_remaining == 0
        assert a.id in {b.id for b in _active_books_in(in_memory_db, cid)}

    def test_tick_floors_at_zero(self, in_memory_db):
        from app.planner.planner import _tick_cooldowns
        a = _make_book(in_memory_db, calibre_id=1, title="A")  # cooldown 0
        in_memory_db.flush()
        _tick_cooldowns(in_memory_db, a.channel_id); in_memory_db.refresh(a)
        assert a.cooldown_remaining == 0   # never goes negative


class TestReadGating:
    """Soft read-gating: a *streak* of unread drops ramps a source's acceptance down (transient —
    never touches quota_weight); a single unread drop is free."""

    def _drop_for(self, db, book, acked: bool, token, slug):
        from app.models import utcnow
        d = Drop(book_id=book.id, feedback_token=token, reader_slug=slug,
                 chapter_start=0, chapter_end=0, word_count=100,
                 acknowledged_at=(utcnow() if acked else None))
        db.add(d); db.flush()
        return d

    def _streak(self, db, book, n_unread, prefix="u"):
        for i in range(n_unread):
            self._drop_for(db, book, acked=False, token=f"{prefix}t{i}", slug=f"{prefix}s{i}")

    def test_ramp_ladder(self):
        from app.planner.planner import _unread_penalty
        assert _unread_penalty(0, 0.2) == 1.0
        assert _unread_penalty(1, 0.2) == 1.0          # one unread drop costs nothing
        assert _unread_penalty(2, 0.2) == pytest.approx(0.8)
        assert _unread_penalty(3, 0.2) == pytest.approx(0.6)
        assert _unread_penalty(4, 0.2) == pytest.approx(0.4)
        assert _unread_penalty(5, 0.2) == pytest.approx(0.2)
        assert _unread_penalty(50, 0.2) == pytest.approx(0.2)   # floored, never zero

    def test_no_drops_not_penalised(self, in_memory_db):
        from app.planner.planner import _unread_penalties
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        assert _unread_penalties(in_memory_db, [a], 0.2) == {}

    def test_single_unread_not_penalised(self, in_memory_db):
        from app.planner.planner import _unread_penalties
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        self._streak(in_memory_db, a, 1)
        assert _unread_penalties(in_memory_db, [a], 0.2) == {}

    def test_streak_of_three_is_penalised(self, in_memory_db):
        from app.planner.planner import _unread_penalties
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        self._streak(in_memory_db, a, 3)
        assert _unread_penalties(in_memory_db, [a], 0.2) == {a.id: pytest.approx(0.6)}

    def test_reading_the_latest_resets_the_streak(self, in_memory_db):
        from app.planner.planner import _unread_penalties
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        self._streak(in_memory_db, a, 4)
        self._drop_for(in_memory_db, a, acked=True, token="rt", slug="rs")  # newest, read
        assert _unread_penalties(in_memory_db, [a], 0.2) == {}

    def test_feedback_acknowledges_drop(self, in_memory_db, epub_path):
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        d = self._drop_for(in_memory_db, a, acked=False, token="t1", slug="s1")
        apply_feedback(in_memory_db, d, FeedbackAction.read, epub_path.parent)
        assert d.acknowledged_at is not None

    def test_read_action_no_weight_change(self, in_memory_db, epub_path):
        a = _make_book(in_memory_db, calibre_id=1, title="A", quota_weight=1.0)
        d = self._drop_for(in_memory_db, a, acked=False, token="t1", slug="s1")
        apply_feedback(in_memory_db, d, FeedbackAction.read, epub_path.parent)
        assert a.quota_weight == 1.0   # ✓ read is neutral
        assert a.thumbs_up == 0 and a.thumbs_down == 0

    def test_penalty_applied_in_plan_and_weight_untouched(self, in_memory_db, epub_path):
        """_plan_drops routes the penalty: a penalised source is picked less over many cycles."""
        from app.planner.planner import _plan_drops
        random.seed(1)
        book = _make_book(in_memory_db, calibre_id=1)
        adapter = _mock_adapter(1, epub_path)  # 5×~500-word chapters

        def count(penalties):
            total = 0
            for _ in range(60):
                book.cursor_chapter_index = 0  # reset so the same first unit is a candidate
                # base_budget high (not oversized); effective budget < unit so p<1 and the penalty bites.
                plans = _plan_drops([book], adapter, budget=400, base_budget=5000,
                                    penalties=penalties)
                total += sum(len(p.chapters) for p in plans)
            return total

        assert count({book.id: 0.2}) < count({})
        assert book.quota_weight == 1.0


class TestWeightFade:
    """Below the skip floor a source fades in proportion to its weight — no hard skip."""

    def test_fade_factor(self):
        from app.planner.planner import _weight_fade
        b = MagicMock(); b.quota_weight = 0.5
        assert _weight_fade(b, 1.0) == pytest.approx(0.5)
        b.quota_weight = 1.0
        assert _weight_fade(b, 1.0) == 1.0
        b.quota_weight = 3.0
        assert _weight_fade(b, 1.0) == 1.0
        b.quota_weight = 0.0
        assert _weight_fade(b, 1.0) == 0.0
        b.quota_weight = 0.3
        assert _weight_fade(b, 0.0) == 1.0    # floor 0 disables the fade

    def test_faded_source_posts_less_often(self, in_memory_db, epub_path):
        from app.planner.planner import _plan_drops
        random.seed(2)
        book = _make_book(in_memory_db, calibre_id=1)
        adapter = _mock_adapter(1, epub_path)

        def count(weight):
            book.quota_weight = weight
            total = 0
            for _ in range(80):
                book.cursor_chapter_index = 0
                plans = _plan_drops([book], adapter, budget=400, base_budget=5000,
                                    weight_skip_floor=1.0)
                total += sum(len(p.chapters) for p in plans)
            return total

        assert count(0.3) < count(1.0)
        assert count(0.0) == 0            # weight 0 never posts (but isn't dropped)
        assert book.status == BookStatus.active


class TestAdminSetWeight:
    def test_zero_does_not_drop_and_cap_applies(self, in_memory_db):
        from types import SimpleNamespace
        from app.routers.admin import set_weight
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        in_memory_db.commit()
        req = SimpleNamespace(headers={})

        set_weight(book.id, req, weight=0.0, db=in_memory_db)
        assert book.quota_weight == 0.0 and book.status == BookStatus.active

        set_weight(book.id, req, weight=500.0, db=in_memory_db)
        assert book.quota_weight == 100.0


class TestCooldownStall:
    """Regression: a lone cooling-down source must not freeze its channel."""

    def test_sole_source_sits_out_one_broadcast_then_returns(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        book.cooldown_remaining = 2  # as set by a 👎
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            first = run_release_cycle(in_memory_db, Path("/fake"))
            in_memory_db.commit()
            assert first == []                      # sat out this broadcast
            in_memory_db.refresh(book)
            assert book.cooldown_remaining == 1     # ...and the counter is still moving

            second = run_release_cycle(in_memory_db, Path("/fake"))
        assert len(second) >= 1                     # eligible again — not frozen


class TestExtraCap:
    """🪝 extras are limited per channel per release cycle (config.extra_per_channel_per_cycle)."""

    def _drop(self, db, book, n):
        d = Drop(book_id=book.id, feedback_token=f"xt{n}", reader_slug=f"xs{n}", chapter_start=0,
                 chapter_end=0, word_count=100, channel_id=book.channel_id, feed_key="1")
        db.add(d); db.flush()
        return d

    def _extra(self, db, drop, epub_path):
        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            return apply_feedback(db, drop, FeedbackAction.extra, Path("/fake"))

    def test_second_extra_in_a_batch_is_refused_by_the_gate(self, in_memory_db, epub_path):
        from app.planner.planner import extra_limit_reached
        book = _make_book(in_memory_db, calibre_id=1)
        d1, d2 = self._drop(in_memory_db, book, 1), self._drop(in_memory_db, book, 2)

        assert not extra_limit_reached(in_memory_db, d1)
        assert self._extra(in_memory_db, d1, epub_path) is not None
        assert extra_limit_reached(in_memory_db, d2)          # default allowance is 1

    def test_repeat_click_on_granted_drop_is_not_refused(self, in_memory_db, epub_path):
        from app.planner.planner import extra_limit_reached
        book = _make_book(in_memory_db, calibre_id=1)
        d1 = self._drop(in_memory_db, book, 1)
        self._extra(in_memory_db, d1, epub_path)
        assert not extra_limit_reached(in_memory_db, d1)       # a double-click, not a new request

    def test_allowance_is_configurable(self, in_memory_db, epub_path):
        from app.models import Config
        from app.planner.planner import extra_limit_reached
        in_memory_db.get(Config, 1).extra_per_channel_per_cycle = 2
        book = _make_book(in_memory_db, calibre_id=1)
        d1, d2, d3 = (self._drop(in_memory_db, book, i) for i in (1, 2, 3))
        self._extra(in_memory_db, d1, epub_path)
        assert not extra_limit_reached(in_memory_db, d2)
        self._extra(in_memory_db, d2, epub_path)
        assert extra_limit_reached(in_memory_db, d3)

    def test_release_cycle_resets_the_allowance(self, in_memory_db, epub_path):
        from app.planner.planner import extra_limit_reached
        book = _make_book(in_memory_db, calibre_id=1)
        d1, d2 = self._drop(in_memory_db, book, 1), self._drop(in_memory_db, book, 2)
        self._extra(in_memory_db, d1, epub_path)
        assert extra_limit_reached(in_memory_db, d2)
        in_memory_db.commit()

        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            run_release_cycle(in_memory_db, Path("/fake"))
        assert not extra_limit_reached(in_memory_db, d2)

    def test_allowance_is_per_channel(self, in_memory_db, epub_path):
        from app.models import Channel
        from app.planner.planner import extra_limit_reached
        other = Channel(name="Other", slug="other", parallel_slots=1, weight=1)
        in_memory_db.add(other); in_memory_db.flush()
        a = _make_book(in_memory_db, calibre_id=1, title="A")
        b = _make_book(in_memory_db, calibre_id=2, title="B", channel_id=other.id)
        da, db_ = self._drop(in_memory_db, a, 1), self._drop(in_memory_db, b, 2)
        self._extra(in_memory_db, da, epub_path)
        assert not extra_limit_reached(in_memory_db, db_)

    def test_refused_confirm_post_records_no_event_and_no_weight_change(self, in_memory_db, epub_path):
        """The idempotency trap: a refusal must not insert the FeedbackEvent, or the same drop
        could never be extra'd again after the next release."""
        from app.models import FeedbackEvent
        from app.routers.feedback import confirm_post
        book = _make_book(in_memory_db, calibre_id=1, quota_weight=1.0)
        d1, d2 = self._drop(in_memory_db, book, 1), self._drop(in_memory_db, book, 2)
        self._extra(in_memory_db, d1, epub_path)
        in_memory_db.commit()
        weight = book.quota_weight

        resp = confirm_post(d2.feedback_token, action="extra", db=in_memory_db)

        assert resp.status_code == 200 and b"No extra chapters left" in resp.body
        assert book.quota_weight == weight
        assert in_memory_db.query(FeedbackEvent).filter_by(drop_id=d2.id).count() == 0
        assert d2.acknowledged_at is None

        # ...and after a new batch opens the very same drop can be extra'd.
        from app.planner.planner import reset_extra_allowance
        reset_extra_allowance(in_memory_db)
        with patch("app.planner.planner.CalibreAdapter") as MockAdapter, \
                patch("app.websub.publisher.publish_updates"):
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            resp = confirm_post(d2.feedback_token, action="extra", db=in_memory_db)
        assert resp.status_code == 303
        assert in_memory_db.query(FeedbackEvent).filter_by(drop_id=d2.id).count() == 1

    def test_extra_on_paused_source_injects_nothing(self, in_memory_db, epub_path):
        book = _make_book(in_memory_db, calibre_id=1)
        book.paused = True
        d1 = self._drop(in_memory_db, book, 1)
        assert self._extra(in_memory_db, d1, epub_path) is None


class TestFreshPass:
    """A chapter that arrived by an upstream update is released in the next batch (budget
    permitting) instead of being left to the stochastic roll."""

    def _tracked(self, db, calibre_id, cursor=3, fresh_from=3, weight=1.0, fetched_at=None):
        b = _make_book(db, calibre_id=calibre_id, title=f"T{calibre_id}", quota_weight=weight)
        b.tracked = True
        b.cursor_chapter_index = cursor
        b.fresh_from_index = fresh_from
        b.last_fetch_at = fetched_at
        db.flush()
        return b

    def _plan(self, books, epub_path, budget=600, penalties=None):
        from app.planner.planner import _plan_drops
        adapter = _mock_adapter(1, epub_path)  # 5 × 500-word chapters
        return _plan_drops(books, adapter, budget=budget, base_budget=budget,
                           penalties=penalties)

    def test_fresh_chapter_ignores_the_stochastic_roll(self, in_memory_db, epub_path):
        """Even with acceptance forced to 0 (worst unread penalty) the fresh chapter still ships."""
        b = self._tracked(in_memory_db, 1)
        for seed in range(25):
            random.seed(seed)
            plans = self._plan([b], epub_path, penalties={b.id: 0.0})
            assert sum(len(p.chapters) for p in plans) == 1

    def test_non_fresh_source_is_still_stochastic(self, in_memory_db, epub_path):
        b = self._tracked(in_memory_db, 1, fresh_from=None)
        assert self._plan([b], epub_path, penalties={b.id: 0.0}) == []

    def test_reader_still_behind_is_not_fresh(self, in_memory_db, epub_path):
        """Cursor before the first new chapter → older chapters go first via the normal pass."""
        b = self._tracked(in_memory_db, 1, cursor=1, fresh_from=3)
        assert self._plan([b], epub_path, penalties={b.id: 0.0}) == []

    def test_one_unit_per_fresh_source_per_cycle(self, in_memory_db, epub_path):
        """A multi-chapter dump can't eat the batch: 3 fresh chapters → 1 released this cycle."""
        b = self._tracked(in_memory_db, 1, cursor=2, fresh_from=2)   # chapters 2,3,4 all fresh
        plans = self._plan([b], epub_path, budget=5000, penalties={b.id: 0.0})
        assert sum(len(p.chapters) for p in plans) == 1

    def test_budget_limits_fresh_and_oldest_arrival_wins(self, in_memory_db, epub_path):
        from datetime import datetime, timezone
        early = self._tracked(in_memory_db, 1, fetched_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
        late = self._tracked(in_memory_db, 2, fetched_at=datetime(2026, 7, 2, tzinfo=timezone.utc))
        plans = self._plan([late, early], epub_path, budget=600,
                           penalties={early.id: 0.0, late.id: 0.0})
        assert [p.book.id for p in plans] == [early.id]          # only one 500-word unit fits
        assert late.fresh_from_index == 3                         # ...and the other stays fresh

    def test_weight_zero_source_never_posts_even_when_fresh(self, in_memory_db, epub_path):
        b = self._tracked(in_memory_db, 1, weight=0.0)
        assert self._plan([b], epub_path) == []

    def test_cycle_releases_fresh_chapter_and_clears_flag_when_caught_up(self, in_memory_db, epub_path):
        b = self._tracked(in_memory_db, 1, cursor=4, fresh_from=4)   # one chapter left
        in_memory_db.commit()
        with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
            MockAdapter.return_value = _mock_adapter(1, epub_path)
            drops = run_release_cycle(in_memory_db, Path("/fake"))
        assert [d.book_id for d in drops] == [b.id]
        in_memory_db.refresh(b)
        assert b.cursor_chapter_index == 5 and b.fresh_from_index is None


class TestFreshMarking:
    """fetch.client.apply_result flags chapters that arrive by an *update*."""

    def _book(self, db, total):
        b = _make_book(db, calibre_id=1)
        b.tracked = True
        b.total_chapters = total
        b.cursor_chapter_index = total or 0
        db.flush()
        return b

    def _raw(self, count, **kw):
        return {"url": "u", "calibre_id": 1, "chapter_count": count, "error": None, "stub": None, **kw}

    def test_update_that_adds_chapters_marks_fresh_from_old_total(self, in_memory_db):
        from app.fetch.client import apply_result
        b = self._book(in_memory_db, total=10)
        apply_result(b, self._raw(12))
        assert b.fresh_from_index == 10

    def test_first_download_is_not_fresh(self, in_memory_db):
        from app.fetch.client import apply_result
        b = self._book(in_memory_db, total=None)   # never downloaded / import backfill
        apply_result(b, self._raw(200))
        assert b.fresh_from_index is None

    def test_no_new_chapters_is_not_fresh(self, in_memory_db):
        from app.fetch.client import apply_result
        b = self._book(in_memory_db, total=10)
        apply_result(b, self._raw(10))
        assert b.fresh_from_index is None

    def test_earliest_unreleased_fresh_index_is_kept(self, in_memory_db):
        from app.fetch.client import apply_result
        b = self._book(in_memory_db, total=10)
        apply_result(b, self._raw(12))       # fresh from 10
        apply_result(b, self._raw(13))       # a second update before the first is released
        assert b.fresh_from_index == 10

    def test_stub_clears_the_flag(self, in_memory_db):
        from app.fetch.client import apply_result
        b = self._book(in_memory_db, total=10)
        b.fresh_from_index = 8
        apply_result(b, self._raw(7, stub={"old": 10, "new": 7}))
        assert b.fresh_from_index is None
