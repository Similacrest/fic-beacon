"""Self-hosted WebSub (PubSubHubbub) hub for realtime feed push.

Our feeds advertise <link rel="hub" href="{base}/websub/hub">. A subscriber's hub
(e.g. InoReader) POSTs a subscribe request here; we verify intent by GETting the
callback with a challenge, then store the subscription. On each new drop the publisher
(app/websub/publisher.py) POSTs the Atom body to verified subscribers.

WebSub is a W3C standard and degrades gracefully — readers without it just keep polling.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import db_session
from app.models import WebSubSubscription, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/websub")

_DEFAULT_LEASE = 10 * 24 * 3600  # 10 days when the subscriber doesn't specify one
# Backoff before/between verification callbacks. A subscriber (e.g. Inoreader) arms its
# verification endpoint only after it receives our 202, so the very first callback can
# race ahead of that. Retry a few times before giving up.
_VERIFY_DELAYS = (0.0, 2.0, 5.0)


def _is_own_topic(topic: str) -> bool:
    return bool(topic) and topic.startswith(f"{settings.base_url}/feed")


def _get_sub(db: Session, topic: str, callback: str) -> WebSubSubscription | None:
    return (
        db.query(WebSubSubscription)
        .filter(
            WebSubSubscription.topic_url == topic,
            WebSubSubscription.callback_url == callback,
        )
        .first()
    )


async def _verify_intent(callback: str, mode: str, topic: str, challenge: str, lease: int) -> bool:
    """WebSub verification of intent: GET the callback; it must echo the challenge."""
    params = {
        "hub.mode": mode,
        "hub.topic": topic,
        "hub.challenge": challenge,
        "hub.lease_seconds": str(lease),
    }
    logger.debug("WebSub verify → GET %s params=%s", callback, params)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(callback, params=params)
    except httpx.HTTPError as exc:
        logger.debug("WebSub verify GET to %s errored: %r", callback, exc)
        return False
    body = resp.text
    logger.debug(
        "WebSub verify ← %s status=%s len=%d echo=%s",
        callback, resp.status_code, len(body), challenge in body,
    )
    if resp.status_code // 100 != 2:
        logger.debug("WebSub verify GET to %s returned non-2xx %s", callback, resp.status_code)
        return False
    if challenge not in body:
        logger.debug(
            "WebSub verify callback %s did not echo the challenge (body[:200]=%r)",
            callback, body[:200],
        )
        return False
    return True


@router.post("/hub")
async def hub(request: Request, background: BackgroundTasks) -> Response:
    """Accept a (un)subscribe request and verify intent asynchronously.

    WebSub requires the hub to return 202 *immediately* and verify intent out of band
    (spec §5.3). Subscribers like Inoreader only arm their verification callback once
    they've received this 202, so a synchronous verify-before-respond would race and
    get rejected (the 409s we saw in the logs). We validate what we can up front (bad
    mode / foreign topic → 4xx now) and defer the callback round-trip + DB write.
    """
    form = await request.form()
    mode = form.get("hub.mode")
    topic = form.get("hub.topic")
    callback = form.get("hub.callback")
    lease_raw = form.get("hub.lease_seconds")
    has_secret = bool(form.get("hub.secret"))
    logger.debug(
        "WebSub hub ← mode=%r topic=%r callback=%r lease=%r secret=%s",
        mode, topic, callback, lease_raw, has_secret,
    )
    if mode not in ("subscribe", "unsubscribe") or not topic or not callback:
        logger.debug("WebSub hub → 400 (mode/topic/callback invalid)")
        raise HTTPException(status_code=400, detail="invalid hub request")
    if not _is_own_topic(topic):
        logger.debug("WebSub hub → 404 (foreign topic %r)", topic)
        raise HTTPException(status_code=404, detail="unknown topic")

    lease = int(lease_raw or 0) or _DEFAULT_LEASE
    logger.debug(
        "WebSub hub → 202; scheduled verify (mode=%s lease=%ds) for %s", mode, lease, callback
    )
    background.add_task(
        _verify_and_store, mode, topic, callback, lease, form.get("hub.secret")
    )
    return Response(status_code=202)


async def _verify_and_store(
    mode: str, topic: str, callback: str, lease: int, secret: str | None
) -> None:
    """Verify intent against the subscriber's callback, then persist (own DB session).

    Runs after the 202 has been sent. Verification is retried with backoff to absorb the
    arming race (a subscriber may not be ready for our callback the instant it gets the
    202). A still-failing verification is logged and dropped — there's no live request to
    return an error to.
    """
    challenge = secrets.token_urlsafe(32)
    for attempt, delay in enumerate(_VERIFY_DELAYS, start=1):
        if delay:
            logger.debug(
                "WebSub verify backoff %.1fs before attempt %d for %s", delay, attempt, callback
            )
            await asyncio.sleep(delay)
        logger.debug(
            "WebSub verify attempt %d/%d (%s) for %s", attempt, len(_VERIFY_DELAYS), mode, callback
        )
        if await _verify_intent(callback, mode, topic, challenge, lease):
            with db_session() as db:
                _store_subscription(db, mode, topic, callback, lease, secret)
                db.commit()
            logger.info("WebSub %s verified for %s (attempt %d)", mode, callback, attempt)
            return
    logger.warning(
        "WebSub %s verification failed for callback %s after %d attempts",
        mode, callback, len(_VERIFY_DELAYS),
    )


def _store_subscription(
    db: Session, mode: str, topic: str, callback: str, lease: int, secret: str | None
) -> None:
    sub = _get_sub(db, topic, callback)
    if mode == "subscribe":
        if sub is None:
            logger.debug("WebSub store: new subscription topic=%s callback=%s", topic, callback)
            sub = WebSubSubscription(topic_url=topic, callback_url=callback)
            db.add(sub)
        else:
            logger.debug("WebSub store: refresh subscription topic=%s callback=%s", topic, callback)
        sub.secret = secret
        sub.verified = True
        sub.lease_expires_at = utcnow() + timedelta(seconds=lease)
    elif sub is not None:
        logger.debug("WebSub store: delete subscription topic=%s callback=%s", topic, callback)
        db.delete(sub)
    else:
        logger.debug("WebSub store: unsubscribe for unknown topic=%s callback=%s (noop)", topic, callback)
