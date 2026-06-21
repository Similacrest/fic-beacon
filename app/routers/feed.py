from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.feed.builder import build_feed
from app.models import Config, Drop

router = APIRouter()


def _check_feed_secret(session: Session, token: str) -> None:
    cfg = session.get(Config, 1)
    expected = cfg.feed_secret if cfg else settings.feed_secret
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Invalid feed token")


@router.get("/feed")
def get_feed_atom(
    token: str = Query(..., description="Feed secret token"),
    fmt: str = Query("atom", description="atom or rss"),
    db: Session = Depends(get_db),
) -> Response:
    _check_feed_secret(db, token)
    drops = (
        db.query(Drop)
        .join(Drop.book)
        .order_by(Drop.published_at.desc())
        .limit(settings.feed_item_limit)
        .all()
    )
    atom_xml, rss_xml = build_feed(drops)
    if fmt == "rss":
        return Response(content=rss_xml, media_type="application/rss+xml; charset=utf-8")
    return Response(content=atom_xml, media_type="application/atom+xml; charset=utf-8")
