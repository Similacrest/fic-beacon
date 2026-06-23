"""Budget / Drop Planner.

Budget model (global round-robin, never pre-slice):
  Phase 1 — guaranteed minimum: each active book always gets exactly one chapter
  regardless of budget. This is the "never starve a book" rule.

  Phase 2 — bonus round-robin: iterate active books (highest quota first) in
  repeated passes. Each pass offers one extra chapter to a book if BOTH
  conditions hold: (a) the book's pro-rata per-book share + tolerance allows it,
  AND (b) the global counter has headroom (global_words ≤ budget + tolerance).
  This bounds total overshoot to budget + tolerance + phase-1 overshoot rather
  than N × budget.

  Per-book share = budget * quota_weight / total_quota (proportional weight).
  Budget is a soft guide — chapters are never split; an oversized single chapter
  posts whole.
"""
from __future__ import annotations

import logging
import random
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.calibre.adapter import CalibreAdapter, CalibreBook
from app.epub.chapterizer import Chapter, chapterize
from app.models import (
    Book, BookStatus, BudgetMode, Channel, Config, Drop, FeedbackAction, FeedbackEvent, OngoingFeed,
)

logger = logging.getLogger(__name__)


@dataclass
class PlannedDrop:
    book: Book
    chapters: list[Chapter]
    word_count: int


def run_drop_cycle(session: Session, library_path: Path) -> list[Drop]:
    """Execute one scheduled drop cycle across all channels.

    The implicit default group (channel_id IS NULL) uses the Config budget/slots;
    each Channel uses its own. Cadence is global — every group drops this cycle.
    """
    cfg = _get_config(session)
    adapter = CalibreAdapter(library_path)
    drops: list[Drop] = []

    for channel, parallel_slots, base_budget, tolerance in _drop_groups(session, cfg):
        channel_id = channel.id if channel else None
        credit_holder = channel if channel else cfg

        # Promote queued books into open slots *first* (fresh imports start 'queued').
        _fill_empty_slots(session, parallel_slots, channel_id)
        session.flush()

        active_books = _active_books_in(session, channel_id)
        if not active_books:
            continue

        # Token-bucket budget: this cycle's allowance is the base budget plus any
        # signed carry-over from prior cycles, so the long-run mean tracks the base.
        available = base_budget + credit_holder.budget_credit
        effective = max(0, int(available))

        plans = _plan_drops(active_books, adapter, effective, tolerance)
        used = 0
        for plan in plans:
            drop = _materialise(session, plan, channel_id)
            if drop:
                drops.append(drop)
                used += plan.word_count
                _advance_cursor(session, plan, adapter, cfg)

        # Carry the leftover (can be negative after an oversized post); clamp so a big
        # overshoot doesn't suppress drops for many cycles.
        leftover = available - used
        credit_holder.budget_credit = max(-base_budget, min(base_budget, leftover))

        _fill_empty_slots(session, parallel_slots, channel_id)

    session.flush()
    return drops


def _drop_groups(session: Session, cfg: Config):
    """Yield (channel|None, parallel_slots, budget, tolerance) for every group.

    The default group (None) covers unchanneled books and uses the Config budget.
    """
    yield None, cfg.parallel_slots, _compute_budget(session, cfg), cfg.overshoot_tolerance
    for channel in session.query(Channel).order_by(Channel.queue_order, Channel.id).all():
        yield channel, channel.parallel_slots, _channel_budget(channel, cfg), cfg.overshoot_tolerance


def _channel_budget(channel: Channel, cfg: Config) -> int:
    if channel.budget_mode == BudgetMode.minutes:
        return channel.budget_minutes * cfg.wpm
    return channel.budget_words


def _channel_filter(channel_id: int | None):
    return Book.channel_id.is_(None) if channel_id is None else Book.channel_id == channel_id


def _active_books_in(session: Session, channel_id: int | None) -> list[Book]:
    return (
        session.query(Book)
        .filter(Book.status == BookStatus.active, _channel_filter(channel_id))
        .order_by(Book.slot_index, Book.queue_position)
        .all()
    )


def create_extra_drop(session: Session, book: Book, library_path: Path) -> Drop | None:
    """Inject one out-of-cycle drop for the given book (triggered by 'extra' feedback)."""
    cfg = _get_config(session)
    adapter = CalibreAdapter(library_path)
    calibre_book = adapter.get_book(book.calibre_id)
    if calibre_book is None:
        return None
    chapters = _get_chapters(calibre_book, adapter, book)
    if not chapters:
        return None
    plan = PlannedDrop(book=book, chapters=[chapters[0]], word_count=chapters[0].word_count)
    drop = _materialise(session, plan)
    if drop:
        _advance_cursor(session, plan, adapter, cfg)
    session.flush()
    return drop


# ── internals ────────────────────────────────────────────────────────────────


