"""Admin routes for ongoing serial sources.

An ongoing source is a Book with kind=ongoing and an RSS feed_url, living in a channel.
The poller buffers its new chapters (OngoingEntry, released=False); the planner releases
them, batched, at broadcast time — weighted in the channel budget like EPUBs, and votable/
droppable via the same feedback links.

Ongoings are never queued and never capped: all active ongoings are eligible each broadcast
(self-gating on whether a chapter is buffered). They are load-balanced (sticky) across the
channel's numbered feed slots by _assign_slots — several ongoings may share a slot alongside
the slot's active EPUB.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import ensure_inbox_channel, get_db
from app.models import Book, BookKind, BookStatus, Channel, Drop, FeedbackEvent, OngoingEntry
from app.ongoing.opml import parse_opml
from app.version import __version__

router = APIRouter(prefix="/admin/ongoing")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals["version"] = __version__


def _add_source(
    db: Session,
    title: str,
    feed_url: str,
    channel_id: int,
    assume_read: bool = True,
    linked_calibre_id: int | None = None,
) -> Book | None:
    """Create an ongoing source if its feed_url isn't already registered.

    When assume_read=True (default), the source's existing feed entries are immediately
    polled and marked as already-read so only future chapters reach the reader.
    Returns the new Book, or None if it already existed / URL was blank.
    """
    feed_url = feed_url.strip()
    if not feed_url:
        return None
    exists = db.query(Book).filter(Book.feed_url == feed_url).first()
    if exists is not None:
        return None
    max_pos = db.query(func.max(Book.queue_position)).scalar() or 0
    book = Book(
        kind=BookKind.ongoing,
        feed_url=feed_url,
        title=(title or feed_url).strip(),
        status=BookStatus.active,
        queue_position=max_pos + 1,
        channel_id=channel_id,
        linked_calibre_id=linked_calibre_id,
    )
    db.add(book)
    db.flush()
    if assume_read:
        from app.ongoing.poller import seed_source_as_read
        try:
            seed_source_as_read(db, book)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Could not seed '%s' as already-read; buffered entries will release at next drop",
                book.title,
            )
    return book


@router.get("/", response_class=HTMLResponse)
def ongoing_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    sources = (
        db.query(Book)
        .filter(Book.kind == BookKind.ongoing)
        .order_by(Book.title)
        .all()
    )
    channels = db.query(Channel).filter(Channel.is_inbox.is_(False)).order_by(Channel.queue_order, Channel.name).all()
    chan_name = {c.id: c.name for c in db.query(Channel).all()}
    # Unreleased (buffered) entry count per source.
    buffered = dict(
        db.query(OngoingEntry.source_id, func.count(OngoingEntry.id))
        .filter(OngoingEntry.released.is_(False))
        .group_by(OngoingEntry.source_id)
        .all()
    )
    return templates.TemplateResponse(request, "admin/ongoing.html", {
        "sources": sources,
        "channels": channels,
        "chan_name": chan_name,
        "buffered": buffered,
    })


@router.post("/add")
def add_feed(
    feed_url: str = Form(...),
    title: str = Form(""),
    channel_id: int | None = Form(None),
    linked_calibre_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.database import ensure_default_channel
    target_id = channel_id or ensure_default_channel(db).id
    _add_source(db, title, feed_url, target_id, assume_read=True, linked_calibre_id=linked_calibre_id)
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/import-opml")
async def import_opml(
    opml_file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    content = await opml_file.read()
    try:
        entries = parse_opml(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # OPML imports land in the hidden Inbox — user assigns them to real channels manually.
    target_id = ensure_inbox_channel(db).id
    # Don't poll at upload time — entries will be assumed-read on first Poll Now.
    added = sum(
        1 for title, url in entries if _add_source(db, title, url, target_id, assume_read=False)
    )
    db.commit()
    return RedirectResponse(url=f"/admin/ongoing/?imported={added}", status_code=303)


@router.post("/{source_id}/toggle")
def toggle_source(source_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Pause (drop) or resume (activate) an ongoing source."""
    source = db.get(Book, source_id)
    if source is None or source.kind != BookKind.ongoing:
        raise HTTPException(status_code=404)
    source.status = (
        BookStatus.dropped if source.status == BookStatus.active else BookStatus.active
    )
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/{source_id}/delete")
def delete_source(source_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    source = db.get(Book, source_id)
    if source and source.kind == BookKind.ongoing:
        drop_ids = [d.id for d in db.query(Drop.id).filter(Drop.book_id == source.id)]
        if drop_ids:
            db.query(FeedbackEvent).filter(FeedbackEvent.drop_id.in_(drop_ids)).delete(
                synchronize_session=False
            )
            db.query(Drop).filter(Drop.id.in_(drop_ids)).delete(synchronize_session=False)
        db.delete(source)  # ongoing_entries cascade-delete with the source
        db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/batch-delete")
def batch_delete_sources(
    book_ids: list[int] = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    for book_id in book_ids:
        source = db.get(Book, book_id)
        if source and source.kind == BookKind.ongoing:
            drop_ids = [d.id for d in db.query(Drop.id).filter(Drop.book_id == source.id)]
            if drop_ids:
                db.query(FeedbackEvent).filter(FeedbackEvent.drop_id.in_(drop_ids)).delete(
                    synchronize_session=False
                )
                db.query(Drop).filter(Drop.id.in_(drop_ids)).delete(synchronize_session=False)
            db.delete(source)
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/poll-now")
def poll_now(db: Session = Depends(get_db)) -> RedirectResponse:
    from app.ongoing.poller import poll_all_feeds
    poll_all_feeds(db)
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)
