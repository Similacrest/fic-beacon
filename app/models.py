import json
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BookStatus(str, PyEnum):
    queued = "queued"
    active = "active"
    completed = "completed"
    dropped = "dropped"


class BudgetMode(str, PyEnum):
    words = "words"
    minutes = "minutes"


class FeedbackAction(str, PyEnum):
    up = "up"
    down = "down"
    extra = "extra"      # super-up: strong boost + inject an out-of-cycle drop
    drop = "drop"        # super-down: drop the source immediately
    pause = "pause"      # stop this source from broadcasting until resumed from the dashboard
    read = "read"        # acknowledge the drop as read (soft read-gating); no weight change


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def label_breakpoints(book: "Book") -> list[tuple[int, int]]:
    """Parse `book.label_map` into sorted `(physical_index, cumulative_offset)` breakpoints.

    Empty (no map) → the book uses the legacy scalar `chapter_label_offset` instead.
    """
    raw = book.label_map
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [(int(i), int(o)) for i, o in data]
    except (TypeError, ValueError):
        return []


def label_offset_at(book: "Book", physical_index: int) -> int:
    """The chapter-label offset in effect at `physical_index`.

    Piecewise from `label_map` when present (the offset of the largest breakpoint whose index
    is ≤ `physical_index`); otherwise the flat legacy scalar `chapter_label_offset`.
    """
    bps = label_breakpoints(book)
    if not bps:
        return book.chapter_label_offset
    offset = 0
    for idx, off in bps:  # ascending
        if idx <= physical_index:
            offset = off
        else:
            break
    return offset


def absolute_chapter_number(book: "Book", physical_index: int) -> int:
    """Map a 0-based physical EPUB chapter index to its absolute, human chapter number.

    Normally `physical_index + 1`. After a stub (the site removed chapters and the EPUB was
    overwritten shorter), a per-chapter-URL identity diff records the removed positions so labels
    stay exact past a gap **anywhere** in the work — including a middle gap where labels jump
    (e.g. physical index 4 reads as chapter 73) and across repeated stubs. This piecewise offset
    lives in `label_map`; the legacy scalar `chapter_label_offset` is the fallback for older rows
    and count-only fallback stubs. See `label_offset_at` / Book.label_map / cursor_floor.
    """
    return physical_index + label_offset_at(book, physical_index) + 1


