from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.feed.builder import build_channel_slot_feed
from app.models import Channel, Config

router = APIRouter()


def _check_feed_secret(session: Session, token: str) -> None:
    cfg = session.get(Config, 1)
    expected = cfg.feed_secret if cfg else settings.feed_secret
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Invalid feed token")


@router.api_route("/feed/{channel_slug}/{feed_key}", methods=["GET", "HEAD"])
def get_feed_slot(
    channel_slug: str,
    feed_key: str,
    token: str = Query(..., description="Feed secret token"),
    fmt: str = Query("atom", description="atom or rss"),
    db: Session = Depends(get_db),
) -> Response:
    """One feed per numbered slot — both EPUB backlog and ongoing serials occupy slots."""
    _check_feed_secret(db, token)
    channel = db.query(Channel).filter(Channel.slug == channel_slug).first()
    if channel is None:
        raise HTTPException(status_code=404, detail="Unknown channel")
    # Only the channel's real slots (1..parallel_slots) are valid feeds; anything above the
    # limit (or non-numeric) has no bucket and must not resolve.
    if not feed_key.isdigit() or not (1 <= int(feed_key) <= channel.parallel_slots):
        raise HTTPException(status_code=404, detail="No such slot in this channel")
    # Shared builder (also used by the WebSub publisher) so a pushed body byte-matches this GET.
    _, atom_xml, rss_xml = build_channel_slot_feed(db, channel, feed_key, token)
    if fmt == "rss":
        return Response(content=rss_xml, media_type="application/rss+xml; charset=utf-8")
    return Response(content=atom_xml, media_type="application/atom+xml; charset=utf-8")
