"""Poll ongoing serial RSS/Atom feeds and buffer new chapters.

Each ongoing source (a Book with kind=ongoing and a feed_url) is polled on a schedule.
New entries are appended to the OngoingEntry buffer with released=False; the drop
planner releases the oldest unreleased entries at drop time, weighted in the channel
budget like EPUB chapters. So ongoing chapters arrive batched at drop time, not whenever
the author posts.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import feedparser
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.models import Book, BookKind, OngoingEntry, utcnow

logger = logging.getLogger(__name__)

# ── Chapter number extraction ─────────────────────────────────────────────────

_CHAPTER_NUM_RE = re.compile(
    r"\b(?:chapter|ch\.?|episode|part)\s+(\d+)\b", re.IGNORECASE
)
_CHAPTER_WORD_RE = re.compile(
    r"\b(?:chapter|ch\.?)\s+"
    r"((?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)\b",
    re.IGNORECASE,
)
_WORD_TO_INT: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _word_ordinal_to_int(word: str) -> int | None:
    parts = re.split(r"[\s-]+", word.lower().strip())
    if len(parts) == 1:
        return _WORD_TO_INT.get(parts[0])
    if len(parts) == 2:
        tens = _WORD_TO_INT.get(parts[0])
        ones = _WORD_TO_INT.get(parts[1])
        if tens is not None and ones is not None:
            return tens + ones
    return None


def _extract_chapter_num(title: str) -> int | None:
    """Extract a chapter number from an entry title, or None if not found."""
    m = _CHAPTER_NUM_RE.search(title)
    if m:
        return int(m.group(1))
    m = _CHAPTER_WORD_RE.search(title)
    if m:
        return _word_ordinal_to_int(m.group(1))
    return None


# ── Feed polling ─────────────────────────────────────────────────────────────


def poll_all_feeds(session: Session) -> int:
    """Poll every ongoing source and buffer new entries. Returns count of new entries."""
    sources = (
        session.query(Book)
        .filter(Book.kind == BookKind.ongoing, Book.feed_url.isnot(None))
        .all()
    )
    total_new = 0
    for source in sources:
        try:
            total_new += poll_source(session, source)
        except Exception:  # never let one bad feed break the cycle
            logger.exception(
                "Failed to poll ongoing source '%s' (%s)", source.title, source.feed_url
            )
    session.flush()
    return total_new


def poll_source(session: Session, source: Book) -> int:
    """Fetch one source's feed and append any new entries to the buffer."""
    parsed = feedparser.parse(source.feed_url)
    existing = {
        guid
        for (guid,) in session.query(OngoingEntry.guid).filter(
            OngoingEntry.source_id == source.id
        )
    }
    new_count = 0
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("link")
        if not guid or guid in existing:
            continue
        existing.add(guid)
        title = entry.get("title", "") or ""
        html = _entry_content(entry)
        session.add(OngoingEntry(
            source_id=source.id,
            guid=guid,
            title=title,
            link=entry.get("link"),
            content_html=html,
            word_count=_count_words(html),
            published_at=_entry_published(entry) or utcnow(),
            released=False,
            chapter_num=_extract_chapter_num(title),
        ))
        new_count += 1
    return new_count


def seed_source_as_read(session: Session, source: Book) -> None:
    """Poll a freshly-added source and immediately mark all entries as already read.

    This implements the "assume I've read everything before adding" contract: the full
    feed is buffered, then every entry is released without a drop_id (no item emitted).
    cursor_chapter_index is set to the last known chapter number (from title extraction
    or sequential counting), so the dashboard can show the right progress baseline and
    the user can rewind from there.
    """
    poll_source(session, source)
    session.flush()
    entries = (
        session.query(OngoingEntry)
        .filter(OngoingEntry.source_id == source.id, OngoingEntry.released.is_(False))
        .order_by(OngoingEntry.published_at, OngoingEntry.id)
        .all()
    )
    last_chapter = 0
    for i, entry in enumerate(entries, start=1):
        entry.released = True
        if entry.chapter_num is not None:
            last_chapter = entry.chapter_num
        else:
            last_chapter = i
    source.cursor_chapter_index = last_chapter


# ── helpers ──────────────────────────────────────────────────────────────────


def _entry_content(entry) -> str:
    """Full HTML of an entry: prefer content[0], fall back to summary/description."""
    content_list = getattr(entry, "content", None)
    if content_list:
        return content_list[0].get("value", "") or ""
    return getattr(entry, "summary", "") or getattr(entry, "description", "") or ""


def _count_words(html: str) -> int:
    text = BeautifulSoup(html or "", "lxml").get_text(" ")
    return len(re.findall(r"\S+", text))


def _entry_published(entry) -> datetime | None:
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if t is None:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
