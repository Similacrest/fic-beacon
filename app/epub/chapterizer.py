"""Split an EPUB into ordered chapters, using the TOC for titles.

Strategy:
 - Use ebooklib to parse the EPUB.
 - Build an ordered chapter list from the spine, enriched with titles from
   the TOC (EPUB3 nav.xhtml or EPUB2 toc.ncx).
 - Filter out non-content pages (cover, copyright, TOC page itself, etc.)
   by matching TOC entries — only spine items referenced in the TOC become chapters.
 - If the TOC references a spine item multiple times (anchor fragments), we treat
   the first fragment occurrence as the chapter boundary and merge the rest into it.
 - Fallback: if the TOC yields no usable items, fall back to all spine DOCUMENT items.
 - Cache results in-process keyed by (epub_path, mtime) to avoid re-parsing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag

import zipfile

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub


@dataclass
class Chapter:
    index: int       # 0-based position in this book's chapter list
    title: str
    html: str        # cleaned chapter HTML (body content)
    word_count: int
    source_url: str | None = None  # canonical per-chapter URL (FanFicFare <meta name="chapterurl">)


_cache: dict[tuple[str, float], list[Chapter]] = {}

# Spine items with fewer than this many words are treated as front/back matter
# (cover image pages, blank pages, maps, etc.) and excluded from the chapter list.
_MIN_CONTENT_WORDS = 50

# FanFicFare's per-chapter canonical URL marker, matched against raw chapter bytes.
_CHAPTERURL_RE = re.compile(
    rb'<meta[^>]*\bname=["\']chapterurl["\'][^>]*\bcontent=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def chapterize(epub_path: Path) -> list[Chapter]:
    """Return ordered chapters for the EPUB at epub_path.

    Results are cached per (path, mtime); safe to call on every request.
    """
    mtime = epub_path.stat().st_mtime
    key = (str(epub_path), mtime)
    if key in _cache:
        return _cache[key]
    result = _parse(epub_path)
    _cache[key] = result
    return result


def _parse(epub_path: Path) -> list[Chapter]:
    book = epub.read_epub(str(epub_path), {"ignore_ncx": False})
    # ebooklib drops <head> contents on parse, so the FanFicFare per-chapter
    # URL meta is read separately from the raw zip, keyed by the file href.
    url_map = _chapter_url_map(epub_path)

    # Build a map from spine item name (href without fragment) → EpubHtml
    spine_items: dict[str, epub.EpubHtml] = {}
    for _idref, _linear in book.spine:
        item = book.get_item_with_id(_idref)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            spine_items[item.get_name()] = item  # type: ignore[arg-type]

    # For FanFicFare books, every real chapter carries a chapterurl; front-matter
    # (Title Page, metadata) does not. So when url_map is populated we treat
    # "has a chapterurl" as the definition of a content chapter.
    is_fff = bool(url_map)

    # Try to extract ordered chapter list from TOC
    toc_entries = _flatten_toc(book.toc)
    chapters: list[Chapter] = []

    if toc_entries:
        seen_hrefs: set[str] = set()
        for title, href in toc_entries:
            bare_href, _fragment = urldefrag(href)
            # Normalize: the href in TOC is relative to the epub root
            bare_name = _normalize_name(bare_href)
            if bare_name in seen_hrefs:
                continue  # already included this file (multi-anchor chapter)
            seen_hrefs.add(bare_name)
            item = spine_items.get(bare_name)
            if item is None:
                continue
            chapter_url = url_map.get(item.get_name().rsplit("/", 1)[-1])
            html = _extract_body(item.get_content())
            if not _is_content_chapter(html, chapter_url, is_fff):
                continue  # FFF front-matter, or cover/map/blank in published books
            chapters.append(
                Chapter(
                    index=len(chapters),
                    title=title or f"Chapter {len(chapters) + 1}",
                    html=html,
                    word_count=_count_words(html),
                    source_url=chapter_url,
                )
            )
    else:
        # Fallback: use all spine document items in order
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            chapter_url = url_map.get(item.get_name().rsplit("/", 1)[-1])
            html = _extract_body(item.get_content())
            if not html.strip() or not _is_content_chapter(html, chapter_url, is_fff):
                continue
            chapters.append(
                Chapter(
                    index=len(chapters),
                    title=_guess_title(html) or f"Chapter {len(chapters) + 1}",
                    html=html,
                    word_count=_count_words(html),
                    source_url=chapter_url,
                )
            )

    return chapters


def _is_content_chapter(html: str, chapter_url: str | None, is_fff: bool) -> bool:
    """Decide whether a spine item is a real story chapter.

    FanFicFare books: a chapter is real iff it has a chapterurl (excludes the
    Title Page / metadata front-matter). Other books: fall back to a word-count
    floor to drop covers, maps, and blank pages.
    """
    if is_fff:
        return chapter_url is not None
    return _count_words(html) >= _MIN_CONTENT_WORDS


def _flatten_toc(toc: list | tuple, depth: int = 0) -> list[tuple[str, str]]:
    """Recursively flatten ebooklib TOC into [(title, href)] pairs."""
    result = []
    for entry in toc:
        if isinstance(entry, epub.Link):
            result.append((entry.title or "", entry.href or ""))
        elif isinstance(entry, tuple):
            # (Section, [children])
            section, children = entry
            if hasattr(section, "href") and section.href:
                result.append((section.title or "", section.href or ""))
            result.extend(_flatten_toc(children, depth + 1))
        elif isinstance(entry, list):
            result.extend(_flatten_toc(entry, depth + 1))
    return result


def _normalize_name(href: str) -> str:
    """Normalise a TOC href to match the spine item name."""
    # Strip leading ./ or /
    return href.lstrip("./").lstrip("/")


def _extract_body(raw: bytes) -> str:
    """Return the inner HTML of the <body> element, cleaned."""
    soup = BeautifulSoup(raw, "lxml-xml")
    body = soup.find("body")
    if body is None:
        # Fall back to html parser if lxml-xml can't find body
        soup = BeautifulSoup(raw, "lxml")
        body = soup.find("body") or soup
    return str(body)


def _chapter_url_map(epub_path: Path) -> dict[str, str]:
    """Map {chapter file href -> canonical per-chapter URL} read from the raw EPUB.

    FanFicFare writes <meta name="chapterurl" content="..."> into each chapter's
    <head> — the exact source URL for that chapter (an AO3 chapter, an FFN chapter
    number, a forum post, etc.). ebooklib strips <head> on parse, so we read the
    raw zip entries directly. Returns {} for non-FanFicFare EPUBs.
    """
    # Keyed by file *basename* so it matches whether ebooklib reports names
    # relative to the OPF directory or to the zip root.
    url_map: dict[str, str] = {}
    try:
        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if not name.endswith((".xhtml", ".html", ".htm")):
                    continue
                m = _CHAPTERURL_RE.search(zf.read(name))
                if m:
                    url_map[name.rsplit("/", 1)[-1]] = m.group(1).decode("utf-8", "replace").strip()
    except (zipfile.BadZipFile, OSError):
        pass
    return url_map


def _count_words(html: str) -> int:
    text = BeautifulSoup(html, "lxml").get_text(" ")
    return len(re.findall(r"\S+", text))


def _guess_title(html: str) -> str:
    """Extract the first heading as a title fallback."""
    soup = BeautifulSoup(html, "lxml")
    for tag in ("h1", "h2", "h3"):
        el = soup.find(tag)
        if el:
            return el.get_text(strip=True)
    return ""
