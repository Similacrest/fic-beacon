"""Feedback routes.

Four ordered actions per drop — a symmetric strength scale:

    🪝 extra (super-up)  ·  👍 up  ·  👎 down  ·  ❌ drop (super-down)

Light votes fire instantly so they're frictionless from inside a reader:
  GET /fb/{token}?action=up|down   → apply immediately, show a tiny "recorded" page.

Strong/destructive actions keep a one-tap confirmation interstitial, which also guards
against reader/proxy prefetching bare GET links:
  GET  /fb/confirm/{token}?action=extra|drop  → confirmation page
  POST /fb/confirm/{token}                     → apply mutation, redirect to /fb/done

apply_feedback() is additionally idempotent per (drop, action), so even a prefetched
up/down counts at most once.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Drop, FeedbackAction
from app.planner.planner import apply_feedback

router = APIRouter(prefix="/fb")

# Actions that mutate instantly on a bare GET (no confirmation page).
_INSTANT_ACTIONS = {"up", "down"}

# Strong/destructive actions that require a confirmation interstitial.
_CONFIRM_LABELS = {
    "extra": ("🪝 Extra chapter now", "Post an extra chapter now and strongly boost this source."),
    "drop": ("❌ Drop this source", "Remove this source from the rotation immediately."),
}

_DONE_HTML = (
    "<html><body style='font-family:sans-serif;padding:2em'>"
    "<p>✅ Feedback recorded. You can close this tab.</p>"
    "</body></html>"
)


def _get_drop(token: str, db: Session) -> Drop:
    drop = db.query(Drop).filter(Drop.feedback_token == token).first()
    if drop is None:
        raise HTTPException(status_code=404, detail="Unknown feedback token")
    return drop


@router.get("/done", response_class=HTMLResponse)
def done() -> HTMLResponse:
    return HTMLResponse(_DONE_HTML)


@router.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_get(
    token: str,
    action: str = Query(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if action not in _CONFIRM_LABELS:
        raise HTTPException(status_code=400, detail="Unknown action")
    drop = _get_drop(token, db)
    label, description = _CONFIRM_LABELS[action]
    return HTMLResponse(_confirm_page(token, action, label, description, drop.book.title))


@router.post("/confirm/{token}", response_class=RedirectResponse)
def confirm_post(
    token: str,
    action: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if action not in _CONFIRM_LABELS:
        raise HTTPException(status_code=400, detail="Unknown action")
    drop = _get_drop(token, db)
    apply_feedback(db, drop, FeedbackAction(action), settings.calibre_library_path)
    db.commit()
    return RedirectResponse(url="/fb/done", status_code=303)


@router.get("/{token}", response_class=HTMLResponse)
def instant_get(
    token: str,
    action: str = Query(...),
    db: Session = Depends(get_db),
):
    """Instant up/down via bare GET. extra/drop are redirected to their confirm page."""
    if action in _CONFIRM_LABELS:
        return RedirectResponse(url=f"/fb/confirm/{token}?action={action}", status_code=303)
    if action not in _INSTANT_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action")
    drop = _get_drop(token, db)
    apply_feedback(db, drop, FeedbackAction(action), settings.calibre_library_path)
    db.commit()
    return HTMLResponse(_DONE_HTML)


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
