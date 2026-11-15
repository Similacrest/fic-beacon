"""One chapter list for a story that spans several EPUBs (stub archives + the live EPUB).

When a site removes chapters the fetcher keeps the pre-stub EPUB as an *archive* Calibre entry
(`book.segments`) and overwrites the live one. The unified list is: the archive chapters in order,
then any live chapter whose URL is new. Because an archive is the pre-stub EPUB, the unified
positions of its chapters equal the old physical indices, so cursors and label maps stay valid.

`build_unified` is pure; `load_unified` does the IO (adapter + chapterizer) with a small cache.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from app.config import settings
from app.epub.chapterizer import Chapter, chapterize
from app.models import Book, absolute_chapter_number
from app.ongoing.story_key import chapter_key

logger = logging.getLogger(__name__)


@dataclass
class UChapter:
    position: int
    title: str
    html: str
    word_count: int
    source_url: str | None
    calibre_id: int          # the EPUB that serves this chapter (images are read from it)
    physical_index: int      # index inside that EPUB
    segment: int             # index into the ordered segments (archives…, live last)
    short_live: bool = False  # a newer copy exists but the length rule kept this (fuller) one

    @property
    def index(self) -> int:  # drop-in for Chapter.index in the planner
        return self.position


@dataclass
class UnifiedResult:
    chapters: list[UChapter] = field(default_factory=list)
    unreadable: list[int] = field(default_factory=list)


def build_unified(segments: list[tuple[int, list[Chapter]]], ratio: float) -> list[UChapter]:
    """Merge `(calibre_id, chapters)` segments (archives first, live last) into one list.

    A chapter whose URL was already seen in an earlier segment keeps its position; the later copy
    replaces it unless its word count is `< ratio ×` or `> (1/ratio) ×` the earlier one (a truncated
    or bloated re-render), in which case the earlier copy stays and is flagged `short_live`.
    Identity is `chapter_key` (the site's chapter id, so a renamed slug still matches). Chapters with no URL have no identity and are always appended. In a later EPUB the first
    occurrence of a URL wins; the first EPUB is kept verbatim.
    """
    out: list[UChapter] = []
    by_url: dict[str, int] = {}
    for seg_no, (cid, chapters) in enumerate(segments):
        seen_here: set[str] = set()
        for c in chapters:
            u = chapter_key(c.source_url)   # identity, not the raw URL: slugs get renamed
            if u and seg_no > 0:
                if u in seen_here:
                    continue
                seen_here.add(u)
            new = UChapter(len(out), c.title, c.html, c.word_count, c.source_url, cid, c.index, seg_no)
            pos = by_url.get(u) if u else None
            if pos is None or seg_no == 0:
                # the first EPUB is taken verbatim (positions == physical indices, so an existing
                # cursor never shifts); a repeated URL there keeps pointing at its first row
                if u:
                    by_url.setdefault(u, new.position)
                out.append(new)
                continue
            old = out[pos]
            w = old.word_count
            if w > 0 and (c.word_count < ratio * w or c.word_count > w / ratio):
                old.short_live = True
                continue
            new.position = pos
            out[pos] = new
    return out


# (calibre_id, mtime) of every segment → result. Small: stories with several segments are rare.
_cache: "OrderedDict[tuple, UnifiedResult]" = OrderedDict()
_CACHE_MAX = 32


def load_unified(book: Book, adapter) -> UnifiedResult:
    """The unified chapter list of `book`. An archive (or the live EPUB) that is missing from Calibre
    or unreadable is reported in `unreadable`; `chapters` then holds only the segments before it,
    because the positions of everything after it are unknowable."""
    ids = [s.calibre_id for s in book.segments]
    if book.calibre_id is not None:
        ids.append(book.calibre_id)
    paths: list[tuple[int, object, float]] = []
    for cid in ids:
        try:
            cb = adapter.get_book(cid)
            if cb is None:
                raise FileNotFoundError(cid)
            p = adapter.epub_path(cb)
            paths.append((cid, p, p.stat().st_mtime))
        except Exception as exc:  # missing entry / file
            logger.warning("book %s: EPUB of calibre id %s unavailable: %s", book.id, cid, exc)
            return _partial(paths, cid)
    key = tuple((cid, mt) for cid, _, mt in paths)
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit
    parsed: list[tuple[int, list[Chapter]]] = []
    for cid, p, _ in paths:
        try:
            parsed.append((cid, chapterize(p)))
        except Exception as exc:  # corrupt zip etc.
            logger.warning("book %s: EPUB of calibre id %s unreadable: %s", book.id, cid, exc)
            return UnifiedResult(
                build_unified(parsed, settings.segment_length_ratio), [cid])
    res = UnifiedResult(build_unified(parsed, settings.segment_length_ratio), [])
    _cache[key] = res
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return res


def _partial(done: list[tuple[int, object, float]], bad: int) -> UnifiedResult:
    parsed: list[tuple[int, list[Chapter]]] = []
    for cid, p, _ in done:
        try:
            parsed.append((cid, chapterize(p)))
        except Exception:
            break
    return UnifiedResult(build_unified(parsed, settings.segment_length_ratio), [bad])


def chapter_label(book: Book, position: int) -> int:
    """The author's chapter number for a unified position."""
    return absolute_chapter_number(book, position)
