"""APScheduler wiring.

Runs in-process with the FastAPI app (single worker only — see docker-compose.yml).
The cron schedule is read from Config at startup and can be updated via the admin UI.

Two recurring jobs:
  - drop_cycle: fires on cadence_cron to materialise chapter drops.
  - poll_ongoing: polls ongoing serial feeds hourly, buffering new chapters for release
    at the next drop cycle.
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
        # Always poll ongoing feeds first so a broadcast releases the freshest chapters.
        poll_all_feeds(session)
        drops = run_drop_cycle(session, settings.calibre_library_path)
        session.commit()
        publish_updates(session, drops)
    logger.info("Drop cycle complete — %d drop(s) created.", len(drops))


def _run_poll() -> None:
    from app.ongoing.poller import poll_all_feeds
    with db_session() as session:
        poll_all_feeds(session)
        session.commit()
    logger.info("Ongoing feed poll complete.")


def start(cadence_cron: str) -> None:
    """Start the scheduler with the drop-cycle cron and the ongoing-feed poll interval."""
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
        _run_poll,
        trigger="interval",
        hours=1,
        id="poll_ongoing",
        replace_existing=True,
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
    for job_id in ("drop_cycle", "poll_ongoing"):
        job = _scheduler.get_job(job_id) if _scheduler.running else None
        out[job_id] = job.next_run_time if job else None
    return out


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