def _get_config(session: Session) -> Config:
    cfg = session.get(Config, 1)
    if cfg is None:
        raise RuntimeError("Config row missing — call init_db() first")
    return cfg


def _effective_budget(cfg: Config) -> int:
    """Base budget from config (words or minutes), before ongoing-feed adjustment."""
    if cfg.budget_mode == BudgetMode.minutes:
        return cfg.global_budget_minutes * cfg.wpm
    return cfg.global_budget_words


def _compute_budget(session: Session, cfg: Config) -> int:
    """Effective drop budget for this cycle.

    v2 balancing: when target_total_words is set, the synthetic budget is
    target_total_words minus the estimated word volume already arriving from
    the user's real ongoing fic feeds. This keeps combined reading volume
    near the configured target regardless of how many ongoing serials are
    actively updating.
    """
    base = _effective_budget(cfg)
    if not cfg.target_total_words:
        return base
    ongoing_words = sum(
        f.estimated_words_per_cycle
        for f in session.query(OngoingFeed).filter(OngoingFeed.is_active.is_(True)).all()
    )
    return max(0, cfg.target_total_words - ongoing_words)


def _plan_drops(
    active_books: list[Book],
    adapter: CalibreAdapter,
    budget: int,
    tolerance: int = 0,  # unused; kept for signature compatibility (stochastic handles overshoot)
) -> list[PlannedDrop]:
    """Pure-stochastic selection over whole units (chapters), never splitting.

    Repeated weight-ordered passes: a candidate unit of size w is included with
    probability p that falls as the cycle runs over budget and rises with the
    source's quota_weight. Excluded units roll over whole to a later cycle. There is
    *no* guaranteed first chapter — over budget, even a source's first unit can defer.
    A unit larger than the whole budget is posted whole (once per source per cycle),
    since it could never otherwise fit.
    """
    # Pre-load remaining chapters for every book (highest quota first for fair ordering).
    ordered = sorted(active_books, key=lambda b: -b.quota_weight)
    book_remaining: dict[int, list[Chapter]] = {}
    valid: list[Book] = []
    for book in ordered:
        calibre_book = adapter.get_book(book.calibre_id)
        if calibre_book is None:
            logger.warning(
                "Active book id=%s (calibre_id=%s) not found in metadata.db — skipping",
                book.id, book.calibre_id,
            )
            continue
        rem = _get_chapters(calibre_book, adapter, book)
        if rem:
            book_remaining[book.id] = list(rem)
            valid.append(book)
        else:
            logger.warning(
                "Active book '%s' yielded no chapters at cursor %s — EPUB missing at "
                "%s or already complete; no drop produced",
                book.title, book.cursor_chapter_index, adapter.epub_path(calibre_book),
            )

    if not valid:
        return []

    selected: dict[int, list[Chapter]] = {b.id: [] for b in valid}
    used = 0

    changed = True
    while changed:
        changed = False
        for book in valid:
            remaining = book_remaining[book.id]
            if not remaining:
                continue
            unit = remaining[0]
            p = _inclusion_probability(
                word_count=unit.word_count,
                used=used,
                budget=budget,
                weight=book.quota_weight,
                first_for_book=not selected[book.id],
            )
            if random.random() < p:
                selected[book.id].append(unit)
                remaining.pop(0)
                used += unit.word_count
                changed = True

    return [
        PlannedDrop(book=book, chapters=chs, word_count=sum(c.word_count for c in chs))
        for book in valid
        if (chs := selected[book.id])
    ]


def _inclusion_probability(
    word_count: int, used: int, budget: int, weight: float, first_for_book: bool
) -> float:
    """Probability of including a whole unit this cycle (see _plan_drops)."""
    if word_count <= 0:
        return 1.0
    # Oversized unit: larger than the entire budget → post whole, but only as a
    # source's first unit this cycle so we don't dump a whole book at a tiny budget.
    if word_count > budget and first_for_book:
        return 1.0
    remaining = budget - used
    if remaining <= 0:
        return 0.0
    base = min(1.0, remaining / word_count)
    # Weight bias: higher weight pushes p toward 1, lower weight toward 0.
    return base ** (1.0 / max(weight, 1e-6))


def _get_chapters(
    calibre_book: CalibreBook, adapter: CalibreAdapter, book: Book
) -> list[Chapter]:
    epub_path = adapter.epub_path(calibre_book)
    if not epub_path.exists():
        return []
    all_chapters = chapterize(epub_path)
    return all_chapters[book.cursor_chapter_index:]


