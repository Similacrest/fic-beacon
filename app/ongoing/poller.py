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

Polling runs **pre-drop** (the release cycle submits fetches first); freshly fetched chapters
land in the *next* broadcast. Tracked books *without* a feed (auth-gated stories fetchable
only via FanFicFare's personal.ini) have no RSS signal and are handled by the daily sweep.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
from sqlalchemy import or_
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
    calibre_id (never fetched) or an unknown status are kept. A manual "Fetch now" bypasses this. #status is read live from
    metadata.db so the user's Calibre status edits take effect without a restart.
    """
    # The fetcher-recorded status (Book.story_status) covers stories added by URL, whose Calibre
    # #status stays blank forever; either source saying "done" is enough to skip.
    live = [s for s in sources if not is_done(s.story_status)]
    have_id = [s for s in live if s.calibre_id is not None]
    if not have_id:
        return live
    try:
        statuses = CalibreAdapter(settings.calibre_library_path).status_map(
            [s.calibre_id for s in have_id]
        )
    except Exception:  # never let a metadata read break the cycle
        logger.exception("Failed to read #status for fetch-skip; fetching all")
        return live
    return [s for s in live if not is_done(statuses.get(s.calibre_id))]


def _fetch_in_flight(book: Book) -> bool:
    """A fetch for this book was submitted recently and hasn't reported back yet.

    Bounded by the job timeout so a status left at `fetching…` by a lost job can't block the book
    for good (the scheduler's stale-job sweep clears those too)."""
    if not (book.last_fetch_status or "").startswith("fetching"):
        return False
    at = book.last_fetch_at
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < timedelta(seconds=settings.fetcher_job_timeout * 2)


_FEED_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_FEED_MAX_BYTES = 5 * 1024 * 1024


def _fetch_feed(url: str):
    """Fetch + parse a trigger feed with a hard timeout and size cap. None = "not modified".

    `feedparser.parse(url)` fetches via urllib with *no* timeout: one hung feed host would block
    the whole poll — which runs inside the release cycle holding the cycle lock.
    """
    with httpx.Client(timeout=_FEED_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": "Fic-Beacon/feed-trigger"}) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code == 304:  # some hosts answer 304 unprompted; it means "no change"
                return None
            resp.raise_for_status()
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > _FEED_MAX_BYTES:
                    break
    return feedparser.parse(body)


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
    newest_guid: dict[int, str] = {}
    for source in sources:
        if _fetch_in_flight(source):
            continue  # a fetch is already in flight — its result decides what this entry meant
        try:
            parsed = _fetch_feed(source.feed_url)
            newest = _newest_guid(parsed) if parsed is not None else None
            if newest is None or newest == source.last_seen_guid:
                continue
            if source.last_seen_guid is None:  # first sight: seed only, it was downloaded at add time
                source.last_seen_guid = newest
                continue
            # A genuine new entry → queue a fetch. It is NOT marked seen yet: that happens when the
            # fetch actually lands chapters (scheduler._settle_trigger), so a failed or fruitless
            # fetch is retried next cycle rather than waiting for yet another chapter to be posted.
            changed.append(source)
            newest_guid[source.id] = newest
        except Exception:  # never let one bad feed break the cycle
            logger.exception(
                "Failed to poll tracked source '%s' (%s)", source.title, source.feed_url
            )
    kept = _drop_done(changed)
    for source in changed:
        if source not in kept:  # finished story: nothing to fetch, so the entry is simply seen
            source.last_seen_guid = newest_guid[source.id]
    changed = kept
    if changed:
        submit_and_track(session, changed, {b.id: newest_guid[b.id] for b in changed})
    from app.state import LAST_POLL_RUN, mark_run
    mark_run(session, LAST_POLL_RUN)
    session.flush()
    return len(changed)


def fetch_pending(session: Session) -> int:
    """Submit an initial download for every tracked book that has no Calibre EPUB yet.

    Runs both right after stories are added by URL (which only creates the rows) *and* as a
    backstop at the start of each release cycle, so an initial fetch that was lost — the add-time
    trigger is one-shot and a restart/race can drop it, and `poll_all_feeds` only *seeds* a
    feed's first-sight GUID without downloading — self-heals instead of stranding the book at
    "pending" forever. A failed fetch leaves calibre_id NULL, so it is retried next call.
    In-flight fetches (status "fetching…") are skipped so we never double-submit. Returns count.
    """
    from app.scheduler import submit_and_track

    sources = (
        session.query(Book)
        .filter(
            Book.tracked.is_(True),
            Book.calibre_id.is_(None),
            # Not already downloading — submit_and_track sets "fetching…" on submit; skipping
            # those avoids re-submitting a book whose (slow, ~15 min) fetch is still running.
            # NULL status (never touched) is still eligible.
            or_(
                Book.last_fetch_status.is_(None),
                ~Book.last_fetch_status.like("fetching%"),
            ),
        )
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
