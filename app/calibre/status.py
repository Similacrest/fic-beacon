"""Classify a Calibre source's publication status (the **#status** custom column).

The user keeps a hierarchical `#status` column whose values track the source site's state.
FanFicFare auto-sets the first four; the rest are set manually:

    In-Progress, Incomplete, Hiatus, Abandoned   (auto-set), Completed, Published, —

We collapse those into three intents that drive Library import routing and the fetch sweep:

  - **updating** — still gaining chapters (In-Progress / Incomplete / Hiatus). Library import
    routes these to a *tracked* (auto-updating) source.
  - **done** — finished or shelved (Completed / Abandoned / Published). Library import routes
    these to the *backlog* queue, and the feedless sweep/poller skips re-fetching them.
  - **unknown** — blank / "—" / anything unrecognised. Treated as backlog on import, and never
    skipped on fetch (a tracked story whose status we don't know is still polled).
"""
from __future__ import annotations

# Match case-insensitively; synonyms cover other sites' wording (AO3 "Complete",
# RoyalRoad "Ongoing"/"Dropped") so the classifier is robust to non-FanFicFare imports.
_UPDATING = {"in-progress", "in progress", "incomplete", "hiatus", "ongoing", "active"}
_DONE = {"completed", "complete", "abandoned", "dropped", "published"}


def classify_status(status: str | None) -> str:
    """Return "updating", "done", or "unknown" for a raw #status value."""
    if not status:
        return "unknown"
    key = status.strip().lower()
    if key in _UPDATING:
        return "updating"
    if key in _DONE:
        return "done"
    return "unknown"


def is_updating(status: str | None) -> bool:
    return classify_status(status) == "updating"


def is_done(status: str | None) -> bool:
    return classify_status(status) == "done"
