"""Budget / Drop Planner.

Budget model (per-channel, pure-stochastic, never pre-slice):
  Every source belongs to a channel, and each channel runs independently each cycle
  with effective budget B = base_budget + budget_credit (signed carry-over so the
  long-run mean tracks the base budget).

  Candidates are each active source's next whole unit, taken in weight-ordered passes.
  A unit of size w is *included* this cycle with probability
  p = clamp((B − used)/w, 0, 1), biased up by the source's quota_weight (p = base ** (1/w)).
  Included → emit + advance cursor; excluded → roll the whole unit over to a later cycle.

  There is *no* guaranteed first chapter: over budget, even a source's first unit can
  defer, and a low-weight source may get nothing some cycles. A unit larger than the
  whole budget is posted whole (once per source per cycle) since it could never fit.
  Units are never split — an oversized single chapter posts whole. After the pass,
  budget_credit += base_budget − used (clamped to ±base_budget).
"""
from __future__ import annotations

import logging
import random
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.calibre.adapter import CalibreAdapter, CalibreBook
from app.config import settings
from app.epub.chapterizer import Chapter, chapterize, materialize_image_urls
from app.models import (
    Book, BookStatus, BudgetMode, Channel, Config, Drop, FeedbackAction,
    FeedbackEvent,
)

logger = logging.getLogger(__name__)


@dataclass
class Unit:
    """One drop-able chunk — a whole EPUB chapter.

    Field-compatible with Chapter (title/html/word_count/source_url/index). Both backlog
    and tracked (auto-updating) books are EPUBs now, so there is a single unit shape.
    """
    title: str
    html: str
    word_count: int
    source_url: str | None
    index: int


@dataclass
class PlannedDrop:
    book: Book
    chapters: list[Unit]
    word_count: int


@dataclass
class SkippedSource:
    """A source that had pending units but left ≥1 un-dropped this broadcast.

    held_out=True when *nothing* dropped (lost the weighted budget roll outright);
    otherwise some units dropped and the rest rolled over to a later broadcast.
    """
    book: Book
    dropped_count: int
    remaining_count: int

    @property
    def held_out(self) -> bool:
        return self.dropped_count == 0


def _remaining_units(book: Book, adapter: CalibreAdapter) -> list[Unit] | None:
    """Remaining units for a source. None = source unresolvable (warned); [] = none now."""
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
    """Execute one scheduled drop cycle across every channel.

    Every source belongs to a channel; each channel drops independently using its own
    budget and slots. Cadence is global — every channel drops this cycle.
    """
    cfg = _get_config(session)
    adapter = CalibreAdapter(library_path)
    drops: list[Drop] = []
    skip_log: list[dict] = []

    channels = (
        session.query(Channel)
        .order_by(Channel.queue_order, Channel.id)
        .all()
    )
    for channel in channels:
        base_budget = _channel_budget(channel, cfg)

        # Assign slots first: promote queued EPUBs and pin ongoings (fresh imports start
        # 'queued'), so every active source's drops land in the right slot's feed.
        _assign_slots(session, channel.parallel_slots, channel.id)
        session.flush()

        active_books = _active_books_in(session, channel.id)
        if not active_books:
            continue

        # Token-bucket budget: this cycle's allowance is the base budget plus any
        # signed carry-over from prior cycles, so the long-run mean tracks the base.
        available = base_budget + channel.budget_credit
        effective = max(0, int(available))

        skips: list[SkippedSource] = []
        plans = _plan_drops(active_books, adapter, effective, skips_out=skips)
        used = 0
        for plan in plans:
            drop = _materialise(session, plan, channel.id)
            if drop:
                drops.append(drop)
                used += plan.word_count
                _advance_cursor(session, plan, adapter, cfg)

        for skip in skips:
            skip_log.append({
                "channel": channel.name,
                "title": skip.book.title,
                "kind": "tracked" if skip.book.tracked else "backlog",
                "weight": round(skip.book.quota_weight, 2),
                "dropped": skip.dropped_count,
                "remaining": skip.remaining_count,
                "held_out": skip.held_out,
            })

        # Carry the leftover (can be negative after an oversized post); clamp so a big
        # overshoot doesn't suppress drops for many cycles.
        leftover = available - used
        channel.budget_credit = max(-base_budget, min(base_budget, leftover))

        # Re-fill slots freed by EPUBs that just completed this broadcast.
        _assign_slots(session, channel.parallel_slots, channel.id)

    import json

    from app.state import LAST_DROP_RUN, LAST_SKIPS, mark_run, set_value
    mark_run(session, LAST_DROP_RUN)
    set_value(session, LAST_SKIPS, json.dumps(skip_log))
    session.flush()
    return drops


