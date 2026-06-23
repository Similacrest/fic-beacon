from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
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


class BookKind(str, PyEnum):
    epub = "epub"        # Calibre-backed backlog book
    ongoing = "ongoing"  # RSS-backed ongoing serial (feed_url)


class FeedbackAction(str, PyEnum):
    up = "up"
    down = "down"
    extra = "extra"      # super-up: strong boost + inject an out-of-cycle drop
    drop = "drop"        # super-down: drop the source immediately


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Channel(Base):
    """A TV-style channel grouping sources by a Calibre tag prefix.

    Each channel has its own reading budget and parallel slots; the drop cadence is
    global (Config.cadence_cron). One feed per slot is served from the channel.
    """
    __tablename__ = "channel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Calibre tag (or tag prefix, e.g. "Fantasy") used to suggest membership on import.
    tag_match: Mapped[str | None] = mapped_column(String, nullable=True)
    parallel_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    budget_words: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)
    budget_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    budget_mode: Mapped[BudgetMode] = mapped_column(
        Enum(BudgetMode), nullable=False, default=BudgetMode.words
    )
    # Signed carry-over so the stochastic per-cycle mean tracks the budget (Phase D).
    budget_credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    queue_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL for ongoing (RSS) sources, which aren't backed by a Calibre book.
    calibre_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    kind: Mapped[BookKind] = mapped_column(
        Enum(BookKind), nullable=False, default=BookKind.epub
    )
    # Ongoing serial RSS feed (kind=ongoing only).
    feed_url: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False, default="Unknown")
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    total_chapters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[BookStatus] = mapped_column(
        Enum(BookStatus), nullable=False, default=BookStatus.queued
    )
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Channel membership; NULL = the implicit default group (uses Config budget/slots).
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel.id"), nullable=True, index=True
    )
    # Stable slot number within the channel (1..parallel_slots), set when active.
    slot_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # quota_weight: relative priority; normalized against sum of all active weights
    quota_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    cursor_chapter_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbs_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbs_down: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    drops: Mapped[list["Drop"]] = relationship("Drop", back_populates="book")
    ongoing_entries: Mapped[list["OngoingEntry"]] = relationship(
        "OngoingEntry", back_populates="source", cascade="all, delete-orphan"
    )


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
    # Snapshot of the channel + per-slot feed this drop belongs to, so feed filtering
    # stays stable even after the source completes and the slot is reused.
    # feed_key is "1".."N" for backlog slots, or "ongoing" for a channel's serial feed.
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
    """Single-row settings table (id is always 1)."""
    __tablename__ = "config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    global_budget_words: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)
    # Used only when budget_mode=minutes; effective_budget = global_budget_minutes * wpm
    global_budget_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    budget_mode: Mapped[BudgetMode] = mapped_column(
        Enum(BudgetMode), nullable=False, default=BudgetMode.words
    )
    wpm: Mapped[int] = mapped_column(Integer, nullable=False, default=250)
    overshoot_tolerance: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    parallel_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    cadence_cron: Mapped[str] = mapped_column(String, nullable=False, default="0 8 * * *")
    thumbs_down_drop_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    feed_secret: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Signed carry-over for the default group's stochastic budget (mirrors Channel.budget_credit).
    budget_credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


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


class OngoingEntry(Base):
    """A buffered chapter from an ongoing serial's RSS feed.

    The poller appends new entries (released=False) hourly; the drop planner releases
    the oldest unreleased entries at drop time, weighted in the channel budget like
    EPUB chapters. Each released entry becomes (part of) a Drop.
    """
    __tablename__ = "ongoing_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("book.id"), nullable=False, index=True)
    guid: Mapped[str] = mapped_column(String, nullable=False, index=True)  # unique per source
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    link: Mapped[str | None] = mapped_column(String, nullable=True)
    content_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    released: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    drop_id: Mapped[int | None] = mapped_column(ForeignKey("drops.id"), nullable=True)

    source: Mapped["Book"] = relationship("Book", back_populates="ongoing_entries")
