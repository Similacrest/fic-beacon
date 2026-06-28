"""Generate a standards-compliant Atom + RSS 2.0 feed from materialized drops.

Each item:
  - <title>: "Book Title — Chapter Title"
  - <link>/<id>: stable permalink (source URL if available, else /read/{slug})
  - Full chapter HTML in <content> (Atom) and <description> (RSS)
  - Four tokenized feedback hyperlinks appended to content, in order:
      [🪝 Extra chapter now] [👍 More like this] [👎 Less like this] [❌ Drop this source]
    up/down are instant bare-GET links (/fb/{token}); extra/drop route through the
    confirmation page (/fb/confirm/{token}). 🪝 extra is shown only when a next unit
    is available. See app/routers/feedback.py for the contract.
"""
from __future__ import annotations

from datetime import timezone

from feedgen.feed import FeedGenerator

from app.config import settings
from app.models import Drop, absolute_chapter_number


def build_feed(
    drops: list[Drop],
    self_url: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> tuple[bytes, bytes]:
    """Return (atom_xml, rss_xml) bytes for the given drops (newest first).

    self_url is the feed's canonical URL (per-slot for channel feeds); it doubles as
    the WebSub topic. A hub link is advertised so WebSub readers can get realtime push.
    """
    self_url = self_url or f"{settings.base_url}/feed"
    fg = FeedGenerator()
    fg.id(self_url)
    fg.title(title or "Fic Beacon — Backlog Feed")
    fg.author({"name": "Fic Beacon"})
    fg.link(href=self_url, rel="self")
    fg.link(href=settings.base_url, rel="alternate")
    fg.link(href=f"{settings.base_url}/websub/hub", rel="hub")
    fg.language("en")
    fg.description(description or "Your Calibre backlog, drip-fed as a serial.")

    for drop in drops:
        _add_entry(fg, drop)

    return fg.atom_str(pretty=True), fg.rss_str(pretty=True)


def _add_entry(fg: FeedGenerator, drop: Drop) -> None:
    book = drop.book
    chapter_label = drop.chapter_titles or f"Chapter {absolute_chapter_number(book, drop.chapter_start)}"
    if ";" in chapter_label:
        # Multiple chapters: show range
        parts = [p.strip() for p in chapter_label.split(";")]
        chapter_label = f"{parts[0]} – {parts[-1]}"

    title = f"{book.title} — {chapter_label}"
    permalink = _permalink(drop)

    content = drop.content_html + _feedback_html(drop, _extra_available(drop))

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


def _extra_available(drop: Drop) -> bool:
    """Whether a 'next unit' exists for this drop's source — drives the 🪝 extra link.

    Every source is an EPUB now: an extra is available iff another chapter remains past
    the cursor. (Tracked stories self-gate the same way; a fetch later adds more.)
    """
    book = drop.book
    if book.status.value == "completed":
        return False
    return book.total_chapters is None or book.cursor_chapter_index < book.total_chapters


def _feedback_html(drop: Drop, extra_available: bool) -> str:
    """Four ordered actions: 🪝 extra · 👍 up · 👎 down · ❌ drop.

    up/down are instant bare-GET links; extra/drop route through the confirm page.
    The 🪝 extra link is shown only when a next unit is available.
    """
    token = drop.feedback_token
    instant = f"{settings.base_url}/fb/{token}"
    confirm = f"{settings.base_url}/fb/confirm/{token}"

    links = []
    if extra_available:
        links.append(f'<a href="{confirm}?action=extra">🪝 Extra chapter now</a>')
    links.append(f'<a href="{instant}?action=up">👍 More like this</a>')
    links.append(f'<a href="{instant}?action=down">👎 Less like this</a>')
    links.append(f'<a href="{confirm}?action=drop">❌ Drop this source</a>')

    return (
        '\n<hr/>\n'
        '<p class="beacon-feedback" style="font-size:0.9em;color:#666;">'
        + ' &nbsp;'.join(links)
        + '</p>'
    )
