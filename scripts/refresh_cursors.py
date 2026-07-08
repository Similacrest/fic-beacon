#!/usr/bin/env python
"""One-off maintenance: recompute total_chapters and clamp any stale cursor.

Use after EPUBs are updated in the Calibre library *outside* the normal fetcher path
(e.g. a manual bulk transfer). It re-reads each source's EPUB, refreshes
``total_chapters``, and clamps ``cursor_chapter_index`` / ``cursor_floor`` that now
point past the EPUB's end back to the last chapter. Reading positions that are still
in range are left untouched, so genuinely-unread new chapters still drop normally.

This is a read-mostly repair — it does NOT rewrite labels or run stub detection (a
manual transfer has no per-chapter URL diff to work from). It only keeps the DB's
counts/cursors from pointing past real content.

Runs against the app's own SQLite DB and the read-only Calibre mount, so run it inside
the beacon container:

    docker exec fic-beacon-beacon-1 /app/.venv/bin/python /app/scripts/refresh_cursors.py         # dry-run
    docker exec fic-beacon-beacon-1 /app/.venv/bin/python /app/scripts/refresh_cursors.py --apply  # write

Prints a per-source before/after summary; without --apply nothing is committed.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/refresh_cursors.py` from the repo root (app.* on the path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.calibre.adapter import CalibreAdapter  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import db_session  # noqa: E402
from app.epub.chapterizer import chapterize  # noqa: E402
from app.models import Book  # noqa: E402


def refresh(apply: bool) -> int:
    """Recompute totals and clamp stale cursors. Returns the number of rows changed."""
    adapter = CalibreAdapter(settings.calibre_library_path)
    changed = 0
    with db_session() as session:
        books = session.query(Book).filter(Book.calibre_id.isnot(None)).all()
        for book in books:
            cbook = adapter.get_book(book.calibre_id)
            if cbook is None:
                print(f"  ! skip id={book.id} {book.title!r} — calibre_id={book.calibre_id} "
                      "not in metadata.db")
                continue
            epub_path = adapter.epub_path(cbook)
            if not epub_path.exists():
                print(f"  ! skip id={book.id} {book.title!r} — EPUB missing at {epub_path}")
                continue

            total = len(chapterize(epub_path))
            old_total = book.total_chapters
            old_cursor = book.cursor_chapter_index
            old_floor = book.cursor_floor

            new_cursor = min(old_cursor, total)
            new_floor = min(old_floor, total)

            if (total, new_cursor, new_floor) == (old_total, old_cursor, old_floor):
                continue

            note = ""
            if new_cursor != old_cursor or new_floor != old_floor:
                note = "  <-- cursor clamped"
            print(f"  id={book.id:<4} {book.title[:48]:<48} "
                  f"total {old_total}->{total}  cursor {old_cursor}->{new_cursor}"
                  f"  floor {old_floor}->{new_floor}{note}")

            book.total_chapters = total
            book.cursor_chapter_index = new_cursor
            book.cursor_floor = new_floor
            changed += 1

        if apply:
            session.commit()
        else:
            session.rollback()
    return changed


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    print(f"Refreshing cursors ({'APPLY — writing' if apply else 'dry-run'})\n")
    changed = refresh(apply)
    verb = "updated" if apply else "would update"
    print(f"\n{verb} {changed} source(s)." + ("" if apply else "  Re-run with --apply to write."))


if __name__ == "__main__":
    main()
