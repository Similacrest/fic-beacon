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
            logger.exception("Failed to poll ongoing source '%s' (%s)", source.title, source.feed_url)
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
        html = _entry_content(entry)
        session.add(OngoingEntry(
            source_id=source.id,
            guid=guid,
            title=entry.get("title", "") or "",
            link=entry.get("link"),
            content_html=html,
            word_count=_count_words(html),
            published_at=_entry_published(entry) or utcnow(),
            released=False,
        ))
        new_count += 1
    return new_count


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
