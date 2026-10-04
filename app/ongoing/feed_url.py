"""Infer an RSS/Atom feed URL from a story's canonical web URL.

Supported sites:
  - RoyalRoad:         /fiction/{id}[/...]       → /syndication/{id}
  - XenForo forums
    (SpaceBattles, SufficientVelocity):
                       /threads/{slug}/           → /threads/{slug}/threadmarks.rss
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_ROYALROAD_RE = re.compile(
    r"https?://(?:www\.)?royalroad\.com/fiction/(\d+)", re.IGNORECASE
)
_XENFORO_RE = re.compile(
    r"(https?://forums\.(?:spacebattles|sufficientvelocity)\.com/threads/[^/?#]+)",
    re.IGNORECASE,
)


def is_fetchable_url(url: str | None) -> bool:
    """True for a plain http(s) URL with a host. Story URLs reach FanFicFare's argv, so anything
    else (notably a value starting with `-`, which would parse as an option) is refused."""
    if not url or url != url.strip() or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


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
