"""View-model for a stubbed story's progress bar (pure; no IO).

A story that stubbed is one chapter list served from several EPUBs. The bar shows which EPUB each
run of chapters comes from, with the *author's* chapter numbers at the boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.epub.unified import UChapter, chapter_label
from app.models import Book


@dataclass
class BarSpan:
    kind: str            # "archive" | "archive_short_live" | "live"
    start_label: int     # author's chapter numbers
    end_label: int
    start_pos: int       # unified positions
    end_pos: int
    width_pct: float
    calibre_id: int


def _kind(book: Book, c: UChapter) -> str:
    if c.short_live:
        return "archive_short_live"
    return "live" if c.segment >= len(book.segments) else "archive"


def segment_bar(book: Book, unified: list[UChapter]) -> list[BarSpan]:
    """Runs of consecutive chapters served the same way, merged into spans."""
    spans: list[BarSpan] = []
    total = len(unified)
    for c in unified:
        kind = _kind(book, c)
        last = spans[-1] if spans else None
        if last and last.kind == kind and last.calibre_id == c.calibre_id:
            last.end_pos = c.position
        else:
            spans.append(BarSpan(kind, 0, 0, c.position, c.position, 0.0, c.calibre_id))
    for s in spans:
        s.start_label = chapter_label(book, s.start_pos)
        s.end_label = chapter_label(book, s.end_pos)
        s.width_pct = (s.end_pos - s.start_pos + 1) / total * 100 if total else 0.0
    return spans


def effective_cursor_floor(book: Book) -> int:
    """With segments the old chapters still exist (in the archives), so there is nothing to floor."""
    return 0 if book.segments else book.cursor_floor
