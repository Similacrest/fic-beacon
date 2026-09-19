"""Budget / Drop Planner.

Budget model (per-channel, slot-diverse, weight-throttled, never pre-slice):
  Every source belongs to a channel, and each channel runs independently each cycle
  with effective budget B = base_budget + budget_credit (signed carry-over so the
  long-run mean tracks the base budget).

  Selection is a **slot round-robin**: rotate through the channel's occupied feed slots and,
  on each slot's turn, let one *weight-proportional* random source pinned to that slot drop its
  next whole unit if a stochastic budget roll (p = clamp((B − used)/w, 0, 1)) passes. An idle
  slot passes its turn, spilling its budget to slots that still have content — so drops spread
  across the slot feeds (diversity) with no wasted budget, while quota_weight governs a source's
  share *within* its slot (down-voting shrinks its slice relative to its slot-mates).

  Chapters that just arrived by an upstream update are the exception: a *fresh pass* releases
  one per fresh tracked source per cycle deterministically (no roll, no unread penalty) while budget
  lasts, so a new chapter is never left to a dice roll. Otherwise there is *no* guaranteed first
  chapter: over budget even a source's first unit can defer, and a low-share source may get nothing
  some cycles. A unit larger than the whole base budget is posted
  whole (once per source per cycle, by a prior accumulation pass) since it could never fit. Units
  are never split. After the pass, budget_credit += base_budget − used (clamped to ±base_budget,
  or up to the largest pending oversized unit).
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
    FeedbackEvent, utcnow,
)
from app.state import EXTRA_USED_PREFIX, delete_value, get_value, list_with_prefix, set_value

logger = logging.getLogger(__name__)

# Hard ceiling on quota_weight. Votes are additive (cfg.vote_step / cfg.extra_boost_step), so the
# natural range is ~0–3; the cap only bites for someone who spams 🪝 for months.
WEIGHT_CAP = 100.0

# Soft read-gating: a source with several *consecutive* unread drops has its stochastic acceptance
# scaled down (0.8, 0.6, 0.4 … floored at cfg.unacked_penalty_floor), so an un-caught-up reader falls
# behind more slowly. It's a transient nudge: it never touches quota_weight and evaporates the
# moment any drop is read. See _unread_penalties / _plan_drops.
_UNREAD_RAMP_STEP = 0.2
_UNREAD_STREAK_LOOKBACK = 8


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


def run_release_cycle(session: Session, library_path: Path) -> list[Drop]:
    """Execute one scheduled release cycle across every channel.

    Every source belongs to a channel; each channel drops independently using its own
    budget and slots. Cadence is global — every channel drops this cycle.
    """
    cfg = _get_config(session)
    adapter = CalibreAdapter(library_path)
    drops: list[Drop] = []
    skip_log: list[dict] = []

    # A new batch opens: every channel's 🪝 extra allowance starts over.
    reset_extra_allowance(session)

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

        # Tick the 👎 cooldown *before* selecting, and before the empty-channel early exit: a
        # channel whose only source is cooling down has no active books, so ticking after the
        # `continue` would freeze it forever. A 👎 sets cooldown=2, so it is 1 when selection
        # runs — the source sits out exactly one broadcast and is eligible again the next.
        _tick_cooldowns(session, channel.id)
        session.flush()

        active_books = _active_books_in(session, channel.id)
        if not active_books:
            continue

        # Soft read-gating: sources with a streak of unread drops get a ramped-down stochastic
        # acceptance this cycle (they still trickle). One unread drop is normal — no penalty.
        penalties = _unread_penalties(session, active_books, cfg.unacked_penalty_floor)

        # Token-bucket budget: this cycle's allowance is the base budget plus any
        # signed carry-over from prior cycles, so the long-run mean tracks the base.
        available = base_budget + channel.budget_credit
        effective = max(0, int(available))

        skips: list[SkippedSource] = []
        stats: dict = {}
        plans = _plan_drops(
            active_books, adapter, effective, base_budget=int(base_budget),
            skips_out=skips, stats_out=stats, penalties=penalties,
            weight_skip_floor=cfg.weight_skip_floor,
        )
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

        # Carry the leftover (can be negative after an oversized post). Positive credit may
        # accumulate up to the largest pending unit so an oversized chapter can be saved up
        # for across cycles; with nothing oversized pending it caps at the base budget (so an
        # idle channel doesn't runaway). Negative is clamped to one base so a big overshoot
        # doesn't suppress drops for many cycles.
        positive_cap = max(int(base_budget), stats.get("max_pending_unit", 0))
        leftover = available - used
        channel.budget_credit = max(-base_budget, min(positive_cap, leftover))

        # Re-fill slots freed by EPUBs that just completed this broadcast.
        _assign_slots(session, channel.parallel_slots, channel.id)

    import json

    from app.state import LAST_RELEASE_RUN, LAST_SKIPS, mark_run
    mark_run(session, LAST_RELEASE_RUN)
    set_value(session, LAST_SKIPS, json.dumps(skip_log))
    session.flush()
    return drops


def _channel_budget(channel: Channel, cfg: Config) -> float:
    if channel.budget_mode == BudgetMode.minutes:
        return channel.budget * cfg.wpm
    return channel.budget


def _active_books_in(session: Session, channel_id: int) -> list[Book]:
    return (
        session.query(Book)
        .filter(
            Book.status == BookStatus.active,
            Book.channel_id == channel_id,
            Book.paused.is_(False),  # paused sources broadcast nothing
            Book.cooldown_remaining <= 0,  # 👎-down cooldown: sit out a couple of broadcasts
        )
        .order_by(Book.slot_index, Book.queue_position)
        .all()
    )


def _unread_penalty(streak: int, floor: float) -> float:
    """Acceptance multiplier for `streak` consecutive unread drops.

    0 or 1 unread → 1.0 (one unread drop is just "not read yet", never penalised); then 0.8, 0.6,
    0.4 …, never below `floor`.
    """
    if streak <= 1:
        return 1.0
    return max(floor, 1.0 - _UNREAD_RAMP_STEP * (streak - 1))


def _unread_penalties(session: Session, books: list[Book], floor: float) -> dict[int, float]:
    """Per-source unread-streak multipliers (only sources actually penalised are present).

    The streak is the number of *consecutive* most-recent drops still unacknowledged, derived from
    the drop rows themselves (no counter to keep in sync with the three ack paths). A 🪝-injected
    drop is created already acknowledged, so asking for an extra chapter resets the streak — it is
    evidence of engagement, never of falling behind. Best-effort: a drop is acknowledged on /read/
    open, any /fb/ click, or the explicit ✓ Mark-read action.
    """
    out: dict[int, float] = {}
    for book in books:
        recent = (
            session.query(Drop.acknowledged_at)
            .filter(Drop.book_id == book.id)
            .order_by(Drop.published_at.desc(), Drop.id.desc())
            .limit(_UNREAD_STREAK_LOOKBACK)
            .all()
        )
        streak = 0
        for (acked_at,) in recent:
            if acked_at is not None:
                break
            streak += 1
        penalty = _unread_penalty(streak, floor)
        if penalty < 1.0:
            out[book.id] = penalty
    return out


def _is_fresh(book: Book) -> bool:
    """A tracked story with chapters that arrived by an upstream update and that the reader has
    reached (cursor at/after the first new chapter). A reader still behind on older chapters keeps
    reading them in order via the normal pass."""
    return (
        book.tracked
        and book.fresh_from_index is not None
        and book.cursor_chapter_index >= book.fresh_from_index
    )


def _weight_fade(book: Book, floor: float) -> float:
    """Below `floor` a source fades in proportion to its weight (0 → never posts); else 1.0."""
    if floor <= 0 or book.quota_weight >= floor:
        return 1.0
    return max(0.0, book.quota_weight) / floor


def _tick_cooldowns(session: Session, channel_id: int) -> None:
    """Decrement the 👎-down cooldown once per broadcast for this channel's active sources.

    Runs at the *start* of a channel's turn (before selection) so a source set to cooldown=2 by a
    👎 is at 1 when selection runs — it sits out exactly one broadcast, then is eligible again.
    """
    (
        session.query(Book)
        .filter(
            Book.channel_id == channel_id,
            Book.status == BookStatus.active,
            Book.paused.is_(False),
            Book.cooldown_remaining > 0,
        )
        .update({Book.cooldown_remaining: Book.cooldown_remaining - 1},
                synchronize_session=False)
    )


def reset_extra_allowance(session: Session) -> None:
    """Start a new batch: forget how many 🪝 extras each channel has used."""
    for key, _ in list_with_prefix(session, EXTRA_USED_PREFIX):
        delete_value(session, key)


def _extras_used(session: Session, channel_id: int) -> int:
    raw = get_value(session, f"{EXTRA_USED_PREFIX}{channel_id}")
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def extra_limit_reached(session: Session, drop: Drop) -> bool:
    """True if a 🪝 click on `drop` must be refused: its channel has already used its
    `config.extra_per_channel_per_cycle` extras since the last release cycle.

    Callers check this **before** `apply_feedback`, because `apply_feedback` records the
    `FeedbackEvent` that makes `(drop, extra)` idempotent — recording it for a refused click would
    permanently block a legitimate retry on the same drop after the next release. A repeat click on
    a drop whose extra was already granted is not a new request, so it is never "refused" (it is
    simply a no-op in `apply_feedback`).
    """
    already = (
        session.query(FeedbackEvent.id)
        .filter(FeedbackEvent.drop_id == drop.id, FeedbackEvent.action == FeedbackAction.extra)
        .first()
    )
    if already is not None:
        return False
    cfg = _get_config(session)
    return _extras_used(session, drop.book.channel_id) >= max(0, cfg.extra_per_channel_per_cycle)


def create_extra_drop(session: Session, book: Book, library_path: Path) -> Drop | None:
    """Inject one out-of-cycle drop for the given book (triggered by 'extra' feedback)."""
    if book.paused or book.status != BookStatus.active:
        return None  # a stale feed link on a paused/dropped/completed source must not resurrect it
    cfg = _get_config(session)
    adapter = CalibreAdapter(library_path)
    units = _remaining_units(book, adapter)
    if not units:
        return None
    plan = PlannedDrop(book=book, chapters=[units[0]], word_count=units[0].word_count)
    drop = _materialise(session, plan, book.channel_id)  # sets feed_key from book.slot_index
    if drop:
        # The reader explicitly asked for this chapter, so it is *not* evidence they're behind:
        # born acknowledged, it also resets the unread streak (see _unread_penalties). Without this
        # the injected drop sat unread and penalised the source next cycle — "asked for more, got
        # skipped".
        drop.acknowledged_at = utcnow()
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
    base_budget: int | None = None,
    skips_out: list["SkippedSource"] | None = None,
    stats_out: dict | None = None,
    penalties: dict[int, float] | None = None,
    weight_skip_floor: float = 1.0,
) -> list[PlannedDrop]:
    """Select whole units (chapters) for one channel — slot-diverse and weight-throttled.

    `budget` is this cycle's effective allowance (base budget + accumulated credit);
    `base_budget` is the per-cycle base (defaults to `budget`). Three passes:

    1. **Accumulation pass** — an *oversized* unit (larger than `base_budget`) can never fit in
       a single cycle, so it waits until `budget` (base + saved-up credit) can afford it, then
       posts whole (at most one per source per cycle). Its long-run rate tracks the base budget.
    1.5 **Fresh pass** — tracked sources with chapters that arrived by an upstream update (see
       `_is_fresh`) release their next whole unit deterministically, one per source per cycle,
       oldest arrival first, while budget lasts (no roll, no unread penalty, no weight fade).
    2. **Slot round-robin** — rotate through the channel's occupied slots; on each slot's turn a
       **weight-proportional** random source pinned to that slot drops its next normal-sized unit
       if a stochastic budget roll passes. A slot with no eligible source passes its turn, so an
       idle slot's budget spills to slots that still have content (no wasted budget). This spreads
       drops across the slot feeds (diversity) while `quota_weight` governs each source's share
       *within* a slot — down-voting a source shrinks its slice relative to its slot-mates. Units
       never split; excluded units roll over whole to a later cycle.

    A source's stochastic acceptance is also scaled by `penalties[book.id]` (the transient unread-
    streak ramp — an *absolute* back-off, so even a lone unread source trickles slower) and by the
    weight fade (`weight / weight_skip_floor` when its weight is below the floor).

    `stats_out`, if given, receives `max_pending_unit` (the largest next-unit size) so the caller
    can size the credit cap to save up for an oversized chapter.
    """
    if base_budget is None:
        base_budget = budget
    penalties = penalties or {}

    # Pre-load remaining units for every source (weight order gives the pass-1 accumulation a
    # stable, higher-weight-first claim on saved-up credit).
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
        if stats_out is not None:
            stats_out["max_pending_unit"] = 0
        return []

    # Largest next-unit in the channel — the caller uses this to cap accumulated credit so an
    # oversized chapter can be saved up for (but idle channels don't runaway). See run_release_cycle.
    if stats_out is not None:
        stats_out["max_pending_unit"] = max(book_remaining[b.id][0].word_count for b in valid)

    selected: dict[int, list[Unit]] = {b.id: [] for b in valid}
    used = 0

    # Pass 1 — accumulation: post an oversized next-unit (bigger than the base per-cycle budget)
    # whole, once the effective budget has saved up enough to afford it. Weight order gives
    # higher-weight sources first claim. At most one oversized unit per source per cycle.
    for book in valid:
        remaining = book_remaining[book.id]
        if not remaining:
            continue
        unit = remaining[0]
        if unit.word_count > base_budget and budget - used >= unit.word_count:
            selected[book.id].append(unit)
            remaining.pop(0)
            used += unit.word_count

    # Pass 1.5 — fresh tracked chapters: a chapter the site just published is the most valuable thing
    # in the batch, so it is released deterministically (no stochastic roll, no unread penalty, no
    # weight fade) rather than left to a dice roll. One unit per fresh source per cycle, round-robin
    # — a 10-chapter dump must not eat the whole batch — oldest arrival first, while budget lasts;
    # a unit that doesn't fit the remaining budget stays fresh for the next cycle. Oversized units
    # are pass 1's (accumulation); a weight-0 source never posts.
    fresh = [
        b for b in sorted(valid, key=lambda b: (b.last_fetch_at is None, b.last_fetch_at, b.id))
        if _is_fresh(b) and not selected[b.id] and b.quota_weight > 0
    ]
    for book in fresh:
        remaining = book_remaining[book.id]
        if not remaining:
            continue
        unit = remaining[0]
        if unit.word_count <= base_budget and used + unit.word_count <= budget:
            selected[book.id].append(unit)
            remaining.pop(0)
            used += unit.word_count

    # Pass 2 — slot round-robin over normal (fits-in-base) units. Each occupied slot takes turns;
    # per turn one weight-proportional source in that slot rolls for its next unit. An idle slot
    # passes, spilling its budget to slots that still have content.
    by_slot: dict[int, list[Book]] = {}
    for book in valid:
        by_slot.setdefault(book.slot_index or 0, []).append(book)
    slots = sorted(by_slot)

    progress = True
    while progress and used < budget:
        progress = False
        for slot in slots:
            candidates = [
                b for b in by_slot[slot]
                if book_remaining[b.id]
                and book_remaining[b.id][0].word_count <= base_budget  # oversized → pass 1
            ]
            if not candidates:
                continue  # idle slot passes its turn; its budget spills to the others
            book = _weighted_choice(candidates)
            unit = book_remaining[book.id][0]
            penalty = penalties.get(book.id, 1.0) * _weight_fade(book, weight_skip_floor)
            p = _inclusion_probability(
                unit.word_count, used=used, budget=budget,
                base_budget=base_budget, penalty=penalty,
            )
            if random.random() < p:
                selected[book.id].append(unit)
                book_remaining[book.id].pop(0)
                used += unit.word_count
                progress = True

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


def _weighted_choice(books: list[Book]) -> Book:
    """Pick one source at random, weighted by `quota_weight` (higher weight → picked more often).

    This is how weight throttles a source's *share within its slot*: a down-voted source has a
    lower weight and so wins fewer of the slot's round-robin turns. A single-candidate slot always
    returns that source — weight is moot when nothing competes (see the round-robin spill).
    """
    weights = [max(b.quota_weight, 1e-6) for b in books]
    return random.choices(books, weights=weights, k=1)[0]


def _inclusion_probability(
    word_count: int, used: int, budget: int,
    base_budget: int | None = None, penalty: float = 1.0,
) -> float:
    """Probability of including a whole *normal-sized* unit at the current budget mark.

    Falls as the cycle runs over budget; 0 for an oversized unit (larger than `base_budget` —
    the accumulation pass owns those). `penalty` (<1 for an unacknowledged source) scales it down
    as an absolute read-gating back-off. `base_budget` defaults to `budget` when unspecified.
    """
    if base_budget is None:
        base_budget = budget
    if word_count <= 0:
        return 1.0
    if word_count > base_budget:
        return 0.0
    remaining = budget - used
    if remaining <= 0:
        return 0.0
    return min(1.0, remaining / word_count) * penalty


def _get_chapters(
    calibre_book: CalibreBook, adapter: CalibreAdapter, book: Book
) -> list[Chapter]:
    epub_path = adapter.epub_path(calibre_book)
    if not epub_path.exists():
        return []
    all_chapters = chapterize(epub_path)
    # Keep total_chapters honest for *every* active source we look at each broadcast — not just
    # the ones we emit (see _advance_cursor). A caught-up source (cursor at the end) is never
    # selected, so if its EPUB later shrinks — e.g. an author unpublishes chapters, common with
    # RoyalRoad — its stale total_chapters would otherwise show phantom "N waiting" on the
    # dashboard forever. The chapterizer is mtime-cached, so this is ~free.
    book.total_chapters = len(all_chapters)
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
            # picks them up automatically). Keep the slot pinned. Caught up ⇒ nothing fresh.
            book.fresh_from_index = None
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
      onto a slot — the slot with the lowest summed quota_weight, tie-broken by the fewest chapters
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
            Book.paused.is_(False),
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
                Book.paused.is_(False),  # a paused (freed-slot) book waits for resume, not promotion
            )
            .order_by(Book.queue_position)
            .limit(free)
            .all()
        )
        for book in queued:
            book.status = BookStatus.active
            book.slot_index = _lowest_free_slot(epub_slots, parallel_slots)
            epub_slots.add(book.slot_index)

    _place_tracked(session, parallel_slots, channel_id)


def _place_tracked(session: Session, parallel_slots: int, channel_id: int) -> None:
    """Tracked books: uncapped, load-balanced across all slots (sticky).

    Only (re)places sources lacking a valid slot; never promotes or demotes backlog books.
    """
    def _valid(idx: int | None) -> bool:
        return idx is not None and 1 <= idx <= parallel_slots

    ongoings = (
        session.query(Book)
        .filter(
            Book.channel_id == channel_id,
            Book.tracked.is_(True),
            Book.status == BookStatus.active,
            Book.paused.is_(False),
        )
        .all()
    )
    # Load per slot = summed quota_weight of the works pinned there (not a raw count), so a slot
    # carrying a heavyweight serial isn't treated as equal to one holding a dormant story.
    # (Pending-chapter counts make a poor proxy: a caught-up tracked story has 0 pending.)
    work_load: dict[int, float] = {s: 0.0 for s in range(1, parallel_slots + 1)}
    for epub in session.query(Book).filter(
        Book.channel_id == channel_id, Book.tracked.is_(False),
        Book.status == BookStatus.active, Book.paused.is_(False),
    ):
        if _valid(epub.slot_index):
            work_load[epub.slot_index] += max(epub.quota_weight, 0.0)
    for ongoing in ongoings:
        if _valid(ongoing.slot_index):
            work_load[ongoing.slot_index] += max(ongoing.quota_weight, 0.0)
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
            key=lambda s: (work_load[s], int(chapter_freq.get(str(s), 0)), s),
        )

    for ongoing in ongoings:
        if not _valid(ongoing.slot_index):
            slot = _balanced_slot()
            ongoing.slot_index = slot
            work_load[slot] += max(ongoing.quota_weight, 0.0)


def apply_feedback(
    session: Session,
    drop: Drop,
    action: FeedbackAction,
    library_path: Path,
) -> Drop | None:
    """Apply a feedback action and record the event.

    Idempotent per (drop, action): a reader/proxy prefetching a bare-GET link, or a
    double-click, applies the effect at most once. Actions: a strength scale — extra
    (super-up) · up · down · drop (super-down) — plus ⏸ pause and ✓ read (utility).
    Any action also acknowledges the drop (soft read-gating).
    """
    cfg = _get_config(session)
    book = drop.book

    # Any feedback interaction acknowledges the drop as read (soft read-gating).
    if drop.acknowledged_at is None:
        drop.acknowledged_at = utcnow()

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
        book.quota_weight = min(WEIGHT_CAP, book.quota_weight + cfg.vote_step)

    elif action == FeedbackAction.down:
        book.thumbs_down += 1
        book.quota_weight = max(0.0, book.quota_weight - cfg.vote_step)
        if book.quota_weight <= 0.0:
            # Auto-drop fires only from here — a source ground down to zero by 👎s. (An admin
            # typing 0 into the weight box does not drop it; it just never posts.)
            book.status = BookStatus.dropped
            book.slot_index = None
            _refill_book_channel(session, book)
        else:
            # Back the source off for one broadcast so a thumbs-down is felt immediately (2 = the
            # tick at the start of the next cycle leaves 1, so it sits that one out).
            book.cooldown_remaining = max(2, book.cooldown_remaining)

    elif action == FeedbackAction.extra:
        # Super-up: count as three upvotes, add the configurable boost, inject a drop.
        book.thumbs_up += 3
        book.quota_weight = min(WEIGHT_CAP, book.quota_weight + cfg.extra_boost_step)
        extra_drop = create_extra_drop(session, book, library_path)
        if extra_drop is not None:  # spend one of this channel's per-batch extras
            key = f"{EXTRA_USED_PREFIX}{book.channel_id}"
            set_value(session, key, str(_extras_used(session, book.channel_id) + 1))

    elif action == FeedbackAction.read:
        # Explicit ✓ Mark-read: acknowledge only (handled above) — no weight change, no event
        # side effects. Lets a reader who reads in-feed signal "caught up" without voting.
        pass

    elif action == FeedbackAction.pause:
        # Stop this source broadcasting until resumed from the dashboard. Reversible, so it
        # fires instantly (bare GET) like up/down — no confirm page.
        pause_book(session, book)

    elif action == FeedbackAction.drop:
        # Super-down: drop the source immediately, regardless of threshold.
        book.status = BookStatus.dropped
        book.slot_index = None
        _refill_book_channel(session, book)

    session.flush()
    return extra_drop


def pause_book(session: Session, book: Book) -> None:
    """Pause a source: it broadcasts nothing until resumed.

    A backlog (untracked) book FREES its slot — it re-enters the queue and the next queued
    book streams into the freed slot. A tracked story just stops (its sticky slot is kept).
    """
    book.paused = True
    if not book.tracked and book.status == BookStatus.active:
        book.status = BookStatus.queued
        book.slot_index = None
        _refill_book_channel(session, book)  # promote the next queued book into the freed slot


def resume_book(session: Session, book: Book) -> None:
    """Resume a paused source so it can broadcast again.

    A backlog book re-competes for a free slot (streaming in if one is open, else waiting in
    the queue); a tracked story becomes eligible again. Rebalance the channel's slots so the
    resumed book is (re)placed.
    """
    book.paused = False
    channel = session.get(Channel, book.channel_id)
    if channel is not None:
        _assign_slots(session, channel.parallel_slots, book.channel_id)


def assign_channel_slots(session: Session, channel_id: int) -> None:
    """Place a channel's *tracked* stories in slots right now (used at add/import time).

    Backlog promotion is left to the release cycle (imports of done/blank-status books must stay
    `queued`).

    Without this a freshly added source has no slot until the next release cycle, so it is
    invisible on the dashboard's slot cards and every bulk add is placed in one lump.
    """
    channel = session.get(Channel, channel_id)
    if channel is not None:
        session.flush()
        _place_tracked(session, channel.parallel_slots, channel_id)


def _refill_book_channel(session: Session, book: Book) -> None:
    """Promote the next queued EPUB into the slot freed within this book's channel."""
    channel = session.get(Channel, book.channel_id)
    if channel is not None:
        _assign_slots(session, channel.parallel_slots, book.channel_id)