def _channel_budget(channel: Channel, cfg: Config) -> int:
    if channel.budget_mode == BudgetMode.minutes:
        return channel.budget * cfg.wpm
    return channel.budget


def _active_books_in(session: Session, channel_id: int) -> list[Book]:
    return (
        session.query(Book)
        .filter(Book.status == BookStatus.active, Book.channel_id == channel_id)
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
    drop = _materialise(session, plan, book.channel_id)  # sets feed_key from book.slot_index
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


def _plan_drops(
    active_books: list[Book],
    adapter: CalibreAdapter,
    budget: int,
    skips_out: list["SkippedSource"] | None = None,
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

    if skips_out is not None:
        for book in valid:
            remaining_count = len(book_remaining[book.id])
            if remaining_count > 0:  # something rolled over to a later broadcast
                skips_out.append(SkippedSource(
                    book=book,
                    dropped_count=len(selected[book.id]),
                    remaining_count=remaining_count,
                ))

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


def _materialise(session: Session, plan: PlannedDrop, channel_id: int) -> Drop | None:
    if not plan.chapters:
        return None

    first = plan.chapters[0]
    last = plan.chapters[-1]
    combined_html = "\n".join(
        f'<section class="chapter">\n{ch.html}\n</section>' for ch in plan.chapters
    )
    # Resolve in-EPUB image references to this book's read-only image route so the
    # stored HTML is self-contained (and byte-stable for WebSub).
    combined_html = materialize_image_urls(
        combined_html, plan.book.calibre_id, settings.base_url
    )
    titles = "; ".join(ch.title for ch in plan.chapters)

    feed_key = str(plan.book.slot_index or 1)
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
    book.total_chapters = len(all_chapters)
    if new_cursor >= len(all_chapters):
        book.cursor_chapter_index = len(all_chapters)
        if book.tracked:
            # Tracked stories never "complete" — they self-gate at the end of the current
            # EPUB and resume when the next fetch adds chapters (mtime-cached chapterizer
            # picks them up automatically). Keep the slot pinned.
            return
        book.status = BookStatus.completed
        book.slot_index = None  # free the slot for the next queued book
    else:
        book.cursor_chapter_index = new_cursor


def _lowest_free_slot(used: set[int], parallel_slots: int) -> int:
    for i in range(1, parallel_slots + 1):
        if i not in used:
            return i
    return max(used, default=0) + 1  # over-subscribed; keep slots unique anyway


def _assign_slots(
    session: Session, parallel_slots: int, channel_id: int
) -> None:
    """Pin a channel's sources to feed slots (1..parallel_slots).

    A slot is a feed *bucket*, not a single-book reservation:

    - **Backlog (untracked) EPUBs** stream one-at-a-time per slot. At most `parallel_slots`
      are active (one per slot); any extra active ones are demoted back to `queued`, and
      queued ones are promoted into slots with no active backlog book.
    - **Tracked (auto-updating) books** are uncapped and never queued. Each is load-balanced
      onto a slot — the slot with the fewest pinned works, tie-broken by the fewest chapters
      ever dropped there — so several tracked stories may share a slot alongside the slot's
      backlog book.

    Assignment is sticky: a source with a valid, unique-where-required slot keeps it;
    only sources lacking one are (re)placed. Drops carry the source's slot as feed_key.
    """
    def _valid(idx: int | None) -> bool:
        return idx is not None and 1 <= idx <= parallel_slots

    # ── Backlog (untracked) books: one active per slot, capped at parallel_slots ──
    active_epubs = (
        session.query(Book)
        .filter(
            Book.channel_id == channel_id,
            Book.tracked.is_(False),
            Book.status == BookStatus.active,
        )
        .order_by(Book.queue_position)
        .all()
    )
    # Demote any active backlog books beyond the cap (e.g. after shrinking parallel_slots).
    for book in active_epubs[parallel_slots:]:
        book.status = BookStatus.queued
        book.slot_index = None
    active_epubs = active_epubs[:parallel_slots]

    epub_slots: set[int] = set()
    needs_slot: list[Book] = []
    for book in active_epubs:
        if _valid(book.slot_index) and book.slot_index not in epub_slots:
            epub_slots.add(book.slot_index)
        else:  # missing, duplicate, or out-of-range
            book.slot_index = None
            needs_slot.append(book)
    for book in needs_slot:
        book.slot_index = _lowest_free_slot(epub_slots, parallel_slots)
        epub_slots.add(book.slot_index)

    # Promote queued EPUBs into any slot with no active EPUB.
    free = parallel_slots - len(active_epubs)
    if free > 0:
        queued = (
            session.query(Book)
            .filter(
                Book.channel_id == channel_id,
                Book.tracked.is_(False),
                Book.status == BookStatus.queued,
            )
            .order_by(Book.queue_position)
            .limit(free)
            .all()
        )
        for book in queued:
            book.status = BookStatus.active
            book.slot_index = _lowest_free_slot(epub_slots, parallel_slots)
            epub_slots.add(book.slot_index)

    # ── Tracked books: uncapped, load-balanced across all slots (sticky) ───────
    ongoings = (
        session.query(Book)
        .filter(
            Book.channel_id == channel_id,
            Book.tracked.is_(True),
            Book.status == BookStatus.active,
        )
        .all()
    )
    work_count: dict[int, int] = {s: 0 for s in range(1, parallel_slots + 1)}
    for slot in epub_slots:
        work_count[slot] += 1
    for ongoing in ongoings:
        if _valid(ongoing.slot_index):
            work_count[ongoing.slot_index] += 1
    # Chapters ever dropped into each slot (string feed_key), used as the tie-breaker.
    chapter_freq = dict(
        session.query(Drop.feed_key, func.count(Drop.id))
        .filter(Drop.channel_id == channel_id)
        .group_by(Drop.feed_key)
        .all()
    )

    def _balanced_slot() -> int:
        return min(
            range(1, parallel_slots + 1),
            key=lambda s: (work_count[s], int(chapter_freq.get(str(s), 0)), s),
        )

    for ongoing in ongoings:
        if not _valid(ongoing.slot_index):
            slot = _balanced_slot()
            ongoing.slot_index = slot
            work_count[slot] += 1


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
            _refill_book_channel(session, book)
        else:
            # Gently reduce share
            book.quota_weight = max(0.1, book.quota_weight * 0.8)

    elif action == FeedbackAction.extra:
        # Super-up: count as three upvotes, boost weight by the configurable factor, inject a drop.
        book.thumbs_up += 3
        book.quota_weight = max(0.1, book.quota_weight * cfg.extra_boost_multiplier)
        extra_drop = create_extra_drop(session, book, library_path)

    elif action == FeedbackAction.drop:
        # Super-down: drop the source immediately, regardless of threshold.
        book.status = BookStatus.dropped
        book.slot_index = None
        _refill_book_channel(session, book)

    session.flush()
    return extra_drop


def _refill_book_channel(session: Session, book: Book) -> None:
    """Promote the next queued EPUB into the slot freed within this book's channel."""
    channel = session.get(Channel, book.channel_id)
    if channel is not None:
        _assign_slots(session, channel.parallel_slots, book.channel_id)
