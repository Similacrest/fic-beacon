"""Admin UI — Jinja + HTMX single-user management interface."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.calibre.adapter import CalibreAdapter
from app.calibre.genre import effective_genres, pick_channel_id
from app.calibre.status import classify_status
from app.config import settings
from app.epub.chapterizer import chapterize
from app.database import ensure_default_channel, get_db
from app.models import (
    Book, BookStatus, BudgetMode, Channel, Config, Drop,
    FeedbackEvent, WebSubSubscription, absolute_chapter_number,
)
from app.ongoing.feed_url import infer_feed_url
from app.state import LAST_DROP_RUN, LAST_POLL_RUN, LAST_SKIPS, get_run, get_value
from app.version import __version__

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals["version"] = __version__
templates.env.globals["abs_chapter"] = absolute_chapter_number


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app import scheduler

    active = db.query(Book).filter(Book.status == BookStatus.active).order_by(Book.queue_position).all()
    queued = db.query(Book).filter(Book.status == BookStatus.queued).order_by(Book.queue_position).all()
    completed = db.query(Book).filter(Book.status == BookStatus.completed).order_by(Book.added_at.desc()).limit(10).all()
    dropped = db.query(Book).filter(Book.status == BookStatus.dropped).order_by(Book.added_at.desc()).limit(10).all()
    cfg = db.get(Config, 1)
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()

    # ── Per-channel slot view: what's broadcasting in each feed right now ──────
    slot_view = _build_slot_view(db, channels, active, queued)

    # ── System status: cron last/next runs + WebSub subscribers ───────────────
    next_runs = scheduler.next_run_times()
    status = {
        "last_drop": get_run(db, LAST_DROP_RUN),
        "last_poll": get_run(db, LAST_POLL_RUN),
        "next_drop": next_runs.get("drop_cycle"),
        "next_sweep": next_runs.get("feedless_sweep"),
    }
    subscribers = _build_subscriber_view(db, channels)

    try:
        last_skips = json.loads(get_value(db, LAST_SKIPS) or "[]")
    except json.JSONDecodeError:
        last_skips = []

    fetch_progress = _build_fetch_progress(db)

    return templates.TemplateResponse(request, "admin/index.html", {
        "active": active,
        "queued": queued,
        "completed": completed,
        "dropped": dropped,
        "cfg": cfg,
        "channels": channels,
        "slot_view": slot_view,
        "status": status,
        "subscribers": subscribers,
        "last_skips": last_skips,
        "fetch_progress": fetch_progress,
    })


def _build_fetch_progress(db) -> list[dict]:
    """Tracked stories with an async fetch in flight: phase + elapsed seconds since submit."""
    from datetime import timezone
    from app.models import utcnow
    now = utcnow()
    fetching = (
        db.query(Book)
        .filter(Book.tracked.is_(True), Book.last_fetch_status.like("fetching%"))
        .order_by(Book.last_fetch_at)
        .all()
    )
    out = []
    for b in fetching:
        started = b.last_fetch_at
        if started is not None and started.tzinfo is None:  # SQLite drops tz → assume UTC
            started = started.replace(tzinfo=timezone.utc)
        elapsed = int((now - started).total_seconds()) if started else None
        out.append({"book": b, "status": b.last_fetch_status, "elapsed": elapsed})
    return out


def _pending_chapters(book: Book) -> int:
    """How many whole chapters sit past the cursor (rough 'waiting to drop' count)."""
    if book.total_chapters is None:
        return 0
    return max(0, book.total_chapters - book.cursor_chapter_index)


def _build_slot_view(db, channels, active, queued):
    """Assemble, per channel, what each numbered slot feed is broadcasting:
    its streaming backlog book + pinned tracked stories, the most recent drops in that
    feed, plus the channel's queued backlog books and tracked stories with chapters
    waiting past their cursor.
    """
    view = []
    for ch in channels:
        # Recent drops for this channel, bucketed by feed_key (slot).
        recent = (
            db.query(Drop)
            .join(Drop.book)
            .filter(Drop.channel_id == ch.id)
            .order_by(Drop.published_at.desc())
            .limit(40)
            .all()
        )
        drops_by_slot: dict[str, list[Drop]] = {}
        for d in recent:
            drops_by_slot.setdefault(d.feed_key or "?", []).append(d)

        slots = []
        for s in range(1, ch.parallel_slots + 1):
            here = [b for b in active if b.channel_id == ch.id and b.slot_index == s]
            slots.append({
                "n": s,
                "epub": next((b for b in here if not b.tracked), None),
                "ongoings": [b for b in here if b.tracked],
                "drops": drops_by_slot.get(str(s), [])[:5],
            })

        queued_epubs = [b for b in queued if b.channel_id == ch.id and not b.tracked]
        # Tracked stories with chapters waiting past their cursor — eligible next
        # broadcast, may be held out by the stochastic budget.
        waiting_ongoings = [
            b for b in active
            if b.channel_id == ch.id and b.tracked and _pending_chapters(b)
        ]
        view.append({
            "channel": ch,
            "slots": slots,
            "queued_epubs": queued_epubs,
            "waiting_ongoings": waiting_ongoings,
        })
    return view


def _build_subscriber_view(db, channels):
    """Group WebSub subscribers by topic feed, flagging verified/expired state."""
    from app.models import utcnow
    now = utcnow()
    by_slug = {ch.slug: ch.name for ch in channels}
    subs = db.query(WebSubSubscription).order_by(WebSubSubscription.topic_url).all()
    out = []
    for sub in subs:
        topic = sub.topic_url.split("?", 1)[0]
        label = topic.replace(f"{settings.base_url}/feed/", "")
        exp = sub.lease_expires_at
        if exp is not None and exp.tzinfo is None:
            from datetime import timezone
            exp = exp.replace(tzinfo=timezone.utc)
        out.append({
            "label": label,
            "callback": sub.callback_url,
            "verified": sub.verified,
            "expired": exp is not None and exp < now,
            "lease_expires_at": exp,
        })
    return out


# ── Calibre import / Library ──────────────────────────────────────────────────

@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    adapter = CalibreAdapter(settings.calibre_library_path)
    calibre_books = adapter.list_books()
    existing_ids = {b.calibre_id for b in db.query(Book.calibre_id).all()}
    importable = [b for b in calibre_books if b.calibre_id not in existing_ids]
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()
    # The single "Add" button routes each book by its #status (see classify_status); expose
    # the verdict so the row can show whether it will be tracked or queued.
    routing = {b.calibre_id: classify_status(b.source_status) for b in importable}
    return templates.TemplateResponse(
        request, "admin/library.html", {
            "books": importable,
            "channels": channels,
            "routing": routing,
        }
    )


# Backward-compatible redirect for old /admin/import bookmarks.
@router.get("/import", response_class=HTMLResponse)
def import_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/library", status_code=301)


def _epub_chapter_count(adapter: CalibreAdapter, cbook) -> int:
    """Number of content chapters in a book's current EPUB (0 if missing/unreadable).

    Falls back to 0 (start at chapter 1) rather than failing the whole batch import on a
    single malformed EPUB.
    """
    try:
        return len(chapterize(adapter.epub_path(cbook)))
    except Exception:
        return 0


@router.post("/library/add")
def add_from_library(
    calibre_ids: list[int] = Form(...),
    channel_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Add selected Calibre books, routing each by its #status custom column.

    - #status updating (In-Progress/Incomplete/Hiatus) → a **tracked** auto-updating source.
      If the book is marked #read=Yes (caught up) the cursor starts at the current EPUB end so
      only *new* chapters drop; otherwise it starts at chapter 1.
    - #status done (Completed/Abandoned/Published) or blank → a **backlog** queue entry read
      from chapter 1.

    The books already live in Calibre, so tracked ones need no initial fetch; future updates
    are RSS-triggered (inferred feed) or picked up by the daily feedless sweep.
    """
    adapter = CalibreAdapter(settings.calibre_library_path)
    default_channel_id = ensure_default_channel(db).id
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.id).all() if not channel_id else []
    existing_ids = {b.calibre_id for b in db.query(Book.calibre_id).all()}
    max_pos = db.query(func.max(Book.queue_position)).scalar() or 0
    for cid in calibre_ids:
        if cid in existing_ids:
            continue
        cbook = adapter.get_book(cid)
        if cbook is None:
            continue
        if channel_id:
            target_id = channel_id
        else:
            genres = effective_genres(cbook.genres, cbook.genre_tags, cbook.source_url)
            target_id = pick_channel_id(genres, channels, default_channel_id)
        max_pos += 1
        if classify_status(cbook.source_status) == "updating":
            count = _epub_chapter_count(adapter, cbook) if cbook.read else 0
            db.add(Book(
                calibre_id=cid,
                title=cbook.title,
                author=cbook.author,
                source_url=cbook.source_url,
                tracked=True,
                feed_url=infer_feed_url(cbook.source_url),
                status=BookStatus.active,
                queue_position=max_pos,
                channel_id=target_id,
                cursor_chapter_index=count,
                total_chapters=count or None,
            ))
        else:
            db.add(Book(
                calibre_id=cid,
                title=cbook.title,
                author=cbook.author,
                source_url=cbook.source_url,
                status=BookStatus.queued,
                queue_position=max_pos,
                channel_id=target_id,
            ))
    db.commit()
    return RedirectResponse(url="/admin/library", status_code=303)


