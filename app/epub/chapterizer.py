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

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag

import warnings
import zipfile

import ebooklib
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub


def _html_soup(markup) -> BeautifulSoup:
    """Parse with the lenient lxml HTML parser (so namespaced epub:type / role attrs
    match as plain strings), suppressing bs4's XML-content notice for XHTML input."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(markup, "lxml")


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

# EPUB3 note-target marker (epub:type="rearnote"/"footnote"/"endnote"), matched on the
# element's epub:type. The in-text reference carries epub:type="noteref" / role="doc-noteref".
_NOTE_TYPE_RE = re.compile(r"(rear|foot|end)note", re.IGNORECASE)
_NOTE_BLOCK_TAGS = ("p", "li", "aside", "div", "section")

# Placeholder root for in-EPUB image references. The chapterizer rewrites every
# relative <img>/<image>/srcset URL to "{_IMG_SENTINEL}/{epub-internal-path}" so a
# chapter's HTML is book-agnostic and cacheable; materialise_image_urls() later swaps
# the sentinel for "{base_url}/img/{calibre_id}" — the route that streams the bytes
# straight out of the (read-only) EPUB zip. Absolute/external/data: URLs are left alone.
_IMG_SENTINEL = "__beacon-epub-img__"


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
    # End/footnotes usually live in a back-matter file separate from the chapter that
    # cites them, so a single-chapter drop would dangle every noteref. Index every note
    # block by id up front so each chapter can inline the ones it references.
    note_index = _build_note_index(book)

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
            html = _extract_body(item.get_content(), item.get_name())
            if not _is_content_chapter(html, chapter_url, is_fff):
                continue  # FFF front-matter, or cover/map/blank in published books
            html = _inline_footnotes(html, item.get_name(), note_index)
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
            html = _extract_body(item.get_content(), item.get_name())
            if not html.strip() or not _is_content_chapter(html, chapter_url, is_fff):
                continue
            html = _inline_footnotes(html, item.get_name(), note_index)
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


def _extract_body(raw: bytes, chapter_name: str = "") -> str:
    """Return the inner HTML of the <body> element, cleaned.

    chapter_name is the EPUB-internal path of this chapter file; it anchors the
    resolution of relative image URLs to absolute (EPUB-root) paths.
    """
    soup = BeautifulSoup(raw, "lxml-xml")
    body = soup.find("body")
    if body is None:
        # Fall back to html parser if lxml-xml can't find body
        soup = BeautifulSoup(raw, "lxml")
        body = soup.find("body") or soup
    _rewrite_images(body, chapter_name)
    return str(body)


def _rewrite_images(body, chapter_name: str) -> None:
    """Point every in-EPUB image at the sentinel route, in place.

    Resolves relative <img src>, SVG <image href>/<image xlink:href>, and <img srcset>
    against the chapter's directory to an EPUB-root path, then prefixes _IMG_SENTINEL.
    External (http/https/data:/root-absolute) references are left untouched.
    """
    base_dir = posixpath.dirname(chapter_name)
    for img in body.find_all(["img", "image"]):
        for attr in ("src", "href", "xlink:href"):
            if img.has_attr(attr):
                resolved = _resolve_internal(img[attr], base_dir)
                if resolved is not None:
                    img[attr] = resolved
        if img.has_attr("srcset"):
            img["srcset"] = _rewrite_srcset(img["srcset"], base_dir)


def _resolve_internal(src: str, base_dir: str) -> str | None:
    """Map an in-EPUB relative URL to "{_IMG_SENTINEL}/{epub-root-path}".

    Returns None (leave the attribute unchanged) for empty, anchor, root-absolute,
    data:, or fully-qualified external URLs — only library-internal paths are rewritten.
    """
    src = (src or "").strip()
    if not src or src.startswith(("#", "/", "data:", "mailto:")) or "://" in src:
        return None
    path, _frag = urldefrag(src)
    resolved = posixpath.normpath(posixpath.join(base_dir, path)).lstrip("/")
    if not resolved or resolved.startswith(".."):
        return None
    return f"{_IMG_SENTINEL}/{resolved}"


def _rewrite_srcset(srcset: str, base_dir: str) -> str:
    """Rewrite each candidate URL in a srcset attribute, preserving descriptors."""
    out = []
    for candidate in srcset.split(","):
        parts = candidate.split()
        if not parts:
            continue
        resolved = _resolve_internal(parts[0], base_dir)
        if resolved is not None:
            parts[0] = resolved
        out.append(" ".join(parts))
    return ", ".join(out)


def materialize_image_urls(html: str, calibre_id: int, base_url: str) -> str:
    """Swap the image sentinel for this book's live image route.

    Called when a drop's content_html is materialised (and anywhere chapter HTML is
    rendered) so stored content is self-contained and byte-stable. The result points at
    "{base_url}/img/{calibre_id}/{epub-path}", served read-only from the EPUB zip.
    """
    return html.replace(_IMG_SENTINEL, f"{base_url.rstrip('/')}/img/{calibre_id}")


def _basename(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _is_noteref(tag) -> bool:
    """An in-text reference link to a note, covering two conventions:

    - EPUB3 semantic: epub:type="noteref" / role="doc-noteref".
    - Plain/older: a superscript anchor — `<a href="…#id"><sup>…</sup></a>` — which is how
      books with no note semantics (e.g. Harari's *Homo Deus*) mark endnotes. The <sup>
      wrapper is the discriminator: it does not fire on the bare-text Part/chapter cross-
      links that nav-heavy books carry.
    """
    if tag.name != "a" or "#" not in (tag.get("href", "") or ""):
        return False
    if "noteref" in (tag.get("epub:type", "") or "") or tag.get("role") == "doc-noteref":
        return True
    return tag.find("sup") is not None


def _cross_file_frag(href: str, current_name: str) -> str | None:
    """Return the fragment id iff href points at a note in *another* spine file.

    Same-file references (`#id`, or a link back to the current file) already resolve inside
    the dropped item, so only cross-file notes need inlining."""
    if "#" not in href:
        return None
    target, frag = href.split("#", 1)
    if not target or _basename(target) == _basename(current_name):
        return None
    return frag or None


def _build_note_index(book) -> dict[str, str]:
    """Map {note id -> inline-ready note HTML} for every cross-file note the book cites.

    Two passes: first collect the fragment ids that note markers point at (so ordinary
    cross-references are ignored), then resolve each id to its note block — unwrapping the
    note's own number/back-link anchors (they'd dangle once detached) and sentinel-
    rewriting any in-EPUB images, so the fragment drops into any chapter as-is.
    """
    soups: dict[str, BeautifulSoup] = {}
    wanted: set[str] = set()
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = _html_soup(item.get_content())
        soups[item.get_name()] = soup
        for a in soup.find_all(_is_noteref):
            frag = _cross_file_frag(a.get("href", ""), item.get_name())
            if frag:
                wanted.add(frag)
    if not wanted:
        return {}

    index: dict[str, str] = {}
    for name, soup in soups.items():
        for el in soup.find_all(id=True):
            nid = el.get("id")
            if nid not in wanted or nid in index:
                continue
            block = el if el.name in _NOTE_BLOCK_TAGS else (el.find_parent(_NOTE_BLOCK_TAGS) or el)
            # Unwrap (keep text, drop the link) the note's own number / back-reference
            # anchors: noterefs and any link out to another file. Keeps the authored
            # number ("1." / "1") while removing links that would dangle once detached.
            for a in block.find_all("a"):
                if _is_noteref(a) or _cross_file_frag(a.get("href", ""), name):
                    a.unwrap()
            _rewrite_images(block, name)
            inner = block.decode_contents().strip()
            if inner:
                index[nid] = inner
    return index


def _inline_footnotes(html: str, chapter_name: str, note_index: dict[str, str]) -> str:
    """Append the cross-file notes this chapter cites as an end-of-chapter footnote block.

    Each cited note is pulled from note_index and rendered in a styled <aside> at the
    chapter's end; the marker is rewritten to a local "#fb-note-{id}" anchor (ids are
    book-unique, so notes never collide when several chapters share one drop). Markers to
    unknown ids (ordinary cross-references) and same-file notes are left untouched.
    """
    if not note_index or ("noteref" not in html and "<sup" not in html and "doc-noteref" not in html):
        return html
    soup = _html_soup(html)
    collected: list[tuple[str, str]] = []  # (anchor_id, note_html)
    seen: set[str] = set()
    for a in soup.find_all(_is_noteref):
        frag = _cross_file_frag(a.get("href", ""), chapter_name)
        if not frag or frag not in note_index:
            continue
        anchor = f"fb-note-{frag}"
        a["href"] = f"#{anchor}"
        if frag not in seen:
            seen.add(frag)
            collected.append((anchor, note_index[frag]))
    if not collected:
        return html

    items = "".join(f'<li id="{aid}">{note}</li>' for aid, note in collected)
    aside = (
        '<aside class="beacon-endnotes" epub:type="footnotes" '
        'style="font-size:0.85em;color:#666;border-top:1px solid #ddd;'
        'margin-top:2em;padding-top:0.5em;">'
        '<ol style="list-style:none;padding-left:0;">'
        f"{items}</ol></aside>"
    )
    body = soup.find("body") or soup
    body.append(_html_soup(aside).find("aside"))
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
