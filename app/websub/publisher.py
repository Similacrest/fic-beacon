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
from app.feed.builder import build_feed
from app.models import Channel, Config, Drop, WebSubSubscription, utcnow

logger = logging.getLogger(__name__)


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


# ── feed builders (mirror app/routers/feed.py) ────────────────────────────────


def _recent_drops(query):
    return query.order_by(Drop.published_at.desc()).limit(settings.feed_item_limit).all()


def _channel_slot_feed(session: Session, channel_id: int, feed_key: str) -> tuple[str, bytes] | None:
    channel = session.get(Channel, channel_id)
    if channel is None:
        return None
    drops = _recent_drops(
        session.query(Drop)
        .join(Drop.book)
        .filter(Drop.channel_id == channel_id, Drop.feed_key == feed_key)
    )
    slot_label = f"Slot {feed_key}"
    cfg = session.get(Config, 1)
    secret = cfg.feed_secret if cfg else settings.feed_secret
    # Tokened to mirror the advertised rel=self (app/routers/feed.py) so the topic is
    # fetchable and the pushed body is byte-identical to a GET of the topic URL.
    topic = f"{settings.base_url}/feed/{channel.slug}/{feed_key}?token={secret}"
    # title/description must mirror app/routers/feed.py exactly so the pushed body is
    # byte-identical to a GET of the topic (WebSub compares the two).
    atom, _ = build_feed(
        drops,
        self_url=topic,
        title=f"Fic Beacon — {channel.name} · {slot_label}",
        description=f"{channel.name} — {slot_label}",
    )
    return topic, atom


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
    for sub in subs:
        exp = sub.lease_expires_at
        if exp is not None:
            if exp.tzinfo is None:  # SQLite returns naive datetimes; treat as UTC
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                continue
        try:
            _post(sub, topic_url, atom_bytes)
        except httpx.HTTPError as exc:
            logger.warning("WebSub push to %s failed: %s", sub.callback_url, exc)


def _post(sub: WebSubSubscription, topic_url: str, atom_bytes: bytes) -> None:
    hub_url = f"{settings.base_url}/websub/hub"
    headers = {
        "Content-Type": "application/atom+xml",
        "Link": f'<{hub_url}>; rel="hub", <{topic_url}>; rel="self"',
    }
    if sub.secret:
        sig = hmac.new(sub.secret.encode(), atom_bytes, hashlib.sha1).hexdigest()
        headers["X-Hub-Signature"] = f"sha1={sig}"
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        client.post(sub.callback_url, content=atom_bytes, headers=headers)
