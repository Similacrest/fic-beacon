"""Admin UI — Jinja + HTMX single-user management interface."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, contains_eager

from app.calibre.adapter import CalibreAdapter
from app.calibre.genre import effective_genres, pick_channel_id
from app.calibre.status import classify_status
from app.config import settings
from app.epub.chapterizer import chapterize
from app.database import delete_book_cascade, ensure_default_channel, get_db
from app import timefmt
from app.models import (
    Book, BookStatus, BudgetMode, Channel, Config, Drop, FeedbackAction, Schedule,
    WebSubSubscription, absolute_chapter_number,
)
from app.ongoing.feed_url import infer_feed_url
from app.state import (
    BOOK_NOTE_PREFIX, LAST_POLL_RUN, LAST_RELEASE_RUN, LAST_SKIPS, get_run, get_value, list_with_prefix,
)
from app.version import __version__

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals["version"] = __version__
templates.env.globals["abs_chapter"] = absolute_chapter_number
templates.env.filters["ago"] = timefmt.relative
templates.env.filters["stamp"] = timefmt.absolute


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

    try:
        last_skips = json.loads(get_value(db, LAST_SKIPS) or "[]")
    except json.JSONDecodeError:
        last_skips = []
    book_notes = {
        int(k[len(BOOK_NOTE_PREFIX):]): v
        for k, v in list_with_prefix(db, BOOK_NOTE_PREFIX) if k[len(BOOK_NOTE_PREFIX):].isdigit()
    }

    # ── Per-channel slot view: what's broadcasting in each feed right now ──────
    slot_view = _build_slot_view(db, channels, active, queued, last_skips, book_notes)

    # ── System status: cron last/next runs + WebSub subscribers ───────────────
    schedules = db.query(Schedule).order_by(Schedule.sort_order, Schedule.id).all()
    next_times = scheduler.next_release_times()
    status = {
        "last_drop": get_run(db, LAST_RELEASE_RUN),
        "last_poll": get_run(db, LAST_POLL_RUN),
        "next_sweep": scheduler.next_sweep_time(),
    }
    schedule_rows = [{"schedule": sc, "next": next_times.get(sc.id)} for sc in schedules]
    # Manual release cycle / feed check currently running (drives the progress banner and
    # disables the trigger buttons). None when idle.
    cycle = scheduler.cycle_status()
    push_view, push_orphans = _build_push_view(db, channels)

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
        "push_view": push_view,
        "push_orphans": push_orphans,
        "fetch_progress": fetch_progress,
        "cycle": cycle,
        "schedule_rows": schedule_rows,
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


def _skip_entry(last_skips: list[dict], book: Book, channel_name: str) -> dict | None:
    """The book's entry in the last release's skip log. Entries written before 0.17 carry no
    book_id; match those on title within the channel."""
    for e in last_skips:
        if e.get("book_id") == book.id:
            return e
    for e in last_skips:
        if "book_id" not in e and e.get("title") == book.title and e.get("channel") == channel_name:
            return e
    return None


def _build_slot_view(db, channels, active, queued, last_skips=(), book_notes=None):
    """Assemble, per channel, what each numbered slot feed is broadcasting: one row per source
    pinned to the slot (its backlog book first, then tracked stories) with a one-line *why*
    (`planner.explain`), the most recent drops in that feed, and the channel's queue.
    """
    from sqlalchemy import func

    from app.planner.explain import explain, pending_chapters
    from app.planner.planner import unread_streak

    last_skips = list(last_skips)
    book_notes = book_notes or {}
    last_drop_at = dict(
        db.query(Drop.book_id, func.max(Drop.published_at)).group_by(Drop.book_id).all()
    )

    def row(book: Book, ch: Channel, queue_pos: int | None = None) -> dict:
        return {
            "book": book,
            "why": explain(
                book, _skip_entry(last_skips, book, ch.name),
                unread=unread_streak(db, book) if book.status == BookStatus.active else 0,
                note=book_notes.get(book.id), queue_pos=queue_pos,
            ),
            "pending": pending_chapters(book),
            "last_drop": last_drop_at.get(book.id),
        }

    view = []
    for ch in channels:
        # Recent drops for this channel, bucketed by feed_key (slot).
        recent = (
            db.query(Drop)
            .join(Drop.book)
            .options(contains_eager(Drop.book))
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
            here.sort(key=lambda b: (b.tracked, b.title.lower()))  # the backlog book first
            slots.append({
                "n": s,
                "rows": [row(b, ch) for b in here],
                "drops": drops_by_slot.get(str(s), [])[:5],
            })

        # Active sources without a valid slot (e.g. slot count just lowered) still need a why.
        unslotted = [
            b for b in active
            if b.channel_id == ch.id and not (1 <= (b.slot_index or 0) <= ch.parallel_slots)
        ]
        queued_here = [b for b in queued if b.channel_id == ch.id]
        view.append({
            "channel": ch,
            "slots": slots,
            "unslotted": [row(b, ch) for b in unslotted],
            "queue": [row(b, ch, i) for i, b in enumerate(queued_here, 1)],
        })
    return view


_FEED_PATH = re.compile(r"^/feed/([^/]+)/(\d+)/?$")


def _build_push_view(db, channels) -> tuple[dict[tuple[str, int], dict], int]:
    """WebSub subscribers per slot feed: `{(channel slug, slot): {"active": n, "bad": n}}` plus the
    number of subscriptions whose topic is no longer a channel/slot feed. `bad` = unverified or
    lease expired (they won't receive pushes). Matched on the topic's *path*, so a subscription
    made under an older base URL still lands on its feed."""
    from datetime import timezone

    from app.models import utcnow
    now = utcnow()
    known = {(c.slug, n) for c in channels for n in range(1, c.parallel_slots + 1)}
    per_feed: dict[tuple[str, int], dict] = {}
    orphans = 0
    for sub in db.query(WebSubSubscription).all():
        m = _FEED_PATH.match(urlparse(sub.topic_url).path)
        key = (m.group(1), int(m.group(2))) if m else None
        if key not in known:
            orphans += 1
            continue
        exp = sub.lease_expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        ok = sub.verified and not (exp is not None and exp < now)
        slot = per_feed.setdefault(key, {"active": 0, "bad": 0})
        slot["active" if ok else "bad"] += 1
    return per_feed, orphans


@router.get("/books/{book_id}", response_class=HTMLResponse)
def story_page(book_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """One place per story: its status and why, every control, and its history."""
    from app.models import FeedbackEvent
    from app.planner.explain import explain, pending_chapters
    from app.planner.planner import unread_streak

    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404)
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()
    channel = db.get(Channel, book.channel_id)
    try:
        last_skips = json.loads(get_value(db, LAST_SKIPS) or "[]")
    except json.JSONDecodeError:
        last_skips = []
    queue_pos = _queue_position(db, book)
    why = explain(
        book, _skip_entry(last_skips, book, channel.name if channel else ""),
        unread=unread_streak(db, book) if book.status == BookStatus.active else 0,
        note=get_value(db, f"{BOOK_NOTE_PREFIX}{book.id}"), queue_pos=queue_pos,
    )
    drops = (
        db.query(Drop).filter(Drop.book_id == book.id)
        .order_by(Drop.published_at.desc(), Drop.id.desc()).limit(15).all()
    )
    votes = (
        db.query(FeedbackEvent)
        .filter(FeedbackEvent.book_id == book.id, FeedbackEvent.action != FeedbackAction.read)
        .order_by(FeedbackEvent.created_at.desc()).limit(15).all()
    )
    return templates.TemplateResponse(request, "admin/story.html", {
        "book": book,
        "channel": channel,
        "channels": channels,
        "why": why,
        "pending": pending_chapters(book),
        "queue_pos": queue_pos,
        "drops": drops,
        "votes": votes,
        "here": f"/admin/books/{book.id}",
    })


@router.post("/books/{book_id}/set-slot")
def set_slot(
    book_id: int,
    request: Request,
    slot_index: int = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Manually pin an active source to a feed slot (drag-and-drop on the dashboard).

    Slots are per-channel; this never changes a book's channel. Backlog (untracked) books
    are one-per-slot, so moving one onto an occupied slot **swaps** the two. Tracked stories
    are uncapped, so they just move. The planner's slot assignment is sticky, so a valid
    manual pin persists across broadcasts (see planner._assign_slots).
    """
    book = db.get(Book, book_id)
    if book is not None and book.status == BookStatus.active:
        channel = db.get(Channel, book.channel_id)
        if (channel is not None and 1 <= slot_index <= channel.parallel_slots
                and slot_index != book.slot_index):
            if not book.tracked:
                occupant = (
                    db.query(Book)
                    .filter(
                        Book.channel_id == book.channel_id,
                        Book.tracked.is_(False),
                        Book.status == BookStatus.active,
                        Book.slot_index == slot_index,
                        Book.id != book.id,
                    )
                    .first()
                )
                if occupant is not None:  # keep one backlog book per slot — swap
                    occupant.slot_index = book.slot_index
            book.slot_index = slot_index
            db.commit()
    return _saved(request, next)


# ── Calibre import / Library ──────────────────────────────────────────────────

def _source_domain(url: str | None) -> str:
    """Bare host of a source URL (drops a leading www.), for the library 'source' filter."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _queue_position(db: Session, book: Book) -> int | None:
    """1-based place in its channel's queue (None unless the book is queued)."""
    if book.status != BookStatus.queued:
        return None
    ids = [
        bid for (bid,) in db.query(Book.id)
        .filter(Book.status == BookStatus.queued, Book.channel_id == book.channel_id)
        .order_by(Book.queue_position, Book.id)
    ]
    return ids.index(book.id) + 1 if book.id in ids else None


def _id_list(raw: str | None, cap: int = 200) -> list[int]:
    """Comma-separated ids from a query string (digits only, capped)."""
    out: list[int] = []
    for part in (raw or "").split(","):
        if part.strip().isdigit():
            out.append(int(part))
    return out[:cap]


def _add_results(db: Session, params) -> dict | None:
    """What the last Add did, from the redirect's query string: per source a link to its story
    page and its current *why* (queued #n, downloading…, on air…), plus counts."""
    from app.planner.explain import explain

    groups = {}
    for key in ("added", "dupes"):
        books = [b for bid in _id_list(params.get(key)) if (b := db.get(Book, bid)) is not None]
        groups[key] = [
            {"book": b, "channel": db.get(Channel, b.channel_id),
             "why": explain(b, None, queue_pos=_queue_position(db, b))}
            for b in books
        ]
    invalid = int(params["invalid"]) if str(params.get("invalid", "")).isdigit() else 0
    missing = int(params["missing"]) if str(params.get("missing", "")).isdigit() else 0
    empty = params.get("error") == "empty"
    if not (groups["added"] or groups["dupes"] or invalid or missing or empty):
        return None
    return {**groups, "invalid": invalid, "missing": missing, "empty": empty}


@router.get("/add", response_class=HTMLResponse)
def add_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """One place to add sources: paste story URLs and/or pick books already in Calibre."""
    adapter = CalibreAdapter(settings.calibre_library_path)
    calibre_books = adapter.list_books()
    existing_ids = {b.calibre_id for b in db.query(Book.calibre_id).all()}
    importable = [b for b in calibre_books if b.calibre_id not in existing_ids]
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.name).all()
    # Each book is routed by its #status (see classify_status); expose the verdict so the row
    # can show whether it will be tracked or queued.
    routing = {b.calibre_id: classify_status(b.source_status) for b in importable}
    # Per-row source website (host) + the distinct values, to drive the library filters.
    source_domain = {b.calibre_id: _source_domain(b.source_url) for b in importable}
    statuses = sorted({b.source_status for b in importable if b.source_status})
    sources = sorted({d for d in source_domain.values() if d})
    return templates.TemplateResponse(
        request, "admin/add.html", {
            "books": importable,
            "channels": channels,
            "routing": routing,
            "source_domain": source_domain,
            "statuses": statuses,
            "sources": sources,
            "results": _add_results(db, request.query_params),
        }
    )


