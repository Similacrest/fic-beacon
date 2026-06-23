"""Admin UI — Jinja + HTMX single-user management interface."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.calibre.adapter import CalibreAdapter
from app.config import settings
from app.database import get_db
from app.models import Book, BookStatus, Channel, Config, Drop, FeedbackEvent
from app.version import __version__

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals["version"] = __version__


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    active = db.query(Book).filter(Book.status == BookStatus.active).order_by(Book.queue_position).all()
    queued = db.query(Book).filter(Book.status == BookStatus.queued).order_by(Book.queue_position).all()
    completed = db.query(Book).filter(Book.status == BookStatus.completed).order_by(Book.added_at.desc()).limit(10).all()
    dropped = db.query(Book).filter(Book.status == BookStatus.dropped).order_by(Book.added_at.desc()).limit(10).all()
    cfg = db.get(Config, 1)
    return templates.TemplateResponse(request, "admin/index.html", {
        "active": active,
        "queued": queued,
        "completed": completed,
        "dropped": dropped,
        "cfg": cfg,
        "feed_url": f"{settings.base_url}/feed?token={cfg.feed_secret if cfg else ''}",
    })


# ── Calibre import ────────────────────────────────────────────────────────────

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    adapter = CalibreAdapter(settings.calibre_library_path)
    calibre_books = adapter.list_books()
    existing_ids = {b.calibre_id for b in db.query(Book.calibre_id).all()}
    importable = [b for b in calibre_books if b.calibre_id not in existing_ids]
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()
    return templates.TemplateResponse(
        request, "admin/import.html", {"books": importable, "channels": channels}
    )


@router.post("/import")
def do_import(
    calibre_ids: list[int] = Form(...),
    channel_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    adapter = CalibreAdapter(settings.calibre_library_path)
    max_pos = db.query(Book.queue_position).order_by(Book.queue_position.desc()).scalar() or 0
    for cid in calibre_ids:
        cbook = adapter.get_book(cid)
        if cbook is None:
            continue
        max_pos += 1
        db.add(Book(
            calibre_id=cid,
            title=cbook.title,
            author=cbook.author,
            source_url=cbook.source_url,
            status=BookStatus.queued,
            queue_position=max_pos,
            channel_id=channel_id or None,
        ))
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


# ── Channels ──────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "channel"


@router.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()
    cfg = db.get(Config, 1)
    token = cfg.feed_secret if cfg else ""
    # Per-channel slot feed URLs (numbered backlog slots + the shared 'ongoing' feed).
    feeds = {}
    for ch in channels:
        keys = [str(i) for i in range(1, ch.parallel_slots + 1)] + ["ongoing"]
        feeds[ch.id] = [
            (k, f"{settings.base_url}/feed/{ch.slug}/{k}?token={token}") for k in keys
        ]
    return templates.TemplateResponse(request, "admin/channels.html", {
        "channels": channels, "feeds": feeds,
    })


@router.post("/channels")
def create_channel(
    name: str = Form(...),
    tag_match: str = Form(""),
    parallel_slots: int = Form(2),
    budget_words: int = Form(5000),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    name = name.strip()
    if name:
        slug = _slugify(name)
        # Ensure slug uniqueness with a numeric suffix if needed.
        base_slug, n = slug, 2
        while db.query(Channel).filter(Channel.slug == slug).first() is not None:
            slug = f"{base_slug}-{n}"
            n += 1
        max_order = db.query(Channel.queue_order).order_by(Channel.queue_order.desc()).scalar() or 0
        db.add(Channel(
            name=name,
            slug=slug,
            tag_match=tag_match.strip() or None,
            parallel_slots=max(1, parallel_slots),
            budget_words=max(1, budget_words),
            queue_order=max_order + 1,
        ))
        db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.post("/channels/{channel_id}/delete")
def delete_channel(channel_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    channel = db.get(Channel, channel_id)
    if channel:
        # Unassign member books (they fall back to the default group).
        db.query(Book).filter(Book.channel_id == channel_id).update(
            {Book.channel_id: None, Book.slot_index: None}, synchronize_session=False
        )
        db.delete(channel)
        db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


# ── Queue management ──────────────────────────────────────────────────────────

@router.post("/books/{book_id}/move")
def move_book(
    book_id: int,
    direction: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book is None:
        return RedirectResponse(url="/admin/", status_code=303)

    if direction == "up":
        swap = (
            db.query(Book)
            .filter(
                Book.queue_position < book.queue_position,
                Book.status == book.status,
            )
            .order_by(Book.queue_position.desc())
            .first()
        )
    else:
        swap = (
            db.query(Book)
            .filter(
                Book.queue_position > book.queue_position,
                Book.status == book.status,
            )
            .order_by(Book.queue_position)
            .first()
        )
    if swap:
        book.queue_position, swap.queue_position = swap.queue_position, book.queue_position
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/set-cursor")
def set_cursor(
    book_id: int,
    chapter_index: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book is not None:
        upper = book.total_chapters if book.total_chapters is not None else chapter_index
        book.cursor_chapter_index = max(0, min(chapter_index, upper))
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/drop")
def drop_book(book_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book:
        book.status = BookStatus.dropped
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/dropped/clear")
def clear_dropped(db: Session = Depends(get_db)) -> RedirectResponse:
    """Permanently remove all dropped sources (and their drops/feedback)."""
    dropped = db.query(Book).filter(Book.status == BookStatus.dropped).all()
    for book in dropped:
        drop_ids = [d.id for d in db.query(Drop.id).filter(Drop.book_id == book.id)]
        if drop_ids:
            db.query(FeedbackEvent).filter(FeedbackEvent.drop_id.in_(drop_ids)).delete(
                synchronize_session=False
            )
            db.query(Drop).filter(Drop.id.in_(drop_ids)).delete(synchronize_session=False)
        db.delete(book)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/requeue")
def requeue_book(book_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book:
        book.status = BookStatus.queued
        book.cursor_chapter_index = 0
        book.thumbs_down = 0
        book.thumbs_up = 0
        book.quota_weight = 1.0
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    cfg = db.get(Config, 1)
    return templates.TemplateResponse(request, "admin/config.html", {"cfg": cfg})


@router.post("/config")
def save_config(
    global_budget_words: int = Form(...),
    global_budget_minutes: int = Form(...),
    budget_mode: str = Form(...),
    wpm: int = Form(...),
    overshoot_tolerance: int = Form(...),
    parallel_slots: int = Form(...),
    cadence_cron: str = Form(...),
    thumbs_down_drop_threshold: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cfg = db.get(Config, 1)
    if cfg is None:
        return RedirectResponse(url="/admin/config", status_code=303)
    cfg.global_budget_words = global_budget_words
    cfg.global_budget_minutes = global_budget_minutes
    cfg.budget_mode = budget_mode  # type: ignore[assignment]
    cfg.wpm = wpm
    cfg.overshoot_tolerance = overshoot_tolerance
    cfg.parallel_slots = parallel_slots
    cfg.cadence_cron = cadence_cron
    cfg.thumbs_down_drop_threshold = thumbs_down_drop_threshold
    db.commit()
    # Reschedule the running drop job so the new cadence takes effect immediately
    from app import scheduler
    try:
        scheduler.update_cadence(cadence_cron)
    except Exception:
        pass  # job not yet scheduled (e.g. during tests); next startup picks it up
    return RedirectResponse(url="/admin/", status_code=303)


# ── Manual drop trigger ───────────────────────────────────────────────────────

@router.post("/trigger-drop")
def trigger_drop(db: Session = Depends(get_db)) -> RedirectResponse:
    from app.planner.planner import run_drop_cycle
    run_drop_cycle(db, settings.calibre_library_path)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)
