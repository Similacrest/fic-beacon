"""Feedback routes: confirmation interstitial + POST mutation.

All three actions (up / down / extra) follow the same pattern:
  1. GET  /fb/confirm/{token}?action=...  → HTML confirmation page
  2. POST /fb/confirm/{token}?action=...  → apply mutation, redirect to /fb/done

Using a confirm page guards against reader/proxy prefetching bare GET links, which would
silently fire mutations without user intent.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Drop, FeedbackAction
from app.planner.planner import apply_feedback

router = APIRouter(prefix="/fb")

_ACTION_LABELS = {
    "up": ("👍 More like this", "Increase this book's share of the reading budget."),
    "down": ("👎 Drop this book", "Reduce this book's priority (may drop it from rotation)."),
    "extra": ("➕ Extra chapter now", "Post an additional chapter for this book right now."),
}


def _get_drop(token: str, db: Session) -> Drop:
    drop = db.query(Drop).filter(Drop.feedback_token == token).first()
    if drop is None:
        raise HTTPException(status_code=404, detail="Unknown feedback token")
    return drop


@router.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_get(
    token: str,
    action: str = Query(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if action not in _ACTION_LABELS:
        raise HTTPException(status_code=400, detail="Unknown action")
    drop = _get_drop(token, db)
    label, description = _ACTION_LABELS[action]
    book_title = drop.book.title
    return HTMLResponse(_confirm_page(token, action, label, description, book_title))


@router.post("/confirm/{token}", response_class=RedirectResponse)
def confirm_post(
    token: str,
    action: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if action not in _ACTION_LABELS:
        raise HTTPException(status_code=400, detail="Unknown action")
    drop = _get_drop(token, db)
    apply_feedback(db, drop, FeedbackAction(action), settings.calibre_library_path)
    db.commit()
    return RedirectResponse(url="/fb/done", status_code=303)


@router.get("/done", response_class=HTMLResponse)
def done() -> HTMLResponse:
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:2em'>"
        "<p>✅ Feedback recorded. You can close this tab.</p>"
        "</body></html>"
    )


def _confirm_page(
    token: str, action: str, label: str, description: str, book_title: str
) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Confirm Feedback</title>
<style>
  body {{ font-family: sans-serif; max-width: 480px; margin: 4em auto; padding: 1em; }}
  button {{ font-size: 1.1em; padding: .5em 1.5em; cursor: pointer; }}
  .book {{ font-style: italic; }}
</style>
</head>
<body>
  <h2>{label}</h2>
  <p class="book">for <strong>{book_title}</strong></p>
  <p>{description}</p>
  <form method="post" action="/fb/confirm/{token}">
    <input type="hidden" name="action" value="{action}">
    <button type="submit">{label}</button>
    &nbsp;
    <a href="javascript:history.back()">Cancel</a>
  </form>
</body>
</html>"""