@router.post("/add")
def add_sources(
    urls: str = Form(""),
    calibre_ids: list[int] = Form([]),
    channel_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """The single Add action: pasted story URLs (one per line → tracked, downloaded by the
    fetcher) and/or selected Calibre books (routed by `#status`) into one channel — or, with no
    channel chosen, Calibre books by genre and URLs into General. Redirects to the Add page with
    the outcome of every item."""
    from app import scheduler
    from app.ongoing.feed_url import is_fetchable_url
    from app.planner.planner import assign_channel_slots
    from app.routers.ongoing import _add_tracked_story, _valid_channel_id, existing_for_url

    lines = [ln.strip() for ln in urls.splitlines() if ln.strip()]
    if not lines and not calibre_ids:
        return RedirectResponse(url="/admin/add?error=empty", status_code=303)

    added: list[Book] = []
    dupes: list[Book] = []
    invalid = 0
    url_channel = _valid_channel_id(db, channel_id)
    for line in lines:
        if not is_fetchable_url(line):
            invalid += 1
            continue
        book = _add_tracked_story(db, line, "", url_channel)
        if book is not None:
            added.append(book)
        else:
            prior = existing_for_url(db, line)
            if prior is not None and prior not in dupes:
                dupes.append(prior)
    if lines:
        assign_channel_slots(db, url_channel)
    imported, present, missing = import_library_books(db, calibre_ids, channel_id) if calibre_ids else ([], [], 0)
    added += imported
    dupes += [b for b in present if b not in dupes]
    db.commit()
    if any(b.calibre_id is None for b in added):
        scheduler.trigger_fetch_pending()

    from urllib.parse import urlencode
    q = {}
    if added:
        q["added"] = ",".join(str(b.id) for b in added[:200])
    if dupes:
        q["dupes"] = ",".join(str(b.id) for b in dupes[:200])
    if invalid:
        q["invalid"] = invalid
    if missing:
        q["missing"] = missing
    return RedirectResponse(url="/admin/add" + (f"?{urlencode(q)}" if q else ""), status_code=303)


@router.get("/library", response_class=HTMLResponse)
def library_redirect() -> RedirectResponse:
    """The Library page became part of Add."""
    return RedirectResponse(url="/admin/add", status_code=303)


# Backward-compatible redirect for old /admin/import bookmarks.
@router.get("/import", response_class=HTMLResponse)
def import_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/add", status_code=301)


def _epub_chapter_count(adapter: CalibreAdapter, cbook) -> int:
    """Number of content chapters in a book's current EPUB (0 if missing/unreadable).

    Falls back to 0 (start at chapter 1) rather than failing the whole batch import on a
    single malformed EPUB.
    """
    try:
        return len(chapterize(adapter.epub_path(cbook)))
    except Exception:
        return 0


def import_library_books(
    db: Session, calibre_ids: list[int], channel_id: int | None,
) -> tuple[list[Book], list[Book], int]:
    """Import Calibre books, routing each by its #status custom column.

    - #status updating (In-Progress/Incomplete/Hiatus) → a **tracked** auto-updating source.
      If the book is marked #read=Yes (caught up) the cursor starts at the current EPUB end so
      only *new* chapters drop; otherwise it starts at chapter 1.
    - #status done (Completed/Abandoned/Published) or blank → a **backlog** queue entry read
      from chapter 1.

    The books already live in Calibre, so tracked ones need no initial fetch; future updates
    are RSS-triggered (inferred feed) or picked up by the daily feedless sweep.

    Returns `(added, already_present, not_in_calibre)`. The caller commits.
    """
    adapter = CalibreAdapter(settings.calibre_library_path)
    default_channel_id = ensure_default_channel(db).id
    if channel_id and db.get(Channel, channel_id) is None:
        channel_id = None  # stale/forged id → fall back to genre routing instead of a 500
    cfg = db.get(Config, 1)
    tracked_weight = cfg.tracked_default_weight if cfg else 2.0
    channels = db.query(Channel).order_by(Channel.queue_order, Channel.id).all() if not channel_id else []
    existing = {b.calibre_id: b for b in db.query(Book).filter(Book.calibre_id.isnot(None))}
    max_pos = db.query(func.max(Book.queue_position)).scalar() or 0
    touched_channels: set[int] = set()
    added: list[Book] = []
    present: list[Book] = []
    missing = 0
    for cid in dict.fromkeys(calibre_ids):  # de-duplicated, order kept
        if cid in existing:
            present.append(existing[cid])
            continue
        cbook = adapter.get_book(cid)
        if cbook is None:
            missing += 1
            continue
        if channel_id:
            target_id = channel_id
        else:
            genres = effective_genres(cbook.genres, cbook.genre_tags, cbook.source_url)
            target_id = pick_channel_id(genres, channels, default_channel_id)
        max_pos += 1
        touched_channels.add(target_id)
        if classify_status(cbook.source_status) == "updating":
            count = _epub_chapter_count(adapter, cbook) if cbook.read else 0
            book = Book(
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
                # Prioritise ongoing serials over the finite backlog (config-tunable).
                quota_weight=tracked_weight,
            )
        else:
            book = Book(
                calibre_id=cid,
                title=cbook.title,
                author=cbook.author,
                source_url=cbook.source_url,
                status=BookStatus.queued,
                queue_position=max_pos,
                channel_id=target_id,
            )
        db.add(book)
        added.append(book)
    # Place the imports in slots now (balanced) rather than lumping them until the next cycle.
    from app.planner.planner import assign_channel_slots
    for tid in touched_channels:
        assign_channel_slots(db, tid)
    db.flush()
    return added, present, missing


@router.post("/library/add")
def add_from_library(
    calibre_ids: list[int] = Form(...),
    channel_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Add selected Calibre books (compat endpoint — the Add page posts to `/admin/add`)."""
    import_library_books(db, calibre_ids, channel_id)
    db.commit()
    return RedirectResponse(url="/admin/add", status_code=303)


@router.post("/books/{book_id}/track")
def set_book_tracked(
    book_id: int,
    tracked: int = Form(...),
    next: str | None = Form(None),
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
    return _back(next)


# ── Channels ──────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "channel"


def _unique_slug(db: Session, base: str, exclude_id: int | None = None) -> str:
    """A slug not used by any other channel, suffixing -2, -3, … on collision."""
    base = _slugify(base)
    slug, n = base, 2
    while True:
        clash = db.query(Channel).filter(Channel.slug == slug)
        if exclude_id is not None:
            clash = clash.filter(Channel.id != exclude_id)
        if clash.first() is None:
            return slug
        slug = f"{base}-{n}"
        n += 1


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
    })


@router.post("/channels")
def create_channel(
    name: str = Form(...),
    genre_match: str = Form(""),
    parallel_slots: int = Form(2),
    weight: float = Form(1.0),
    feed_item_limit: int = Form(settings.feed_item_limit),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/channels", status_code=303)
    slug = _unique_slug(db, name)
    max_order = db.query(func.max(Channel.queue_order)).scalar() or 0
    db.add(Channel(
        name=name,
        slug=slug,
        genre_match=genre_match.strip() or None,
        parallel_slots=max(1, parallel_slots),
        weight=max(0.01, weight),
        feed_item_limit=max(1, feed_item_limit),
        queue_order=max_order + 1,
    ))
    db.commit()
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.post("/channels/{channel_id}/edit")
def edit_channel(
    channel_id: int,
    name: str = Form(...),
    slug: str = Form(""),
    genre_match: str = Form(""),
    parallel_slots: int = Form(1),
    weight: float = Form(1.0),
    feed_item_limit: int = Form(50),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Edit channel settings, including the slug.

    Changing the slug rewrites this channel's feed URLs (`/feed/{slug}/{key}`), so any reader
    already subscribed to the old URL stops updating until re-subscribed. Blank ⇒ keep current.
    """
    channel = db.get(Channel, channel_id)
    name = name.strip()
    if channel and name:
        channel.name = name
        if slug.strip():
            channel.slug = _unique_slug(db, slug, exclude_id=channel.id)
        channel.genre_match = genre_match.strip() or None
        channel.parallel_slots = max(1, parallel_slots)
        channel.weight = max(0.01, weight)
        channel.feed_item_limit = max(1, feed_item_limit)
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
    next: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    book = db.get(Book, book_id)
    target = db.get(Channel, channel_id)
    if book and target and book.channel_id != target.id:
        from app.planner.planner import _assign_slots
        old = db.get(Channel, book.channel_id)
        if not book.tracked and book.status == BookStatus.active:
            # A streaming backlog book joins the new channel's queue rather than taking over a
            # slot there (which would push out the book already streaming in it).
            book.status = BookStatus.queued
        if not book.tracked:  # … at the back of that queue, not wherever its old position lands
            last = db.query(func.max(Book.queue_position)).scalar() or 0
            book.queue_position = last + 1
        book.channel_id = target.id
        book.slot_index = None
        db.flush()
        # Rebalance both now: the old channel's freed slot takes its next queued book; in the new
        # one a tracked story is placed and a backlog book streams if a slot is free.
        for ch in (old, target):
            if ch is not None:
                _assign_slots(db, ch.parallel_slots, ch.id)
        db.commit()
    return _back(next)


# ── Queue management ──────────────────────────────────────────────────────────

@router.post("/books/{book_id}/move")
def move_book(
    book_id: int,
    direction: str = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book is None:
        return _back(next)

    if direction == "up":
        swap = (
            db.query(Book)
            .filter(Book.queue_position < book.queue_position, Book.status == book.status,
                    Book.channel_id == book.channel_id)
            .order_by(Book.queue_position.desc())
            .first()
        )
    else:
        swap = (
            db.query(Book)
            .filter(Book.queue_position > book.queue_position, Book.status == book.status,
                    Book.channel_id == book.channel_id)
            .order_by(Book.queue_position)
            .first()
        )
    if swap:
        book.queue_position, swap.queue_position = swap.queue_position, book.queue_position
        db.commit()
    return _back(next)


def _back(next: str | None) -> RedirectResponse:
    """Redirect after a book action: back to the page it came from (`next`, an /admin/ path —
    the dashboard or a story page), else the dashboard. Never off-site."""
    ok = isinstance(next, str) and next.startswith("/admin/") and "//" not in next and "\\" not in next
    url = next if ok else "/admin/"  # (a direct call passes the Form() default — not a str)
    return RedirectResponse(url=url, status_code=303)


def _saved(request: Request, next: str | None = None) -> Response:
    """Reply to an inline auto-save: a bodyless 204 for HTMX (no swap, keeps focus),
    or a redirect back for a plain form submit (no-JS fallback)."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204)
    return _back(next)


@router.post("/books/{book_id}/set-cursor")
def set_cursor(
    book_id: int,
    request: Request,
    chapter_index: int = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    book = db.get(Book, book_id)
    if book is not None:
        upper = book.total_chapters if book.total_chapters is not None else chapter_index
        # cursor_floor blocks rewinding into a stub-rewritten body (see stub handling).
        book.cursor_chapter_index = max(book.cursor_floor, min(chapter_index, upper))
        db.commit()
    return _saved(request, next)


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
def cursor_latest(book_id: int, next: str | None = Form(None), db: Session = Depends(get_db)) -> RedirectResponse:
    """Jump the cursor to the current EPUB end — 'ongoing' handling, only new chapters drop.

    Computes the chapter count on demand, so it works immediately after adding a book (before
    any release cycle has populated total_chapters).
    """
    book = db.get(Book, book_id)
    if book is not None:
        count = _book_chapter_count(book)
        if count is not None:
            book.total_chapters = count
            book.cursor_chapter_index = max(book.cursor_floor, count)
            db.commit()
    return _back(next)


@router.post("/books/{book_id}/cursor-start")
def cursor_start(book_id: int, next: str | None = Form(None), db: Session = Depends(get_db)) -> RedirectResponse:
    """Rewind the cursor to the start — 'from-start' handling, read the whole work."""
    book = db.get(Book, book_id)
    if book is not None:
        count = _book_chapter_count(book)
        if count is not None:  # populate the count so the UI can show progress
            book.total_chapters = count
        book.cursor_chapter_index = book.cursor_floor
        db.commit()
    return _back(next)


@router.post("/books/{book_id}/set-weight")
def set_weight(
    book_id: int,
    request: Request,
    weight: float = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    from app.planner.planner import WEIGHT_CAP
    book = db.get(Book, book_id)
    if book is not None:
        book.quota_weight = max(0.0, min(WEIGHT_CAP, round(weight, 3)))  # never auto-drops
        db.commit()
    return _saved(request, next)


@router.post("/books/{book_id}/drop")
def drop_book(book_id: int, next: str | None = Form(None), db: Session = Depends(get_db)) -> RedirectResponse:
    from app.planner.planner import _refill_book_channel
    book = db.get(Book, book_id)
    if book:
        book.status = BookStatus.dropped
        book.slot_index = None
        _refill_book_channel(db, book)
        db.commit()
    return _back(next)


@router.post("/books/{book_id}/pause")
def pause_book_route(book_id: int, next: str | None = Form(None), db: Session = Depends(get_db)) -> RedirectResponse:
    from app.planner.planner import pause_book
    book = db.get(Book, book_id)
    if book and not book.paused:
        pause_book(db, book)
        db.commit()
    return _back(next)


@router.post("/books/{book_id}/resume")
def resume_book_route(book_id: int, next: str | None = Form(None), db: Session = Depends(get_db)) -> RedirectResponse:
    from app.planner.planner import resume_book
    book = db.get(Book, book_id)
    if book and book.paused:
        resume_book(db, book)
        db.commit()
    return _back(next)


@router.post("/dropped/clear")
def clear_dropped(db: Session = Depends(get_db)) -> RedirectResponse:
    """Permanently remove all dropped sources (and their drops/feedback)."""
    dropped = db.query(Book).filter(Book.status == BookStatus.dropped).all()
    for book in dropped:
        delete_book_cascade(db, book)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/books/{book_id}/requeue")
def requeue_book(book_id: int, next: str | None = Form(None), db: Session = Depends(get_db)) -> RedirectResponse:
    book = db.get(Book, book_id)
    if book:
        # Tracked stories are never queued: they stay active and get re-pinned to a slot.
        book.status = BookStatus.active if book.tracked else BookStatus.queued
        book.slot_index = None
        # Restart from the beginning of the *current* body — but never below cursor_floor (a stub
        # rewrote the body; chapters under the floor no longer exist).
        book.cursor_chapter_index = book.cursor_floor
        book.fresh_from_index = None
        book.paused = False
        book.thumbs_down = 0
        book.thumbs_up = 0
        book.quota_weight = 1.0
        book.cooldown_remaining = 0
        if book.tracked:
            from app.planner.planner import assign_channel_slots
            assign_channel_slots(db, book.channel_id)
        db.commit()
    return _back(next)


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    cfg = db.get(Config, 1)
    return templates.TemplateResponse(request, "admin/config.html", {"cfg": cfg})


@router.post("/config")
def save_config(
    wpm: int = Form(...),
    vote_step: float = Form(...),
    extra_boost_step: float = Form(...),
    weight_skip_floor: float = Form(...),
    unacked_penalty_floor: float = Form(...),
    extra_per_channel_per_cycle: int = Form(1),
    tracked_default_weight: float = Form(...),
    websub_max_push_bytes: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cfg = db.get(Config, 1)
    if cfg is None:
        return RedirectResponse(url="/admin/config", status_code=303)
    cfg.wpm = wpm
    cfg.vote_step = max(0.01, vote_step)
    cfg.extra_boost_step = max(0.0, extra_boost_step)
    cfg.weight_skip_floor = max(0.0, weight_skip_floor)
    cfg.unacked_penalty_floor = min(1.0, max(0.05, unacked_penalty_floor))
    cfg.extra_per_channel_per_cycle = max(0, extra_per_channel_per_cycle)
    cfg.tracked_default_weight = max(0.1, tracked_default_weight)
    cfg.websub_max_push_bytes = max(0, websub_max_push_bytes)
    db.commit()
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


# ── Release schedules ─────────────────────────────────────────────────────────

def _schedule_fields(name: str, cron: str, budget: float, budget_mode: str) -> tuple[dict | None, str | None]:
    """Validate a schedule form → (fields, None) or (None, error message)."""
    from app import scheduler
    name, cron = name.strip(), " ".join(cron.split())
    if not name:
        return None, "Give the schedule a name."
    reason = scheduler.validate_cron(cron)
    if reason:
        return None, f"Invalid cron “{cron}”: {reason}"
    mode = BudgetMode(budget_mode) if budget_mode in ("words", "minutes") else BudgetMode.words
    return {"name": name, "cron": cron, "budget": max(0.01, budget), "budget_mode": mode}, None


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app import scheduler
    cfg = db.get(Config, 1)
    return templates.TemplateResponse(request, "admin/schedules.html", {
        "schedules": db.query(Schedule).order_by(Schedule.sort_order, Schedule.id).all(),
        "next_times": scheduler.next_release_times(),
        "wpm": cfg.wpm if cfg else 250,
        "channels": db.query(Channel).order_by(Channel.queue_order, Channel.name).all(),
    })


@router.post("/schedules")
def create_schedule(
    name: str = Form(...), cron: str = Form(...), budget: float = Form(5000),
    budget_mode: str = Form("words"), db: Session = Depends(get_db),
) -> RedirectResponse:
    from urllib.parse import quote
    from app import scheduler
    fields, error = _schedule_fields(name, cron, budget, budget_mode)
    if error:
        return RedirectResponse(url=f"/admin/schedules?error={quote(error)}", status_code=303)
    max_order = db.query(func.max(Schedule.sort_order)).scalar() or 0
    db.add(Schedule(sort_order=max_order + 1, **fields))
    db.commit()
    scheduler.reload_schedules()
    return RedirectResponse(url="/admin/schedules", status_code=303)


def _schedule_status(schedule: Schedule | None, error: str | None = None) -> str:
    """Inline status line for a schedule row (the auto-save response fragment)."""
    from html import escape

    from app import scheduler
    if error:
        return f'<span class="sched-err" style="color:#a00">✗ Not saved — {escape(error)}</span>'
    if not schedule.enabled:
        return "✓ saved — disabled, never fires"
    nxt = scheduler.next_release_times().get(schedule.id)
    when = f" · next: {nxt.strftime('%a %Y-%m-%d %H:%M')}" if nxt else " · not scheduled"
    return f"✓ saved{when}"


@router.post("/schedules/{schedule_id}/edit")
def edit_schedule(
    request: Request, schedule_id: int, name: str = Form(...), cron: str = Form(...), budget: float = Form(5000),
    budget_mode: str = Form("words"), enabled: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from urllib.parse import quote
    from app import scheduler
    schedule = db.get(Schedule, schedule_id)
    htmx = bool(request.headers.get("HX-Request"))  # inline auto-save from the Schedules page
    if schedule is None:
        if htmx:
            return HTMLResponse(_schedule_status(None, "This schedule no longer exists — reload."))
        return RedirectResponse(url="/admin/schedules", status_code=303)
    fields, error = _schedule_fields(name, cron, budget, budget_mode)
    if error:
        if htmx:  # nothing is saved until the whole row validates
            return HTMLResponse(_schedule_status(None, error))
        return RedirectResponse(url=f"/admin/schedules?error={quote(error)}", status_code=303)
    for key, value in fields.items():
        setattr(schedule, key, value)
    schedule.enabled = enabled is not None  # an unchecked checkbox posts nothing
    db.commit()
    scheduler.reload_schedules()
    if htmx:
        return HTMLResponse(_schedule_status(schedule))
    return RedirectResponse(url="/admin/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Delete a schedule. The last one is kept — a release needs a schedule to know its budget."""
    from app import scheduler
    schedule = db.get(Schedule, schedule_id)
    if schedule is not None and db.query(Schedule).count() > 1:
        db.delete(schedule)
        db.commit()
        scheduler.reload_schedules()
    return RedirectResponse(url="/admin/schedules", status_code=303)


# ── Manual release / feed-check triggers ──────────────────────────────────────
# Both run OFF the request path: the scheduler runs the work in its own thread and the
# request returns at once, so the admin can safely close/refresh the tab. An in-process
# lock (scheduler.cycle_status) means a new run is refused while one is in flight; the
# dashboard also disables the buttons and shows a progress banner while busy.

@router.post("/release-now")
def release_now(
    schedule_id: int | None = Form(None), db: Session = Depends(get_db)
) -> RedirectResponse:
    """Run a release now under the chosen schedule's budget (default: the first enabled one)."""
    from app import scheduler
    scheduler.trigger_release_now(schedule_id)  # no-op if a cycle is already running
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/poll-now")
def poll_now(db: Session = Depends(get_db)) -> RedirectResponse:
    from app import scheduler
    scheduler.trigger_feed_check()  # no-op if a cycle is already running
    return RedirectResponse(url="/admin/", status_code=303)
