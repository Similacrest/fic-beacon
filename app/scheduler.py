"""APScheduler wiring.

Runs in-process with the FastAPI app (single worker only — see docker-compose.yml).
The cron schedule is read from Config at startup and can be updated via the admin UI.

Two recurring jobs:
  - drop_cycle: fires on cadence_cron. Polls tracked feeds first (triggering fetches for
    any with new chapters), then materialises chapter drops from the current EPUB state.
  - feedless_sweep: once a day, fetches tracked stories that have no RSS feed (auth-gated),
    since they have no notification signal of their own.
"""
import logging
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


def _run_cycle() -> None:
    from app.ongoing.poller import poll_all_feeds
    from app.planner.planner import run_drop_cycle
    from app.websub.publisher import publish_updates
    with db_session() as session:
        # Poll tracked feeds first: any with a new chapter are fetched into Calibre now,
        # so this broadcast reads the freshest EPUB state.
        poll_all_feeds(session)
        drops = run_drop_cycle(session, settings.calibre_library_path)
        session.commit()
        publish_updates(session, drops)
    logger.info("Drop cycle complete — %d drop(s) created.", len(drops))


def _run_feedless_sweep() -> None:
    from app.ongoing.poller import sweep_feedless
    with db_session() as session:
        fetched = sweep_feedless(session)
        session.commit()
    logger.info("Feedless sweep complete — %d source(s) fetched.", fetched)


def _run_fetch_pending() -> None:
    from app.ongoing.poller import fetch_pending
    with db_session() as session:
        fetched = fetch_pending(session)
        session.commit()
    logger.info("Pending fetch complete — %d source(s) downloaded.", fetched)


def trigger_fetch_pending() -> None:
    """Background-fetch newly-added tracked stories (initial download).

    Returns immediately so the admin request isn't blocked on slow FanFicFare downloads.
    Falls back to running inline if the scheduler isn't started (e.g. tests).
    """
    if not _scheduler.running:
        _run_fetch_pending()
        return
    from datetime import datetime, timedelta
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
