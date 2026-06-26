"""APScheduler wiring.

Runs in-process with the FastAPI app (single worker only — see docker-compose.yml).
The cron schedule is read from Config at startup and can be updated via the admin UI.

Recurring jobs:
  - drop_cycle: fires on cadence_cron. Polls tracked feeds first (submitting async fetches
    for any with new chapters), then materialises chapter drops from the *current* EPUB
    state. It does NOT wait for fetches — a 15-minute FanFicFare run can't block the
    broadcast, so freshly fetched chapters land in the next cycle.
  - feedless_sweep: once a day, fetches tracked stories that have no RSS feed (auth-gated).

Async fetch lifecycle: a fetch is *submitted* to the fetcher (which returns a job_id and
works in the background); a transient per-job `fetch_poll_{id}` interval job polls until the
job is done, then folds each result into its Book row. The job→book mapping is persisted in
app_state so a restart mid-fetch resumes polling.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import db_session

logger = logging.getLogger(__name__)


def _timezone() -> ZoneInfo | None:
    """Resolve BEACON_TZ to a ZoneInfo, or None to let APScheduler use the local tz."""
    if not settings.tz:
        return None
    try:
        return ZoneInfo(settings.tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Invalid BEACON_TZ %r — falling back to local timezone", settings.tz)
        return None


_scheduler = BackgroundScheduler()


# ── Async fetch orchestration ────────────────────────────────────────────────────────────

def submit_and_track(session, books) -> str | None:
    """Submit a batch fetch for `books` and schedule polling. Returns the job_id, or None.

    Marks each book `fetching…`, persists the job→book mapping in app_state, and registers a
    `fetch_poll_{job_id}` interval job that folds results when the fetcher finishes. The
    caller commits the session (the persisted mapping must be durable before the poll fires).
    """
    from app.fetch.client import submit_fetch
    from app.models import utcnow
    from app.state import FETCH_JOB_PREFIX, set_value

    books = [b for b in books if b.source_url]
    if not books:
        return None
    job_id = submit_fetch([b.source_url for b in books])
    if job_id is None:
        for b in books:
            b.last_fetch_status = "error: could not reach fetcher"
        return None

    now = utcnow()
    for b in books:
        b.last_fetch_status = "fetching…"
        b.last_fetch_at = now  # submit time → drives the dashboard "elapsed" display
    set_value(session, FETCH_JOB_PREFIX + job_id, json.dumps({
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "url_to_book": {b.source_url: b.id for b in books},
    }))
    _schedule_poll(job_id)
    return job_id


def _schedule_poll(job_id: str) -> None:
    if not _scheduler.running:
        return
    _scheduler.add_job(
        _poll_fetch_job,
        trigger="interval",
        seconds=settings.fetcher_poll_interval,
        next_run_time=datetime.now() + timedelta(seconds=settings.fetcher_poll_interval),
        id=f"fetch_poll_{job_id}",
        args=[job_id],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def _unschedule_poll(job_id: str) -> None:
    try:
        _scheduler.remove_job(f"fetch_poll_{job_id}")
    except Exception:
        pass


def _poll_fetch_job(job_id: str) -> None:
    """Poll one in-flight fetch job; fold results when done, give up after the timeout."""
    from app.fetch.client import apply_result, poll_fetch
    from app.state import FETCH_JOB_PREFIX, delete_value, get_value
    from app.models import Book

    with db_session() as session:
        raw = get_value(session, FETCH_JOB_PREFIX + job_id)
        if raw is None:  # mapping gone (already handled) — stop polling
            _unschedule_poll(job_id)
            return
        meta = json.loads(raw)
        url_to_book = meta.get("url_to_book", {})

        status = poll_fetch(job_id)
        if status is None:  # transient HTTP error — try again next interval
            if _expired(meta):
                _abandon(session, url_to_book, "fetcher unreachable")
                delete_value(session, FETCH_JOB_PREFIX + job_id)
                session.commit()
                _unschedule_poll(job_id)
            return

        results = status.get("results") or []
        by_url = {r.get("url"): r for r in results}

        if status.get("status") == "running":
            for url, book_id in url_to_book.items():  # live phase for the dashboard
                book = session.get(Book, book_id)
                phase = (by_url.get(url) or {}).get("phase")
                if book is not None and phase and phase not in ("done", "error"):
                    book.last_fetch_status = f"fetching: {phase}"
            if _expired(meta):
                _abandon(session, url_to_book, "fetch timed out")
                delete_value(session, FETCH_JOB_PREFIX + job_id)
                session.commit()
                _unschedule_poll(job_id)
            else:
                session.commit()
            return

        # done (or unknown — job vanished): fold whatever we have, error the rest.
        for url, book_id in url_to_book.items():
            book = session.get(Book, book_id)
            if book is None:
                continue
            raw_result = by_url.get(url)
            if raw_result is None:
                book.last_fetch_status = "error: no result from fetcher"
            else:
                apply_result(book, raw_result)
        delete_value(session, FETCH_JOB_PREFIX + job_id)
        session.commit()
    _unschedule_poll(job_id)


def _expired(meta: dict) -> bool:
    try:
        submitted = datetime.fromisoformat(meta["submitted_at"])
    except (KeyError, ValueError):
        return True
    return datetime.now(timezone.utc) - submitted > timedelta(seconds=settings.fetcher_job_timeout)


def _abandon(session, url_to_book: dict, reason: str) -> None:
    from app.models import Book
    for book_id in url_to_book.values():
        book = session.get(Book, book_id)
        if book is not None:
            book.last_fetch_status = f"error: {reason}"


def _resume_pending_polls() -> None:
    """Re-register poll jobs for fetches that were in flight when the app last stopped."""
    from app.state import FETCH_JOB_PREFIX, list_with_prefix
    with db_session() as session:
        for key, _ in list_with_prefix(session, FETCH_JOB_PREFIX):
            _schedule_poll(key[len(FETCH_JOB_PREFIX):])


# ── Recurring jobs ───────────────────────────────────────────────────────────────────────

def _run_cycle() -> None:
    from app.ongoing.poller import poll_all_feeds
    from app.planner.planner import run_drop_cycle
    from app.websub.publisher import publish_updates
    with db_session() as session:
        # Poll tracked feeds first: any with a new chapter has an async fetch submitted now.
        # We do NOT wait for it — broadcast the current EPUB state; new chapters land next cycle.
        poll_all_feeds(session)
        drops = run_drop_cycle(session, settings.calibre_library_path)
        session.commit()
        publish_updates(session, drops)
    logger.info("Drop cycle complete — %d drop(s) created.", len(drops))


def _run_feedless_sweep() -> None:
    from app.ongoing.poller import sweep_feedless
    with db_session() as session:
        submitted = sweep_feedless(session)
        session.commit()
    logger.info("Feedless sweep complete — fetch submitted for %d source(s).", submitted)


def _run_fetch_pending() -> None:
    from app.ongoing.poller import fetch_pending
    with db_session() as session:
        submitted = fetch_pending(session)
        session.commit()
    logger.info("Pending fetch submitted for %d source(s).", submitted)


def trigger_fetch_pending() -> None:
    """Submit fetches for newly-added tracked stories (initial download), off the request path.

    Runs inline if the scheduler isn't started (e.g. tests) so the submit still happens.
    """
    if not _scheduler.running:
        _run_fetch_pending()
        return
    _scheduler.add_job(
        _run_fetch_pending,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=1),
        id="fetch_pending",
        replace_existing=True,
    )


def start(cadence_cron: str) -> None:
    """Start the scheduler with the drop-cycle cron and the daily feedless sweep."""
    tz = _timezone()
    if tz is not None:
        _scheduler.configure(timezone=tz)
    _scheduler.add_job(
        _run_cycle,
        trigger=CronTrigger.from_crontab(cadence_cron, timezone=tz),
        id="drop_cycle",
        replace_existing=True,
        misfire_grace_time=300,
    )
    _scheduler.add_job(
        _run_feedless_sweep,
        trigger=CronTrigger.from_crontab("0 4 * * *", timezone=tz),  # 04:00 daily
        id="feedless_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    if not _scheduler.running:
        _scheduler.start()
    _resume_pending_polls()


def update_cadence(cadence_cron: str) -> None:
    """Reschedule the drop cycle with a new cron expression (called after config save)."""
    _scheduler.reschedule_job(
        "drop_cycle",
        trigger=CronTrigger.from_crontab(cadence_cron, timezone=_timezone()),
    )


def next_run_times() -> dict[str, object]:
    """Next scheduled fire time per job (None if not scheduled), for the dashboard."""
    out: dict[str, object] = {}
    for job_id in ("drop_cycle", "feedless_sweep"):
        job = _scheduler.get_job(job_id) if _scheduler.running else None
        out[job_id] = job.next_run_time if job else None
    return out


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
