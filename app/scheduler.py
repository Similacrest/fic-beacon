"""APScheduler wiring.

Runs in-process with the FastAPI app (single worker only — see docker-compose.yml).
The cron schedule is read from Config at startup and can be updated via the admin UI.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import db_session
from app.models import Config

_scheduler = BackgroundScheduler()


def _run_cycle() -> None:
    from app.planner.planner import run_drop_cycle
    with db_session() as session:
        drops = run_drop_cycle(session, settings.calibre_library_path)
        session.commit()
    print(f"[beacon] Drop cycle complete — {len(drops)} drop(s) created.")


def start(cadence_cron: str) -> None:
    """Start the scheduler with the given cron expression."""
    _scheduler.add_job(
        _run_cycle,
        trigger=CronTrigger.from_crontab(cadence_cron),
        id="drop_cycle",
        replace_existing=True,
        misfire_grace_time=300,
    )
    if not _scheduler.running:
        _scheduler.start()


def update_cadence(cadence_cron: str) -> None:
    """Reschedule the drop cycle with a new cron expression (called after config save)."""
    _scheduler.reschedule_job(
        "drop_cycle",
        trigger=CronTrigger.from_crontab(cadence_cron),
    )


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
