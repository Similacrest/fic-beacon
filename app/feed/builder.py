"""Generate a standards-compliant Atom + RSS 2.0 feed from materialized drops.

Each item:
  - <title>: "Book Title — Chapter Title"
  - <link>/<id>: stable permalink (source URL if available, else /read/{slug})
  - Full chapter HTML in <content> (Atom) and <description> (RSS)
  - Three tokenized feedback hyperlinks appended to content:
      [👍 More like this] [👎 Drop this book] [➕ Extra chapter now]
    These are plain <a href> GET links to /fb/confirm/{token}?action=...
    The /fb/confirm page shows a confirmation form; mutation happens on POST.
    This guards against reader/proxy link prefetching.
"""
from __future__ import annotations

from datetime import timezone

from feedgen.feed import FeedGenerator

from app.config import settings
from app.models import Drop


def build_feed(drops: list[Drop]) -> tuple[bytes, bytes]:
    """Return (atom_xml, rss_xml) bytes for the given drops (newest first)."""
    fg = FeedGenerator()
    fg.id(f"{settings.base_url}/feed")
    fg.title("Fic Beacon — Backlog Feed")
    fg.author({"name": "Fic Beacon"})
    fg.link(href=f"{settings.base_url}/feed", rel="self")
    fg.link(href=settings.base_url, rel="alternate")
    fg.language("en")
    fg.description("Your Calibre backlog, drip-fed as a serial.")

    for drop in drops:
        _add_entry(fg, drop)

    return fg.atom_str(pretty=True), fg.rss_str(pretty=True)


def _add_entry(fg: FeedGenerator, drop: Drop) -> None:
    book = drop.book
    chapter_label = drop.chapter_titles or f"Chapter {drop.chapter_start + 1}"
    if ";" in chapter_label:
        # Multiple chapters: show range
        parts = [p.strip() for p in chapter_label.split(";")]
        chapter_label = f"{parts[0]} – {parts[-1]}"

    title = f"{book.title} — {chapter_label}"
    permalink = _permalink(drop)

    content = drop.content_html + _feedback_html(drop)

    fe = fg.add_entry(order="append")
    # GUID must be unique AND stable per drop, independent of the link target.
    # (Several drops of one FanFicFare book would otherwise share the work URL
    #  and get collapsed into a single item by readers.) reader_slug is a uuid4.
    fe.id(f"urn:fic-beacon:drop:{drop.reader_slug}")
    fe.guid(f"urn:fic-beacon:drop:{drop.reader_slug}", permalink=False)
    fe.title(title)
    fe.link(href=permalink)
    fe.published(drop.published_at.replace(tzinfo=timezone.utc))
    fe.updated(drop.published_at.replace(tzinfo=timezone.utc))
    fe.author({"name": book.author})
    fe.content(content, type="html")
    fe.summary(f"{book.title} — {chapter_label} ({drop.word_count:,} words)")


def _permalink(drop: Drop) -> str:
    """The clickable link for the item, in order of precision:
    1. The exact per-chapter source URL (FanFicFare chapterurl) for this drop.
    2. The whole-work source URL (Calibre 'url' identifier) as a fallback.
    3. Our self-hosted reader page (non-FanFicFare books).
    """
    if drop.source_url:
        return drop.source_url
    if drop.book.source_url:
        return drop.book.source_url
    return f"{settings.base_url}/read/{drop.reader_slug}"


def _feedback_html(drop: Drop) -> str:
    base = f"{settings.base_url}/fb/confirm/{drop.feedback_token}"
    up_url = f"{base}?action=up"
    down_url = f"{base}?action=down"
    extra_url = f"{base}?action=extra"
    return (
        f'\n<hr/>\n'
        f'<p class="beacon-feedback" style="font-size:0.9em;color:#666;">'
        f'<a href="{up_url}">👍 More like this</a> &nbsp;'
        f'<a href="{down_url}">👎 Drop this book</a> &nbsp;'
        f'<a href="{extra_url}">➕ Extra chapter now</a>'
        f'</p>'
    )
