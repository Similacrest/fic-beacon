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

import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.calibre.adapter import CalibreAdapter, CalibreBook
from app.epub.chapterizer import Chapter, chapterize
from app.models import Book, BookStatus, BudgetMode, Config, Drop, FeedbackAction, FeedbackEvent, OngoingFeed


@dataclass
class PlannedDrop:
    book: Book
    chapters: list[Chapter]
    word_count: int


def run_drop_cycle(session: Session, library_path: Path) -> list[Drop]:
    """Execute one scheduled drop cycle. Returns the list of created Drop rows."""
    cfg = _get_config(session)
    budget = _compute_budget(session, cfg)

    active_books = (
        session.query(Book)
        .filter(Book.status == BookStatus.active)
        .order_by(Book.queue_position)
        .all()
    )
    if not active_books:
        return []

    adapter = CalibreAdapter(library_path)
    plans = _plan_drops(active_books, adapter, budget, cfg.overshoot_tolerance)

    drops: list[Drop] = []
    for plan in plans:
        drop = _materialise(session, plan)
        if drop:
            drops.append(drop)
            _advance_cursor(session, plan, adapter, cfg)

    _fill_empty_slots(session, cfg.parallel_slots)
    session.flush()
    return drops


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
    tolerance: int,
) -> list[PlannedDrop]:
    total_quota = sum(b.quota_weight for b in active_books) or 1.0

    # Pre-load remaining chapters for every book (highest quota first for fair ordering)
    ordered = sorted(active_books, key=lambda b: -b.quota_weight)
    book_remaining: dict[int, list[Chapter]] = {}
    valid: list[Book] = []
    for book in ordered:
        calibre_book = adapter.get_book(book.calibre_id)
        if calibre_book is None:
            continue
        rem = _get_chapters(calibre_book, adapter, book)
        if rem:
            book_remaining[book.id] = list(rem)
            valid.append(book)

    if not valid:
        return []

    selected: dict[int, list[Chapter]] = {b.id: [] for b in valid}
    global_words = 0

    # Phase 1: give every book its first chapter unconditionally
    for book in valid:
        ch = book_remaining[book.id].pop(0)
        selected[book.id].append(ch)
        global_words += ch.word_count

    # Phase 2: round-robin bonus chapters while the global counter has headroom
    changed = True
    while changed:
        changed = False
        for book in valid:
            remaining = book_remaining[book.id]
            if not remaining:
                continue
            next_ch = remaining[0]
            per_book_share = int(budget * book.quota_weight / total_quota)
            book_words = sum(c.word_count for c in selected[book.id])
            if (
                book_words + next_ch.word_count <= per_book_share + tolerance
                and global_words + next_ch.word_count <= budget + tolerance
            ):
                selected[book.id].append(next_ch)
                book_remaining[book.id].pop(0)
                global_words += next_ch.word_count
                changed = True

    return [
        PlannedDrop(book=book, chapters=chs, word_count=sum(c.word_count for c in chs))
        for book in valid
        if (chs := selected[book.id])
    ]


def _get_chapters(
    calibre_book: CalibreBook, adapter: CalibreAdapter, book: Book
) -> list[Chapter]:
    epub_path = adapter.epub_path(calibre_book)
    if not epub_path.exists():
        return []
    all_chapters = chapterize(epub_path)
    return all_chapters[book.cursor_chapter_index:]


def _materialise(session: Session, plan: PlannedDrop) -> Drop | None:
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
    else:
        book.cursor_chapter_index = new_cursor
        book.total_chapters = len(all_chapters)


def _fill_empty_slots(session: Session, parallel_slots: int) -> None:
    """Promote queued books into any slots freed by completed/dropped books."""
    active_count = (
        session.query(Book).filter(Book.status == BookStatus.active).count()
    )
    slots_available = parallel_slots - active_count
    if slots_available <= 0:
        return

    queued = (
        session.query(Book)
        .filter(Book.status == BookStatus.queued)
        .order_by(Book.queue_position)
        .limit(slots_available)
        .all()
    )
    for book in queued:
        book.status = BookStatus.active


def apply_feedback(
    session: Session,
    drop: Drop,
    action: FeedbackAction,
    library_path: Path,
) -> None:
    """Apply a feedback action and record the event."""
    cfg = _get_config(session)
    book = drop.book

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
            _fill_empty_slots(session, cfg.parallel_slots)
        else:
            # Gently reduce share
            book.quota_weight = max(0.1, book.quota_weight * 0.8)

    elif action == FeedbackAction.extra:
        create_extra_drop(session, book, library_path)

    session.flush()
