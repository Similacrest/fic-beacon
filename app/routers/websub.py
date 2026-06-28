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


async def _verify_intent(
    callback: str, mode: str, topic: str, challenge: str, lease: int,
    verify_token: str | None = None,
) -> tuple[bool, str]:
    """WebSub verification of intent: GET the callback; it must echo the challenge.

    Returns (ok, detail). `detail` summarises the outcome (status / body snippet / error)
    so the caller can log *why* a verification failed without re-issuing the request.

    Echoes back `hub.verify_token` when the subscriber supplied one: PubSubHubbub 0.3
    (which Inoreader/Superfeedr use) has the subscriber match a pending subscription on
    *both* hub.topic AND hub.verify_token before echoing the challenge — drop the token
    and it returns a bare 200 with no challenge (the empty-body 200s we saw).
    """
    params = {
        "hub.mode": mode,
        "hub.topic": topic,
        "hub.challenge": challenge,
        "hub.lease_seconds": str(lease),
    }
    if verify_token:
        params["hub.verify_token"] = verify_token
    # MERGE our verification params into the callback's existing query string — do NOT pass
    # `params=` to client.get(), which (httpx ≥0.28) *replaces* the callback's query. Readers
    # like Inoreader key their pending verification on params already in the callback URL
    # (e.g. ?feed_id=…&hub_id=…); clobbering those makes the callback unable to match the
    # request and it answers a bare empty 200 (verification silently fails).
    target = httpx.URL(callback).copy_merge_params(params)
    logger.debug("WebSub verify → GET %s", target)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(target)
    except httpx.HTTPError as exc:
        return False, f"request error: {exc!r}"
    body = resp.text
    echo = challenge in body
    detail = f"status={resp.status_code} len={len(body)} echo={echo} body[:300]={body[:300]!r}"
    logger.debug("WebSub verify ← %s %s", callback, detail)
    if resp.status_code // 100 != 2:
        return False, detail
    if not echo:
        return False, detail
    return True, detail


def _preferred_verify_mode(form) -> str:
    """The subscriber's preferred verification mode from `hub.verify` (PuSH 0.3).

    `hub.verify` lists "sync"/"async" in preference order (repeated fields and/or a
    comma-separated value). We support both, so honour the first one listed. WebSub (0.4)
    drops the field — absent → async. Inoreader/Superfeedr send `sync`, and a sync
    subscriber only keeps its verification callback armed *during* its subscribe request,
    so it must be verified inline (a deferred async callback arrives too late → empty 200).
    """
    flat: list[str] = []
    for value in form.getlist("hub.verify"):
        flat.extend(p.strip() for p in value.split(",") if p.strip())
    for m in flat:
        if m in ("sync", "async"):
            return m
    return "async"


@router.post("/hub")
async def hub(request: Request, background: BackgroundTasks) -> Response:
    """Accept a (un)subscribe request and verify intent (PuSH 0.3 sync / WebSub async).

    Validate what we can up front (bad mode / foreign topic → 4xx). Then honour the
    subscriber's `hub.verify` preference: **sync** subscribers (Inoreader) are verified
    inline — call their callback while their subscribe request is still open, then return
    `204`; **async** subscribers get an immediate `202` and an out-of-band callback.
    """
    form = await request.form()
    mode = form.get("hub.mode")
    topic = form.get("hub.topic")
    callback = form.get("hub.callback")
    lease_raw = form.get("hub.lease_seconds")
    verify_token = form.get("hub.verify_token")
    secret = form.get("hub.secret")
    verify_mode = _preferred_verify_mode(form)
    # Log every field (key + value) so a required/unexpected param is never dropped unseen.
    logger.debug("WebSub hub ← form=%s", {k: form.get(k) for k in form.keys()})
    logger.debug(
        "WebSub hub ← mode=%r topic=%r callback=%r lease=%r verify=%s verify_token=%r secret=%s",
        mode, topic, callback, lease_raw, verify_mode, verify_token, bool(secret),
    )
    if mode not in ("subscribe", "unsubscribe") or not topic or not callback:
        logger.debug("WebSub hub → 400 (mode/topic/callback invalid)")
        raise HTTPException(status_code=400, detail="invalid hub request")
    if not _is_own_topic(topic):
        logger.debug("WebSub hub → 404 (foreign topic %r)", topic)
        raise HTTPException(status_code=404, detail="unknown topic")

    lease = int(lease_raw or 0) or _DEFAULT_LEASE

    if verify_mode == "sync":
        # Verify now, while the subscriber's request is open and its callback is armed.
        challenge = secrets.token_urlsafe(32)
        logger.debug("WebSub hub: sync verify (mode=%s lease=%ds) for %s", mode, lease, callback)
        ok, detail = await _verify_intent(callback, mode, topic, challenge, lease, verify_token)
        if not ok:
            logger.warning(
                "WebSub sync %s verification failed for %s; %s", mode, callback, detail
            )
            raise HTTPException(status_code=409, detail="intent verification failed")
        with db_session() as db:
            _store_subscription(db, mode, topic, callback, lease, secret)
            db.commit()
        logger.info("WebSub %s verified (sync) for %s", mode, callback)
        return Response(status_code=204)

    logger.debug(
        "WebSub hub → 202; scheduled async verify (mode=%s lease=%ds) for %s", mode, lease, callback
    )
    background.add_task(
        _verify_and_store, mode, topic, callback, lease, secret, verify_token
    )
    return Response(status_code=202)


async def _verify_and_store(
    mode: str, topic: str, callback: str, lease: int, secret: str | None,
    verify_token: str | None = None,
) -> None:
    """Verify intent against the subscriber's callback, then persist (own DB session).

    Runs after the 202 has been sent. Verification is retried with backoff to absorb the
    arming race (a subscriber may not be ready for our callback the instant it gets the
    202). A still-failing verification is logged and dropped — there's no live request to
    return an error to.
    """
    challenge = secrets.token_urlsafe(32)
    last_detail = ""
    for attempt, delay in enumerate(_VERIFY_DELAYS, start=1):
        if delay:
            logger.debug(
                "WebSub verify backoff %.1fs before attempt %d for %s", delay, attempt, callback
            )
            await asyncio.sleep(delay)
        logger.debug(
            "WebSub verify attempt %d/%d (%s) for %s", attempt, len(_VERIFY_DELAYS), mode, callback
        )
        ok, last_detail = await _verify_intent(
            callback, mode, topic, challenge, lease, verify_token
        )
        if ok:
            with db_session() as db:
                _store_subscription(db, mode, topic, callback, lease, secret)
                db.commit()
            logger.info("WebSub %s verified for %s (attempt %d)", mode, callback, attempt)
            return
    # Surface the last response at WARNING (visible at the default INFO level) so the
    # reason is diagnosable without re-running under DEBUG.
    logger.warning(
        "WebSub %s verification failed for callback %s after %d attempts; last %s",
        mode, callback, len(_VERIFY_DELAYS), last_detail,
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
