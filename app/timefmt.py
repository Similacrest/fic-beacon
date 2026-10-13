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


def local_tz():
    """The scheduling timezone (`BEACON_TZ`), else UTC."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from app.config import settings
    try:
        return ZoneInfo(settings.tz) if settings.tz else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def absolute(dt: datetime | None) -> str:
    """Full timestamp in the scheduling timezone (BEACON_TZ, else UTC), labelled."""
    if dt is None:
        return ""
    from app.config import settings
    local = _aware(dt).astimezone(local_tz())
    return local.strftime("%Y-%m-%d %H:%M ") + (settings.tz or "UTC")


def compact(n: float | int | None) -> str:
    """1234 → "1.2k", 42300 → "42k", 1500000 → "1.5M"; small numbers unchanged."""
    if n is None:
        return "0"
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(n) >= 10_000:
        return f"{n / 1000:.0f}k"
    if abs(n) >= 1_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{int(n)}"


def duration(seconds: int | None) -> str:
    """75 → "1m 15s"; 12 → "12s"; 3700 → "1h 1m"."""
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"
