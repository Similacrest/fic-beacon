"""Push feed updates to WebSub subscribers after new drops.

Called after a drop cycle / extra drop commits. For each channel slot feed that changed
it POSTs the fresh Atom body to every verified, unexpired subscriber of that topic.
Best-effort: failures are logged, never raised, so a slow subscriber can't break the
drop cycle.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timezone

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.feed.builder import build_feed, slot_feed_drops, slot_feed_meta
from app.models import Channel, Config, Drop, WebSubSubscription, utcnow

logger = logging.getLogger(__name__)

# Rough per-entry envelope (feedback links, <entry> wrapper, permalinks) added on top of a
# drop's stored content_html when estimating a push body's size. Deliberately generous so we
# stay comfortably under the budget rather than overshoot it.
_ENTRY_OVERHEAD = 2_000
# The feed-level <feed>/author/link envelope, counted once.
_FEED_OVERHEAD = 1_500


def publish_updates(session: Session, drops: list[Drop]) -> None:
    """Notify subscribers of every channel/slot feed touched by `drops`."""
    if not drops:
        return
    seen: set[tuple[int, str]] = set()
    for drop in drops:
        if drop.channel_id is None or drop.feed_key is None:
            continue
        key = (drop.channel_id, drop.feed_key)
        if key in seen:
            continue
        seen.add(key)
        built = _channel_slot_feed(session, drop.channel_id, drop.feed_key)
        if built:
            _notify_topic(session, *built)
    logger.debug("WebSub publish: %d drop(s) touched %d slot feed(s)", len(drops), len(seen))


def _channel_slot_feed(session: Session, channel_id: int, feed_key: str) -> tuple[str, bytes] | None:
    channel = session.get(Channel, channel_id)
    if channel is None:
        return None
    cfg = session.get(Config, 1)
    secret = cfg.feed_secret if cfg else settings.feed_secret
    max_bytes = cfg.websub_max_push_bytes if cfg else 100_000
    # The push body is *trimmed to the newest drops that fit `max_bytes`* rather than the whole
    # feed_item_limit: a full slot feed can be ~1.6 MB and Inoreader silently drops oversized fat
    # pings (200-acked, never ingested → realtime dies). This diverges from a GET of the topic —
    # that's fine, readers merge the pushed newest items into the polled feed by GUID.
    self_url, title, description = slot_feed_meta(channel, feed_key, secret)
    drops = slot_feed_drops(session, channel, feed_key)
    drops = _trim_to_budget(drops, max_bytes)
    atom, _ = build_feed(drops, self_url=self_url, title=title, description=description)
    return self_url, atom


def _trim_to_budget(drops: list[Drop], max_bytes: int) -> list[Drop]:
    """Keep the newest drops whose estimated bytes fit `max_bytes`, always keeping ≥1 (a single
    oversized chapter is pushed whole — we never split a unit). `drops` is newest-first.
    `max_bytes <= 0` disables trimming (push the whole feed)."""
    if max_bytes <= 0:
        return drops
    chosen: list[Drop] = []
    total = _FEED_OVERHEAD
    for drop in drops:
        size = len(drop.content_html or "") + _ENTRY_OVERHEAD
        if chosen and total + size > max_bytes:
            break
        chosen.append(drop)
        total += size
    return chosen


# ── delivery ──────────────────────────────────────────────────────────────────


def _notify_topic(session: Session, topic_url: str, atom_bytes: bytes) -> None:
    now = utcnow()
    # Our advertised rel=self (and thus topic_url here) is tokened, but a subscriber may
    # have registered either the bare token-free URL or a ?token=… poll URL. Match every
    # form sharing the token-free base so realtime push isn't silently dropped.
    base = topic_url.split("?", 1)[0]
    subs = (
        session.query(WebSubSubscription)
        .filter(
            or_(
                WebSubSubscription.topic_url == base,
                WebSubSubscription.topic_url.like(base + "?%"),
            ),
            WebSubSubscription.verified.is_(True),
        )
        .all()
    )
    logger.debug(
        "WebSub push: topic=%s (%d bytes) → %d verified subscriber(s)",
        topic_url, len(atom_bytes), len(subs),
    )
    for sub in subs:
        exp = sub.lease_expires_at
        if exp is not None:
            if exp.tzinfo is None:  # SQLite returns naive datetimes; treat as UTC
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                logger.debug("WebSub push: skip expired subscriber %s (lease %s)", sub.callback_url, exp)
                continue
        try:
            _post(sub, topic_url, atom_bytes)
            logger.debug("WebSub push → %s ok", sub.callback_url)
        except httpx.HTTPError as exc:
            logger.warning("WebSub push to %s failed: %s", sub.callback_url, exc)


def _post(sub: WebSubSubscription, topic_url: str, atom_bytes: bytes) -> None:
    hub_url = f"{settings.base_url}/websub/hub"
    headers = {
        # Must match the topic's Content-Type exactly, charset included — WebSub
        # validators compare them (see app/routers/feed.py:_render).
        "Content-Type": "application/atom+xml; charset=utf-8",
        "Link": f'<{hub_url}>; rel="hub", <{topic_url}>; rel="self"',
    }
    if sub.secret:
        sig = hmac.new(sub.secret.encode(), atom_bytes, hashlib.sha1).hexdigest()
        headers["X-Hub-Signature"] = f"sha1={sig}"
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        client.post(sub.callback_url, content=atom_bytes, headers=headers)
