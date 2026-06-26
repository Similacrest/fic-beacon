"""Helpers for the AppState key/value runtime store (last cron run times, skip log)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AppState, utcnow

LAST_DROP_RUN = "last_drop_run_at"
LAST_POLL_RUN = "last_poll_run_at"
LAST_SKIPS = "last_broadcast_skips"  # JSON: sources held out / partly deferred last broadcast
FETCH_JOB_PREFIX = "fetch_job:"  # + job_id → JSON {submitted_at, url_to_book} for in-flight fetches


def set_value(session: Session, key: str, value: str) -> None:
    """Upsert a raw string value for `key`."""
    row = session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value


def get_value(session: Session, key: str) -> str | None:
    row = session.get(AppState, key)
    return row.value if row else None


def delete_value(session: Session, key: str) -> None:
    row = session.get(AppState, key)
    if row is not None:
        session.delete(row)


def list_with_prefix(session: Session, prefix: str) -> list[tuple[str, str]]:
    """All (key, value) AppState rows whose key starts with `prefix`."""
    rows = session.query(AppState).filter(AppState.key.like(f"{prefix}%")).all()
    return [(r.key, r.value) for r in rows]


def mark_run(session: Session, key: str) -> None:
    """Stamp `key` with the current UTC time."""
    set_value(session, key, utcnow().isoformat())


def get_run(session: Session, key: str) -> datetime | None:
    """Return the stored timestamp for `key` as a tz-aware datetime, or None."""
    value = get_value(session, key)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
