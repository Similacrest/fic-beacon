"""Short relative times for the admin UI ("3h ago", "in 2d"); the full timestamp goes in a tooltip."""
from __future__ import annotations

from datetime import datetime, timezone


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tz → stored as UTC


def relative(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    secs = int((_aware(dt) - _aware(now)).total_seconds())
    future, secs = secs > 0, abs(secs)
    if secs < 60:
        return "just now" if not future else "in <1m"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            n = secs // size
            break
    text = f"{n}{unit}"
    return f"in {text}" if future else f"{text} ago"


def absolute(dt: datetime | None) -> str:
    """Full timestamp in the scheduling timezone (BEACON_TZ, else UTC), labelled."""
    if dt is None:
        return ""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from app.config import settings
    try:
        tz = ZoneInfo(settings.tz) if settings.tz else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    local = _aware(dt).astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M ") + (settings.tz or "UTC")
