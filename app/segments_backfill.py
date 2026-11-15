"""One-time migration of stories that stubbed before segments existed.

Their pre-stub EPUBs are already in Calibre as `url-archive:` entries. For each book with no
segments yet whose story has archives, record them as segments, then recompute the cursor against
the unified list: the pre-stub cursor is not recoverable, so it becomes the position just after the
last chapter actually released (matching the drops' chapter URLs into the unified list). Label
state described the old live EPUB's indices, which no longer mean anything, so it is reset; the
old values go in the report. Idempotent (books with segments are skipped).
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.epub.unified import load_unified
from app.models import Book, BookSegment, Drop
from app.state import get_value, set_value

logger = logging.getLogger(__name__)

REPORT_KEY = "segment_backfill_report"


def backfill_segments(session: Session, adapter) -> list[dict]:
    """Returns one report row per migrated book (also appended to `app_state[REPORT_KEY]`)."""
    taken = {r[0] for r in session.query(BookSegment.calibre_id)}
    rows: list[dict] = []
    books = (session.query(Book).filter(Book.calibre_id.isnot(None), Book.source_url.isnot(None))
             .order_by(Book.id).all())
    for book in books:
        if book.segments:
            continue
        ids = [i for i in adapter.archive_ids(book.source_url) if i not in taken and i != book.calibre_id]
        if not ids:
            continue
        for n, cid in enumerate(ids):
            book.segments.append(BookSegment(ordinal=n, calibre_id=cid))
        res = load_unified(book, adapter)
        if res.unreadable:
            logger.warning("book %s: not migrating to segments, EPUB %s is unreadable", book.id, res.unreadable)
            for seg in list(book.segments):
                book.segments.remove(seg)
            session.flush()
            continue
        taken.update(ids)
        position = {}
        for c in res.chapters:
            if c.source_url:
                position.setdefault(c.source_url, c.position)
        released = [position[d.source_url] + (d.chapter_end - d.chapter_start)
                    for d in session.query(Drop).filter(Drop.book_id == book.id)
                    if d.source_url in position]
        total = len(res.chapters)
        row = {"book_id": book.id, "title": book.title, "archive_ids": ids,
               "old_cursor": book.cursor_chapter_index,
               "old_label_offset": book.chapter_label_offset, "old_label_map": book.label_map}
        book.cursor_chapter_index = max(released) + 1 if released else min(book.cursor_chapter_index, total)
        book.cursor_chapter_index = min(book.cursor_chapter_index, total)
        book.cursor_floor = 0
        book.label_map = None
        book.chapter_label_offset = 0
        book.total_chapters = total
        row["new_cursor"] = book.cursor_chapter_index
        rows.append(row)
        logger.warning("segments backfill: %s", json.dumps(row))
    if rows:
        try:
            prior = json.loads(get_value(session, REPORT_KEY) or "[]")
        except json.JSONDecodeError:
            prior = []
        set_value(session, REPORT_KEY, json.dumps(prior + rows))
    return rows


def backfill_on_startup(session: Session) -> list[dict]:
    """`backfill_segments` against the configured library; a missing/unreadable library or any
    failure is logged and never stops startup."""
    try:
        from app.calibre.adapter import CalibreAdapter
        from app.config import settings
        with session.begin_nested():  # a failure part-way rolls back this step only
            return backfill_segments(session, CalibreAdapter(settings.calibre_library_path))
    except Exception:
        logger.warning("segments backfill skipped", exc_info=True)
        return []
