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
    Book, BookKind, BookStatus, BudgetMode, Channel, Config, Drop, FeedbackAction,
    FeedbackEvent, OngoingEntry,
)

logger = logging.getLogger(__name__)


@dataclass
class Unit:
    """One drop-able chunk — an EPUB chapter or a buffered ongoing entry.

    Field-compatible with Chapter (title/html/word_count/source_url/index) so the
    planner and feed builder treat both source kinds identically. entry_id is set
    only for ongoing units, so materialising can mark the buffered entry released.
    """
    title: str
    html: str
    word_count: int
    source_url: str | None
    index: int
    entry_id: int | None = None


@dataclass
class PlannedDrop:
    book: Book
    chapters: list[Unit]
    word_count: int


def _remaining_units(book: Book, adapter: CalibreAdapter) -> list[Unit] | None:
    """Remaining units for a source. None = source unresolvable (warned); [] = none now."""
    if book.kind == BookKind.ongoing:
        entries = sorted(
            (e for e in book.ongoing_entries if not e.released),
            key=lambda e: (e.published_at, e.id),
        )
        return [
            Unit(title=e.title or f"Update {i + 1}", html=e.content_html,
                 word_count=e.word_count, source_url=e.link, index=i, entry_id=e.id)
            for i, e in enumerate(entries)
        ]

    calibre_book = adapter.get_book(book.calibre_id)
    if calibre_book is None:
        logger.warning(
            "Active book id=%s (calibre_id=%s) not found in metadata.db — skipping",
            book.id, book.calibre_id,
        )
        return None
    return [
        Unit(title=c.title, html=c.html, word_count=c.word_count,
             source_url=c.source_url, index=c.index)
        for c in _get_chapters(calibre_book, adapter, book)
    ]


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
    units = _remaining_units(book, adapter)
    if not units:
        return None
    plan = PlannedDrop(book=book, chapters=[units[0]], word_count=units[0].word_count)
    channel_id = book.channel_id
    drop = _materialise(session, plan, channel_id)
    if drop:
        drop.feed_key = "ongoing" if book.kind == BookKind.ongoing else str(book.slot_index or 1)
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
    """Base drop budget for the default (unchanneled) group.

    Ongoing serials are now syndicated as in-budget sources (they compete in the
    weighted stochastic budget like EPUBs), so there is no word-count subtraction.
    """
    return _effective_budget(cfg)


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
    # Pre-load remaining units for every source (highest quota first for fair ordering).
    ordered = sorted(active_books, key=lambda b: -b.quota_weight)
    book_remaining: dict[int, list[Unit]] = {}
    valid: list[Book] = []
    for book in ordered:
        units = _remaining_units(book, adapter)
        if units is None:
            continue  # unresolvable source — already warned
        if units:
            book_remaining[book.id] = list(units)
            valid.append(book)
        else:
            logger.info("Active source '%s' has no pending units this cycle", book.title)

    if not valid:
        return []

    selected: dict[int, list[Unit]] = {b.id: [] for b in valid}
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

    feed_key = "ongoing" if plan.book.kind == BookKind.ongoing else str(plan.book.slot_index or 1)
    drop = Drop(
        book_id=plan.book.id,
        channel_id=channel_id,
        feed_key=feed_key,
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
    # For ongoing sources, mark the buffered entries this drop released.
    entry_ids = [u.entry_id for u in plan.chapters if u.entry_id is not None]
    if entry_ids:
        session.flush()
        for entry in session.query(OngoingEntry).filter(OngoingEntry.id.in_(entry_ids)):
            entry.released = True
            entry.drop_id = drop.id
    return drop


def _advance_cursor(
    session: Session,
    plan: PlannedDrop,
    adapter: CalibreAdapter,
    cfg: Config,
) -> None:
    book = plan.book
    if book.kind == BookKind.ongoing:
        return  # ongoing sources never "complete"; entries are released in _materialise
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
    """Promote queued *EPUB* books into open numbered slots within one channel group.

    Ongoing sources are not slot-gated — they're always-active and share the channel's
    'ongoing' feed — so slots count and promote backlog (EPUB) books only. Also
    normalizes slot assignment for active EPUB books missing a slot_index.
    """
    active = [b for b in _active_books_in(session, channel_id) if b.kind == BookKind.epub]
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
        .filter(
            Book.status == BookStatus.queued,
            Book.kind == BookKind.epub,
            _channel_filter(channel_id),
        )
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
) -> Drop | None:
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
        return None

    extra_drop: Drop | None = None
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
        extra_drop = create_extra_drop(session, book, library_path)

    elif action == FeedbackAction.drop:
        # Super-down: drop the source immediately, regardless of threshold.
        book.status = BookStatus.dropped
        book.slot_index = None
        _refill_book_channel(session, book, cfg)

    session.flush()
    return extra_drop


def _refill_book_channel(session: Session, book: Book, cfg: Config) -> None:
    """Promote the next queued book into the slot freed within this book's channel."""
    if book.channel_id is None:
        _fill_empty_slots(session, cfg.parallel_slots, None)
        return
    channel = session.get(Channel, book.channel_id)
    slots = channel.parallel_slots if channel else cfg.parallel_slots
    _fill_empty_slots(session, slots, book.channel_id)
