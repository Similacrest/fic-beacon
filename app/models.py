from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, func,
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calibre_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False, default="Unknown")
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    total_chapters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[BookStatus] = mapped_column(
        Enum(BookStatus), nullable=False, default=BookStatus.queued
    )
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # quota_weight: relative priority; normalized against sum of all active weights
    quota_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    cursor_chapter_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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

    # v2 placeholder
    target_total_words: Mapped[int | None] = mapped_column(Integer, nullable=True)


class OngoingFeed(Base):
    """v2: OPML-imported ongoing feeds for volume balancing."""
    __tablename__ = "ongoing_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    feed_url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Rolling estimate of words per cycle (updated by v2 poller)
    estimated_words_per_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
