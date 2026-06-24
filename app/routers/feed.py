from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.feed.builder import build_feed
from app.models import Channel, Config, Drop

router = APIRouter()


def _check_feed_secret(session: Session, token: str) -> None:
    cfg = session.get(Config, 1)
    expected = cfg.feed_secret if cfg else settings.feed_secret
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Invalid feed token")


def _render(drops, fmt: str, self_url=None, title=None, description=None) -> Response:
    atom_xml, rss_xml = build_feed(drops, self_url=self_url, title=title, description=description)
    if fmt == "rss":
        return Response(content=rss_xml, media_type="application/rss+xml; charset=utf-8")
    return Response(content=atom_xml, media_type="application/atom+xml; charset=utf-8")


@router.get("/feed/{channel_slug}/{feed_key}")
def get_feed_slot(
    channel_slug: str,
    feed_key: str,
    token: str = Query(..., description="Feed secret token"),
    fmt: str = Query("atom", description="atom or rss"),
    db: Session = Depends(get_db),
) -> Response:
    """One feed per slot: numbered backlog slot, or 'ongoing' serial feed for the channel."""
    _check_feed_secret(db, token)
    channel = db.query(Channel).filter(Channel.slug == channel_slug).first()
    if channel is None:
        raise HTTPException(status_code=404, detail="Unknown channel")
    drops = (
        db.query(Drop)
        .join(Drop.book)
        .filter(Drop.channel_id == channel.id, Drop.feed_key == feed_key)
        .order_by(Drop.published_at.desc())
        .limit(settings.feed_item_limit)
        .all()
    )
    slot_label = "In progress" if feed_key == "ongoing" else f"Slot {feed_key}"
    self_url = f"{settings.base_url}/feed/{channel_slug}/{feed_key}"
    title = f"Fic Beacon — {channel.name} · {slot_label}"
    return _render(drops, fmt, self_url=self_url, title=title,
                   description=f"{channel.name} — {slot_label}")
