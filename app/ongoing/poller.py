"""Detect ongoing-serial updates and trigger fetches.

RSS feeds are used **only as a notification** that new chapters exist — never for their
content. For each tracked book with a feed_url we read the newest entry's GUID; if it
changed since we last looked, we ask the fetcher container to download the new chapters
into Calibre (`app.fetch.client.fetch_book`). The chapters are then served from the EPUB
through the normal chapterizer/cursor path, exactly like the backlog.

Polling runs **pre-drop** (right before each broadcast), so the freshest chapters land in
the EPUBs before the planner reads them. Tracked books *without* a feed (auth-gated stories
fetchable only via FanFicFare's personal.ini) have no RSS signal and are handled by the
daily sweep instead.
"""
from __future__ import annotations

import logging

import feedparser
from sqlalchemy.orm import Session

from app.fetch.client import fetch_book
from app.models import Book

logger = logging.getLogger(__name__)


def _newest_guid(parsed) -> str | None:
    """The GUID of a feed's newest entry (feeds are conventionally newest-first)."""
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("link")
        if guid:
            return guid
    return None


def poll_all_feeds(session: Session) -> int:
    """Check every feed-backed tracked book; fetch the ones whose feed shows something new.

    Returns the number of books fetched this pass. First sight of a feed only *seeds* the
    last-seen GUID (the book was already downloaded when it was added), so we don't refetch
    on the very first poll.
    """
    sources = (
        session.query(Book)
        .filter(Book.tracked.is_(True), Book.feed_url.isnot(None))
        .all()
    )
    fetched = 0
    for source in sources:
        try:
            newest = _newest_guid(feedparser.parse(source.feed_url))
            if newest is None or newest == source.last_seen_guid:
                continue
            first_sight = source.last_seen_guid is None
            source.last_seen_guid = newest
            if not first_sight:  # genuine new chapter → pull it into Calibre
                fetch_book(session, source)
                fetched += 1
        except Exception:  # never let one bad feed break the cycle
            logger.exception(
                "Failed to poll tracked source '%s' (%s)", source.title, source.feed_url
            )
    from app.state import LAST_POLL_RUN, mark_run
    mark_run(session, LAST_POLL_RUN)
    session.flush()
    return fetched


def fetch_pending(session: Session) -> int:
    """Initial-download every tracked book that has no Calibre EPUB yet. Returns count.

    Run in the background after stories are added by URL (which only creates the rows).
    A failed fetch leaves calibre_id NULL, so it is retried on the next call.
    """
    sources = (
        session.query(Book)
        .filter(Book.tracked.is_(True), Book.calibre_id.is_(None))
        .all()
    )
    fetched = 0
    for source in sources:
        try:
            fetch_book(session, source)
            fetched += 1
        except Exception:
            logger.exception("Initial fetch failed for '%s' (%s)", source.title, source.source_url)
    session.flush()
    return fetched


def sweep_feedless(session: Session) -> int:
    """Fetch every tracked book that has no feed (auth-gated). Runs daily. Returns count."""
    sources = (
        session.query(Book)
        .filter(Book.tracked.is_(True), Book.feed_url.is_(None))
        .all()
    )
    fetched = 0
    for source in sources:
        try:
            fetch_book(session, source)
            fetched += 1
        except Exception:
            logger.exception("Failed to sweep-fetch '%s' (%s)", source.title, source.source_url)
    session.flush()
    return fetched
