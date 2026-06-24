"""Infer an RSS/Atom feed URL from a story's canonical web URL.

Supported sites:
  - RoyalRoad:         /fiction/{id}[/...]       → /syndication/{id}
  - XenForo forums
    (SpaceBattles, SufficientVelocity):
                       /threads/{slug}/           → /threads/{slug}/threadmarks.rss
"""
from __future__ import annotations

import re

_ROYALROAD_RE = re.compile(
    r"https?://(?:www\.)?royalroad\.com/fiction/(\d+)", re.IGNORECASE
)
_XENFORO_RE = re.compile(
    r"(https?://forums\.(?:spacebattles|sufficientvelocity)\.com/threads/[^/?#]+)",
    re.IGNORECASE,
)


def infer_feed_url(source_url: str | None) -> str | None:
    """Return the feed URL for a known story URL, or None if unrecognised."""
    if not source_url:
        return None
    m = _ROYALROAD_RE.match(source_url)
    if m:
        return f"https://www.royalroad.com/syndication/{m.group(1)}"
    m = _XENFORO_RE.match(source_url)
    if m:
        return m.group(1).rstrip("/") + "/threadmarks.rss"
    return None