def _materialise(session: Session, plan: PlannedDrop, channel_id: int | None = None) -> Drop | None:
    if not plan.chapters:
        return None

    first = plan.chapters[0]
    last = plan.chapters[-1]
    combined_html = "\n".join(
        f'<section class="chapter">\n{ch.html}\n</section>' for ch in plan.chapters
    )
    titles = "; ".join(ch.title for ch in plan.chapters)

    drop = Drop(
        book_id=plan.book.id,
        channel_id=channel_id,
        feed_key=str(plan.book.slot_index or 1),
        word_count=plan.word_count,
        chapter_start=first.index,
        chapter_end=last.index,
        chapter_titles=titles,
        content_html=combined_html,
        # Per-chapter canonical link for the first chapter in this drop
        source_url=first.source_url,
        feedback_token=secrets.token_urlsafe(24),
        reader_slug=str(uuid.uuid4()),
    )
    session.add(drop)
    return drop


def _advance_cursor(
    session: Session,
    plan: PlannedDrop,
    adapter: CalibreAdapter,
    cfg: Config,
) -> None:
    book = plan.book
    calibre_book = adapter.get_book(book.calibre_id)
    if calibre_book is None:
        return
    epub_path = adapter.epub_path(calibre_book)
    if not epub_path.exists():
        return
    all_chapters = chapterize(epub_path)
    new_cursor = book.cursor_chapter_index + len(plan.chapters)
    if new_cursor >= len(all_chapters):
        book.status = BookStatus.completed
        book.cursor_chapter_index = len(all_chapters)
        book.total_chapters = len(all_chapters)
        book.slot_index = None  # free the slot for the next queued book
    else:
        book.cursor_chapter_index = new_cursor
        book.total_chapters = len(all_chapters)


def _lowest_free_slot(used: set[int], parallel_slots: int) -> int:
    for i in range(1, parallel_slots + 1):
        if i not in used:
            return i
    return max(used, default=0) + 1  # over-subscribed; keep slots unique anyway


def _fill_empty_slots(
    session: Session, parallel_slots: int, channel_id: int | None = None
) -> None:
    """Promote queued books into open slots *within one channel group*.

    Also normalizes slot assignment: any already-active book missing a slot_index
    gets the lowest free slot. channel_id=None is the implicit default group.
    """
    active = _active_books_in(session, channel_id)
    used = {b.slot_index for b in active if b.slot_index is not None}
    for book in active:
        if book.slot_index is None:
            book.slot_index = _lowest_free_slot(used, parallel_slots)
            used.add(book.slot_index)

    slots_available = parallel_slots - len(active)
    if slots_available <= 0:
        return

    queued = (
        session.query(Book)
        .filter(Book.status == BookStatus.queued, _channel_filter(channel_id))
        .order_by(Book.queue_position)
        .limit(slots_available)
        .all()
    )
    for book in queued:
        book.status = BookStatus.active
        book.slot_index = _lowest_free_slot(used, parallel_slots)
        used.add(book.slot_index)


def apply_feedback(
    session: Session,
    drop: Drop,
    action: FeedbackAction,
    library_path: Path,
) -> None:
    """Apply a feedback action and record the event.

    Idempotent per (drop, action): a reader/proxy prefetching a bare-GET link, or a
    double-click, applies the effect at most once. The four actions form a symmetric
    strength scale — extra (super-up) · up · down · drop (super-down).
    """
    cfg = _get_config(session)
    book = drop.book

    # Idempotency guard — skip if this exact (drop, action) was already recorded.
    already = (
        session.query(FeedbackEvent)
        .filter(FeedbackEvent.drop_id == drop.id, FeedbackEvent.action == action)
        .first()
    )
    if already is not None:
        return

    session.add(
        FeedbackEvent(
            token=drop.feedback_token,
            book_id=book.id,
            drop_id=drop.id,
            action=action,
        )
    )

    if action == FeedbackAction.up:
        book.thumbs_up += 1
        book.quota_weight = max(0.1, book.quota_weight * 1.25)

    elif action == FeedbackAction.down:
        book.thumbs_down += 1
        if book.thumbs_down >= cfg.thumbs_down_drop_threshold:
            book.status = BookStatus.dropped
            book.slot_index = None
            _refill_book_channel(session, book, cfg)
        else:
            # Gently reduce share
            book.quota_weight = max(0.1, book.quota_weight * 0.8)

    elif action == FeedbackAction.extra:
        # Super-up: count as three upvotes, boost weight strongly, and inject a drop.
        book.thumbs_up += 3
        book.quota_weight = max(0.1, book.quota_weight * (1.25 ** 3))
        create_extra_drop(session, book, library_path)

    elif action == FeedbackAction.drop:
        # Super-down: drop the source immediately, regardless of threshold.
        book.status = BookStatus.dropped
        book.slot_index = None
        _refill_book_channel(session, book, cfg)

    session.flush()


def _refill_book_channel(session: Session, book: Book, cfg: Config) -> None:
    """Promote the next queued book into the slot freed within this book's channel."""
    if book.channel_id is None:
        _fill_empty_slots(session, cfg.parallel_slots, None)
        return
    channel = session.get(Channel, book.channel_id)
    slots = channel.parallel_slots if channel else cfg.parallel_slots
    _fill_empty_slots(session, slots, book.channel_id)
