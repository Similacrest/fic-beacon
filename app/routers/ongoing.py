"""Admin routes for managing ongoing fic RSS feeds (v2 balancing strategy)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Config, OngoingFeed
from app.ongoing.opml import parse_opml
from app.version import __version__

router = APIRouter(prefix="/admin/ongoing")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals["version"] = __version__


@router.get("/", response_class=HTMLResponse)
def ongoing_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    feeds = db.query(OngoingFeed).order_by(OngoingFeed.title).all()
    cfg = db.get(Config, 1)
    ongoing_total = sum(f.estimated_words_per_cycle for f in feeds if f.is_active)
    synthetic_budget = None
    if cfg and cfg.target_total_words:
        synthetic_budget = max(0, cfg.target_total_words - ongoing_total)
    return templates.TemplateResponse(request, "admin/ongoing.html", {
        "feeds": feeds,
        "cfg": cfg,
        "ongoing_total": ongoing_total,
        "synthetic_budget": synthetic_budget,
    })


@router.post("/import-opml")
async def import_opml(
    opml_file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    content = await opml_file.read()
    try:
        entries = parse_opml(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    existing_urls = {f.feed_url for f in db.query(OngoingFeed.feed_url).all()}
    added = 0
    for title, url in entries:
        if url not in existing_urls:
            db.add(OngoingFeed(title=title, feed_url=url))
            existing_urls.add(url)
            added += 1
    db.commit()
    return RedirectResponse(url=f"/admin/ongoing/?imported={added}", status_code=303)


@router.post("/add")
def add_feed(
    feed_url: str = Form(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    existing = db.query(OngoingFeed).filter(OngoingFeed.feed_url == feed_url).first()
    if existing is None:
        db.add(OngoingFeed(title=title.strip() or feed_url, feed_url=feed_url.strip()))
        db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/{feed_id}/toggle")
def toggle_feed(feed_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    feed = db.get(OngoingFeed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404)
    feed.is_active = not feed.is_active
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/{feed_id}/delete")
def delete_feed(feed_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    feed = db.get(OngoingFeed, feed_id)
    if feed:
        db.delete(feed)
        db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/poll-now")
def poll_now(db: Session = Depends(get_db)) -> RedirectResponse:
    from app.ongoing.poller import poll_all_feeds
    poll_all_feeds(db)
    db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)


@router.post("/set-target")
def set_target(
    target_total_words: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cfg = db.get(Config, 1)
    if cfg:
        cfg.target_total_words = target_total_words if target_total_words > 0 else None
        db.commit()
    return RedirectResponse(url="/admin/ongoing/", status_code=303)
