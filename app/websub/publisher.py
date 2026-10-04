"""Push feed updates to WebSub subscribers after new drops.

Called after a release cycle / extra drop commits. For each channel slot feed that changed
it POSTs the fresh Atom body to every verified, unexpired subscriber of that topic.
Best-effort: failures are logged, never raised, so a slow subscriber can't break the
release cycle.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from datetime import timezone

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.feed.builder import build_feed, slot_feed_drops, slot_feed_meta
from app.models import Channel, Config, Drop, WebSubSubscription, utcnow
from app.security import current_feed_secret
from app.websub import safety

logger = logging.getLogger(__name__)

# Rough per-entry envelope (feedback links, <entry> wrapper, permalinks) added on top of a
# drop's stored content_html when estimating a push body's size. Deliberately generous so we
# stay comfortably under the budget rather than overshoot it.
_ENTRY_OVERHEAD = 2_000
# The feed-level <feed>/author/link envelope, counted once.
_FEED_OVERHEAD = 1_500


def publish_in_background(drops: list[Drop]) -> None:
    """Push `drops`' feeds to subscribers on a worker thread (own DB session).

    Delivery is synchronous HTTP with up-to-10s timeouts per subscriber; doing it inline would hold
    the release-cycle lock (and, for 🪝, the user's request) while slow subscribers time out.
    Call after the drops are committed.
    """
    ids = [d.id for d in drops if d.id is not None]
    if ids:
        threading.Thread(target=_publish_ids, args=(ids,), name="websub-push", daemon=True).start()


def _publish_ids(ids: list[int]) -> None:
    from app.database import db_session
    try:
        with db_session() as session:
            publish_updates(session, session.query(Drop).filter(Drop.id.in_(ids)).all())
    except Exception:  # best-effort: never let a push failure surface anywhere
        logger.exception("Background WebSub push failed")


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
    atom, _ = build_feed(
        drops, self_url=self_url, title=title, description=description, secret=secret
    )
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
    # Only subscriptions whose stored topic carries the *current* feed secret receive content
    # (the hub enforces this at subscribe time; re-checking here covers rotated secrets and
    # legacy token-free rows). The SQL filter just narrows to this feed's path.
    secret = current_feed_secret(session)
    base = topic_url.split("?", 1)[0]
    candidates = (
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
    subs = [
        s for s in candidates
        if s.topic_url.split("?", 1)[0] == base and safety.topic_status(s.topic_url, secret) == "ok"
    ]
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
        except (httpx.HTTPError, safety.UnsafeCallback) as exc:
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
    # Re-validate on every push (DNS may have changed since subscribe), connect to the address
    # we validated, never follow redirects, and don't read the response body.
    url = safety.parse_callback(sub.callback_url)
    pinned, host_headers, extensions = safety.pin(url, safety.public_addresses(url)[0])
    with httpx.Client(timeout=safety.TIMEOUT, follow_redirects=False) as client:
        with client.stream(
            "POST", pinned, content=atom_bytes, headers={**headers, **host_headers},
            extensions=extensions,
        ):
            pass