class Channel(Base):
    """A TV-style channel grouping sources by a Calibre genre prefix.

    Each channel has its own reading budget and parallel slots; the drop cadence is
    global (Config.cadence_cron). One feed per slot is served from the channel.
    """
    __tablename__ = "channel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Comma-separated Calibre #genre_manual prefixes (e.g. "Fantasy,Sci-Fi" matches either
    # hierarchy) used to auto-route books into this channel on import.
    genre_match: Mapped[str | None] = mapped_column(String, nullable=True)
    parallel_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # Single budget value; budget_mode selects the unit (words or reading-time minutes).
    # Float so toggling words↔minutes (× / ÷ wpm) in the UI is reversible without rounding loss.
    budget: Mapped[float] = mapped_column(Float, nullable=False, default=5000.0)
    budget_mode: Mapped[BudgetMode] = mapped_column(
        Enum(BudgetMode), nullable=False, default=BudgetMode.words
    )
    # Signed carry-over so the stochastic per-cycle mean tracks the budget.
    budget_credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    queue_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Max items served in each of this channel's slot feeds (newest-first). Older drops stay in
    # the DB (permalinks keep working) but fall off the tail of the feed. Per-channel so a
    # high-volume channel can keep a longer tail. Seeded from settings.feed_item_limit.
    feed_item_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50)


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The Calibre book backing this source. Every source is a library EPUB now —
    # backlog books are imported; tracked stories are downloaded into Calibre by the
    # fetcher container. Nullable only for safety during transitional states.
    calibre_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False, default="Unknown")
    # The work's canonical URL. For tracked books this is ALSO the FanFicFare fetch URL.
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    total_chapters: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Update tracking (replaces the old kind=ongoing split) ──────────────────
    # tracked=True → the fetcher auto-updates this book's EPUB (RSS-triggered if it has
    # a feed_url, else via the daily sweep). tracked books never "complete".
    tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Optional RSS/Atom feed used only as a fast *notification* that new chapters exist
    # (its body is never read for content). Absent → the book relies on the daily sweep.
    feed_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Newest feed GUID seen, so the poller can detect "something new → trigger a fetch".
    last_seen_guid: Mapped[str | None] = mapped_column(String, nullable=True)
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Short human status of the last fetch ("ok", "ok (stub 141→101)", or an error).
    last_fetch_status: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[BookStatus] = mapped_column(
        Enum(BookStatus), nullable=False, default=BookStatus.queued
    )
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Channel membership — every source lives in exactly one channel (no default group).
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channel.id"), nullable=False, index=True
    )
    # Stable slot number within the channel (1..parallel_slots), set when active.
    slot_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Paused sources broadcast nothing: they're excluded from candidate selection and slot
    # assignment. Pausing a backlog book FREES its slot (it re-enters the queue, so the next
    # queued book streams in); resume competes for a slot again. Tracked stories just stop.
    # Toggled by the ⏸ feed action or the dashboard; resume is dashboard-only (a paused source
    # emits no feed items to carry a resume link).
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Broadcasts this source must sit out before it's a candidate again. A 👎 down sets it to
    # ≥2 so a thumbs-down meaningfully backs the source off for a couple of cycles (not just a
    # weight nudge). The planner excludes candidates with cooldown_remaining > 0 and ticks it
    # down once per broadcast. 0 = eligible.
    cooldown_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # quota_weight: relative priority; normalized against sum of all active weights
    quota_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # 0-based PHYSICAL index of the next chapter to drop within the current EPUB.
    cursor_chapter_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Legacy scalar: added to the physical index to derive the absolute chapter label after a stub
    # (see absolute_chapter_number). 0 for normal books. Used only when label_map is empty — the
    # count-only fallback (non-FanFicFare EPUBs) and rows predating the piecewise map.
    chapter_label_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Piecewise stub label map: JSON list of [physical_index, cumulative_offset] breakpoints built
    # by matching pre/post-stub chapters on their canonical URL, so labels stay exact past a gap
    # anywhere in the work (front/middle/tail) and compose across repeated stubs. NULL for books
    # with no (URL-diffed) stub. See label_offset_at / absolute_chapter_number.
    label_map: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lowest physical index the cursor may be rewound to. Remapped on a stub (to the first
    # surviving chapter at/after the old floor) so it stays anchored to real content. 0 for
    # normal books; the count-only fallback sets it to the new length instead.
    cursor_floor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbs_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbs_down: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    drops: Mapped[list["Drop"]] = relationship("Drop", back_populates="book")


class Drop(Base):
    __tablename__ = "drops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Snapshot of the channel + per-slot feed this drop belongs to, so feed filtering stays
    # stable even after the source completes and the slot is reused. feed_key is "1".."N".
    channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    feed_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    chapter_start: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_end: Mapped[int] = mapped_column(Integer, nullable=False)  # inclusive
    # Titles of chapters included (semicolon-delimited if multiple)
    chapter_titles: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Canonical per-chapter source URL for the FIRST chapter in this drop
    # (FanFicFare chapterurl). Used as the item link; None for non-FFF books.
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Full HTML content of all chapters in this drop
    content_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # When the reader acknowledged this drop as read — set on opening /read/{slug}, clicking any
    # /fb/ feedback link, or the explicit ✓ Mark-read action. Soft read-gating: a source whose
    # most-recent drop is still unacknowledged gets a reduced inclusion probability, so the reader
    # can't fall as far behind. Best-effort (many permalinks point at the source site, not /read/).
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Unguessable token for feedback links (bound to this drop)
    feedback_token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Stable slug used for the reader permalink
    reader_slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    book: Mapped["Book"] = relationship("Book", back_populates="drops")
    feedback_events: Mapped[list["FeedbackEvent"]] = relationship(
        "FeedbackEvent", back_populates="drop"
    )


class FeedbackEvent(Base):
    __tablename__ = "feedback_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String, nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id"), nullable=False)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"), nullable=False)
    action: Mapped[FeedbackAction] = mapped_column(Enum(FeedbackAction), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    drop: Mapped["Drop"] = relationship("Drop", back_populates="feedback_events")


class Config(Base):
    """Single-row settings table (id is always 1) — true globals only.

    Budget, parallel slots, and budget-mode are per-channel (see Channel); this row
    holds only settings that are inherently global: reading speed, the drop cadence,
    the vote/fade tuning, and the feed secret.
    """
    __tablename__ = "config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Global reading speed: converts a channel's minutes-mode budget to words; also drives
    # reading-time estimates in the UI.
    wpm: Mapped[int] = mapped_column(Integer, nullable=False, default=250)
    cadence_cron: Mapped[str] = mapped_column(String, nullable=False, default="0 7,19 * * *")
    feed_secret: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Additive vote steps on quota_weight (natural scale ~0–3, hard cap 100.0). 👍 adds and 👎
    # subtracts `vote_step`; 🪝 extra adds `extra_boost_step`. A 👎 that takes the weight to 0 drops
    # the source (only the 👎 path auto-drops — typing 0 in the admin UI does not).
    vote_step: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    extra_boost_step: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # Below this weight a source *fades*: its stochastic acceptance is scaled by weight/floor, so it
    # trickles ever more slowly instead of being skipped outright (weight 0 never posts).
    weight_skip_floor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # Floor of the transient unread-drop ramp (see planner._unread_penalty): a reader who is
    # several drops behind slows a source down, but never below this fraction and never via weight.
    unacked_penalty_floor: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    # Default quota_weight given to a *tracked* (ongoing) source on import, vs 1.0 for backlog, so
    # real ongoing serials outrank the finite archive in the stochastic budget pass. Admin-tunable.
    tracked_default_weight: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    # Max byte size of a WebSub push body. A slot feed carries up to feed_item_limit full-content
    # chapters (~1.6 MB for 50 items), and Inoreader silently drops oversized fat pings — it returns
    # 200 but never ingests, so realtime dies and the feed falls back to slow polling. The push is
    # trimmed to the newest drops that fit this budget (always ≥1 item, never splitting a chapter);
    # the polled feed at /feed/… is unaffected. Admin-tunable. 0 disables trimming (push full feed).
    websub_max_push_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=100_000)


class WebSubSubscription(Base):
    """A WebSub (PubSubHubbub) subscriber to one of our feeds (realtime push)."""
    __tablename__ = "websub_subscription"
    __table_args__ = (UniqueConstraint("topic_url", "callback_url", name="uq_topic_callback"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_url: Mapped[str] = mapped_column(String, nullable=False, index=True)  # one of our feed URLs
    callback_url: Mapped[str] = mapped_column(String, nullable=False)
    secret: Mapped[str | None] = mapped_column(String, nullable=True)  # for X-Hub-Signature
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class AppState(Base):
    """Tiny key/value store for runtime state (e.g. last cron run times).

    Deliberately a *new table* rather than extra Config columns. (Schema changes now go
    through Alembic migrations — see app/database.py:run_migrations — so column additions
    are fine too; this just keeps volatile runtime state out of the settings row.)
    """
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
