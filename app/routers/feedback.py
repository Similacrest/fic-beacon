"""Feedback routes.

Per-drop actions along a strength scale plus two utility actions:

    🪝 extra (super-up)  ·  👍 up  ·  👎 down  ·  ⏸ pause  ·  ❌ drop (super-down)  ·  ✓ read

Light/reversible actions fire instantly so they're frictionless from inside a reader:
  GET /fb/{token}?action=up|down|pause|read  → apply immediately, show a tiny "recorded" page.
  (pause is reversible from the dashboard; read is a neutral read-acknowledgement.)

Strong/destructive actions keep a one-tap confirmation interstitial, which also guards
against reader/proxy prefetching bare GET links:
  GET  /fb/confirm/{token}?action=extra|drop  → confirmation page
  POST /fb/confirm/{token}                     → apply mutation, redirect to /fb/done

apply_feedback() is additionally idempotent per (drop, action), so even a prefetched
action counts at most once.

🪝 extra is rate-limited per channel per release cycle (config.extra_per_channel_per_cycle):
an over-limit click gets an explanatory page and changes nothing (see extra_limit_reached).
"""
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Channel, Drop, FeedbackAction
from app.planner.planner import apply_feedback, extra_limit_reached

router = APIRouter(prefix="/fb")

# Actions that mutate instantly on a bare GET (no confirmation page). Pause is reversible
# (resume from the dashboard) and read is a neutral acknowledgement, so both are frictionless
# like up/down.
_INSTANT_ACTIONS = {"up", "down", "pause", "read"}

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
    if action == "extra" and extra_limit_reached(db, drop):
        return _extra_limit_response(db, drop)
    label, description = _CONFIRM_LABELS[action]
    return HTMLResponse(_confirm_page(token, action, label, description, drop.book.title))


@router.post("/confirm/{token}", response_class=RedirectResponse)
def confirm_post(
    token: str,
    action: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    if action not in _CONFIRM_LABELS:
        raise HTTPException(status_code=400, detail="Unknown action")
    drop = _get_drop(token, db)
    if action == "extra" and extra_limit_reached(db, drop):
        # Refuse *before* apply_feedback records the FeedbackEvent (see extra_limit_reached).
        return _extra_limit_response(db, drop)
    extra_drop = apply_feedback(db, drop, FeedbackAction(action), settings.calibre_library_path)
    db.commit()
    if extra_drop is not None:
        from app.websub.publisher import publish_updates
        publish_updates(db, [extra_drop])
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


def _extra_limit_response(db: Session, drop: Drop) -> HTMLResponse:
    """The 🪝 allowance for this channel is spent — say so and when the next release lands."""
    from app import scheduler
    channel = db.get(Channel, drop.book.channel_id)
    nxt = scheduler.next_release_time()
    when = f" The next release is {nxt.strftime('%a %H:%M')}." if nxt else ""
    name = escape(channel.name if channel else "this channel")
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;max-width:480px;margin:4em auto;padding:1em'>"
        f"<h2>🪝 No extra chapters left for {name}</h2>"
        f"<p>You've used this channel's extra-chapter allowance until the next release.{when}</p>"
        "<p>Nothing was changed.</p></body></html>"
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
