"""Reader page: stable permalink for each drop.

Served at /read/{slug} — used as the guid and fallback link in the feed,
especially for books without a source URL or when a reader clips long content.
"""
from fastapi import APIRouter, Depends, HTTPException
from html import escape

from fastapi.responses import HTMLResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.models import Drop, utcnow
from app.sanitize import CSP_HEADERS, sanitize_html
from app.security import current_feed_secret, sign_image_urls

router = APIRouter()


@router.get("/read/{slug}", response_class=HTMLResponse)
def reader_page(slug: str, db: Session = Depends(get_db)) -> HTMLResponse:
    drop = db.query(Drop).filter(Drop.reader_slug == slug).first()
    if drop is None:
        raise HTTPException(status_code=404)

    # Opening the reader page acknowledges the drop as read (soft read-gating).
    if drop.acknowledged_at is None:
        drop.acknowledged_at = utcnow()
        db.commit()

    book = drop.book
    chapter_label = drop.chapter_titles or f"Chapter {drop.chapter_start + 1}"
    title = f"{book.title} — {chapter_label}"

    prev_drop, next_drop = _neighbours(db, drop)

    # The same plain-GET feedback row the feed items carry, so votes/pause work from /read/ too.
    # Image and feedback URLs are root-relative: the page may be served on the reader origin
    # (BEACON_READER_BASE_URL) rather than base_url, and must not send the browser to another host.
    from app.feed.builder import feedback_block
    return HTMLResponse(
        _reader_html(
            title, book.author, drop.word_count,
            sign_image_urls(
                sanitize_html(drop.content_html), current_feed_secret(db), settings.base_url,
                settings.image_alias_bases, out_base="",
            ),
            feedback_block(drop, base=""),
            prev_slug=prev_drop.reader_slug if prev_drop else None,
            next_slug=next_drop.reader_slug if next_drop else None,
        ),
        headers=CSP_HEADERS,
    )


def _neighbours(db: Session, drop: Drop) -> tuple[Drop | None, Drop | None]:
    """The same story's release just before and just after this one, in release order.

    Only released drops exist, so ▶ Next stops at the latest released chapter. Release order (not
    chapter index) because a stub renumbers physical chapter indices, and a re-release after a
    cursor move is a later release of an earlier chapter."""
    same_book = db.query(Drop).filter(Drop.book_id == drop.book_id, Drop.id != drop.id)
    earlier = or_(Drop.published_at < drop.published_at,
                  and_(Drop.published_at == drop.published_at, Drop.id < drop.id))
    later = or_(Drop.published_at > drop.published_at,
                and_(Drop.published_at == drop.published_at, Drop.id > drop.id))
    prev = same_book.filter(earlier).order_by(Drop.published_at.desc(), Drop.id.desc()).first()
    nxt = same_book.filter(later).order_by(Drop.published_at, Drop.id).first()
    return prev, nxt


def _nav_html(prev_slug: str | None, next_slug: str | None) -> str:
    """← Previous / Next → between this story's releases. Root-relative, like the rest of the page."""
    if prev_slug is None and next_slug is None:
        return ""
    prev = (f'<a rel="prev" href="/read/{escape(prev_slug)}">← Previous</a>' if prev_slug
            else '<span class="off">← Previous</span>')
    nxt = (f'<a rel="next" href="/read/{escape(next_slug)}">Next →</a>' if next_slug
           else '<span class="off" title="Not released yet">Next → <small>(latest released)</small></span>')
    return f'<nav class="chapnav">{prev}{nxt}</nav>'


def _reader_html(
    title: str, author: str, word_count: int, content: str, feedback: str = "",
    prev_slug: str | None = None, next_slug: str | None = None,
) -> str:
    nav = _nav_html(prev_slug, next_slug)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{
      font-family: Georgia, serif;
      max-width: 700px;
      margin: 2em auto;
      padding: 1em;
      line-height: 1.7;
      font-size: 1.1em;
    }}
    h1 {{ font-size: 1.4em; }}
    .meta {{ color: #666; font-size: .9em; margin-bottom: 2em; }}
    .chapter {{ margin-bottom: 3em; }}
    hr {{ border: none; border-top: 1px solid #ddd; margin: 2em 0; }}
    .chapnav {{ display: flex; justify-content: space-between; gap: 1em; margin: 1.5em 0;
               font-family: system-ui, sans-serif; font-size: .95em; }}
    .chapnav a, .chapnav .off {{ padding: .5em .9em; border: 1px solid #ccc; border-radius: 6px;
               text-decoration: none; }}
    .chapnav .off {{ color: #aaa; border-color: #eee; }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  <p class="meta">{escape(author)} &middot; {word_count:,} words</p>
  {nav}
  {content}
  {feedback}
  {nav}
</body>
</html>"""