@router.post("/books/{book_id}/track")
def set_book_tracked(
    book_id: int,
    tracked: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Toggle whether an existing book auto-updates. Tracked books are never queued."""
    book = db.get(Book, book_id)
    if book is not None:
        book.tracked = bool(tracked)
        if book.tracked:
            if book.feed_url is None:
                book.feed_url = infer_feed_url(book.source_url)
            if book.status == BookStatus.queued:
                book.status = BookStatus.active
                book.slot_index = None
        elif book.status == BookStatus.active:
            # Untracked ⇒ finite backlog: re-enter the queue so the one-per-slot
            # rebalance treats it like any other backlog book (cursor is preserved).
            book.status = BookStatus.queued
            book.slot_index = None
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
    feeds = {}
    for ch in channels:
        keys = [str(i) for i in range(1, ch.parallel_slots + 1)]
        feeds[ch.id] = [
            (k, f"{settings.base_url}/feed/{ch.slug}/{k}?token={token}") for k in keys
        ]
    return templates.TemplateResponse(request, "admin/channels.html", {
        "channels": channels, "feeds": feeds,
        "wpm": cfg.wpm if cfg else 250,
    })


@router.post("/channels")
def create_channel(
    name: str = Form(...),
    genre_match: str = Form(""),
    parallel_slots: int = Form(2),
    budget_mode: str = Form("words"),
    budget: int = Form(5000),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/channels", status_code=303)
    slug = _slugify(name)
    base_slug, n = slug, 2
    while db.query(Channel).filter(Channel.slug == slug).first() is not None:
        slug = f"{base_slug}-{n}"
        n += 1
    max_order = db.query(func.max(Channel.queue_order)).scalar() or 0
    mode = BudgetMode(budget_mode) if budget_mode in ("words", "minutes") else BudgetMode.words
    db.add(Channel(
        name=name,
        slug=slug,
        genre_match=genre_match.strip() or None,
        parallel_slots=max(1, parallel_slots),
        budget_mode=mode,
        budget=max(1, budget),
        queue_order=max_order + 1,
    ))
    db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.post("/channels/{channel_id}/edit")
def edit_channel(
    channel_id: int,
    name: str = Form(...),
    genre_match: str = Form(""),
    parallel_slots: int = Form(1),
    budget_mode: str = Form("words"),
    budget: int = Form(5000),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Edit channel settings. The slug (and thus feed URLs) stays fixed."""
    channel = db.get(Channel, channel_id)
    name = name.strip()
    if channel and name:
        channel.name = name
        channel.genre_match = genre_match.strip() or None
        channel.parallel_slots = max(1, parallel_slots)
        channel.budget_mode = BudgetMode(budget_mode) if budget_mode in ("words", "minutes") else BudgetMode.words
        channel.budget = max(1, budget)
        db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.post("/channels/{channel_id}/delete")
def delete_channel(channel_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    channel = db.get(Channel, channel_id)
    if channel:
        fallback = (
            db.query(Channel)
            .filter(Channel.id != channel_id)
            .order_by(Channel.queue_order, Channel.id)
            .first()
        )
        if fallback is not None:
            db.query(Book).filter(Book.channel_id == channel_id).update(
                {Book.channel_id: fallback.id, Book.slot_index: None},
                synchronize_session=False,
            )
            db.delete(channel)
            db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.post("/books/{book_id}/set-channel")
def set_book_channel(
    book_id: int,
    channel_id: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    book = db.get(Book, book_id)
    target = db.get(Channel, channel_id)
    if book and target and book.channel_id != target.id:
        book.channel_id = target.id
        book.slot_index = None
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


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
            .filter(Book.queue_position < book.queue_position, Book.status == book.status)
            .order_by(Book.queue_position.desc())
            .first()
        )
    else:
        swap = (
            db.query(Book)
            .filter(Book.queue_position > book.queue_position, Book.status == book.status)
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
        # cursor_floor blocks rewinding into a stub-rewritten body (see stub handling).
        book.cursor_chapter_index = max(book.cursor_floor, min(chapter_index, upper))
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


def _book_chapter_count(book: Book) -> int | None:
    """Content-chapter count of a book's current EPUB, computed on demand (None if no EPUB)."""
    if book.calibre_id is None:
        return None
    adapter = CalibreAdapter(settings.calibre_library_path)
    cbook = adapter.get_book(book.calibre_id)
    if cbook is None:
        return None
    return _epub_chapter_count(adapter, cbook)


@router.post("/books/{book_id}/cursor-latest")
def cursor_latest(book_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Jump the cursor to the current EPUB end — 'ongoing' handling, only new chapters drop.

    Computes the chapter count on demand, so it works immediately after adding a book (before
    any drop cycle has populated total_chapters).
    """
    book = db.get(Book, book_id)
    if book is not None:
        count = _book_chapter_count(book)
        if count is not None:
            book.total_chapters = count
            book.cursor_chapter_index = max(book.cursor_floor, count)
            db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/cursor-start")
def cursor_start(book_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Rewind the cursor to the start — 'from-start' handling, read the whole work."""
    book = db.get(Book, book_id)
    if book is not None:
        count = _book_chapter_count(book)
        if count is not None:  # populate the count so the UI can show progress
            book.total_chapters = count
        book.cursor_chapter_index = book.cursor_floor
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/set-weight")
def set_weight(
    book_id: int,
    weight: float = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book is not None:
        book.quota_weight = max(0.1, min(10.0, round(weight, 3)))
        db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/batch-drop")
def batch_drop(
    book_ids: list[int] = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.planner.planner import _refill_book_channel
    for book_id in book_ids:
        book = db.get(Book, book_id)
        if book and book.status not in (BookStatus.completed, BookStatus.dropped):
            book.status = BookStatus.dropped
            book.slot_index = None
            _refill_book_channel(db, book)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/drop")
def drop_book(book_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    from app.planner.planner import _refill_book_channel
    book = db.get(Book, book_id)
    if book:
        book.status = BookStatus.dropped
        book.slot_index = None
        _refill_book_channel(db, book)
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
        book.slot_index = None
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
    wpm: int = Form(...),
    cadence_cron: str = Form(...),
    thumbs_down_drop_threshold: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cfg = db.get(Config, 1)
    if cfg is None:
        return RedirectResponse(url="/admin/config", status_code=303)
    cfg.wpm = wpm
    cfg.cadence_cron = cadence_cron
    cfg.thumbs_down_drop_threshold = thumbs_down_drop_threshold
    db.commit()
    from app import scheduler
    try:
        scheduler.update_cadence(cadence_cron)
    except Exception:
        pass
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/config/regenerate-secret")
def regenerate_feed_secret(db: Session = Depends(get_db)) -> RedirectResponse:
    """Rotate the feed secret — every feed URL's ?token= changes.

    This is the hard reset: all existing reader subscriptions (every channel) break
    until re-added with the new URLs, and stale WebSub subscriptions are cleared since
    their topics no longer resolve. Per-drop feedback links are unaffected.
    """
    cfg = db.get(Config, 1)
    if cfg is not None:
        cfg.feed_secret = secrets.token_urlsafe(32)
        db.query(WebSubSubscription).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


# ── Manual drop / poll triggers ───────────────────────────────────────────────

@router.post("/trigger-drop")
def trigger_drop(db: Session = Depends(get_db)) -> RedirectResponse:
    from app.ongoing.poller import poll_all_feeds
    from app.planner.planner import run_drop_cycle
    from app.websub.publisher import publish_updates
    # Check feeds first so any new chapters are queued for fetch (async — they land in a
    # later broadcast); this drop broadcasts the current EPUB state.
    poll_all_feeds(db)
    drops = run_drop_cycle(db, settings.calibre_library_path)
    db.commit()
    publish_updates(db, drops)
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/poll-now")
def poll_now(db: Session = Depends(get_db)) -> RedirectResponse:
    from app.ongoing.poller import poll_all_feeds
    poll_all_feeds(db)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)
