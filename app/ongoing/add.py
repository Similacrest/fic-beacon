"""Adding a tracked story by URL: validation, duplicate detection, the row itself.

A tracked source is a `Book` with `tracked=True` and a `source_url` FanFicFare can fetch. The
fetcher container downloads its chapters into the Calibre library; from there it is served as a
normal EPUB through the chapterizer/cursor path, like the backlog. An optional `feed_url` (RSS) is
only a fast *notification* that new chapters exist — its body is never read. The admin Add page
(`POST /admin/add`) is the one caller.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import ensure_default_channel
from app.models import Book, BookStatus, Channel, Config
from app.ongoing.feed_url import infer_feed_url, is_fetchable_url
from app.ongoing.story_key import normalize_story_url, story_key


def valid_channel_id(db: Session, channel_id: int | None) -> int:
    """The requested channel if it exists, else the default one (a stale/forged id must not 500)."""
    if channel_id and db.get(Channel, channel_id) is not None:
        return channel_id
    return ensure_default_channel(db).id


def existing_for_url(db: Session, story_url: str) -> Book | None:
    """The source already registered for this story, if any. The same story under another spelling
    (pasted slug/page, FanFicFare's canonical form) counts as the same one."""
    story_url = normalize_story_url(story_url)
    key = story_key(story_url)
    for book in db.query(Book).filter(Book.source_url.isnot(None)).order_by(Book.id):
        if book.source_url == story_url or (key and story_key(book.source_url) == key):
            return book
    return None


def add_tracked_story(db: Session, story_url: str, title: str, channel_id: int) -> Book | None:
    """Create a tracked story row (no fetch yet) unless its URL is invalid or already registered.

    The FanFicFare download is kicked off in the background by the caller
    (`scheduler.trigger_fetch_pending`) so the request returns promptly. Returns the new Book, or
    None if the URL was blank / unfetchable / already tracked.
    """
    story_url = normalize_story_url(story_url)
    if not is_fetchable_url(story_url) or existing_for_url(db, story_url) is not None:
        return None
    max_pos = db.query(func.max(Book.queue_position)).scalar() or 0
    cfg = db.get(Config, 1)
    book = Book(
        tracked=True,
        source_url=story_url,
        feed_url=infer_feed_url(story_url),
        title=(title or story_url).strip(),
        status=BookStatus.active,
        queue_position=max_pos + 1,
        channel_id=channel_id,
        last_fetch_status="pending",
        # A blank title is a placeholder replaced by the real Calibre title once the first fetch
        # lands (scheduler._sync_title). Ongoing serials start above the finite backlog.
        quota_weight=cfg.tracked_default_weight if cfg else 2.0,
    )
    db.add(book)
    db.flush()
    return book
