"""Poll ongoing RSS/Atom feeds and estimate recent word volume.

Called by the scheduler every few hours so that the drop planner always has a
fresh estimate of how many words the user's real ongoing fics are delivering per
cycle.  The estimate is stored in OngoingFeed.estimated_words_per_cycle and
subtracted from Config.target_total_words to compute the synthetic drop budget.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

import feedparser
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.models import OngoingFeed

# Default look-back window: how far into the past we count entries as "recent".
# Matches a daily drop cadence; callers can override.
_DEFAULT_WINDOW_HOURS = 24


def poll_feed(feed_url: str, window_hours: int = _DEFAULT_WINDOW_HOURS) -> int:
    """Fetch feed_url and return word count of entries published within window_hours."""
    parsed = feedparser.parse(feed_url)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total = 0
    for entry in parsed.entries:
        pub = _entry_published(entry)
        if pub is not None and pub < cutoff:
            continue  # older than the window
        total += _entry_word_count(entry)
    return total


def poll_all_feeds(session: Session, window_hours: int = _DEFAULT_WINDOW_HOURS) -> None:
    """Poll every active OngoingFeed and update its estimated_words_per_cycle."""
    feeds = session.query(OngoingFeed).filter(OngoingFeed.is_active.is_(True)).all()
    for feed in feeds:
        try:
            words = poll_feed(feed.feed_url, window_hours=window_hours)
        except Exception:
            words = feed.estimated_words_per_cycle  # keep last known value on error
        feed.estimated_words_per_cycle = words
        feed.last_polled_at = datetime.now(timezone.utc)
    session.flush()


# ── helpers ──────────────────────────────────────────────────────────────────


def _entry_published(entry) -> datetime | None:
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if t is None:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _entry_word_count(entry) -> int:
    content_list = getattr(entry, "content", None)
    if content_list:
        raw = content_list[0].get("value", "")
    else:
        raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    text = BeautifulSoup(raw, "lxml").get_text(" ")
    return len(re.findall(r"\S+", text))
