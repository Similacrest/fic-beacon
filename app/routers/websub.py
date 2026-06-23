"""Self-hosted WebSub (PubSubHubbub) hub for realtime feed push.

Our feeds advertise <link rel="hub" href="{base}/websub/hub">. A subscriber's hub
(e.g. InoReader) POSTs a subscribe request here; we verify intent by GETting the
callback with a challenge, then store the subscription. On each new drop the publisher
(app/websub/publisher.py) POSTs the Atom body to verified subscribers.

WebSub is a W3C standard and degrades gracefully — readers without it just keep polling.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import WebSubSubscription, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/websub")

_DEFAULT_LEASE = 10 * 24 * 3600  # 10 days when the subscriber doesn't specify one


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
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(callback, params=params)
    except httpx.HTTPError:
        return False
    return resp.status_code // 100 == 2 and challenge in resp.text


@router.post("/hub")
async def hub(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    mode = form.get("hub.mode")
    topic = form.get("hub.topic")
    callback = form.get("hub.callback")
    if mode not in ("subscribe", "unsubscribe") or not topic or not callback:
        raise HTTPException(status_code=400, detail="invalid hub request")
    if not _is_own_topic(topic):
        raise HTTPException(status_code=404, detail="unknown topic")

    lease = int(form.get("hub.lease_seconds") or 0) or _DEFAULT_LEASE
    challenge = secrets.token_urlsafe(32)
    if not await _verify_intent(callback, mode, topic, challenge, lease):
        raise HTTPException(status_code=409, detail="intent verification failed")

    sub = _get_sub(db, topic, callback)
    if mode == "subscribe":
        if sub is None:
            sub = WebSubSubscription(topic_url=topic, callback_url=callback)
            db.add(sub)
        sub.secret = form.get("hub.secret")
        sub.verified = True
        sub.lease_expires_at = utcnow() + timedelta(seconds=lease)
    elif sub is not None:
        db.delete(sub)
    db.commit()
    return Response(status_code=202)
