"""The dashboard's at-a-glance health: what needs attention, and how much has been released lately.

Everything here is derived from rows and records that already exist (the books, the drops, the
last run result, the skip log); nothing is stored for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Book, BookStatus, Drop, utcnow
from app.timefmt import local_tz

SPARK_DAYS = 14
STUCK_DOWNLOAD_SECONDS = 30 * 60


def volume_series(db: Session, days: int = SPARK_DAYS, now: datetime | None = None) -> dict:
    """Words and chapters released per local day for the last `days` days (oldest first).

    Days are cut in the scheduling timezone, so "today" matches the user's release times.
    Returns the bars plus the total and the peak day, ready to render.
    """
    tz = local_tz()
    now = (now or utcnow()).astimezone(tz)
    today = now.date()
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(timezone.utc)
    buckets = {first + timedelta(days=i): [0, 0] for i in range(days)}
    rows = db.query(Drop.published_at, Drop.word_count, Drop.chapter_start, Drop.chapter_end).filter(
        Drop.published_at >= since
    )
    for published, words, c0, c1 in rows:
        if published.tzinfo is None:  # SQLite drops tz → stored as UTC
            published = published.replace(tzinfo=timezone.utc)
        day = published.astimezone(tz).date()
        if day in buckets:
            buckets[day][0] += words or 0
            buckets[day][1] += max(1, (c1 - c0) + 1)
    peak = max((w for w, _ in buckets.values()), default=0)
    bars = [
        {
            "date": d, "words": w, "chapters": c,
            "pct": round(100 * w / peak) if peak else 0,
            "today": d == today,
        }
        for d, (w, c) in buckets.items()
    ]
    top = max(bars, key=lambda b: b["words"]) if peak else None
    return {
        "bars": bars,
        "total_words": sum(b["words"] for b in bars),
        "total_chapters": sum(b["chapters"] for b in bars),
        "peak_words": peak,
        "peak_date": top["date"] if top else None,
        "days": days,
    }


def unread_count(db: Session, days: int = SPARK_DAYS, now: datetime | None = None) -> int:
    """Drops from the last `days` days that were never opened, ✓-marked or voted on."""
    since = (now or utcnow()) - timedelta(days=days)
    return (
        db.query(func.count(Drop.id))
        .join(Book, Book.id == Drop.book_id)
        .filter(Drop.acknowledged_at.is_(None), Drop.published_at >= since,
                Book.status == BookStatus.active)
        .scalar()
        or 0
    )


def attention_items(
    db: Session,
    *,
    book_notes: dict[int, str],
    last_skips: list[dict],
    fetch_progress: list[dict],
    push_bad: int,
    push_orphans: int,
    last_run: dict | None,
    schedules_enabled: int,
    cycle_running: bool,
    releases_paused: datetime | None = None,
) -> list[dict]:
    """Things that need a human, most important first. Each: tone (bad|warn), text, href?.

    Deliberately short: a healthy system returns [].
    """
    items: list[dict] = []

    if last_run and last_run.get("error") and not cycle_running:
        what = "Release" if last_run.get("op") == "release" else "Feed check"
        items.append({"tone": "bad", "text": f"{what} failed: {last_run['error']}", "href": None})

    if releases_paused is not None:
        items.append({"tone": "warn", "text": "Scheduled releases are paused — nothing is fetched or "
                                              "released until you resume",
                      "href": None})

    if schedules_enabled == 0:
        items.append({"tone": "bad", "text": "No release schedule is enabled — nothing will release",
                      "href": "/admin/schedules"})

    live = db.query(Book).filter(Book.status.in_([BookStatus.active, BookStatus.queued]))
    by_id = {b.id: b for b in live}

    for book_id, note in book_notes.items():
        b = by_id.get(book_id)
        if b is not None:
            items.append({"tone": "warn", "text": f"{b.title}: {note}", "href": f"/admin/books/{b.id}"})

    for b in by_id.values():
        if (b.tracked and not b.paused and (b.last_fetch_status or "").startswith("error")
                and b.id not in book_notes):
            msg = (b.last_fetch_status or "").removeprefix("error:").strip()
            items.append({"tone": "warn", "text": f"{b.title}: fetch failed — {msg}"[:160],
                          "href": f"/admin/books/{b.id}"})

    from app.models import AppState
    from app.state import FETCH_GIVEUP_PREFIX
    for row in db.query(AppState).filter(AppState.key.like(FETCH_GIVEUP_PREFIX + "%")):
        try:
            bid = int(row.key.removeprefix(FETCH_GIVEUP_PREFIX))
        except ValueError:
            continue
        b = by_id.get(bid)
        if b is not None and not b.paused and bid not in book_notes:
            items.append({"tone": "warn",
                          "text": f"{b.title}: gave up on a new feed entry — repeated fetches found no new "
                                  "chapters (the story may be behind its feed). Try Fetch now."[:200],
                          "href": f"/admin/books/{b.id}"})

    missing = {e.get("book_id") for e in last_skips if e.get("missing")}
    for bid in missing:
        b = by_id.get(bid)
        if b is not None and bid not in book_notes:
            items.append({"tone": "warn", "text": f"{b.title}: not found in Calibre",
                          "href": f"/admin/books/{b.id}"})

    for f in fetch_progress:
        if f.get("elapsed") and f["elapsed"] > STUCK_DOWNLOAD_SECONDS:
            b = f["book"]
            items.append({"tone": "warn",
                          "text": f"{b.title}: download running {f['elapsed'] // 60} min",
                          "href": f"/admin/books/{b.id}"})

    if push_bad:
        items.append({"tone": "warn",
                      "text": f"{push_bad} reader subscription(s) unverified or expired — "
                              "those feeds poll instead of getting pushes",
                      "href": None})
    if push_orphans:
        items.append({"tone": "warn",
                      "text": f"{push_orphans} subscription(s) on feeds that no longer exist",
                      "href": None})
    return items
