"""Admin routes for tracked (auto-updating) stories.

A tracked source is a Book with `tracked=True` and a `source_url` FanFicFare can fetch.
The fetcher container downloads its chapters into the Calibre library; from there it is
served as a normal EPUB through the chapterizer/cursor path, just like the backlog. An
optional `feed_url` (RSS) is used only as a fast *notification* that new chapters exist —
its body is never read. Feed-less stories (auth-gated) are refreshed by the daily sweep.

Tracked stories are never queued and never capped: all are eligible each broadcast
(self-gating on whether a chapter sits past the cursor), load-balanced across the channel's
numbered feed slots by _assign_slots.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import ensure_default_channel, get_db
from app.models import Book, BookStatus, Channel, Drop, FeedbackEvent
from app.ongoing.feed_url import infer_feed_url
from app.version import __version__

router = APIRouter(prefix="/admin/ongoing")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals["version"] = __version__


def _add_tracked_story(
    db: Session, story_url: str, title: str, channel_id: int
) -> Book | None:
    """Create a tracked story row (no fetch yet) unless its URL is already registered.

    The actual FanFicFare download is kicked off in the background by the caller (see
    scheduler.trigger_fetch_pending) so the request returns promptly. Returns the new
    Book, or None if the URL was blank / already tracked.
    """
    story_url = story_url.strip()
    if not story_url:
        return None
    if db.query(Book).filter(Book.source_url == story_url).first() is not None:
        return None
    max_pos = db.query(func.max(Book.queue_position)).scalar() or 0
    book = Book(
        tracked=True,
        source_url=story_url,
        feed_url=infer_feed_url(story_url),
        title=(title or story_url).strip(),
        status=BookStatus.active,
        queue_position=max_pos + 1,
        channel_id=channel_id,
        last_fetch_status="pending",
    )
    db.add(book)
    db.flush()
    return book


@router.get("/", response_class=HTMLResponse)
def ongoing_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    sources = db.query(Book).filter(Book.tracked.is_(True)).order_by(Book.title).all()
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()
    chan_name = {c.id: c.name for c in db.query(Channel).all()}
    return templates.TemplateResponse(request, "admin/ongoing.html", {
        "sources": sources,
        "channels": channels,
        "chan_name": chan_name,
    })


@router.post("/add")
def add_story(
    source_url: str = Form(...),
    title: str = Form(""),
    channel_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app import scheduler
    target_id = channel_id or ensure_default_channel(db).id
    _add_tracked_story(db, source_url, title, target_id)
    db.commit()
    scheduler.trigger_fetch_pending()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/add-bulk")
def add_bulk(
    urls: str = Form(...),
    channel_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Add many tracked stories at once — one story URL per line."""
    from app import scheduler
    target_id = channel_id or ensure_default_channel(db).id
    added = sum(
        1 for line in urls.splitlines()
        if line.strip() and _add_tracked_story(db, line, "", target_id)
    )
    db.commit()
    scheduler.trigger_fetch_pending()
    return RedirectResponse(url=f"/admin/ongoing/?added={added}", status_code=303)


@router.post("/{source_id}/toggle")
def toggle_source(source_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Pause (drop) or resume (activate) a tracked source."""
    source = db.get(Book, source_id)
    if source is None or not source.tracked:
        raise HTTPException(status_code=404)
    source.status = (
        BookStatus.dropped if source.status == BookStatus.active else BookStatus.active
    )
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/{source_id}/fetch-now")
def fetch_now(source_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Queue an async fetch for one source (initial download or manual refresh).

    Returns immediately — the fetcher downloads in the background (up to ~15 min) and the
    row shows `fetching…` until the poll job folds the result.
    """
    from app import scheduler
    source = db.get(Book, source_id)
    if source is None or not source.tracked:
        raise HTTPException(status_code=404)
    scheduler.submit_and_track(db, [source])
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


def _delete_source(db: Session, source: Book) -> None:
    drop_ids = [d.id for d in db.query(Drop.id).filter(Drop.book_id == source.id)]
    if drop_ids:
        db.query(FeedbackEvent).filter(FeedbackEvent.drop_id.in_(drop_ids)).delete(
            synchronize_session=False
        )
        db.query(Drop).filter(Drop.id.in_(drop_ids)).delete(synchronize_session=False)
    db.delete(source)


@router.post("/{source_id}/delete")
def delete_source(source_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    source = db.get(Book, source_id)
    if source is not None:
        _delete_source(db, source)
        db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/batch-delete")
def batch_delete_sources(
    book_ids: list[int] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    for book_id in (book_ids or []):
        source = db.get(Book, book_id)
        if source is not None:
            _delete_source(db, source)
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


def _selected_tracked(db: Session, book_ids: list[int] | None) -> list[Book]:
    """Resolve a batch of ids to the tracked Books that exist (None ⇒ empty selection)."""
    return [
        b for b in (db.get(Book, i) for i in (book_ids or []))
        if b is not None and b.tracked
    ]


@router.post("/batch-pause")
def batch_pause_sources(
    book_ids: list[int] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Pause (exclude from polling/broadcast) every selected tracked story."""
    for source in _selected_tracked(db, book_ids):
        source.status = BookStatus.dropped
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/batch-resume")
def batch_resume_sources(
    book_ids: list[int] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Resume every selected tracked story."""
    for source in _selected_tracked(db, book_ids):
        source.status = BookStatus.active
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/batch-fetch")
def batch_fetch_sources(
    book_ids: list[int] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Queue an async fetch for every selected tracked story (one batched job)."""
    from app import scheduler
    sources = _selected_tracked(db, book_ids)
    if sources:
        scheduler.submit_and_track(db, sources)
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)
