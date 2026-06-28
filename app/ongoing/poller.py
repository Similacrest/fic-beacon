"""Detect ongoing-serial updates and submit fetches.

RSS feeds are used **only as a notification** that new chapters exist — never for their
content. For each tracked book with a feed_url we read the newest entry's GUID; if it
changed since we last looked, we submit an async fetch to the fetcher container, which
downloads the new chapters into Calibre. The chapters are then served from the EPUB through
the normal chapterizer/cursor path, exactly like the backlog.

Fetches are **batched and asynchronous**: each function collects the books that need
fetching and hands them to `scheduler.submit_and_track`, which submits a single batch job to
the fetcher (one warm FanFicFare process for the new ones) and polls for completion in the
background — broadcasts never block on a slow (~15 min) download.

Polling runs **pre-drop** (the drop cycle submits fetches first); freshly fetched chapters
land in the *next* broadcast. Tracked books *without* a feed (auth-gated stories fetchable
only via FanFicFare's personal.ini) have no RSS signal and are handled by the daily sweep.
"""
from __future__ import annotations

import logging

import feedparser
from sqlalchemy.orm import Session

from app.calibre.adapter import CalibreAdapter
from app.calibre.status import is_done
from app.config import settings
from app.models import Book

logger = logging.getLogger(__name__)


def _drop_done(sources: list[Book]) -> list[Book]:
    """Filter out tracked stories the source site marks done (#status Completed/Abandoned/…).

    Their EPUBs are already complete in Calibre, so re-fetching them just burns fetcher time;
    already-downloaded chapters still drop through the normal cursor path. Books without a
    calibre_id (never fetched) or an unknown status are kept. #status is read live from
    metadata.db so the user's Calibre status edits take effect without a restart.
    """
    have_id = [s for s in sources if s.calibre_id is not None]
    if not have_id:
        return sources
    try:
        statuses = CalibreAdapter(settings.calibre_library_path).status_map(
            [s.calibre_id for s in have_id]
        )
    except Exception:  # never let a metadata read break the cycle
        logger.exception("Failed to read #status for fetch-skip; fetching all")
        return sources
    return [s for s in sources if not is_done(statuses.get(s.calibre_id))]


def _newest_guid(parsed) -> str | None:
    """The GUID of a feed's newest entry (feeds are conventionally newest-first)."""
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("link")
        if guid:
            return guid
    return None


def poll_all_feeds(session: Session) -> int:
    """Check every feed-backed tracked book; submit a batch fetch for those showing something new.

    Returns the number of books queued for fetch. First sight of a feed only *seeds* the
    last-seen GUID (the book was already downloaded when it was added), so we don't refetch
    on the very first poll.
    """
    from app.scheduler import submit_and_track

    sources = (
        session.query(Book)
        .filter(Book.tracked.is_(True), Book.feed_url.isnot(None))
        .all()
    )
    changed: list[Book] = []
    for source in sources:
        try:
            newest = _newest_guid(feedparser.parse(source.feed_url))
            if newest is None or newest == source.last_seen_guid:
                continue
            first_sight = source.last_seen_guid is None
            source.last_seen_guid = newest
            if not first_sight:  # genuine new chapter → queue a fetch
                changed.append(source)
        except Exception:  # never let one bad feed break the cycle
            logger.exception(
                "Failed to poll tracked source '%s' (%s)", source.title, source.feed_url
            )
    changed = _drop_done(changed)
    if changed:
        submit_and_track(session, changed)
    from app.state import LAST_POLL_RUN, mark_run
    mark_run(session, LAST_POLL_RUN)
    session.flush()
    return len(changed)


def fetch_pending(session: Session) -> int:
    """Submit an initial download for every tracked book that has no Calibre EPUB yet.

    Run in the background after stories are added by URL (which only creates the rows).
    A failed fetch leaves calibre_id NULL, so it is retried on the next call. Returns count.
    """
    from app.scheduler import submit_and_track

    sources = (
        session.query(Book)
        .filter(Book.tracked.is_(True), Book.calibre_id.is_(None))
        .all()
    )
    if sources:
        submit_and_track(session, sources)
    session.flush()
    return len(sources)


def sweep_feedless(session: Session) -> int:
    """Submit a fetch for every tracked book that has no feed (auth-gated). Runs daily."""
    from app.scheduler import submit_and_track

    sources = _drop_done(
        session.query(Book)
        .filter(Book.tracked.is_(True), Book.feed_url.is_(None))
        .all()
    )
    if sources:
        submit_and_track(session, sources)
    session.flush()
    return len(sources)
